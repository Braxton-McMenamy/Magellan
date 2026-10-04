"""Edge conventions and the propagation table.

The important idea in Magellan is that change impact does *not* travel along
edges in the direction the edge points. It depends on the relation:

* ``CALLS`` points caller -> callee. If the **callee** changes, the caller may
  break, so impact travels *backwards* (``reverse`` weight is high).
* ``WRITES`` points writer -> variable. If the **writer** changes, the variable's
  value changes, so impact travels *forwards* (``forward`` weight is high) and
  then onward to every reader of that variable.

Each edge kind therefore declares two weights in ``[0, 1]``:

``forward``
    impact flowing src -> dst ("I changed; what did I affect downstream?")
``reverse``
    impact flowing dst -> src ("this changed; who depended on it?")

Weights multiply along a path, so a 4-hop chain of 0.9 edges scores 0.65 while a
chain through a 0.1 edge dies out. That gives a naturally ranked blast radius
instead of "everything is connected to everything".

Zero means impact does not propagate that way at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from magellan_lite.polyglot.core.model import EdgeKind

#: Coarse grouping used for UI filters and for the legend in reports.
CATEGORIES = {
    "structural": "Ownership and containment -- the skeleton of the map.",
    "linkage": "How names reach across module boundaries.",
    "type": "Shape contracts: inheritance, overrides, annotations.",
    "control": "Who executes whom.",
    "data": "Who touches which state.",
    "lifecycle": "Resource acquisition and release.",
}


@dataclass(frozen=True)
class EdgeSpec:
    kind: EdgeKind
    category: str
    src_reads: str          # how to read the edge aloud, src -> dst
    forward: float          # impact weight src -> dst
    reverse: float          # impact weight dst -> src
    why_forward: str
    why_reverse: str
    #: Drawn as a dashed line in the UI: relation is inferred, not literal.
    inferred: bool = False

    def weight(self, outgoing: bool) -> float:
        """``outgoing`` is True when we are walking src -> dst."""
        return self.forward if outgoing else self.reverse


def _spec(*args, **kw) -> EdgeSpec:
    return EdgeSpec(*args, **kw)


EDGE_SPECS: dict[EdgeKind, EdgeSpec] = {
    # ---- structural ---------------------------------------------------
    EdgeKind.CONTAINS: _spec(
        EdgeKind.CONTAINS, "structural", "{src} contains {dst}",
        forward=0.30, reverse=0.12,
        why_forward="The container's shell changed (imports, class body, decorators), "
                    "so members may be redefined or reordered.",
        why_reverse="A member changed, which slightly changes the container's surface.",
    ),
    EdgeKind.PARAM_OF: _spec(
        EdgeKind.PARAM_OF, "structural", "{src} is a parameter of {dst}",
        forward=0.55, reverse=0.70,
        why_forward="The parameter's meaning or type changed, so the function body "
                    "that consumes it is affected.",
        why_reverse="The function's signature changed, so this parameter may have "
                    "moved, been renamed, or been dropped.",
    ),

    # ---- linkage ------------------------------------------------------
    EdgeKind.IMPORTS: _spec(
        EdgeKind.IMPORTS, "linkage", "{src} imports {dst}",
        forward=0.08, reverse=0.55,
        why_forward="The importing module changed; the imported module rarely cares.",
        why_reverse="The imported module changed, so anything it publishes may have "
                    "shifted under this importer.",
    ),
    EdgeKind.BINDS: _spec(
        EdgeKind.BINDS, "linkage", "{src} is a local name for {dst}",
        forward=0.20, reverse=0.90,
        why_forward="The alias was rebound to something else.",
        why_reverse="The bound target changed, and this name is how the module "
                    "reaches it.",
    ),
    EdgeKind.REEXPORTS: _spec(
        EdgeKind.REEXPORTS, "linkage", "{src} re-exports {dst}",
        forward=0.15, reverse=0.80,
        why_forward="The module's public surface (__all__) changed.",
        why_reverse="A re-exported symbol changed, so every downstream consumer of "
                    "this module's public API is affected.",
    ),

    # ---- type ---------------------------------------------------------
    EdgeKind.INHERITS: _spec(
        EdgeKind.INHERITS, "type", "{src} inherits from {dst}",
        forward=0.12, reverse=0.95,
        why_forward="The subclass changed; the base is unaffected.",
        why_reverse="The base class changed. Every subclass inherits that change, "
                    "including behaviour it never asked for.",
    ),
    EdgeKind.OVERRIDES: _spec(
        EdgeKind.OVERRIDES, "type", "{src} overrides {dst}",
        forward=0.50, reverse=0.85,
        why_forward="The override changed, so callers holding the base type now see "
                    "different behaviour through the same contract.",
        why_reverse="The overridden method's contract changed; this override may no "
                    "longer be substitutable for it.",
        inferred=True,
    ),
    EdgeKind.ANNOTATES: _spec(
        EdgeKind.ANNOTATES, "type", "{src} is annotated as {dst}",
        forward=0.10, reverse=0.65,
        why_forward="The annotated thing changed, not the type.",
        why_reverse="The type changed shape, so everything declared to be that type "
                    "may need to change with it.",
    ),

    # ---- control ------------------------------------------------------
    EdgeKind.CALLS: _spec(
        EdgeKind.CALLS, "control", "{src} calls {dst}",
        forward=0.22, reverse=0.90,
        why_forward="The caller changed, so it may now pass different arguments into "
                    "this callee.",
        why_reverse="The callee changed its behaviour, signature, or return value, "
                    "and this caller depends on it.",
    ),
    EdgeKind.INSTANTIATES: _spec(
        EdgeKind.INSTANTIATES, "control", "{src} constructs {dst}",
        forward=0.20, reverse=0.90,
        why_forward="The constructing code changed its arguments.",
        why_reverse="The class's constructor or invariants changed, and this code "
                    "builds instances of it.",
    ),
    EdgeKind.DECORATES: _spec(
        EdgeKind.DECORATES, "control", "{src} decorates {dst}",
        forward=0.85, reverse=0.15,
        why_forward="The decorator changed, so it wraps every decorated target "
                    "differently -- a change with no visible call site.",
        why_reverse="The decorated target changed.",
    ),
    EdgeKind.RAISES: _spec(
        EdgeKind.RAISES, "control", "{src} raises {dst}",
        forward=0.35, reverse=0.45,
        why_forward="The raiser changed, so this exception may now fire under new "
                    "conditions.",
        why_reverse="The exception type changed shape, and this code constructs it.",
    ),
    EdgeKind.HANDLES: _spec(
        EdgeKind.HANDLES, "control", "{src} catches {dst}",
        forward=0.25, reverse=0.50,
        why_forward="The handler changed, so failures are absorbed differently.",
        why_reverse="The exception type changed, so this handler may stop matching "
                    "it -- errors would escape instead of being caught.",
    ),

    # ---- data ---------------------------------------------------------
    EdgeKind.READS: _spec(
        EdgeKind.READS, "data", "{src} reads {dst}",
        forward=0.06, reverse=0.85,
        why_forward="The reader changed.",
        why_reverse="The state this code reads changed its value, type, or lifetime.",
    ),
    EdgeKind.WRITES: _spec(
        EdgeKind.WRITES, "data", "{src} writes {dst}",
        forward=0.80, reverse=0.45,
        why_forward="The writer changed, so the value landing in this state is "
                    "different -- and every reader of it inherits that.",
        why_reverse="The state's declaration changed, so this write may no longer fit.",
    ),
    EdgeKind.MUTATES: _spec(
        EdgeKind.MUTATES, "data", "{src} mutates {dst} in place",
        forward=0.88, reverse=0.50,
        why_forward="In-place mutation changes shared state without rebinding it. "
                    "Readers elsewhere see the new contents with no assignment to "
                    "point at -- this is how unbounded growth and aliasing bugs "
                    "spread.",
        why_reverse="The mutated state's definition changed.",
    ),
    EdgeKind.ASSIGNS_FROM: _spec(
        EdgeKind.ASSIGNS_FROM, "data", "{src} holds the result of {dst}",
        forward=0.30, reverse=0.85,
        why_forward="The variable changed.",
        why_reverse="The producing call's return value changed, so this variable now "
                    "holds something different.",
    ),

    # ---- lifecycle ----------------------------------------------------
    EdgeKind.ACQUIRES: _spec(
        EdgeKind.ACQUIRES, "lifecycle", "{src} acquires a resource via {dst}",
        forward=0.45, reverse=0.70,
        why_forward="The acquiring code changed, so resource lifetime may have "
                    "changed.",
        why_reverse="The resource factory changed what it hands out or who must "
                    "release it.",
        inferred=True,
    ),
    EdgeKind.RELEASES: _spec(
        EdgeKind.RELEASES, "lifecycle", "{src} releases a resource via {dst}",
        forward=0.45, reverse=0.60,
        why_forward="The releasing code changed.",
        why_reverse="The release path changed.",
        inferred=True,
    ),
}

assert set(EDGE_SPECS) == set(EdgeKind), "every EdgeKind needs a propagation spec"


def spec(kind: EdgeKind) -> EdgeSpec:
    return EDGE_SPECS[kind]


def conventions_table() -> list[dict[str, object]]:
    """Machine-readable dump of the edge conventions, for docs and for the UI."""
    rows: list[dict[str, object]] = []
    for kind, s in EDGE_SPECS.items():
        rows.append(
            {
                "edge": kind.value,
                "category": s.category,
                "reads": s.src_reads.format(src="A", dst="B"),
                "forward": s.forward,
                "reverse": s.reverse,
                "inferred": s.inferred,
                "why_forward": s.why_forward,
                "why_reverse": s.why_reverse,
            }
        )
    rows.sort(key=lambda r: (list(CATEGORIES).index(str(r["category"])), str(r["edge"])))
    return rows


#: Edge kinds that mean "this code actually uses that node". Containment and
#: parameter ownership are deliberately excluded: a module *containing* a
#: function is not a reference to it, and treating it as one makes every
#: deletion look like a dangling reference to itself.
REFERENCING_KINDS: frozenset[EdgeKind] = frozenset({
    EdgeKind.CALLS, EdgeKind.INSTANTIATES, EdgeKind.READS, EdgeKind.WRITES,
    EdgeKind.MUTATES, EdgeKind.DECORATES, EdgeKind.REEXPORTS, EdgeKind.OVERRIDES,
    EdgeKind.INHERITS, EdgeKind.ANNOTATES, EdgeKind.ASSIGNS_FROM, EdgeKind.BINDS,
    EdgeKind.RAISES, EdgeKind.HANDLES, EdgeKind.IMPORTS,
})
