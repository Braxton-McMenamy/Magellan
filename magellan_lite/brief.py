"""The brief: a verdict small enough for an AI agent to act on.

``check`` is written for a person: every change, the whole checklist, the whole blast radius.
An agent that has just edited code wants the opposite: one line saying whether to stop, the
few things to fix (each with where it is and what to do), the files it did not edit but
should read again, and the tests to run. ``magellan-lite brief`` gives that, and so does the
MCP tool ``magellan_lite_brief`` (mcp.py).

    magellan-lite brief [PATH] [--against git:HEAD] [--format json|text] [--limit 8]

The JSON, with its keys kept stable (lists are cut at ``--limit``; ``omitted`` says how many
entries each one lost, and ``check --format json`` has them all)::

    {
      "verdict": "block",                       # ok | review | block: the same as `check`
      "summary": "Do not commit yet: 1 critical finding, first signature-break at ...",
      "against": "git:HEAD",
      "counts": {"findings": {"critical": 1, "high": 0, "medium": 0, "low": 0},
                 "changes": 1, "reached": 2, "errors": 0},
      "changes": [{"name": "app.pay.charge", "kind": "signature", "where": "app/pay.py:1",
                   "detail": "(amount)  ->  (amount, currency)"}],
      "do_first": [{"severity": "critical", "rule": "signature-break",
                    "where": "app/shop.py:5", "what": "...", "why": "...", "fix": "..."}],
      "reaches": [{"name": "app.shop.checkout", "where": "app/shop.py:4", "hops": 1,
                   "score": 0.85, "why": "app.shop.checkout calls app.pay.charge (...)"}],
      "check_these_files": [{"path": "app/shop.py", "why": "signature-break at line 5; ..."}],
      "tests_to_run": [{"path": "tests/test_pay.py", "why": "mentions charge"}],
      "errors": ["legacy/old.py:3: invalid syntax"],
      "omitted": {"changes": 0, "do_first": 0, "reaches": 0, "check_these_files": 0,
                  "tests_to_run": 0, "errors": 0}
    }

- ``do_first``: the checklist, worst first: by severity, then blocking rules, then closeness
  to the edit (inside an edited definition, then one hop away, two...). Every finding is about
  the change already: the engine keeps only what lands in edited code, or what the edit breaks.
- ``reaches``: the blast radius (radius.py), the highest scores first; ``where`` is the file.
- ``check_these_files``: files the change did not edit that hold a finding or a definition it
  reaches, findings first, then by how hard they are hit.
- ``tests_to_run``: test files (found by their names: ``test_*.py``, ``*_test.py``,
  ``*Test.java``, ``*.spec.ts``, files under ``tests/`` in the other languages...) that the
  change edited, or that mention a definition it changed or reaches closely. A plain text
  search, so it errs quiet: a short or common name (``get``, ``run``) counts only when its
  class or module is named in the file too.
"""

from __future__ import annotations

import re
from pathlib import Path

from magellan_lite.combine import _changed
from magellan_lite.defs import Definition, definitions
from magellan_lite.engine import check_snapshots, load_before
from magellan_lite.findings import SEVERITIES, Finding, blocking_rules
from magellan_lite.graph import _NO_GUESS
from magellan_lite.report import VERDICTS, Report
from magellan_lite.source import Snapshot, module_name, working_tree

#: how many entries each list keeps by default
LIMIT = 8
#: reached definitions this close (score) count for tests_to_run; the full brief's "near"
NEAR = 0.25
#: names too vague to look for alone in a test file
_VAGUE = _NO_GUESS | {"main", "init", "setup", "process", "handle", "test", "value", "data",
                      "name", "result", "item", "call", "apply", "execute", "build", "make",
                      "new", "reset", "next", "save", "check", "parse", "render", "create"}
_TOKENS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_COBOL_TOKENS = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*[A-Za-z0-9_]")   # CALC-INTEREST


def brief(root: str | Path = ".", against: str = "git:HEAD", limit: int = LIMIT) -> dict:
    """Check a project on disk against a baseline (``git:REV`` or a directory) and brief it."""
    root = Path(root).resolve()
    before, after = load_before(root, against), working_tree(root)
    return build(check_snapshots(before, after, root, against), before, after, limit)


def build(report: Report, before: Snapshot, after: Snapshot, limit: int = LIMIT) -> dict:
    """The brief of a finished check (see the module docstring for its shape)."""
    limit = max(1, int(limit))
    defs = definitions(after)                       # trees are parsed already: cheap
    touched = _changed(before.files, after.files) | set(report.files_changed)
    changed = {c.name for c in report.changes} | {c.before.name for c in report.changes
                                                    if c.before}
    reached = {a["name"]: a for a in report.affected}
    blocking = blocking_rules()
    by_path: dict[str, list[Definition]] = {}
    for d in defs.values():
        by_path.setdefault(d.path, []).append(d)

    def closeness(f: Finding) -> float:
        d = _innermost(by_path.get(f.path, ()), f.line)
        if d is not None and d.name in changed:
            return 0
        if d is not None and d.name in reached:
            return reached[d.name]["hops"]
        return 0.5 if f.path in touched else 99

    findings = sorted(report.findings, key=lambda f: (
        f.rank, f.rule not in blocking, closeness(f), f.path, f.line, f.rule))
    severity = {s: sum(f.severity == s for f in findings) for s in SEVERITIES}

    near = [defs[a["name"]] for a in report.affected
            if a["score"] >= NEAR and a["name"] in defs]
    edited = [c.after or c.before for c in report.changes] + \
             [c.before for c in report.changes if c.kind == "renamed"]
    lists = {
        "changes": [{"name": c.name, "kind": c.kind, "where": f"{c.path}:{c.line}",
                     "detail": _cut(c.detail, 160)} for c in report.changes],
        "do_first": [{"severity": f.severity, "rule": f.rule, "where": f"{f.path}:{f.line}",
                      "what": _cut(f.message, 240), "why": _cut(f.detail, 240),
                      "fix": _cut(f.fix, 200)} for f in findings],
        "reaches": [{"name": a["name"], "where": f"{a['path']}:{a['line']}", "hops": a["hops"],
                     "score": a["score"], "why": _cut(a["why"], 200)} for a in report.affected],
        "check_these_files": _files_to_check(findings, report.affected, touched),
        "tests_to_run": find_tests(after, edited, near, touched),
        "errors": list(report.errors),
    }
    counts = {"findings": severity, "changes": len(report.changes),
              "reached": len(report.affected), "errors": len(report.errors)}
    out = {
        "verdict": report.verdict,
        "summary": _summary(report, findings, counts,
                            len({a["path"] for a in report.affected} - touched)),
        "against": report.against,
        "counts": counts,
    }
    out.update({key: items[:limit] for key, items in lists.items()})
    out["omitted"] = {key: max(0, len(items) - limit) for key, items in lists.items()}
    return out


def fails(b: dict, fail_on: str) -> bool:
    """Should this brief stop a commit? ``fail_on`` is a verdict, or ``never`` (as ``check``)."""
    return fail_on != "never" and VERDICTS.index(b["verdict"]) >= VERDICTS.index(fail_on)


# -- the parts -------------------------------------------------------------------------------

def _innermost(defs, line: int) -> Definition | None:
    """The narrowest of ``defs`` (one file's) around ``line``: the method, not its class."""
    best = None
    for d in defs:
        if d.line <= line <= d.end_line:
            if best is None or d.end_line - d.line < best.end_line - best.line:
                best = d
    return best


def _files_to_check(findings: list[Finding], affected: list[dict], touched: set[str]) -> list[dict]:
    """Files the change did not edit that hold a finding or a definition it reaches."""
    files: dict[str, dict] = {}
    for f in findings:
        if f.path in touched or not f.path:
            continue
        entry = files.setdefault(f.path, {"rank": 9, "score": 0.0, "findings": [], "reached": []})
        entry["rank"] = min(entry["rank"], f.rank)
        entry["findings"].append(f"{f.rule} at line {f.line}")
    for a in affected:
        if a["path"] in touched:
            continue
        entry = files.setdefault(a["path"], {"rank": 9, "score": 0.0, "findings": [],
                                             "reached": []})
        entry["score"] = max(entry["score"], a["score"])
        entry["reached"].append(a)

    out = []
    for path, e in sorted(files.items(), key=lambda kv: (kv[1]["rank"], -kv[1]["score"], kv[0])):
        why = []
        if e["findings"]:
            why.append(_listing(e["findings"], 2))
        if e["reached"]:
            why.append("the change reaches " + _listing(
                [f"{a['name']} ({_plural(a['hops'], 'hop')})" for a in e["reached"]], 2))
        out.append({"path": path, "why": "; ".join(why)})
    return out


def find_tests(snapshot: Snapshot, changed: list[Definition], near: list[Definition],
               touched: set[str] = frozenset()) -> list[dict]:
    """Test files to run, best first: ``[{"path", "why"}]``. Those the change edited, then those
    that mention a changed definition, then those that mention a closely reached one."""
    looks: dict[tuple[str, str], int] = {}       # (word, context) -> 0 changed, 1 reached
    held: dict[str, list[str]] = {}             # test file -> its tests the change reaches
    for tier, group in ((0, changed), (1, near)):
        for d in group:
            if d is None:
                continue
            if is_test_file(d.path):            # a test runs where it is: never "mentioned"
                held.setdefault(d.path, []).append(_look_for(d)[0])
                continue
            key = _look_for(d)
            looks[key] = min(looks.get(key, tier), tier)
    out = []
    for path in sorted(snapshot.files):
        if not is_test_file(path):
            continue
        text = snapshot.files[path]
        words = set(_TOKENS.findall(text))
        if "-" in text:
            words |= set(_COBOL_TOKENS.findall(text))
        hits: list[list[str]] = [[], []]
        for (word, context), tier in sorted(looks.items()):
            if word in words and (not context or context in words) and word not in hits[tier]:
                hits[tier].append(word)
        mine = sorted(set(held.get(path, ())))
        why = []
        if path in touched:
            why.append("this change edits it")
        if hits[0] or hits[1]:
            why.append("mentions " + _listing(hits[0] + hits[1], 4))
        if mine and path not in touched:
            why.append(f"the change reaches {_listing(mine, 2)} here")
        if not why:
            continue
        out.append(((path not in touched, not hits[0], -len(hits[0]), -len(hits[1] + mine),
                     path), {"path": path, "why": "; ".join(why)}))
    return [entry for _key, entry in sorted(out, key=lambda kv: kv[0])]


def is_test_file(path: str) -> bool:
    """A test file, by the names test runners look for. Python: ``test*.py`` (unittest) and
    ``*_test.py`` (pytest). The other languages: ``*Test.java``, ``*.spec.ts``, ``test_*.c``,
    or anything under a ``test``/``tests``/``__tests__`` folder (Maven's ``src/test``)."""
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    if name.endswith(".py"):
        return stem.startswith("test") or stem.endswith("_test")
    if any(part in ("test", "tests", "__tests__", "testing") for part in path.split("/")[:-1]):
        return True
    low = stem.lower()
    return low.startswith("test") or low.endswith(("test", "tests", ".spec", "_spec"))


def _look_for(d: Definition) -> tuple[str, str]:
    """``(word, context)``: the name a test would use for ``d``, and, when that name is too
    short or common to mean ``d`` alone, the class or module that must be named too."""
    word = (d.label or d.short).split("(", 1)[0]
    bare = d.name.split("@", 1)[-1].split("(", 1)[0]
    owner = bare.rsplit(".", 1)[0] if "." in bare else ""
    if d.kind == "method" and owner:
        context = owner.rsplit(".", 1)[-1]                  # the class
    elif d.path.endswith(".py"):
        context = module_name(d.path).rsplit(".", 1)[-1]    # the module
    else:
        context = d.path.rsplit("/", 1)[-1].split(".", 1)[0]
    if word.startswith("__") and word.endswith("__") or word.startswith("<"):   # __init__, <init>
        return context, ""
    if len(word) < 4 or word.lower() in _VAGUE:
        return word, context
    return word, ""


def _summary(report: Report, findings: list[Finding], counts: dict, other_files: int) -> str:
    if not report.changes and not findings:
        return (f"Nothing to check: no function, class or constant differs from "
                f"{report.against}." + _not_checked(counts["errors"]))
    sev = [f"{n} {s}" for s, n in counts["findings"].items() if n]
    found = (" and ".join([", ".join(sev[:-1]), sev[-1]] if len(sev) > 1 else sev)
             + f" finding{'' if len(findings) == 1 else 's'}") if sev else ""
    first = f"{findings[0].rule} at {findings[0].path}:{findings[0].line}" if findings else ""
    reach = ""
    if report.affected:
        reach = (f"; it reaches {_plural(len(report.affected), 'definition')} the change did "
                 f"not edit" + (f", in {_plural(other_files, 'other file')}" if other_files
                                else ""))
    changes = (f"{_plural(len(report.changes), 'change')} in "
               f"{_plural(len(report.files_changed), 'file')}")
    if report.verdict == "block":
        head = f"Do not commit yet: {found}, first {first}"
    elif report.verdict == "review":
        head = f"Review before committing: {found}, first {first}"
    elif findings:
        head = f"OK to commit after a look: {changes}, only {found} (first {first})"
    else:
        head = f"OK to commit as far as Magellan Lite can see: {changes}, nothing on the checklist"
    return head + reach + "." + _not_checked(counts["errors"])


def _not_checked(n: int) -> str:
    if not n:
        return ""
    return f" {n} {'file or rule' if n == 1 else 'files or rules'} could not be checked: see errors."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _listing(items: list[str], most: int) -> str:
    shown = ", ".join(items[:most])
    return shown + (f" and {len(items) - most} more" if len(items) > most else "")


def _cut(text: str, most: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= most else text[:most - 1].rstrip() + "…"


# -- the terminal ----------------------------------------------------------------------------

_OMITTED = {"changes": ("change", "changes"), "do_first": ("finding", "findings"),
            "reaches": ("reached definition", "reached definitions"),
            "check_these_files": ("file to check", "files to check"),
            "tests_to_run": ("test file", "test files"), "errors": ("error", "errors")}
_KIND = {"removed": "del", "renamed": "ren", "signature": "sig", "value": "val", "body": "body",
         "added": "new"}


def text(b: dict) -> str:
    """The brief in a terminal: terse, no colour, safe to paste into a prompt."""
    lines = [f"magellan-lite brief: {b['verdict'].upper()} · {b['summary']}"]
    if b["changes"]:
        lines.append("  changes")
        lines += [f"    {_KIND.get(c['kind'], c['kind']):>4}  {c['name']}  {c['where']}"
                  for c in b["changes"]]
    if b["do_first"]:
        lines.append("  do first")
        for i, f in enumerate(b["do_first"], 1):
            lines.append(f"    {i}. {f['severity'].upper()} {f['rule']}  {f['where']}")
            lines.append(f"       {f['what']}")
            if f["why"]:
                lines.append(f"       why: {f['why']}")
            if f["fix"]:
                lines.append(f"       fix: {f['fix']}")
    if b["reaches"]:
        lines.append("  reaches")
        lines += [f"    {a['score']:.2f}  {_plural(a['hops'], 'hop'):<7} {a['name']}  {a['where']}"
                  for a in b["reaches"]]
    if b["check_these_files"]:
        lines.append("  check these files (the change did not edit them)")
        lines += [f"    {f['path']}  ({f['why']})" for f in b["check_these_files"]]
    if b["tests_to_run"]:
        lines.append("  tests to run")
        lines += [f"    {t['path']}  ({t['why']})" for t in b["tests_to_run"]]
    if b["errors"]:
        lines.append("  not checked")
        lines += [f"    {e}" for e in b["errors"]]
    cut = [f"{n} more {_OMITTED[key][n != 1]}" for key, n in b["omitted"].items() if n]
    if cut:
        lines.append(f"  (not shown: {', '.join(cut)}; `magellan-lite check` lists everything)")
    return "\n".join(lines)
