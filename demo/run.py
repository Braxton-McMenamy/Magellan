"""Replay famous failures through Magellan Lite: the demo, and the team's scoreboard.

    python demo/run.py            # one line per change
    python demo/run.py -v         # with each finding

Each folder in demo/incidents/ rebuilds a real incident as a short code history, one folder
per change (see its incident.json). For every change, the runner asks what a pre-commit check
would have said, and marks each expected rule:

    caught    the rule fired
    waiting   nobody has written that rule yet -- see TODO(checklist) in magellan_lite/rules
    MISSED    the rule exists but stayed quiet: a bug to fix

The website shows the same results, as an animated story per incident:
`python demo/build_site.py` writes them to site/data/ (run it after a rule lands).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from magellan_lite.incidents import replay_all  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*", help="incident folders (default: all)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    results = replay_all(HERE / "incidents", args.names)

    tally: dict[str, int] = {}
    for inc in results:
        for s in inc["steps"]:
            tally[s["status"]] = tally.get(s["status"], 0) + 1
            rules = ", ".join(f"{r} ({how})" for r, how in s["rules"].items())
            print(f"{s['status']:<11} {s['verdict']:<7} {inc['name']}/{s['dir']}"
                  + (f"  {rules}" if rules else ""))
            if args.verbose:
                for f in s["findings"]:
                    print(f"              [{f['severity']}] {f['rule']} {f['path']}:{f['line']}"
                          f"  {f['message']}")
    print("\n" + ", ".join(f"{n} {k}" for k, n in sorted(tally.items())))
    return 1 if tally.get("MISSED") else 0


if __name__ == "__main__":
    sys.exit(main())
