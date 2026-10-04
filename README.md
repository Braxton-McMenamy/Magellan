# Magellan Lite

**Know what your change breaks before you commit it.**

Magellan Lite maps a Python project from its syntax trees, finds exactly what a change touched,
and runs a checklist built from real software failures on that code only. One verdict:
**ok**, **review** or **block**.

```sh
pip install -e .                       # Python 3.10+, no dependencies
magellan-lite check                    # the working tree against your last commit
magellan-lite check --format json      # for tools and CI
magellan-lite rules                    # the checklist
magellan-lite serve --open             # the website and its live API on http://localhost:8000
magellan-lite share                    # publish your work in progress to your team (no commit)
magellan-lite team                     # your work + each teammate's: what only the combination breaks
python demo/run.py                     # famous failures, replayed (the demo and the scoreboard)
python demo/build_site.py              # the website's data, from the real engine (after a rule lands)
python -m unittest discover -s tests -t .
```

The project page is `site/index.html`: see [Website](#website) and [Local server and API](#local-server-and-api).

## How it works

1. **Map** (`magellan_lite/defs.py`): every function, method, class and constant, named
   stably (`pkg.mod.Class.method`) and hashed from its syntax tree. Reformatting, moving code,
   editing a docstring or switching line endings is never a change.
2. **Diff** (`diff.py`): added, removed, renamed, or a changed signature, body or value,
   compared with `git:HEAD` (or any revision, or another folder).
3. **Reach** (`graph.py`, `radius.py`): a call graph of who calls and reads what, walked
   *backwards* from each change -- if `parse_record` changes, its callers are at risk, then
   theirs. Every hop fades the score (a call x0.9), so the result is a ranked list of the code
   the change can break that nobody touched, each with the path that reaches it.
4. **Check** (`engine.py`, `rules/`): every rule runs on the files the change touched, and only
   findings inside a changed definition are kept, so old problems elsewhere stay quiet.
   `signature-break` and `removed-still-referenced` use the call graph to find the calls a
   change breaks, wherever they are.

## Working as a team

Two changes can each be fine and still break together. Braxton adds a required `layout`
parameter to `parse_record` (and updates the call he knows about); Alice, on her own branch,
adds a new call `parse_record(row)`. Different files, so git merges them without a conflict;
each branch's checks pass; the merge raises TypeError in production.

```sh
magellan-lite share     # Braxton: pushes his working tree to refs/wip/braxton
magellan-lite team      # Alice: her work against everyone's shared work in progress
```

```
  braxton  (shared just now: 2 changes in 2 files)
    their changes: channel.parse_record (signature), collector.collect (body)
    BLOCK · 1 problem that only the combination has
    [ ] CRITICAL signature-break  sensor/api.py:5
        sensor.api.upload calls parse_record() the old way: it now requires layout, ...
```

`share` builds a commit from the working tree with a throwaway index (tracked and new files,
as `.gitignore` allows) and pushes it to `refs/wip/<your git user.name>`: your branch, index
and stash are untouched. `team` fetches `refs/wip/*`, overlays each teammate's changed files on
your working tree (a file you both edited is checked with your version, and named), and
reports only the findings neither of you has alone. There are no accounts and no server: the
remote you already use is the shared place, and its permissions decide who sees what.
`team` exits 1 when a combination blocks (`--fail-on`), and `--format json` is for tools.

## Writing a rule

```python
import ast
from magellan_lite.findings import rule

@rule("bare-except", "low", fix="Catch what you expect, e.g. `except ValueError:`.")
def bare_except(tree: ast.Module, path: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            yield node, "a bare `except:` also catches Ctrl+C and SystemExit"
```

Put it in its own file in `magellan_lite/rules/`, import it in `rules/__init__.py`, and test
it in `tests/rules/` (`tests/rules/checks.py` has a one-line helper). Rules about what changed
*between* versions use `@change_rule` instead: see `magellan_lite/findings.py`.

## Task board

Every task is a `TODO(<tag>)` comment where the work goes; `git grep -n "TODO("` lists them.
Each says what to build and how you know it's done.

| Tag | Who | What |
|---|---|---|
| `TODO(starter)` | new to Python's `ast` | Four small rules with numbered steps and a test waiting for each (`bare_except` → `assert_tuple` → `compare_none` → `debug_leftover`). Delete the test's `@unittest.skip` line when you're done. |
| `TODO(qol)` | comfortable | The PR comment bot (most visible), `--format markdown`, `hook install`, settings from `pyproject.toml`, `# magellan: ignore[rule]`, colours. |
| `TODO(checklist)` | comfortable | Done: the five famous-failure rules (`leap-day-date`, `reused-value`, `loop-without-progress`, `regex-catastrophic-backtracking`, `unsynchronized-shared-state`); every incident is caught. Next: make them see more (a regex kept in a constant is the easiest; see `magellan_lite/rules/__init__.py`). After any rule change, `python demo/run.py` must stay all caught; then run `python demo/build_site.py`. |
| engine | Brayton | Done: the blast radius (call graph and propagation), renames, the "reaches" section, `signature-break` and `removed-still-referenced`. The sensor incident is caught. |
| `TODO(site)` | Braxton | The website. Done: the live hero, the famous-failure player, the checklist, Try it. Next: a share link for Try it (`site/js/tryit.js`), opening the player on one incident (`site/js/story.js`). |

## Layout

```
magellan_lite/
  findings.py   Finding, @rule / @change_rule        shared contract: change together
  report.py     Report and the verdict               shared contract: change together
  source.py     reading files (UTF-8), snapshots
  defs.py       the map: definitions from syntax trees
  diff.py       what changed (renames included), and which lines that covers
  graph.py      the call graph: who calls and reads what
  radius.py     the blast radius: what a change reaches, ranked
  git.py        the project at a git revision
  engine.py     one check, start to finish
  output.py     the terminal report
  web.py        a check plus the code map, for the website (server, browser, build)
  server.py     magellan-lite serve: the website and its API
  cli.py        the command line
  rules/        the checklist, one file per rule
tests/          unittest; tests/helpers.py makes throwaway git repositories
demo/           incidents/ (famous failures, one folder per change), run.py (the scoreboard),
                build_site.py (writes site/data/ from the real engine)
site/           the project page (Braxton's): plain HTML, CSS and JavaScript, no build step;
                site/data/ is generated, never edited by hand
```

## Website

Live at <https://magellan-code.pages.dev> (Cloudflare Pages, project `magellan-code`).
`site/` is the only copy of the page: the deploy publishes this folder from `main` as it is, so
a change here goes live on the next deploy. Plain HTML, CSS and JavaScript, no build step.
`index.html` stays at the top of `site/`, and every link is relative (`css/style.css`, not
`/css/style.css`) so the page also works opened straight from disk.

What's on it, all drawn from the real engine:

- **The hero** types out a real `magellan-lite check` of the sensor change.
- **Famous failures, replayed**: an animated story per incident. What happened and what it
  cost (every figure from a cited source in its `incident.json`), the code change, the code
  map lighting up hop by hop as the change reaches its callers, the issue, and whether Magellan
  Lite catches it today or the rule is still in progress.
- **The checklist**: every rule, and the ones still being written.
- **Try it**: three small projects, each with one line marked "change this here". Make the
  change (or press *Do it for me*) and the verdict, checklist and map update as you type:
  *block*, *review* and *ok* respectively. On the public
  site the engine runs *in the visitor's browser*: `site/data/engine.js` is Magellan Lite's
  own source, run by [Pyodide](https://pyodide.org) (Python compiled to WebAssembly, loaded
  from a CDN), so nothing is uploaded. Under `magellan-lite serve` it uses the local API
  instead, which also works offline.

`site/data/` is written by `python demo/build_site.py`: the replayed incidents, the hero, the
rules, the Try-it examples (each checked to give the verdict it promises) and the engine
bundle. Run it after changing a rule, the engine or an incident, and commit what it writes;
`tests/test_site.py` fails while it is out of date. To work on the page, open
`site/index.html` in a browser, or run `magellan-lite serve` for the page and the live API on
<http://localhost:8000>.

## Local server and API

`magellan-lite serve` serves `site/` and a JSON API that runs Magellan Lite for real, on
<http://localhost:8000>. Standard library only. It listens on this computer alone unless
started with `--host 0.0.0.0` (then the local network, or a Tailscale network, can reach it).
Code sent to it is only parsed, never run; a request is capped at 1 MB and 200 files per side.
API responses allow any origin, so a page opened straight from disk can call it too.

| Endpoint | Returns |
|---|---|
| `GET /api/health` | `{"ok": true, "version": "..."}` |
| `GET /api/rules` | `[{"id", "severity", "blocking", "kind", "fix"}]` |
| `GET /api/incidents` | the famous failures replayed now, the same data as `python demo/run.py` (`?refresh=1` to re-run after a rule changes) |
| `POST /api/check` | body `{"before": {"path.py": "source"}, "after": {"path.py": "source"}}`; returns the report: `verdict`, `changes`, `findings`, `affected` (the blast radius), `errors` |

From the website:

```js
const report = await fetch("http://localhost:8000/api/check", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({before: {"app.py": oldCode}, after: {"app.py": newCode}}),
}).then((r) => r.json());
// report.verdict: "ok" | "review" | "block"; report.findings[]; report.affected[]
```

Errors come back as `{"error": "..."}` with status 400 (a bad request) or 413 (too large).

## Honest limits

Static analysis: a clean report is not proof. Magellan Lite reads code, not deployments,
configuration or hardware, and `demo/incidents/` says for each failure what a code check can
and cannot see. The ideas come from Magellan, a larger multi-language engine one of us wrote
before this event; this project is a new, smaller implementation built here.
