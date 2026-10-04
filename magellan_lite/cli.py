"""Command line.

    magellan-lite check [PATH] [--against git:HEAD] [--format text|json] [--fail-on block]
    magellan-lite rules

``check`` exits 1 when the verdict reaches ``--fail-on`` (default ``block``), so it can gate
a commit; 2 when it cannot run (not a git repository, no commits yet).
"""

from __future__ import annotations

import argparse
import json
import sys

from magellan_lite import __version__
from magellan_lite.git import GitError
from magellan_lite.report import VERDICTS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="magellan-lite",
        description="What your change touched, and the checklist of what it could break.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="compare the working tree with a baseline and list what "
                                     "to check")
    c.add_argument("path", nargs="?", default=".", help="the project (default: here)")
    c.add_argument("--against", default="git:HEAD", metavar="BASELINE",
                   help="git:REV, or a directory holding the old version (default: git:HEAD)")
    c.add_argument("--format", choices=("text", "json"), default="text")
    c.add_argument("--fail-on", choices=(*VERDICTS[1:], "never"), default="block",
                   help="exit 1 when the verdict is at least this (default: block)")

    sub.add_parser("rules", help="list the checklist rules")

    s = sub.add_parser("serve", help="the website and its live API on this computer")
    s.add_argument("--host", default="127.0.0.1",
                   help="127.0.0.1 (default) is this computer only; 0.0.0.0 lets other "
                        "machines on the network (or a Tailscale tailnet) reach it")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--open", action="store_true", help="open the website in the browser")

    # TODO(qol): `--format markdown`: the checklist as a GitHub task list (`- [ ] **HIGH** ...`)
    #   to paste into a PR. Write it in output.py next to text(). Done when a test checks that
    #   every finding becomes one `- [ ]` line.
    # TODO(qol): `magellan-lite hook install`: write .git/hooks/pre-commit running
    #   `magellan-lite check --fail-on block` (write it with newline="\n": sh wants LF on
    #   Windows too). Done when committing a blocking change in a test repo is refused.
    # TODO(qol): settings from pyproject.toml `[tool.magellan-lite]`: fail-on, disabled rules,
    #   excluded paths. Python 3.11+ has tomllib; on 3.10 fall back to defaults.
    # TODO(qol): colour in the terminal (verdict in red/yellow/green), off when NO_COLOR is
    #   set or output is not a terminal.
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # a Windows pipe defaults to the system code page, which cannot print every character
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)

    if args.command == "rules":
        import magellan_lite.rules  # noqa: F401
        from magellan_lite.findings import RULES
        for r in sorted(RULES.values(), key=lambda r: r.id):
            kind = "file" if r.per_file else "change"
            print(f"{r.id:<34} {r.severity:<8} {kind:<6} {'blocking' if r.blocking else ''}")
        return 0

    if args.command == "serve":
        from magellan_lite.server import serve
        try:
            return serve(args.host, args.port, args.open)
        except OSError as exc:                       # the port is taken, usually
            print(f"magellan-lite: cannot listen on {args.host}:{args.port}: {exc}",
                  file=sys.stderr)
            return 2

    from magellan_lite.engine import check
    from magellan_lite.output import text
    try:
        report = check(args.path, args.against)
    except (GitError, ValueError, OSError) as exc:
        print(f"magellan-lite: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), indent=2) if args.format == "json" else text(report))
    return 1 if report.fails(args.fail_on) else 0
