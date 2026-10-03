"""A throwaway git repository for tests: commit a "before", write an "after", check it.

    with Project() as p:
        p.commit({"app/pay.py": "def charge(amount): ..."})
        p.write({"app/pay.py": "def charge(amount, currency): ..."})
        report = p.check()
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from magellan_lite.engine import check
from magellan_lite.report import Report


def _rmtree(path: Path) -> None:
    def force(func, p, *_):                     # git objects are read-only on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=force)
    else:
        shutil.rmtree(path, onerror=force)


class Project:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="magellan-lite-test-"))
        self.git("init", "-q")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "core.autocrlf", "false")

    def __enter__(self) -> "Project":
        return self

    def __exit__(self, *exc) -> None:
        _rmtree(self.root)

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def write(self, files: dict[str, str | None], newline: str = "\n") -> None:
        """Write (or, for None, delete) files; text is dedented, so indent it freely."""
        for rel, text in files.items():
            p = self.root / rel
            if text is None:
                p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8", newline=newline)

    def commit(self, files: dict[str, str | None] | None = None, message: str = "c") -> None:
        if files:
            self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)

    def check(self, against: str = "git:HEAD") -> Report:
        return check(self.root, against)
