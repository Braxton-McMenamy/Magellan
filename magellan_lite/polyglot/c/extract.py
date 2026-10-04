"""What a C backend extracts, independent of how it parsed.

Both backends (libclang through :mod:`magellan_lite.polyglot.c.clang`, and the lexical parser in
:mod:`magellan_lite.polyglot.c.lexical`) produce an :class:`Extraction`: declarations, references
and switch statements, keyed by a USR-like string. :mod:`magellan_lite.polyglot.c.frontend` turns
that into graph nodes and edges, so the two backends cannot drift in how C maps onto
the shared model.

Keys follow libclang's USR shapes so the lexical parser can mimic them:

    c:@F@parse              extern function          c:util.c@F@helper   static function
    c:@S@pt  c:@U@u  c:@E@e struct / union / enum    c:@S@pt@FI@x        field
    c:@E@color@RED          enumerator               c:@g_count          extern global
    c:util.c@g              static global            c:util.h@T@pt_t     typedef
    macro:util.h@MAX        macro (per defining file)
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field

HEADER_SUFFIXES = (".h",)
SOURCE_SUFFIXES = (".c",) + HEADER_SUFFIXES
SKIP_DIRS = {".git", ".hg", ".svn", ".magellan", "node_modules", "__pycache__", ".venv", "venv",
             "build", "_build", "cmake-build-debug", "cmake-build-release", "out", "dist",
             "third_party", "vendor", "external", "deps"}


@dataclass
class Decl:
    key: str
    kind: str                     # function|struct|union|enum|typedef|field|enumerator|global|macro
    name: str
    path: str                     # relative to the project root, "/"-separated
    line: int
    end_line: int
    start: int = 0                # byte offsets of the whole declaration in the file
    end: int = 0
    static: bool = False
    is_def: bool = False          # a function body, a struct body, a non-extern global
    params: list[tuple[str, str]] = field(default_factory=list)   # (name, type)
    ptypes: list[str] = field(default_factory=list)   # canonical parameter types
    ret: str = ""
    variadic: bool = False
    noproto: bool = False         # K&R `int f()`: any arguments accepted
    type: str = ""                # field/global type, typedef target
    value: str = ""               # enumerator value, macro body, global initializer
    parent: str = ""              # key of the struct/enum holding a field/enumerator
    function_like: bool = False   # macros
    offset: int = -1              # field bit offset
    size: int = -1                # struct/union size in bytes
    const: bool = False
    conditional: bool = False     # inside #if/#ifdef (not the include guard)
    anonymous: bool = False
    fnptr: str = ""               # canonical function type, for fields/globals that hold one
    tu: str = ""                  # translation unit this was seen in


@dataclass
class Ref:
    src: str                      # key of the enclosing declaration
    kind: str                     # calls|reads|writes|mutates|annotates|imports
    path: str
    line: int
    col: int = 0
    dst: str = ""                 # key of the target, or "" with ext set
    ext: str = ""                 # name of a declaration outside the project
    args: int | None = None
    dynamic: bool = False
    confidence: float = 1.0
    conditional: bool = False
    tu: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class Switch:
    fn: str
    path: str
    line: int
    enum: str                     # key of the enum its case labels come from
    covered: list[str]
    default: bool
    subject: str = ""


@dataclass
class Extraction:
    backend: str
    files: dict[str, str] = field(default_factory=dict)        # path -> text
    decls: list[Decl] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    switches: list[Switch] = field(default_factory=list)
    includes: list[tuple[str, str, int]] = field(default_factory=list)  # (from, to, line)
    diagnostics: list[str] = field(default_factory=list)
    address_taken: dict[str, str] = field(default_factory=dict)   # fn key -> canonical fn type
    #: (function key, path) -> names of its parameters and locals: they shadow file scope
    local_names: dict[tuple[str, str], set[str]] = field(default_factory=dict)


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------
#: C++ files: their presence makes ``.h`` ownership a question (see :func:`source_files`)
CXX_SUFFIXES = (".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h++", ".ipp", ".tpp")
#: the same test the C++ frontend uses for "this header is C++"
_CXX_HINT = re.compile(r"^\s*(class|namespace|template\s*<|using\s+namespace)\b|::|\bpublic:",
                       re.M)


def reads_as_cxx(text: str) -> bool:
    return bool(_CXX_HINT.search(re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)))


def source_files(root: str) -> list[str]:
    """The ``.c`` and ``.h`` files the C frontend maps.

    ``.h`` is shared with C++. The rule is complementary to the C++ frontend's: in a
    project with C++ sources, a header that reads as C++ (``class``, ``namespace``,
    ``template <``, ``::``, ``public:`` outside comments) is left to C++; with no ``.c``
    files C++ takes every header and this frontend does not run at all.
    """
    out: list[str] = []
    has_cxx = False
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for n in sorted(names):
            if n.endswith(SOURCE_SUFFIXES):
                out.append(os.path.relpath(os.path.join(base, n), root).replace(os.sep, "/"))
            elif n.endswith(CXX_SUFFIXES):
                has_cxx = True
    if has_cxx:
        out = [f for f in out if not (f.endswith(".h") and reads_as_cxx(read_text(root, f)))]
    return out


def read_text(root: str, rel: str) -> str:
    try:
        with open(os.path.join(root, rel), "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


_IDENTITY = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)[ \t]*\([ \t]*(\w+)[ \t]*\)"
                       r"[ \t]+\(?[ \t]*\2[ \t]*\)?[ \t]*$", re.M)


def identity_macros(texts) -> frozenset[str]:
    """Function-like macros that expand to their argument: ``#define OF(args) args``.

    K&R-compatible headers wrap every prototype in one (zlib's ``OF((void *p, int n))``),
    and dropping it when the K&R support goes is a no-op the hashes must not see.
    """
    return frozenset(m.group(1) for t in texts for m in _IDENTITY.finditer(t))


def blank_identity_calls(text: str, names: frozenset[str]) -> str:
    """``OF((a, b))`` -> ``   (a, b) ``: the call as its expansion, every offset unchanged."""
    if not names or not any(n in text for n in names):
        return text
    out = list(text)
    rx = re.compile(r"\b(" + "|".join(sorted(map(re.escape, names))) + r")\s*\(")
    for m in rx.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if text[line_start:m.start()].lstrip().startswith("#"):
            continue                                  # the #define itself
        depth, i = 0, m.end() - 1
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth:
            continue
        for k in range(m.start(), m.end()):
            out[k] = " " if text[k] != "\n" else "\n"
        out[i] = " "
    return "".join(out)


def module_of(path: str) -> str:
    """``src/util.c`` and ``src/util.h`` are both module ``c@src.util``."""
    stem = path.rsplit(".", 1)[0] if "." in path.rsplit("/", 1)[-1] else path
    return "c@" + stem.replace("/", ".")


# --------------------------------------------------------------------------
# text normalization and hashing
# --------------------------------------------------------------------------
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_TOKEN = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|[A-Za-z_]\w*|\d[\w.]*|'
                    r'->|\+\+|--|<<=|>>=|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^]=|##|\.\.\.|\S')


def strip_comments(text: str) -> str:
    """Comments out, newlines kept (line numbers stay valid); strings are respected."""
    def repl(m: re.Match) -> str:
        s = m.group(0)
        if s[0] in "\"'":
            return s
        return "\n" * s.count("\n") if s.startswith("/*") else ""
    return re.sub(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|/\*.*?\*/|//[^\n]*',
                  repl, text, flags=re.S)


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(strip_comments(text).replace("\\\n", " "))


def h(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


def norm_hash(text: str) -> str:
    """Hash of the token stream: whitespace, comments and line breaks do not count."""
    return h(" ".join(tokens(text))) if text.strip() else ""


def leading_comment(text: str, start: int) -> str:
    """The comment block immediately above offset ``start`` (the declaration's docs)."""
    before = re.sub(r"#\s*define\s*$", "", text[:start]).rstrip()   # a macro's name follows it
    out: list[str] = []
    while before.endswith("*/") or re.search(r"//[^\n]*$", before):
        if before.endswith("*/"):
            i = before.rfind("/*")
            if i < 0:
                break
            out.append(before[i:])
            before = before[:i].rstrip()
        else:
            i = before.rfind("//")
            out.append(before[i:])
            before = before[:i].rstrip()
    return "\n".join(reversed(out))


def normalize_type(t: str) -> str:
    return " ".join(tokens(t)).replace(" *", "*").replace("* ", "*")


# --------------------------------------------------------------------------
# preprocessor conditionals
# --------------------------------------------------------------------------
_IF = re.compile(r"^\s*#\s*(if|ifdef|ifndef)\b")
_ENDIF = re.compile(r"^\s*#\s*endif\b")
#: an include guard: ``#ifndef X`` then ``#define X`` (never a function-like ``X(...)``)
GUARD = re.compile(r"^\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\b(?!\()", re.M)
_CPLUSPLUS = re.compile(r"^\s*#\s*(?:ifdef\s+__cplusplus|if\s+defined\s*\(?\s*__cplusplus\s*\)?)\s*$")


def conditional_lines(text: str) -> list[bool]:
    """``out[line-1]`` is True when that line sits inside an ``#if`` region.

    The include guard does not count, nor does ``#ifdef __cplusplus`` (it only
    wraps ``extern "C" {``, which says nothing about whether C code runs).
    """
    clean = strip_comments(text)
    lines = clean.split("\n")
    guard_line = -1
    m = GUARD.search(clean)
    if m and not clean[:m.start()].strip():
        guard_line = clean.count("\n", 0, m.start(1))
    out: list[bool] = []
    stack: list[bool] = []                 # True for a region that counts
    for i, ln in enumerate(lines):
        if _IF.match(ln):
            stack.append(i != guard_line and not _CPLUSPLUS.match(ln))
            out.append(any(stack[:-1]))
            continue
        if _ENDIF.match(ln):
            out.append(any(stack))
            if stack:
                stack.pop()
            continue
        out.append(any(stack))
    return out


def guard_macro(text: str) -> str:
    clean = strip_comments(text)
    m = GUARD.search(clean)
    return m.group(1) if m and not clean[:m.start()].strip() else ""


def macro_call_args(text: str, pos: int) -> int | None:
    """Arguments passed at a macro or call site starting at ``pos`` (the name)."""
    m = re.compile(r"[A-Za-z_]\w*\s*\(").match(text, pos)
    if not m:
        return None
    depth, i, n, seen = 1, m.end(), 0, False
    while i < len(text) and depth:
        ch = text[i]
        if ch in "\"'":
            q = ch
            i += 1
            while i < len(text) and text[i] != q:
                i += 2 if text[i] == "\\" else 1
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 1:
            n += 1
        elif not ch.isspace():
            seen = True
        i += 1
    return n + 1 if seen else 0
