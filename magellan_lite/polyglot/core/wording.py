"""How a finding says what will go wrong, in the terms of the language it is about.

Rules are shared across languages but their consequences are not: a Python import of a
moved name fails with ImportError, a Fortran caller of a routine moved into a module fails
to link, and "keep the old parameters with defaults" is advice C and Java cannot take.
"""
from __future__ import annotations

from magellan_lite.polyglot.core.model import Graph

_MOVED = {
    "python": "An import of the old location now fails with ImportError.",
    "typescript": "An import or require() of the old module no longer finds it: a "
                  "module-not-found error, or undefined at the call.",
    "c": "Code that declares it through the old header no longer compiles, and objects "
         "built against the old symbol fail to link.",
    "cpp": "Code that names it through the old header or namespace no longer compiles.",
    "java": "Code that imports it from the old package or class no longer compiles.",
    "fortran": "Callers that do not USE the new module still call the old external "
               "routine, which no longer exists: the link fails (undefined reference).",
    "cobol": "Programs that CALL or COPY it under the old name no longer find it.",
}
_MOVED_FIX = {
    "python": "Update the imports to the new module, or re-export it from the old one.",
    "typescript": "Update the import paths, or re-export it from the old module.",
    "c": "Include the header that now declares it, or keep a declaration in the old one.",
    "cpp": "Include the new header (or qualify the new namespace), or keep a using-"
           "declaration at the old location.",
    "java": "Update the imports, or leave a deprecated delegate at the old location.",
    "fortran": "Add `use` of the new module to the remaining callers, or keep an external "
               "wrapper under the old name.",
    "cobol": "Repoint the CALLs and COPYs, or keep a program under the old name that calls "
             "the new one.",
}
_KEEP_OLD = {
    "python": "Update the call sites listed above, or keep the old parameters accepted with "
              "defaults so existing callers keep working.",
    "typescript": "Update the call sites listed above, or make the new parameters optional "
                  "so existing callers keep working.",
    "cpp": "Update the call sites listed above, or give the new parameters default "
           "arguments (or keep the old overload) so existing callers keep compiling.",
    "c": "Update the call sites listed above, or keep the old function under its name and "
         "add the new behaviour under a new one (C has no default arguments).",
    "java": "Update the call sites listed above, or keep an overload with the old "
            "parameters that delegates to the new one.",
    "fortran": "Update the call sites listed above; with an explicit interface, an "
               "`optional` argument keeps existing callers valid.",
    "cobol": "Update every CALL ... USING listed above, or keep the old LINKAGE layout.",
}


def lang_of(graph: Graph | None, node_id: str) -> str:
    """The language a node belongs to (Python when the frontend did not say)."""
    node = graph.nodes.get(node_id) if graph is not None else None
    lang = (node.meta.get("lang") if node is not None else None) or "python"
    return "typescript" if lang in ("javascript", "ts", "js") else lang


def moved_consequence(lang: str) -> str:
    return _MOVED.get(lang, _MOVED["python"])


def moved_fix(lang: str) -> str:
    return _MOVED_FIX.get(lang, _MOVED_FIX["python"])


def keep_old_parameters(lang: str) -> str:
    return _KEEP_OLD.get(lang, _KEEP_OLD["python"])


# ------------------------------------------------------------- plurals
import re as _re

_COUNTED = _re.compile(
    r"\b(\d+)((?:[ \t]+[A-Za-z][\w-]*){0,3}?[ \t]+)([A-Za-z][\w-]*?)\(s\)"
    r"(?:([ \t]+(?:that[ \t]+|which[ \t]+)?)(are|have|depend|break|reach|call|use|need|fit|fail|raise|run)\b)?")
_SINGULAR_VERB = {"are": "is", "have": "has", "reach": "reaches"}


def tidy(text: str) -> str:
    """``1 call site(s) are`` -> ``1 call site is``; ``2 hop(s)`` -> ``2 hops``.

    Rules write counts as ``N thing(s)``, which reads badly wherever it is shown; this picks
    the form the number calls for, and the verb right after it. Display only: fingerprints
    and suppressions never see message text.
    """
    if not text or "(s)" not in text:
        return text

    def one(m: _re.Match) -> str:
        n, gap, word, space, verb = int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5)
        noun = word if n == 1 else word + "s"
        if verb:
            verb = _SINGULAR_VERB.get(verb, verb + "s") if n == 1 else verb
            return f"{m.group(1)}{gap}{noun}{space}{verb}"
        return f"{m.group(1)}{gap}{noun}"

    return _COUNTED.sub(one, text)
