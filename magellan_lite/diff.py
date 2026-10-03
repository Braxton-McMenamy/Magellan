"""What changed between two maps, and which lines of the new version that covers."""

from __future__ import annotations

from dataclasses import dataclass

from magellan_lite.defs import Definition

#: how much each kind of change can do to the code that depends on it, worst first
KINDS = ("removed", "signature", "value", "body", "added")


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
        return {"removed": f"{self.before.kind} deleted", "added": f"new {self.after.kind}",
                "body": f"{self.after.kind} body changed"}[self.kind]

    def to_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "path": self.path, "line": self.line,
                "detail": self.detail}


def diff(before: dict[str, Definition], after: dict[str, Definition]) -> list[Change]:
    """Every definition added, removed or edited. One change per definition: the worst."""
    # TODO(engine): renames. A function renamed with its body unchanged shows up as one
    #   "removed" and one "added". Pair them (same kind, same body hash, one removed, one
    #   added) into a single Change("renamed", ...) so callers of the old name can be checked.
    #   Done when: tests/test_map.py renames charge -> bill and gets exactly one change.
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
    out.sort(key=lambda c: (KINDS.index(c.kind), c.path, c.line))
    return out


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
