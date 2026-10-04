"""Two people's work, checked together: the part of the team check that needs no git.

``combine`` takes three versions of a project -- the shared base, your work and a teammate's --
and reports only the problems the combination has. ``magellan-lite team`` (team.py) feeds it
from git; the website's Team suite feeds it from GitHub, in the browser, so this module must
stay free of git, subprocess and the file system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from magellan_lite.engine import check_snapshots
from magellan_lite.report import Report
from magellan_lite.source import Snapshot

WIP = "refs/wip"


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
            shared: float = 0.0, alone: tuple[Report, Report] | None = None) -> Combination:
    """Check ``mine`` and ``theirs`` (both made from ``base``) together. A file only they
    changed comes from them; a file you both changed is checked with your version.
    ``alone``: each one's own check, when the caller has them already."""
    b, m, t = base.files, mine.files, theirs.files
    yours, their = _changed(b, m), _changed(b, t)
    both = sorted(p for p in yours & their if not _same(m.get(p), t.get(p)))
    together = dict(m)
    for p in their - yours:
        if p in t:
            together[p] = t[p]
        else:
            together.pop(p, None)                       # they deleted it

    def run(after: Snapshot) -> Report:
        return check_snapshots(base, after, root, "team")

    alone_mine, alone_theirs = alone or (run(mine), run(theirs))
    joint = run(derive(base, together))
    key = lambda f: (f.rule, f.path, f.line, f.message)   # noqa: E731
    known = {key(f) for f in alone_mine.findings + alone_theirs.findings}
    only = [f for f in joint.findings if key(f) not in known]
    report = Report(root=root.as_posix(), against=f"{WIP}/{name}", changes=joint.changes,
                    findings=only, affected=joint.affected if only else [], errors=joint.errors)
    return Combination(name, shared, alone_theirs.changes, report, both)


def derive(base: Snapshot, files: dict[str, str], label: str = "") -> Snapshot:
    """A version made from ``base``: the files it did not change keep base's syntax trees, so
    checking several people's work against one base parses each file once."""
    out = Snapshot(dict(files), label)
    for path, tree in base._trees.items():
        if files.get(path) is base.files.get(path) or _same(files.get(path), base.files.get(path)):
            out._trees[path] = tree
            if path in base.errors:
                out.errors[path] = base.errors[path]
    return out
