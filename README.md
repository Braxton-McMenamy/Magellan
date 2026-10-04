# Magellan Lite

**Know what your change breaks before you commit it.**

Magellan Lite maps a Python project from its syntax trees, finds exactly what a change touched,
and runs a checklist built from real software failures on that code only. One verdict:
**ok**, **review** or **block**.

```sh
pip install -e .                       # in a clone; Python 3.10+, no dependencies (see Install)
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

## Install

Python 3.10 or newer, and git. Nothing else.

To use the command-line tool, install it straight from GitHub:

```sh
pip install "git+https://github.com/Braxton-McMenamy/Magellan.git"           # the main branch
pip install "git+https://github.com/Braxton-McMenamy/Magellan.git@backend"   # or any branch
```

To work on Magellan Lite itself, install it editable, so your edits count without a reinstall:

```sh
git clone https://github.com/Braxton-McMenamy/Magellan.git
cd Magellan
pip install -e .
```

Or let pip do the clone. It puts it in `src/magellan-lite`, inside your virtual environment
(or the current folder without one):

```sh
pip install -e "git+https://github.com/Braxton-McMenamy/Magellan.git#egg=magellan-lite"
```

`pip install -e https://github.com/Braxton-McMenamy/Magellan` does not work: an editable
install needs a folder on your computer or a `git+` URL, and a plain web link is neither.

An installed copy has the whole tool: `check`, `rules`, `share`, `team`, and the API of
`serve`. The website's pages, `demo/` and the tests are only in a clone.

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

**Live, for the whole team:** the website's [Team suite](https://magellan-code.pages.dev/suite.html)
reads a GitHub repository's `refs/wip/*` and checks everyone's work alone, two by two and all at
once, in the browser (`web.team_live`, on Pyodide in a worker), refreshing every 90 seconds (15
with a token). The moment a repository is connected, its *Team live* tab draws the whole
project in 3D; as people share, each one's changes take their own colour on it, and below come
each person and every pair that breaks only together. *You* shows your work against each
teammate's. The [Scene](https://magellan-code.pages.dev/scene.html) shows any of those maps full
screen. In VS Code, turn on **Magellan Lite: Share On Save** and your work is shared as you
save (at most every 15 seconds), so the suite follows along. A public repository's shared work
is public: the extension says so when you turn it on.

## For AI agents

An AI agent that edits code (Claude Code, Cursor, ...) can ask what a careful person would:
what does my change break, who depends on this old function before I touch it, is this code
dead? Two ways in.

**The brief.** `magellan-lite brief` is the check cut down for an agent, as JSON: a `verdict`
(ok, review or block), a one-line `summary`, `do_first` (the findings to fix, worst first,
each with `where` as path:line, what, why and a fix), `reaches` (the blast radius),
`check_these_files` (files the change did not edit but reaches: read them again),
`tests_to_run` (test files that mention what changed), `counts` and `errors`. Each list stops
at `--limit` (8) and `omitted` says how much was cut. `--format text` is the terminal version;
it exits like `check`. The shape is in `magellan_lite/brief.py`.

**The MCP server.** `magellan-lite mcp` offers the tools over the Model Context Protocol
(stdin and stdout, standard library only):

| Tool | When an agent calls it |
|---|---|
| `magellan_lite_brief` | after editing, before committing: the brief above |
| `magellan_lite_check` | for everything the brief cut: the full report, optionally with the code map |
| `magellan_lite_reach` | before changing a function, class or constant: its callers and theirs, hop by hop, with files and tests |
| `magellan_lite_node` | to understand one definition: where it is, its signature, who calls it and what it calls |
| `magellan_lite_unused` | before modernizing: definitions nothing in the project mentions (probably dead code) |
| `magellan_lite_team` | your work against your teammates' shared work in progress |
| `magellan_lite_rules` | to look up a rule id from a finding |

Add it to Claude Code, in the project's folder (`--scope project` writes `.mcp.json`, so the
whole team gets it):

```sh
claude mcp add magellan-lite -- python -m magellan_lite mcp
```

Any other client takes the same command in its MCP settings:

```json
{
  "mcpServers": {
    "magellan-lite": {"command": "python", "args": ["-m", "magellan_lite", "mcp"]}
  }
}
```

`python` has to be the one Magellan Lite is installed in (with `pip install`, the command
`magellan-lite mcp` works too). Every tool takes an optional `path`, the project's folder
(default: the one the server started in). `reach`, `node` and `unused` read the files as they
are, so code outside git works too; `brief` and `check` compare with the last commit, or with a
folder holding the old version (`against`). A definition can be named in full
(`billing.ledger.post`, `c@parse.load`), by its short name (`post`: an ambiguous one returns
the candidates), or as `path:line`.

## Other languages, and old code

Lite reads Python 3 itself, and everything else through the full Magellan's frontends,
vendored in `magellan_lite/polyglot/` (26k lines, standard library only) and run by
`magellan_lite/languages.py`:

| Language | Reads | Catches in a change |
|---|---|---|
| C | `.c`, `.h` | call sites a new prototype breaks, deleted functions still called, `goto fail`, struct layout and enum value shifts |
| Java | `.java` | an overload whose parameters changed under its callers, unhandled new enum members |
| Fortran (66 to 2023) | `.f`, `.f90`, ... | `CALL`s an argument list no longer fits (implicit interfaces too), `COMMON` blocks that no longer line up, `INTENT(OUT)` read first |
| COBOL | `.cbl`, `.cpy`, ... | copybook layouts that moved, `CALL ... USING` mismatches, truncating `MOVE`s, `PERFORM ... THRU` ranges |
| C++ | `.cpp`, `.hpp`, ... | the same as C, when libclang is installed (`pip install libclang`) |
| TypeScript/JavaScript | `.ts`, `.js`, ... | when Node.js and the `typescript` package are installed |
| Python 2 | `.py` | read through a line-for-line rewrite, so code waiting for its upgrade is on the map |

Every language joins the same map, so the blast radius crosses them: a C function behind a
Java `native` method reaches its Java callers. Their rules are on the checklist
(`magellan-lite rules`) and count toward the verdict. In the browser, the Team suite loads the
other languages' frontends (`site/data/polyglot.js`, 1.2 MB) only when a repository has files
in them; C++ and TypeScript/JavaScript stay skipped there (no libclang, no Node.js).

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

Who owns what, and what is left. A task still open is a `TODO(<tag>)` comment where the work
goes; `git grep -n "TODO("` lists them.

| Area | Who | Done | Open |
|---|---|---|---|
| Engine | Brayton | The map, diff and blast radius; renames; `signature-break`, `removed-still-referenced`; the other languages (`languages.py`, `polyglot/`); the rules (the five famous-failure rules, the starter rules, the fifteen ported from the full Magellan); `brief` and the MCP server; team checks | |
| The extension's core | Brayton | Nothing to set up (`python.js`), the repository from git (`repo.js`), the sidebar, the walkthrough, the extension page | |
| Website | Braxton | The hero, the famous-failure player (opens from `#incident=<name>`), the checklist, Try it (with a Share link), the Team suite, Copy buttons, the favicon, other languages in the browser | Keep the Team section's lines true (`TODO(starter)` in `site/index.html`) |
| Quality, QoL and graphs | Faidh | Status bar colours, `showLow`, the icon; `--format markdown`, `hook install`, `[tool.magellan-lite]` settings, `# magellan: ignore[rule]`, colour, the PR comment bot; the 3D map, the Flow/3D panel, the skull, the Scene | A screenshot of a squiggle for the extension page (`TODO(faidh) 4` in `editors/vscode/README.md`) |

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
  web.py        a check plus the code map, for the website (server, browser, build); team_live
  combine.py    two people's work checked together (no git: the browser runs it too)
  team.py       magellan-lite share / team: the git side
  security.py   the website's security headers, for the server and for Cloudflare Pages
  languages.py  C, C++, Java, Fortran, COBOL, TypeScript, Python 2: their frontends, in Lite's map
  polyglot/     those frontends, vendored from the full Magellan (fix them there first)
  server.py     magellan-lite serve: the website and its API
  cli.py        the command line
  rules/        the checklist, one file per rule
tests/          unittest; tests/helpers.py makes throwaway git repositories
demo/           incidents/ (famous failures, one folder per change), run.py (the scoreboard),
                build_site.py (writes site/data/ from the real engine)
site/           the website (Braxton's): plain HTML, CSS and JavaScript, no build step. Three
                pages: index.html (home), suite.html (Team suite), scene.html (Scene).
                site/data/ and site/_headers are generated, never edited by hand; js/map.js +
                css/map.css draw every code map (website and extension)
editors/vscode/ the VS Code extension: on-save checks, the status bar, team conflicts, the map
                panel (see its README; `npm test` there, or tests/test_extension.py)
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

Beside the home page:

- **Team suite** (`suite.html`): connect a GitHub repository and watch the team's shared work
  in progress, checked together (see [Working as a team](#working-as-a-team)). It reads GitHub
  from the browser (`js/github.js`: public repositories need no token; a token, for private
  ones or faster updates, stays in the tab's session and goes only to api.github.com) and
  checks in a worker (`js/engine-worker.js`), so the page never freezes.
- **Scene** (`scene.html`): one map, as big as the screen, laid out like a 3D editor (what to
  show on the left, the viewport, the picked dot's properties on the right). Two views of the
  same map:
  - **3D** (`js/graph3d.js` via `js/scene3d.js`): the whole project as clusters on a sphere
    (by language, folder, or who calls whom), coloured by the change: what changed in orange
    (or in its author's colour), what it reaches in red by how hard, a red ring on a finding.
    Drag to turn, wheel to fly, double-click to open.
  - **Flow** (`js/map.js`): the change hop by hop, left to right. Pick any dot and press
    *What depends on it*: Flow shows everything that would feel a change to it, the question to
    ask before changing old code.

  The skull lights up probably-dead code: definitions whose name appears nowhere else in the
  project (`web.py`, `unused` on each map node; decorated definitions, overrides and tests are
  left out). Before a repository is connected, the Scene shows Magellan Lite's own code
  (`data/project.js`), so there is always something to explore.

`site/data/` is written by `python demo/build_site.py`: the replayed incidents, the hero, the
Scene's first picture (`project.js`), the
rules, the Try-it examples (each checked to give the verdict it promises) and the engine
bundle. It also writes `site/_headers`, the security headers Cloudflare Pages sends with every
file: the same ones `magellan-lite serve` sends (`magellan_lite/security.py`), including a
content security policy that lets a page load only its own scripts and Pyodide, and contact
only itself, the CDN and GitHub. Run it after changing a rule, the engine, an incident or an
inline script, and commit what it writes; `tests/test_site.py` fails while it is out of date.
To work on the pages, open `site/index.html` in a browser, or run `magellan-lite serve` for
the pages and the live API on <http://localhost:8000> (the Team suite needs this or the live
site: browsers don't run workers for pages opened from disk).

## Local server and API

`magellan-lite serve` serves `site/` and a JSON API that runs Magellan Lite for real, on
<http://localhost:8000>. Standard library only. It listens on this computer alone unless
started with `--host 0.0.0.0` (then the local network, or a Tailscale network, can reach it).
Code sent to it is only parsed, never run; a request is capped at 1 MB and 200 files per side.
Browsers may call the API only from the website's own address and the origins given with
`--allow-origin` (the default allows <https://magellan-code.pages.dev>).

| Endpoint | Returns |
|---|---|
| `GET /api/health` | `{"ok": true}` |
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
