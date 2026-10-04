"""What the website draws for one change: the report, plus the map it was worked out on.

The local server's ``/api/check``, the website's "Try it" box (which runs this module in the
visitor's browser, through Pyodide) and ``demo/build_site.py`` all call ``check_with_map``, so
the page draws the same thing whichever of them answered. The Team suite calls ``team_live``:
everyone's shared work in progress at once.
"""

from __future__ import annotations

from pathlib import Path

from magellan_lite.combine import _changed, combine, derive
from magellan_lite.defs import Definition, definitions
from magellan_lite.engine import check_snapshots
from magellan_lite.graph import build_graph
from magellan_lite.source import Snapshot, module_name

#: past this many definitions the map keeps only the change's neighbourhood
MAX_NODES = 60
#: the whole project, for the 3D scene: past this many, the best connected definitions
PROJECT_NODES = 1200


def check_with_map(before: dict[str, str], after: dict[str, str]) -> dict:
    """``Report.to_dict()`` for two versions given as ``{path: source}``, plus ``map``."""
    b, a = Snapshot(dict(before), "before"), Snapshot(dict(after), "after")
    report = check_snapshots(b, a, Path("."), "request").to_dict()
    report["map"] = code_map(b, a, report)
    return report


def team_live(base: dict[str, str], works: dict[str, dict[str, str]]) -> dict:
    """Everyone's work in progress, checked alone, two by two, and all together.

    ``base`` is the version everyone builds on (the default branch); ``works`` is each
    person's version of the project, ``{name: {path: source}}``. Returns

    - ``members``: each person's own check, with its map (``name`` plus ``Report.to_dict()``);
    - ``pairs``: for each two people, the problems only their two changes together have;
    - ``team``: everyone's changes applied at once, checked, with a map of the whole project
      (up to ``PROJECT_NODES`` definitions) whose nodes say whose change made them (``by``) and
      whose change reaches them (``reached_by``). A file two people both changed is taken from
      the first of them by name, and listed in ``overlap``. With nobody's work yet, it is the
      project as it is: something to look at from the first moment.
    """
    root = Path(".")
    b = Snapshot(dict(base), "base")
    snaps = {name: derive(b, files, name) for name, files in sorted(works.items())}
    alone = {name: check_snapshots(b, s, root, name) for name, s in snaps.items()}

    members = []
    for name, s in snaps.items():
        r = alone[name].to_dict()
        r["map"] = code_map(b, s, r)
        members.append({"name": name, **r})

    names, pairs = list(snaps), []
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            c = combine(root, b, snaps[x], snaps[y], y, alone=(alone[x], alone[y])).to_dict()
            pairs.append({"a": x, "b": y, "verdict": c["verdict"], "findings": c["findings"],
                          "affected": c["affected"], "overlap": c["overlap"]})

    together, owner, overlap = dict(base), {}, {}
    for name, s in snaps.items():
        for path in sorted(_changed(b.files, s.files)):
            if path in owner:
                overlap.setdefault(path, [owner[path]]).append(name)
                continue
            owner[path] = name
            if path in s.files:
                together[path] = s.files[path]
            else:
                together.pop(path, None)
    t = derive(b, together, "team")
    team = check_snapshots(b, t, root, "team").to_dict()
    team["map"] = code_map(b, t, team, PROJECT_NODES, whole=True)     # the whole project
    changed_by = {n: {c.name for c in alone[n].changes} for n in names}
    reached_by = {n: {a["name"] for a in alone[n].affected} for n in names}
    for node in team["map"]["nodes"]:
        node["by"] = [n for n in names if node["id"] in changed_by[n]]
        node["reached_by"] = [n for n in names if node["id"] in reached_by[n]]
    team["overlap"] = [{"path": p, "names": ns} for p, ns in sorted(overlap.items())]
    return {"members": members, "pairs": pairs, "team": team}


def code_map(before: Snapshot, after: Snapshot, report: dict, limit: int = MAX_NODES,
             whole: bool = False) -> dict:
    """The definitions of the new version (and the ones the change deleted) and who calls or
    reads whom, each marked with what the change did to it: ``{"nodes": [...], "edges": [...]}``.

    Past ``limit`` definitions only the change's neighbourhood is kept -- or, with ``whole``
    (the project map the 3D scene draws), the neighbourhood and then the best connected rest.
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
    if len(keep) > limit:                       # the change, what it reaches, their neighbours
        core = set(changed) | set(affected) | {n for n, (d, _) in defs.items()
                                              if _has_finding(d, report["findings"])}
        near = {e["src"] for e in edges if e["dst"] in core} | \
               {e["dst"] for e in edges if e["src"] in core}
        keep = set(sorted(core)[:limit])
        keep |= set(sorted(near - keep)[:max(0, limit - len(keep))])
        if whole and len(keep) < limit:
            degree: dict[str, int] = {}
            for e in edges:
                degree[e["src"]] = degree.get(e["src"], 0) + 1
                degree[e["dst"]] = degree.get(e["dst"], 0) + 1
            rest = sorted(set(defs) - keep, key=lambda n: (-degree.get(n, 0), n))
            keep |= set(rest[:limit - len(keep)])
        edges = [e for e in edges if e["src"] in keep and e["dst"] in keep]

    nodes = []
    for name in sorted(keep):
        d, removed = defs[name]
        mod = module_name(d.path)
        hit = affected.get(name)
        nodes.append({
            "id": name,
            "label": d.label or (name[len(mod) + 1:] if name.startswith(mod + ".") else name),
            "path": d.path, "line": d.line, "kind": d.kind, "lang": d.lang or "python",
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
