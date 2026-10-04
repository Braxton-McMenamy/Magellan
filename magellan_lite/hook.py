"""``magellan-lite hook install``: run the check before every commit.

It writes git's pre-commit hook, which runs ``magellan-lite check``: a blocking verdict (or
the ``fail-on`` in pyproject.toml's ``[tool.magellan-lite]``) stops the commit, and
``git commit --no-verify`` skips the check once. The hook runs the check with the Python that
installed it, so it works from a virtual environment that is not active (an editor's commit
button); when that Python is gone it falls back to the ``magellan-lite`` on PATH.

A pre-commit hook that is someone else's is left alone unless ``--force`` is given; it is
then kept as ``pre-commit.bak``.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from magellan_lite.git import GitError, _run
from magellan_lite.source import decode

#: the second line of every hook this module writes: how it knows its own
MARKER = "# magellan-lite pre-commit hook"


def script(python: str, project: str = ".") -> str:
    """The hook: sh, which git runs on Windows too. ``project`` is the checked folder,
    relative to the repository's top (where git runs hooks)."""
    target = "" if project == "." else " " + shlex.quote(project)
    return (f"#!/bin/sh\n"
            f"{MARKER}, written by `magellan-lite hook install`.\n"
            f"# A blocking verdict stops the commit; `git commit --no-verify` skips the check once.\n"
            f"python={shlex.quote(python)}\n"
            f'if [ -x "$python" ]; then\n'
            f'    exec "$python" -m magellan_lite check{target}\n'
            f"fi\n"
            f"if ! command -v magellan-lite >/dev/null 2>&1; then\n"
            f'    echo "magellan-lite: not found. Install it again, then run'
            f' magellan-lite hook install." >&2\n'
            f"    exit 1\n"
            f"fi\n"
            f"exec magellan-lite check{target}\n")


def hook_path(root: Path) -> Path:
    """Where git looks for the pre-commit hook (``core.hooksPath`` and worktrees included)."""
    out = decode(_run(root, "rev-parse", "--git-path", "hooks/pre-commit")).strip()
    return (root / out).resolve()


def install(path: str | Path = ".", force: bool = False) -> int:
    """Write the hook and say so; 1 when an existing hook is not ours and ``force`` is off,
    2 when there is no repository."""
    root = Path(path).resolve()
    try:
        hook = hook_path(root)
        project = decode(_run(root, "rev-parse", "--show-prefix")).strip().rstrip("/") or "."
    except (GitError, OSError) as exc:
        print(f"magellan-lite: cannot install the hook: {exc}", file=sys.stderr)
        return 2
    shown = _shown(hook, root)

    kept = ""
    if hook.exists():
        mine = MARKER in hook.read_text(encoding="utf-8", errors="replace")
        if not mine and not force:
            print(f"magellan-lite: {shown} already exists and is not Magellan Lite's, so it was "
                  f"left as it is.\n"
                  f"  To replace it (the old one is kept as pre-commit.bak), run "
                  f"`magellan-lite hook install --force`.\n"
                  f"  Or add `magellan-lite check` to it yourself.", file=sys.stderr)
            return 1
        if not mine:
            hook.replace(hook.with_name("pre-commit.bak"))
            kept = " The hook that was there is kept as pre-commit.bak."

    hook.parent.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable).as_posix() if sys.executable else ""
    hook.write_text(script(python, project), encoding="utf-8", newline="\n")  # sh wants LF
    os.chmod(hook, hook.stat().st_mode | 0o111)
    print(f"magellan-lite: installed the pre-commit hook ({shown}). Every commit is checked "
          f"now, and a blocking verdict stops it; `git commit --no-verify` skips the check "
          f"once.{kept}")
    return 0


def _shown(hook: Path, root: Path) -> str:
    try:
        return hook.relative_to(root).as_posix()
    except ValueError:
        return hook.as_posix()
