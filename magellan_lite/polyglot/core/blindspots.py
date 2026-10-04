"""Where a static map cannot see: calls whose target is decided at run time.

``CALL WS-PROG`` in COBOL, ``importlib.import_module(name)`` in Python, ``Class.forName(s)``
in Java, ``dlsym`` in C: the code calls *something*, and which thing depends on data. The
map has no edge there, so a caller found only this way is invisible to the blast radius,
to "nothing calls this any more", and to a port's "every caller has moved".

This module does not guess the targets. It finds the sites, lexically and per line (comments
and strings masked first, as :mod:`magellan_lite.polyglot.core.legacy` does), so a report can say how much
of the map rests on what it could not see, and where to look or what to trace
(``magellan trace`` records the calls a Python run really makes).
"""

from __future__ import annotations

import bisect
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from magellan_lite.polyglot.core.legacy import _SKIP, language_of, mask


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    rule: str
    what: str
    hint: str
    lang: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _Rule:
    id: str
    pattern: re.Pattern
    what: str
    hint: str


def _r(id_: str, rx: str, what: str, hint: str, flags: int = re.M) -> _Rule:
    return _Rule(id_, re.compile(rx, flags), what, hint)


_TRACE = "run it under `magellan trace` to record the calls it really makes"
#: a name or expression, or a literal that is only part of the argument ("plugins." + name,
#: "m_%s" % n, f"x{y}", `./${lang}`): masking keeps quotes, so the shape is still visible
_NOT_LITERAL = (r"\(\s*(?:(?![\s\"'`)])|[rbuf]*[\"'][^\"'\n]*[\"']\s*(?:\+|%|\.format\b)"
                r"|f[\"']|`)")            # a template literal: masked, but it is there to interpolate

#: ``(*fp)(x)`` used as an expression: after an operator, an opening bracket, a statement
#: start or ``return`` -- not after a type, as in ``typedef void (*cb)(int);``
_FNPTR_CALL = (r"(?:^|[=;{}(),!&|?:+\-]|\breturn)\s*\(\s*\*\s*\w+(?:\[[^\]]*\])?\s*\)\s*\(")

RULES: dict[str, tuple[_Rule, ...]] = {
    "python": (
        _r("py-import-by-name", r"\b(?:importlib\.)?import_module" + _NOT_LITERAL + r"|\b__import__" + _NOT_LITERAL,
           "module imported by a name computed at run time", _TRACE),
        _r("py-load-source", r"\bimp\.load_(?:source|module|compiled)\s*\(|\bspec_from_file_location\s*\(",
           "module loaded from a file path", _TRACE),
        # calling what getattr found: reading a data field by name is not a hidden call
        _r("py-getattr-call", r"\bgetattr\s*\([^,()]+,\s*(?![\s\"'])[^()]*?(?:\([^()]*\))?[^()]*\)\s*\(",
           "method called by a name computed at run time", _TRACE),
        _r("py-eval", r"(?<![.\w])(?<!def )(?:eval|exec)\s*\(", "code run from a string", _TRACE),
        _r("py-entry-points", r"\bentry_points\s*\(|\biter_entry_points\s*\(",
           "plug-ins found through package metadata",
           "list the installed plug-ins; their callers are in other packages"),
    ),
    "typescript": (
        _r("js-require-by-name", r"\brequire" + _NOT_LITERAL,
           "module required by a name computed at run time", "log the names it resolves to"),
        _r("js-import-by-name", r"(?<![.\w])import" + _NOT_LITERAL,
           "module imported by a name computed at run time", "log the names it resolves to"),
        _r("js-eval", r"(?<![.\w])(?:eval|Function)\s*\(", "code run from a string",
           "replace with a table of functions"),
        _r("js-index-call", r"\b\w+\[(?![\"'`\d])[^\]]+\]\s*\(",
           "method chosen by a computed key", "list the keys it can take"),
    ),
    "java": (
        _r("java-for-name", r"\bClass\.forName\s*\(|\.loadClass\s*\(",
           "class loaded by name", "list the class names it can receive"),
        _r("java-reflect-invoke", r"\.getMethod\s*\(|\.getDeclaredMethod\s*\(|\.invoke\s*\(",
           "method called through reflection", "list the method names it can receive"),
        _r("java-service-loader", r"\bServiceLoader\.load\s*\(",
           "implementations found by ServiceLoader", "check META-INF/services for the providers"),
    ),
    "c": (
        _r("c-dlsym", r"\b(?:dlsym|GetProcAddress)\s*\(", "function looked up in a shared library",
           "list the symbols it looks up"),
        _r("c-fn-pointer-call", _FNPTR_CALL + r"|\b\w+\s*->\s*\w+\s*\(",
           "call through a function pointer", "list the functions stored in it"),
    ),
    "cpp": (
        _r("c-dlsym", r"\b(?:dlsym|GetProcAddress)\s*\(", "function looked up in a shared library",
           "list the symbols it looks up"),
        _r("c-fn-pointer-call", _FNPTR_CALL, "call through a function pointer",
           "list the functions stored in it"),
    ),
    "fortran": (
        _r("f-procedure-pointer", r"\bprocedure\s*\([^)]*\)\s*,\s*pointer\b",
           "procedure pointer", "list the procedures assigned to it", re.I | re.M),
        _r("f-external-dummy", r"^\s*external\s+\w+", "procedure passed in as an argument",
           "the caller decides which routine runs", re.I | re.M),
    ),
    "cobol": (
        # a literal keeps its quotes after masking: CALL 'X' is static, CALL WS-PROG is not
        _r("cobol-call-by-name", r"(?<![\w-])CALL\s+(?![\"'])[A-Z][\w-]*", "program called by a data name",
           "list the values that data item can hold (often a table or a parameter file)",
           re.I | re.M),
        _r("cobol-cics-link", r"\bEXEC\s+CICS\s+(?:LINK|XCTL)\b", "program started through CICS",
           "the PROGRAM() operand names it; check the CICS tables for variable names", re.I | re.M),
    ),
}


def scan_text(text: str, path: str) -> list[Site]:
    lang = language_of(path)
    if lang is None:
        return []
    masked = mask(text, lang, path)
    starts = [0] + [i + 1 for i, ch in enumerate(masked) if ch == "\n"]
    out: list[Site] = []
    seen: set[tuple[int, str]] = set()
    for rule in RULES.get(lang, ()):
        for m in rule.pattern.finditer(masked):
            line = bisect.bisect_right(starts, m.start())
            if (line, rule.id) in seen:
                continue
            seen.add((line, rule.id))
            out.append(Site(path, line, rule.id, rule.what, rule.hint, lang))
    from magellan_lite.polyglot.core.suppress import ignored_on_line
    lines = text.split("\n")
    return sorted((s for s in out if not ignored_on_line(lines, s.line, s.rule)),
                  key=lambda s: (s.line, s.rule))


def testish(path: str) -> bool:
    """Tests and fixtures: listed after production code, where a hidden caller matters most."""
    p = "/" + path.lower()
    return any(k in p for k in ("/tests/", "/test/", "/fixtures/", "/testdata/")) or \
        p.rsplit("/", 1)[-1].startswith(("test_", "test."))


def scan_tree(root: str | Path, paths: list[str] | None = None, keep=None) -> list[Site]:
    """Every site under ``root`` (or in just ``paths``, relative to it; or in the files
    ``keep(rel_path)`` accepts, decided before anything is read)."""
    root = str(root)
    out: list[Site] = []
    if paths is not None:
        rels = paths
    else:
        rels = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP and not d.startswith(".")]
            rels += [os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
                     for fn in filenames if language_of(fn) is not None]
    for rel in rels:
        if keep is not None and not keep(rel):
            continue
        try:
            text = (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out += scan_text(text, rel)
    return sorted(out, key=lambda x: (testish(x.path), x.path, x.line))
