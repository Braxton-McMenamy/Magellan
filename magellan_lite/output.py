"""How a report reads in a terminal. JSON is ``Report.to_dict``."""

from __future__ import annotations

from magellan_lite.report import Report

_KIND = {"removed": "del", "signature": "sig", "value": "val", "body": "body", "added": "new"}

# TODO(engine): once report.affected is filled, print a "reaches" section between the
#   changes and the checklist: score, hops and name of each affected definition, worst first.


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def text(report: Report, max_changes: int = 12) -> str:
    lines = [f"magellan-lite: {report.verdict.upper()} · {_count(len(report.findings), 'finding')}"
             f" · {_count(len(report.changes), 'change')} in "
             f"{_count(len(report.files_changed), 'file')} (against {report.against})"]

    if report.changes:
        lines += ["", "  changes"]
        width = max(len(c.name) for c in report.changes[:max_changes])
        for c in report.changes[:max_changes]:
            lines.append(f"    {_KIND[c.kind]:>4}  {c.name:<{width}}  {c.path}:{c.line}")
        if len(report.changes) > max_changes:
            lines.append(f"    ... and {len(report.changes) - max_changes} more")

    lines += ["", "  checklist"]
    if not report.findings:
        lines.append("    nothing to check in what this change touched")
    for f in report.findings:
        lines.append(f"    [ ] {f.severity.upper():<8} {f.rule}  {f.path}:{f.line}")
        lines.append(f"        {f.message}")
        if f.detail:
            lines.append(f"        {f.detail}")
        if f.fix:
            lines.append(f"        fix: {f.fix}")

    if report.errors:
        lines += ["", "  not checked"]
        lines += [f"    {e}" for e in report.errors]
    return "\n".join(lines)
