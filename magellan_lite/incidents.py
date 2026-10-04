"""Famous failures, replayed change by change: the demo, and the team's scoreboard.

Each folder in demo/incidents/ holds ``incident.json`` (what happened, and what each change
should get flagged for) and ``steps/<n>-<what changed>/`` (the code after each change). Every
step is checked against the step before it -- what a pre-commit check would have said about
that change -- and each expected rule is marked:

    caught    the rule fired
    waiting   nobody has written that rule yet
    MISSED    the rule exists but stayed quiet: a bug to fix

Used by ``python demo/run.py`` and by the local server's ``/api/incidents``.
"""

from __future__ import annotations

import json
from pathlib import Path

from magellan_lite.engine import check
from magellan_lite.findings import RULES

#: demo/incidents/ in a checkout of this repository
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "demo" / "incidents"


def find_incidents(folder: Path = DEFAULT_DIR) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if (p / "incident.json").is_file())


def replay(incident: Path) -> dict:
    """One incident: its story, and for each change the verdict, findings and status."""
    import magellan_lite.rules  # noqa: F401  registers every rule

    spec = json.loads((incident / "incident.json").read_text(encoding="utf-8"))
    dirs = [incident / "steps" / s["dir"] for s in spec["steps"]]
    steps = []
    for step, before, after in zip(spec["steps"][1:], dirs, dirs[1:]):
        report = check(after, against=str(before))
        found = {f.rule for f in report.findings}
        expect = step.get("expect", [])
        rules = {r: "caught" if r in found else "MISSED" if r in RULES else "waiting"
                 for r in expect}
        unwanted = [r for r in step.get("expect_not", []) if r in found]
        status = ("MISSED" if "MISSED" in rules.values() or unwanted
                  else "waiting" if "waiting" in rules.values()
                  else "caught" if expect
                  else "known miss" if step.get("known_miss") else "quiet")
        steps.append({"dir": step["dir"], "what": step.get("what", ""),
                      "verdict": report.verdict, "status": status, "rules": rules,
                      "unwanted": unwanted, "known_miss": step.get("known_miss", ""),
                      "findings": [f.to_dict() for f in report.findings],
                      "changes": [c.to_dict() for c in report.changes],
                      "affected": report.affected[:10]})
    return {"name": incident.name, "title": spec.get("title", incident.name),
            "what_happened": spec.get("what_happened", ""), "sources": spec.get("sources", []),
            "language": spec.get("language", ""), "steps": steps,
            # the website's story: when, what it cost, what the rule looks for
            **{k: spec[k] for k in ("date", "damage", "damage_of", "damage_note", "catch",
                                    "out_of_reach") if k in spec}}


def replay_all(folder: Path = DEFAULT_DIR, names: list[str] | None = None) -> list[dict]:
    return [replay(p) for p in find_incidents(folder) if not names or p.name in names]
