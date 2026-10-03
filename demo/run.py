"""Replay famous failures through Magellan Lite: the demo, and the team's scoreboard.

    python demo/run.py            # one line per change
    python demo/run.py -v         # with each finding

Each folder in demo/incidents/ rebuilds a real incident as a short git history (see its
incident.json). For every change, the runner asks what a pre-commit check would have said,
and marks each expected rule:

    caught    the rule fired
    waiting   nobody has written that rule yet -- see TODO(checklist) in magellan_lite/rules
    MISSED    the rule exists but stayed quiet: a bug to fix

TODO(site): show these results on the website (site/, Braxton's). See main() below.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from magellan_lite.engine import check  # noqa: E402
from magellan_lite.findings import RULES  # noqa: E402
import magellan_lite.rules  # noqa: E402,F401


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _rmtree(path: Path) -> None:
    def force(func, p, *_):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=force)
    else:
        shutil.rmtree(path, onerror=force)


def _place(step: Path, repo: Path) -> None:
    for p in repo.iterdir():
        if p.name != ".git":
            _rmtree(p) if p.is_dir() else p.unlink()
    shutil.copytree(step, repo, dirs_exist_ok=True)


def replay(incident: Path) -> dict:
    spec = json.loads((incident / "incident.json").read_text(encoding="utf-8"))
    repo = Path(tempfile.mkdtemp(prefix=f"magellan-lite-{incident.name}-"))
    steps = []
    try:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.name", "replay")
        _git(repo, "config", "user.email", "replay@example.invalid")
        _git(repo, "config", "core.autocrlf", "false")
        for i, step in enumerate(spec["steps"]):
            _place(incident / "steps" / step["dir"], repo)
            if i:
                report = check(repo, "git:HEAD")
                found = sorted({f.rule for f in report.findings})
                expect = step.get("expect", [])
                rules = {r: ("caught" if r in found else "MISSED" if r in RULES else "waiting")
                         for r in expect}
                unwanted = [r for r in step.get("expect_not", []) if r in found]
                status = ("MISSED" if "MISSED" in rules.values() or unwanted
                          else "waiting" if "waiting" in rules.values()
                          else "caught" if expect
                          else "known miss" if step.get("known_miss") else "quiet")
                steps.append({"dir": step["dir"], "what": step.get("what", ""),
                              "verdict": report.verdict, "status": status, "rules": rules,
                              "unwanted": unwanted, "known_miss": step.get("known_miss", ""),
                              "findings": [f.to_dict() for f in report.findings]})
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "--allow-empty", "-m", step["dir"])
    finally:
        _rmtree(repo)
    return {"name": incident.name, "title": spec.get("title", incident.name),
            "what_happened": spec.get("what_happened", ""), "sources": spec.get("sources", []),
            "language": spec.get("language", ""), "steps": steps}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*", help="incident folders (default: all)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    folders = sorted(p for p in (HERE / "incidents").iterdir() if (p / "incident.json").is_file())
    results = [replay(p) for p in folders if not args.names or p.name in args.names]

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

    # TODO(site): hand these results to the website. `results` is a list with one dict per
    #   incident: name, title, what_happened, sources, language, and steps (each with what,
    #   verdict, status, rules, findings). Agree the format with Braxton, then write it here,
    #   e.g. json.dump(results, ...) to a file the page reads. Opening index.html straight from
    #   disk can't fetch() a JSON file; a .js file that sets a global (`window.X = [...]`) can
    #   be loaded with <script>, or serve the folder with
    #   `python -m http.server 8000 --directory site`.
    return 1 if tally.get("MISSED") else 0


if __name__ == "__main__":
    sys.exit(main())
