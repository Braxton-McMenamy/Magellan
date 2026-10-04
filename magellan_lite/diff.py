"""What changed between two maps, and which lines of the new version that covers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from magellan_lite.defs import Definition

#: how much each kind of change can do to the code that depends on it, worst first
KINDS = ("removed", "renamed", "signature", "value", "body", "added")


@dataclass
class Change:
    kind: str                       # one of KINDS
    name: str
    path: str
    line: int
    before: Definition | None = None
    after: Definition | None = None

    @property
    def detail(self) -> str:
        if self.kind == "signature":
            return f"{self.before.signature}  ->  {self.after.signature}"
        if self.kind == "value":
            return f"{self.before.value}  ->  {self.after.value}"
        # one branch at a time: a removed change has no `after`, an added one no `before`
        if self.kind == "removed":
            return f"{self.before.kind} deleted"
        if self.kind == "added":
            return f"new {self.after.kind}"
        if self.kind == "renamed":
            return f"renamed from {self.before.name}"
        return f"{self.after.kind} body changed"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "path": self.path, "line": self.line,
                "detail": self.detail}


def diff(before: dict[str, Definition], after: dict[str, Definition]) -> list[Change]:
    """Every definition added, removed, renamed or edited. One change per definition: the worst."""
    out: list[Change] = []
    for name in sorted(set(before) | set(after)):
        b, a = before.get(name), after.get(name)
        if a is None:
            out.append(Change("removed", name, b.path, b.line, before=b))
        elif b is None:
            out.append(Change("added", name, a.path, a.line, after=a))
        elif a.signature != b.signature:
            out.append(Change("signature", name, a.path, a.line, before=b, after=a))
        elif a.kind == "constant" and a.value != b.value:
            out.append(Change("value", name, a.path, a.line, before=b, after=a))
        elif a.body != b.body:
            out.append(Change("body", name, a.path, a.line, before=b, after=a))
    out = _pair_renames(out)
    out.sort(key=lambda c: (KINDS.index(c.kind), c.path, c.line))
    return out


def _pair_renames(changes: list[Change]) -> list[Change]:
    """A function, method or class deleted under one name and added under another with the
    same signature and body is one rename (or a move to another module), not two changes.

    Pairs only when the match is unique. Constants are never paired: the same value under a
    new name can be a new meaning -- Knight Capital's reused flag looked exactly like that.
    """
    def key(d: Definition) -> tuple:
        return d.kind, d.signature, d.body

    removed: dict[tuple, list[Change]] = defaultdict(list)
    added: dict[tuple, list[Change]] = defaultdict(list)
    for c in changes:
        if c.kind == "removed" and c.before.kind != "constant":
            removed[key(c.before)].append(c)
        elif c.kind == "added" and c.after.kind != "constant":
            added[key(c.after)].append(c)
    paired: set[int] = set()
    renames: list[Change] = []
    for k, gone in removed.items():
        new = added.get(k, [])
        if len(gone) == 1 and len(new) == 1:
            old, now = gone[0], new[0]
            renames.append(Change("renamed", now.name, now.path, now.line,
                                  before=old.before, after=now.after))
            paired |= {id(old), id(now)}
    return [c for c in changes if id(c) not in paired] + renames


Spans = dict[str, list[tuple[int, int]]]


def changed_spans(changes: list[Change]) -> Spans:
    """``{path: [(first line, last line)]}`` of every definition the change added or edited.

    A class whose own body is unchanged but whose method changed contributes only the method:
    findings belong to the narrowest thing that changed.
    """
    out: Spans = {}
    for c in changes:
        if c.after is not None:
            out.setdefault(c.after.path, []).append((c.after.line, c.after.end_line))
    return {p: sorted(set(s)) for p, s in out.items()}


def in_spans(spans: Spans, path: str, line: int) -> bool:
    return any(start <= line <= end for start, end in spans.get(path, ()))
