"""Fortran source reading: physical lines to logical statements.

Handles both source forms and the preprocessor, and nothing else. What comes out
is a flat list of :class:`Stmt`, one per statement, with

* ``code``: lowercase outside strings, whitespace collapsed, each string or
  Hollerith constant replaced by ``@<n>`` (``strs[n]`` holds it with its quotes);
* ``text``: the same with the strings put back, which is what hashes are made of.

Fixed form (``.f``, ``.for``, ``.f77``, ``.ftn``, upper-case ``.F`` variants):
columns 1-5 are the label, a non-blank non-zero column 6 continues the previous
line, ``C``/``c``/``*``/``!`` (and ``D`` debug lines) in column 1 are comments,
and everything after column 72 is sequence numbering and is dropped *before*
anything else looks at the line, so renumbering a deck changes no hash.
Tab-format lines (DEC) are accepted: a leading tab starts column 7, or continues
the previous line when followed by a digit 1-9.

Free form: ``!`` comments, trailing ``&`` continuation (with an optional leading
``&`` on the next line), ``;`` separating statements.

Preprocessor lines (``#...``, in any file) are not evaluated. Every branch of an
``#if``/``#ifdef`` is kept, and statements inside one carry ``cond=True`` so the
edges they produce are conditional. ``#if 0`` blocks are dropped.
``#include "x"`` becomes an ``include`` statement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FIXED_SUFFIXES = (".f", ".for", ".f77", ".ftn", ".fpp")
FREE_SUFFIXES = (".f90", ".f95", ".f03", ".f08", ".f18", ".f23")
INCLUDE_SUFFIXES = (".inc", ".fi", ".fh", ".h", ".cmn", ".com")


@dataclass
class Stmt:
    code: str
    strs: list[str]
    line: int
    end: int
    label: str = ""
    cond: bool = False
    origin: str = ""                 # file the statement physically came from
    text: str = ""
    hollerith: int = 0               # Hollerith constants in the statement (F66 relic)
    at: int = 0                      # line of the INCLUDE that brought it in (0: none)

    def string(self, i: int) -> str:
        """The i-th string constant without its quotes."""
        s = self.strs[i]
        return s[1:-1] if len(s) >= 2 and s[0] in "'\"" else s


@dataclass
class Source:
    path: str
    form: str                        # "fixed" | "free"
    stmts: list[Stmt] = field(default_factory=list)
    comments: list[tuple[int, str]] = field(default_factory=list)
    preprocessed: bool = False
    macros: set[str] = field(default_factory=set)
    lines: int = 0


def form_of(path: str) -> str:
    low = path.lower()
    if low.endswith(FREE_SUFFIXES):
        return "free"
    if low.endswith(FIXED_SUFFIXES):
        return "fixed"
    return "unknown"


def sniff_form(text: str) -> str:
    """For include files and odd suffixes: does this look like fixed form?"""
    fixed = free = 0
    for raw in text.splitlines()[:400]:
        if not raw.strip() or raw.startswith("#"):
            continue
        c = raw[0]
        if c in "cC*" and (len(raw) == 1 or not raw[1:2].isalpha() or raw[:4].lower() in
                            ("c   ", "c---", "c***", "c===")):
            fixed += 1
            continue
        if raw.rstrip().endswith("&"):
            free += 1
        if c not in " \t0123456789!":
            free += 1                # statement text in column 1
        elif len(raw) > 5 and raw[:5].strip().isdigit() is False and raw[5] not in " 0\t" \
                and not raw[:5].strip():
            fixed += 1               # column-6 continuation
    return "free" if free > fixed else "fixed"


_DIRECTIVE = re.compile(r"#\s*(\w+)\s*(.*)")


class _Cpp:
    """Tracks #if nesting. Nothing is evaluated except a literal ``#if 0``."""

    def __init__(self) -> None:
        self.stack: list[list[bool]] = []      # [dead, taken_true] per level
        self.defines: set[str] = set()         # macro names (lowercase, as code is)

    @property
    def cond(self) -> bool:
        return bool(self.stack)

    @property
    def dead(self) -> bool:
        return any(level[0] for level in self.stack)

    def directive(self, line: str) -> str | None:
        """Consume one directive; return an include target if it is one."""
        m = _DIRECTIVE.match(line.strip())
        if not m:
            return None
        word, rest = m.group(1), m.group(2).strip()
        if word in ("if", "ifdef", "ifndef"):
            zero = word == "if" and rest in ("0", "(0)")
            self.stack.append([zero, not zero])
        elif word in ("elif", "else"):
            if self.stack:
                # after `#if 0` the other branch is live; otherwise every branch is kept
                self.stack[-1][0] = False
        elif word == "define" and not self.dead:
            dm = re.match(r"(\w+)", rest)
            if dm:
                self.defines.add(dm.group(1).lower())
        elif word == "endif":
            if self.stack:
                self.stack.pop()
        elif word == "include" and not self.dead:
            inc = re.match(r"[\"<]([^\">]+)[\">]", rest)
            if inc:
                return inc.group(1)
        return None


def _scan_free(line: str, quote: str | None) -> tuple[str, str | None, bool, str]:
    """One free-form physical line: (content, quote state at end, continues, comment)."""
    stripped = line.lstrip()
    if stripped.startswith("&"):
        line = stripped[1:]           # a continuation (or a continued string) resumes here
    i, n = 0, len(line)
    out = []
    comment = ""
    while i < n:
        c = line[i]
        if quote is not None:
            out.append(c)
            if c == quote:
                if i + 1 < n and line[i + 1] == quote:
                    out.append(line[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            out.append(c)
        elif c == "!":
            comment = line[i + 1:].strip()
            break
        else:
            out.append(c)
        i += 1
    content = "".join(out)
    cont = False
    body = content.rstrip()
    if body.endswith("&"):
        cont = True
        content = body[:-1]
    elif quote is not None:
        cont = True
    return content, quote, cont, comment


def _physical_to_logical_free(lines: list[str], cpp: _Cpp, include_text=None):
    """Yield (content, first, last, cond, comments, include_target).

    A ``#include`` between two statements is yielded as an include target (the parser
    reads it as statements). One inside a continued statement is spliced in as text, as
    cpp does: json-fortran builds ~30-argument dummy lists that way, and reading the
    directive line as code lost every one of those arguments.
    """
    buf: list[str] = []
    first = 0
    quote = None
    cond = False
    for no, raw in enumerate(lines, 1):
        if raw.lstrip().startswith("#"):
            inc = cpp.directive(raw)
            if inc and not buf:
                yield ("", no, no, cpp.cond, [], inc)
            elif inc and include_text is not None and not cpp.dead:
                for raw2 in (include_text(inc) or "").splitlines():
                    if not raw2.strip() or raw2.lstrip().startswith("#"):
                        continue
                    content, quote, cont, _ = _scan_free(raw2, quote)
                    if not content.strip() and quote is None and not cont:
                        continue
                    buf.append(content)
                    if not cont:
                        yield ("".join(buf), first, no, cond, [], None)
                        buf = []
                        quote = None
            continue
        if cpp.dead:
            continue
        if not raw.strip():
            continue
        content, quote, cont, comment = _scan_free(raw, quote)
        if comment:
            yield (None, no, no, False, [comment], None)
        if not content.strip() and quote is None and not cont:
            continue                  # comment-only line, possibly between continuations
        if not buf:
            first = no
            cond = cpp.cond
        buf.append(content)
        if not cont:
            yield ("".join(buf), first, no, cond, [], None)
            buf = []
            quote = None
    if buf:
        yield ("".join(buf), first, len(lines), cond, [], None)


def _fixed_line(raw: str) -> str:
    """Expand DEC tab format to columns and drop sequence columns 73+."""
    if raw.startswith("\t"):
        rest = raw[1:]
        if rest[:1] in "123456789":
            raw = "     " + rest[0] + rest[1:]
        else:
            raw = "      " + rest
    elif "\t" in raw[:6]:
        head, _, rest = raw.partition("\t")
        if rest[:1] in "123456789":
            raw = head.ljust(5)[:5] + rest[0] + rest[1:]
        else:
            raw = head.ljust(6)[:6] + rest
    return raw[:72]


def _strip_fixed_comment(text: str, quote: str | None) -> tuple[str, str | None, str]:
    if quote is None and "'" not in text and '"' not in text:
        i = text.find("!")
        return (text, None, "") if i < 0 else (text[:i], None, text[i + 1:].strip())
    out = []
    comment = ""
    for i, c in enumerate(text):
        if quote is not None:
            out.append(c)
            if c == quote:
                quote = None          # a doubled quote reopens on the next char
            continue
        if c in "'\"":
            quote = c
            out.append(c)
        elif c == "!":
            comment = text[i + 1:].strip()
            break
        else:
            out.append(c)
    return "".join(out), quote, comment


def _physical_to_logical_fixed(lines: list[str], cpp: _Cpp):
    buf: list[str] = []
    first = last = 0
    label = ""
    cond = False
    quote = None
    for no, raw0 in enumerate(lines, 1):
        raw = raw0.rstrip("\r\n")
        if raw.startswith("#"):
            inc = cpp.directive(raw)
            if inc:
                if buf:
                    yield ("".join(buf), first, last, cond, label, None)
                    buf = []
                yield ("", no, no, cpp.cond, "", inc)
            continue
        if cpp.dead:
            continue
        if not raw.strip():
            continue
        c0 = raw[0]
        if c0 in "cC*!dD":
            yield (None, no, no, False, raw[1:72].strip(), None)
            continue
        line = _fixed_line(raw)
        if line[:6].lstrip().startswith("!"):
            yield (None, no, no, False, line.lstrip()[1:].strip(), None)
            continue
        if not line.strip():
            continue
        is_cont = len(line) > 5 and line[5] not in " 0"
        body = line[6:]
        if is_cont and buf:
            body, quote, comment = _strip_fixed_comment(body, quote)
            if comment:
                yield (None, no, no, False, comment, None)
            buf.append(body if quote else body.rstrip())
            last = no
            continue
        if buf:
            yield ("".join(buf), first, last, cond, label, None)
        quote = None
        body, quote, comment = _strip_fixed_comment(body, None)
        if comment:
            yield (None, no, no, False, comment, None)
        lab = line[:5].strip()
        label = lab if lab.isdigit() else ""
        buf = [body if quote else body.rstrip()]
        first = last = no
        cond = cpp.cond
    if buf:
        yield ("".join(buf), first, last, cond, label, None)


_HOLLERITH_PREV = set("(,/=")
_WS = re.compile(r"\s+")
_MAYBE_HOLLERITH = re.compile(r"\d[hH]")


def _tokenize(content: str) -> tuple[str, list[str], int]:
    """Mask strings and Hollerith constants; lowercase and collapse the rest."""
    if content.isascii() and "'" not in content and '"' not in content \
            and not _MAYBE_HOLLERITH.search(content):
        # nothing to mask (most statements): the character loop below would only
        # lowercase and turn tabs into blanks
        return _WS.sub(" ", content.lower()).strip(), [], 0
    out: list[str] = []
    strs: list[str] = []
    i, n = 0, len(content)
    prev_sig = ""                     # previous non-blank char outside strings
    holl = 0
    while i < n:
        c = content[i]
        if c in "'\"":
            j = i + 1
            while j < n:
                if content[j] == c:
                    if j + 1 < n and content[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            strs.append(content[i:j + 1])
            out.append(f"@{len(strs) - 1}")
            i = j + 1
            prev_sig = "@"
            continue
        if c.isdigit() and (prev_sig in _HOLLERITH_PREV or prev_sig == ""):
            j = i
            while j < n and content[j].isdigit():
                j += 1
            if j < n and content[j] in "hH" and prev_sig != "":
                count = int(content[i:j])
                if 0 < count <= n - j - 1 + 8:
                    # fixed form strips trailing blanks, so a Hollerith may run short
                    lit = content[j + 1:j + 1 + count].ljust(count)
                    strs.append("'" + lit.replace("'", "''") + "'")
                    out.append(f"@{len(strs) - 1}")
                    holl += 1
                    i = j + 1 + count
                    prev_sig = "@"
                    continue
            out.append(content[i:j].lower())
            prev_sig = content[j - 1]
            i = j
            continue
        out.append(c.lower() if c != "\t" else " ")
        if not c.isspace():
            prev_sig = c
        i += 1
    code = _WS.sub(" ", "".join(out)).strip()
    return code, strs, holl


def split_semicolons(code: str) -> list[str]:
    return [p.strip() for p in code.split(";") if p.strip()]


def restore(code: str, strs: list[str]) -> str:
    if not strs:
        return code
    return re.sub(r"@(\d+)", lambda m: strs[int(m.group(1))], code)


def read(text: str, path: str, form: str | None = None, include_text=None) -> Source:
    """``include_text(target)``: the text of a file a ``#include`` inside a continued
    free-form statement names (None: unknown), spliced in where it stands."""
    form = form or form_of(path)
    if form == "unknown":
        form = sniff_form(text)
    lines = text.splitlines()
    src = Source(path=path, form=form, lines=len(lines))
    cpp = _Cpp()
    src.preprocessed = any(l.startswith("#") for l in lines[:2000])
    if form == "fixed":
        gen = _physical_to_logical_fixed(lines, cpp)
        for content, first, last, cond, extra, inc in gen:
            if content is None:
                if extra:
                    src.comments.append((first, extra))
                continue
            if inc:
                src.stmts.append(Stmt(code="include @0", strs=[f"'{inc}'"], line=first,
                                      end=last, cond=cond, origin=path,
                                      text=f"include '{inc}'"))
                continue
            _emit(src, content, first, last, extra, cond, path)
    else:
        for content, first, last, cond, comments, inc in _physical_to_logical_free(
                lines, cpp, include_text):
            if content is None:
                src.comments.extend((first, c) for c in comments)
                continue
            if inc:
                src.stmts.append(Stmt(code="include @0", strs=[f"'{inc}'"], line=first,
                                      end=last, cond=cond, origin=path,
                                      text=f"include '{inc}'"))
                continue
            label = ""
            m = re.match(r"\s*(\d{1,5})\s+(?=\S)", content)
            if m:
                label = m.group(1)
                content = content[m.end():]
            _emit(src, content, first, last, label, cond, path)
    src.macros = cpp.defines
    return src


def _emit(src: Source, content: str, first: int, last: int, label: str, cond: bool,
          path: str) -> None:
    code, strs, holl = _tokenize(content)
    parts = split_semicolons(code) if ";" in code else [code]
    for k, part in enumerate(parts):
        if not part:
            continue
        src.stmts.append(Stmt(code=part, strs=strs, line=first, end=last,
                              label=label if k == 0 else "", cond=cond, origin=path,
                              text=restore(part, strs), hollerith=holl if k == 0 else 0))
