"""Write the website's data from the code. Run it after changing a rule, the engine or an
incident, and commit what it writes:

    python demo/build_site.py            # write site/data/
    python demo/build_site.py --check    # exit 1 if site/data/ is out of date (the tests do this)

site/ is published exactly as it is (the host runs no build step), so everything the page
shows is generated here, by the real engine:

    site/data/incidents.js   the famous failures, replayed: the story player and the scoreboard
    site/data/showcase.js    the hero's terminal output, the checklist, the three "Try it" examples
    site/data/engine.js      Magellan Lite's own source, which "Try it" runs in the browser
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from magellan_lite import __version__  # noqa: E402
from magellan_lite.engine import check_snapshots  # noqa: E402
from magellan_lite.findings import RULES  # noqa: E402
from magellan_lite.incidents import find_incidents, replay  # noqa: E402
from magellan_lite.output import text  # noqa: E402
from magellan_lite.source import Snapshot, working_tree  # noqa: E402
from magellan_lite.web import check_with_map  # noqa: E402

DATA = ROOT / "site" / "data"
INCIDENTS = HERE / "incidents"
#: the hero replays this incident's change, as `magellan-lite check` prints it
HERO = "sensor-signature-break"
#: modules the browser never needs: git, the server, the command line
NOT_IN_BROWSER = {"__main__.py", "cli.py", "git.py", "incidents.py", "server.py", "team.py"}


# -- the famous failures ----------------------------------------------------------------------
def incidents() -> list[dict]:
    """Every incident replayed (the scoreboard), each with the story the player animates."""
    return [{**replay(folder), "story": story(folder)} for folder in find_incidents(INCIDENTS)]


def story(folder: Path) -> dict:
    """The one change the player shows: its code, what it reaches, what was found."""
    spec = json.loads((folder / "incident.json").read_text(encoding="utf-8"))
    steps = spec["steps"]
    dirs = [s["dir"] for s in steps]
    feature = spec.get("feature") or next(
        (s["dir"] for s in reversed(steps) if s.get("expect")), dirs[-1])
    i = dirs.index(feature)
    before = working_tree(folder / "steps" / dirs[i - 1]).files
    after = working_tree(folder / "steps" / feature).files
    report = check_with_map(before, after)
    found = {f["rule"] for f in report["findings"]}
    expect = steps[i].get("expect", [])
    shown = spec.get("show") or _worth_showing(before, after, report)
    return {
        "step": feature, "what": steps[i].get("what", ""), "verdict": report["verdict"],
        "rules": {r: "caught" if r in found else "MISSED" if r in RULES else "waiting"
                  for r in expect},
        "files": [file_view(p, before.get(p), after.get(p), report["findings"]) for p in shown],
        "changes": report["changes"], "findings": report["findings"],
        "affected": report["affected"], "map": report["map"],
    }


def _worth_showing(before: dict, after: dict, report: dict) -> list[str]:
    """Files with findings first (they may be files nobody touched), then the edited ones."""
    out = [f["path"] for f in report["findings"]]
    out += sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    return list(dict.fromkeys(out))[:2]


def file_view(path: str, old: str | None, new: str | None, findings: list[dict]) -> dict:
    """The whole file as a diff: ``[op, line number in the new file or None, text]``."""
    a, b = (old or "").splitlines(), (new or "").splitlines()
    lines: list[list] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            lines += [[" ", j + 1, b[j]] for j in range(j1, j2)]
            continue
        lines += [["-", None, a[i]] for i in range(i1, i2)]
        lines += [["+", j + 1, b[j]] for j in range(j1, j2)]
    state = ("added" if old is None else "deleted" if new is None
             else "untouched" if old == new else "changed")
    return {"path": path, "state": state, "lines": lines,
            "marks": sorted({f["line"] for f in findings if f["path"] == path})}


# -- the hero, the checklist, the examples --------------------------------------------------
def hero() -> dict:
    """The terminal in the hero: the sensor change, checked as `magellan-lite check` would
    check it with the previous step committed."""
    folder = INCIDENTS / HERO
    dirs = [s["dir"] for s in json.loads((folder / "incident.json").read_text("utf-8"))["steps"]]
    before, after = (working_tree(folder / "steps" / d) for d in dirs[-2:])
    report = check_snapshots(Snapshot(before.files), Snapshot(after.files), Path("."), "git:HEAD")
    return {"command": "magellan-lite check", "verdict": report.verdict,
            "output": text(report)}


def rules() -> list[dict]:
    import magellan_lite.rules  # noqa: F401  registers every rule
    return [{"id": r.id, "severity": r.severity, "blocking": r.blocking,
             "kind": "file" if r.per_file else "change", "fix": r.fix}
            for r in sorted(RULES.values(), key=lambda r: r.id)]


def _lines(*lines: str) -> str:
    return "\n".join(lines) + "\n"


#: "Try it": three small projects, each with one line to change ("change this here ->").
#: Checked here: unchanged they must be ok with no changes, and with `find` replaced by
#: `replace` they must give `verdict` with exactly `rules`. Otherwise the build fails, so the
#: website never shows an example that doesn't do what it says.
EXAMPLES = [
    {"id": "parameter", "title": "Add a parameter", "file": "shop/prices.py",
     "find": "def price_with_tax(amount):", "replace": "def price_with_tax(amount, state):",
     "hint": "Taxes now depend on the state. Give price_with_tax a second parameter, state.",
     "blurb": "cart.py calls price_with_tax, and checkout.py calls cart.py. Nobody will touch "
              "either of them.",
     "verdict": "block", "rules": ["signature-break"],
     "files": {
         "shop/__init__.py": "",
         "shop/prices.py": _lines("def price_with_tax(amount):",
                                  "    return round(amount * 1.08, 2)"),
         "shop/cart.py": _lines("from shop.prices import price_with_tax", "", "",
                                "def total(prices):",
                                "    return sum(price_with_tax(p) for p in prices)"),
         "shop/checkout.py": _lines("from shop.cart import total", "", "",
                                    "def checkout(prices):",
                                    "    return f\"You pay ${total(prices)}\"")}},
    {"id": "default", "title": "Simplify a default", "file": "tags.py",
     "find": "def add_tag(tag, tags=None):", "replace": "def add_tag(tag, tags=[]):",
     "hint": "Looks simpler: make the default an empty list, tags=[].",
     "blurb": "A default is built once, when the function is defined, not on every call.",
     "verdict": "review", "rules": ["mutable-default-argument"],
     "files": {
         "tags.py": _lines("def add_tag(tag, tags=None):",
                           "    if tags is None:",
                           "        tags = []",
                           "    tags.append(tag)",
                           "    return tags"),
         "posts.py": _lines("from tags import add_tag", "", "",
                            "def tag_post(post, tag):",
                            "    post[\"tags\"] = add_tag(tag)",
                            "    return post")}},
    {"id": "format", "title": "Tidy the formatting", "file": "greet.py",
     "find": "    message=greeting+', '+name+'!'",
     "replace": "    message = greeting + \", \" + name + \"!\"",
     "hint": "Tidy this line: spaces around the operators, double quotes.",
     "blurb": "Magellan Lite compares syntax trees, not text, so formatting is never a change.",
     "verdict": "ok", "rules": [],
     "files": {
         "greet.py": _lines("def greet(name, greeting='Hello'):",
                            "    message=greeting+', '+name+'!'",
                            "    return message")}},
]


def examples() -> list[dict]:
    out = []
    for e in EXAMPLES:
        files, path = e["files"], e["file"]
        if files[path].count(e["find"]) != 1:
            raise SystemExit(f"build_site: {e['id']!r}: `find` must occur once in {path}")
        changed = {**files, path: files[path].replace(e["find"], e["replace"])}
        for after, want in ((files, ("ok", [], 0)), (changed, (e["verdict"], sorted(e["rules"])))):
            report = check_with_map(files, after)
            got = (report["verdict"], sorted({f["rule"] for f in report["findings"]}))
            if after is files:
                got += (len(report["changes"]),)
            if got != want:
                raise SystemExit(f"build_site: the {e['id']!r} example gives {got}, not {want}"
                                 f"{' unchanged' if after is files else ' once changed'}")
        out.append({k: e[k] for k in ("id", "title", "file", "find", "replace", "hint", "blurb",
                                      "verdict", "files")})
    return out


# -- the engine, for the browser ------------------------------------------------------------
def engine() -> dict:
    pkg = ROOT / "magellan_lite"
    files = {}
    for p in sorted(pkg.rglob("*.py")):
        if p.parent == pkg and p.name in NOT_IN_BROWSER:
            continue
        # read_text turns CRLF into LF: the bundle is the same on every OS
        files[p.relative_to(ROOT).as_posix()] = p.read_text(encoding="utf-8")
    return {"version": __version__, "files": files}


# -- writing ------------------------------------------------------------------------------------
def _js(name: str, value, what: str, indent: int | None = 1) -> str:
    return (f"// {what}: written by demo/build_site.py -- do not edit by hand\n"
            f"window.{name} = {json.dumps(value, indent=indent)};\n")


def build() -> dict[Path, str]:
    """Every generated file and what it should contain."""
    return {
        DATA / "incidents.js": _js("MAGELLAN_INCIDENTS", incidents(),
                                   "the famous failures, replayed from demo/incidents/"),
        DATA / "showcase.js": _js("MAGELLAN_SHOWCASE",
                                  {"version": __version__, "hero": hero(), "rules": rules(),
                                   "examples": examples()},
                                  "the hero, the checklist and the Try-it examples"),
        DATA / "engine.js": _js("MAGELLAN_ENGINE", engine(),
                                "Magellan Lite's source, run in the browser by Try it", None),
    }


def stale(files: dict[Path, str]) -> list[Path]:
    return [p for p, content in files.items()
            if not p.is_file() or p.read_text(encoding="utf-8") != content]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="only report whether site/data/ is up to date")
    args = ap.parse_args(argv)
    files = build()
    old = stale(files)
    if args.check:
        for p in old:
            print(f"out of date: {p.relative_to(ROOT).as_posix()}")
        if old:
            print("run: python demo/build_site.py")
        return 1 if old else 0
    DATA.mkdir(parents=True, exist_ok=True)
    for p in old:
        p.write_text(files[p], encoding="utf-8", newline="\n")
    for p in files:
        print(f"{'wrote    ' if p in old else 'unchanged'} {p.relative_to(ROOT).as_posix()}"
              f"  ({len(files[p]) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
