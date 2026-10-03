"""One check: map both versions, diff them, run the checklist on what the change touched."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from magellan_lite.defs import Definition, definitions
from magellan_lite.diff import Change, Spans, changed_spans, diff, in_spans
from magellan_lite.findings import Finding, iter_change_rules, run_file_rules
from magellan_lite.git import snapshot_at
from magellan_lite.report import Report
from magellan_lite.source import Snapshot, working_tree


@dataclass
class ChangeContext:
    """Everything a ``@change_rule`` can look at."""
    root: Path
    before: Snapshot
    after: Snapshot
    before_defs: dict[str, Definition]
    after_defs: dict[str, Definition]
    changes: list[Change]
    spans: Spans


def load_before(root: Path, against: str) -> Snapshot:
    """``git:REV`` (``git:`` alone means HEAD), or a directory holding the old version."""
    if against.startswith("git:"):
        return snapshot_at(root, against[4:] or "HEAD")
    old = Path(against)
    if old.is_dir():
        return working_tree(old)
    raise ValueError(f"--against must be git:REV or a directory, not {against!r}")


def check(root: str | Path = ".", against: str = "git:HEAD") -> Report:
    import magellan_lite.rules  # noqa: F401  importing the package registers every rule

    root = Path(root).resolve()
    before, after = load_before(root, against), working_tree(root)
    before_defs, after_defs = definitions(before), definitions(after)
    changes = diff(before_defs, after_defs)
    spans = changed_spans(changes)
    report = Report(root=root.as_posix(), against=against, changes=changes)

    for path in sorted(spans):
        tree = after.tree(path)
        if tree is None:
            continue
        found, errors = run_file_rules(tree, path)
        report.findings += [f for f in found if in_spans(spans, path, f.line)]
        report.errors += errors

    # TODO(qol): inline suppressions. A finding whose line (or the line above) carries
    #   `# magellan: ignore[rule-id]` is dropped here and counted instead ("1 suppressed").
    #   Done when: tests/test_engine.py has a case where the comment silences exactly one rule.

    # TODO(engine): the blast radius -- the headline feature. Build a call graph from the
    #   after snapshot (who calls whom: ast.Call nodes resolved through imports to
    #   Definition names), walk it backwards from each changed function, and fill
    #   report.affected with {"name", "path", "line", "hops", "score", "why"} (score fades with
    #   distance, e.g. 0.9 per hop). Port the idea, not the code, from magellan/core/propagate.py.
    #   Done when: on the sensor demo, collector.collect is listed as affected by the
    #   parse_record signature change, one hop away.

    ctx = ChangeContext(root, before, after, before_defs, after_defs, changes, spans)
    for r in iter_change_rules():
        try:
            report.findings += [f if f.fix or not r.fix else _with_fix(f, r.fix)
                                for f in r.check(ctx)]
        except Exception as exc:                    # noqa: BLE001 - reported, not hidden
            report.errors.append(f"rule {r.id} failed: {type(exc).__name__}: {exc}")

    report.errors += sorted(after.errors.values())
    report.findings.sort(key=lambda f: (f.rank, f.path, f.line, f.rule))
    return report


def _with_fix(f: Finding, fix: str) -> Finding:
    f.fix = fix
    return f
