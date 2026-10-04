"""The Magellan graph: node kinds, edge kinds, and the container that holds them.

Identity convention
-------------------
Every node has a string id of the form ``<kind-prefix>:<dotted.path>``. The
prefix makes ids self-describing and keeps namespaces from colliding (a class
``Config`` and a global ``Config`` in the same module are different nodes).

    pkg:app.services              package
    mod:app.services.billing      module
    cls:app.services.billing.Ledger
    fn:app.services.billing.charge            module-level function
    fn:app.services.billing.Ledger.post       method (same prefix, longer path)
    var:app.services.billing.RETRY_LIMIT      module global
    attr:app.services.billing.Ledger.rate     class attribute
    iattr:app.services.billing.Ledger.entries instance attribute (self.entries)
    loc:app.services.billing.charge.total     local variable
    par:app.services.billing.charge.amount    parameter
    ali:app.services.billing.Decimal          name bound by an import
    ext:requests.get                          outside the analyzed project

Ids are stable across line-number changes, which is what lets the differ tell
"this function moved" from "this function changed".
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator


class NodeKind(str, enum.Enum):
    PACKAGE = "package"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    GLOBAL = "global_var"
    CLASS_ATTR = "class_attr"
    INSTANCE_ATTR = "instance_attr"
    LOCAL = "local_var"
    PARAMETER = "parameter"
    IMPORT_ALIAS = "import_alias"
    EXTERNAL = "external"

    @property
    def prefix(self) -> str:
        return _KIND_PREFIX[self]

    @property
    def is_callable(self) -> bool:
        return self in (NodeKind.FUNCTION, NodeKind.METHOD)

    @property
    def is_container(self) -> bool:
        """Containers own other nodes and get a 'shell' hash rather than a body hash."""
        return self in (NodeKind.PACKAGE, NodeKind.MODULE, NodeKind.CLASS)

    @property
    def is_data(self) -> bool:
        return self in (
            NodeKind.GLOBAL,
            NodeKind.CLASS_ATTR,
            NodeKind.INSTANCE_ATTR,
            NodeKind.LOCAL,
            NodeKind.PARAMETER,
        )


_KIND_PREFIX: dict[NodeKind, str] = {
    NodeKind.PACKAGE: "pkg",
    NodeKind.MODULE: "mod",
    NodeKind.CLASS: "cls",
    NodeKind.FUNCTION: "fn",
    NodeKind.METHOD: "fn",
    NodeKind.GLOBAL: "var",
    NodeKind.CLASS_ATTR: "attr",
    NodeKind.INSTANCE_ATTR: "iattr",
    NodeKind.LOCAL: "loc",
    NodeKind.PARAMETER: "par",
    NodeKind.IMPORT_ALIAS: "ali",
    NodeKind.EXTERNAL: "ext",
}


def make_id(kind: NodeKind, qualname: str) -> str:
    return f"{kind.prefix}:{qualname}"


class EdgeKind(str, enum.Enum):
    """Relations between nodes. See :mod:`magellan_lite.polyglot.core.edges` for the propagation table."""

    # --- structural: who owns whom -------------------------------------
    CONTAINS = "contains"          # module -> class, class -> method, fn -> local
    PARAM_OF = "param_of"          # parameter -> function

    # --- linkage: how names reach across modules ------------------------
    IMPORTS = "imports"            # module -> module
    BINDS = "binds"                # import alias -> the thing it names
    REEXPORTS = "reexports"        # module -> symbol it republishes via __all__

    # --- type: the shape contract ---------------------------------------
    INHERITS = "inherits"          # class -> base class
    OVERRIDES = "overrides"        # method -> method it shadows in an ancestor
    ANNOTATES = "annotates"        # function/var -> class named in its annotation

    # --- control: who runs whom -----------------------------------------
    CALLS = "calls"                # callable -> callable
    INSTANTIATES = "instantiates"  # callable -> class
    DECORATES = "decorates"        # decorator callable -> decorated node
    RAISES = "raises"              # callable -> exception class
    HANDLES = "handles"            # callable -> exception class it catches

    # --- data: who touches which state ----------------------------------
    READS = "reads"                # callable -> variable/attribute
    WRITES = "writes"              # callable -> variable/attribute it rebinds
    MUTATES = "mutates"            # callable -> variable/attribute it mutates in place
    ASSIGNS_FROM = "assigns_from"  # variable -> callable whose result it holds

    # --- lifecycle: resources ------------------------------------------
    ACQUIRES = "acquires"          # callable -> resource-producing callable
    RELEASES = "releases"          # callable -> resource-closing callable


@dataclass
class Node:
    id: str
    kind: NodeKind
    name: str
    qualname: str
    module: str
    path: str = ""
    lineno: int = 0
    end_lineno: int = 0
    col: int = 0

    parent: str | None = None
    signature: str | None = None
    bases: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    public: bool = True

    # systems-design labelling, consumed by the UI (see magellan_lite.polyglot.core.taxonomy)
    layer: str = "unknown"
    data_role: str = "unknown"
    tags: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    # change detection
    sig_hash: str = ""
    body_hash: str = ""
    doc_hash: str = ""

    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Short human label, e.g. ``billing.Ledger.post``."""
        return self.qualname

    @property
    def location(self) -> str:
        return f"{self.path}:{self.lineno}" if self.path else self.module

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["kind"] = self.kind.value
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Node:
        d = dict(d)
        d["kind"] = NodeKind(d["kind"])
        return Node(**d)


#: Edge kinds whose every occurrence is a distinct site with its own data.
_PER_SITE_KINDS = frozenset({EdgeKind.CALLS, EdgeKind.INSTANTIATES})


@dataclass
class Edge:
    src: str
    dst: str
    kind: EdgeKind
    lineno: int = 0
    path: str = ""
    # 1.0 = statically certain; lower when we inferred it by duck typing etc.
    confidence: float = 1.0
    # True when the relation only fires on some paths (inside if/try/loop).
    conditional: bool = False
    # True when the target was reached through dynamic machinery (getattr, **kwargs).
    dynamic: bool = False
    context: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    #: Column of the expression. Part of the identity of call-shaped edges only: two
    #: calls on one line (`f("a") + f("b")`) are two call sites with different
    #: arguments, and collapsing them lost the second one's literals -- which let
    #: value-domain analysis call a live branch impossible.
    col: int = 0

    @property
    def key(self) -> tuple[str, str, str, int, int]:
        col = self.col if self.kind in _PER_SITE_KINDS else 0
        return (self.src, self.dst, self.kind.value, self.lineno, col)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["kind"] = self.kind.value
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Edge:
        d = dict(d)
        d["kind"] = EdgeKind(d["kind"])
        return Edge(**d)


@dataclass
class FileRecord:
    """One analyzed source file. ``sha256`` is the cheap first-pass change filter."""

    path: str
    module: str
    sha256: str
    lines: int = 0
    source_root: str = ""
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> FileRecord:
        return FileRecord(**d)


def _fold(a: str, b: str) -> str:
    return hashlib.sha256(f"{a}|{b}".encode()).hexdigest()[:16]


def _merge_redefinition(existing: Node, node: Node) -> None:
    """Fold a second definition of the same name into the first.

    The same id is defined more than once by ``@overload`` stubs followed by the
    implementation, ``@property`` plus ``@x.setter``, and ``if``/``else`` or
    ``try``/``except ImportError`` alternatives. Keeping only the first meant the
    code that actually runs -- the overload implementation, the setter -- had no
    hash at all, so editing it produced no change and a clean verdict.

    Every definition's hashes are folded in, in source order, so an edit to any of
    them is a change. The location stays at the first definition, but the span grows
    to cover all of them. Signature and arity come from the last definition that is
    neither an ``@overload`` stub nor a property accessor, which is what callers bind to.
    """
    existing.tags = sorted(set(existing.tags) | set(node.tags))
    for attr in ("sig_hash", "body_hash", "doc_hash"):
        mine, theirs = getattr(existing, attr), getattr(node, attr)
        if theirs:
            setattr(existing, attr, _fold(mine, theirs) if mine else theirs)
    existing.end_lineno = max(existing.end_lineno, node.end_lineno)
    existing.decorators = existing.decorators + [
        d for d in node.decorators if d not in existing.decorators]
    tails = {d.split("(")[0].rsplit(".", 1)[-1] for d in node.decorators}
    is_stub = "overload" in tails
    is_accessor = bool(tails & {"setter", "deleter", "getter"})
    if node.signature and not is_stub and not is_accessor:
        existing.signature = node.signature
        if "arity" in node.meta:
            existing.meta["arity"] = node.meta["arity"]
    elif not existing.signature:
        existing.signature = node.signature
    existing.meta["redefined"] = existing.meta.get("redefined", 0) + 1


class Graph:
    """Nodes, edges, and the adjacency indexes propagation walks over."""

    def __init__(self, root: str = "") -> None:
        self.root = root
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.files: dict[str, FileRecord] = {}
        self._edge_keys: set[tuple[str, str, str, int, int]] = set()
        self._out: dict[str, list[int]] = {}
        self._in: dict[str, list[int]] = {}
        self.diagnostics: list[str] = []

    # -- construction ---------------------------------------------------
    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing is not None:
            _merge_redefinition(existing, node)
            return existing
        self.nodes[node.id] = node
        return node

    #: Self-edges are noise for most relations (a module "containing" itself), but
    #: for a call they are the whole point: direct recursion. Dropping them made
    #: `def walk(n): return walk(n)` look like a function with no callers.
    SELF_EDGE_KINDS = frozenset({EdgeKind.CALLS, EdgeKind.INSTANTIATES})

    def add_edge(self, edge: Edge) -> Edge | None:
        if edge.src == edge.dst and edge.kind not in self.SELF_EDGE_KINDS:
            return None
        if edge.key in self._edge_keys:
            return None
        self._edge_keys.add(edge.key)
        idx = len(self.edges)
        self.edges.append(edge)
        self._out.setdefault(edge.src, []).append(idx)
        self._in.setdefault(edge.dst, []).append(idx)
        return edge

    def reindex(self) -> None:
        self._out.clear()
        self._in.clear()
        self._edge_keys.clear()
        for idx, e in enumerate(self.edges):
            self._edge_keys.add(e.key)
            self._out.setdefault(e.src, []).append(idx)
            self._in.setdefault(e.dst, []).append(idx)

    # -- queries --------------------------------------------------------
    def out_edges(self, node_id: str) -> list[Edge]:
        return [self.edges[i] for i in self._out.get(node_id, ())]

    def in_edges(self, node_id: str) -> list[Edge]:
        return [self.edges[i] for i in self._in.get(node_id, ())]

    def neighbors(self, node_id: str) -> Iterator[tuple[Edge, str, bool]]:
        """Yield ``(edge, other_id, outgoing)`` for every edge touching ``node_id``."""
        for e in self.out_edges(node_id):
            yield e, e.dst, True
        for e in self.in_edges(node_id):
            yield e, e.src, False

    def of_kind(self, *kinds: NodeKind) -> Iterator[Node]:
        wanted = set(kinds)
        for n in self.nodes.values():
            if n.kind in wanted:
                yield n

    def children(self, node_id: str) -> list[Node]:
        return [
            self.nodes[e.dst]
            for e in self.out_edges(node_id)
            if e.kind is EdgeKind.CONTAINS and e.dst in self.nodes
        ]

    def enclosing_module(self, node_id: str) -> Node | None:
        node = self.nodes.get(node_id)
        if node is None:
            return None
        if node.kind is NodeKind.MODULE:
            return node
        return self.nodes.get(make_id(NodeKind.MODULE, node.module))

    def stats(self) -> dict[str, Any]:
        by_node: dict[str, int] = {}
        for n in self.nodes.values():
            by_node[n.kind.value] = by_node.get(n.kind.value, 0) + 1
        by_edge: dict[str, int] = {}
        for e in self.edges:
            by_edge[e.kind.value] = by_edge.get(e.kind.value, 0) + 1
        return {
            "files": len(self.files),
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "nodes_by_kind": dict(sorted(by_node.items(), key=lambda kv: -kv[1])),
            "edges_by_kind": dict(sorted(by_edge.items(), key=lambda kv: -kv[1])),
        }

    # -- serialization --------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "magellan": 1,
            "root": self.root,
            "files": [f.to_dict() for f in self.files.values()],
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "diagnostics": self.diagnostics,
        }

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=1, sort_keys=False)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Graph:
        g = Graph(root=d.get("root", ""))
        for fd in d.get("files", []):
            rec = FileRecord.from_dict(fd)
            g.files[rec.path] = rec
        for nd in d.get("nodes", []):
            n = Node.from_dict(nd)
            g.nodes[n.id] = n
        g.edges = [Edge.from_dict(ed) for ed in d.get("edges", [])]
        g.diagnostics = list(d.get("diagnostics", []))
        g.reindex()
        return g

    @staticmethod
    def load(path: str) -> Graph:
        with open(path, encoding="utf-8") as fh:
            return Graph.from_dict(json.load(fh))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Graph nodes={len(self.nodes)} edges={len(self.edges)} files={len(self.files)}>"
