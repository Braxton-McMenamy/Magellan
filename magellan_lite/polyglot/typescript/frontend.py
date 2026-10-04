"""TypeScript / JavaScript frontend.

The heavy lifting is done by the TypeScript compiler itself: ``ts_extract.js``
parses every ``.ts/.tsx/.js/.jsx`` file, runs the type checker to resolve each
identifier to its declaration (through imports, re-exports and aliases), and
prints declarations and references as JSON. This module turns that into the same
``Node`` and ``Edge`` types the Python frontend produces, so change detection,
impact propagation, dead-code and risk rules apply unchanged.

Nothing is executed and nothing is emitted. Node.js and the ``typescript``
package are needed at analysis time (found via ``require``, ``MAGELLAN_TS_PATH``
or the global npm root); when they are missing the frontend is skipped and the
graph is whatever the other frontends produced.

Confidence follows the checker: a call it resolves to a project declaration is
1.0. A call through an interface or abstract method is linked to every
implementation at 0.6, since only the runtime knows which one runs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from magellan_lite.polyglot.python.project import read_source
from magellan_lite.polyglot.python.state import AnalysisState
from magellan_lite.polyglot.core.hashing import file_hash
from magellan_lite.polyglot.core.model import Edge, EdgeKind, FileRecord, Graph, Node, NodeKind
from magellan_lite.polyglot.core.taxonomy import Signals, is_mutable_container_expr

HELPER = Path(__file__).with_name("ts_extract.js")
CONFIG_FILES = ("tsconfig.json", "jsconfig.json", "package.json")
SOURCE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
SKIP_DIRS = {"node_modules", "dist", "build", "out", "coverage", ".git", ".magellan",
             ".next", ".turbo", "target", "__pycache__", "venv", ".venv"}

_KINDS = {
    "module": NodeKind.MODULE, "class": NodeKind.CLASS, "function": NodeKind.FUNCTION,
    "method": NodeKind.METHOD, "global_var": NodeKind.GLOBAL,
    "class_attr": NodeKind.CLASS_ATTR, "instance_attr": NodeKind.INSTANCE_ATTR,
}
_EDGES = {
    "calls": EdgeKind.CALLS, "instantiates": EdgeKind.INSTANTIATES,
    "reads": EdgeKind.READS, "writes": EdgeKind.WRITES, "mutates": EdgeKind.MUTATES,
    "inherits": EdgeKind.INHERITS, "decorates": EdgeKind.DECORATES,
    "raises": EdgeKind.RAISES, "imports": EdgeKind.IMPORTS,
    "reexports": EdgeKind.REEXPORTS,
}
_IO_ROOTS = {"fs", "http", "https", "net", "child_process", "dgram", "tls", "zlib", "stream"}


# --------------------------------------------------------------------------
def has_sources(root: str | Path) -> bool:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(SOURCE_SUFFIXES) and not fn.endswith((".d.ts", ".d.mts", ".d.cts")):
                return True
    return False


def _env() -> dict[str, str]:
    env = dict(os.environ)
    if "MAGELLAN_TS_PATH" not in env:
        try:
            root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=20).stdout.strip()
            if root:
                env["MAGELLAN_TS_PATH"] = root
        except (OSError, subprocess.SubprocessError):
            pass
    return env


def available() -> bool:
    if shutil.which("node") is None:
        return False
    try:
        r = subprocess.run(["node", str(HELPER), ".", "--check"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30, env=_env())
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def extract(root: str | Path, timeout: int = 600) -> dict | None:
    """Run the extractor; ``None`` when Node or TypeScript is unavailable."""
    if shutil.which("node") is None:
        return None
    r = subprocess.run(["node", "--max-old-space-size=4096", str(HELPER), str(root)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, env=_env())
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
def _arity(sig: dict) -> dict:
    params = [p for p in sig.get("params", []) if not p["rest"]]
    required = 0
    for i, p in enumerate(params):
        if not p["optional"] and p["default"] is None:
            required = i + 1
    return {
        "positional": [p["name"] for p in params],
        "required_positional": required,
        "keyword_only": [],
        "required_keyword_only": [],
        "star_args": any(p["rest"] for p in sig.get("params", [])),
        "star_kwargs": False,
        "defaults": [p["default"] if p["default"] is not None else "?"
                     for p in params[required:]],
    }


def merge(state: AnalysisState, root: str | Path) -> set[str]:
    """Add the TypeScript/JavaScript files under ``root`` to the graph.

    Returns the ids of the nodes it added (empty when there was nothing to do
    or the toolchain is unavailable).
    """
    graph = state.graph
    if not has_sources(root):
        return set()
    data = extract(root)
    if data is None:
        graph.diagnostics.append(
            "TypeScript/JavaScript sources found but Node.js with the `typescript` "
            "package is unavailable; skipped (set MAGELLAN_TS_PATH or install it)")
        return set()

    added: set[str] = set()
    module_of_path: dict[str, str] = {}
    roots_of_module: dict[str, set[str]] = {}
    exits: set[str] = set()
    for f in data["files"]:
        src = read_source(Path(root) / f["path"])
        graph.files[f["path"]] = FileRecord(
            path=f["path"], module=f["module"], sha256=file_hash(src),
            lines=f["lines"], source_root="")
        module_of_path[f["path"]] = f["module"]
        roots_of_module[f["module"]] = set(f.get("importedRoots", ()))
        for holder, flags in f.get("flags", ()):
            if flags.get("exits"):
                exits.add(holder)

    decl_by_id: dict[str, dict] = {}
    for d in data["decls"]:
        kind = _KINDS[d["kind"]]
        module = module_of_path.get(d["path"], "")
        meta: dict = {"lang": "typescript"}
        if kind.is_callable:
            meta["arity"] = _arity(d["sig"])
            meta["async"] = bool(d.get("async"))
            meta["awaits"] = bool(d.get("async"))
        if kind in (NodeKind.GLOBAL, NodeKind.CLASS_ATTR, NodeKind.INSTANCE_ATTR):
            meta["annotation"] = d.get("annotation", "")
            meta["value"] = d.get("value", "")
        node = Node(
            id=d["id"], kind=kind, name=d["name"], qualname=d["qualname"], module=module,
            path=d["path"], lineno=d["line"], end_lineno=d["endLine"], parent=d.get("parent"),
            signature=d.get("signature"), bases=list(d.get("bases", [])),
            decorators=list(d.get("decorators", [])), public=bool(d.get("exported", True)),
            tags=sorted(set(d.get("tags", []) + (["async"] if d.get("async") else []))),
            sig_hash=d.get("sigHash", ""), body_hash=d.get("bodyHash", ""),
            doc_hash=d.get("docHash", ""), meta=meta,
        )
        if graph.add_node(node) is node:
            added.add(node.id)
            decl_by_id[node.id] = d
        if node.parent and node.parent in graph.nodes:
            graph.add_edge(Edge(src=node.parent, dst=node.id, kind=EdgeKind.CONTAINS,
                                lineno=node.lineno, path=node.path))

    def external(ext_name: str) -> str:
        eid = f"ext:{ext_name}"
        if eid not in graph.nodes:
            is_env = ext_name.startswith("env.")
            graph.add_node(Node(id=eid, kind=NodeKind.EXTERNAL, name=ext_name.split(".")[-1],
                                qualname=ext_name, module="env" if is_env else "",
                                meta={"env": True, "distribution": "env"} if is_env
                                else {"lang": "typescript"}))
            added.add(eid)
        return eid

    for r in data["refs"]:
        kind = _EDGES.get(r["kind"])
        if kind is None:
            continue
        dst = r["dst"]
        if dst.startswith("ext:"):
            dst = external(r.get("ext") or dst[4:])
        if r["src"] not in graph.nodes or dst not in graph.nodes:
            continue
        meta = {k: r[k] for k in ("callee", "args", "method", "grows", "op", "in_place",
                                  "env_var", "default", "required") if k in r}
        src_node = graph.nodes[r["src"]]
        graph.add_edge(Edge(
            src=r["src"], dst=dst, kind=kind, lineno=r["line"], path=r["path"],
            confidence=float(r.get("confidence", 1.0)),
            conditional=bool(r.get("conditional")), dynamic=False,
            context=src_node.qualname, meta=meta))

    _link_implementations(graph, added)
    _signals(state, graph, added, decl_by_id, roots_of_module, exits)
    graph.diagnostics.append(
        f"typescript {data.get('tsVersion', '?')}: {len(data['files'])} files, "
        f"{len(data['decls'])} declarations")
    return added


def _link_implementations(graph: Graph, added: set[str]) -> None:
    """Calls typed against an interface or abstract method may run any implementation."""
    children: dict[str, list[str]] = {}
    for e in graph.edges:
        if e.kind is EdgeKind.INHERITS and e.src in added and e.dst in added:
            children.setdefault(e.dst, []).append(e.src)

    def descendants(cid: str, seen: set[str] | None = None) -> list[str]:
        seen = seen if seen is not None else set()
        out: list[str] = []
        for c in children.get(cid, ()):
            if c not in seen:
                seen.add(c)
                out.append(c)
                out.extend(descendants(c, seen))
        return out

    def method_of(cls_id: str, name: str) -> Node | None:
        for e in graph.out_edges(cls_id):
            if e.kind is EdgeKind.CONTAINS:
                n = graph.nodes.get(e.dst)
                if n is not None and n.kind is NodeKind.METHOD and n.name == name:
                    return n
        return None

    impls: dict[str, list[Node]] = {}
    for nid in list(added):
        n = graph.nodes[nid]
        if n.kind is not NodeKind.METHOD or not n.parent:
            continue
        parent = graph.nodes.get(n.parent)
        if parent is None:
            continue
        found = [m for d in descendants(parent.id)
                 if (m := method_of(d, n.name)) is not None]
        if found:
            impls[nid] = found
            for m in found:
                graph.add_edge(Edge(src=m.id, dst=nid, kind=EdgeKind.OVERRIDES,
                                    lineno=m.lineno, path=m.path))

    for e in list(graph.edges):
        if e.kind is EdgeKind.CALLS and e.dst in impls:
            for m in impls[e.dst]:
                graph.add_edge(Edge(
                    src=e.src, dst=m.id, kind=EdgeKind.CALLS, lineno=e.lineno, path=e.path,
                    confidence=min(e.confidence, 0.6), conditional=e.conditional,
                    dynamic=True, context=e.context, meta=dict(e.meta)))


def _signals(state: AnalysisState, graph: Graph, added: set[str],
             decls: dict[str, dict], roots_of_module: dict[str, set[str]],
             exits: set[str]) -> None:
    out_kinds: dict[str, list[Edge]] = {}
    for e in graph.edges:
        if e.src in added:
            out_kinds.setdefault(e.src, []).append(e)
    state_kinds = (NodeKind.GLOBAL, NodeKind.CLASS_ATTR, NodeKind.INSTANCE_ATTR)

    for nid in added:
        node = graph.nodes[nid]
        d = decls.get(nid, {})
        edges = out_kinds.get(nid, [])
        module_parts = tuple(node.module.split(".")) if node.module else ()
        path_parts = tuple(node.path.split("/")) if node.path else ()
        if node.path and (".test." in node.path or ".spec." in node.path
                          or "__tests__" in node.path):
            path_parts += ("tests",)
        roots = frozenset(roots_of_module.get(node.module, ()))
        if node.kind is NodeKind.MODULE:
            sig = state.sig(nid)
            sig.module_parts, sig.path_parts = module_parts, path_parts
            sig.imported_roots = roots
            sig.own_statement_count = int(d.get("statements", 0))
            sig.decorators = tuple(
                dec for cid in added
                if graph.nodes[cid].parent == nid and graph.nodes[cid].kind is NodeKind.CLASS
                for dec in graph.nodes[cid].decorators)
        elif node.kind.is_callable:
            sig = state.sig(nid)
            sig.module_parts, sig.path_parts, sig.imported_roots = module_parts, path_parts, roots
            sig.decorators = tuple(node.decorators)
            sig.returns_value = bool(d.get("returnsValue"))
            sig.has_params = bool(d.get("hasParams"))
            sig.own_statement_count = int(d.get("statements", 0))
            sig.call_count = sum(1 for e in edges if e.kind is EdgeKind.CALLS)
            sig.calls_exit = nid in exits
            sig.calls_open = any(
                e.kind is EdgeKind.CALLS and graph.nodes.get(e.dst) is not None
                and graph.nodes[e.dst].kind is NodeKind.EXTERNAL
                and graph.nodes[e.dst].qualname.split(".")[0] in _IO_ROOTS for e in edges)
            for e in edges:
                tgt = graph.nodes.get(e.dst)
                if tgt is None or tgt.kind not in state_kinds:
                    continue
                if e.kind in (EdgeKind.WRITES, EdgeKind.MUTATES):
                    sig.mutates_state = True
                    if tgt.kind is NodeKind.GLOBAL:
                        sig.writes_global = True
                elif e.kind is EdgeKind.READS and tgt.kind is NodeKind.GLOBAL:
                    sig.reads_global = True
        elif node.kind in state_kinds:
            sig = state.sig(nid)
            value = str(node.meta.get("value", ""))
            container = is_mutable_container_expr(value) or value.startswith(
                ("[", "{", "new Map", "new Set", "new Array", "new WeakMap"))
            sig.is_mutable_container = bool(container)
            sig.is_constant = bool(d.get("isConst")) and not container
