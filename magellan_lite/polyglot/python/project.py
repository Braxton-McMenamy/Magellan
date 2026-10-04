"""Finding the source files and deciding what each module is called.

Module naming matters more than it looks: ``IMPORTS`` edges are only useful if
the dotted name written in an ``import`` statement resolves to the same string we
used to name the target module. We follow Python's own rule -- walk up while
``__init__.py`` exists -- and treat the directory where that chain stops as a
source root. That makes both flat layouts and ``src/`` layouts work without
configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".nox", ".venv", "venv", "env", "node_modules",
    "build", "dist", "site-packages", ".eggs", ".magellan", ".idea", ".vscode",
)


@dataclass
class SourceFile:
    path: Path
    relpath: str
    module: str
    source_root: Path
    is_package_init: bool = False

    @property
    def package(self) -> str:
        """Dotted name of the package this module lives in ('' for top level)."""
        if self.is_package_init:
            return self.module
        return self.module.rpartition(".")[0]


@dataclass
class Project:
    root: Path
    files: list[SourceFile] = field(default_factory=list)
    #: module name -> SourceFile, for resolving imports to internal modules
    by_module: dict[str, SourceFile] = field(default_factory=dict)
    source_roots: set[Path] = field(default_factory=set)
    skipped: list[str] = field(default_factory=list)

    def resolve_module_prefix(self, dotted: str) -> str | None:
        """Longest internal module that is a prefix of ``dotted``.

        ``app.services.billing.charge`` -> ``app.services.billing`` when that
        module exists, which is how ``from app.services.billing import charge``
        and ``app.services.billing.charge(...)`` both land on the right node.
        """
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if cand in self.by_module:
                return cand
        return None


def _is_excluded(name: str, excludes: set[str]) -> bool:
    return name in excludes or name.endswith(".egg-info")


def module_name_for(path: Path, excludes: set[str]) -> tuple[str, Path]:
    """Return ``(dotted_module_name, source_root)`` for a .py file."""
    parts: list[str] = []
    stem = path.stem
    is_init = stem == "__init__"
    if not is_init:
        parts.append(stem)
    d = path.parent
    while (d / "__init__.py").exists() and d.parent != d and not _is_excluded(d.name, excludes):
        parts.insert(0, d.name)
        d = d.parent
    if not parts:
        # a bare __init__.py whose directory is not itself a package
        parts = [path.parent.name]
    return ".".join(parts), d


def discover(
    root: str | Path,
    excludes: set[str] | None = None,
    follow_symlinks: bool = False,
) -> Project:
    root = Path(root).resolve()
    ex = set(DEFAULT_EXCLUDES) | (excludes or set())
    proj = Project(root=root)

    if root.is_file():
        candidates = [root]
        root = root.parent
        proj.root = root
    else:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
            dirnames[:] = [d for d in dirnames if not _is_excluded(d, ex) and not d.startswith(".")]
            for fn in filenames:
                if fn.endswith(".py"):
                    candidates.append(Path(dirpath) / fn)

    for path in sorted(candidates):
        module, src_root = module_name_for(path, ex)
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        sf = SourceFile(
            path=path,
            relpath=rel,
            module=module,
            source_root=src_root,
            is_package_init=path.stem == "__init__",
        )
        if module in proj.by_module:
            # Two files claim the same dotted name (common with duplicated test
            # helpers or vendored copies). Keep the shallower one; record both.
            other = proj.by_module[module]
            keep, drop = (other, sf) if len(other.relpath) <= len(sf.relpath) else (sf, other)
            proj.by_module[module] = keep
            proj.skipped.append(f"duplicate module '{module}': kept {keep.relpath}, shadowed {drop.relpath}")
            if keep is sf:
                proj.files = [f for f in proj.files if f is not other]
                proj.files.append(sf)
            continue
        proj.by_module[module] = sf
        proj.files.append(sf)
        proj.source_roots.add(src_root)

    return proj


def read_source(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")
