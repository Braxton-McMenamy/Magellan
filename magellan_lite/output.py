"""How a report reads in a terminal (``text``) and in a pull request (``markdown``). JSON is
``Report.to_dict``."""

from __future__ import annotations

import os
import re

from magellan_lite.report import Report

_KIND = {"removed": "del", "renamed": "ren", "signature": "sig", "value": "val", "body": "body",
         "added": "new"}

#: the verdict's colour in a terminal: ANSI bold red, yellow, green
_COLOUR = {"block": "1;31", "review": "1;33", "ok": "1;32"}


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _summary(report: Report, verdict: str) -> str:
    """``REVIEW · 1 finding · 1 suppressed · 2 changes in 1 file``"""
    return (f"{verdict} · {_count(len(report.findings), 'finding')}"
            + (f" · {report.suppressed} suppressed" if report.suppressed else "")
            + f" · {_count(len(report.changes), 'change')} in "
              f"{_count(len(report.files_changed), 'file')}")


def wants_colour(stream, environ=None) -> bool:
    """Colour only for a person at a terminal: never when ``NO_COLOR`` is set (no-color.org),
    into a pipe or a file, or on a dumb terminal."""
    environ = os.environ if environ is None else environ
    if environ.get("NO_COLOR") or environ.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    if not (isatty and isatty()):
        return False
    return os.name != "nt" or _windows_console_colour()


def _windows_console_colour() -> bool:
    """Ask the Windows console to read colour codes (Windows 10 and later can); False when
    it will not, so the codes are never printed as junk."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)                 # standard output
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        vt = 0x0004                                         # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(mode.value & vt or kernel32.SetConsoleMode(handle, mode.value | vt))
    except (AttributeError, OSError, ValueError):
        return False


def text(report: Report, max_changes: int = 12, max_affected: int = 10,
         colour: bool = False) -> str:
    verdict = report.verdict.upper()
    if colour:
        verdict = f"\033[{_COLOUR[report.verdict]}m{verdict}\033[0m"
    lines = [f"magellan-lite: {_summary(report, verdict)} (against {report.against})"]

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


_VERDICT_MEANS = {
    "block": "A blocking finding: as it stands, this change breaks something.",
    "review": "Nothing blocking, but something here needs a person to look.",
    "ok": "Nothing in what this change touched needs a person.",
}


def _md(s: str) -> str:
    """Plain text for markdown: `code spans` stay code; outside them, characters markdown
    would read as formatting (``__init__`` turning bold, ``<x>`` vanishing) are escaped."""
    parts = s.split("`")
    if len(parts) % 2 == 0:                     # an unpaired backtick: no code spans at all
        return re.sub(r"([\\`*_<>\[\]])", r"\\\1", s)
    return "`".join(re.sub(r"([\\*_<>\[\]])", r"\\\1", p) if i % 2 == 0 else p
                    for i, p in enumerate(parts))


def markdown(report: Report, max_changes: int = 30, max_affected: int = 15) -> str:
    """The report as GitHub markdown, to paste into a pull request: the checklist is a task
    list, one ``- [ ]`` line per finding. The pull request bot
    (.github/workflows/magellan-lite.yml) posts it as a comment."""
    lines = [f"### Magellan Lite: {_summary(report, '**' + report.verdict.upper() + '**')}",
             "",
             f"{_VERDICT_MEANS[report.verdict]} Checked against `{report.against}`.",
             ""]

    if not report.findings:
        lines.append("Nothing to check in what this change touched.")
    for f in report.findings:
        lines.append(f"- [ ] **{f.severity.upper()}** `{f.path}:{f.line}` {_md(f.message)} "
                     f"(`{f.rule}`)")
        if f.detail:
            lines.append(f"  - {_md(f.detail)}")
        if f.fix:
            lines.append(f"  - Fix: {_md(f.fix)}")

    def details(summary: str, body: list[str]) -> None:
        lines.extend(["", f"<details><summary>{summary}</summary>", "", *body, "", "</details>"])

    if report.changes:
        body = [f"- {c.kind} `{c.name}` in `{c.path}:{c.line}`"
                for c in report.changes[:max_changes]]
        if len(report.changes) > max_changes:
            body.append(f"- ... and {len(report.changes) - max_changes} more")
        details(f"What changed: {_count(len(report.changes), 'change')} in "
                f"{_count(len(report.files_changed), 'file')}", body)

    if report.affected:
        body = [f"- `{a['name']}` in `{a['path']}:{a['line']}`, "
                f"{_count(a['hops'], 'hop')} away: {_md(a.get('why', ''))}"
                for a in report.affected[:max_affected]]
        if len(report.affected) > max_affected:
            body.append(f"- ... and {len(report.affected) - max_affected} more")
        details(f"What it reaches: {_count(len(report.affected), 'definition')} the change "
                f"did not touch", body)

    if report.errors:
        details(f"Not checked: {len(report.errors)}", [f"- {_md(e)}" for e in report.errors])
    return "\n".join(lines)
