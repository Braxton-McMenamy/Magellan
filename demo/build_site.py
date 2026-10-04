"""Write the website's data from the code. Run it after changing a rule, the engine or an
incident, and commit what it writes:

    python demo/build_site.py            # write site/data/
    python demo/build_site.py --check    # exit 1 if site/data/ is out of date (the tests do this)

site/ is published exactly as it is (the host runs no build step), so everything the page
shows is generated here, by the real engine:

    site/data/incidents.js   the famous failures, replayed: the story player and the scoreboard
    site/data/showcase.js    the hero's terminal output, the checklist, the "Try it" examples
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
NOT_IN_BROWSER = {"__main__.py", "cli.py", "git.py", "incidents.py", "server.py"}


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


#: "Try it" examples. Each is checked here: if the engine stops saying `verdict` and finding
#: exactly `rules`, the build fails, so the website never shows an example that doesn't work.
PRESETS = [
    {"id": "signature", "title": "Add a parameter, miss a caller", "open": "shop/prices.py",
     "blurb": "price_with_tax() gains a required state. cart.py, which nobody touched, still "
              "calls it the old way.",
     "verdict": "block", "rules": ["signature-break"],
     "before": {
         "shop/__init__.py": "",
         "shop/prices.py": _lines("TAX_RATE = 0.08", "", "",
                                  "def price_with_tax(amount):",
                                  "    return round(amount * (1 + TAX_RATE), 2)"),
         "shop/cart.py": _lines("from shop.prices import price_with_tax", "", "",
                                "def total(items):",
                                "    return sum(price_with_tax(item[\"price\"]) for item in items)"),
         "shop/checkout.py": _lines("from shop.cart import total", "", "",
                                    "def checkout(items, pay):",
                                    "    amount = total(items)",
                                    "    pay(amount)",
                                    "    return amount")},
     "after": {
         "shop/prices.py": _lines("TAX_RATES = {\"TX\": 0.0825, \"CA\": 0.0725, \"OR\": 0.0}",
                                  "", "",
                                  "def price_with_tax(amount, state):",
                                  "    return round(amount * (1 + TAX_RATES[state]), 2)")}},
    {"id": "removed", "title": "Delete a function that's still used", "open": "app/text.py",
     "blurb": "legacy_slug() looks unused and is deleted. posts.py still calls it.",
     "verdict": "block", "rules": ["removed-still-referenced"],
     "before": {
         "app/__init__.py": "",
         "app/text.py": _lines("import re", "", "",
                               "def slugify(title):",
                               "    return re.sub(r\"[^a-z0-9]+\", \"-\", title.lower()).strip(\"-\")",
                               "", "",
                               "def legacy_slug(title):",
                               "    return title.lower().replace(\" \", \"_\")"),
         "app/posts.py": _lines("from app.text import legacy_slug", "", "",
                                "def post_url(post):",
                                "    return \"/posts/\" + legacy_slug(post[\"title\"])", "", "",
                                "def sitemap(posts):",
                                "    return [post_url(p) for p in posts]")},
     "after": {
         "app/text.py": _lines("import re", "", "",
                               "def slugify(title):",
                               "    return re.sub(r\"[^a-z0-9]+\", \"-\", title.lower()).strip(\"-\")")}},
    {"id": "default", "title": "A shared default list", "open": "tags.py",
     "blurb": "A tidy-up replaces `tags=None` with `tags=[]`. Every call now shares one list.",
     "verdict": "review", "rules": ["mutable-default-argument"],
     "before": {"tags.py": _lines("def add_tag(tag, tags=None):",
                                  "    if tags is None:",
                                  "        tags = []",
                                  "    tags.append(tag)",
                                  "    return tags")},
     "after": {"tags.py": _lines("def add_tag(tag, tags=[]):",
                                 "    tags.append(tag)",
                                 "    return tags")}},
    {"id": "leftovers", "title": "A quick fix, debugging left in", "open": "settings.py",
     "blurb": "The team's starter rules: a bare except, a print, == None, an assert that "
              "always passes.",
     "verdict": "review",
     "rules": ["assert-on-tuple", "bare-except", "compare-to-none", "debug-leftover"],
     "before": {"settings.py": _lines("import json", "", "",
                                      "def load_settings(path):",
                                      "    with open(path) as f:",
                                      "        return json.load(f)")},
     "after": {"settings.py": _lines("import json", "", "",
                                     "def load_settings(path):",
                                     "    try:",
                                     "        with open(path) as f:",
                                     "            settings = json.load(f)",
                                     "    except:",
                                     "        settings = {}",
                                     "    print(\"settings:\", settings)",
                                     "    if settings.get(\"theme\") == None:",
                                     "        settings[\"theme\"] = \"dark\"",
                                     "    assert (settings[\"theme\"] in (\"dark\", \"light\"), "
                                     "\"unknown theme\")",
                                     "    return settings")}},
    {"id": "reformat", "title": "Only reformatting", "open": "greet.py",
     "blurb": "Spacing, quotes, a comment and a docstring. The syntax tree is the same, so "
              "nothing changed.",
     "verdict": "ok", "rules": [],
     "before": {"greet.py": _lines("def greet(name,greeting='Hello'):",
                                   "    message=greeting+', '+name+'!'",
                                   "    return message")},
     "after": {"greet.py": _lines("def greet(name, greeting=\"Hello\"):",
                                  "    \"\"\"Say hello.\"\"\"",
                                  "    message = greeting + \", \" + name + \"!\"  # tidied",
                                  "    return message")}},
    {"id": "blank", "title": "Your own code", "open": "app.py",
     "blurb": "Paste the old version on the left and the new one on the right.",
     "verdict": "ok", "rules": [],
     "before": {"app.py": _lines("def add(a, b):", "    return a + b")},
     "after": {}},
]


def presets() -> list[dict]:
    out = []
    for p in PRESETS:
        after = {**p["before"], **p["after"]}
        report = check_with_map(p["before"], after)
        got = (report["verdict"], sorted({f["rule"] for f in report["findings"]}))
        if got != (p["verdict"], sorted(p["rules"])):
            raise SystemExit(f"build_site: the {p['id']!r} example now gives {got}, "
                             f"not {(p['verdict'], sorted(p['rules']))}: update PRESETS")
        out.append({k: p[k] for k in ("id", "title", "blurb", "open")}
                   | {"before": p["before"], "after": after})
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
                                   "presets": presets()},
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
