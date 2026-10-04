"""Team: two people's changes that are fine apart and break together.

    magellan-lite share        # publish your work in progress (no commit, no branch, no push of your branch)
    magellan-lite team         # check your work against everyone else's

Git merges two changes to different files without a word, and each branch's checks pass on
their own. When one person adds a required parameter and another adds a new call the old way,
the merge is a TypeError waiting in production. ``team`` combines your working tree with each
teammate's shared work in progress and reports only the problems the *combination* has.

Sharing: ``share`` snapshots your working tree -- tracked and new files, as .gitignore allows
-- into a commit object built with a throwaway index, and pushes it to ``refs/wip/<you>`` on
the remote. Your branch, index and stash are untouched. Who can see it is whoever can see the
repository: there are no accounts and no server beyond the remote you already use.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from magellan_lite.engine import check_snapshots
from magellan_lite.findings import Finding
from magellan_lite.git import GitError, snapshot_at
from magellan_lite.report import Report
from magellan_lite.source import Snapshot, decode, working_tree

WIP = "refs/wip"


def _git(root: Path, *args: str, env: dict | None = None) -> str:
    try:
        r = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=root,
                           capture_output=True, env={**os.environ, **(env or {})})
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None
    if r.returncode != 0:
        why = decode(r.stderr).strip().splitlines()
        raise GitError(why[-1] if why else f"git {args[0]} failed")
    return decode(r.stdout).strip()


def who(root: Path) -> str:
    """This person's name in ``refs/wip/``: git's user.name, as a safe ref component."""
    try:
        name = _git(root, "config", "user.name")
    except GitError:
        name = os.environ.get("USERNAME") or os.environ.get("USER") or "me"
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "me"


def share(root: str | Path = ".", name: str | None = None, remote: str = "origin") -> tuple[str, str]:
    """Push the working tree to ``refs/wip/<name>``. Returns ``(ref, commit)``."""
    root = Path(root).resolve()
    top = Path(_git(root, "rev-parse", "--show-toplevel"))
    name = name or who(root)
    head = _git(top, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}   # never the real index
        _git(top, "read-tree", "HEAD", env=env)
        _git(top, "add", "-A", env=env)
        tree = _git(top, "write-tree", env=env)
    commit = _git(top, "commit-tree", tree, "-p", head, "-m", f"magellan-lite: {name}'s work in progress")
    ref = f"{WIP}/{name}"
    _git(top, "push", "--force", "--quiet", remote, f"{commit}:{ref}")   # replaces your own last share
    return ref, commit


@dataclass
class Combination:
    """Your work and one teammate's, checked together."""
    name: str
    shared: float                   # when they shared it (unix time)
    their_changes: list = field(default_factory=list)
    report: Report | None = None    # the problems only the combination has
    overlap: list[str] = field(default_factory=list)   # files you both edited

    @property
    def verdict(self) -> str:
        return self.report.verdict if self.report else "ok"

    def to_dict(self) -> dict:
        return {"name": self.name, "shared": self.shared, "verdict": self.verdict,
                "their_changes": [c.to_dict() for c in self.their_changes],
                "findings": [f.to_dict() for f in (self.report.findings if self.report else [])],
                "affected": self.report.affected if self.report else [],
                "overlap": self.overlap}


def _same(x: str | None, y: str | None) -> bool:
    """Equal apart from line endings: a Windows checkout (CRLF) of an unchanged file is
    unchanged."""
    if x is None or y is None:
        return x is y
    return x.replace("\r\n", "\n") == y.replace("\r\n", "\n")


def _changed(a: dict, b: dict) -> set[str]:
    return {p for p in set(a) | set(b) if not _same(a.get(p), b.get(p))}


def combine(root: Path, base: Snapshot, mine: Snapshot, theirs: Snapshot, name: str,
            shared: float = 0.0) -> Combination:
    """Check ``mine`` and ``theirs`` (both made from ``base``) together. A file only they
    changed comes from them; a file you both changed is checked with your version."""
    b, m, t = base.files, mine.files, theirs.files
    yours, their = _changed(b, m), _changed(b, t)
    both = sorted(p for p in yours & their if not _same(m.get(p), t.get(p)))
    together = dict(m)
    for p in their - yours:
        if p in t:
            together[p] = t[p]
        else:
            together.pop(p, None)                       # they deleted it

    def run(after: dict) -> Report:
        return check_snapshots(Snapshot(dict(b), "base"), Snapshot(dict(after)), root, "team")

    alone_mine, alone_theirs, joint = run(m), run(t), run(together)
    key = lambda f: (f.rule, f.path, f.line, f.message)   # noqa: E731
    known = {key(f) for f in alone_mine.findings + alone_theirs.findings}
    only = [f for f in joint.findings if key(f) not in known]
    report = Report(root=root.as_posix(), against=f"{WIP}/{name}", changes=joint.changes,
                    findings=only, affected=joint.affected if only else [], errors=joint.errors)
    return Combination(name, shared, alone_theirs.changes, report, both)


def team(root: str | Path = ".", remote: str = "origin", me: str | None = None) -> list[Combination]:
    """Fetch everyone's shared work in progress and check yours against each."""
    root = Path(root).resolve()
    me = me or who(root)
    local = f"refs/remotes/{remote}/wip"
    _git(root, "fetch", "--quiet", "--prune", remote, f"+{WIP}/*:{local}/*")
    listing = _git(root, "for-each-ref", "--format=%(refname) %(objectname) %(committerdate:unix)", local)
    mine = working_tree(root)
    out = []
    for line in listing.splitlines():
        ref, sha, when = line.split()
        name = ref[len(local) + 1:]
        if name == me:
            continue
        base = _git(root, "merge-base", "HEAD", sha)
        out.append(combine(root, snapshot_at(root, base), mine, snapshot_at(root, sha), name,
                           float(when)))
    return out


def text(results: list[Combination], now: float | None = None) -> str:
    """The team report as it reads in a terminal."""
    if not results:
        return ("magellan-lite team: nobody else has shared work in progress yet "
                "(they run `magellan-lite share`)")
    now = now or time.time()

    def ago(t: float) -> str:
        s = max(0, int(now - t))
        return ("just now" if s < 60 else f"{s // 60} min ago" if s < 3600
                else f"{s // 3600} h ago" if s < 86400 else f"{s // 86400} d ago")

    lines = [f"magellan-lite team: your work against {len(results)} "
             f"{'teammate' + chr(39) + 's' if len(results) == 1 else 'teammates' + chr(39)} "
             f"work in progress"]
    for c in sorted(results, key=lambda c: (c.verdict != "block", c.verdict != "review", c.name)):
        files = sorted({ch.path for ch in c.their_changes})
        lines += ["", f"  {c.name}  (shared {ago(c.shared)}: {len(c.their_changes)} change"
                      f"{'' if len(c.their_changes) == 1 else 's'} in {len(files)} file"
                      f"{'' if len(files) == 1 else 's'})"]
        if c.their_changes:
            lines.append("    their changes: " + ", ".join(
                f"{'.'.join(ch.name.split('.')[-2:])} ({ch.kind})" for ch in c.their_changes[:6])
                + (" ..." if len(c.their_changes) > 6 else ""))
        findings: list[Finding] = c.report.findings if c.report else []
        if not findings:
            lines.append("    fine together")
        else:
            lines.append(f"    {c.verdict.upper()} · {len(findings)} problem"
                         f"{'' if len(findings) == 1 else 's'} that only the combination has")
            for f in findings:
                lines.append(f"    [ ] {f.severity.upper():<8} {f.rule}  {f.path}:{f.line}")
                lines.append(f"        {f.message}")
                if f.fix:
                    lines.append(f"        fix: {f.fix}")
        if c.overlap:
            lines.append(f"    you both edit {', '.join(c.overlap)}: checked with your version")
    return "\n".join(lines)
