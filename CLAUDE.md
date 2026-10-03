# Working in this repository

Magellan Lite: a pre-commit check for Python (see README.md). Built at a hackathon by a team
of three; each teammate uses their own Claude Code.

## Rules for everyone's Claude

- **The team writes this code.** On a `TODO(starter)` task the teammate is learning: explain
  the idea, point at the numbered steps in the file, review what they write, and give hints
  before answers. Write the code yourself only when they ask you to.
- **No AI attribution in commits or PRs** (`.claude/settings.json` turns it off). Commits go out
  under the teammate's own name.
- **Stay in your lane.** Each task is a `TODO(<tag>)` in the file where the work goes. Don't
  rewrite files outside the task. `findings.py` and `report.py` are the shared contract: change
  them only when the team agrees.
- **The website is Braxton's** (`index.html`, `style.css` at the root). Python work doesn't
  edit it; where the backend should feed it, leave a `TODO(site)`.
- **Tests before done:** `python -m unittest discover -s tests -t .` passes, and
  `python demo/run.py` shows no MISSED.

## Conventions

- Python 3.10+, standard library only. No new dependencies.
- Paths are stored relative to the project root with forward slashes (`Path.as_posix()`).
  The team is on Windows and Linux.
- Read and write files with `encoding="utf-8"`; decode subprocess output as UTF-8 too.
- One rule per file in `magellan_lite/rules/`, imported in `rules/__init__.py`, tested in
  `tests/rules/` (see `tests/rules/checks.py`).
- A rule should be quiet when unsure: a check people learn to ignore is worthless. Every rule
  test has cases that must NOT be flagged.
- Finding text: `message` says what is wrong and where, `detail` why it matters, `fix` what to
  do. Plain words, no jargon.
