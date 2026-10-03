"""Reading a project: which files, their text, their syntax trees.

Lessons from running the full Magellan on Windows are built in: files are read as UTF-8
whatever the system locale, paths use forward slashes on every OS, and line endings never
count as a change (definitions are hashed from syntax trees, not text).
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

#: directories that hold no project code (tools, environments, build output)
SKIP_DIRS = frozenset({"__pycache__", "venv", "env", "build", "dist", "node_modules",
                       "site-packages"})


def is_skipped_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def iter_python_files(root: Path) -> list[str]:
    """Every ``.py`` file under ``root``, as sorted forward-slash paths relative to it."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not is_skipped_dir(d))
        for name in sorted(filenames):
            if name.endswith(".py"):
                out.append(Path(dirpath, name).relative_to(root).as_posix())
    return out


def decode(data: bytes) -> str:
    """Source bytes as text: UTF-8 (a BOM is dropped), never the system's code page."""
    return data.decode("utf-8-sig", errors="replace")


def module_name(path: str) -> str:
    """``pkg/mod.py`` -> ``pkg.mod``; ``pkg/__init__.py`` -> ``pkg``; ``src/`` is dropped."""
    parts = path[:-3].split("/") if path.endswith(".py") else path.split("/")
    if parts and parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "__init__"


class Snapshot:
    """One version of a project: ``{relative path: source text}``, parsed on demand."""

    def __init__(self, files: dict[str, str], label: str = "") -> None:
        self.files = files
        self.label = label
        self.errors: dict[str, str] = {}
        self._trees: dict[str, ast.Module | None] = {}

    def tree(self, path: str) -> ast.Module | None:
        """The file's syntax tree, or None when it does not parse (the error is kept)."""
        if path not in self._trees:
            try:
                self._trees[path] = ast.parse(self.files[path], filename=path)
            except SyntaxError as exc:
                self._trees[path] = None
                self.errors[path] = f"{path}:{exc.lineno or 0}: {exc.msg}"
        return self._trees[path]


def working_tree(root: str | Path) -> Snapshot:
    """The project as it is on disk now."""
    root = Path(root)
    files = {rel: decode((root / rel).read_bytes()) for rel in iter_python_files(root)}
    return Snapshot(files, label=str(root))
