"""How a report reads in a terminal. JSON is ``Report.to_dict``."""

from __future__ import annotations

from magellan_lite.report import Report

_KIND = {"removed": "del", "renamed": "ren", "signature": "sig", "value": "val", "body": "body",
         "added": "new"}


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def text(report: Report, max_changes: int = 12, max_affected: int = 10) -> str:
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

    if report.affected:
        lines += ["", f"  reaches {_count(len(report.affected), 'definition')} the change "
                      f"did not touch (score fades with distance)"]
        width = max(len(a["name"]) for a in report.affected[:max_affected])
        for i, a in enumerate(report.affected[:max_affected]):
            lines.append(f"    {a['score']:.2f}  {_count(a['hops'], 'hop'):<7} "
                         f"{a['name']:<{width}}  {a['path']}:{a['line']}")
            if i < 3:
                lines.append(f"          because {a['why']}")
        if len(report.affected) > max_affected:
            lines.append(f"    ... and {len(report.affected) - max_affected} more")

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
