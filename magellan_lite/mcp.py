"""Magellan Lite as tools an AI agent can call: a Model Context Protocol (MCP) server.

    magellan-lite mcp [PATH]
    claude mcp add magellan-lite -- python -m magellan_lite mcp

It speaks MCP over stdin and stdout: newline-delimited JSON-RPC 2.0, one message per line,
standard library only. It answers ``initialize``, ``ping``, ``tools/list`` and ``tools/call``;
other requests get JSON-RPC's "method not found", and notifications get no answer. Nothing but
protocol messages is written to stdout: anything else a tool prints goes to stderr.

Every tool takes an optional ``path``: the project's root folder (default: the PATH the server
was started with, else its working directory). Each returns JSON text. A call that cannot be
answered (no such definition, not a git repository) comes back as a tool result with
``isError`` and a JSON ``{"error": ...}`` saying why in plain words; an unknown tool or a
malformed request is a JSON-RPC error.

``magellan_lite_brief``   after an edit: ok / review / block, what to fix first, files to
                          re-read, tests to run (brief.py)
``magellan_lite_check``   the full report behind the brief, optionally with the code map
``magellan_lite_reach``   who depends on a definition: its callers and theirs, hop by hop
``magellan_lite_node``    one definition: where it is, its signature, both sides of its edges
``magellan_lite_unused``  definitions nothing in the project mentions: probably dead code
``magellan_lite_team``    your work against your teammates' shared work in progress
``magellan_lite_rules``   the checklist

``reach``, ``node`` and ``unused`` read the working tree only (no git needed) and keep the
last project's map until a file changes, so asking about several functions in a row is quick.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from magellan_lite import __version__
from magellan_lite.git import GitError

#: the protocol versions this server can speak (it uses nothing that differs between them)
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
#: what it answers a client that names no version (the full Magellan's choice)
PROTOCOL = "2024-11-05"

INSTRUCTIONS = (
    "Magellan Lite reads a project's code (Python, Java, C, C++, Fortran, COBOL, TypeScript) "
    "without running it. Before changing a function, especially old code, call "
    "magellan_lite_reach to see what depends on it. After editing, before committing or "
    "saying you are done, call magellan_lite_brief: on block, fix do_first and call it again; "
    "re-read check_these_files and run tests_to_run.")


class ToolError(Exception):
    """A call the tool cannot answer, said plainly; ``extra`` goes into the JSON."""

    def __init__(self, message: str, **extra) -> None:
        super().__init__(message)
        self.extra = extra


# -- the project, mapped once per state of its files ------------------------------------------

@dataclass
class Project:
    """The working tree's definitions and call graph."""
    root: Path
    snapshot: object
    defs: dict
    graph: object
    _unused: set | None = field(default=None, repr=False)

    @property
    def unused(self) -> set[str]:
        if self._unused is None:
            from magellan_lite.web import _unmentioned
            self._unused = _unmentioned(self.snapshot, self.defs)
        return self._unused


_LAST: dict[Path, tuple[tuple, Project]] = {}


def project(root: Path) -> Project:
    """The project as it is on disk now; mapped again only when a source file changed."""
    from magellan_lite.defs import definitions
    from magellan_lite.graph import build_graph
    from magellan_lite.source import Snapshot, decode, iter_source_files

    files = iter_source_files(root)
    stamp = []
    for rel in files:                           # look before reading: an edit made meanwhile
        st = os.stat(root / rel)                # shows up as a new stamp on the next call
        stamp.append((rel, st.st_mtime_ns, st.st_size))
    hit = _LAST.get(root)
    if hit and hit[0] == tuple(stamp):
        return hit[1]
    snap = Snapshot({rel: decode((root / rel).read_bytes()) for rel in files}, label=str(root))
    defs = definitions(snap)
    p = Project(root, snap, defs, build_graph(snap, defs))
    _LAST.clear()                               # one project at a time: one agent, one repo
    _LAST[root] = (tuple(stamp), p)
    return p


# -- the tools ---------------------------------------------------------------------------------

_PATH = {"type": "string",
         "description": "The project's root folder (default: the folder the server was "
                        "started for)."}
_AGAINST = {"type": "string",
            "description": "The baseline to compare with: git:REV (default git:HEAD, the last "
                           "commit) or a folder holding the old version."}
_NAME = {"type": "string",
         "description": "The definition: a qualified name (billing.ledger.post, "
                        "billing.Ledger.post, c@parse.load, java@geo.Shape.area(double,double)), "
                        "a short name (post) that the tool resolves, or path:line."}


def _limit(default: int) -> dict:
    return {"type": "integer", "minimum": 1, "description": f"Entries per list (default {default})."}


def _int(a: dict, key: str, default: int, lo: int = 1, hi: int = 10_000) -> int:
    v = a.get(key, default)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int):
        raise ToolError(f"{key} must be a whole number, not {v!r}")
    return max(lo, min(hi, v))


def _str(a: dict, key: str, default: str = "") -> str:
    v = a.get(key)
    if v is None or v == "":
        return default
    if not isinstance(v, str):
        raise ToolError(f"{key} must be a string, not {v!r}")
    return v


def _against(root: Path, a: dict) -> str:
    """``git:REV``, or a folder (relative to the project) holding the old version."""
    against = _str(a, "against", "git:HEAD")
    if against.startswith("git:"):
        return against
    folder = (root / against).resolve()
    if not folder.is_dir():
        raise ToolError(f"against must be git:REV (git:HEAD, git:main, ...) or a folder holding "
                        f"the old version; {against!r} is neither")
    return str(folder)


def _brief(root: Path, a: dict):
    from magellan_lite.brief import LIMIT, brief
    return brief(root, _against(root, a), _int(a, "limit", LIMIT))


def _check(root: Path, a: dict):
    from magellan_lite.engine import check_snapshots, load_before
    from magellan_lite.source import working_tree
    against = _against(root, a)
    before, after = load_before(root, against), working_tree(root)
    out = check_snapshots(before, after, root, against).to_dict()
    if a.get("map"):
        from magellan_lite.web import code_map
        out["map"] = code_map(before, after, out)
    return out


def _reach(root: Path, a: dict):
    from magellan_lite.brief import NEAR, find_tests
    from magellan_lite.diff import Change
    from magellan_lite.radius import MAX_HOPS, blast_radius

    p = project(root)
    d = resolve(p, _str(a, "name"))
    depth = _int(a, "depth", MAX_HOPS, 1, MAX_HOPS)
    limit = _int(a, "limit", 40)
    # scored as `check` scores the usual edit: a new signature (a new value, for a constant)
    kind = "value" if d.kind == "constant" else "signature"
    hit = [x for x in blast_radius([Change(kind, d.name, d.path, d.line, before=d, after=d)],
                                   p.graph, p.defs) if x["hops"] <= depth]
    shown = hit[:limit]
    hops = []
    for n in sorted({x["hops"] for x in shown}):
        hops.append({"hop": n, "definitions": [
            {"name": x["name"], "kind": p.defs[x["name"]].kind if x["name"] in p.defs else "",
             "where": f"{x['path']}:{x['line']}", "score": x["score"], "via": x["why"]}
            for x in shown if x["hops"] == n]})
    files: dict[str, dict] = {}
    for x in hit:
        f = files.setdefault(x["path"], {"path": x["path"], "definitions": 0, "nearest_hop": 99})
        f["definitions"] += 1
        f["nearest_hop"] = min(f["nearest_hop"], x["hops"])
    imported = sorted({f"{i.path}:{i.line}" for i in p.graph.imports if i.target == d.name})
    direct = sum(x["hops"] == 1 for x in hit)
    if hit:
        summary = (f"{_n(len(hit), 'definition')} in {_n(len(files), 'file')} "
                   f"depend{'s' if len(hit) == 1 else ''} on {d.name}; {direct} "
                   f"use{'s' if direct == 1 else ''} it directly.")
    else:
        summary = (f"Nothing in this project calls or reads {d.name}"
                   + (f" (it is imported at {', '.join(imported[:3])})" if imported else "")
                   + ": see note for what a code reading cannot see.")
    near = [p.defs[x["name"]] for x in hit if x["score"] >= NEAR and x["name"] in p.defs]
    files_ranked = sorted(files.values(), key=lambda f: (f["nearest_hop"], -f["definitions"],
                                                         f["path"]))
    tests = find_tests(p.snapshot, [d], near)
    return {
        "target": _card(d),
        "summary": summary,
        "hops": hops,
        "files": files_ranked[:limit],
        "imported_at": imported[:limit],
        "tests": tests[:limit],
        "omitted": {"definitions": max(0, len(hit) - len(shown)),
                    "files": max(0, len(files_ranked) - limit),
                    "imported_at": max(0, len(imported) - limit),
                    "tests": max(0, len(tests) - limit)},
        "note": "Found by reading the code, not running it: a call through a computed name "
                "(getattr, reflection, a function pointer, a dynamic COBOL CALL) or from "
                "outside this project is not seen. A 'via' ending in 'a guess' was matched by "
                "method name alone. Scores fade with each hop (a call x0.9).",
    }


def _node(root: Path, a: dict):
    p = project(root)
    d = resolve(p, _str(a, "name"))
    limit = _int(a, "limit", 30)

    def group(edges, other) -> list[dict]:
        by: dict[tuple, dict] = {}
        for e in edges:
            entry = by.setdefault((other(e), e.kind), {"name": other(e), "how": e.kind,
                                                       "at": [], "guess": e.guess})
            entry["guess"] = entry["guess"] and e.guess
            site = f"{e.path}:{e.line}"
            if site not in entry["at"]:
                entry["at"].append(site)
        return sorted(by.values(), key=lambda x: (x["guess"], x["name"], x["how"]))

    called_by = group(p.graph.callers(d.name), lambda e: e.src)
    calls = group(p.graph.uses(d.name), lambda e: e.dst)
    stem = d.name.split("(", 1)[0]
    members = sorted(n for n in p.defs if n.startswith(stem + ".") and n != d.name
                     and "." not in n[len(stem) + 1:].split("(", 1)[0])
    owner = stem.rsplit(".", 1)[0]
    out = _card(d)
    out.update({
        "end_line": d.end_line,
        "member_of": owner if owner in p.defs else "",
        "members": members[:limit],
        "called_by": called_by[:limit],
        "calls": calls[:limit],
        "imported_at": sorted({f"{i.path}:{i.line}" for i in p.graph.imports
                               if i.target == d.name})[:limit],
        "probably_unused": d.name in p.unused,
        "counts": {"called_by": len(called_by), "calls": len(calls), "members": len(members)},
    })
    return out


_UNUSED_CAVEAT = (
    "Probably dead code: each name appears nowhere in the project except where it is defined. "
    "It can still be used through a computed name (getattr, reflection, a plugin registry, a "
    "dynamic COBOL CALL), by code outside this project (a library's users, a script, a "
    "scheduled job), or by a framework convention. Check each one before deleting it. "
    "Decorated definitions, dunder methods, subclasses and tests are left out.")


def _unused(root: Path, a: dict):
    p = project(root)
    under = _str(a, "under").replace("\\", "/").strip("/")
    limit = _int(a, "limit", 50)
    names = [n for n in p.unused
             if not under or p.defs[n].path == under or p.defs[n].path.startswith(under + "/")]
    names.sort(key=lambda n: (p.defs[n].path, p.defs[n].line))
    return {"count": len(names), "caveat": _UNUSED_CAVEAT,
            "definitions": [{k: v for k, v in _card(p.defs[n]).items()
                             if k in ("name", "kind", "lang", "where")} for n in names[:limit]],
            "omitted": max(0, len(names) - limit)}


def _team(root: Path, a: dict):
    from magellan_lite import team
    from magellan_lite.report import VERDICTS
    remote = _str(a, "remote", "origin")
    try:                                        # git's own words for these are no help
        remotes = team._git(root, "remote").split()
    except GitError:
        raise ToolError(f"{root.as_posix()} is not in a git repository. The team check reads "
                        "teammates' shared work in progress from a git remote, so it only "
                        "works inside a clone that teammates share to.") from None
    if remote not in remotes:
        raise ToolError(f"this repository has no remote named {remote!r}"
                        + (f" (it has {', '.join(remotes)}: pass one as remote)" if remotes
                           else " (it has none)")
                        + ". The team check reads teammates' work in progress from the remote "
                          "they run `magellan-lite share` against.")
    results = team.team(root, remote, _str(a, "name") or None)
    verdict = max((c.verdict for c in results), key=VERDICTS.index, default="ok")
    if not results:
        summary = ("Nobody else has shared work in progress on this remote yet (teammates run "
                   "`magellan-lite share`), so there is nothing to combine.")
    else:
        bad = [c.name for c in results if c.verdict != "ok"]
        whose = "1 teammate's" if len(results) == 1 else f"{len(results)} teammates'"
        summary = (f"Your work against {whose} shared work in progress: "
                   + (f"problems only the combination has with {', '.join(bad)}." if bad
                      else "fine together."))
    return {"verdict": verdict, "summary": summary,
            "teammates": [c.to_dict() for c in results]}


def _rules(root: Path, a: dict):
    import magellan_lite.rules  # noqa: F401  registers every rule
    from magellan_lite.findings import RULES
    return {"rules": [{"id": r.id, "severity": r.severity, "blocking": r.blocking,
                       "kind": "file" if r.per_file else "change", "fix": r.fix}
                      for r in sorted(RULES.values(), key=lambda r: r.id)],
            "note": "A file rule looks at each edited definition; a change rule at the whole "
                    "change and the code it reaches. A blocking rule's high or critical "
                    "finding makes the verdict block; any medium or worse makes it review."}


_BASELINE_HELP = ("Magellan Lite compares the working tree with a baseline. With against=git:REV "
                  "(the default is git:HEAD) the project must be in a git repository with at "
                  "least one commit, and REV must exist; otherwise pass against as a folder "
                  "holding the old version.")

TOOLS: dict[str, dict] = {
    "magellan_lite_brief": {
        "description": "Call this after editing code, before committing or saying you are "
                       "done. Compares the working tree with the last commit (or `against`) and "
                       "returns a short JSON verdict (ok / review / block) with do_first (the "
                       "findings to fix, each with path:line and a fix), check_these_files "
                       "(files you did not edit that your change reaches), and tests_to_run. On "
                       "block, fix do_first and call it again.",
        "properties": {"path": _PATH, "against": _AGAINST, "limit": _limit(8)},
        "run": _brief, "git_help": _BASELINE_HELP,
    },
    "magellan_lite_check": {
        "description": "The full report behind the brief: every changed definition, every "
                       "finding, the whole blast radius (affected) and anything not checked "
                       "(errors). Use it when the brief's lists were cut (its omitted counts) or "
                       "you need everything; set map for the code map's nodes and edges.",
        "properties": {"path": _PATH, "against": _AGAINST,
                       "map": {"type": "boolean",
                               "description": "Add the code map (nodes and edges) the editor "
                                              "and website draw."}},
        "run": _check, "git_help": _BASELINE_HELP,
    },
    "magellan_lite_reach": {
        "description": "Before changing a function, method, class or constant (especially old "
                       "code you are modernizing), call this to see what depends on it: its "
                       "callers, their callers, hop by hop, each with the call site that links "
                       "it, the files they are in, and tests that mention them. An ambiguous "
                       "short name returns the candidates to choose from.",
        "properties": {"path": _PATH, "name": _NAME,
                       "depth": {"type": "integer", "minimum": 1,
                                 "description": "Hops to follow (default and most: 6). 1 lists "
                                                "the direct callers only."},
                       "limit": _limit(40)},
        "required": ["name"], "run": _reach,
    },
    "magellan_lite_node": {
        "description": "One definition up close: where it is, its language and signature, who "
                       "calls or reads it and what it calls or reads (with the call sites), "
                       "where it is imported, and its members if it is a class. Use it to "
                       "understand code before editing it, or to walk the call graph a step at "
                       "a time.",
        "properties": {"path": _PATH, "name": _NAME, "limit": _limit(30)},
        "required": ["name"], "run": _node,
    },
    "magellan_lite_unused": {
        "description": "Definitions whose name appears nowhere else in the project: probably "
                       "dead code, each with path:line. Use it before modernizing, to skip or "
                       "remove code nothing uses, and check each one first: a call by a "
                       "computed name, reflection, or a user outside the project is not seen.",
        "properties": {"path": _PATH,
                       "under": {"type": "string",
                                 "description": "Only definitions in this folder or file, "
                                                "relative to the project root."},
                       "limit": _limit(50)},
        "run": _unused,
    },
    "magellan_lite_team": {
        "description": "Your uncommitted work checked against each teammate's shared work in "
                       "progress (published with `magellan-lite share` to refs/wip/* on the git "
                       "remote): only the problems the combination has, such as their new call "
                       "to a function you changed. Only meaningful in a git repository whose "
                       "remote teammates share to; it fetches from that remote.",
        "properties": {"path": _PATH,
                       "remote": {"type": "string",
                                  "description": "The git remote (default origin)."},
                       "name": {"type": "string",
                                "description": "Your own name under refs/wip/, so your own "
                                               "share is skipped (default: git's user.name)."}},
        "run": _team,
        "git_help": "The team check needs a git repository with a remote where teammates "
                    "publish their work in progress with `magellan-lite share`.",
    },
    "magellan_lite_rules": {
        "description": "The checklist Magellan Lite runs on a change: each rule's id, severity, "
                       "whether it can block, and its usual fix. Use it to understand a rule id "
                       "seen in a finding.",
        "properties": {"path": _PATH},
        "run": _rules,
    },
}


# -- finding a definition by name --------------------------------------------------------------

def resolve(p: Project, query: str):
    """The definition ``query`` names: exact, then without the language prefix or Java's
    parameter list, then by its last dotted parts, then by its short name, then ignoring case
    (COBOL). ``path:line`` names the innermost definition there. Raises ``ToolError`` with the
    candidates when the name is ambiguous, and close names when it is unknown."""
    q = query.strip()
    if not q:
        raise ToolError("name is empty: give a definition's name (pkg.mod.func, or a short "
                        "name) or path:line")
    m = re.fullmatch(r"(.+):(\d+)", q)
    if m and m.group(1).replace("\\", "/") in p.snapshot.files:
        path, line = m.group(1).replace("\\", "/"), int(m.group(2))
        here = [d for d in p.defs.values() if d.path == path and d.line <= line <= d.end_line]
        if here:
            return min(here, key=lambda d: d.end_line - d.line)
        raise ToolError(f"no function, class or constant around {path}:{line}")

    def bare(n: str) -> str:
        return n.split("@", 1)[-1]

    def stem(n: str) -> str:
        return bare(n).split("(", 1)[0]

    low = q.lower()
    tiers = (
        lambda n, d: n == q,
        lambda n, d: bare(n) == q,
        lambda n, d: stem(n) == q,
        lambda n, d: stem(n).endswith("." + q) or bare(n).endswith("." + q),
        lambda n, d: (d.label or d.short).split("(", 1)[0] == q,
        lambda n, d: stem(n).lower() == low or stem(n).lower().endswith("." + low),
    )
    for match in tiers:
        found = sorted((d for n, d in p.defs.items() if match(n, d)),
                       key=lambda d: (d.path, d.line))
        if len(found) == 1:
            return found[0]
        if found:
            raise ToolError(
                f"{q!r} matches {len(found)} definitions: call again with one of these names",
                candidates=[{k: v for k, v in _card(d).items() if k in ("name", "kind", "where")}
                            for d in found[:20]],
                more=max(0, len(found) - 20))
    words = {stem(n) for n in p.defs} | {stem(n).rsplit(".", 1)[-1] for n in p.defs}
    close = difflib.get_close_matches(q, sorted(words), n=6, cutoff=0.6)
    raise ToolError(f"no function, method, class or constant named {q!r} in {p.root.as_posix()}",
                    did_you_mean=close)


def _card(d) -> dict:
    from magellan_lite.languages import NAMES
    lang = d.lang or "python"
    out = {"name": d.name, "kind": d.kind, "lang": NAMES.get(lang, lang),
           "where": f"{d.path}:{d.line}",
           "signature": re.sub(r"\s*#[0-9a-f]{8,}$", "", d.signature)}
    if d.kind == "constant" and d.value and not d.lang:
        out["value"] = d.value if len(d.value) <= 200 else d.value[:199] + "…"
    return out


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# -- the protocol -------------------------------------------------------------------------------

def _reply(id_, result) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


class Server:
    """The tools for one default project; ``handle`` takes one JSON-RPC message."""

    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()

    def handle(self, msg) -> dict | None:
        """One message in, at most one out: notifications and stray responses get none."""
        if not isinstance(msg, dict):
            return _error(None, -32600, "invalid request: expected a JSON object")
        if "method" not in msg and ("result" in msg or "error" in msg):
            return None                             # a response to a request we never sent
        method, id_ = msg.get("method"), msg.get("id")
        if not isinstance(method, str) or msg.get("jsonrpc") not in ("2.0", None):
            return _error(id_, -32600, "invalid request: a JSON-RPC 2.0 request has a "
                                       "string 'method'")
        if "id" not in msg:
            return None                             # notifications/initialized, cancelled...
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(id_, -32602, "invalid params: expected an object")

        if method == "initialize":
            asked = params.get("protocolVersion")
            return _reply(id_, {
                "protocolVersion": asked if asked in VERSIONS else
                                   VERSIONS[0] if isinstance(asked, str) and asked else PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "magellan-lite", "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _reply(id_, {})
        if method == "tools/list":
            return _reply(id_, {"tools": [
                {"name": name, "description": spec["description"],
                 "inputSchema": {"type": "object", "properties": spec["properties"],
                                 "required": spec.get("required", []),
                                 "additionalProperties": False}}
                for name, spec in TOOLS.items()]})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments")
            if name not in TOOLS:
                return _error(id_, -32602, f"unknown tool: {name!r} (tools/list has the "
                                           f"names: {', '.join(TOOLS)})")
            if args is not None and not isinstance(args, dict):
                return _error(id_, -32602, "invalid params: arguments must be an object")
            text, is_error = self.call(name, args or {})
            return _reply(id_, {"content": [{"type": "text", "text": text}],
                                "isError": is_error})
        return _error(id_, -32601, f"method not found: {method}")

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Run one tool: ``(JSON text, is_error)``. Never raises."""
        spec = TOOLS[name]
        unknown = sorted(set(args) - set(spec["properties"]))
        try:
            if unknown:
                raise ToolError(f"unknown argument {', '.join(map(repr, unknown))}: {name} takes "
                                f"{', '.join(spec['properties'])}")
            for key in spec.get("required", ()):
                if args.get(key) in (None, ""):
                    raise ToolError(f"{name} needs {key!r}")
            result = spec["run"](self._root(args), args)
        except ToolError as exc:
            return _dumps({"error": str(exc), **exc.extra}), True
        except GitError as exc:
            return _dumps({"error": f"{spec.get('git_help', 'git could not answer.')} "
                                    f"Git said: {exc}"}), True
        except (ValueError, OSError) as exc:
            return _dumps({"error": str(exc)}), True
        except Exception as exc:                    # noqa: BLE001 - a tool never stops the server
            return _dumps({"error": f"{name} failed: {type(exc).__name__}: {exc}"}), True
        return _dumps(result), False

    def _root(self, args: dict) -> Path:
        given = _str(args, "path")
        root = (self.root / given if given else self.root).resolve()
        if not root.is_dir():
            raise ToolError(f"not a folder: {root.as_posix()}")
        return root


def _dumps(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def serve(root: str | Path = ".", stdin=None, stdout=None) -> int:
    """Answer JSON-RPC messages, one per line, until stdin closes."""
    if stdin is None:
        stdin = sys.stdin
        if hasattr(stdin, "reconfigure"):           # MCP is UTF-8; Windows pipes are not
            stdin.reconfigure(encoding="utf-8", errors="replace")
    if stdout is None:
        stdout = sys.stdout
        if hasattr(stdout, "reconfigure"):          # "\n" ends a message, also on Windows
            stdout.reconfigure(encoding="utf-8", newline="\n")
    server = Server(root)

    def send(msg) -> None:
        stdout.write(json.dumps(msg) + "\n")        # ASCII on the wire: no code page trouble
        stdout.flush()

    while True:
        line = stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send(_error(None, -32700, "parse error: each line must be one JSON-RPC message"))
            continue
        # a tool's stray print() must never reach the protocol stream
        with contextlib.redirect_stdout(sys.stderr):
            if isinstance(msg, list):                # a batch (older protocol versions)
                out = [r for r in map(server.handle, msg) if r is not None] if msg else \
                    [_error(None, -32600, "invalid request: empty batch")]
            else:
                out = server.handle(msg)
        if out:
            send(out)
