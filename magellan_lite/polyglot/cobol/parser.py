"""A structural COBOL parser: enough grammar to build the graph, not to compile.

What it recovers, per program:

* the unit structure: ``PROGRAM-ID`` (nested programs and ``END PROGRAM``), the four
  divisions, ``FILE-CONTROL`` ``SELECT`` entries, ``PROCEDURE DIVISION USING``;
* the DATA DIVISION record tree: levels 01-49, 66 RENAMES, 77, 88 condition names, with
  PIC, USAGE, OCCURS, REDEFINES, VALUE, SIGN, and a computed byte size and offset;
* COPY expansion, as the compiler does it (``REPLACING`` included), remembering for every
  token which copybook it came from, so copybook items can be shared nodes;
* sections and paragraphs, and inside them the statements that create edges: PERFORM
  (THRU, inline, loops), GO TO (DEPENDING), ALTER, CALL ... USING, ENTRY, the data each
  statement reads and writes, file I/O, EXEC SQL (tables, host variables, cursors) and
  EXEC CICS (LINK/XCTL, file and queue I/O, HANDLE CONDITION labels), EVALUATE facts.

It never raises on odd input: anything it does not understand is skipped, and
:func:`parse_program_file` reports problems as strings.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable

from magellan_lite.polyglot.cobol.source import Source, Token, canonical_pic, pic_runs, read, tokenize

# --------------------------------------------------------------------------
VERBS = frozenset("""
ACCEPT ADD ALLOCATE ALTER CALL CANCEL CLOSE COMMIT COMPUTE CONTINUE DELETE DISABLE DISPLAY
DIVIDE ENABLE ENTER ENTRY EVALUATE EXAMINE EXEC EXECUTE EXHIBIT EXIT FREE GENERATE GO GOBACK
IF INITIALIZE INITIATE INSPECT INVOKE JSON MERGE MOVE MULTIPLY NEXT NOTE OPEN PERFORM PURGE
RAISE READ READY RECEIVE RELEASE RESET RESUME RETURN REWRITE ROLLBACK SEARCH SEND SERVICE
SET SORT START STOP STRING SUBTRACT SUPPRESS TERMINATE TRACE TRANSFORM UNLOCK UNSTRING USE
VALIDATE WRITE XML
""".split())
#: words that end the current statement without starting a new verb
STRUCTURE = frozenset({"ELSE", "WHEN", "THEN"})
PHRASE_START = frozenset({"AT", "NOT", "INVALID", "ON", "END-OF-PAGE", "EOP", "OVERFLOW",
                          "EXCEPTION"})
OPENERS = frozenset({"IF", "EVALUATE", "SEARCH"})
FIGURATIVE = frozenset("""
ZERO ZEROS ZEROES SPACE SPACES HIGH-VALUE HIGH-VALUES LOW-VALUE LOW-VALUES QUOTE QUOTES
NULL NULLS ALL TRUE FALSE
""".split())
USAGES = {
    "COMP": "BINARY", "COMPUTATIONAL": "BINARY", "COMP-4": "BINARY",
    "COMPUTATIONAL-4": "BINARY", "BINARY": "BINARY", "COMP-5": "COMP-5",
    "COMPUTATIONAL-5": "COMP-5", "COMP-3": "PACKED", "COMPUTATIONAL-3": "PACKED",
    "PACKED-DECIMAL": "PACKED", "COMP-1": "COMP-1", "COMPUTATIONAL-1": "COMP-1",
    "COMP-2": "COMP-2", "COMPUTATIONAL-2": "COMP-2", "DISPLAY": "DISPLAY",
    "DISPLAY-1": "DISPLAY-1", "INDEX": "INDEX", "POINTER": "POINTER",
    "PROCEDURE-POINTER": "PROCEDURE-POINTER", "FUNCTION-POINTER": "FUNCTION-POINTER",
    "NATIONAL": "NATIONAL", "COMP-X": "COMP-X", "FLOAT-SHORT": "COMP-1",
    "FLOAT-LONG": "COMP-2", "UTF-8": "UTF-8",
}
DATA_CLAUSES = frozenset({
    "REDEFINES", "PIC", "PICTURE", "USAGE", "OCCURS", "VALUE", "VALUES", "SIGN", "LEADING",
    "TRAILING", "JUST", "JUSTIFIED", "SYNC", "SYNCHRONIZED", "BLANK", "GLOBAL", "EXTERNAL",
    "RENAMES", "IS", "BASED", "GROUP-USAGE", "DYNAMIC", "TYPE", "TYPEDEF",
} | set(USAGES))
TERMINATORS = frozenset({"GOBACK", "STOP-RUN", "EXIT-PROGRAM", "CICS-RETURN", "CICS-XCTL"})
#: statements and features that mark code for modernization
OBSOLETE = {
    "ALTER": "alter", "EXAMINE": "examine", "TRANSFORM": "transform", "EXHIBIT": "exhibit",
    "READY": "ready-trace", "RESET": "reset-trace", "NOTE": "note", "ENTER": "enter",
}


# --------------------------------------------------------------------------
@dataclass(eq=False)          # an item is itself: two FILLERs with equal fields are two items
class DataItem:
    level: int
    name: str
    line: int
    col: int
    path: str
    copy: str = ""
    shared: bool = True
    section: str = ""
    pic: str = ""
    usage: str = ""
    occurs: tuple | None = None          # (min, max, depending-name)
    redefines: str = ""
    renames: tuple = ()
    values: list[str] = field(default_factory=list)
    sign: str = ""
    flags: list[str] = field(default_factory=list)
    indexes: list[str] = field(default_factory=list)
    fd: str = ""
    children: list["DataItem"] = field(default_factory=list)
    parent: "DataItem | None" = None
    size: int | None = None
    offset: int | None = None
    #: which copybook owns the node ("" = the program); set by assign_owners
    owner: str = ""
    rel: tuple = ()
    end_line: int = 0

    @property
    def is_filler(self) -> bool:
        return self.name == "FILLER"

    @property
    def is_condition(self) -> bool:
        return self.level == 88

    def ancestors(self) -> list["DataItem"]:
        out, p = [], self.parent
        while p is not None:
            out.append(p)
            p = p.parent
        return out

    def own_layout(self) -> str:
        occ = ""
        if self.occurs:
            lo, hi, dep = self.occurs
            occ = f"OCCURS {lo}" + (f" TO {hi}" if hi != lo else "") + (" DEPENDING" if dep else "")
        parts = [self.pic and f"PIC {self.pic}", self.usage and self.usage != "DISPLAY"
                 and self.usage, occ, self.redefines and f"REDEFINES {self.redefines}",
                 self.renames and "RENAMES " + " THRU ".join(self.renames),
                 self.sign, " ".join(self.flags)]
        return " ".join(p for p in parts if p)

    def layout(self, depth: int = 0) -> str:
        """Everything a program compiled against this item depends on: shape, not names."""
        kids = [c for c in self.children if not c.is_condition]
        inner = ";".join(c.layout(depth + 1) for c in kids)
        return f"{depth}:{self.own_layout()}" + (f"{{{inner}}}" if kids else "")


@dataclass
class FileDef:
    name: str
    line: int
    path: str
    assign: str = ""
    organization: str = ""
    access: str = ""
    keys: list[str] = field(default_factory=list)
    status: str = ""
    records: list[str] = field(default_factory=list)
    clauses: str = ""


@dataclass
class Ref:
    chain: tuple
    access: str          # "r" | "w" | "rw"
    line: int
    col: int
    path: str
    cond: bool
    verb: str = ""       # the statement's verb (MOVE, COMPUTE, ...), when known
    stmt: tuple = ()     # (path, line, col) of the statement, to pair a MOVE's operands


@dataclass
class Paragraph:
    name: str
    section: str
    is_section: bool
    line: int
    col: int
    path: str
    end_line: int = 0
    tokens: list[Token] = field(default_factory=list)
    performs: list[dict] = field(default_factory=list)
    gotos: list[dict] = field(default_factory=list)
    alters: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    io: list[dict] = field(default_factory=list)
    sql: list[dict] = field(default_factory=list)
    cics: list[dict] = field(default_factory=list)
    evaluates: list[dict] = field(default_factory=list)
    entries: list[dict] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    terminal: bool = False
    statements: int = 0
    copy: str = ""


@dataclass
class CopyRef:
    name: str
    key: str             # resolved copybook key, "" when not found
    line: int
    col: int
    path: str
    replacing: bool
    context: str = ""    # division it appeared in (filled later)


@dataclass
class Program:
    name: str
    path: str
    line: int
    end_line: int = 0
    parent: str = ""
    literal_name: str = ""
    using: list[tuple[str, tuple]] = field(default_factory=list)   # (mode, chain)
    returning: tuple = ()
    items: list[DataItem] = field(default_factory=list)            # roots in order
    files: dict[str, FileDef] = field(default_factory=dict)
    paragraphs: list[Paragraph] = field(default_factory=list)
    preamble: Paragraph | None = None
    copies: list[CopyRef] = field(default_factory=list)
    env_tokens: list[Token] = field(default_factory=list)
    header_tokens: list[Token] = field(default_factory=list)
    cursors: dict[str, list[str]] = field(default_factory=dict)
    ws_sql: list[dict] = field(default_factory=list)
    has_procedure: bool = False
    flags: set[str] = field(default_factory=set)

    def all_items(self) -> list[DataItem]:
        out: list[DataItem] = []

        def walk(it: DataItem) -> None:
            out.append(it)
            for c in it.children:
                walk(c)
        for r in self.items:
            walk(r)
        return out


# --------------------------------------------------------------------------
# copybook library and COPY expansion
# --------------------------------------------------------------------------
class Library:
    """Copybooks by member name. Lookup prefers the copy closest to the including file."""

    def __init__(self) -> None:
        self.by_name: dict[str, list[str]] = {}
        self.sources: dict[str, Source] = {}
        self.keys: dict[str, str] = {}             # path -> key
        self.loader: Callable[[str], str | None] | None = None
        self._tokens: dict[str, list[Token]] = {}

    def add(self, name: str, path: str) -> None:
        self.by_name.setdefault(name.upper(), []).append(path)

    def finalize(self) -> None:
        """Give each copybook path a unique key: ``copy:`` + its member name, for the
        shallowest copy.

        Adding a second copy of a member elsewhere (a sample tree, a backup directory)
        must not rename the one that was already there, or every node under it would
        look deleted. Copybooks and programs are separate namespaces (COPY names a
        library member, CALL a load module), and a CICS program's COMMAREA copybook is
        usually named after the program, so the prefix keeps a program appearing or
        going away from renaming its copybook.
        """
        for name, paths in self.by_name.items():
            paths.sort(key=lambda p: (p.count("/"), p))
            for k, p in enumerate(paths):
                self.keys[p] = COPYBOOK_KEY + (name if k == 0 else alt_key(name, p))

    def find(self, name: str, near: str) -> str | None:
        paths = self.by_name.get(name.upper())
        if not paths:
            return None
        if len(paths) == 1:
            return paths[0]
        here = PurePosixPath(near).parts

        def closeness(p: str) -> tuple[int, str]:
            parts = PurePosixPath(p).parts
            common = 0
            for a, b in zip(here, parts):
                if a != b:
                    break
                common += 1
            return (-common, p)
        return min(paths, key=closeness)

    def source(self, path: str) -> Source | None:
        if path not in self.sources:
            text = self.loader(path) if self.loader else None
            self.sources[path] = read(text, path) if text is not None else None
        return self.sources[path]

    def tokens(self, path: str) -> list[Token]:
        """The member's tokens as written, tokenized once per build.

        A copybook is COPYed by many programs; each splice clones these (the clones get
        their own ``copy``/``shared`` marks) instead of tokenizing the text again.
        """
        if path not in self._tokens:
            src = self.source(path)
            self._tokens[path] = tokenize(src) if src is not None else []
        return self._tokens[path]


def alt_key(name: str, path: str) -> str:
    """Key for a second definition of ``name``: qualified by where it lives."""
    stem = PurePosixPath(path).with_suffix("").as_posix()
    return f"{name}@" + re.sub(r"[^A-Za-z0-9_-]", "_", stem)


#: prefix of every copybook key (``mod:cobol@copy:CUSTREC``); no COBOL name has a colon
COPYBOOK_KEY = "copy:"


def member_name(key: str) -> str:
    """The copybook member a key stands for: ``copy:CUSTREC@samples_CUSTREC`` -> CUSTREC."""
    return key.removeprefix(COPYBOOK_KEY).split("@", 1)[0]


_REPL_SPLIT = re.compile(r"\s+")


def _pseudo_text(toks: list[Token], i: int) -> tuple[str, int]:
    """Text between ``==`` delimiters (or one token) starting at ``i``; returns (text, next)."""
    if i < len(toks) and toks[i].text == "==":
        j = i + 1
        parts: list[str] = []
        while j < len(toks) and toks[j].text != "==":
            parts.append(toks[j].text)
            j += 1
        return _join_tokens(toks[i + 1:j]), j + 1
    if i < len(toks):
        return toks[i].text, i + 1
    return "", i


def _join_tokens(toks: list[Token]) -> str:
    """Rebuild source text from tokens, keeping adjacency (``(PFX)-NAME``)."""
    out = ""
    prev = None
    for t in toks:
        if prev is not None and not (prev.line == t.line and prev.col + len(prev.text) == t.col):
            out += " "
        out += t.text
        prev = t
    return out


def _replacer(pairs: list[tuple[str, str, str]]) -> Callable[[str], str]:
    compiled = []
    for mode, old, new in pairs:
        old = old.strip()
        if not old:
            continue
        esc = r"\s+".join(re.escape(p) for p in _REPL_SPLIT.split(old))
        word_like = re.fullmatch(r"[A-Za-z0-9_\-]+", old) is not None
        if not mode and word_like and (old.endswith("-") or old.startswith("-")):
            # A COBOL word can't begin or end with a hyphen, so ==ACCT-== can never match a
            # whole word: it can only mean a prefix (==-BAL==, a suffix). The compilers that
            # accept it treat it so; read as a whole word it silently renamed nothing.
            mode = "LEADING" if old.endswith("-") else "TRAILING"
        if mode == "LEADING":
            rx = re.compile(r"(?<![A-Za-z0-9_\-])" + esc, re.I)
        elif mode == "TRAILING":
            rx = re.compile(esc + r"(?![A-Za-z0-9_\-])", re.I)
        elif word_like:
            rx = re.compile(r"(?<![A-Za-z0-9_\-])" + esc + r"(?![A-Za-z0-9_\-])", re.I)
        else:
            rx = re.compile(esc, re.I)
        compiled.append((rx, new))

    def apply(line: str) -> str:
        for rx, new in compiled:
            line = rx.sub(lambda _m: new, line)
        return line
    return apply


def expand(tokens: list[Token], lib: Library, near: str, copies: list[CopyRef],
           missing: set[str], depth: int = 0, chain: tuple = (),
           shared: bool = True) -> list[Token]:
    """Splice copybooks into ``tokens``, as the compiler's library phase does."""
    out: list[Token] = []
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        up = t.up
        is_copy = t.kind == "WORD" and up == "COPY" and i + 1 < n \
            and tokens[i + 1].kind in ("WORD", "LIT")
        is_include = (t.kind == "WORD" and up in ("EXEC", "EXECUTE") and i + 3 < n
                      and tokens[i + 1].up == "SQL" and tokens[i + 2].up == "INCLUDE")
        if not (is_copy or is_include):
            out.append(t)
            i += 1
            continue
        if is_include:
            name = tokens[i + 3].text.strip("'\"").upper()
            j = i + 4
            while j < n and tokens[j].up != "END-EXEC":
                j += 1
            j += 1
            if j < n and tokens[j].kind == "PERIOD":
                j += 1
            pairs: list[tuple[str, str, str]] = []
        else:
            name = tokens[i + 1].text.strip("'\"").upper()
            j = i + 2
            if j + 1 < n and tokens[j].up in ("OF", "IN"):
                j += 2
            if j < n and tokens[j].up == "SUPPRESS":
                j += 1
            pairs = []
            if j < n and tokens[j].up == "REPLACING":
                j += 1
                while j < n and tokens[j].kind != "PERIOD":
                    mode = ""
                    if tokens[j].up in ("LEADING", "TRAILING"):
                        mode = tokens[j].up
                        j += 1
                    old, j = _pseudo_text(tokens, j)
                    if j < n and tokens[j].up == "BY":
                        j += 1
                    new, j = _pseudo_text(tokens, j)
                    pairs.append((mode, old, new))
            while j < n and tokens[j].kind != "PERIOD":
                j += 1
            j += 1                            # the COPY statement's own period
        found = lib.find(name, near)
        key = lib.keys[found] if found else ""
        copies.append(CopyRef(name, key, t.line, t.col, t.path, bool(pairs)))
        if found and key not in chain and depth < 12:
            src = lib.source(found)
            if src is not None:
                keep = shared and not pairs
                if pairs:
                    fix = _replacer(pairs)
                    lines = [type(cl)(cl.lineno, cl.col, fix(cl.text), cl.path)
                             for cl in src.lines]
                    inner = tokenize(Source(src.path, src.fmt, lines, src.comments,
                                            src.normalized))
                    for tok in inner:
                        tok.copy, tok.shared = key, keep
                else:
                    inner = [tok.clone(key, keep) for tok in lib.tokens(found)]
                sub_copies: list[CopyRef] = []
                inner = expand(inner, lib, found, sub_copies, missing, depth + 1,
                               chain + (key,), keep)
                for c in sub_copies:
                    c.context = "nested"
                copies.extend(sub_copies)
                out.extend(inner)
        elif not found:
            missing.add(name)
        i = j
    return out


# --------------------------------------------------------------------------
# helpers over token lists
# --------------------------------------------------------------------------
def _last_line(toks: list[Token], path: str, default: int) -> int:
    """The last line of ``toks`` in ``path`` itself.

    Copybooks are spliced into the token stream with their own line numbers, so the last
    token can come from a copybook COPYed at the end and say line 40 of a 4,000-line
    program (CardDemo COACTUPC: every COPY after line 372 was dropped from the program).
    """
    for t in reversed(toks):
        if t.path == path:
            return max(t.line, default)
    return default


def _is_level(t: Token) -> bool:
    return t.kind == "WORD" and t.text.isdigit() and len(t.text) <= 2 and (
        1 <= int(t.text) <= 49 or int(t.text) in (66, 77, 88))


def _sentences(toks: list[Token]) -> list[list[Token]]:
    out, cur = [], []
    for t in toks:
        if t.kind == "PERIOD":
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def _group_exec(toks: list[Token]) -> list[Token | list[Token]]:
    """Collapse ``EXEC ... END-EXEC`` into one list element."""
    out: list = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t.kind == "WORD" and t.up in ("EXEC", "EXECUTE") and i + 1 < n \
                and toks[i + 1].up in ("SQL", "CICS", "DLI", "SQLIMS", "HTML", "XML", "JAVA"):
            j = i + 1
            while j < n and toks[j].up != "END-EXEC":
                j += 1
            out.append(toks[i:j + 1])
            i = j + 1
            continue
        out.append(t)
        i += 1
    return out


def _lit_value(text: str) -> str:
    if text[:1] in "'\"":
        return text[1:-1]
    return text


def norm_value(text: str) -> str:
    """Comparable form of a literal: quotes off, figuratives folded, numbers normalized."""
    up = text.upper()
    if up in ("SPACE", "SPACES"):
        return "SPACES"
    if up in ("ZERO", "ZEROS", "ZEROES"):
        return "0"
    if up in ("LOW-VALUE", "LOW-VALUES"):
        return "LOW-VALUES"
    if up in ("HIGH-VALUE", "HIGH-VALUES"):
        return "HIGH-VALUES"
    v = _lit_value(text)
    if text[:1] not in "'\"" and re.fullmatch(r"[+-]?\d+(\.\d+)?", v):
        try:
            f = float(v)
            return str(int(f)) if f == int(f) else str(f)
        except ValueError:
            return v
    return v


# --------------------------------------------------------------------------
# data division
# --------------------------------------------------------------------------
def _parse_values(toks: list[Token], i: int) -> tuple[list[str], int]:
    vals: list[str] = []
    n = len(toks)
    if i < n and toks[i].up in ("IS", "ARE"):
        i += 1
    while i < n:
        t = toks[i]
        if t.kind == "LIT" or (t.kind == "WORD" and (re.fullmatch(r"[+-]?\d*\.?\d+", t.text)
                                                     or t.up in FIGURATIVE)):
            if t.up == "ALL" and i + 1 < n:
                vals.append("ALL " + toks[i + 1].text)
                i += 2
                continue
            if i + 2 < n and toks[i + 1].up in ("THRU", "THROUGH"):
                vals.append(f"{t.text} THRU {toks[i + 2].text}")
                i += 3
                continue
            vals.append(t.text)
            i += 1
            continue
        if t.up in ("WHEN", "SET", "TO", "FALSE", "IS"):
            i += 1
            continue
        break
    return vals, i


def _parse_entry(toks: list[Token], section: str) -> DataItem | None:
    lv = toks[0]
    level = int(lv.text)
    i = 1
    name = "FILLER"
    line, col = lv.line, lv.col
    if i < len(toks) and toks[i].kind == "WORD" and toks[i].up not in DATA_CLAUSES:
        name = toks[i].up
        line, col = toks[i].line, toks[i].col
        i += 1
    item = DataItem(level=level, name=name, line=line, col=col, path=lv.path, copy=lv.copy,
                    shared=lv.shared and (i < 2 or toks[1].shared), section=section,
                    end_line=toks[-1].line)
    n = len(toks)
    while i < n:
        t = toks[i]
        w = t.up
        if w == "REDEFINES" and i + 1 < n:
            item.redefines = toks[i + 1].up
            i += 2
        elif w in ("PIC", "PICTURE"):
            i += 1
            if i < n and toks[i].up == "IS":
                i += 1
            if i < n:
                item.pic = canonical_pic(toks[i].text)
                i += 1
        elif w == "USAGE":
            i += 1
            if i < n and toks[i].up == "IS":
                i += 1
            if i < n:
                item.usage = USAGES.get(toks[i].up, toks[i].up)
                i += 1
        elif w in USAGES:
            item.usage = USAGES[w]
            i += 1
        elif w == "OCCURS":
            i += 1
            lo = hi = 1
            dep = ""
            if i < n and toks[i].text.isdigit():
                lo = hi = int(toks[i].text)
                i += 1
            if i + 1 < n and toks[i].up == "TO" and toks[i + 1].text.isdigit():
                hi = int(toks[i + 1].text)
                i += 2
            while i < n and toks[i].up not in DATA_CLAUSES - {"IS"} or (
                    i < n and toks[i].up == "IS"):
                w2 = toks[i].up
                if w2 == "DEPENDING":
                    i += 1
                    if i < n and toks[i].up == "ON":
                        i += 1
                    if i < n:
                        dep = toks[i].up
                elif w2 == "INDEXED":
                    i += 1
                    if i < n and toks[i].up == "BY":
                        i += 1
                    while i < n and toks[i].kind == "WORD" and toks[i].up not in DATA_CLAUSES \
                            and toks[i].up not in ("ASCENDING", "DESCENDING"):
                        item.indexes.append(toks[i].up)
                        i += 1
                    continue
                i += 1
            item.occurs = (lo, hi, dep)
        elif w in ("VALUE", "VALUES"):
            vals, i = _parse_values(toks, i + 1)
            item.values.extend(vals)
        elif w in ("SIGN", "LEADING", "TRAILING"):
            parts = []
            while i < n and toks[i].up in ("SIGN", "IS", "LEADING", "TRAILING", "SEPARATE",
                                           "CHARACTER"):
                if toks[i].up not in ("IS", "CHARACTER"):
                    parts.append(toks[i].up)
                i += 1
            item.sign = " ".join(p for p in parts if p != "SIGN") or "SIGN"
        elif w in ("JUST", "JUSTIFIED"):
            item.flags.append("JUSTIFIED")
            i += 1
        elif w in ("SYNC", "SYNCHRONIZED"):
            item.flags.append("SYNC")
            i += 1
        elif w == "BLANK":
            item.flags.append("BLANK-WHEN-ZERO")
            i += 1
        elif w in ("GLOBAL", "EXTERNAL", "BASED"):
            item.flags.append(w)
            i += 1
        elif w == "RENAMES":
            names = []
            i += 1
            while i < n:
                if toks[i].up in ("THRU", "THROUGH", "OF", "IN"):
                    i += 1
                    continue
                if toks[i].kind == "WORD":
                    names.append(toks[i].up)
                i += 1
            item.renames = tuple(names[:2])
        else:
            i += 1
    return item


def _pic_size(pic: str, usage: str, sign: str) -> int | None:
    runs = pic_runs(pic)
    if not runs:
        return None
    nines = sum(n for ch, n in runs if ch == "9")
    if usage in ("PACKED",):
        return nines // 2 + 1
    if usage in ("BINARY", "COMP-5", "COMP-X"):
        return 2 if nines <= 4 else 4 if nines <= 9 else 8
    if usage == "COMP-1":
        return 4
    if usage == "COMP-2":
        return 8
    symbols = {ch for ch, _n in runs}
    size = sum(n for ch, n in runs if ch not in "SVP")
    if "SEPARATE" in sign and "S" in symbols:
        size += 1
    if usage == "NATIONAL" or "N" in symbols:
        size *= 2
    return size


def _fixed_size(item: DataItem) -> int | None:
    if item.usage in ("INDEX", "POINTER", "PROCEDURE-POINTER", "FUNCTION-POINTER"):
        return 4 if item.usage != "PROCEDURE-POINTER" else 8
    if item.usage in ("COMP-1",) and not item.pic:
        return 4
    if item.usage in ("COMP-2",) and not item.pic:
        return 8
    return None


def compute_sizes(items: list[DataItem]) -> None:
    def size(it: DataItem, usage: str) -> int | None:
        u = it.usage or usage
        if it.usage == "" and usage:
            it_usage = usage
        else:
            it_usage = u
        kids = [c for c in it.children if c.level not in (66, 88)]
        if kids:
            total: int | None = 0
            for c in kids:
                s = size(c, it_usage)
                if c.redefines:
                    continue
                if s is None or total is None:
                    total = None
                else:
                    total += s * (c.occurs[1] if c.occurs else 1)
            it.size = total
        else:
            fixed = _fixed_size(it) if not it.pic else None
            it.size = fixed if fixed is not None else (
                _pic_size(it.pic, it_usage, it.sign) if it.pic else None)
        return it.size

    def place(it: DataItem, start: int | None) -> None:
        it.offset = start
        cur = start
        kids = [c for c in it.children if c.level not in (66, 88)]
        by_name: dict[str, DataItem] = {}
        for c in kids:
            if c.redefines and c.redefines in by_name:
                place(c, by_name[c.redefines].offset)
            else:
                place(c, cur)
                if cur is not None and c.size is not None:
                    cur += c.size * (c.occurs[1] if c.occurs else 1)
                else:
                    cur = None
            by_name[c.name] = c
        for c in it.children:
            if c.level == 88:
                c.offset, c.size = it.offset, it.size

    for r in items:
        if r.level in (66, 88):
            continue
        size(r, "")
        place(r, 0)


def parse_data(toks: list[Token], prog: Program | None = None
               ) -> tuple[list[DataItem], dict[str, FileDef]]:
    """Record tree from DATA DIVISION tokens (or a data copybook)."""
    roots: list[DataItem] = []
    files: dict[str, FileDef] = {}
    stack: list[DataItem] = []
    section = "WORKING-STORAGE"
    fd = ""
    last: DataItem | None = None
    grouped = _group_exec(toks)
    flat: list[Token] = []
    for g in grouped:
        if isinstance(g, list):
            if prog is not None:
                _ws_exec(g, prog)
            continue
        flat.append(g)
    for sent in _sentences(flat):
        head = sent[0]
        w = head.up
        if len(sent) >= 2 and sent[1].up == "SECTION":
            section = w
            fd = ""
            stack.clear()
            continue
        if w in ("FD", "SD", "RD", "CD") and len(sent) > 1:
            fd = sent[1].up
            f = files.setdefault(fd, FileDef(fd, sent[1].line, sent[1].path))
            f.clauses = " ".join(t.up for t in sent[2:])
            stack.clear()
            continue
        if not _is_level(head):
            continue
        item = _parse_entry(sent, section)
        if item is None:
            continue
        if item.level == 88:
            if last is not None:
                item.parent = last
                last.children.append(item)
            continue
        if item.level in (1, 77, 66):
            stack.clear()
            if item.level == 1 and fd:
                item.fd = fd
                files[fd].records.append(item.name)
            if item.level == 66:
                item.parent = None
            roots.append(item)
            if item.level != 66:
                stack.append(item)
            last = item
            continue
        while stack and stack[-1].level >= item.level:
            stack.pop()
        if stack:
            item.parent = stack[-1]
            stack[-1].children.append(item)
            item.fd = stack[0].fd
        else:
            roots.append(item)       # a copybook fragment starting at 05, or a stray level
        stack.append(item)
        last = item
    compute_sizes(roots)
    return roots, files


def _ws_exec(block: list[Token], prog: Program) -> None:
    """EXEC SQL inside the DATA DIVISION: cursor declarations and DCLGEN tables."""
    words = [t.up for t in block]
    if len(words) > 2 and words[1] == "SQL" and words[2] == "DECLARE":
        name = block[3].up if len(block) > 3 else ""
        tables = _sql_tables(block)
        if "CURSOR" in words:
            prog.cursors[name] = tables
        elif "TABLE" in words:
            prog.ws_sql.append({"op": "DECLARE-TABLE", "tables": [name], "line": block[0].line})


# --------------------------------------------------------------------------
# procedure division
# --------------------------------------------------------------------------
_SQL_KEYWORDS = frozenset("""
SELECT FROM WHERE AND OR NOT INTO VALUES SET ORDER GROUP BY HAVING UNION JOIN INNER OUTER
LEFT RIGHT FULL ON AS WITH FOR FETCH FIRST ROWS ONLY DISTINCT CURSOR DECLARE OPEN CLOSE
UPDATE DELETE INSERT TABLE NULL IS IN EXISTS BETWEEN LIKE CASE WHEN THEN ELSE END COUNT
SUM MAX MIN AVG CURRENT OF READ UR CS RR RS LOCK MODE SHARE EXCLUSIVE ASC DESC LIMIT
OPTIMIZE HOLD RETURN RESULT SETS SCROLL SENSITIVE INSENSITIVE STATIC DYNAMIC TIMESTAMP DATE
""".split())


def _sql_tables(block: list[Token]) -> list[str]:
    toks = [t for t in block[2:-1]]
    tables: list[str] = []
    i = 0
    while i < len(toks):
        w = toks[i].up
        take = w in ("FROM", "JOIN", "UPDATE", "TABLE") or (
            w == "INTO" and i > 0 and toks[i - 1].up == "INSERT")
        if take and i + 1 < len(toks) and toks[i + 1].kind == "WORD" \
                and not toks[i + 1].text.startswith(":"):
            name = toks[i + 1].up
            j = i + 2
            while j + 1 < len(toks) and toks[j].text == "." and toks[j + 1].kind == "WORD":
                name += "." + toks[j + 1].up
                j += 2
            if name not in _SQL_KEYWORDS and name != "(":
                tables.append(name)
            i = j
            # comma lists after FROM: the tokenizer dropped the commas, so take
            # further bare names until a keyword
            if w == "FROM":
                while i < len(toks) and toks[i].kind == "WORD" \
                        and toks[i].up not in _SQL_KEYWORDS and not toks[i].text.startswith(":"):
                    nxt = toks[i].up
                    # "FROM T1 A, T2 B": aliases are short; tables usually are not
                    if i + 1 < len(toks) and toks[i + 1].kind == "WORD" \
                            and toks[i + 1].up not in _SQL_KEYWORDS:
                        if len(nxt) > 3:
                            tables.append(nxt)
                    i += 1
            continue
        i += 1
    return list(dict.fromkeys(tables))


def _chain(items: list, i: int) -> tuple[tuple, int, list[int]]:
    """A data reference at ``items[i]``: ``(chain, next index, subscript token indexes)``."""
    names = [items[i].up.lstrip(":")]
    j = i + 1
    while j + 1 < len(items) and isinstance(items[j], Token) and items[j].up in ("OF", "IN") \
            and isinstance(items[j + 1], Token) and items[j + 1].kind == "WORD":
        names.append(items[j + 1].up)
        j += 2
    subs: list[int] = []
    if j < len(items) and isinstance(items[j], Token) and items[j].text == "(":
        depth = 0
        k = j
        while k < len(items):
            t = items[k]
            if isinstance(t, Token) and t.text == "(":
                depth += 1
            elif isinstance(t, Token) and t.text == ")":
                depth -= 1
                if depth == 0:
                    break
            else:
                subs.append(k)
            k += 1
        j = k + 1
    return tuple(names), j, subs


_PHASE_WORDS: dict[str, dict[str, str]] = {
    "MOVE": {"TO": "w"},
    "COMPUTE": {"=": "r", "EQUAL": "r"},
    "ADD": {"TO": "TO", "GIVING": "w"},
    "SUBTRACT": {"FROM": "TO", "GIVING": "w"},
    "MULTIPLY": {"BY": "TO", "GIVING": "w"},
    "DIVIDE": {"INTO": "TO", "BY": "r", "GIVING": "w", "REMAINDER": "w"},
    "INITIALIZE": {"REPLACING": "r", "BY": "r"},
    "SET": {"TO": "r", "UP": "r", "DOWN": "r"},
    "STRING": {"INTO": "w", "POINTER": "rw", "DELIMITED": "r"},
    "UNSTRING": {"INTO": "w", "DELIMITER": "w", "COUNT": "w", "TALLYING": "w",
                 "POINTER": "rw", "DELIMITED": "r"},
    "INSPECT": {"TALLYING": "w", "FOR": "r", "REPLACING": "r", "CONVERTING": "r",
                "BEFORE": "r", "AFTER": "r"},
    "READ": {"INTO": "w", "KEY": "r"},
    "RETURN": {"INTO": "w"},
    "WRITE": {"FROM": "r"},
    "REWRITE": {"FROM": "r"},
    "RELEASE": {"FROM": "r"},
    "SEARCH": {"VARYING": "w"},
    "PERFORM": {"VARYING": "w", "AFTER": "w", "FROM": "r", "BY": "r", "UNTIL": "r"},
    "ACCEPT": {"FROM": "r"},
    "CALL": {"RETURNING": "w", "GIVING": "w"},
}
_START_PHASE = {"COMPUTE": "w", "INITIALIZE": "w", "SET": "w", "ACCEPT": "w"}
_FILE_VERBS = {"OPEN", "CLOSE", "READ", "DELETE", "START", "RETURN", "SORT", "MERGE",
               "UNLOCK"}
_RECORD_VERBS = {"WRITE", "REWRITE", "RELEASE"}
_SKIP_WORDS = frozenset("""
TO FROM BY INTO GIVING OF IN IS ARE THAN EQUAL EQUALS GREATER LESS NOT AND OR ALL ROUNDED
CORRESPONDING CORR THRU THROUGH TIMES UNTIL VARYING AFTER BEFORE WITH TEST USING
REFERENCE CONTENT VALUE DELIMITED SIZE POINTER INITIAL LEADING FIRST CHARACTERS TALLYING
REPLACING CONVERTING NUMERIC ALPHABETIC ALPHANUMERIC NATIONAL DATA KEY INVALID AT END
NEXT RECORD INPUT OUTPUT I-O EXTEND UPON ADVANCING LINE LINES PAGE POSITIVE NEGATIVE
ON ERROR EXCEPTION OVERFLOW OTHER ALSO ANY OMITTED DEPENDING PROCEED RETURNING TRUE FALSE
REMAINDER COUNT DELIMITER UP DOWN ADDRESS LENGTH FUNCTION DATE DAY DAY-OF-WEEK TIME
""".split()) | FIGURATIVE | VERBS | {"THEN", "ELSE", "WHEN"}


def literal_moves(para: Paragraph):
    """``(literal, target chain)`` for each ``MOVE 'LIT' TO X [Y ...]`` in a paragraph."""
    toks = para.tokens
    for k in range(len(toks) - 3):
        if toks[k].up == "MOVE" and toks[k + 1].kind == "LIT" and toks[k + 2].up == "TO":
            j = k + 3
            while j < len(toks) and toks[j].kind == "WORD" and toks[j].up not in VERBS:
                chain, j2, _ = _chain(toks, j)
                yield _lit_value(toks[k + 1].text), chain
                j = j2
                if j < len(toks) and toks[j].up not in ("OF", "IN") and toks[j].kind != "WORD":
                    break


#: words that open an inline PERFORM's phrase (``PERFORM UNTIL X``, ``PERFORM WITH TEST``)
_INLINE_PERFORM_START = frozenset({"UNTIL", "VARYING", "WITH", "TEST", "FOREVER"})


class _ProcParser:
    def __init__(self, prog: Program, names: set[str]) -> None:
        self.prog = prog
        self.names = names             # paragraph and section names

    # -- statements --------------------------------------------------------
    def run(self, para: Paragraph) -> None:
        items = _group_exec(para.tokens)
        stack: list[str] = []
        segs: list[tuple[str, list, bool]] = []      # (verb, items, conditional)
        cur_verb, cur, cur_cond = "", [], False

        def close() -> None:
            nonlocal cur, cur_verb
            if cur_verb or cur:
                segs.append((cur_verb, cur, cur_cond))
            cur, cur_verb = [], ""

        i = 0
        while i < len(items):
            it = items[i]
            if isinstance(it, list):
                close()
                segs.append(("EXEC", it, bool(stack)))
                i += 1
                continue
            w = it.up if it.kind == "WORD" else it.text
            if it.kind == "PERIOD":
                close()
                stack.clear()
                segs.append((".", [], False))
                i += 1
                continue
            if it.kind == "WORD" and w.startswith("END-") and w != "END-EXEC":
                close()
                opener = w[4:]
                if opener in stack:
                    while stack and stack.pop() != opener:
                        pass
                elif stack and stack[-1].startswith("PHRASE:") and stack[-1][7:] == opener:
                    stack.pop()
                else:
                    for k in range(len(stack) - 1, -1, -1):
                        if stack[k] == "PHRASE:" + opener:
                            del stack[k:]
                            break
                i += 1
                continue
            if it.kind == "WORD" and w in STRUCTURE:
                close()
                cur_verb, cur_cond = "_COND", True
                i += 1
                continue
            if it.kind == "WORD" and w in PHRASE_START and cur_verb and cur_verb not in (
                    "_COND", "IF", "EVALUATE", "PERFORM", "GO", "SEARCH") and \
                    self._phrase(items, i):
                verb = cur_verb
                close()
                stack.append("PHRASE:" + verb)
                cur_verb, cur_cond = "_COND", True
                i += 1
                continue
            if it.kind == "WORD" and w in VERBS and not (w == "NEXT" and not (
                    i + 1 < len(items) and isinstance(items[i + 1], Token)
                    and items[i + 1].up == "SENTENCE")):
                close()
                cur_verb, cur_cond = w, bool(stack)
                if w in OPENERS:
                    stack.append(w)
                if w == "PERFORM" and self._inline_perform(items, i + 1):
                    stack.append("PERFORM")
                    segs.append(("PERFORM-INLINE", [], bool(stack[:-1])))
                    cur_verb = "PERFORM-LOOP"
                if w not in ("EXIT", "CONTINUE"):
                    para.statements += 1          # no-ops do not make a paragraph "do" anything
                i += 1
                continue
            cur.append(it)
            i += 1
        close()
        for verb, body, cond in segs:
            self._segment(para, verb, body, cond)
        self._evaluates(para, items)
        self._terminal(para)

    def _phrase(self, items: list, i: int) -> bool:
        w = items[i].up
        nxt = items[i + 1].up if i + 1 < len(items) and isinstance(items[i + 1], Token) else ""
        if w == "AT":
            return nxt in ("END", "END-OF-PAGE", "EOP")
        if w == "NOT":
            return nxt in ("AT", "INVALID", "ON", "END", "SIZE", "OVERFLOW", "EXCEPTION")
        if w == "INVALID":
            return True
        if w == "ON":
            return nxt in ("SIZE", "OVERFLOW", "EXCEPTION", "ERROR")
        return w in ("END-OF-PAGE", "EOP", "OVERFLOW", "EXCEPTION")

    def _inline_perform(self, items: list, i: int) -> bool:
        """Is this ``PERFORM ... END-PERFORM`` (statements inline), not ``PERFORM X``?

        Decided by the words after PERFORM, not by whether X is a known paragraph:
        a PERFORM of a paragraph that does not exist (deleted, misspelt) is still
        out-of-line, and reading it as inline left an END-PERFORM open that made
        every later statement, GOBACK included, look conditional.
        """
        if i >= len(items) or not isinstance(items[i], Token):
            return True
        t = items[i]
        if t.kind == "WORD" and t.up in self.names:
            return False
        if t.kind == "PERIOD":
            return False
        if t.kind != "WORD" or t.up in _INLINE_PERFORM_START:
            return True
        nxt = items[i + 1] if i + 1 < len(items) else None
        return isinstance(nxt, Token) and nxt.up == "TIMES"     # PERFORM WS-N TIMES

    def _segment(self, para: Paragraph, verb: str, body: list, cond: bool) -> None:
        if verb == "EXEC":
            self._exec(para, body, cond)
            return
        if verb in ("", ".", "PERFORM-INLINE"):
            if verb == "PERFORM-INLINE":
                para.flags.add("inline-perform")
            return
        first = body[0] if body and isinstance(body[0], Token) else None
        if verb in OBSOLETE:
            para.flags.add(OBSOLETE[verb])
        if verb == "NEXT":
            para.flags.add("next-sentence")
        if verb == "PERFORM":
            self._perform(para, body, cond)
        elif verb == "PERFORM-LOOP":
            self._refs(para, "PERFORM", body, cond)
            return
        elif verb == "GO":
            self._goto(para, body, cond)
        elif verb == "ALTER":
            self._alter(para, body)
        elif verb == "CALL":
            self._call(para, body, cond)
            return
        elif verb == "ENTRY":
            self._entry(para, body)
            return
        elif verb == "STOP":
            if first is not None and first.up == "RUN":
                para.flags.add("stop-run")
                if not cond:
                    para.flags.add("_term")
            elif first is not None:
                para.flags.add("stop-literal")
            return
        elif verb == "GOBACK":
            para.flags.add("goback")
            if not cond:
                para.flags.add("_term")
            return
        elif verb == "EXIT":
            if first is not None and first.up == "PROGRAM":
                para.flags.add("exit-program")
                if not cond:
                    para.flags.add("_term")
            elif first is not None and first.up in ("PARAGRAPH", "SECTION", "PERFORM"):
                para.flags.add("_early-exit")      # leaves early: it can return
            return
        if verb in _FILE_VERBS or verb in _RECORD_VERBS:
            self._io(para, verb, body, cond)
        self._refs(para, verb, body, cond, skip_first=verb in _FILE_VERBS - {"RETURN"}
                   or verb in ("OPEN", "CLOSE"))

    # -- control flow ------------------------------------------------------
    def _proc_name(self, body: list, i: int) -> tuple[str, int]:
        t = body[i]
        name = t.up
        j = i + 1
        if j + 1 < len(body) and isinstance(body[j], Token) and body[j].up in ("OF", "IN"):
            name = f"{body[j + 1].up}.{name}"     # qualified by its section
            j += 2
        return name, j

    def _perform(self, para: Paragraph, body: list, cond: bool) -> None:
        if not body or not isinstance(body[0], Token) or body[0].kind != "WORD":
            return
        first, i = self._proc_name(body, 0)
        last = ""
        if i + 1 < len(body) and isinstance(body[i], Token) and body[i].up in ("THRU", "THROUGH"):
            last, i = self._proc_name(body, i + 1)
        rest = [t.up for t in body[i:] if isinstance(t, Token)]
        loop = any(w in ("UNTIL", "VARYING", "TIMES") for w in rest)
        para.performs.append({"first": first, "last": last, "line": body[0].line,
                              "col": body[0].col, "path": body[0].path, "cond": cond or loop,
                              "loop": loop})
        if last:
            para.flags.add("perform-thru")
        if loop:
            self._refs(para, "PERFORM", body[i:], cond)

    def _goto(self, para: Paragraph, body: list, cond: bool) -> None:
        toks = [t for t in body if isinstance(t, Token)]
        if toks and toks[0].up == "TO":
            toks = toks[1:]
        targets, dep = [], ""
        k = 0
        while k < len(toks):
            t = toks[k]
            if t.up == "DEPENDING":
                rest = toks[k + 1:]
                if rest and rest[0].up == "ON":
                    rest = rest[1:]
                dep = rest[0].up if rest else "?"
                self._refs(para, "GO", rest, cond)
                break
            if t.kind == "WORD" and t.up not in ("OF", "IN"):
                targets.append(t.up)
            k += 1
        line = body[0].line if body else para.line
        col = body[0].col if body else 0
        path = body[0].path if body else para.path
        para.gotos.append({"targets": targets, "depending": dep, "line": line, "col": col,
                           "path": path, "cond": cond or bool(dep)})
        para.flags.add("go-to-depending" if dep else "go-to")
        if not targets:
            para.flags.add("alterable-go-to")
        if not cond and not dep and targets:
            para.flags.add("_term")

    def _alter(self, para: Paragraph, body: list) -> None:
        toks = [t.up for t in body if isinstance(t, Token)]
        k = 0
        while k < len(toks):
            if k + 2 < len(toks) and toks[k + 1] == "TO":
                dst_i = k + 2
                if toks[dst_i] == "PROCEED" and dst_i + 2 < len(toks):
                    dst_i += 2
                para.alters.append({"src": toks[k], "dst": toks[dst_i],
                                    "line": body[0].line})
                k = dst_i + 1
            else:
                k += 1
        para.flags.add("alter")

    def _args(self, body: list, i: int) -> tuple[list[dict], int]:
        mode = "REFERENCE"
        args: list[dict] = []
        while i < len(body):
            t = body[i]
            if not isinstance(t, Token):
                break
            w = t.up
            if w in ("RETURNING", "GIVING", "ON", "NOT", "EXCEPTION", "OVERFLOW", "END-CALL"):
                break
            if w == "BY":
                i += 1
                continue
            if w in ("REFERENCE", "CONTENT", "VALUE"):
                mode = w
                i += 1
                continue
            if w == "OMITTED":
                args.append({"mode": mode, "chain": (), "omitted": True, "line": t.line,
                             "col": t.col})
                i += 1
                continue
            if w in ("ADDRESS", "LENGTH") and i + 1 < len(body) \
                    and isinstance(body[i + 1], Token) and body[i + 1].up == "OF":
                chain, j, _ = _chain(body, i + 2) if i + 2 < len(body) else ((), i + 2, [])
                args.append({"mode": mode, "chain": chain, "special": w, "line": t.line,
                             "col": t.col})
                i = j
                continue
            if t.kind == "LIT" or (t.kind == "WORD" and (t.text[:1].isdigit()
                                                         or t.up in FIGURATIVE)):
                args.append({"mode": mode, "chain": (), "literal": t.text, "line": t.line,
                             "col": t.col})
                i += 1
                continue
            if t.kind == "WORD":
                chain, j, _ = _chain(body, i)
                args.append({"mode": mode, "chain": chain, "line": t.line, "col": t.col})
                i = j
                continue
            i += 1
        return args, i

    def _call(self, para: Paragraph, body: list, cond: bool) -> None:
        if not body or not isinstance(body[0], Token):
            return
        t = body[0]
        i = 1
        if t.kind == "LIT":
            target, literal = _lit_value(t.text), True
        else:
            chain, i, _ = _chain(body, 0)
            target, literal = ".".join(chain), False
            if t.up in ("PROCEDURE-POINTER", "FUNCTION-POINTER"):
                return
        args: list[dict] = []
        if i < len(body) and isinstance(body[i], Token) and body[i].up == "USING":
            args, i = self._args(body, i + 1)
        returning = ()
        if i < len(body) and isinstance(body[i], Token) and body[i].up in ("RETURNING",
                                                                            "GIVING"):
            if i + 1 < len(body) and isinstance(body[i + 1], Token):
                returning, _, _ = _chain(body, i + 1)
        para.calls.append({"kind": "call", "target": target, "literal": literal,
                           "args": args, "returning": returning, "line": t.line,
                           "col": t.col, "path": t.path, "cond": cond})
        if not literal:
            para.flags.add("dynamic-call")
            para.refs.append(Ref((target.split(".")[0],) + tuple(target.split(".")[1:]),
                                 "r", t.line, t.col, t.path, cond))
        for a in args:
            if a.get("chain"):
                para.refs.append(Ref(a["chain"], "r", a["line"], a["col"], t.path, cond))
        if returning:
            para.refs.append(Ref(returning, "w", t.line, t.col, t.path, cond))

    def _entry(self, para: Paragraph, body: list) -> None:
        if not body or not isinstance(body[0], Token):
            return
        t = body[0]
        args: list[dict] = []
        i = 1
        if i < len(body) and isinstance(body[i], Token) and body[i].up == "USING":
            args, i = self._args(body, i + 1)
        para.entries.append({"name": _lit_value(t.text), "args": args, "line": t.line,
                             "col": t.col, "path": t.path})

    # -- I/O ---------------------------------------------------------------
    def _io(self, para: Paragraph, verb: str, body: list, cond: bool) -> None:
        toks = [t for t in body if isinstance(t, Token)]
        if verb in ("OPEN", "CLOSE"):
            mode = ""
            for t in toks:
                if t.up in ("INPUT", "OUTPUT", "I-O", "EXTEND"):
                    mode = t.up
                elif t.kind == "WORD" and t.up not in _SKIP_WORDS and t.up not in (
                        "REEL", "UNIT", "REWIND", "LOCK", "REMOVAL", "NO"):
                    para.io.append({"verb": verb, "name": t.up, "mode": mode,
                                    "line": t.line, "col": t.col, "path": t.path,
                                    "cond": cond})
            return
        if toks and toks[0].kind == "WORD":
            para.io.append({"verb": verb, "name": toks[0].up, "line": toks[0].line,
                            "col": toks[0].col, "path": toks[0].path, "cond": cond})

    def _exec(self, para: Paragraph, block: list[Token], cond: bool) -> None:
        if len(block) < 2:
            return
        lang = block[1].up
        line, path = block[0].line, block[0].path
        if lang == "SQL":
            para.flags.add("exec-sql")
            words = [t.up for t in block[2:-1]]
            op = words[0] if words else ""
            tables = _sql_tables(block)
            if op in ("OPEN", "FETCH", "CLOSE") and len(words) > 1:
                cur = words[1]
                para.sql.append({"op": op, "cursor": cur, "tables": [], "line": line,
                                 "path": path, "cond": cond})
            elif tables or op in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "CALL"):
                para.sql.append({"op": op, "tables": tables, "line": line, "path": path,
                                 "cond": cond})
            into = False
            for t in block[2:-1]:
                if t.up == "INTO":
                    into = True
                elif t.up in ("FROM", "WHERE", "VALUES", "SET"):
                    into = False
                if t.kind == "WORD" and t.text.startswith(":"):
                    chain = tuple(p for p in t.up.lstrip(":").split(":")[0].split(".") if p)
                    if chain:
                        para.refs.append(Ref(chain[::-1] if len(chain) > 1 else chain,
                                             "w" if into and op in ("SELECT", "FETCH") else "r",
                                             t.line, t.col, t.path, cond))
            return
        if lang == "CICS":
            para.flags.add("exec-cics")
            self._cics(para, block, cond)
            return
        if lang == "DLI":
            para.flags.add("exec-dli")

    def _cics(self, para: Paragraph, block: list[Token], cond: bool) -> None:
        toks = block[2:-1]
        if not toks:
            return
        cmd = toks[0].up
        if len(toks) > 1 and toks[1].kind == "WORD" and toks[1].text != "(" and \
                cmd in ("HANDLE", "SEND", "RECEIVE", "READQ", "WRITEQ", "DELETEQ", "IGNORE",
                        "PUSH", "POP"):
            cmd = f"{cmd} {toks[1].up}"
        opts: dict[str, list[Token]] = {}
        i = 1
        while i < len(toks):
            t = toks[i]
            if t.kind == "WORD" and i + 1 < len(toks) and toks[i + 1].text == "(":
                depth, j, arg = 0, i + 1, []
                while j < len(toks):
                    if toks[j].text == "(":
                        depth += 1
                        if depth == 1:
                            j += 1
                            continue
                    elif toks[j].text == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    arg.append(toks[j])
                    j += 1
                opts.setdefault(t.up, arg)
                i = j + 1
                continue
            if t.kind == "WORD":
                opts.setdefault(t.up, [])
            i += 1
        line, col, path = block[0].line, block[0].col, block[0].path
        base = cmd.split()[0]
        if base in ("LINK", "XCTL") and "PROGRAM" in opts:
            arg = opts["PROGRAM"]
            if arg:
                literal = arg[0].kind == "LIT"
                target = _lit_value(arg[0].text) if literal else ".".join(
                    _chain(arg, 0)[0])
                args = []
                if "COMMAREA" in opts and opts["COMMAREA"]:
                    ch = _chain(opts["COMMAREA"], 0)[0]
                    args.append({"mode": "REFERENCE", "chain": ch, "line": line, "col": col})
                para.calls.append({"kind": "cics-" + base.lower(), "target": target,
                                   "literal": literal, "args": args, "returning": (),
                                   "line": line, "col": col, "path": path, "cond": cond})
                if not literal:
                    para.flags.add("dynamic-call")
            if base == "XCTL" and not cond:
                para.flags.add("_term")
        if base == "RETURN" and not cond:
            para.flags.add("_term")
        if base == "HANDLE" or base == "PUSH":
            for k, arg in opts.items():
                if arg and arg[0].kind == "WORD" and arg[0].up in self.names:
                    para.gotos.append({"targets": [arg[0].up], "depending": "", "line": line,
                                       "col": col, "path": path, "cond": True,
                                       "handler": k})
                    para.flags.add("cics-handle")
        resource = ""
        for key in ("FILE", "DATASET", "QUEUE", "QNAME", "MAP", "TRANSID"):
            if key in opts and opts[key]:
                a = opts[key][0]
                resource = f"{key}:{_lit_value(a.text) if a.kind == 'LIT' else a.up}"
                break
        para.cics.append({"cmd": cmd, "resource": resource, "line": line, "path": path,
                          "cond": cond})
        writes_opts = {"INTO", "SET", "RESP", "RESP2", "LENGTH", "ABSTIME", "ITEM",
                       "NUMITEMS"} | ({"RIDFLD"} if base in ("READNEXT", "READPREV")
                                      else set()) if base in ("RECEIVE", "READ", "READNEXT",
                                                         "READPREV", "READQ", "ASKTIME",
                                                         "RETRIEVE", "ASSIGN", "INQUIRE",
                                                         "STARTBR", "FORMATTIME") \
            else {"RESP", "RESP2"}
        for k, arg in opts.items():
            if not arg or arg[0].kind != "WORD" or k in ("PROGRAM", "FILE", "DATASET",
                                                          "MAP", "MAPSET", "QUEUE", "TRANSID"):
                continue
            if arg[0].up in ("LENGTH", "ADDRESS") and len(arg) > 2:
                arg = arg[2:]
            chain = _chain(arg, 0)[0]
            if chain[0] in self.names:
                continue
            para.refs.append(Ref(chain, "w" if k in writes_opts or base in (
                "FORMATTIME",) and k not in ("ABSTIME",) else "r", line, col, path, cond))

    # -- data references ---------------------------------------------------
    def _refs(self, para: Paragraph, verb: str, body: list, cond: bool,
              skip_first: bool = False) -> None:
        table = _PHASE_WORDS.get(verb, {})
        toks = [t for t in body if isinstance(t, Token)]
        stmt = (toks[0].path, toks[0].line, toks[0].col) if toks else ()
        words = {t.up for t in toks}
        giving = "GIVING" in words
        phase = _START_PHASE.get(verb, "r")
        if verb == "INSPECT":
            phase = "rw" if words & {"REPLACING", "CONVERTING"} else "r"
        first_done = False
        i = 0
        while i < len(toks):
            t = toks[i]
            w = t.up
            if t.kind != "WORD":
                if t.text in table:
                    phase = table[t.text]
                i += 1
                continue
            if w in table:
                p = table[w]
                if p == "TO":
                    p = "r" if giving else "rw"
                phase = p
                i += 1
                continue
            if w in ("ADDRESS", "LENGTH") and i + 1 < len(toks) and toks[i + 1].up == "OF":
                i += 2
                continue
            if w == "FUNCTION" and i + 1 < len(toks):
                i += 2
                continue
            if w in _SKIP_WORDS or w[:1].isdigit() or w.startswith(("+", "-", ".")) \
                    or w in self.names:
                i += 1
                continue
            chain, j, subs = _chain(toks, i)
            if skip_first and not first_done:
                first_done = True
                i = j
                continue
            para.refs.append(Ref(chain, phase, t.line, t.col, t.path, cond, verb, stmt))
            for k in subs:
                s = toks[k]
                if s.kind == "WORD" and s.up not in _SKIP_WORDS and not s.up[:1].isdigit():
                    para.refs.append(Ref((s.up,), "r", s.line, s.col, s.path, cond))
            if not first_done:
                first_done = True
                if verb == "ACCEPT":
                    phase = "r"
                elif verb == "INSPECT":
                    phase = "r"
                elif verb in ("UNSTRING",):
                    phase = "r"
            if verb == "INSPECT" and phase == "w":
                phase = "r"
            i = j

    # -- EVALUATE facts ----------------------------------------------------
    def _evaluates(self, para: Paragraph, items: list) -> None:
        flat = [t for t in items if isinstance(t, Token)]
        for i, t in enumerate(flat):
            if t.kind == "WORD" and t.up == "EVALUATE":
                fact = self._evaluate(flat, i)
                if fact is not None:
                    para.evaluates.append(fact)

    def _evaluate(self, toks: list[Token], i: int) -> dict | None:
        j = i + 1
        subject: list[Token] = []
        while j < len(toks) and toks[j].up != "WHEN" and toks[j].kind != "PERIOD":
            subject.append(toks[j])
            j += 1
        if not subject or any(t.up == "ALSO" for t in subject):
            return None
        depth = 0
        whens: list[list[Token]] = []
        other = False
        cur: list[Token] | None = None
        while j < len(toks):
            t = toks[j]
            if t.kind == "PERIOD":
                break
            if t.kind == "WORD" and t.up == "EVALUATE":
                depth += 1
            elif t.kind == "WORD" and t.up == "END-EVALUATE":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and t.kind == "WORD" and t.up == "WHEN":
                if j + 1 < len(toks) and toks[j + 1].up == "OTHER":
                    other = True
                    cur = None
                else:
                    cur = []
                    whens.append(cur)
                j += 1
                continue
            elif cur is not None and depth == 0:
                if t.kind == "WORD" and (t.up in VERBS or t.up in ("CONTINUE",)):
                    cur = None
                else:
                    cur.append(t)
            j += 1
        objs: list[dict] = []
        for w in whens:
            if not w:
                continue
            if all(x.kind == "LIT" or (x.kind == "WORD" and (
                    re.fullmatch(r"[+-]?\d*\.?\d+", x.text) or x.up in FIGURATIVE
                    or x.up in ("THRU", "THROUGH"))) for x in w):
                vals, k = [], 0
                while k < len(w):
                    if k + 2 < len(w) and w[k + 1].up in ("THRU", "THROUGH"):
                        vals.append(f"{norm_value(w[k].text)} THRU {norm_value(w[k + 2].text)}")
                        k += 3
                        continue
                    vals.append(norm_value(w[k].text))
                    k += 1
                objs.append({"lits": vals})
            elif w[0].kind == "WORD" and all(x.kind == "WORD" for x in w) and \
                    len(w) in (1, 3, 5) and all(w[k].up in ("OF", "IN")
                                               for k in range(1, len(w), 2)):
                objs.append({"name": [w[k].up for k in range(0, len(w), 2)]})
            else:
                objs.append({"complex": True})
        subj = [s.up for s in subject]
        if subj in (["TRUE"], ["FALSE"]):
            subject_chain: list[str] = subj
        elif all(s.kind == "WORD" for s in subject) and len(subject) in (1, 3, 5):
            subject_chain = [subject[k].up for k in range(0, len(subject), 2)]
        else:
            return None
        return {"subject": subject_chain, "whens": objs, "other": other,
                "line": toks[i].line, "col": toks[i].col, "path": toks[i].path}

    def _terminal(self, para: Paragraph) -> None:
        para.terminal = "_term" in para.flags
        para.flags.discard("_term")


# --------------------------------------------------------------------------
# program units
# --------------------------------------------------------------------------
def _split_programs(toks: list[Token]) -> list[tuple[str, list[Token], str]]:
    """``(name, tokens, parent)`` per program unit, nested ones included."""
    ends: dict[str, int] = {}
    for k in range(len(toks) - 2):
        if toks[k].up == "END" and toks[k + 1].up == "PROGRAM":
            ends.setdefault(toks[k + 2].text.strip("'\"").upper(), k)
    units: list[tuple[str, list[Token], str]] = []
    stack: list[tuple[str, list[Token]]] = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        starts = t.up in ("IDENTIFICATION", "ID") and i + 1 < n and toks[i + 1].up == "DIVISION"
        pid = t.up in ("PROGRAM-ID", "FUNCTION-ID", "CLASS-ID")
        if starts or (pid and not (i >= 2 and toks[i - 2].up == "DIVISION")):
            k = i
            while k < n and k < i + 6 and toks[k].up not in ("PROGRAM-ID", "FUNCTION-ID",
                                                             "CLASS-ID"):
                k += 1
            if k < n and k < i + 6:
                j = k + 1
                if j < n and toks[j].kind == "PERIOD":
                    j += 1
                raw = toks[j].text if j < n else "UNKNOWN"
                name = raw.strip("'\"").upper()
                if stack:
                    cur_name = stack[-1][0]
                    end_at = ends.get(cur_name)
                    if end_at is None or end_at < i:
                        units.append((stack[-1][0], stack[-1][1],
                                      stack[-2][0] if len(stack) > 1 else ""))
                        stack.pop()
                stack.append((name, []))
                stack[-1][1].append(t)
                i += 1
                continue
        if t.up == "END" and i + 2 < n and toks[i + 1].up == "PROGRAM":
            name = toks[i + 2].text.strip("'\"").upper()
            j = i + 3
            if j < n and toks[j].kind == "PERIOD":
                j += 1
            while stack:
                nm, body = stack.pop()
                units.append((nm, body, stack[-1][0] if stack else ""))
                if nm == name:
                    break
            i = j
            continue
        if stack:
            stack[-1][1].append(t)
        i += 1
    while stack:
        nm, body = stack.pop()
        units.append((nm, body, stack[-1][0] if stack else ""))
    units.sort(key=lambda u: (u[1][0].line if u[1] else 0))
    return units


def _divisions(toks: list[Token]) -> dict[str, tuple[int, int]]:
    marks: list[tuple[str, int, int]] = []
    for k in range(len(toks) - 1):
        if toks[k + 1].up == "DIVISION" and toks[k].up in (
                "IDENTIFICATION", "ID", "ENVIRONMENT", "DATA", "PROCEDURE"):
            name = "IDENTIFICATION" if toks[k].up == "ID" else toks[k].up
            j = k + 2
            if name == "PROCEDURE":
                marks.append((name, k, j))
                continue
            if j < len(toks) and toks[j].kind == "PERIOD":
                j += 1
            marks.append((name, k, j))
    out: dict[str, tuple[int, int]] = {}
    for idx, (name, at, body) in enumerate(marks):
        end = marks[idx + 1][1] if idx + 1 < len(marks) else len(toks)
        out.setdefault(name, (body, end))
    return out


def _file_control(toks: list[Token], prog: Program) -> None:
    for sent in _sentences(toks):
        if not sent or sent[0].up != "SELECT":
            continue
        k = 1
        if k < len(sent) and sent[k].up == "OPTIONAL":
            k += 1
        if k >= len(sent):
            continue
        name = sent[k].up
        f = prog.files.setdefault(name, FileDef(name, sent[k].line, sent[k].path))
        words = [t for t in sent[k + 1:]]
        j = 0
        while j < len(words):
            w = words[j].up
            nxt = [x for x in words[j + 1:j + 4] if x.up not in ("TO", "IS", "MODE", "USING")]
            if w == "ASSIGN" and nxt:
                f.assign = _lit_value(nxt[0].text).upper()
            elif w == "ORGANIZATION" and nxt:
                f.organization = nxt[0].up
            elif w == "ACCESS" and nxt:
                f.access = nxt[0].up
            elif w == "KEY" and nxt:
                f.keys.append(nxt[0].up)
            elif w == "STATUS" and nxt:
                f.status = nxt[0].up
            j += 1
        f.clauses = " ".join(t.up for t in sent[k + 1:])


def _procedure(toks: list[Token], prog: Program, notes: list[str]) -> None:
    """Split the PROCEDURE DIVISION into its header, preamble, sections and paragraphs."""
    i, n = 0, len(toks)
    # header: PROCEDURE DIVISION [USING ...] [RETURNING x] .
    hdr: list[Token] = []
    while i < n and toks[i].kind != "PERIOD":
        hdr.append(toks[i])
        i += 1
    i += 1
    prog.header_tokens = hdr
    prog.has_procedure = True
    words = [t.up for t in hdr]
    if "USING" in words:
        k = words.index("USING") + 1
        mode = "REFERENCE"
        while k < len(hdr):
            w = hdr[k].up
            if w == "RETURNING":
                break
            if w in ("BY", "OPTIONAL"):
                k += 1
                continue
            if w in ("REFERENCE", "VALUE", "CONTENT"):
                mode = w
                k += 1
                continue
            if hdr[k].kind == "WORD":
                chain, k2, _ = _chain(hdr, k)
                prog.using.append((mode, chain))
                k = k2
                continue
            k += 1
    if "RETURNING" in words:
        k = words.index("RETURNING") + 1
        if k < len(hdr):
            prog.returning = (hdr[k].up,)

    body = toks[i:]
    # pass 1: headers
    headers: list[tuple[int, str, bool, str]] = []      # (index, name, is_section, section)
    section = ""
    at_start = True
    in_exec = False
    for k, t in enumerate(body):
        if t.kind == "WORD" and t.up in ("EXEC", "EXECUTE"):
            in_exec = True
        if in_exec:
            if t.up == "END-EXEC":
                in_exec = False
            at_start = False
            continue
        if t.kind == "PERIOD":
            at_start = True
            continue
        if at_start and t.kind == "WORD" and k + 1 < len(body):
            nxt = body[k + 1]
            if nxt.up == "SECTION" and t.up not in ("DECLARATIVES",):
                section = t.up
                headers.append((k, t.up, True, ""))
                at_start = False
                continue
            if nxt.kind == "PERIOD" and t.up not in VERBS and not t.up.startswith("END-") \
                    and t.up not in STRUCTURE and t.up not in ("DECLARATIVES", "END", "CONTINUE",
                                                               "EXIT", "GOBACK"):
                headers.append((k, t.up, False, section))
                at_start = False
                continue
            if t.up == "END" and nxt.up == "DECLARATIVES":
                at_start = False
                continue
        at_start = False
    names = {h[1] for h in headers}
    dup = {nm for nm in names if sum(1 for h in headers if h[1] == nm and not h[2]) > 1}
    names |= {f"{h[3]}.{h[1]}" for h in headers if h[3]}
    pp = _ProcParser(prog, names)

    first_hdr = headers[0][0] if headers else len(body)
    pre_toks = [t for t in body[:first_hdr] if not (t.up in ("DECLARATIVES", "END"))]
    if pre_toks:
        pre = Paragraph("", "", False, pre_toks[0].line, pre_toks[0].col, pre_toks[0].path,
                        end_line=_last_line(pre_toks, pre_toks[0].path, pre_toks[0].line),
                        tokens=pre_toks)
        pp.run(pre)
        prog.preamble = pre
    for idx, (k, name, is_section, sect) in enumerate(headers):
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(body)
        start = k + 1
        while start < end and body[start].kind != "PERIOD":
            start += 1
        start += 1
        ptoks = [t for t in body[start:end]
                 if not (t.up == "DECLARATIVES" or (t.up == "END" and False))]
        # drop "END DECLARATIVES ." if it closes this paragraph
        for q in range(len(ptoks) - 1):
            if ptoks[q].up == "END" and ptoks[q + 1].up == "DECLARATIVES":
                ptoks = ptoks[:q]
                break
        h = body[k]
        para = Paragraph(name, sect, is_section, h.line, h.col, h.path,
                         end_line=_last_line(ptoks, h.path, h.line), tokens=ptoks,
                         copy=h.copy)
        if not is_section and name in dup and sect:
            para.flags.add("qualified")
        pp.run(para)
        prog.paragraphs.append(para)
    if dup:
        notes.append(f"{prog.name}: paragraph names defined more than once: "
                     + ", ".join(sorted(dup)))


def parse_units(tokens: list[Token], path: str, notes: list[str]) -> list[Program]:
    progs: list[Program] = []
    for name, utoks, parent in _split_programs(tokens):
        if not utoks:
            continue
        lit = ""
        for k, t in enumerate(utoks):
            if t.up == "PROGRAM-ID":
                j = k + 1
                if j < len(utoks) and utoks[j].kind == "PERIOD":
                    j += 1
                if j < len(utoks):
                    lit = utoks[j].text.strip("'\"")
                break
        prog = Program(name=name, path=path, line=utoks[0].line,
                       end_line=_last_line(utoks, path, utoks[0].line),
                       parent=parent, literal_name=lit or name)
        divs = _divisions(utoks)
        if "ENVIRONMENT" in divs:
            a, b = divs["ENVIRONMENT"]
            prog.env_tokens = utoks[a:b]
            _file_control(utoks[a:b], prog)
        if "DATA" in divs:
            a, b = divs["DATA"]
            items, files = parse_data(utoks[a:b], prog)
            prog.items = items
            for fname, fdef in files.items():
                cur = prog.files.get(fname)
                if cur is None:
                    prog.files[fname] = fdef
                else:
                    cur.records = fdef.records
                    cur.clauses = (cur.clauses + " | " + fdef.clauses).strip(" |")
        if "PROCEDURE" in divs:
            a, b = divs["PROCEDURE"]
            _procedure(utoks[a:b], prog, notes)
        progs.append(prog)
    return progs


# --------------------------------------------------------------------------
# ownership of items: program-local or a shared copybook node
# --------------------------------------------------------------------------
def assign_owners(roots: list[DataItem], default_owner: str) -> None:
    """Give every item an owner (copybook key or the program) and a path under it.

    An item copied from a copybook without REPLACING is the copybook's node, identified
    by its path from the highest ancestor that came from the same copybook. Everything
    else belongs to the program.
    """
    def ordinals(sibs: list[DataItem]) -> dict[int, int]:
        """1-based position of each item among its same-named siblings."""
        seen: dict[str, int] = defaultdict(int)
        out: dict[int, int] = {}
        for c in sibs:
            seen[c.name] += 1
            out[id(c)] = seen[c.name]
        return out

    def walk(it: DataItem, parent_owner: str, parent_rel: tuple, nth: int,
             named_twice: bool) -> None:
        own = it.copy if (it.copy and it.shared) else default_owner
        seg = f"FILLER#{nth}" if it.is_filler else it.name
        if it.parent is not None and own == parent_owner:
            rel = parent_rel + (seg,)
        else:
            rel = (seg,)
        # siblings with the same name get an ordinal
        if named_twice and nth > 1:
            rel = rel[:-1] + (f"{seg}#{nth}",)
        it.owner, it.rel = own, rel
        kids = ordinals(it.children)
        counts = Counter(c.name for c in it.children)
        for c in it.children:
            walk(c, own, rel, kids[id(c)], counts[c.name] > 1)
    top = ordinals(roots)
    for r in roots:
        walk(r, default_owner, (), top[id(r)], False)


def parse_copybook(src: Source, key: str, lib: Library, notes: list[str]) -> tuple[
        list[DataItem], list[CopyRef], bool]:
    """A copybook on its own: its record items (as shared nodes) and nested COPYs.

    Returns ``(items, copies, is_data)``; procedure-code copybooks yield no items.
    """
    cached = lib.sources.get(src.path) is src
    toks = [t.clone(key, True) for t in (lib.tokens(src.path) if cached else tokenize(src))]
    copies: list[CopyRef] = []
    missing: set[str] = set()
    toks = expand(toks, lib, src.path, copies, missing, chain=(key,))
    flat = [t for t in toks if not isinstance(t, list)]
    first = next((t for t in flat if t.kind != "PERIOD"), None)
    grouped = _group_exec(flat)
    lead = next((g for g in grouped if not isinstance(g, list)), None)
    is_data = lead is not None and (_is_level(lead) or lead.up in ("FD", "SD"))
    if first is None or not is_data:
        return [], copies, False
    items, _files = parse_data(flat, None)
    return items, copies, True
