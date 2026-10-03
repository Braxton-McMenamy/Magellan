"""The project as it was at a git revision, read without touching the working tree.

Built on what the full Magellan learned on Windows: git's output is decoded as UTF-8 (not the
system code page), file names are listed from the repository root (``--full-tree``) so a
project in a subdirectory still finds its files, and non-ASCII names are not quoted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from magellan_lite.source import Snapshot, decode, is_skipped_dir


class GitError(RuntimeError):
    """Git could not answer: not a repository, no commits yet, an unknown revision."""


def _run(root: Path, *args: str, data: bytes | None = None) -> bytes:
    try:
        r = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=root, input=data,
                           capture_output=True)
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None
    if r.returncode != 0:
        why = decode(r.stderr).strip().splitlines()
        raise GitError(why[-1] if why else f"git {args[0]} failed")
    return r.stdout


def is_repo(root: str | Path) -> bool:
    try:
        _run(Path(root), "rev-parse", "--show-toplevel")
        return True
    except GitError:
        return False


def snapshot_at(root: str | Path, rev: str = "HEAD") -> Snapshot:
    """The ``.py`` files under ``root`` as they were at ``rev``."""
    root = Path(root).resolve()
    top = Path(decode(_run(root, "rev-parse", "--show-toplevel")).strip()).resolve()
    prefix = root.relative_to(top).as_posix()
    prefix = "" if prefix == "." else prefix + "/"
    try:
        listing = decode(_run(top, "ls-tree", "-r", "--full-tree", "--name-only", rev))
    except GitError as exc:
        raise GitError(f"cannot read revision {rev!r}: {exc}") from None
    names = [n for n in listing.splitlines()
             if n.endswith(".py") and n.startswith(prefix)
             and not any(is_skipped_dir(part) for part in n[len(prefix):].split("/")[:-1])]
    return Snapshot(_read_blobs(top, rev, names, prefix), label=f"git {rev}")


def _read_blobs(top: Path, rev: str, names: list[str], prefix: str) -> dict[str, str]:
    """Every file's content at ``rev`` in one ``git cat-file --batch`` call."""
    if not names:
        return {}
    raw = _run(top, "cat-file", "--batch",
               data="".join(f"{rev}:{n}\n" for n in names).encode("utf-8"))
    out: dict[str, str] = {}
    pos = 0
    for name in names:
        end = raw.index(b"\n", pos)
        header = raw[pos:end].split()
        pos = end + 1
        if len(header) < 3 or header[1] != b"blob":
            continue                                    # "<object> missing"
        size = int(header[2])
        out[name[len(prefix):]] = decode(raw[pos:pos + size])
        pos += size + 1                                 # the blob, then its newline
    return out
