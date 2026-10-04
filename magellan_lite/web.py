"""What the website draws for one change: the report, plus the map it was worked out on.

The local server's ``/api/check``, the website's "Try it" box (which runs this module in the
visitor's browser, through Pyodide) and ``demo/build_site.py`` all call ``check_with_map``, so
the page draws the same thing whichever of them answered.
"""

from __future__ import annotations

from pathlib import Path

from magellan_lite.defs import Definition, definitions
from magellan_lite.engine import check_snapshots
from magellan_lite.graph import build_graph
from magellan_lite.source import Snapshot, module_name

#: past this many definitions the map keeps only the change's neighbourhood
MAX_NODES = 60


def check_with_map(before: dict[str, str], after: dict[str, str]) -> dict:
    """``Report.to_dict()`` for two versions given as ``{path: source}``, plus ``map``."""
    b, a = Snapshot(dict(before), "before"), Snapshot(dict(after), "after")
    report = check_snapshots(b, a, Path("."), "request").to_dict()
    report["map"] = code_map(b, a, report)
    return report


def code_map(before: Snapshot, after: Snapshot, report: dict) -> dict:
    """The definitions of the new version (and the ones the change deleted) and who calls or
    reads whom, each marked with what the change did to it: ``{"nodes": [...], "edges": [...]}``.
    """
    before_defs, after_defs = definitions(before), definitions(after)
    graph = build_graph(after, after_defs, before_defs)

    changed = {c["name"]: c["kind"] for c in report["changes"]}
    affected = {a["name"]: a for a in report["affected"]}
    defs: dict[str, tuple[Definition, bool]] = {n: (d, False) for n, d in after_defs.items()}
    defs.update({n: (d, True) for n, d in before_defs.items() if n not in after_defs})

    edges, seen = [], set()
    for e in graph.edges:
        key = (e.src, e.dst, e.kind)
        if e.src in defs and e.dst in defs and e.src != e.dst and key not in seen:
            seen.add(key)
            edges.append({"src": e.src, "dst": e.dst, "kind": e.kind, "guess": e.guess})

    keep = set(defs)
    if len(keep) > MAX_NODES:                   # the change, what it reaches, their neighbours
        core = set(changed) | set(affected) | {n for n, (d, _) in defs.items()
                                              if _has_finding(d, report["findings"])}
        near = {e["src"] for e in edges if e["dst"] in core} | \
               {e["dst"] for e in edges if e["src"] in core}
        keep = set(sorted(core)[:MAX_NODES])
        keep |= set(sorted(near - keep)[:max(0, MAX_NODES - len(keep))])
        edges = [e for e in edges if e["src"] in keep and e["dst"] in keep]

    nodes = []
    for name in sorted(keep):
        d, removed = defs[name]
        mod = module_name(d.path)
        hit = affected.get(name)
        nodes.append({
            "id": name,
            "label": name[len(mod) + 1:] if name.startswith(mod + ".") else name,
            "path": d.path, "line": d.line, "kind": d.kind,
            "change": changed.get(name, ""),
            "removed": removed,
            "score": hit["score"] if hit else 0,
            "hops": hit["hops"] if hit else 0,
            "finding": not removed and _has_finding(d, report["findings"]),
        })
    return {"nodes": nodes, "edges": edges}


def _has_finding(d: Definition, findings: list[dict]) -> bool:
    return any(f["path"] == d.path and d.line <= f["line"] <= d.end_line and d.kind != "class"
               for f in findings)
