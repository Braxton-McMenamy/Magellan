"""Where a change landed, for rules that re-read source.

A rule that parses a file should report what the change touched, not every old problem in
the file: a function that has looped forever since 2003 is news when an edit lands in it,
not on every commit to its module. ``changed_spans`` gives, per file, the line ranges of the
definitions a change added or edited.
"""

from __future__ import annotations

from dataclasses import dataclass

from magellan_lite.polyglot.core.diff import ChangeKind, ChangeSet
from magellan_lite.polyglot.core.model import Graph, Node, NodeKind

#: definitions whose edit puts their lines in scope (locals and parameters ride along with
#: the function that holds them)
SPAN_KINDS = (NodeKind.FUNCTION, NodeKind.METHOD, NodeKind.CLASS, NodeKind.GLOBAL,
              NodeKind.CLASS_ATTR)

#: changes that leave the definition's own lines untouched
_NOT_EDITS = (ChangeKind.REMOVED, ChangeKind.MOVED, ChangeKind.DOC_CHANGED)


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    node: Node

    def __contains__(self, line: int) -> bool:
        return self.start <= line <= self.end


def changed_spans(graph: Graph, changeset: ChangeSet | None) -> dict[str, list[Span]]:
    """``{path: [Span]}`` for every definition the change added or edited, innermost last.

    A class whose body changed is not a span of its own when one of its methods carries the
    edit: the method is the narrower answer. A whole new file counts in full.
    """
    if changeset is None:
        return {}
    out: dict[str, list[Span]] = {}
    for c in changeset.changes:
        if c.kind in _NOT_EDITS:
            continue
        n = graph.nodes.get(c.node_id)
        if n is None or not n.path or n.kind not in SPAN_KINDS or not n.lineno:
            continue
        out.setdefault(n.path, []).append(Span(n.lineno, max(n.end_lineno or n.lineno, n.lineno), n))
    added = set(changeset.files_added)
    if added:                            # one pass over the graph, not one per new file
        for n in graph.nodes.values():
            if n.path in added and n.kind in SPAN_KINDS and n.lineno:
                out.setdefault(n.path, []).append(Span(n.lineno, max(n.end_lineno or n.lineno,
                                                                      n.lineno), n))
    for path, spans in out.items():
        uniq = {(s.start, s.end, s.node.id): s for s in spans}
        out[path] = sorted(uniq.values(), key=lambda s: (s.start, -s.end))
    return out


def owner(spans: list[Span], line: int) -> Node | None:
    """The innermost changed definition holding ``line``, or None when the line is out of scope."""
    best: Span | None = None
    for s in spans:
        if line in s and (best is None or s.end - s.start <= best.end - best.start):
            best = s
    return best.node if best else None
