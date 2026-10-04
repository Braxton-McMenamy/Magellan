"""Command line.

    magellan-lite check [PATH] [--against git:HEAD] [--format text|json|markdown] [--fail-on block]
    magellan-lite hook install [PATH] [--force]       check before every commit
    magellan-lite brief [PATH] [--against git:HEAD] [--format json|text] [--limit 8]
    magellan-lite plan  [PATH] --edit FILE OLD NEW | --edits JSON   before a change: its worklist
    magellan-lite progress [PATH]                     what is left of the plan
    magellan-lite done  [PATH]                        finished? exit 1 if not (plan.py)
    magellan-lite rules
    magellan-lite share [PATH] [--remote origin]      publish your work in progress
    magellan-lite team  [PATH] [--remote origin]      check it against your teammates'
    magellan-lite mcp   [PATH]                        the tools above, for an AI agent (MCP)

``check`` and ``brief`` exit 1 when the verdict reaches ``--fail-on`` (default ``block``), so
they can gate a commit; 2 when they cannot run (not a git repository, no commits yet).
``check`` reads the project's settings from pyproject.toml's ``[tool.magellan-lite]``
(settings.py); a flag on the command line beats them.
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
    c.add_argument("--format", choices=("text", "json", "markdown"), default="text",
                   help="markdown: the checklist as a GitHub task list, for a pull request")
    c.add_argument("--fail-on", choices=(*VERDICTS[1:], "never"), default=None,
                   help="exit 1 when the verdict is at least this (default: block, or fail-on "
                        "in pyproject.toml's [tool.magellan-lite])")
    c.add_argument("--map", action="store_true",
                   help="with --format json: add the code map the editor and website draw")

    b = sub.add_parser("brief", help="the check, cut down for an AI agent: the verdict, what "
                                     "to fix first, files to re-read, tests to run")
    b.add_argument("path", nargs="?", default=".", help="the project (default: here)")
    b.add_argument("--against", default="git:HEAD", metavar="BASELINE",
                   help="git:REV, or a directory holding the old version (default: git:HEAD)")
    b.add_argument("--format", choices=("json", "text"), default="json")
    b.add_argument("--limit", type=int, default=8, metavar="N",
                   help="entries per list (default: 8)")
    b.add_argument("--fail-on", choices=(*VERDICTS[1:], "never"), default="block",
                   help="exit 1 when the verdict is at least this (default: block)")

    pl = sub.add_parser("plan", help="before a change: the edits you intend, checked in memory; "
                                     "every other place that must change, with its code")
    pl.add_argument("path", nargs="?", default=".", help="the project (default: here)")
    pl.add_argument("--edit", nargs=3, action="append", default=[], metavar=("FILE", "OLD", "NEW"),
                    help="replace OLD (text in FILE now, exactly once) with NEW; repeat for more")
    pl.add_argument("--edits", metavar="JSON",
                    help="the edits as JSON, [{\"path\", \"old\", \"new\"}, ...]: inline, a file, "
                         "or - for stdin")
    pl.add_argument("--against", default="git:HEAD", metavar="BASELINE",
                    help="git:REV, or a directory holding the old version (default: git:HEAD)")
    pl.add_argument("--format", choices=("text", "json"), default="text")
    for name, what in (("progress", "what is left of the plan (planned edits not made yet are "
                                    "applied in memory)"),
                       ("done", "is the plan finished? if so, every line changed and what to "
                                "recompile, for the report")):
        st = sub.add_parser(name, help=what)
        st.add_argument("path", nargs="?", default=".", help="the project (default: here)")
        st.add_argument("--format", choices=("text", "json"), default="text")

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

    m = sub.add_parser("mcp", help="a Model Context Protocol server on stdin/stdout, so an AI "
                                   "agent can call brief, check, reach and the rest as tools")
    m.add_argument("path", nargs="?", default=".",
                   help="the project tools use when a call names none (default: here)")

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

    h = sub.add_parser("hook", help="run the check before every commit (git's pre-commit hook)")
    h.add_argument("action", choices=("install",))
    h.add_argument("path", nargs="?", default=".", help="the project (default: here)")
    h.add_argument("--force", action="store_true",
                   help="replace a pre-commit hook that is not Magellan Lite's (the old one is "
                        "kept as pre-commit.bak)")
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

    if args.command == "mcp":
        from magellan_lite.mcp import serve as serve_mcp
        return serve_mcp(args.path)

    if args.command == "hook":
        from magellan_lite.hook import install
        return install(args.path, args.force)

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

    if args.command in ("plan", "progress", "done"):
        from magellan_lite import plan
        try:
            if args.command == "plan":
                edits = _edits(args)
                result = plan.plan(args.path, edits, args.against)
            else:
                result = getattr(plan, args.command)(args.path)
        except (plan.PlanError, GitError, ValueError, OSError) as exc:
            print(f"magellan-lite: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2) if args.format == "json" else plan.text(result))
        return 0 if args.command != "done" or result["done"] else 1

    if args.command == "brief":
        from magellan_lite import brief
        try:
            b = brief.brief(args.path, args.against, args.limit)
        except (GitError, ValueError, OSError) as exc:
            print(f"magellan-lite: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(b, indent=2) if args.format == "json" else brief.text(b))
        return 1 if brief.fails(b, args.fail_on) else 0

    from magellan_lite import settings
    from magellan_lite.engine import check_snapshots, load_before
    from magellan_lite.output import markdown, text, wants_colour
    from magellan_lite.source import working_tree
    try:
        root = Path(args.path).resolve()
        project = settings.load(root)               # pyproject.toml's [tool.magellan-lite]
        before, after = load_before(root, args.against), working_tree(root)
        report = settings.apply(check_snapshots(before, after, root, args.against), project)
    except (GitError, ValueError, OSError) as exc:
        print(f"magellan-lite: {exc}", file=sys.stderr)
        return 2
    for warning in project.warnings:
        print(f"magellan-lite: {warning}", file=sys.stderr)
    if args.format == "json":
        out = report.to_dict()
        if args.map:
            from magellan_lite.web import PROJECT_NODES, code_map
            out["map"] = code_map(before, after, out, PROJECT_NODES, whole=True)   # the whole project, for 3D
        print(json.dumps(out, indent=2))
    elif args.format == "markdown":
        print(markdown(report))
    else:
        print(text(report, colour=wants_colour(sys.stdout)))
    return 1 if report.fails(args.fail_on or project.fail_on or "block") else 0


def _edits(args) -> list[dict]:
    """``--edit FILE OLD NEW`` (repeated) and ``--edits JSON`` (inline, a file, or -)."""
    from pathlib import Path
    edits = [{"path": f, "old": o, "new": n} for f, o, n in args.edit]   # several lines: --edits
    if args.edits:
        raw = sys.stdin.read() if args.edits == "-" else \
            args.edits if args.edits.lstrip()[:1] in "[{" else Path(args.edits).read_text("utf-8")
        given = json.loads(raw)
        edits += [given] if isinstance(given, dict) else list(given)
    return edits
