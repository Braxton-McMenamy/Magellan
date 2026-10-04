"""A structural Fortran parser: program units, declarations and references.

This is not a compiler front end. It recognises the statements that decide the
graph -- unit headers and ends, ``contains``, ``use``, declarations and their
attributes, ``COMMON``/``EQUIVALENCE``/``SAVE``/``EXTERNAL``, interface blocks,
derived types and their bindings -- and, in executable statements, every
``CALL``, every ``name(...)`` that might be a function reference, and which
names are assigned. Whether ``f(x)`` is a call or an array element is decided
later, in :mod:`magellan_lite.polyglot.fortran.frontend`, against the declarations collected
here.

Every regex runs on :attr:`Stmt.code`, which is lowercase, whitespace-collapsed
and has strings masked, so neither case nor layout nor string contents can
confuse it. Blank-insensitive keyword spellings (``go to``/``goto``, ``end
if``/``endif``, ``double precision``, ``block data``) are accepted; fully
blank-free F77 (``CALLFOO(X)``) is not.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from magellan_lite.polyglot.fortran.source import Source, Stmt

_PATTERNS: dict[str, re.Pattern] = {}


def _rx(pattern: str) -> re.Pattern:
    """``re.compile`` through a plain dict. ``re.match(p, s)`` pays for its own cache
    lookup on every call, and the parser makes ~20 such calls per statement (LAPACK:
    17M of them, 10% of a build)."""
    got = _PATTERNS.get(pattern)
    if got is None:
        got = _PATTERNS[pattern] = re.compile(pattern)
    return got

# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


@dataclass
class Var:
    name: str
    type: str | None = None
    dims: str | None = None
    attrs: set[str] = field(default_factory=set)
    intent: str = ""
    init: str | None = None
    line: int = 0
    typed_line: int = 0

    @property
    def is_array(self) -> bool:
        return self.dims is not None

    @property
    def is_char(self) -> bool:
        return bool(self.type and self.type.startswith("character"))


@dataclass
class Use:
    module: str
    only: list[str] | None            # local names imported when ONLY is given
    renames: dict[str, str]           # local -> name in the module
    line: int
    intrinsic: bool = False
    path: str = ""                    # file of the USE (picks among same-named modules)


@dataclass
class Ref:
    kind: str                         # call | fref | name | write | mutate | bound | passed
    name: str
    line: int
    col: int
    args: int = 0
    keywords: list[str] = field(default_factory=list)
    conditional: bool = False
    obj: str = ""                     # base of a `%` chain
    chain: list[str] = field(default_factory=list)
    has_paren: bool = False
    alt_returns: int = 0
    in_call: bool = False             # a `passed` actual of a CALL statement (not a function)
    seq: int = 0                      # statement number: execution-text order, which the
    #                                   line is not for statements spliced in by INCLUDE
    loops: tuple[int, ...] = ()       # the DO loops around the statement, outermost first
    argtext: str = ""                 # the argument list, to pick a generic's specific by
    #                                   argument type (split by `positionals` when needed)


@dataclass
class Binding:
    name: str
    impl: str
    attrs: set[str]
    line: int
    iface: str = ""
    kind: str = "procedure"           # procedure | generic | final
    specifics: list[str] = field(default_factory=list)


@dataclass
class Generic:
    name: str
    specifics: list[str]
    line: int
    bodies: list[str] = field(default_factory=list)   # specifics given as interface bodies


@dataclass(eq=False)
class Unit:
    kind: str                         # file program module submodule subroutine function
    #                                   blockdata type interface iface-body separate enum
    name: str
    path: str
    line: int = 0
    end: int = 0
    parent: "Unit | None" = None
    header: str = ""
    args: list[str] = field(default_factory=list)
    result: str | None = None
    prefixes: set[str] = field(default_factory=set)
    rtype: str | None = None          # function result type from the prefix
    bind: str | None = None           # None: no bind(C); "" default name
    vars: dict[str, Var] = field(default_factory=dict)
    uses: list[Use] = field(default_factory=list)
    implicit_none: bool = False
    implicit_stated: bool = False
    implicit: dict[str, str] = field(default_factory=dict)
    commons: dict[str, list[str]] = field(default_factory=dict)
    common_lines: dict[str, int] = field(default_factory=dict)
    common_origin: dict[str, str] = field(default_factory=dict)
    equivalences: list[list[str]] = field(default_factory=list)
    externals: set[str] = field(default_factory=set)
    intrinsics: set[str] = field(default_factory=set)
    save_all: bool = False
    saved: set[str] = field(default_factory=set)
    data_names: set[str] = field(default_factory=set)
    entries: list[tuple[str, list[str], int]] = field(default_factory=list)
    includes: list[tuple[str, int, str | None]] = field(default_factory=list)
    children: list["Unit"] = field(default_factory=list)
    in_contains: bool = False
    generics: dict[str, Generic] = field(default_factory=dict)
    iface_bodies: dict[str, "Unit"] = field(default_factory=dict)
    access_default: str = "public"
    public: set[str] = field(default_factory=set)
    private: set[str] = field(default_factory=set)
    stmt_funcs: dict[str, tuple[list[str], int]] = field(default_factory=dict)
    comments: list[str] = field(default_factory=list)
    legacy: Counter = field(default_factory=Counter)
    own: list[str] = field(default_factory=list)     # normalized own statements (body hash)
    refs: list[Ref] = field(default_factory=list)
    io: bool = False
    stops: bool = False
    has_exec: bool = False
    stmt_count: int = 0
    unresolved_includes: list[str] = field(default_factory=list)
    macros: set[str] = field(default_factory=set)   # (file) cpp macros it and its includes define
    namelists: dict[str, list[str]] = field(default_factory=dict)
    cond_lines: set[int] = field(default_factory=set)
    # derived types
    extends: str | None = None
    type_attrs: set[str] = field(default_factory=set)
    bindings: list[Binding] = field(default_factory=list)
    # submodules
    ancestor: str | None = None
    # interface blocks
    abstract: bool = False
    enums: list["Unit"] = field(default_factory=list)
    label_lines: dict[str, int] = field(default_factory=dict)
    goto_targets: list[tuple[int, str]] = field(default_factory=list)
    # construct tracking while parsing
    depth: int = 0
    do_labels: list[str] = field(default_factory=list)
    loops: list[int] = field(default_factory=list)     # open DO loops (ids)

    @property
    def is_procedure(self) -> bool:
        return self.kind in ("subroutine", "function", "separate")

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


# --------------------------------------------------------------------------
# lexical helpers
# --------------------------------------------------------------------------

def close_paren(s: str, i: int) -> int:
    """Index just past the parenthesis group opening at ``s[i] == '('``."""
    depth = 0
    for j in range(i, len(s)):
        c = s[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(s)


def split_top(s: str, sep: str = ",") -> list[str]:
    if "(" not in s and "[" not in s and ")" not in s and "]" not in s:
        if sep not in s:
            return [s.strip()] if s.strip() else []
        return [p.strip() for p in s.split(sep)]
    out, depth, cur = [], 0, []
    i = 0
    while i < len(s):
        c = s[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        if c == sep and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    tail = "".join(cur).strip()
    if tail or out:
        out.append(tail)
    return out


def top_level_index(s: str, token: str) -> int:
    depth = 0
    i = 0
    while i < len(s):
        c = s[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif depth == 0 and s.startswith(token, i):
            return i
        i += 1
    return -1


_TYPE_HEAD = re.compile(
    r"(double\s*precision|double\s*complex|integer|real|complex|logical|character|byte"
    r"|type\s*\(|class\s*\(|procedure\s*\(|typeof\s*\(|classof\s*\()")


def parse_typespec(code: str, i: int = 0) -> tuple[str, int] | None:
    """Type specifier at ``code[i:]`` -> (normalized type, end index)."""
    m = _TYPE_HEAD.match(code, i)
    if not m:
        return None
    word = _rx(r"\s+").sub("", m.group(1))
    j = m.end()
    if word in ("type(", "class(", "procedure(", "typeof(", "classof("):
        k = close_paren(code, j - 1)
        inner = code[j:k - 1].strip().replace(" ", "")
        return f"{word}{inner})", k
    if j < len(code) and (code[j].isalnum() or code[j] == "_"):
        return None                                   # an identifier like `realpart`
    if word == "doubleprecision":
        return "real*8", j
    if word == "doublecomplex":
        return "complex*16", j
    kind = ""
    k = j
    while k < len(code) and code[k] == " ":
        k += 1
    if k < len(code) and code[k] == "*":
        k += 1
        while k < len(code) and code[k] == " ":
            k += 1
        if k < len(code) and code[k] == "(":
            e = close_paren(code, k)
            kind = code[k + 1:e - 1].replace(" ", "")
            k = e
        else:
            mm = _rx(r"\d+").match(code[k:])
            if mm:
                kind = mm.group(0)
                k += mm.end()
        j = k
    elif k < len(code) and code[k] == "(" and word != "byte":
        e = close_paren(code, k)
        inner = code[k + 1:e - 1].replace(" ", "")
        parts = split_top(inner)
        if word == "character":
            ln = ""
            for idx, p in enumerate(parts):
                if p.startswith("len="):
                    ln = p[4:]
                elif "=" not in p and idx == 0:
                    ln = p
            kind = ln or "1"
        else:
            p0 = parts[0] if parts else ""
            kind = p0[5:] if p0.startswith("kind=") else p0
        j = e
    return _norm_type(word, kind), j


_DEFAULT_KIND = {"integer": "4", "real": "4", "logical": "4", "complex": "8"}


def _norm_type(word: str, kind: str) -> str:
    if word == "byte":
        return "integer*1"
    if word == "character":
        return f"character*{kind or '1'}"
    if not kind or kind == _DEFAULT_KIND.get(word):
        return word
    if word == "complex" and kind == "16":
        return "complex*16"
    if kind.isdigit():
        if word == "complex":
            return f"complex*{int(kind) * 2}"
        return f"{word}*{kind}"
    return f"{word}({kind})"


def type_size(t: str | None) -> tuple[str, int]:
    """(base, bytes-or-1) for a normalized type; symbolic kinds stay symbolic."""
    if not t:
        return ("?", 1)
    m = _rx(r"(integer|real|logical|complex|character)\*(\d+)$").match(t)
    if m:
        return (m.group(1), int(m.group(2)))
    return (t, {"integer": 4, "real": 4, "logical": 4, "complex": 8}.get(t, 1))


def shape_of(dims: str | None, attrs: set[str] | frozenset = frozenset()) -> str:
    if dims is None:
        return "scalar"
    parts = split_top(dims)
    if dims.strip() == "..":
        return "assumed-rank"
    rank = len(parts)
    if all(p.strip() == ":" for p in parts):
        kind = "deferred" if attrs & {"allocatable", "pointer"} else "assumed-shape"
    elif parts and parts[-1].strip().endswith("*"):
        kind = "assumed-size"
    elif any(p.strip().endswith(":") for p in parts):
        kind = "assumed-shape"
    else:
        kind = "explicit"
    return f"{kind}/{rank}"


def parse_entities(text: str) -> list[tuple[str, str | None, str | None, str | None,
                                            str | None]]:
    """``a(10), b*8, c = 1, d(3)[*]`` -> [(name, dims, char_len, init, codims)]."""
    out = []
    for item in split_top(text):
        m = _rx(r"([a-z][\w$]*)\s*").match(item)
        if not m:
            continue
        name = m.group(1)
        k = m.end()
        dims = clen = init = codims = None
        if k < len(item) and item[k] == "(":
            e = close_paren(item, k)
            dims = item[k + 1:e - 1].replace(" ", "")
            k = e
        rest = item[k:].strip()
        if rest.startswith("["):                     # coarray: x(10)[*]
            e = rest.find("]")
            codims = rest[1:e].replace(" ", "") if e > 0 else "*"
            rest = rest[e + 1:].strip() if e > 0 else ""
        if rest.startswith("*"):
            mm = _rx(r"\*\s*(\([^)]*\)|\d+)").match(rest)
            if mm:
                clen = mm.group(1).strip("()").replace(" ", "")
                rest = rest[mm.end():].strip()
        if rest.startswith("=>"):
            init = rest[2:].strip()
        elif rest.startswith("="):
            init = rest[1:].strip()
        elif rest.startswith("/"):                   # F77 extension: real x /1.0/
            init = rest.strip("/ ")
        out.append((name, dims, clen, init, codims))
    return out


_OPS = re.compile(r"\.[a-z]+\.")
#: specifiers whose variable the statement sets: `read(u, *, iostat=ios)`, `allocate(.., stat=e)`
_STATUS_SPEC = re.compile(r"\b(iostat|stat|errmsg|iomsg|exist|opened|number|named|nextrec"
                          r"|newunit|size|id)\s*=\s*$")
#: intrinsics that ask about an argument without reading its value
INQUIRY = frozenset({"size", "shape", "lbound", "ubound", "allocated", "associated", "present",
                     "len", "kind", "rank", "is_contiguous", "storage_size", "c_loc", "loc",
                     "sizeof", "c_sizeof", "c_associated", "same_type_as", "extends_type_of",
                     "bit_size", "digits", "epsilon", "huge", "tiny", "precision", "range",
                     "radix", "maxexponent", "minexponent", "new_line", "ieee_value",
                     "ieee_support_nan", "ieee_support_inf", "ieee_support_datatype",
                     "ieee_support_standard"})
_IDENT = re.compile(r"(?<![\w.$%])([a-z][\w$]*)")
_INQUIRY_CALL = re.compile(r"(?<![\w%])(" + "|".join(sorted(INQUIRY)) + r")\s*\(")


def positionals(inner: str) -> tuple[str, ...]:
    """The positional actual arguments of an argument list, as text."""
    return tuple(p for p in split_top(inner) if p and not _rx(r"[a-z]\w*\s*=(?![=>])").match(p))


def arglist(inner: str) -> tuple[int, list[str], int]:
    """Positional count, keyword names and alternate-return actuals of an argument list."""
    if not inner.strip():
        return 0, [], 0
    parts = split_top(inner)
    pos, kws, alt = 0, [], 0
    for p in parts:
        m = _rx(r"([a-z]\w*)\s*=(?![=>])").match(p)
        if m:
            kws.append(m.group(1))
        else:
            pos += 1
            if _rx(r"[*&]\s*\d+$").match(p):
                alt += 1
    return pos, kws, alt


def scan(expr: str, line: int, cond: bool, base_col: int = 0) -> list[Ref]:
    """Every identifier reference in an expression, with call shape where it has one."""
    # a filler that is neither blank nor part of a name: `lwa .lt. (n*3)` must not read
    # as `lwa (n*3)`, a function reference
    s = _OPS.sub(lambda m: "~" * len(m.group(0)), expr)
    out: list[Ref] = []
    components: set[int] = set()
    inquiry: list[tuple[int, int]] = []
    # (a(i), i = 1, n): the bounds end at the closing parenthesis, which tells an implied
    # DO from a keyword specifier (open(u, iostat=ios, iomsg=msg) sets ios)
    implied = set(_rx(r",\s*([a-z]\w*)\s*=(?![=>])[^,()=]*,[^=]*?\)").findall(s))
    for m in _INQUIRY_CALL.finditer(s):
        inquiry.append((m.end(), close_paren(s, m.end() - 1)))
    for m in _IDENT.finditer(s):
        name, start, end = m.group(1), m.start(), m.end()
        if start in components:
            continue
        if name.endswith("_") and s[end:end + 1] == "@":
            # ck_'text': a character constant of kind ck (json-fortran writes every
            # literal this way); the kind is read, `ck_` is not a variable
            out.append(Ref("name", name[:-1], line, base_col + start, conditional=cond))
            continue
        if name in implied or (inquiry and any(a <= start < b for a, b in inquiry)
                               and not s[end:end + 1] == "("):
            out.append(Ref("inquiry", name, line, base_col + start, conditional=cond))
            continue
        k = end
        while k < len(s) and s[k] == " ":
            k += 1
        if k < len(s) and s[k] == "=" and s[k + 1:k + 2] not in ("=", ">"):
            continue                                  # keyword argument / implied-do var
        j = start - 1
        while j >= 0 and s[j] == " ":
            j -= 1
        if j > 0 and s[j] == "=" and _STATUS_SPEC.search(s[:start]):
            out.append(Ref("write", name, line, base_col + start, conditional=cond))
            continue                                  # iostat=ios, stat=ierr: set, not read
        has_paren = k < len(s) and s[k] == "("
        inner = ""
        if has_paren:
            e = close_paren(s, k)
            inner = s[k + 1:e - 1]
            k = e
            if s[k:].lstrip().startswith("::"):
                continue              # [character(len=8) :: ...], a typed array constructor
        # a `%` chain: a%b(i)%c(...)
        chain = []
        last_paren, last_inner = has_paren, inner
        while True:
            q = k
            while q < len(s) and s[q] == " ":
                q += 1
            if q < len(s) and s[q] == "%":
                mm = _rx(r"%\s*([a-z]\w*)\s*").match(s[q:])
                if not mm:
                    break
                chain.append(mm.group(1))
                components.add(q + mm.start(1))
                k = q + mm.end()
                last_paren, last_inner = False, ""
                if k < len(s) and s[k] == "(":
                    e = close_paren(s, k)
                    last_paren, last_inner = True, s[k + 1:e - 1]
                    k = e
            else:
                break
        if chain:
            out.append(Ref("name", name, line, base_col + start, conditional=cond))
            pos, kws, _ = arglist(last_inner) if last_paren else (0, [], 0)
            out.append(Ref("bound", chain[-1], line, base_col + start + 1, args=pos,
                           keywords=kws, conditional=cond, obj=name, chain=chain,
                           has_paren=last_paren,
                           argtext=last_inner if last_paren else ""))
            continue
        if has_paren:
            pos, kws, alt = arglist(inner)
            out.append(Ref("fref", name, line, base_col + start, args=pos, keywords=kws,
                           conditional=cond, has_paren=True, argtext=inner))
            # a bare name as an actual argument: a procedure passed along, or a variable
            # the callee may read or write (which, its dummy's intent says)
            if name not in INQUIRY:           # present(x), size(x): x's value is not used
                out.extend(r for r in _passed(inner, name, line, base_col + start, cond)
                           if r.name not in implied)
        else:
            out.append(Ref("name", name, line, base_col + start, conditional=cond))
    return out


def _header_entities(inner: str, line: int, cond: bool) -> list[Ref]:
    """Refs of a DO CONCURRENT, FORALL, ASSOCIATE or SELECT TYPE/RANK header.

    ``i = 1:n`` and ``a => expr`` name a construct entity the header sets (a write), not a
    read: scanning them as expressions made every such index and associate name a local
    "read before set". The bounds, selectors and a mask are read.
    """
    out: list[Ref] = []
    tm = _rx(r"\s*(?:integer|[a-z]\w*\s*\([^)]*\))\s*::").match(inner)  # integer(8) ::
    body = inner[tm.end():] if tm else inner
    for item in split_top(body):
        m = _rx(r"([a-z]\w*)\s*(?:=>|=(?!=))\s*(.*)$").match(item.strip())
        if m:
            out.append(Ref("write", m.group(1), line, 0, conditional=cond))
            out.extend(scan(m.group(2), line, cond))
        else:
            out.extend(scan(item, line, cond))
    return out


def _internal_file(u: Unit, ctl: str, refs: list[Ref], line: int, cond: bool) -> str:
    """``write (buf, fmt) x``: a character unit is an internal file the WRITE sets.

    Returns the control list left to scan. An integer unit number is read, and so is a
    unit whose declaration this scope does not show (host or module variables).
    """
    items = split_top(ctl)
    for i, item in enumerate(items):
        m = _rx(r"(?:unit\s*=\s*)?([a-z]\w*)\s*(\(.*\))?$").match(item)
        if not m or (i > 0 and not item.startswith("unit")):
            continue
        v = u.vars.get(m.group(1))
        if v is None or not v.is_char:
            return ctl
        refs.append(Ref("mutate" if m.group(2) else "write", m.group(1), line, 0,
                        conditional=cond))
        return ",".join(items[:i] + [m.group(2) or ""] + items[i + 1:])
    return ctl


def _passed(inner: str, callee: str, line: int, col: int, cond: bool,
            in_call: bool = False) -> list[Ref]:
    """``passed`` refs for bare-name actuals: ``obj`` is the callee, ``args`` the slot."""
    out = []
    for i, p in enumerate(split_top(inner)):
        pm = _rx(r"(?:([a-z]\w*)\s*=\s*)?([a-z]\w*)").fullmatch(p.strip())
        if pm:
            out.append(Ref("passed", pm.group(2), line, col, conditional=cond, obj=callee,
                           args=i, keywords=[pm.group(1)] if pm.group(1) else [],
                           in_call=in_call))
    return out


def match_assignment(code: str) -> tuple[str, str, str, bool, int] | None:
    """``lhs = rhs`` -> (base name, lhs text, rhs, is element/component, rhs offset)."""
    m = _rx(r"([a-z][\w$]*)\s*").match(code)
    if not m:
        return None
    k = m.end()
    sub = False
    while k < len(code):
        c = code[k]
        if c == "(":
            k = close_paren(code, k)
            sub = True
        elif c == "[":                               # coindexed: x[img] = ...
            e = code.find("]", k)
            if e < 0:
                return None
            k = e + 1
            sub = True
        elif c == "%":
            mm = _rx(r"%\s*[a-z]\w*\s*").match(code[k:])
            if not mm:
                return None
            k += mm.end()
            sub = True
        elif c == " ":
            k += 1
        else:
            break
    if k < len(code) and code[k] == "=" and code[k + 1:k + 2] != "=":
        ptr = code[k + 1:k + 2] == ">"
        rhs_at = k + (2 if ptr else 1)
        return m.group(1), code[:k].strip(), code[rhs_at:], sub, rhs_at
    return None


# --------------------------------------------------------------------------
# statement patterns
# --------------------------------------------------------------------------
_PREFIX_WORDS = ("recursive", "pure", "elemental", "impure", "non_recursive", "module",
                 "simple")
_CONSTRUCT_NAME = re.compile(
    r"[a-z]\w*\s*:(?!:)\s*(?=(do|if|select|block|associate|forall|where|critical|change)\b)")
_END_UNIT = re.compile(
    r"end\s*(subroutine|function|program|module|submodule|block\s*data|procedure)?"
    r"(?:\s+[a-z]\w*)?$")
_END_CONSTRUCT = re.compile(
    r"end\s*(if|do|select|where|forall|associate|block(?!\s*data)|critical|team|enum|type"
    r"|interface)\b")


def _unit_header(code: str):
    """Recognise a subroutine/function header, returning its parts or None."""
    s = code
    prefixes: set[str] = set()
    rtype = None
    while True:
        m = _rx(r"(recursive|pure|elemental|impure|non_recursive|module|simple)\s+").match(s)
        if m:
            prefixes.add(m.group(1))
            s = s[m.end():]
            continue
        if rtype is None:
            t = parse_typespec(s)
            if t is not None:
                rest = s[t[1]:].lstrip()
                if re.match(r"(recursive|pure|elemental|impure|non_recursive|module|simple"
                            r"|function)\b",
                            rest):
                    rtype = t[0]
                    s = rest
                    continue
        break
    m = _rx(r"(subroutine|function)\s*([a-z][\w$]*)\s*").match(s)
    if not m:
        return None
    kind, name = m.group(1), m.group(2)
    rest = s[m.end():]
    if rest.startswith("="):
        return None                                   # `function = 1`, an assignment
    args: list[str] = []
    if rest.startswith("("):
        e = close_paren(rest, 0)
        args = [a.strip() for a in split_top(rest[1:e - 1]) if a.strip()]
        rest = rest[e:].strip()
    result = None
    bind = None
    mr = _rx(r"result\s*\(\s*([a-z]\w*)\s*\)").search(rest)
    if mr:
        result = mr.group(1)
    mb = _rx(r"bind\s*\(\s*c\s*(?:,\s*name\s*=\s*@(\d+)\s*)?\)").search(rest)
    if mb:
        bind = mb.group(1) or ""
    return kind, name, args, result, bind, prefixes, rtype


_IO = re.compile(r"(read|write|print|open|close|inquire|rewind|backspace|end\s*file|flush|wait)"
                 r"\b\s*")


def _starts_unit(code: str) -> bool:
    """A statement that opens a unit (it then belongs to the file it is written in)."""
    return bool(re.match(r"(program|module|submodule|block\s*data|interface|abstract\s+interface"
                         r"|type\b(?!\s*\())", code)) or (match_assignment(code) is None
                                                          and _unit_header(code) is not None)


class Parser:
    def __init__(self, src: Source, loader: Callable[[str, str], "Source | None"] | None = None):
        self.src = src
        self.loader = loader
        self.file = Unit(kind="file", name=src.path, path=src.path, line=1, end=src.lines)
        self.file.macros |= src.macros
        self.stack: list[Unit] = [self.file]
        self.include_depth = 0
        self.form = src.form
        self.included: list[Source] = []
        self.seq = 0

    # -- driver ------------------------------------------------------------
    def run(self) -> Unit:
        for st in self.src.stmts:
            self.statement(st)
        while len(self.stack) > 1:                  # a missing END: close what is open
            u = self.stack.pop()
            u.end = u.end or self.src.lines
        self._comments()
        return self.file

    def _comments(self) -> None:
        for src in [self.src] + self.included:
            units = [u for u in self.file.walk() if u is not self.file and u.path == src.path]
            for line, text in src.comments:
                owner = self.file if src is self.src else None
                for u in units:
                    if u.line - 3 <= line <= (u.end or u.line) and (
                            owner is None or owner is self.file or u.line >= owner.line):
                        owner = u
                if owner is not None:
                    owner.comments.append(text)

    @property
    def cur(self) -> Unit:
        return self.stack[-1]

    def push(self, u: Unit, attach: bool = True) -> Unit:
        parent = self.cur
        u.parent = parent
        if attach:
            parent.children.append(u)
            parent.own.append(f"<{u.kind} {u.name}>")
        self.stack.append(u)
        return u

    def pop(self, st: Stmt) -> None:
        u = self.stack.pop()
        u.end = st.end
        parent = self.stack[-1] if self.stack else None
        if parent is not None and u not in parent.children:
            # interface blocks and bodies are part of their host's text, not nodes
            parent.own.extend(u.own)

    # -- statements --------------------------------------------------------
    def statement(self, st: Stmt) -> None:
        cur = self.cur
        n0 = len(cur.refs)
        loops = tuple(cur.loops)
        self._statement(st)
        self.seq += 1
        for r in cur.refs[n0:]:
            if not r.seq:
                r.seq = self.seq
                r.loops = loops

    def _statement(self, st: Stmt) -> None:
        cur = self.cur
        if st.at and st.origin != cur.path and not (cur.kind == "file" and
                                                    st.origin == self.file.path) \
                and not _starts_unit(st.code):
            # a fragment of another file inside this unit: locate it at the INCLUDE
            st = Stmt(code=st.code, strs=st.strs, line=st.at, end=st.at, label=st.label,
                      cond=st.cond, origin=st.origin, text=st.text, hollerith=st.hollerith,
                      at=st.at)
        code = st.code
        if st.cond:
            cur.cond_lines.add(st.line)
        m = _CONSTRUCT_NAME.match(code)
        if m:
            code = code[m.end():]

        if _rx(r"include\s*@\d+$").match(code):
            self.include(st)
            return

        own = st.origin == cur.path or cur.kind == "file"

        def keep() -> None:
            if own:
                cur.own.append(_rx(r"\s+").sub("", st.text) if not st.label
                               else st.label + ":" + _rx(r"\s+").sub("", st.text))
                cur.stmt_count += 1

        if st.hollerith and own:
            cur.legacy["hollerith"] += st.hollerith
        if st.label:
            cur.label_lines.setdefault(st.label, st.line)

        # ---- inside a derived-type definition ----
        if cur.kind == "type":
            if _rx(r"end\s*type\b").match(code):
                self.pop(st)
                return
            keep()
            self.type_body(cur, code, st)
            return
        if cur.kind == "enum":
            if _rx(r"end\s*(enum|enumeration)\b").match(code):
                self.pop(st)
                return
            keep()
            m = _rx(r"enumerator\s*(?:::)?\s*(.*)").match(code)
            if m:
                host = cur.parent
                for name, _, _, init, _ in parse_entities(m.group(1)):
                    cur.vars[name] = Var(name, "integer", None, {"parameter"}, init=init or "",
                                         line=st.line)
                    if not cur.name:           # enum, bind(c): enumerators are host constants
                        host.vars[name] = Var(name, "integer", None, {"parameter"},
                                              init=init or "", line=st.line)
            return
        # ---- inside an interface block ----
        if cur.kind == "interface":
            if _rx(r"end\s*interface\b").match(code):
                self.pop(st)
                return
            keep()
            m = _rx(r"(?:module\s+)?procedure\s*(?:::)?\s*(.*)").match(code)
            if m and not _rx(r"module\s+procedure\s*\(").match(code):
                names = [n.strip() for n in split_top(m.group(1)) if n.strip()]
                if cur.name:
                    g = cur.parent.generics.setdefault(cur.name, Generic(cur.name, [], st.line))
                    g.specifics.extend(names)
                return
            hdr = _unit_header(code)
            if hdr:
                kind, name, args, result, bind, prefixes, rtype = hdr
                body = Unit(kind="iface-body", name=name, path=st.origin, line=st.line,
                            header=code, args=args, result=result, prefixes=prefixes,
                            rtype=rtype, bind=self._bind(bind, st))
                body.legacy["iface-" + kind] = 1
                host = cur.parent
                host.iface_bodies[name] = body
                body.abstract = cur.abstract
                if cur.name:
                    g = host.generics.setdefault(cur.name, Generic(cur.name, [], st.line))
                    g.specifics.append(name)
                    g.bodies.append(name)
                if "module" in prefixes:
                    body.legacy["separate-interface"] = 1
                self.push(body, attach=False)
                return
            return
        if cur.kind == "iface-body":
            keep()
            if _END_UNIT.match(code) and not _END_CONSTRUCT.match(code):
                self.pop(st)
                return
            m = _rx(r"(abstract\s+)?interface\b\s*(.*)$").match(code)
            if m:                         # the interface of a dummy procedure, nested
                self.push(Unit(kind="interface", name=m.group(2).strip(), path=st.origin,
                               line=st.line, header=code, abstract=bool(m.group(1))),
                          attach=False)
                return
            self.spec(cur, code, st)
            return

        # ---- unit ends ----
        if code.startswith("end") and _END_UNIT.match(code) and not _END_CONSTRUCT.match(code) \
                and not _rx(r"end\s*file\b").match(code):
            if cur.kind != "file":
                self.pop(st)
            return

        # ---- unit starts ----
        m = _rx(r"program\s+([a-z]\w*)$").match(code)
        if m and cur.kind == "file":
            self.push(Unit(kind="program", name=m.group(1), path=st.origin, line=st.line,
                           header=code))
            return
        m = _rx(r"module\s+(?!procedure\b|subroutine\b|function\b)([a-z]\w*)$").match(code)
        if m and cur.kind == "file":
            self.push(Unit(kind="module", name=m.group(1), path=st.origin, line=st.line,
                           header=code))
            return
        m = _rx(r"submodule\s*\(\s*([a-z]\w*)\s*(?::\s*([a-z]\w*))?\s*\)\s*([a-z]\w*)$").match(code)
        if m and cur.kind == "file":
            u = Unit(kind="submodule", name=m.group(3), path=st.origin, line=st.line,
                     header=code)
            u.ancestor = m.group(1)
            u.extends = m.group(2) or m.group(1)      # the direct parent (sub)module
            self.push(u)
            return
        m = _rx(r"block\s*data(?:\s+([a-z]\w*))?$").match(code)
        if m and cur.kind == "file":
            self.push(Unit(kind="blockdata", name=m.group(1) or "", path=st.origin,
                           line=st.line, header=code))
            return
        m = _rx(r"module\s+procedure\s+([a-z]\w*)$").match(code)
        if m and cur.kind in ("module", "submodule") and cur.in_contains:
            self.push(Unit(kind="separate", name=m.group(1), path=st.origin, line=st.line,
                           header=code, prefixes={"module"}))
            return
        if match_assignment(code) is None:
            hdr = _unit_header(code)
            if hdr and (cur.kind == "file" or cur.in_contains):
                kind, name, args, result, bind, prefixes, rtype = hdr
                self.push(Unit(kind=kind, name=name, path=st.origin, line=st.line, header=code,
                               args=args, result=result, prefixes=prefixes, rtype=rtype,
                               bind=self._bind(bind, st)))
                if "*" in args:
                    self.cur.legacy["alternate-return"] += 1
                return
            if hdr and not cur.in_contains and cur.kind != "file":
                # a unit header without END for the previous one (F77 decks do this)
                while self.cur.kind != "file" and not self.cur.in_contains:
                    self.pop(st)
                self.statement(st)
                return

        if code == "contains":
            cur.in_contains = True
            keep()
            return
        m = _rx(r"(abstract\s+)?interface\b\s*(.*)$").match(code)
        if m and not _rx(r"interface\s*=").match(code):
            name = m.group(2).strip()
            keep()
            iface = Unit(kind="interface", name=name, path=st.origin, line=st.line,
                         header=code, abstract=bool(m.group(1)))
            self.push(iface, attach=False)
            return
        m = _rx(r"type\s*(?:,\s*(.*?)\s*)?::\s*([a-z]\w*)\s*(?:\([\w\s,]*\))?$").match(code) or \
            _rx(r"type\s+(?!is\b)([a-z]\w*)\s*(?:\([\w\s,]*\))?$()").match(code)
        if m and not code.startswith("type(") and not _rx(r"type\s*\(").match(code):
            if m.re.pattern.startswith("type\\s*(?:"):
                attrs_s, name = m.group(1) or "", m.group(2)
            else:
                attrs_s, name = "", m.group(1)
            t = Unit(kind="type", name=name, path=st.origin, line=st.line, header=code)
            for a in split_top(attrs_s):
                a = a.strip()
                em = _rx(r"extends\s*\(\s*([a-z]\w*)\s*\)").match(a)
                if em:
                    t.extends = em.group(1)
                elif a:
                    t.type_attrs.add(a.replace(" ", ""))
            if cur.kind == "module":
                if "private" in t.type_attrs:
                    cur.private.add(name)
                elif "public" in t.type_attrs:
                    cur.public.add(name)
            self.push(t)
            return
        if _rx(r"enum\s*,\s*bind").match(code):
            e = Unit(kind="enum", name="", path=st.origin, line=st.line, header=code)
            cur.enums.append(e)
            self.push(e, attach=False)
            return
        m = _rx(r"enumeration\s+type\b\s*(?:,\s*[^:]*)?(?:::)?\s*([a-z]\w*)$").match(code)
        if m:                                           # F2023
            e = Unit(kind="enum", name=m.group(1), path=st.origin, line=st.line, header=code)
            cur.enums.append(e)
            self.push(e, attach=False)
            return

        if cur.kind == "file":
            # Outside any unit, a statement can only open an unnamed main program (F77
            # decks often have no PROGRAM statement). Its declarations belong to it, not
            # to the file: attaching them to the file lost every DIMENSION and type.
            if code in ("end",):
                return
            self.push(Unit(kind="program", name="main", path=st.origin, line=st.line,
                           header=""))
            self.statement(st)
            return

        keep()
        if self.spec(cur, code, st):
            return
        self.executable(cur, code, st)

    def _bind(self, bind: str | None, st: Stmt) -> str | None:
        if bind is None:
            return None
        if bind == "":
            return ""
        return st.string(int(bind)).strip()

    def include(self, st: Stmt) -> None:
        cur = self.cur
        m = _rx(r"include\s*@(\d+)").match(st.code)
        target = st.string(int(m.group(1))).strip()
        resolved = None
        sub = None
        if self.loader is not None and self.include_depth < 8:
            sub = self.loader(target, st.origin)
        if sub is not None:
            resolved = sub.path
        if st.origin == cur.path or cur.kind == "file":
            cur.own.append(f"include'{target}'")
        cur.includes.append((target, st.line, resolved))
        cur.legacy["include"] += 1
        if sub is None:
            cur.unresolved_includes.append(target)
            return
        self.file.macros |= sub.macros
        self.include_depth += 1
        try:
            if not any(s is sub for s in self.included):
                self.included.append(sub)
            for s2 in sub.stmts:
                # keep the included file's own line numbers: a procedure defined in an
                # .inc file (ODEPACK has one per file) is located there; a fragment spliced
                # into a unit of another file is mapped back to the INCLUDE line
                s2 = Stmt(code=s2.code, strs=s2.strs, line=s2.line, end=s2.end, label=s2.label,
                          cond=s2.cond or st.cond, origin=s2.origin, text=s2.text,
                          hollerith=s2.hollerith, at=st.at or st.line)
                self.statement(s2)
        finally:
            self.include_depth -= 1

    # -- derived types -----------------------------------------------------
    def type_body(self, t: Unit, code: str, st: Stmt) -> None:
        if code == "contains":
            t.in_contains = True
            return
        if code in ("sequence", "private", "public"):
            t.type_attrs.add(code if code == "sequence" else "private-components"
                             if code == "private" else "public-components")
            return
        if t.in_contains:
            m = _rx(r"procedure\s*(?:\(\s*([a-z]\w*)\s*\))?\s*(?:,\s*(.*?))?\s*::\s*(.*)$"
                    ).match(code) or _rx(r"procedure\s+()()([a-z].*)$").match(code)
            if m:
                attrs = {a.strip().replace(" ", "") for a in split_top(m.group(2) or "")
                         if a.strip()}
                for item in split_top(m.group(3)):
                    mm = _rx(r"([a-z]\w*)\s*(?:=>\s*([a-z]\w*))?").match(item.strip())
                    if mm:
                        t.bindings.append(Binding(mm.group(1), mm.group(2) or
                                                  (m.group(1) if "deferred" in attrs else
                                                   mm.group(1)),
                                                  attrs, st.line, iface=m.group(1) or ""))
                return
            m = _rx(r"generic\s*(?:,\s*(.*?))?\s*::\s*(.+?)\s*=>\s*(.*)$").match(code)
            if m:
                t.bindings.append(Binding(m.group(2).replace(" ", ""), "", set(), st.line,
                                          kind="generic",
                                          specifics=[s.strip() for s in split_top(m.group(3))]))
                return
            m = _rx(r"final\s*(?:::)?\s*(.*)$").match(code)
            if m:
                for s in split_top(m.group(1)):
                    t.bindings.append(Binding(s.strip(), s.strip(), {"final"}, st.line,
                                              kind="final"))
                return
            return
        self.declaration(t, code, st)

    # -- specification statements ----------------------------------------
    def declaration(self, u: Unit, code: str, st: Stmt) -> bool:
        t = parse_typespec(code)
        if t is None:
            return False
        typ, k = t
        rest = code[k:].strip()
        if _rx(r"(recursive|pure|elemental|function|subroutine)\b").match(rest):
            return False
        attrs: set[str] = set()
        dims_attr = None
        intent = ""
        if "::" in rest and top_level_index(rest, "::") >= 0:
            i = top_level_index(rest, "::")
            attr_s, ents = rest[:i].strip().lstrip(","), rest[i + 2:]
            for a in split_top(attr_s):
                a = a.strip()
                if not a:
                    continue
                dm = _rx(r"dimension\s*\((.*)\)$").match(a)
                im = _rx(r"intent\s*\(\s*(in\s*out|inout|in|out)\s*\)").match(a)
                if dm:
                    dims_attr = dm.group(1).replace(" ", "")
                elif im:
                    intent = im.group(1).replace(" ", "")
                else:
                    attrs.add(_rx(r"\s+").sub("", a.split("(")[0]) if not a.startswith("bind")
                              else "bind")
        else:
            ents = rest.lstrip(",").strip()
        for name, dims, clen, init, codims in parse_entities(ents):
            v = u.vars.get(name) or Var(name, line=st.line)
            v.type = typ if clen is None or not typ.startswith("character") \
                else f"character*{clen}"
            v.typed_line = st.line
            if dims is not None:
                v.dims = dims
            elif dims_attr is not None:
                v.dims = dims_attr
            v.attrs |= attrs
            if codims is not None or any(a.startswith("codimension") for a in attrs):
                v.attrs.add("coarray")
            if intent:
                v.intent = intent
            if init is not None:
                v.init = init
            u.vars[name] = v
            if "external" in attrs:
                u.externals.add(name)
            if "intrinsic" in attrs:
                u.intrinsics.add(name)
            if "save" in attrs:
                u.saved.add(name)
            if u.kind == "module":
                if "private" in attrs:
                    u.private.add(name)
                elif "public" in attrs:
                    u.public.add(name)
        return True

    def _attr_stmt(self, u: Unit, code: str, st: Stmt) -> bool:
        m = re.match(r"(optional|allocatable|pointer|target|value|volatile|protected|contiguous"
                     r"|asynchronous)\b\s*(?:::)?\s*(.*)$", code)
        if m and not match_assignment(code):
            for name, dims, _, _, _ in parse_entities(m.group(2)):
                v = u.vars.setdefault(name, Var(name, line=st.line))
                v.attrs.add(m.group(1))
                if dims is not None:
                    v.dims = dims
            return True
        m = _rx(r"intent\s*\(\s*(in\s*out|inout|in|out)\s*\)\s*(?:::)?\s*(.*)$").match(code)
        if m:
            for name, _, _, _, _ in parse_entities(m.group(2)):
                u.vars.setdefault(name, Var(name, line=st.line)).intent = \
                    m.group(1).replace(" ", "")
            return True
        m = _rx(r"dimension\b\s*(?:::)?\s*(.*)$").match(code)
        if m and not match_assignment(code):
            for name, dims, _, _, _ in parse_entities(m.group(1)):
                u.vars.setdefault(name, Var(name, line=st.line)).dims = dims
            return True
        return False

    def spec(self, u: Unit, code: str, st: Stmt) -> bool:
        """Handle a specification statement; False if it is executable."""
        asg = match_assignment(code)
        if asg is not None:
            base = asg[0]
            v = u.vars.get(base)
            # statement function: f(a, b) = expr, before any executable statement. In a
            # module or internal procedure `a(i) = x` is more likely an element of a host or
            # use-associated array we cannot see, unless f is declared here as a scalar
            # (LAPACK's zlartg.f90: `real(wp) :: ABSSQ` then `ABSSQ( t ) = ...`)
            if (not u.has_exec and u.kind in ("program", "subroutine", "function")
                    and asg[3]
                    and _rx(r"[a-z]\w*\s*\(\s*(?:[a-z]\w*\s*(?:,\s*[a-z]\w*\s*)*)?\)"
                            ).fullmatch(asg[1])
                    and not (v is not None and v.is_array) and base not in u.args
                    and ((v is not None and v.type is not None and not v.is_char)
                         or (u.parent is not None and u.parent.kind == "file" and not u.uses))):
                args = _rx(r"[a-z]\w*").findall(asg[1].split("(", 1)[1])
                u.stmt_funcs[base] = (args, st.line)
                u.legacy["statement-function"] += 1
                for r in scan(asg[2], st.line, False, asg[4]):
                    if r.name not in args:
                        u.refs.append(r)
                return True
            return False
        m = _rx(r"use\b\s*(?:,\s*(intrinsic|non_intrinsic)\s*)?(?:::)?\s*([a-z]\w*)\s*"
                r"(?:,\s*(.*))?$").match(code)
        if m:
            only = None
            renames: dict[str, str] = {}
            rest = (m.group(3) or "").strip()
            om = _rx(r"only\s*:\s*(.*)$").match(rest)
            items = split_top(om.group(1)) if om else split_top(rest) if rest else []
            if om:
                only = []
            for it in items:
                it = it.strip()
                if not it or it.startswith(("operator", "assignment")):
                    continue
                rm = _rx(r"([a-z]\w*)\s*=>\s*([a-z]\w*)$").match(it)
                if rm:
                    renames[rm.group(1)] = rm.group(2)
                    if only is not None:
                        only.append(rm.group(1))
                elif only is not None and _rx(r"[a-z]\w*").fullmatch(it):
                    only.append(it)
            u.uses.append(Use(m.group(2), only, renames, st.line,
                              intrinsic=m.group(1) == "intrinsic", path=st.origin))
            return True
        if code.startswith("implicit"):
            u.implicit_stated = True
            if _rx(r"implicit\s*none\b").match(code):
                u.implicit_none = True
                return True
            body = code[len("implicit"):].strip()
            for item in split_top(body):
                p = item.rfind("(")
                if p < 0:
                    continue
                t = parse_typespec(item[:p].strip())
                letters = item[p + 1:item.rfind(")")]
                if t is None:
                    continue
                for rng in split_top(letters):
                    lm = _rx(r"([a-z])\s*(?:-\s*([a-z]))?").match(rng.strip())
                    if lm:
                        lo, hi = lm.group(1), lm.group(2) or lm.group(1)
                        for c in range(ord(lo), ord(hi) + 1):
                            u.implicit[chr(c)] = t[0]
            return True
        if code.startswith("common"):
            m = _rx(r"common\b\s*(.*)$").match(code)
            if m and not match_assignment(code):
                self.common(u, m.group(1), st)
                return True
        if code.startswith("equivalence"):
            groups = _rx(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)").findall(code[len("equivalence"):])
            for g in groups:
                names = [_rx(r"\s*([a-z]\w*)").match(p).group(1) for p in split_top(g)
                         if _rx(r"\s*([a-z]\w*)").match(p)]
                if names:
                    u.equivalences.append(names)
            u.legacy["equivalence"] += 1
            return True
        m = _rx(r"save\b\s*(?:::)?\s*(.*)$").match(code)
        if m and not match_assignment(code):
            rest = m.group(1).strip()
            if not rest:
                u.save_all = True
            else:
                for item in split_top(rest):
                    item = item.strip()
                    if item.startswith("/"):
                        u.saved.add(item)
                    elif item:
                        u.saved.add(item)
            u.legacy["save"] += 1
            return True
        m = _rx(r"external\b\s*(?:::)?\s*(.*)$").match(code)
        if m:
            for n in split_top(m.group(1)):
                if n.strip():
                    u.externals.add(n.strip())
            return True
        m = _rx(r"intrinsic\b\s*(?:::)?\s*(.*)$").match(code)
        if m:
            for n in split_top(m.group(1)):
                if n.strip():
                    u.intrinsics.add(n.strip())
            return True
        m = _rx(r"(public|private)\b\s*(?:::)?\s*(.*)$").match(code)
        if m:
            names = [n.strip() for n in split_top(m.group(2)) if n.strip()]
            if not names:
                u.access_default = m.group(1)
            else:
                target = u.public if m.group(1) == "public" else u.private
                for n in names:
                    target.add(n.replace(" ", ""))
            return True
        m = _rx(r"parameter\s*\((.*)\)$").match(code)
        if m:
            for item in split_top(m.group(1)):
                pm = _rx(r"([a-z]\w*)\s*=\s*(.*)$").match(item.strip())
                if pm:
                    v = u.vars.setdefault(pm.group(1), Var(pm.group(1), line=st.line))
                    v.attrs.add("parameter")
                    v.init = pm.group(2).strip()
            return True
        if _rx(r"data\b").match(code) and not match_assignment(code):
            for name in _rx(r"(?:^data|/\s*,?)\s*([a-z]\w*)").findall(code):
                u.data_names.add(name)
            for grp in _rx(r"(?:^data\s*|/\s*,?\s*)([^/]*)/").findall(code):
                for part in split_top(grp):
                    mm = _rx(r"\s*\(?\s*([a-z]\w*)").match(part)
                    if mm:
                        u.data_names.add(mm.group(1))
            u.legacy["data"] += 1
            return True
        m = _rx(r"entry\s+([a-z]\w*)\s*(?:\((.*)\))?").match(code)
        if m:
            args = [a.strip() for a in split_top(m.group(2) or "") if a.strip()]
            u.entries.append((m.group(1), args, st.line))
            u.legacy["entry"] += 1
            return True
        m = _rx(r"namelist\s*/\s*([a-z]\w*)\s*/(.*)$").match(code)
        if m:                     # a group name is not a variable; READ nml= sets members
            u.namelists.setdefault(m.group(1), []).extend(
                re.findall(r"[a-z]\w*", re.sub(r"/[^/]*/", ",", m.group(2))))
            return True
        if _rx(r"(namelist|import|sequence|format\s*\(|bind\s*\()").match(code):
            return True
        if self._attr_stmt(u, code, st):
            return True
        return self.declaration(u, code, st)

    def common(self, u: Unit, text: str, st: Stmt) -> None:
        block = ""
        u.legacy["common"] += 1
        for m in _rx(r"/\s*([a-z]\w*)?\s*/|([a-z][\w$]*)\s*(\((?:[^()]|\([^()]*\))*\))?"
                     ).finditer(text):
            if m.group(0).startswith("/"):
                block = m.group(1) or ""
                continue
            name = m.group(2)
            members = u.commons.setdefault(block, [])
            members.append(name)
            u.common_lines.setdefault(block, st.line)
            u.common_origin.setdefault(block, st.origin)
            if m.group(3):
                u.vars.setdefault(name, Var(name, line=st.line)).dims = \
                    m.group(3)[1:-1].replace(" ", "")
            else:
                u.vars.setdefault(name, Var(name, line=st.line))
            u.vars[name].attrs.add("common")

    # -- executable statements ----------------------------------------------
    def executable(self, u: Unit, code: str, st: Stmt, forced_cond: bool = False) -> None:
        u.has_exec = True
        own = st.origin == u.path
        cond = forced_cond or st.cond or u.depth > 0
        refs = u.refs
        line = st.line

        def add(rs):
            refs.extend(rs)

        closing_label = bool(not forced_cond and st.label and st.label in u.do_labels)
        try:
            asg = match_assignment(code)
            if asg is not None:
                base, lhs, rhs, sub, rhs_at = asg
                if "(" in lhs:
                    add(scan(lhs[lhs.index("("):], line, cond, lhs.index("(")))
                add(scan(rhs, line, cond, rhs_at))       # evaluated before the store
                refs.append(Ref("mutate" if sub else "write", base, line, 0, conditional=cond))
                if code[len(lhs):].lstrip().startswith("=>"):
                    tgt = _rx(r"\s*([a-z]\w*)\s*").fullmatch(rhs)
                    last = _rx(r"([a-z]\w*)\s*$").findall(lhs.split("(")[0] if "%" not in lhs
                                      else lhs.rsplit("%", 1)[1])
                    if tgt and last:                    # p => impl: a procedure pointer target
                        refs.append(Ref("ptrassign", last[0], line, 0, conditional=cond,
                                        obj=tgt.group(1)))
                return
            m = _rx(r"if\s*\(").match(code)
            if m:
                e = close_paren(code, m.end() - 1)
                add(scan(code[m.end():e - 1], line, cond, m.end()))
                rest = code[e:].strip()
                if rest == "then":
                    u.depth += 1
                elif _rx(r"\d+\s*,\s*\d+\s*(?:,\s*\d+)?").fullmatch(rest):
                    if own:
                        u.legacy["arithmetic-if"] += 1
                    u.goto_targets += [(line, x) for x in _rx(r"\d+").findall(rest)]
                elif rest:
                    self.executable(u, rest, st, forced_cond=True)
                return
            m = _rx(r"else\s*if\s*\(").match(code)
            if m:
                e = close_paren(code, m.end() - 1)
                add(scan(code[m.end():e - 1], line, cond, m.end()))
                return
            if _rx(r"end\s*(if|select|where|forall)\b").match(code) or code in ("endif",):
                u.depth = max(0, u.depth - 1)
                return
            if _rx(r"end\s*do\b").match(code):
                if not closing_label:
                    u.depth = max(0, u.depth - 1)
                    if u.loops:
                        u.loops.pop()
                return
            m = _rx(r"do\b\s*(\d+)?\s*,?\s*(.*)$").match(code)
            if m and (m.group(1) or not m.group(2) or re.match(
                    r"(while\b|concurrent\b|[a-z]\w*\s*=)", m.group(2))):
                label, rest = m.group(1), m.group(2)
                if label:
                    if own and label in u.do_labels:
                        u.legacy["shared-do-termination"] += 1
                    u.do_labels.append(label)
                    if own:
                        u.legacy["labelled-do"] += 1
                u.depth += 1
                self.seq += 1
                u.loops.append(self.seq)
                wm = _rx(r"(while|concurrent)\s*(.*)$").match(rest)
                if wm and wm.group(1) == "concurrent" and wm.group(2).startswith("("):
                    # the header; locality specs after it (local(t), shared(a)) declare
                    hdr = wm.group(2)
                    add(_header_entities(hdr[1:close_paren(hdr, 0) - 1], line, True))
                elif wm:
                    add(scan(wm.group(2), line, True))
                else:
                    vm = _rx(r"([a-z]\w*)\s*=(.*)$").match(rest)
                    if vm:
                        refs.append(Ref("write", vm.group(1), line, 0, conditional=cond))
                        add(scan(vm.group(2), line, cond, 5))
                return
            m = _rx(r"select\s*(case|type|rank)\s*\((.*)\)$").match(code)
            if m:
                u.depth += 1
                add(_header_entities(m.group(2), line, cond))      # select type (p => x)
                return
            if _rx(r"(case|type\s+is|class\s+is|class\s+default|rank)\b").match(code):
                return
            m = _rx(r"(where|forall)\s*\(").match(code)
            if m and m.group(1) == "forall" and own:
                u.legacy["forall"] += 1
            if m:
                e = close_paren(code, m.end() - 1)
                if m.group(1) == "forall":
                    add(_header_entities(code[m.end():e - 1], line, cond))
                else:
                    add(scan(code[m.end():e - 1], line, cond, m.end()))
                rest = code[e:].strip()
                if rest:
                    self.executable(u, rest, st, forced_cond=True)
                else:
                    u.depth += 1
                return
            m = _rx(r"go\s*to\s*\(([\d\s,]+)\)\s*,?\s*(.*)$").match(code)
            if m:
                if own:
                    u.legacy["computed-goto"] += 1
                u.goto_targets += [(line, x) for x in _rx(r"\d+").findall(m.group(1))]
                add(scan(m.group(2), line, cond))
                return
            m = _rx(r"go\s*to\s*([a-z]\w*)").match(code)
            if m:
                if own:
                    u.legacy["assigned-goto"] += 1
                u.goto_targets.append((line, "?"))       # could go anywhere
                return
            m = _rx(r"go\s*to\s*(\d+)$").match(code)
            if m:
                if own:
                    u.legacy["goto"] += 1
                u.goto_targets.append((line, m.group(1)))
                return
            if _rx(r"assign\s*\d+\s*to\s*[a-z]").match(code):
                if own:
                    u.legacy["assign"] += 1
                return
            m = _rx(r"call\s*([a-z].*)$").match(code)
            if m:
                self.call(u, m.group(1), st, cond, m.start(1))
                return
            m = _rx(r"return\b\s*(.*)$").match(code)
            if m:
                if m.group(1).strip():
                    if own:
                        u.legacy["alternate-return"] += 1
                    add(scan(m.group(1), line, cond))
                return
            if _rx(r"(error\s*)?stop\b").match(code):
                u.stops = True
                return
            if _rx(r"pause\b").match(code):
                if own:
                    u.legacy["pause"] += 1
                return
            m = _IO.match(code)
            if m:
                u.io = True
                word = m.group(1)
                rest = code[m.end():]
                items = rest
                if rest.startswith("("):
                    e = close_paren(rest, 0)
                    ctl = rest[1:e - 1]
                    if word == "write":
                        ctl = _internal_file(u, ctl, refs, line, cond)
                    add(scan(ctl, line, cond, m.end() + 1))
                    items = rest[e:]
                elif word in ("read", "print"):
                    parts = split_top(rest)
                    items = ",".join(parts[1:])
                if word == "read":
                    for item in split_top(items):
                        im = _rx(r"\s*\(?\s*([a-z]\w*)").match(item)
                        if im:
                            refs.append(Ref("write", im.group(1), line, 0, conditional=cond))
                add(scan(items, line, cond, m.end()))
                return
            m = re.match(r"(sync\s+(?:all|images|memory|team)|event\s+(?:post|wait)|form\s+team"
                         r"|change\s+team|lock|unlock|fail\s+image|notify\s+wait)\b\s*(.*)$", code)
            if m:                                       # image control (F2008/F2018)
                inner = m.group(2)
                if inner.startswith("("):
                    add(scan(inner[1:close_paren(inner, 0) - 1], line, cond))
                return
            m = _rx(r"(allocate|deallocate|nullify)\s*\((.*)\)$").match(code)
            if m:
                body = m.group(2)
                k = top_level_index(body, "::")
                if k >= 0:                  # allocate(character(len=n) :: s): a type-spec
                    spec, body = body[:k], body[k + 2:]
                    if "(" in spec:
                        add(scan(spec[spec.index("(") + 1:spec.rindex(")")], line, cond))
                for item in split_top(body):
                    im = _rx(r"\s*([a-z]\w*)").match(item)
                    if im and "=" not in item.split("(")[0]:
                        refs.append(Ref("write", im.group(1), line, 0, conditional=cond))
                add(scan(body, line, cond))
                return
            if re.match(r"(continue|exit|cycle|else|elsewhere|else\s+where|end|block$|"
                        r"critical|sync|contains)", code):
                return
            m = _rx(r"associate\s*\((.*)\)$").match(code)
            if m:
                add(_header_entities(m.group(1), line, cond))
                return
            add(scan(code, line, cond))
        finally:
            if closing_label:
                n = u.do_labels.count(st.label)
                u.do_labels = [x for x in u.do_labels if x != st.label]
                u.depth = max(0, u.depth - n)
                del u.loops[max(0, len(u.loops) - n):]

    def call(self, u: Unit, text: str, st: Stmt, cond: bool, col: int) -> None:
        m = _rx(r"([a-z][\w$]*)\s*").match(text)
        k = m.end()
        name = m.group(1)
        chain: list[str] = []
        args_inner = None
        while k < len(text):
            if text[k] == "(":
                e = close_paren(text, k)
                args_inner = text[k + 1:e - 1]
                k = e
                while k < len(text) and text[k] == " ":
                    k += 1
            elif text[k] == "%":
                mm = _rx(r"%\s*([a-z]\w*)\s*").match(text[k:])
                if not mm:
                    break
                chain.append(mm.group(1))
                args_inner = None
                k += mm.end()
            else:
                break
        pos, kws, alt = arglist(args_inner) if args_inner is not None else (0, [], 0)
        if alt and st.origin == u.path:
            u.legacy["alternate-return"] += 1
        if chain:
            u.refs.append(Ref("name", name, st.line, col, conditional=cond))
            u.refs.append(Ref("bound", chain[-1], st.line, col + 1, args=pos, keywords=kws,
                              conditional=cond, obj=name, chain=chain, has_paren=True,
                              argtext=args_inner or ""))
        else:
            u.refs.append(Ref("call", name, st.line, col, args=pos, keywords=kws,
                              conditional=cond, alt_returns=alt,
                              argtext=args_inner or ""))
        if args_inner is not None:
            u.refs.extend(scan(args_inner, st.line, cond, col + len(name) + 1))
            u.refs.extend(_passed(args_inner, "" if chain else name, st.line, col, cond,
                                  in_call=True))


def parse(src: Source, loader=None) -> Unit:
    return Parser(src, loader).run()
