"""The blast radius: code the change did not touch that it can still break, ranked.

Impact travels *against* the call graph. If ``parse_record`` changes, the code that calls it
is at risk; then whatever calls that code, and so on. Each change starts with a weight for how
much it can do to the code that depends on it, and every hop multiplies the weight down, so a
chain of direct calls stays hot while a long or uncertain chain fades out:

    deleted or renamed 1.0   signature 0.95   value 0.8   body 0.6     (the seed)
    a call 0.9   reading a constant 0.85   a guessed call x0.5         (each hop)

A definition is listed with the best score any path gives it; propagation stops below
``THRESHOLD`` or after ``MAX_HOPS``. Definitions the change edited are not listed: they are
already in the change.
"""

from __future__ import annotations

from magellan_lite.defs import Definition
from magellan_lite.diff import Change
from magellan_lite.graph import CallGraph, Edge

SEED = {"removed": 1.0, "renamed": 1.0, "signature": 0.95, "value": 0.8, "body": 0.6}
STEP = {"calls": 0.9, "reads": 0.85}
GUESS = 0.5
THRESHOLD = 0.1
MAX_HOPS = 6


def blast_radius(changes: list[Change], graph: CallGraph,
                 defs: dict[str, Definition]) -> list[dict]:
    """The affected definitions, worst first: ``{name, path, line, hops, score, why}``."""
    changed = {c.name for c in changes} | {c.before.name for c in changes if c.before}
    frontier: list[tuple[float, int, str]] = []
    for c in changes:
        seed = SEED.get(c.kind)
        if seed is None:
            continue
        frontier.append((seed, 0, c.name))
        if c.kind == "renamed":                     # callers of the old name are hit too
            frontier.append((seed, 0, c.before.name))

    best: dict[str, tuple[float, int, Edge]] = {}
    while frontier:
        score, hops, name = frontier.pop()
        if hops >= MAX_HOPS:
            continue
        for e in graph.callers(name):
            s = score * STEP[e.kind] * (GUESS if e.guess else 1.0)
            if s < THRESHOLD or e.src in changed:
                continue
            if e.src not in best or s > best[e.src][0] + 1e-9:
                best[e.src] = (s, hops + 1, e)
                frontier.append((s, hops + 1, e.src))

    out = []
    for name, (score, hops, e) in best.items():
        d = defs.get(name)
        out.append({
            "name": name, "path": d.path if d else e.path, "line": d.line if d else e.line,
            "hops": hops, "score": round(score, 2),
            "why": (f"{e.src} {e.kind} {e.dst} ({e.path}:{e.line})"
                    + (" -- a guess" if e.guess else "")),
        })
    out.sort(key=lambda a: (-a["score"], a["hops"], a["name"]))
    return out
