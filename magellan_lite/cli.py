"""Command line.

    magellan-lite check [PATH] [--against git:HEAD] [--format text|json] [--fail-on block]
    magellan-lite rules
    magellan-lite share [PATH] [--remote origin]      publish your work in progress
    magellan-lite team  [PATH] [--remote origin]      check it against your teammates'

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
    c.add_argument("--map", action="store_true",
                   help="with --format json: add the code map the editor and website draw")

    sub.add_parser("rules", help="list the checklist rules")

    sh = sub.add_parser("share", help="publish your work in progress (no commit, no branch) "
                                      "for your teammates' `team` checks")
    sh.add_argument("path", nargs="?", default=".")
    sh.add_argument("--remote", default="origin")
    sh.add_argument("--name", help="your name under refs/wip/ (default: git's user.name)")

    tm = sub.add_parser("team", help="check your work against your teammates' shared work in "
                                     "progress: problems only the combination has")
    tm.add_argument("path", nargs="?", default=".")
    tm.add_argument("--remote", default="origin")
    tm.add_argument("--name", help="your own name, so your own share is skipped")
    tm.add_argument("--format", choices=("text", "json"), default="text")
    tm.add_argument("--fail-on", choices=(*VERDICTS[1:], "never"), default="block",
                    help="exit 1 when any combination's verdict is at least this")

    s = sub.add_parser("serve", help="the website and its live API on this computer")
    s.add_argument("--host", default="127.0.0.1",
                   help="127.0.0.1 (default) is this computer only; 0.0.0.0 lets other "
                        "machines on the network (or a Tailscale tailnet) reach it")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--open", action="store_true", help="open the website in the browser")
    s.add_argument("--public-host", action="append", default=[], metavar="NAME",
                   help="a public host name an HTTPS proxy forwards to this server (e.g. a "
                        "Tailscale Funnel name); only this computer's names and these are answered")
    s.add_argument("--allow-origin", action="append", default=None, metavar="URL",
                   help="a website allowed to call the API from a browser (default: "
                        "https://magellan-code.pages.dev); repeat for more")
    s.add_argument("--behind-proxy", action="store_true",
                   help="trust X-Forwarded-For from a proxy on this computer, so rate limits "
                        "count each real client")

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
            return serve(args.host, args.port, args.open, public_hosts=tuple(args.public_host),
                         origins=args.allow_origin, behind_proxy=args.behind_proxy)
        except OSError as exc:                       # the port is taken, usually
            print(f"magellan-lite: cannot listen on {args.host}:{args.port}: {exc}",
                  file=sys.stderr)
            return 2

    if args.command == "share":
        from magellan_lite.team import share
        try:
            ref, commit = share(args.path, args.name, args.remote)
        except (GitError, OSError) as exc:
            print(f"magellan-lite: {exc}", file=sys.stderr)
            return 2
        print(f"magellan-lite: shared your work in progress as {ref} ({commit[:7]}) on "
              f"{args.remote}. Your branch is untouched.")
        return 0

    if args.command == "team":
        from magellan_lite import team
        try:
            results = team.team(args.path, args.remote, args.name)
        except (GitError, OSError) as exc:
            print(f"magellan-lite: {exc}", file=sys.stderr)
            return 2
        print(json.dumps([c.to_dict() for c in results], indent=2) if args.format == "json"
              else team.text(results))
        return 1 if any(c.report and c.report.fails(args.fail_on) for c in results) else 0

    from pathlib import Path

    from magellan_lite.engine import check_snapshots, load_before
    from magellan_lite.output import text
    from magellan_lite.source import working_tree
    try:
        root = Path(args.path).resolve()
        before, after = load_before(root, args.against), working_tree(root)
        report = check_snapshots(before, after, root, args.against)
    except (GitError, ValueError, OSError) as exc:
        print(f"magellan-lite: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        out = report.to_dict()
        if args.map:
            from magellan_lite.web import code_map
            out["map"] = code_map(before, after, out)
        print(json.dumps(out, indent=2))
    else:
        print(text(report))
    return 1 if report.fails(args.fail_on) else 0
