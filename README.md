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
python demo/run.py                     # famous failures, replayed (the demo and the scoreboard)
python -m unittest discover -s tests -t .
```

The project page is `index.html` at the root: see [Website](#website).

## How it works

1. **Map** (`magellan_lite/defs.py`): every function, method, class and constant, named
   stably (`pkg.mod.Class.method`) and hashed from its syntax tree. Reformatting, moving code,
   editing a docstring or switching line endings is never a change.
2. **Diff** (`diff.py`): added, removed, or a changed signature, body or value, compared with
   `git:HEAD` (or any revision, or another folder).
3. **Check** (`engine.py`, `rules/`): every rule runs on the files the change touched, and only
   findings inside a changed definition are kept, so old problems elsewhere stay quiet.

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
| `TODO(checklist)` | comfortable | The famous-failure rules: `leap-day-date` (start here), `reused-value`, `loop-without-progress`, `regex-catastrophic-backtracking`. Each turns an incident in `python demo/run.py` from *waiting* to *caught*. |
| `TODO(engine)` | Brayton | The blast radius (call graph and propagation: the headline feature), renames, a "reaches" section in the output. |
| `TODO(site)` | Braxton | The website. From the Python side: show `python demo/run.py`'s results on the page (`demo/run.py`, end of `main()`). |

## Layout

```
magellan_lite/
  findings.py   Finding, @rule / @change_rule        shared contract: change together
  report.py     Report and the verdict               shared contract: change together
  source.py     reading files (UTF-8), snapshots
  defs.py       the map: definitions from syntax trees
  diff.py       what changed, and which lines that covers
  git.py        the project at a git revision
  engine.py     one check, start to finish
  output.py     the terminal report
  cli.py        the command line
  rules/        the checklist, one file per rule
tests/          unittest; tests/helpers.py makes throwaway git repositories
demo/           incidents/ (famous failures as git histories) and run.py (the scoreboard)
index.html      the project page (Braxton's): plain HTML and CSS, no build step
style.css
```

## Website

Plain HTML and CSS, no build step, no dependencies. Open `index.html` in a browser, or serve
the folder:

```sh
python -m http.server 8000
```

then visit <http://localhost:8000>. To host it free on GitHub Pages: repository Settings >
Pages > deploy from the `main` branch. It can also be copied to any web server's document
root.

## Honest limits

Static analysis: a clean report is not proof. Magellan Lite reads code, not deployments,
configuration or hardware, and `demo/incidents/` says for each failure what a code check can
and cannot see. The ideas come from Magellan, a larger multi-language engine one of us wrote
before this event; this project is a new, smaller implementation built here.
