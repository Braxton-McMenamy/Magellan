"""Shared state threaded through the analysis passes."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import Enum

from magellan_lite.polyglot.python.project import Project, SourceFile
from magellan_lite.polyglot.core.model import Graph
from magellan_lite.polyglot.core.taxonomy import Signals


class ScopeKind(str, Enum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    COMPREHENSION = "comprehension"


@dataclass
class Scope:
    kind: ScopeKind
    node_id: str
    qualname: str
    parent: "Scope | None" = None
    #: names defined here -> node id
    names: dict[str, str] = field(default_factory=dict)
    #: names whose runtime class we could infer -> class node id
    types: dict[str, str] = field(default_factory=dict)
    globals_declared: set[str] = field(default_factory=set)
    nonlocals_declared: set[str] = field(default_factory=set)
    #: the class node id, when this scope is a method body
    owner_class: str | None = None


@dataclass
class ImportTarget:
    """What a name bound by an import statement actually refers to."""

    kind: str          # "module" | "symbol" | "external"
    module: str        # internal module name, or external dotted root
    symbol: str = ""   # for kind == "symbol"
    dotted: str = ""   # original text, for external reporting
    #: does not run at import time: inside a function, or under `if TYPE_CHECKING:`.
    #: Still a dependency, but it cannot form an import-time cycle.
    deferred: bool = False


@dataclass
class ModuleInfo:
    file: SourceFile
    tree: ast.Module
    node_id: str
    scope: Scope
    imports: dict[str, ImportTarget] = field(default_factory=dict)
    dunder_all: list[str] | None = None
    star_imports: list[str] = field(default_factory=list)
    #: imports inside functions, keyed by their alias node id. Keyed by bound name in
    #: the module-wide table, `a()` importing fast.impl and `b()` importing slow.impl
    #: overwrote each other and both resolved to whichever came last.
    local_imports: dict[str, "ImportTarget"] = field(default_factory=dict)


@dataclass
class AnalysisState:
    project: Project
    graph: Graph
    config: "Config"
    modules: dict[str, ModuleInfo] = field(default_factory=dict)
    #: scope attached to a def/module AST node, keyed by id() of that node
    scopes: dict[int, Scope] = field(default_factory=dict)
    #: class node id -> {member name: node id}
    class_members: dict[str, dict[str, str]] = field(default_factory=dict)
    #: class node id -> unresolved base expressions (text)
    class_base_exprs: dict[str, list[ast.expr]] = field(default_factory=dict)
    #: class node id -> resolved base class node ids
    class_parents: dict[str, list[str]] = field(default_factory=dict)
    #: node id -> labelling signals
    signals: dict[str, Signals] = field(default_factory=dict)
    #: bare method name -> class node ids defining it (duck-typing fallback)
    method_index: dict[str, list[str]] = field(default_factory=dict)
    #: module -> id()s it owns in `scopes`, so a re-analyzed file can purge its
    #: stale entries instead of leaking them across incremental updates
    module_scope_ids: dict[str, list[int]] = field(default_factory=dict)

    def sig(self, node_id: str) -> Signals:
        s = self.signals.get(node_id)
        if s is None:
            s = Signals()
            self.signals[node_id] = s
        return s


@dataclass
class Config:
    """Knobs that trade graph detail against size on very large codebases."""

    include_locals: bool = True
    #: settle guessed (duck-typed) calls with Jedi type inference, when it is installed
    infer_types: bool = True
    infer_budget_seconds: float = 30.0
    #: also map .ts/.tsx/.js/.jsx files, when Node.js and `typescript` are available
    typescript: bool = True
    #: frontends to skip by package name (``c``, ``cpp``, ``java``, ...); see analyze/frontends.py
    skip_languages: tuple[str, ...] = ()
    include_params: bool = True
    include_reads: bool = True
    #: allow low-confidence CALLS edges to same-named methods on unknown receivers
    duck_typing: bool = True
    #: give up on duck typing when a method name is this common (it means nothing)
    duck_typing_max_candidates: int = 4
    #: drop READS edges to builtins and other noise
    external_detail: bool = False
    excludes: set[str] = field(default_factory=set)
