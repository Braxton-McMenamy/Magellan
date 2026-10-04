"""Legacy constructs, line by line: what an upgrade has to remove, and what replaces it.

Each marker names the construct, how far gone it is, the standard or release that retired
it, and the modern form. The editor shows them as caution marks in the gutter; ``magellan
legacy`` lists them with the files that most code depends on first, which is where an
upgrade pays off (and where it is riskiest).

Levels, most urgent first:

- ``removed``: no longer accepted by current compilers or interpreters (``gets``, Python 2
  syntax, Fortran's PAUSE, ``std::auto_ptr``);
- ``unsafe``: still accepted, but a known source of bugs (``strcpy``, ``sprintf``);
- ``deprecated``: still accepted, scheduled for removal;
- ``obsolescent``: still standard, but discouraged and the target of modernization
  (COMMON, GO TO, PERFORM THRU).

The scan is lexical (comments and string literals are masked out first), so it works on
code that does not build, and runs per file in milliseconds.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

LEVELS = ("removed", "unsafe", "deprecated", "obsolescent")


@dataclass(frozen=True)
class Rule:
    id: str
    level: str
    pattern: re.Pattern
    message: str
    replacement: str
    standard: str = ""


@dataclass
class Marker:
    path: str
    line: int
    col: int
    rule: str
    level: str
    message: str
    replacement: str
    standard: str = ""
    language: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _r(id_, level, rx, message, replacement, standard="", flags=re.I | re.M):
    return Rule(id_, level, re.compile(rx, flags), message, replacement, standard)


_PY_REMOVED_MODULES = ("imp|distutils|asyncore|asynchat|smtpd|cgi|cgitb|crypt|imghdr|pipes|"
                       "telnetlib|nntplib|audioop|uu|xdrlib|msilib|nis|ossaudiodev|spwd|sndhdr|"
                       "sunau|chunk|mailcap|lib2to3")

RULES: dict[str, list[Rule]] = {
    "python": [
        _r("py-removed-module", "removed",
           rf"^[ \t]*(?:import|from)\s+({_PY_REMOVED_MODULES})\b",
           "imports a module removed from the standard library",
           "imp -> importlib; distutils -> setuptools/sysconfig; asyncore/asynchat -> asyncio; "
           "cgi -> email/urllib.parse; pipes -> shlex; the rest have PyPI replacements",
           "PEP 594 (3.12/3.13)", re.M),
        _r("py-collections-abc", "removed", r"\bcollections\.(Mapping|MutableMapping|Sequence|"
           r"MutableSequence|Set|MutableSet|Iterable|Iterator|Callable|Hashable|Sized|Container)\b",
           "ABC used from collections, not collections.abc", "collections.abc.\\1", "Python 3.10",
           re.M),
        _r("py-utcnow", "deprecated", r"\bdatetime\.utcnow\s*\(|\butcnow\s*\(\)",
           "naive UTC timestamp", "datetime.now(timezone.utc)", "Python 3.12", re.M),
    ],
    "c": [
        _r("c-gets", "removed", r"\bgets\s*\(", "gets() cannot be used safely",
           "fgets(buf, sizeof buf, stdin)", "C11", re.M),
        _r("c-unbounded-copy", "unsafe", r"\b(strcpy|strcat|sprintf|vsprintf)\s*\(",
           "unbounded copy into a fixed buffer", "snprintf / strncat with the buffer size, "
           "or a length-checked helper", "CERT STR31-C", re.M),
        _r("c-register", "obsolescent", r"\bregister\s+(?=\w)", "register storage class",
           "drop it: compilers allocate registers", "C++17 removed it; meaningless in C", re.M),
        _r("c-kr-definition", "removed",
           r"^[A-Za-z_][\w \t\*]*?\b([A-Za-z_]\w*)\s*\(\s*[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*\s*\)"
           r"[ \t]*\n(?:[ \t]*[A-Za-z_][\w \t\*\[\]]*;[ \t]*(?:/\*.*?\*/)?[ \t]*\n)+[ \t]*\{",
           "K&R (old-style) function definition", "a prototype-style definition: "
           "type name(type a, type b)", "C23 removed it", re.M),
    ],
    "cpp": [
        _r("cpp-prestandard-header", "removed",
           r"#\s*include\s*<(iostream|fstream|iomanip|strstream|stream)\.h>",
           "pre-standard C++ header", "<\\1> and the std:: namespace", "C++98", re.M),
        _r("cpp-auto-ptr", "removed", r"\bstd::auto_ptr\b|\bauto_ptr\s*<", "std::auto_ptr",
           "std::unique_ptr", "C++17", re.M),
        _r("cpp-dynamic-exception-spec", "removed", r"\)\s*(?:const\s*)?throw\s*\(\s*[\w:, ]+\s*\)",
           "dynamic exception specification", "noexcept, or nothing", "C++17", re.M),
        _r("cpp-binders", "removed", r"\bstd::(bind1st|bind2nd|ptr_fun|mem_fun|mem_fun_ref)\b",
           "removed functional adaptor", "a lambda or std::bind", "C++17", re.M),
        _r("cpp-register", "removed", r"\bregister\s+(?=\w)", "register storage class",
           "drop it", "C++17", re.M),
        _r("c-gets", "removed", r"\bgets\s*\(", "gets() cannot be used safely",
           "std::getline or fgets", "C++14", re.M),
        _r("c-unbounded-copy", "unsafe", r"\b(strcpy|strcat|sprintf|vsprintf)\s*\(",
           "unbounded copy into a fixed buffer", "std::string, or snprintf with the buffer size",
           "CERT STR31-C", re.M),
    ],
    "java": [
        _r("java-boxing-constructor", "deprecated",
           r"\bnew\s+(Integer|Long|Short|Byte|Double|Float|Boolean|Character)\s*\(",
           "boxing constructor", "\\1.valueOf(...)", "Java 9 (for removal)", re.M),
        _r("java-legacy-collection", "obsolescent", r"\bnew\s+(Vector|Hashtable|Stack)\s*[<(]",
           "synchronized legacy collection", "ArrayList / HashMap / ArrayDeque "
           "(Collections.synchronized* or java.util.concurrent if shared)", "Java 1.2", re.M),
        _r("java-thread-stop", "removed", r"\.\s*(stop|suspend|resume)\s*\(\s*\)\s*;",
           "Thread.stop/suspend/resume (if called on a Thread)",
           "cooperative interruption: interrupt() and a checked flag", "Java 20", re.M),
        _r("java-finalize", "deprecated", r"\bprotected\s+void\s+finalize\s*\(\s*\)",
           "finalizer", "try-with-resources / java.lang.ref.Cleaner", "Java 9 (for removal)", re.M),
    ],
    "typescript": [
        _r("js-new-buffer", "deprecated", r"\bnew\s+Buffer\s*\(", "new Buffer()",
           "Buffer.from / Buffer.alloc", "Node.js 6", re.M),
        _r("js-arguments-callee", "removed", r"\barguments\.callee\b", "arguments.callee",
           "a named function expression", "ES5 strict mode", re.M),
        _r("js-escape", "deprecated", r"(?<![\w.])(escape|unescape)\s*\(", "escape()/unescape()",
           "encodeURIComponent / decodeURIComponent", "ECMAScript Annex B", re.M),
    ],
    "fortran": [
        _r("f-arithmetic-if", "obsolescent", r"^[ \t]*(?:\d+[ \t]+)?if\s*\(.*\)\s*\d+\s*,\s*\d+\s*,\s*\d+\s*$",
           "arithmetic IF", "IF / ELSE IF / ELSE, or SELECT CASE", "obsolescent F90, deleted F2018"),
        _r("f-computed-goto", "obsolescent", r"^[ \t]*(?:\d+[ \t]+)?go\s*to\s*\(\s*\d+(?:\s*,\s*\d+)*\s*\)",
           "computed GO TO", "SELECT CASE", "obsolescent F95"),
        _r("f-assign", "removed", r"^[ \t]*(?:\d+[ \t]+)?assign\s+\d+\s+to\s+\w+", "ASSIGN statement",
           "an integer state variable and SELECT CASE", "deleted F95"),
        _r("f-pause", "removed", r"^[ \t]*(?:\d+[ \t]+)?pause\b", "PAUSE statement",
           "READ from input, or remove", "deleted F95"),
        _r("f-common", "obsolescent", r"^[ \t]*(?:\d+[ \t]+)?common\s*/", "COMMON block",
           "module variables", "obsolescent F2018"),
        _r("f-equivalence", "obsolescent", r"^[ \t]*(?:\d+[ \t]+)?equivalence\s*\(", "EQUIVALENCE",
           "separate variables, TRANSFER, or pointers", "obsolescent F2018"),
        _r("f-entry", "obsolescent", r"^[ \t]*(?:\d+[ \t]+)?entry\s+\w+", "ENTRY statement",
           "separate module procedures", "obsolescent F2008"),
        _r("f-block-data", "obsolescent", r"^[ \t]*block\s*data\b", "BLOCK DATA unit",
           "initialization in a module", "obsolescent F2008"),
        _r("f-forall", "obsolescent", r"^[ \t]*(?:\w+[ \t]*:[ \t]*)?forall\s*\(", "FORALL",
           "DO CONCURRENT", "obsolescent F2018"),
    ],
    "cobol": [
        _r("cobol-alter", "removed", r"\bALTER\s+[\w-]+\s+TO\b", "ALTER", "a flag and IF/EVALUATE",
           "deleted COBOL 2002"),
        _r("cobol-go-to-depending", "obsolescent", r"\bGO\s+TO\b[\w\s-]*\bDEPENDING\s+ON\b",
           "GO TO ... DEPENDING ON", "EVALUATE", ""),
        _r("cobol-next-sentence", "obsolescent", r"\bNEXT\s+SENTENCE\b", "NEXT SENTENCE",
           "CONTINUE (with scope terminators)", "COBOL 85"),
        _r("cobol-perform-thru", "obsolescent", r"\bPERFORM\s+[\w-]+\s+(?:THRU|THROUGH)\s+[\w-]+",
           "PERFORM THRU", "PERFORM of a single paragraph or section", ""),
        _r("cobol-removed-verb", "removed", r"\b(EXAMINE|TRANSFORM|EXHIBIT|READY\s+TRACE|"
           r"RESET\s+TRACE)\b", "pre-COBOL-74 verb", "INSPECT / DISPLAY", "removed COBOL 74/85"),
        _r("cobol-go-to", "obsolescent", r"\bGO\s+TO\s+[\w-]+\s*\.", "GO TO", "structured PERFORM",
           ""),
    ],
}

#: suffix -> language
_SUFFIX = [
    ((".py",), "python"),
    ((".c", ".h"), "c"),
    ((".cc", ".cpp", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h++", ".ipp", ".tpp", ".inl"), "cpp"),
    ((".java",), "java"),
    ((".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"), "typescript"),
    ((".f", ".for", ".f77", ".ftn", ".fpp", ".f90", ".f95", ".f03", ".f08", ".f18", ".f23"), "fortran"),
    ((".cbl", ".cob", ".cpy", ".cobol"), "cobol"),
]
_FIXED_FORTRAN = (".f", ".for", ".f77", ".ftn", ".fpp")


def language_of(path: str) -> str | None:
    low = path.lower()
    for suffixes, lang in _SUFFIX:
        if low.endswith(suffixes):
            return lang
    return None


def _blank(m: re.Match, keep_quotes: bool = False) -> str:
    s = m.group()
    inner = re.sub(r"[^\n]", " ", s)
    return (s[0] + inner[1:-1] + s[-1]) if keep_quotes and len(s) >= 2 else inner


def mask(text: str, lang: str, path: str = "") -> str:
    """Comments and string literals blanked to spaces; every offset and line unchanged."""
    if lang == "python":
        return re.sub(r"(?s)'''.*?'''|\"\"\".*?\"\"\"|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|#[^\n]*",
                      lambda m: _blank(m, m.group()[0] in "'\""), text)
    if lang in ("c", "cpp", "java", "typescript"):
        rx = r"(?s)/\*.*?\*/|//[^\n]*|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'"
        if lang == "typescript":
            rx += r"|`(?:\\.|[^`\\])*`"
        # #include <x.h> is not a string; "x.h" includes keep their quotes and contents
        return re.sub(rx, lambda m: m.group() if m.group().startswith('"') and
                      re.search(r'#\s*include\s*$', text[max(0, m.start() - 40):m.start()])
                      else _blank(m, m.group()[0] in "'\"`"), text)
    lines = text.split("\n")
    out = []
    for ln in lines:
        if lang == "fortran":
            if path.lower().endswith(_FIXED_FORTRAN) and ln[:1] in ("c", "C", "*", "!"):
                out.append(" " * len(ln))
                continue
            code = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", lambda m: _blank(m, True), ln)
            bang = code.find("!")
            out.append(code if bang < 0 else code[:bang] + " " * (len(code) - bang))
        else:                                       # COBOL: sequence area, comment lines, *>
            if len(ln) > 6 and ln[6:7] in ("*", "/"):
                out.append(" " * len(ln))
                continue
            code = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", lambda m: _blank(m, True), ln)
            code = (" " * min(6, len(code))) + code[6:72] if len(code) > 6 else code
            cut = code.find("*>")
            out.append(code if cut < 0 else code[:cut])
    return "\n".join(out)


def scan_text(text: str, path: str) -> list[Marker]:
    """Every legacy construct in one file."""
    lang = language_of(path)
    if lang is None:
        return []
    masked = mask(text, lang, path)
    starts = [0] + [i + 1 for i, ch in enumerate(masked) if ch == "\n"]
    import bisect
    out: list[Marker] = []
    seen: set[tuple[int, str]] = set()
    for rule in RULES.get(lang, ()):
        for m in rule.pattern.finditer(masked):
            pos = m.start(1) if m.groups() and m.group(1) and rule.id == "c-kr-definition" else m.start()
            ln = bisect.bisect_right(starts, pos)
            col = pos - starts[ln - 1] + 1
            if (ln, rule.id) in seen:
                continue
            seen.add((ln, rule.id))
            repl = m.expand(rule.replacement) if "\\1" in rule.replacement else rule.replacement
            out.append(Marker(path, ln, col, rule.id, rule.level, rule.message, repl,
                              rule.standard, lang))
    if lang == "python":
        out += _python2(text, path)
    from magellan_lite.polyglot.core.suppress import ignored_on_line
    lines = text.split("\n")
    out = [m for m in out if not ignored_on_line(lines, m.line, m.rule)]
    return sorted(out, key=lambda k: (k.line, k.col, k.rule))


def _python2(text: str, path: str) -> list[Marker]:
    """Lines that only parse as Python 2: where the 2-to-3 rewrite had to change them."""
    import ast
    from magellan_lite.polyglot.python.py2 import to_python3
    try:
        ast.parse(text)
        return []
    except SyntaxError:
        pass
    try:
        ported = to_python3(text)
        ast.parse(ported)
    except SyntaxError:
        return []
    out = []
    for i, (a, b) in enumerate(zip(text.split("\n"), ported.split("\n")), 1):
        if a != b:
            what = ("print statement" if re.match(r"\s*print\b", a) else
                    "except X, e" if "except" in a else "exec statement" if "exec" in a else
                    "raise X, msg" if "raise" in a else "Python 2 syntax")
            out.append(Marker(path, i, len(a) - len(a.lstrip()) + 1, "py2-syntax", "removed",
                              f"Python 2 only: {what}", b.strip()[:120], "Python 3.0", "python"))
    return out


_SKIP = {".git", "node_modules", ".magellan", "build", "dist", "__pycache__", ".venv", "venv",
         ".tox", "site-packages"}


def scan_tree(root: str | Path) -> list[Marker]:
    root = str(root)
    out: list[Marker] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP and not d.startswith(".")]
        for fn in filenames:
            if language_of(fn) is None:
                continue
            full = os.path.join(dirpath, fn)
            try:
                text = Path(full).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            out += scan_text(text, os.path.relpath(full, root).replace(os.sep, "/"))
    return out


@dataclass
class FileSummary:
    path: str
    markers: int
    by_level: dict = field(default_factory=dict)
    dependents: int = 0

    @property
    def priority(self) -> float:
        """Most urgent and most depended-on first: that is where an upgrade starts paying off."""
        weight = {"removed": 4, "unsafe": 3, "deprecated": 2, "obsolescent": 1}
        urgency = sum(weight[k] * v for k, v in self.by_level.items())
        return urgency * (1 + self.dependents) ** 0.5


def summarize(markers: list[Marker], graph=None) -> list[FileSummary]:
    by: dict[str, FileSummary] = {}
    for m in markers:
        s = by.setdefault(m.path, FileSummary(m.path, 0))
        s.markers += 1
        s.by_level[m.level] = s.by_level.get(m.level, 0) + 1
    if graph is not None:
        from magellan_lite.polyglot.core.model import EdgeKind
        refs = (EdgeKind.CALLS, EdgeKind.INSTANTIATES, EdgeKind.READS, EdgeKind.INHERITS,
                EdgeKind.IMPORTS)
        users: dict[str, set[str]] = {}
        for e in graph.edges:
            if e.kind not in refs:
                continue
            src, dst = graph.nodes.get(e.src), graph.nodes.get(e.dst)
            if src is None or dst is None or not dst.path or src.path == dst.path:
                continue
            users.setdefault(dst.path, set()).add(src.path)
        for s in by.values():
            s.dependents = len(users.get(s.path, ()))
    return sorted(by.values(), key=lambda s: (-s.priority, s.path))
