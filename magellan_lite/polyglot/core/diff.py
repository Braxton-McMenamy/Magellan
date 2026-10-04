"""Change detection: what actually changed between two maps.

Works on two graphs, so the "before" side can come from a saved baseline or from
``git show HEAD:...`` -- never from running the code. Comparison is by node id
and structural hash, which means:

* reformatting and moving code around produce no changes
* editing a method body marks that method, not its class and not its module
* renaming a function that keeps its body is reported as a rename, not as a
  delete plus an unrelated addition

Each change carries a severity in ``[0, 1]`` that becomes its seed weight when
propagation starts. A signature change starts at 0.95 because it can break
callers nobody edited; a docstring edit starts at 0.05 and dies out immediately.
"""

from __future__ import annotations

import ast
import enum
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from magellan_lite.polyglot.python import py2
from magellan_lite.polyglot.core.edges import REFERENCING_KINDS
from magellan_lite.polyglot.core.hashing import body_hash
from magellan_lite.polyglot.core.model import Edge, EdgeKind, Graph, Node, NodeKind


class ChangeKind(str, enum.Enum):
    ADDED = "added"
    REMOVED = "removed"
    RENAMED = "renamed"
    MOVED = "moved"
    SIGNATURE_CHANGED = "signature_changed"
    BASES_CHANGED = "bases_changed"
    DECORATORS_CHANGED = "decorators_changed"
    BODY_CHANGED = "body_changed"
    VALUE_CHANGED = "value_changed"
    ANNOTATION_CHANGED = "annotation_changed"
    DOC_CHANGED = "doc_changed"


#: Seed weight for propagation. Order matters: the first matching kind wins when
#: several apply to one node, so the most consequential is listed first.
SEVERITY: dict[ChangeKind, float] = {
    ChangeKind.REMOVED: 1.00,
    ChangeKind.SIGNATURE_CHANGED: 0.95,
    ChangeKind.BASES_CHANGED: 0.90,
    ChangeKind.DECORATORS_CHANGED: 0.85,
    ChangeKind.VALUE_CHANGED: 0.70,
    ChangeKind.BODY_CHANGED: 0.60,
    ChangeKind.ANNOTATION_CHANGED: 0.50,
    ChangeKind.ADDED: 0.35,
    ChangeKind.RENAMED: 0.80,
    ChangeKind.MOVED: 0.25,
    ChangeKind.DOC_CHANGED: 0.05,
}

_RANK = list(SEVERITY)


@dataclass
class Change:
    node_id: str
    kind: ChangeKind
    label: str
    node_kind: NodeKind
    path: str = ""
    lineno: int = 0
    detail: str = ""
    #: for signature changes: the concrete ways existing call sites can break
    breaks: list[str] = field(default_factory=list)
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    #: the same breaks, structured so each can be tested against a call site
    specs: list[dict] = field(default_factory=list)

    @property
    def severity(self) -> float:
        if self.kind is ChangeKind.SIGNATURE_CHANGED and self.before.get("arity") is not None:
            # The seed should follow what the change can do to callers. It used
            # to be 0.95 for any signature edit, so a type-hint-only change
            # started propagating harder than a real behaviour change (body, 0.60).
            hard = [b for b in self.specs if b.get("hard")]
            if hard:
                return min(1.0, SEVERITY[self.kind] + 0.05 * len(hard))
            if self.specs:
                return 0.45                       # only defaults changed
            if self.before.get("arity") == self.after.get("arity"):
                return 0.10                       # annotations or stub typing only
            return 0.30                           # compatible: e.g. a new optional arg
        base = SEVERITY[self.kind]
        return min(1.0, base + 0.05 * len(self.breaks))

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["kind"] = self.kind.value
        d["node_kind"] = self.node_kind.value
        d["severity"] = self.severity
        return d


@dataclass
class ChangeSet:
    changes: list[Change] = field(default_factory=list)
    files_added: list[str] = field(default_factory=list)
    files_removed: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.changes)

    def significant(self, floor: float = 0.1) -> list[Change]:
        return sorted(
            (c for c in self.changes if c.severity >= floor),
            key=lambda c: (-c.severity, c.label),
        )

    def seeds(self) -> dict[str, float]:
        """node id -> starting impact weight, strongest change wins."""
        seeds: dict[str, float] = {}
        for c in self.changes:
            if c.severity > seeds.get(c.node_id, 0.0):
                seeds[c.node_id] = c.severity
        return seeds

    def to_dict(self) -> dict[str, Any]:
        return {
            "changes": [c.to_dict() for c in self.changes],
            "files_added": self.files_added,
            "files_removed": self.files_removed,
            "files_modified": self.files_modified,
        }


def compare_arity(old: dict, new: dict) -> list[str]:
    """Concrete ways a signature change breaks call sites that nobody edited."""
    return [b["text"] for b in arity_breaks(old, new)]


def arity_breaks(old: dict, new: dict) -> list[dict]:
    """Signature differences, each saying which *call shapes* it breaks.

    A break is not a property of the signature alone. Moving ``writable`` after
    ``readable`` breaks ``Path(True, True, True, False)`` and nothing that passes
    keywords; changing a default only matters to callers that omit the argument.
    Judged per signature, click's ``Path.__init__`` reorder was reported critical
    against 24 call sites that all pass keywords. Each entry therefore carries a
    ``test`` that :func:`affects` applies to one call site.

    ``hard`` breaks raise TypeError or silently bind the wrong value; soft ones
    (a changed default) change behaviour without failing.
    """
    out: list[dict] = []
    if not old or not new:
        return out
    lang = new.get("lang") or old.get("lang")
    if lang:
        from magellan_lite.polyglot.analyze.frontends import semantics
        mod = semantics(lang)
        if mod is not None and hasattr(mod, "arity_breaks"):
            return mod.arity_breaks(old, new)
    o_pos: list[str] = list(old.get("positional", []))
    n_pos: list[str] = list(new.get("positional", []))
    o_kw: list[str] = list(old.get("keyword_only", []))
    n_kw: list[str] = list(new.get("keyword_only", []))
    o_req = int(old.get("required_positional", 0))
    n_req = int(new.get("required_positional", 0))

    if n_req > o_req:
        added = n_pos[o_req:n_req]
        out.append({"hard": True, "test": "fewer-positional", "required": n_pos[:n_req],
                    "text": f"requires {n_req} positional arguments, was {o_req}"
                            + (f" (new required: {', '.join(added)})" if added else "")
                            + " -- existing calls passing the old count raise TypeError"})

    dropped_pos = [p for p in o_pos if p not in n_pos and p not in n_kw]
    for p in dropped_pos:
        i = o_pos.index(p)
        renamed = (i < len(n_pos) and n_pos[i] not in o_pos and n_pos[i] not in o_kw
                   and (i < o_req) == (i < n_req))
        if renamed:
            # the same slot under a new name: positional calls bind exactly as before
            out.append({"hard": True, "test": "uses-keyword", "name": p,
                        "text": f"parameter {p} renamed to {n_pos[i]} -- calls passing {p} by "
                                f"keyword raise TypeError; positional calls are unaffected"})
            continue
        out.append({"hard": True, "test": "uses-slot-or-keyword", "name": p,
                    "at": i + 1,
                    "text": f"dropped parameter {p} -- calls passing it by keyword raise "
                            f"TypeError, and positional calls reaching it bind shifted values"})
    if not new.get("star_kwargs"):
        for k in o_kw:
            if k not in n_kw and k not in n_pos:
                out.append({"hard": True, "test": "uses-keyword", "name": k,
                            "text": f"dropped keyword-only parameter {k} -- calls passing "
                                    f"it raise TypeError"})

    for p in o_pos:
        if p in n_pos and o_pos.index(p) != n_pos.index(p):
            first = min(o_pos.index(p), n_pos.index(p))
            out.append({"hard": True, "test": "reaches-slot", "at": first + 1, "name": p,
                        "text": f"parameter {p} moved from position {o_pos.index(p)} to "
                                f"{n_pos.index(p)} -- positional calls reaching it bind "
                                f"the wrong value"})
        if p in n_kw:
            out.append({"hard": True, "test": "reaches-slot", "at": o_pos.index(p) + 1,
                        "name": p,
                        "text": f"{p} became keyword-only -- positional calls passing it "
                                f"raise TypeError"})

    if "positional_only" in old:
        for p in new.get("positional_only", []):
            if p not in old.get("positional_only", []):
                out.append({"hard": True, "test": "uses-keyword", "name": p,
                            "text": f"{p} became positional-only -- calls passing it by "
                                    f"keyword raise TypeError"})

    newly_required = sorted(set(new.get("required_keyword_only", []))
                            - set(old.get("required_keyword_only", [])))
    for k in newly_required:
        out.append({"hard": True, "test": "omits-keyword", "name": k,
                    "text": f"new required keyword-only argument {k} -- calls omitting it "
                            f"raise TypeError"})
    if old.get("star_kwargs") and not new.get("star_kwargs"):
        out.append({"hard": True, "test": "unknown-keyword",
                    "accepts": [p for p in n_pos if p not in new.get("positional_only", [])]
                               + n_kw,
                    "text": "no longer accepts **kwargs -- extra keywords now raise TypeError"})
    if old.get("star_args") and not new.get("star_args"):
        out.append({"hard": True, "test": "reaches-slot", "at": len(n_pos) + 1,
                    "text": "no longer accepts *args -- extra positionals now raise TypeError"})

    # Compare defaults per parameter, not as a positional list. Adding a new
    # optional argument shifts the whole list, which used to be reported as
    # "callers relying on the old default" -- for a default that never existed.
    for name, (o, n) in _shared_defaults(old, new).items():
        if o != n:
            out.append({"hard": False, "test": "omits", "name": name,
                        "at": n_pos.index(name) + 1 if name in n_pos else None,
                        "text": f"default value changed for {name}: {o} -> {n} -- callers "
                                f"relying on the old default change behaviour without "
                                f"being edited"})
    return out


def call_shape(edge: Edge) -> dict:
    """How one call site passes its arguments, from what refs recorded on the edge."""
    meta = edge.meta
    opaque = list(meta.get("opaque") or [])
    keywords = {k for part in (meta.get("lits") or {}, meta.get("fwd") or {})
                for k in part if not k.isdigit()}
    keywords |= {k for k in opaque if not k.isdigit() and k not in ("*", "**")}
    return {"positional": int(meta.get("args", 0)), "keywords": keywords,
            "star": "*" in opaque, "dstar": "**" in opaque,
            "known": "args" in meta, "handled": list(meta.get("handled") or ())}


def affects(brk: dict, site: dict, offset: int) -> bool:
    """Would this break hit a call site shaped like ``site``?

    ``offset`` is 1 when the signature's first parameter is the implicit ``self``/
    ``cls`` of a bound call. Anything the site does not let us see -- ``*args``
    unpacking, an edge recorded before shapes were -- counts as affected.
    """
    if not site["known"]:
        return True
    reach = site["positional"] + offset          # slots filled positionally, 1-based
    kws, test = site["keywords"], brk["test"]
    if site["star"] and test in ("reaches-slot", "uses-slot-or-keyword", "fewer-positional"):
        return True
    if site["dstar"] and test in ("uses-keyword", "uses-slot-or-keyword", "unknown-keyword",
                                  "omits-keyword", "omits"):
        return True
    if test == "fewer-positional":
        # every required parameter past the last positional slot must come by keyword
        return any(n not in kws for n in brk["required"][reach:])
    if test == "reaches-slot":
        return reach >= brk["at"]
    if test == "uses-keyword":
        return brk["name"] in kws
    if test == "uses-slot-or-keyword":
        return brk["name"] in kws or reach >= brk["at"]
    if test == "omits-keyword":
        return brk["name"] not in kws
    if test == "unknown-keyword":
        return bool(kws - set(brk["accepts"]))
    if test == "omits":
        filled = brk["at"] is not None and reach >= brk["at"]
        return not filled and brk["name"] not in kws
    if test == "unhandled-exception":
        # a new checked exception: sites that already catch or declare it (or a supertype)
        return not set(brk.get("accepts", ())) & set(site.get("handled", ()))
    if test == "positional-count":
        # languages without keywords/defaults resolution: the break names the counts it hits
        return site["positional"] in set(brk.get("counts", ())) or brk.get("counts") is None
    return True


def _defaults_by_name(arity: dict) -> dict[str, str]:
    positional: list[str] = list(arity.get("positional", []))
    defaults: list[str] = list(arity.get("defaults", []))
    if not defaults:
        return {}
    return dict(zip(positional[len(positional) - len(defaults):], defaults))


def _shared_defaults(old: dict, new: dict) -> dict[str, tuple[str, str]]:
    o, n = _defaults_by_name(old), _defaults_by_name(new)
    return {name: (o[name], n[name]) for name in o.keys() & n.keys()}


def _mk(node: Node, kind: ChangeKind, detail: str = "", **kw) -> Change:
    return Change(
        node_id=node.id,
        kind=kind,
        label=node.qualname,
        node_kind=node.kind,
        path=node.path,
        lineno=node.lineno,
        detail=detail,
        **kw,
    )


def diff_graphs(old: Graph, new: Graph, include_cosmetic: bool = False) -> ChangeSet:
    cs = ChangeSet()

    old_files = set(old.files)
    new_files = set(new.files)
    cs.files_added = sorted(new_files - old_files)
    cs.files_removed = sorted(old_files - new_files)
    cs.files_modified = sorted(
        p for p in old_files & new_files if old.files[p].sha256 != new.files[p].sha256
    )

    removed_ids = set(old.nodes) - set(new.nodes)
    added_ids = set(new.nodes) - set(old.nodes)

    for old_id, new_id in pair_moves(old, new, removed_ids, added_ids):
        o, n = old.nodes[old_id], new.nodes[new_id]
        kind = ChangeKind.MOVED if o.name == n.name else ChangeKind.RENAMED
        detail = (
            f"{o.qualname} -> {n.qualname}"
            if kind is ChangeKind.RENAMED
            else f"moved from {o.path}:{o.lineno} to {n.path}:{n.lineno}"
        )
        ch = _mk(n, kind, detail, before={"id": old_id}, after={"id": new_id})
        if kind is ChangeKind.RENAMED:
            ch.breaks = [f"every reference to '{o.name}' must become '{n.name}'"]
        if n.kind.is_callable:
            # a rename that also changes the parameters breaks callers twice over
            ch.breaks += compare_arity(o.meta.get("arity", {}), n.meta.get("arity", {}))
        cs.changes.append(ch)
        removed_ids.discard(old_id)
        added_ids.discard(new_id)

    for nid in sorted(removed_ids):
        n = old.nodes[nid]
        if n.kind in (NodeKind.EXTERNAL, NodeKind.PACKAGE, NodeKind.IMPORT_ALIAS,
                      NodeKind.LOCAL, NodeKind.PARAMETER):
            # Plumbing. Its disappearance is a consequence of a real edit
            # elsewhere, which is already reported on its own terms.
            continue
        if n.parent in removed_ids:
            continue  # the whole container went; do not itemize its insides
        referrers = surviving_references(old, new, nid)
        ch = _mk(n, ChangeKind.REMOVED,
                 f"{n.kind.value} deleted from {n.path or n.module}")
        if referrers:
            ch.breaks = [
                f"{len(referrers)} reference(s) survive in code that was not edited"
            ]
        cs.changes.append(ch)

    for nid in sorted(added_ids):
        n = new.nodes[nid]
        if n.kind in (NodeKind.EXTERNAL, NodeKind.PACKAGE, NodeKind.LOCAL,
                      NodeKind.PARAMETER, NodeKind.IMPORT_ALIAS):
            continue
        cs.changes.append(_mk(n, ChangeKind.ADDED, f"new {n.kind.value}"))

    for nid in sorted(set(old.nodes) & set(new.nodes)):
        o, n = old.nodes[nid], new.nodes[nid]
        if n.kind in (NodeKind.EXTERNAL, NodeKind.PACKAGE):
            continue
        cs.changes.extend(_diff_node(o, n, include_cosmetic))

    cs.changes.extend(_synthesized_constructor_changes(old, new))
    return cs


# --------------------------------------------------------------------------
# dataclass / NamedTuple constructors
# --------------------------------------------------------------------------
def _is_record(graph: Graph, cls: Node) -> str:
    """"dataclass", "namedtuple", or "" -- classes whose __init__ is their fields."""
    if f"fn:{cls.qualname}.__init__" in graph.nodes:
        return ""                           # an explicit __init__ is diffed on its own
    tails = {d.split("(")[0].rsplit(".", 1)[-1] for d in cls.decorators}
    if "dataclass" in tails:
        return "dataclass"
    if any(b.rsplit(".", 1)[-1] == "NamedTuple" for b in cls.bases):
        return "namedtuple"
    return ""


def record_arity(graph: Graph, cls: Node, _seen: frozenset[str] = frozenset()) -> dict | None:
    """The ``__init__`` a dataclass or NamedTuple gets from its annotated fields."""
    kind = _is_record(graph, cls)
    if not kind or cls.id in _seen:
        return None
    fields: list[tuple[str, bool, bool]] = []       # (name, has_default, kw_only)
    for e in graph.out_edges(cls.id):               # base-class fields come first
        base = graph.nodes.get(e.dst)
        if e.kind is EdgeKind.INHERITS and base is not None and base.kind is NodeKind.CLASS:
            inherited = record_arity(graph, base, _seen | {cls.id})
            if inherited:
                fields += [(p, i >= inherited["required_positional"], False)
                           for i, p in enumerate(inherited["positional"])]
                fields += [(k, k not in inherited["required_keyword_only"], True)
                           for k in inherited["keyword_only"]]
    all_kw = any("kw_only=True" in d for d in cls.decorators)
    kw_from_here = False
    members = sorted((graph.nodes[e.dst] for e in graph.out_edges(cls.id)
                      if e.kind is EdgeKind.CONTAINS and e.dst in graph.nodes),
                     key=lambda m: m.lineno)
    for m in members:
        ann = str(m.meta.get("annotation") or "")
        if m.kind is not NodeKind.CLASS_ATTR or not ann or "ClassVar" in ann:
            continue
        if ann.rsplit(".", 1)[-1] == "KW_ONLY":
            kw_from_here = True
            continue
        value = str(m.meta.get("value") or "")
        if value.startswith(("field(", "dataclasses.field(")) and "init=False" in value:
            continue
        has_default = bool(value) and not (
            value.startswith(("field(", "dataclasses.field(")) and "default" not in value)
        fields = [f for f in fields if f[0] != m.name]
        fields.append((m.name, has_default, all_kw or kw_from_here))
    positional = [f for f in fields if not f[2]]
    kwonly = [f for f in fields if f[2]]
    return {
        "positional": [f[0] for f in positional],
        "required_positional": sum(1 for f in positional if not f[1]),
        "keyword_only": [f[0] for f in kwonly],
        "required_keyword_only": sorted(f[0] for f in kwonly if not f[1]),
        "star_args": False,
        "star_kwargs": False,
        "defaults": [],
    }


def _synthesized_constructor_changes(old: Graph, new: Graph) -> list[Change]:
    """A dataclass gaining a required field breaks every `Cls(...)` like a signature change.

    There is no ``__init__`` node to diff -- the constructor is generated from the
    fields -- so adding a required field used to show up only as "class shell
    changed", with no break and no call sites.
    """
    out: list[Change] = []
    for nid in sorted(set(old.nodes) & set(new.nodes)):
        o, n = old.nodes[nid], new.nodes[nid]
        if n.kind is not NodeKind.CLASS:
            continue
        before, after = record_arity(old, o), record_arity(new, n)
        if before is None or after is None or before == after:
            continue
        specs = arity_breaks(before, after)
        if not specs:
            continue
        sig = lambda a: ", ".join(a["positional"] + [f"*, {k}" for k in a["keyword_only"]])
        out.append(_mk(
            n, ChangeKind.SIGNATURE_CHANGED,
            f"generated __init__({sig(before)})  ->  __init__({sig(after)})",
            breaks=[b["text"] for b in specs], specs=specs,
            before={"arity": before}, after={"arity": after},
        ))
    return out


# --------------------------------------------------------------------------
# pairing renames and moves
# --------------------------------------------------------------------------
_PLUMBING = frozenset({NodeKind.EXTERNAL, NodeKind.PACKAGE, NodeKind.IMPORT_ALIAS,
                       NodeKind.LOCAL, NodeKind.PARAMETER})
_DATA = frozenset({NodeKind.GLOBAL, NodeKind.CLASS_ATTR, NodeKind.INSTANCE_ATTR})
_TRIVIAL_BODIES = (
    "pass", "...", "raise NotImplementedError", "raise NotImplementedError()",
    "return", "return None", '"""doc"""', "return self", "return True",
    "return False", "return 0", "return []", "return {}",
)


@lru_cache(maxsize=1)
def _trivial_body_hashes() -> frozenset[str]:
    """Bodies too generic to say two definitions are the same thing."""
    return frozenset(body_hash(ast.parse(f"def f():\n    {b}\n").body[0])
                     for b in _TRIVIAL_BODIES)


def pair_moves(old: Graph, new: Graph, removed: set[str],
               added: set[str]) -> list[tuple[str, str]]:
    """Pair removed and added nodes that are the same definition under a new id.

    Identical bodies are evidence of a move or rename only when the pairing is
    unambiguous. Several things used to go wrong here: candidates came from set
    iteration, so the result depended on PYTHONHASHSEED; trivial bodies (``pass``,
    ``raise NotImplementedError``) paired unrelated stubs; and two globals that
    happened to hold the same value (``DEBUG = False``, ``VERBOSE = False``) became
    a "rename". Now: same-name pairs (moves) first, then a body shared by exactly
    one removed and one added node (a rename) -- never for data, whose value says
    nothing about identity -- and never on a trivial body.
    """
    trivial = _trivial_body_hashes()

    # A definition some frontends find by link-level name, not by file (a C extern
    # function is reached by its symbol from anywhere), carries meta["symbol"]. One
    # removed and one added node with the same kind and symbol are that definition
    # moved to another file, even when it was edited on the way -- its callers never
    # named the file, so nothing referring to it broke.
    pairs: list[tuple[str, str]] = []
    by_sym: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    for side, graph, ids in ((0, old, removed), (1, new, added)):
        for nid in sorted(ids):
            sym = graph.nodes[nid].meta.get("symbol")
            if sym:
                by_sym.setdefault((graph.nodes[nid].kind.value, sym), ([], []))[side].append(nid)
    for k in sorted(by_sym):
        rs, ads = by_sym[k]
        if len(rs) == 1 and len(ads) == 1:
            pairs.append((rs[0], ads[0]))
    if pairs:
        removed = removed - {p[0] for p in pairs}
        added = added - {p[1] for p in pairs}

    def key(n: Node) -> tuple[str, str] | None:
        if n.kind in _PLUMBING or not n.body_hash or n.body_hash in trivial:
            return None
        return (n.kind.value, n.body_hash)

    rem: dict[tuple[str, str], list[str]] = {}
    add: dict[tuple[str, str], list[str]] = {}
    for nid in sorted(removed):
        k = key(old.nodes[nid])
        if k:
            rem.setdefault(k, []).append(nid)
    for nid in sorted(added):
        k = key(new.nodes[nid])
        if k:
            add.setdefault(k, []).append(nid)

    for k in sorted(rem):
        rs, ads = list(rem[k]), list(add.get(k, []))
        if not ads:
            continue
        for r in list(rs):
            name = old.nodes[r].name
            same_r = [x for x in rs if old.nodes[x].name == name]
            same_a = [a for a in ads if new.nodes[a].name == name]
            if len(same_r) == 1 and len(same_a) == 1:
                pairs.append((r, same_a[0]))
                rs.remove(r)
                ads.remove(same_a[0])
        if len(rs) == 1 and len(ads) == 1 and old.nodes[rs[0]].kind not in _DATA:
            pairs.append((rs[0], ads[0]))

    # A moved module is usually an *edited* module too: a package reorganization
    # rewrites its import lines, and imports are part of the module's shell hash. So
    # pair modules by name as well: exactly one module called `collect` disappeared and
    # exactly one appeared. Without this, moving 43 modules read as 43 deletions and 33
    # critical "still referenced" findings.
    paired_old0 = {p[0] for p in pairs}
    paired_new0 = {p[1] for p in pairs}
    leaf = lambda n: n.qualname.rsplit(".", 1)[-1]
    gone = [nid for nid in sorted(removed) if nid not in paired_old0
            and old.nodes[nid].kind is NodeKind.MODULE]
    came = [nid for nid in sorted(added) if nid not in paired_new0
            and new.nodes[nid].kind is NodeKind.MODULE]
    is_pkg = lambda n: n.path.endswith("__init__.py")
    for o in gone:
        # a package's __init__ and a plain module are different things even when they
        # share a name (frontends/typescript.py vs the new typescript/ package)
        same = [a for a in came if leaf(new.nodes[a]) == leaf(old.nodes[o])
                and is_pkg(new.nodes[a]) == is_pkg(old.nodes[o])]
        rivals = [x for x in gone if leaf(old.nodes[x]) == leaf(old.nodes[o])]
        if len(same) == 1 and len(rivals) == 1:
            pairs.append((o, same[0]))
            came.remove(same[0])

    # When a module or class moves, its members move with it -- by name, whatever
    # their bodies. Without this, a moved class whose methods are `pass` stubs had
    # its methods reported as deleted (and "still referenced", critical).
    paired_old = {p[0] for p in pairs}
    paired_new = {p[1] for p in pairs}
    queue = [p for p in pairs if old.nodes[p[0]].kind in (NodeKind.MODULE, NodeKind.CLASS)]
    while queue:
        o_id, n_id = queue.pop()
        o_q, n_q = old.nodes[o_id].qualname, new.nodes[n_id].qualname
        for e in old.out_edges(o_id):
            if e.kind is not EdgeKind.CONTAINS or e.dst not in removed or e.dst in paired_old:
                continue
            child = old.nodes[e.dst]
            if child.kind in _PLUMBING:
                continue
            twin = child.id.replace(f":{o_q}.", f":{n_q}.", 1)
            if twin in added and twin not in paired_new \
                    and new.nodes[twin].kind is child.kind:
                pairs.append((child.id, twin))
                paired_old.add(child.id)
                paired_new.add(twin)
                if child.kind is NodeKind.CLASS:
                    queue.append((child.id, twin))
    return pairs


# --------------------------------------------------------------------------
# does a reference to a removed or moved name survive the change?
# --------------------------------------------------------------------------
def _unchanged(o: Node, n: Node) -> bool:
    return (o.sig_hash, o.body_hash, o.meta.get("target")) == \
        (n.sig_hash, n.body_hash, n.meta.get("target"))


def surviving_references(
    old: Graph,
    new: Graph,
    old_id: str,
    new_id: str | None = None,
    source_root: str | None = None,
    overlay: dict[str, str] | None = None,
) -> list[Edge]:
    """Old references to ``old_id`` that still stand after the change.

    An old edge only proves the referring code *used to* name the target. It still
    does if the referrer is unchanged -- or, when source is available, if its new
    text still names the target. It does not if the referrer was deleted, or now
    points at ``new_id`` (where a rename or move went). Counting every old edge
    whose source still existed made every clean deletion critical: the caller that
    was edited to stop calling ``helper`` still "referenced" it.

    Without source this is exactly "references in code that was not edited".
    """
    target = old.nodes.get(old_id)
    if target is None:
        return []
    sym = target.meta.get("symbol")
    if sym and any(sym in (n.meta.get("abi_exports") or ()) for n in new.nodes.values()):
        # Referrers bind by link-level symbol, and another definition still exports it
        # (a weak default removed while each program defines its own): nothing dangles.
        return []
    trees: dict[str, ast.Module | None] = {}
    out: list[Edge] = []
    for e in old.in_edges(old_id):
        if e.kind not in REFERENCING_KINDS or e.src == target.parent:
            continue
        if e.meta.get("implicit"):
            continue                # control that runs on by position (COBOL fall-through)
        if e.meta.get("indirect"):
            continue    # reached through a pointer (meta["indirect"]): the code never names it
        if e.meta.get("dispatch"):
            continue    # virtual dispatch (Java): the call names the supertype's method
        # A local or parameter has no hashes of its own, so "unchanged" was trivially
        # true for `fs_enc = get_filesystem_encoding()` even after the function holding
        # it was rewritten. Judge it by the function that owns it.
        judge = _owner(old, e.src)
        if judge is None or judge.id == target.parent:
            continue
        o, n = judge, new.nodes.get(judge.id)
        if n is None:
            continue
        if new_id and any(x.dst == new_id and x.kind in REFERENCING_KINDS
                          for x in new.out_edges(e.src) + new.out_edges(judge.id)):
            continue
        if _rebound(old, new, target, {e.src, judge.id}) \
                or (not target.meta.get("overload_key")
                    and _rebound_to_sibling(old, new, e, target)) \
                or _language_rebound(old, new, e, target):
            continue
        if _unchanged(o, n):
            out.append(e)
        elif target.name in (n.meta.get("local_names") or ()):
            # The edited referrer now declares that name itself (a parameter or local
            # replacing a removed global): spelling it names the local, not the target.
            # Frontends that know scopes set meta["local_names"]; the text check cannot.
            continue
        elif source_root is not None and _still_names(n, target, source_root,
                                                      overlay, trees):
            out.append(e)
    return out


def _rebound(old: Graph, new: Graph, target: Node, srcs: set[str]) -> bool:
    """The referrer now binds every such call to another overload of the same name.

    Frontends for languages with overloading (C++, Java) put the parameter types in the id
    and a shared ``meta["overload_key"]`` on every overload. Retyping ``f(int)`` to
    ``f(long)`` removes one id and adds another; a caller the compiler now resolves to
    ``f(long)`` does not reference the removed one.

    Resolved calls are counted per overload set. Candidate calls (``dynamic``: a template's
    dependent call, a call that did not compile) are counted per argument count: each
    needs a candidate call in the new graph to a sibling that takes that many arguments,
    so replacing ``visit(f, v)``'s overloads with a variadic ``visit(f, args...)`` rebinds
    them, while adding a parameter the calls do not pass does not. A call that stops
    compiling (``meta["unresolved"]``) counts only where it did not compile before either.
    """
    key = target.meta.get("overload_key")
    if not key:
        return False

    def sibling(g: Graph, dst: str) -> Node | None:
        n = g.nodes.get(dst)
        return n if n is not None and n.meta.get("overload_key") == key else None

    def resolved(g: Graph) -> int:
        return sum(1 for s in srcs for x in g.out_edges(s)
                   if x.kind in REFERENCING_KINDS and not x.dynamic and sibling(g, x.dst))

    def candidate_sites(g: Graph, only: str | None = None) -> dict[tuple, list[Edge]]:
        out: dict[tuple, list[Edge]] = {}
        for s in srcs:
            for x in g.out_edges(s):
                if x.kind is EdgeKind.CALLS and x.dynamic and (x.dst == only if only
                                                               else sibling(g, x.dst)):
                    out.setdefault((x.path, x.lineno, x.col), []).append(x)
        return out

    before = resolved(old)
    old_sites = candidate_sites(old, target.id)
    if not before and not old_sites:
        return False
    if before and resolved(new) < before:
        return False
    need: dict[Any, list[bool]] = {}           # argument count -> was each site unresolved
    for edges in old_sites.values():
        need.setdefault(edges[0].meta.get("args"), []).append(
            any(e.meta.get("unresolved") for e in edges))
    new_sites = list(candidate_sites(new).values())
    for args, unresolved_before in need.items():
        fits = sum(1 for edges in new_sites
                   if any(_accepts(sibling(new, e.dst), args)
                          and (not e.meta.get("unresolved") or any(unresolved_before))
                          for e in edges))
        if fits < len(unresolved_before):
            return False
    return True


def _accepts(fn: Node | None, args: Any) -> bool:
    """Could a call passing ``args`` positional arguments bind to ``fn``?"""
    if fn is None:
        return False
    if not isinstance(args, int):
        return True
    arity = fn.meta.get("arity") or {}
    lo = int(arity.get("required_positional", 0))
    hi = len(arity.get("positional", []))
    return lo <= args and (args <= hi or bool(arity.get("star_args")))


def _names_in_text(node: Node, target: Node, source_root: str,
                   overlay: dict[str, str] | None) -> bool:
    """Non-Python source: is the target's name still spelled inside ``node``'s lines?

    Comments are blanked first, and string literals too unless the target is a module: an
    import or ``#include`` names a file inside a string (``from "./util"``). Parsing a C++
    or TypeScript file with ``ast`` failed, which counted every edited referrer as still
    naming a removed function.
    """
    import re
    text = (overlay or {}).get(node.path)
    if text is None:
        try:
            text = (Path(source_root) / node.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True
    lines = text.split("\n")
    lo = 1 if node.kind is NodeKind.MODULE else max(1, node.lineno)
    hi = len(lines) if node.kind is NodeKind.MODULE else (node.end_lineno or node.lineno)
    chunk = "\n".join(lines[lo - 1:hi])
    keep_strings = target.kind in (NodeKind.MODULE, NodeKind.PACKAGE)
    chunk = re.sub(r"/\*.*?\*/|//[^\n]*|\"(?:\\.|[^\"\\\n])*\"",
                   lambda m: m.group() if keep_strings and m.group()[0] == '"' else " ",
                   chunk, flags=re.S)
    return re.search(r"(?<![\w~])" + re.escape(target.name) + r"(?!\w)", chunk) is not None


def _language_rebound(old: Graph, new: Graph, e: Edge, target: Node) -> bool:
    """A language's own say on whether the referrer now means something else by the name.

    ``semantics.rebound(old, new, edge, target)``: in Fortran ``dpmpar(1)`` reads an array
    as validly as it calls a function, so a function replaced by a same-named module
    variable leaves no dangling reference (minpack 7a414a31). In Python the same
    replacement breaks every call, so the hook is the language's to define.
    """
    lang = target.meta.get("lang")
    if not lang:
        return False
    from magellan_lite.polyglot.analyze.frontends import semantics
    hook = getattr(semantics(lang), "rebound", None) if semantics(lang) is not None else None
    return bool(hook(old, new, e, target)) if hook is not None else False


def _rebound_to_sibling(old: Graph, new: Graph, e: Edge, target: Node) -> bool:
    """Did the referrer's calls move to a same-named method (another overload)?

    With overloads (Java, C++) a method's id carries its parameter types. Removing
    ``parse(int)`` leaves ``parse(1)`` compiling against ``parse(long)`` -- or against an
    override of it in a subclass; the frontend already resolved the call there, so the
    old reference did not survive.

    Siblings in the same class are counted, not looked for: the referrer must now make at
    least as many calls to them as it made to them and to the removed method together. A
    call the frontend can no longer resolve has no edge at all, so a referrer that still
    passes the old arguments falls short, however many other calls to siblings it makes
    (commons-cli: ``setUp`` also called the 4-argument ``addOption``, which hid its stale
    3-argument calls). A same-named method elsewhere (an override) counts only at the same
    site, matched by its offset in the referrer. A tie between overloads the frontend
    could not separate (static, confidence 0.5) counts; a name-only guess or a
    virtual-dispatch edge (``dynamic``) does not.
    """
    if not target.meta.get("lang"):
        return False
    o_ref, n_ref = old.nodes.get(e.src), new.nodes.get(e.src)
    if o_ref is None or n_ref is None:
        return False

    def same_name(n: Node | None) -> bool:
        return n is not None and n.name == target.name and n.kind is target.kind

    def calls(g: Graph, keep) -> int:
        return sum(1 for x in g.out_edges(e.src)
                   if x.kind in REFERENCING_KINDS and not x.dynamic and keep(g.nodes.get(x.dst)))

    def sibling(n: Node | None) -> bool:
        return same_name(n) and n.id != target.id and n.parent == target.parent

    to_target = calls(old, lambda n: n is not None and n.id == target.id)
    if to_target and calls(new, sibling) >= calls(old, sibling) + to_target:
        return True
    line = e.lineno - o_ref.lineno + n_ref.lineno
    for x in new.out_edges(e.src):
        if x.kind not in REFERENCING_KINDS or x.dst == target.id or x.dynamic:
            continue
        other = new.nodes.get(x.dst)
        if same_name(other) and other.parent != target.parent and x.lineno == line:
            return True                 # an override, at the same site
    return False


def _owner(graph: Graph, node_id: str) -> Node | None:
    """The nearest node that has code of its own: a local's or parameter's function."""
    node = graph.nodes.get(node_id)
    seen: set[str] = set()
    while node is not None and node.kind in (NodeKind.LOCAL, NodeKind.PARAMETER) \
            and node.parent and node.id not in seen:
        seen.add(node.id)
        node = graph.nodes.get(node.parent)
    return node


def _still_names(node: Node, target: Node, source_root: str,
                 overlay: dict[str, str] | None, trees: dict) -> bool:
    """Does ``node``'s current source still refer to ``target``?

    For a module the test is its full dotted path: after `pkg.collect` moves to
    `pkg.python.collect`, the importer mentions the word "collect" in the *new* path,
    and matching the leaf name counted that as a surviving reference to the old one.
    """
    name = target.name
    if not node.path:
        return False
    if not node.path.endswith(".py"):
        return _still_names_text(node, target, source_root, overlay)
    if node.path not in trees:
        text = (overlay or {}).get(node.path)
        if text is None:
            try:
                text = (Path(source_root) / node.path).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                text = None
        try:
            trees[node.path] = py2.parse(text) if text is not None else None
        except SyntaxError:
            trees[node.path] = None
    tree = trees[node.path]
    if tree is None:
        return True                     # cannot read it: do not claim it was fixed
    if node.kind is NodeKind.MODULE:
        roots: list[ast.AST] = [
            s for s in tree.body
            if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        lo, hi = 0, 10 ** 9
    else:
        roots = [tree]
        lo, hi = node.lineno, node.end_lineno or node.lineno
    if target.kind in (NodeKind.MODULE, NodeKind.PACKAGE):
        dotted = target.qualname
        hit = lambda mod: bool(mod) and (mod == dotted or mod.startswith(dotted + "."))
        for root in roots:
            for sub in ast.walk(root):
                line = getattr(sub, "lineno", None)
                if line is None or not lo <= line <= hi:
                    continue
                if isinstance(sub, ast.ImportFrom) and (
                        hit(sub.module) or any(hit(f"{sub.module}.{a.name}") for a in sub.names)):
                    return True
                if isinstance(sub, ast.Import) and any(hit(a.name) for a in sub.names):
                    return True
                if isinstance(sub, ast.Attribute):
                    try:
                        if hit(ast.unparse(sub)):
                            return True
                    except Exception:           # pragma: no cover - unparse is total
                        pass
        return False
    for root in roots:
        for sub in ast.walk(root):
            line = getattr(sub, "lineno", None)
            if line is None or not lo <= line <= hi:
                continue
            if isinstance(sub, ast.Name) and sub.id == name \
                    and not isinstance(sub.ctx, ast.Store):
                return True
            if isinstance(sub, ast.Attribute) and sub.attr == name:
                return True
            if isinstance(sub, ast.alias) and name in sub.name.split("."):
                return True
            if isinstance(sub, ast.ImportFrom) and sub.module \
                    and name in sub.module.split("."):
                return True
    return False


def _still_names_text(node: Node, target: Node, source_root: str,
                      overlay: dict[str, str] | None) -> bool:
    """A non-Python referrer: does its current source still spell the target?

    A language that reads its own source decides, through ``semantics.still_names``: COBOL
    and Fortran names are case-insensitive, and Java tells a call from a same-named
    parameter. The hook takes ``(text, name)``, or ``(text, word, target, referrer)`` when
    it needs to know what is being looked for. Any other language gets a word match with
    comments blanked (:func:`_names_in_text`).
    """
    import inspect
    lang = node.meta.get("lang") or target.meta.get("lang")
    hook = None
    if lang:
        from magellan_lite.polyglot.analyze.frontends import semantics
        mod = semantics(lang)
        hook = getattr(mod, "still_names", None) if mod is not None else None
    if hook is None:
        return _names_in_text(node, target, source_root, overlay)
    text = (overlay or {}).get(node.path)
    if text is None:
        try:
            text = (Path(source_root) / node.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True                 # cannot read it: do not claim it was fixed
    if node.kind is not NodeKind.MODULE:
        lines = text.splitlines()
        text = "\n".join(lines[max(0, node.lineno - 1):(node.end_lineno or node.lineno)])
    if len(inspect.signature(hook).parameters) < 4:
        return bool(hook(text, target.name))
    qual = target.qualname.split("@", 1)[-1]           # drop the "<lang>@" marker
    if target.kind in (NodeKind.MODULE, NodeKind.PACKAGE):
        word = qual
    elif target.name.startswith("<"):                  # a constructor: named by its class
        word = qual.split("(", 1)[0].rsplit(".", 2)[-2]
    else:
        word = target.name
    if node.name == word:
        text = text.replace(word, "", 1)                # its own declaration, not a use
    return bool(hook(text, word, target, node))


def _diff_node(o: Node, n: Node, include_cosmetic: bool) -> list[Change]:
    out: list[Change] = []

    if o.sig_hash != n.sig_hash and n.kind.is_callable:
        specs = arity_breaks(o.meta.get("arity", {}), n.meta.get("arity", {}))
        out.append(_mk(
            n, ChangeKind.SIGNATURE_CHANGED,
            f"{o.signature or '?'}  ->  {n.signature or '?'}",
            breaks=[b["text"] for b in specs], specs=specs,
            before={"signature": o.signature, "arity": o.meta.get("arity")},
            after={"signature": n.signature, "arity": n.meta.get("arity")},
        ))
    elif o.sig_hash != n.sig_hash and n.kind is NodeKind.CLASS:
        if o.bases != n.bases:
            out.append(_mk(
                n, ChangeKind.BASES_CHANGED,
                f"bases {o.bases or '[]'} -> {n.bases or '[]'}",
                breaks=["subclasses and isinstance checks inherit this change"],
                before={"bases": o.bases}, after={"bases": n.bases},
            ))
        else:
            out.append(_mk(n, ChangeKind.SIGNATURE_CHANGED,
                           "class declaration changed (keywords or metaclass)"))
    elif o.sig_hash != n.sig_hash and n.kind.is_data:
        out.append(_mk(
            n, ChangeKind.ANNOTATION_CHANGED,
            f"annotation {o.meta.get('annotation')} -> {n.meta.get('annotation')}",
        ))

    if o.decorators != n.decorators:
        out.append(_mk(
            n, ChangeKind.DECORATORS_CHANGED,
            f"{o.decorators or '[]'} -> {n.decorators or '[]'}",
            breaks=["decorators change behaviour with no visible call-site edit"],
            before={"decorators": o.decorators}, after={"decorators": n.decorators},
        ))

    if o.body_hash != n.body_hash:
        if n.kind.is_data:
            first_same = o.meta.get("value") == n.meta.get("value")
            detail = (f"one of its {n.meta.get('redefined', 0) + 1} assignments changed"
                      if first_same and n.meta.get("redefined") else
                      f"{o.meta.get('value', '?')}  ->  {n.meta.get('value', '?')}")
            out.append(_mk(
                n, ChangeKind.VALUE_CHANGED, detail,
                before={"value": o.meta.get("value")}, after={"value": n.meta.get("value")},
            ))
        elif not any(c.kind is ChangeKind.SIGNATURE_CHANGED for c in out):
            what = "shell" if n.kind.is_container else "body"
            out.append(_mk(n, ChangeKind.BODY_CHANGED, f"{n.kind.value} {what} changed"))
        else:
            out.append(_mk(n, ChangeKind.BODY_CHANGED, "body changed"))

    if o.doc_hash != n.doc_hash and (include_cosmetic or not out):
        out.append(_mk(n, ChangeKind.DOC_CHANGED, "docstring changed"))

    return out
