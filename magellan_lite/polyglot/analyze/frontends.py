"""Discovery of non-Python language frontends.

Any ``magellan/<lang>/frontend.py`` is a frontend. It must define:

- ``SOURCE_SUFFIXES``: tuple of file suffixes it maps;
- ``available() -> bool``: whether its toolchain is usable here;
- ``merge(state, root) -> set[str]``: add its nodes/edges to ``state.graph``
  and return the ids it added (they get their own labelling pass).

Optional: ``CONFIG_FILES`` (basenames materialized for ``--rev`` snapshots),
``has_sources(root) -> bool`` (cheap pre-check before ``available()``).

A sibling ``magellan/<lang>/semantics.py`` may define ``arity_breaks(old, new)``;
:func:`magellan_lite.polyglot.core.diff.arity_breaks` delegates to it when the arity dict
carries ``"lang": "<lang>"``. It may also define ``findings(before, after,
changeset, root)`` for breaks that are not about call sites (an interface method
every implementer now lacks, a switch missing a new enum constant); see
:func:`language_findings`. ``RULES = {"rule": "blocking" | "change" |
"informational"}`` there says how the brief treats rules only that language
reports, and ``still_names(text, name)`` lets removed-name checks read its
source. See docs/languages.md for the full contract.

Adding a language needs no edit here: drop the package in and it is found.
"""
from __future__ import annotations

import importlib
import os
import pkgutil
from pathlib import Path
from types import ModuleType

from magellan_lite import polyglot as magellan   # the vendored package: its frontends are found here

_cache: list[tuple[str, ModuleType]] | None = None


def all_frontends() -> list[tuple[str, ModuleType]]:
    """``(language, module)`` for every frontend package, sorted by language."""
    global _cache
    if _cache is None:
        found = []
        for info in pkgutil.iter_modules(magellan.__path__):
            if not info.ispkg or info.name == "python":
                continue
            if not (Path(magellan.__path__[0]) / info.name / "frontend.py").exists():
                continue
            found.append((info.name, importlib.import_module(f"magellan_lite.polyglot.{info.name}.frontend")))
        _cache = sorted(found, key=lambda p: p[0])
    return _cache


def enabled(cfg) -> list[tuple[str, ModuleType]]:
    skip = set(getattr(cfg, "skip_languages", ()) or ())
    if not getattr(cfg, "typescript", True):
        skip.add("typescript")
    return [(lang, mod) for lang, mod in all_frontends() if lang not in skip]


def source_suffixes() -> tuple[str, ...]:
    out: list[str] = []
    for _, mod in all_frontends():
        out.extend(getattr(mod, "SOURCE_SUFFIXES", ()))
    return tuple(dict.fromkeys(out))


def config_files() -> tuple[str, ...]:
    out: list[str] = []
    for _, mod in all_frontends():
        out.extend(getattr(mod, "CONFIG_FILES", ()))
    return tuple(dict.fromkeys(out))


#: directories no frontend maps (vendored, generated, tool state)
SKIP_DIRS = frozenset({"node_modules", "dist", "build", "out", "coverage", "venv", ".venv",
                       "__pycache__", "site-packages"})


def source_stamps(root, cfg) -> dict[str, tuple[int, int]]:
    """``relpath -> (mtime_ns, size)`` of every file an enabled frontend maps.

    Cheap enough to take on every refresh; a change here means the frontends must run again.
    """
    suffixes = tuple(s for _, mod in enabled(cfg) for s in getattr(mod, "SOURCE_SUFFIXES", ()))
    out: dict[str, tuple[int, int]] = {}
    if not suffixes:
        return out
    root = str(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(suffixes):
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                out[os.path.relpath(full, root).replace(os.sep, "/")] = (st.st_mtime_ns, st.st_size)
    return out


_semantics: dict[str, ModuleType | None] = {}


def semantics(lang: str) -> ModuleType | None:
    """``magellan.<lang>.semantics`` if that language defines one."""
    if lang not in _semantics:
        try:
            _semantics[lang] = importlib.import_module(f"magellan_lite.polyglot.{lang}.semantics")
        except ImportError:
            _semantics[lang] = None
    return _semantics[lang]


def merge_all(state, root, cfg) -> set[str]:
    """Run every enabled frontend, then link across languages.

    Returns the union of node ids the frontends added.
    """
    from magellan_lite.polyglot.analyze.interop import link
    added: set[str] = set()
    for _, mod in enabled(cfg):
        added |= mod.merge(state, root) or set()
    link(state.graph)
    return added


def language_findings(before, after, changeset, root) -> list:
    """Findings from every language's ``semantics.findings``.

    A failure in one language adds a diagnostic and never stops the check.
    """
    out: list = []
    for lang, _ in all_frontends():
        mod = semantics(lang)
        fn = getattr(mod, "findings", None) if mod is not None else None
        if fn is None:
            continue
        try:
            out += fn(before, after, changeset, root) or []
        except Exception as exc:                # a heuristic pass must not take the check down
            after.diagnostics.append(f"{lang}: language findings skipped: {exc}")
    return out


def rule_classes() -> dict[str, str]:
    """``{rule: "blocking" | "change" | "informational"}`` from each ``semantics.RULES``."""
    out: dict[str, str] = {}
    for lang, _ in all_frontends():
        mod = semantics(lang)
        out.update(getattr(mod, "RULES", None) or {})
    return out
