"""Staged work for an AI agent: plan a change, follow its progress, confirm it is done.

    magellan-lite plan [PATH] --edit FILE OLD NEW [--edit ...] | --edits EDITS.json
    magellan-lite progress [PATH]
    magellan-lite done [PATH]

A plan is the check run on a change before it is made. The agent names the edits it intends,
each ``{path, old, new}`` (usually the definition itself: the new signature line, the wider
PIC). They are applied to the working tree in memory and checked against the last commit, and
what the check finds becomes a worklist:

- ``edit``: every other line that has to change, with the code as written there (a call split
  over continuation lines comes whole; a call inside an INCLUDE file points at that file once,
  with the routines that include it; a field a MOVE would truncate points at its PIC);
- ``recompile``: programs that copy a copybook whose layout changes;
- ``unchanged_uses``: code that uses what changes and needs nothing;
- ``ruled_out``: mentions of the same names that are not uses (a comment, a local array,
  another definition with the same or a similar name), each with the reason.

So an agent does not have to find it all again by hand, which is what agents given only
``reach`` and ``check`` did in the modernization experiment (twice, once per language).

``progress`` checks the working tree as it is now, with any planned edit not made yet applied
on top, so the order of the work does not matter: what is left. ``done`` says whether nothing
is, and lists what a report needs. The plan is kept in the git directory, never in the working
tree.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import tempfile
from pathlib import Path

from magellan_lite.languages import _FIXED_FORM, language

#: findings that are about rebuilding, not editing: they go in ``recompile``
REBUILD = ("copybook-layout-changed", "struct-layout-change")
#: lines of code shown per item at most
CODE_LINES = 6
#: ruled-out sites listed at most (the rest are counted)
RULED_OUT = 40


class PlanError(ValueError):
    """An edit that cannot be placed, said plainly."""


# -- the edits ---------------------------------------------------------------------------------

def _place(text: str, old: str, path: str) -> tuple[int, str]:
    """Where ``old`` is in ``text`` (exactly once), and ``old`` with the file's line endings."""
    for o in (old, old.replace("\n", "\r\n")):
        n = text.count(o) if o else 0
        if n == 1:
            return text.index(o), o
        if n > 1:
            raise PlanError(f"the old text occurs {n} times in {path}: include more of the line "
                            f"around it so it names one place")
    near = difflib.get_close_matches(old.strip().splitlines()[0] if old.strip() else "",
                                     [l.strip() for l in text.splitlines()], n=3, cutoff=0.6)
    raise PlanError(f"the old text is not in {path}"
                    + (f"; the closest lines are: {' | '.join(near)}" if near else
                       ": copy it from the file exactly, spaces included"))


def apply(files: dict[str, str], edits: list[dict]) -> tuple[dict[str, str], list[dict]]:
    """The files with each edit made in memory, and where each edit landed."""
    out, placed = dict(files), []
    for e in edits:
        path = str(e.get("path", "")).replace("\\", "/").lstrip("./") if e.get("path") else ""
        old, new = e.get("old"), e.get("new")
        if not path or not isinstance(old, str) or not isinstance(new, str):
            raise PlanError("each edit needs path, old and new (old: text in the file now, "
                            "new: what replaces it)")
        if path not in out:
            raise PlanError(f"no source file {path} in this project (paths are relative to "
                            f"its root, with forward slashes)")
        at, o = _place(out[path], old, path)
        out[path] = out[path][:at] + (new.replace("\n", "\r\n") if o != old else new) + \
            out[path][at + len(o):]
        line = out[path].count("\n", 0, at) + 1
        placed.append({"path": path, "line": line, "end_line": line + new.count("\n"),
                       "old": old, "new": new})
    return out, placed


def _made(text: str, edit: dict) -> bool:
    """Is a planned edit in ``text`` already? (Text deleted: the old is gone; else the new is
    there. An edit made some other way counts as made: the check judges the result.)"""
    old, new = edit["old"], edit["new"]
    if "\r\n" in text and "\r\n" not in old:      # the file's line endings, not the edit's
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    if new and new in old:
        return old not in text
    return new in text or old not in text


# -- reading code --------------------------------------------------------------------------------

def _comment(line: str, path: str) -> bool:
    lang, s = language(path) or "python", line.strip()
    if lang == "fortran":
        fixed = path.lower().endswith(_FIXED_FORM)
        return (fixed and line[:1] in "cC*!dD") or s.startswith("!")
    if lang == "cobol":
        return len(line) > 6 and line[6] in "*/"
    if lang == "python":
        return s.startswith("#")
    return s.startswith(("//", "/*", "*"))


def statement(lines: list[str], line: int, path: str) -> tuple[int, int]:
    """The first and last line (1-based) of the statement at ``line``: a Fortran call with its
    continuation lines, a COBOL sentence up to its period, other code until brackets close."""
    i = max(0, min(line - 1, len(lines) - 1))
    lang = language(path) or "python"
    start = end = i
    if lang == "fortran" and path.lower().endswith(_FIXED_FORM):
        cont = lambda s: len(s) > 5 and s[:1] not in "cC*!" and s[5] not in " 0"   # noqa: E731
        while start > 0 and cont(lines[start]):
            start -= 1
        while end + 1 < len(lines) and cont(lines[end + 1]):
            end += 1
    elif lang == "fortran":
        while start > 0 and lines[start - 1].split("!", 1)[0].rstrip().endswith("&"):
            start -= 1
        while end + 1 < len(lines) and lines[end].split("!", 1)[0].rstrip().endswith("&"):
            end += 1
    elif lang == "cobol":
        while end + 1 < len(lines) and end < i + 5 and not lines[end].rstrip().endswith("."):
            end += 1
    else:
        depth = 0
        for j in range(i, min(len(lines), i + 10)):
            depth += sum(lines[j].count(c) for c in "([{") - sum(lines[j].count(c) for c in ")]}")
            end = j
            if depth <= 0:
                break
    return start + 1, end + 1


def _code(lines: list[str], first: int, last: int) -> list[str]:
    return [lines[n - 1].rstrip() for n in range(first, min(last, first + CODE_LINES - 1) + 1)]


def _name_re(name: str, path: str) -> re.Pattern:
    """``name`` as a whole word: COBOL words carry hyphens; Fortran and COBOL ignore case."""
    lang = language(path)
    edge = r"[\w-]" if lang == "cobol" else r"\w"
    return re.compile(rf"(?<!{edge}){re.escape(name)}(?!{edge})",
                      re.I if lang in ("fortran", "cobol") else 0)


# -- the worklist ----------------------------------------------------------------------------------

def _site(path: str, first: int, last: int) -> str:
    return f"{path}:{first}" + (f"-{last}" if last > first else "")


def _include_target(snapshot, path: str, text: str) -> str | None:
    """The file a Fortran ``INCLUDE 'x'`` (or C ``#include "x"``) line names, if it is here."""
    m = re.search(r"""^\s*(?:INCLUDE|#\s*include)\s*['"<]([^'">]+)['">]""", text, re.I)
    if not m:
        return None
    base = m.group(1).replace("\\", "/").rsplit("/", 1)[-1].lower()
    found = sorted(p for p in snapshot.files if p.rsplit("/", 1)[-1].lower() == base)
    if len(found) > 1:                     # the one nearest the including file
        here = path.rsplit("/", 1)[0]
        found.sort(key=lambda p: (not p.startswith(here), len(p)))
    return found[0] if found else None


def _edit_items(report, after, targets: list) -> tuple[list[dict], list[dict], list[dict]]:
    """``(edit, recompile, notes)`` from a check's findings."""
    edit: dict[str, dict] = {}
    recompile, notes = [], []
    names = {d.short.split("(", 1)[0] for d in targets} | {(d.label or "").split("(", 1)[0]
                                                           for d in targets}
    names.discard("")
    for f in report.findings:
        if f.rule in REBUILD:
            m = re.search(r"Copied by (\d+) program\(s\): (.*?)\.?$", f.detail or "")
            recompile.append({"where": f"{f.path}:{f.line}", "what": f.message,
                              "programs": m.group(2).split(", ") if m else []})
            continue
        if f.severity not in ("critical", "high"):
            notes.append({"where": f"{f.path}:{f.line}", "rule": f.rule, "what": f.message})
            continue
        lines = after.files.get(f.path, "").splitlines()
        reason = f.message.split(": ", 1)[1].split(" -- ")[0] if ": " in f.message else f.message
        item = {"where": "", "rule": f.rule, "what": f.message, "code": [], "fix": f.fix,
                "group": f"{f.rule}: {reason}", "note": ""}
        if "procedure argument" in f.message:
            item["note"] = (f.detail or "").split(". ", 1)[0]
        first, last = statement(lines, f.line, f.path)
        m = re.match(r"MOVE (\S+) TO (\S+) now drops", f.message)
        inc = _include_target(after, f.path, lines[f.line - 1]) if 0 < f.line <= len(lines) \
            else None
        if m and f.rule == "move-truncates":
            # the work is at the receiving field's PIC, not at the MOVE
            decl = _declaration(after, f.path, m.group(2))
            if decl:
                path, line = decl
                dl = after.files[path].splitlines()
                item.update(where=_site(path, line, line), code=_code(dl, line, line),
                            moved_at=f"{f.path}:{f.line}")
                need = re.search(r"holds (\d+) (digits|bytes)", f.detail or "")
                if need:
                    item["needs"] = f"at least {need.group(1)} integer {need.group(2)}" \
                        if need.group(2) == "digits" else f"at least {need.group(1)} bytes"
                    item["group"] = f"{f.rule}: widen to {item['needs']}"
                drops = f.message.split(" now ", 1)[-1]
                item["note"] = f"MOVE {m.group(1)} TO {m.group(2)} at {f.path}:{f.line} {drops}"
                key = item["where"]
            else:
                item.update(where=_site(f.path, first, last), code=_code(lines, first, last))
                key = item["where"]
        elif inc:
            # the call is inside the INCLUDE file: change it there, once, for every includer
            il = after.files[inc].splitlines()
            # the name the finding says is called, as well as the plan's own names
            called = re.match(r"\S+ calls (\S+?) (?:the old way|through)", f.message)
            want = names | ({called.group(1).rsplit(".", 1)[-1]} if called else set())
            hit = next((n for n, l in enumerate(il, 1) if not _comment(l, inc)
                        and any(_name_re(x, inc).search(l) for x in want)), None)
            if hit is None:
                item.update(where=_site(f.path, first, last), code=_code(lines, first, last))
                key = item["where"]
            else:
                a, b = statement(il, hit, inc)
                key = _site(inc, a, b)
                if key in edit:
                    edit[key]["included_by"].append(f"{f.path}:{f.line}")
                    continue
                item.update(where=key, code=_code(il, a, b), included_by=[f"{f.path}:{f.line}"],
                            what=f"inside {inc}, which {f.path} includes at line {f.line}: "
                                 + f.message.split(" -- ")[0])
        else:
            item.update(where=_site(f.path, first, last), code=_code(lines, first, last))
            key = item["where"]
        if key not in edit:
            edit[key] = item
    return list(edit.values()), recompile, notes


def _declaration(snapshot, path: str, field: str) -> tuple[str, int] | None:
    """Where a COBOL field is declared (its level number and PIC): the program, else the one
    copybook that declares it."""
    pat = re.compile(rf"^\s*\d+\s+{re.escape(field)}(?![\w-]).*\bPIC", re.I)
    for p in [path] + sorted(p for p in snapshot.files if p != path):
        for n, l in enumerate(snapshot.files[p].splitlines(), 1):
            if pat.search(l) and not _comment(l, p):
                return p, n
    return None


def _unchanged_uses(report, graph, after_defs, edit: list[dict]) -> list[dict]:
    """Code that uses what the change changes and needs nothing (copybook users are in
    ``recompile``)."""
    covered = set()
    for item in edit:
        path, span = item["where"].rsplit(":", 1)
        a, _, b = span.partition("-")
        covered |= {(path, n) for n in range(int(a), int(b or a) + 1)}
        for site in item.get("included_by", []) + [item.get("moved_at", "")]:
            if site:
                p, n = site.rsplit(":", 1)
                covered.add((p, int(n)))
    out, seen = [], set()
    for c in report.changes:
        d = after_defs.get(c.name)
        if d is None or (d.lang == "cobol" and d.kind == "class"):
            continue
        for e in graph.callers(c.name):
            if (e.path, e.line) in covered or (e.path, e.line, c.name) in seen:
                continue
            seen.add((e.path, e.line, c.name))
            out.append({"where": f"{e.path}:{e.line}", "who": e.src, "uses": c.name,
                        "how": e.kind + (" (a guess)" if e.guess else "")})
    return sorted(out, key=lambda x: x["where"])


def _ruled_out(after, graph, after_defs, targets: list, sites: set,
               copying: set[str]) -> tuple[list[dict], int]:
    """Mentions of each target's name that are not uses of it, grouped by why. ``copying``:
    the files of the programs that copy a changed copybook."""
    by_site: dict[tuple[str, int], list[str]] = {}
    for e in graph.edges:
        by_site.setdefault((e.path, e.line), []).append(e.dst)
    reasons: dict[str, list[str]] = {}
    for d in targets:
        name = (d.label or d.short).split("(", 1)[0]
        shown = name.upper() if d.lang in ("fortran", "cobol") else name
        same = {n for n, x in after_defs.items() if n != d.name and
                (x.label or x.short).split("(", 1)[0].lower() == name.lower()}
        book = re.match(r"cobol@copy:([^.]+)\.", d.name)
        for path, text in sorted(after.files.items()):
            pat = _name_re(name, path)
            if not pat.search(text):
                continue
            lines = text.splitlines()
            local = _local_declaration(lines, name, path)
            for n, l in enumerate(lines, 1):
                if not pat.search(l) or (path, n) in sites or \
                        (path == d.path and d.line <= n <= d.end_line and not _comment(l, path)):
                    continue
                others = [x for x in by_site.get((path, n), []) if x in same] + \
                    [x for x in same if after_defs[x].path == path and
                     after_defs[x].line <= n <= after_defs[x].end_line]
                if d.name in by_site.get((path, n), []):
                    continue                          # a use: listed elsewhere
                if others:
                    o = after_defs[others[0]]
                    why = f"a different {shown}: {o.name} ({o.path}:{o.line})"
                elif _comment(l, path):
                    why = "a comment"
                elif local:
                    why = (f"a local variable or array named {shown}, not the "
                           f"{'routine' if d.lang == 'fortran' else d.kind}")
                elif d.lang == "fortran" and re.match(r"\s*EXTERNAL\b", l, re.I):
                    why = (f"EXTERNAL {shown}: it is passed as an argument there (the calls "
                           f"made through it are in edit)")
                elif book and language(path) == "cobol" and path not in copying \
                        and not path.lower().endswith((".cpy", ".dcl")):
                    why = f"in a program that does not copy {book.group(1)}"
                else:
                    why = f"no reference to {d.name} is read here"
                reasons.setdefault(why, []).append(f"{path}:{n}")
        # definitions with a similar name, so nobody has to check they are different
        labels = {(x.label or x.short).split("(", 1)[0]: x for x in after_defs.values()
                  if x.lang == d.lang and x.name != d.name}
        close = set(difflib.get_close_matches(name, list(labels), n=8, cutoff=0.8))
        close |= {l for l in labels if l.lower().startswith(name.lower() + "-")}
        for l in sorted(close):
            x = labels[l]
            if x.name not in same:                    # those are among the mentions above
                reasons.setdefault("a different definition with a similar name (not changed)",
                                   []).append(f"{x.path}:{x.line} "
                                              + (l.upper() if x.lang in ("fortran", "cobol") else l))
    out, total = [], sum(len(v) for v in reasons.values())
    shown = 0
    for why, where in sorted(reasons.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        keep = where[:max(3, RULED_OUT - shown)]
        shown += len(keep)
        out.append({"why": why, "count": len(where), "where": keep})
    return out, max(0, total - shown)


def _local_declaration(lines: list[str], name: str, path: str) -> int | None:
    """The line declaring a Fortran local (``REAL INTRST(12)``) named ``name``, if any."""
    if language(path) != "fortran":
        return None
    pat = re.compile(r"^\s*(REAL|INTEGER|DOUBLE\s+PRECISION|LOGICAL|CHARACTER|COMPLEX|DIMENSION)"
                     rf"\b[^!]*(?<!\w){re.escape(name)}(?!\w)", re.I)
    return next((n for n, l in enumerate(lines, 1) if not _comment(l, path) and pat.search(l)),
                None)


def _coverage(snapshot, report) -> dict:
    from magellan_lite.languages import NAMES
    counts: dict[str, int] = {}
    for p in snapshot.files:
        lang = NAMES.get(language(p) or "python", "Python")
        counts[lang] = counts.get(lang, 0) + 1
    return {"files": len(snapshot.files), "by_language": dict(sorted(counts.items())),
            "not_read": report.errors}


# -- the stages ------------------------------------------------------------------------------------

def _store(root: Path) -> Path:
    """Where a project's plan is kept: its git directory, else the temp folder."""
    git = root / ".git"
    if git.is_dir():
        return git / "magellan-lite" / "plan.json"
    key = hashlib.sha1(str(root).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "magellan-lite-plans" / f"{key}.json"


def _run(root: Path, against: str, edits: list[dict]):
    """Check the working tree with ``edits`` made in memory: ``(report, after, graph, defs,
    placed)``."""
    from magellan_lite.defs import definitions
    from magellan_lite.engine import check_snapshots, load_before
    from magellan_lite.graph import build_graph
    from magellan_lite.source import Snapshot, working_tree

    now = working_tree(root)
    files, placed = apply(now.files, edits)
    after = Snapshot(files, "plan")
    before = load_before(root, against)
    report = check_snapshots(before, after, root, against)
    after_defs = definitions(after)
    graph = build_graph(after, after_defs, definitions(before))
    return report, after, graph, after_defs, placed


def _targets(after_defs, placed: list[dict]) -> list:
    """The innermost definition each edit lands in."""
    out = []
    for p in placed:
        here = [d for d in after_defs.values()
                if d.path == p["path"] and d.line <= p["end_line"] and p["line"] <= d.end_line]
        if here:
            d = min(here, key=lambda d: d.end_line - d.line)
            if d not in out:
                out.append(d)
    return out


def plan(root: str | Path, edits: list[dict], against: str = "git:HEAD", save: bool = True) -> dict:
    """The worklist for ``edits`` (see the module's docstring)."""
    root = Path(root).resolve()
    if not edits:
        raise PlanError("plan needs the edits you intend: a list of {path, old, new}")
    report, after, graph, after_defs, placed = _run(root, against, edits)
    targets = _targets(after_defs, placed)
    edit, recompile, notes = _edit_items(report, after, targets)
    uses = _unchanged_uses(report, graph, after_defs, edit)
    sites = {(u["where"].rsplit(":", 1)[0], int(u["where"].rsplit(":", 1)[1])) for u in uses}
    for item in edit:
        path, span = item["where"].rsplit(":", 1)
        a, _, b = span.partition("-")
        sites |= {(path, n) for n in range(int(a), int(b or a) + 1)}
        for s in item.get("included_by", []) + [item.get("moved_at", "")]:
            if s:
                sites.add((s.rsplit(":", 1)[0], int(s.rsplit(":", 1)[1])))
    progs = {p for r in recompile for p in r["programs"]}
    copying = {d.path for d in after_defs.values()
               if d.lang == "cobol" and d.name.split("@", 1)[-1] in progs}
    ruled, more = _ruled_out(after, graph, after_defs, targets, sites, copying)
    out = {
        "stage": "plan",
        "summary": _summary(edit, recompile, uses, ruled),
        "planned": [{"where": _site(p["path"], p["line"], p["end_line"]),
                     "changes": [d.name for d in targets if d.path == p["path"]
                                 and d.line <= p["end_line"] and p["line"] <= d.end_line]}
                    for p in placed],
        "edit": edit, "recompile": recompile, "unchanged_uses": uses,
        "ruled_out": ruled, "ruled_out_omitted": more, "notes": notes,
        "coverage": _coverage(after, report),
        "next": "Make the planned edits and every edit item (in any order), then call "
                "progress; when it says nothing is left, call done.",
    }
    if save:
        store = _store(root)
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"against": against, "edits": [
            {k: p[k] for k in ("path", "old", "new")} for p in placed],
            "edit": [{k: i[k] for k in ("where", "rule", "what")} for i in edit],
            "recompile": recompile}, indent=1), encoding="utf-8")
    return out


def _summary(edit, recompile, uses, ruled) -> str:
    files = len({i["where"].rsplit(":", 1)[0] for i in edit})
    parts = [f"{len(edit)} place{'s' if len(edit) != 1 else ''} to change in "
             f"{files} file{'s' if files != 1 else ''}" if edit else "nothing else to change"]
    progs = sorted({p for r in recompile for p in r["programs"]})
    if progs:
        parts.append(f"{len(progs)} program{'s' if len(progs) != 1 else ''} to recompile")
    if uses:
        parts.append(f"{len(uses)} use{'s' if len(uses) != 1 else ''} that need nothing")
    n = sum(r["count"] for r in ruled)
    if n:
        parts.append(f"{n} mention{'s' if n != 1 else ''} ruled out")
    return "; ".join(parts) + "."


def _saved(root: Path) -> dict:
    store = _store(root)
    if not store.is_file():
        raise PlanError("no plan for this project yet: call plan first with the edits you intend")
    return json.loads(store.read_text(encoding="utf-8"))


def progress(root: str | Path) -> dict:
    """What is left of the plan: the working tree now, planned edits not made yet applied."""
    root = Path(root).resolve()
    saved = _saved(root)
    from magellan_lite.source import working_tree
    now = working_tree(root).files
    pending = [e for e in saved["edits"] if e["path"] in now and not _made(now[e["path"]], e)]
    report, after, graph, after_defs, placed = _run(root, saved["against"], pending)
    edit, recompile, notes = _edit_items(report, after, _targets(after_defs, placed))
    planned = len(saved["edit"])
    left = [i for i in edit]
    out = {
        "stage": "progress",
        "done": not pending and not left,
        "summary": (f"planned edits: {len(saved['edits']) - len(pending)} of "
                    f"{len(saved['edits'])} made; places to change: {len(left)} left"
                    + (f" (the plan listed {planned})" if planned else "") + "."),
        "planned_not_made": [{"where": _site(p["path"], p["line"], p["end_line"]),
                              "new": p["new"]} for p in placed],
        "left": left, "recompile": recompile, "notes": notes,
        "not_read": report.errors,
        "next": ("Nothing is left: call done." if not pending and not left else
                 "Change what is left, then call progress again."),
    }
    return out


def done(root: str | Path) -> dict:
    """Is the plan finished? If so, what a report needs: every line changed, and each item the
    plan listed; if not, what is left."""
    root = Path(root).resolve()
    out = progress(root)
    saved = _saved(root)
    if not out["done"]:
        out["stage"] = "done"
        out["next"] = "Not done: change what is left (left, planned_not_made), then call done again."
        return out
    from magellan_lite.engine import load_before
    from magellan_lite.source import working_tree
    before, now = load_before(root, saved["against"]).files, working_tree(root).files
    changed = {}
    for path in sorted(set(before) | set(now)):
        a, b = before.get(path, "").splitlines(), now.get(path, "").splitlines()
        if a == b:
            continue
        lines = [j + 1 for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes()
                 if tag != "equal" for j in range(j1, j2)]
        changed[path] = _ranges(lines) if lines else "deleted lines only"
    return {
        "stage": "done", "done": True,
        "summary": (f"Done: every planned edit made and nothing left to change; "
                    f"{len(changed)} file{'s' if len(changed) != 1 else ''} changed."),
        "changed": changed,
        "planned_items": saved["edit"],
        "recompile": out["recompile"], "notes": out["notes"], "not_read": out["not_read"],
        "next": "Run the project's own tests or build if it has them; behaviour is not checked "
                "by reading code.",
    }


def _ranges(nums: list[int]) -> str:
    out, start, prev = [], None, None
    for n in sorted(set(nums)):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if prev > start else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(out)


# -- text --------------------------------------------------------------------------------------------

def text(result: dict) -> str:
    """The same, for a terminal or an agent that reads text."""
    out = [f"magellan-lite {result['stage']}: {result['summary']}"]
    if result.get("planned"):
        out.append("  planned (applied in memory)")
        out += [f"     {p['where']}  {', '.join(p['changes'])}" for p in result["planned"]]
    if result.get("planned_not_made"):
        out.append("  planned, not made yet")
        out += [f"     {p['where']}" for p in result["planned_not_made"]]
    items = result.get("edit", result.get("left", []))
    if items:
        out.append(f"  {'edit' if result['stage'] == 'plan' else 'left'} ({len(items)})")
        groups: dict[str, list[dict]] = {}
        for i in items:
            groups.setdefault(i.get("group") or i["rule"], []).append(i)
        for g, members in groups.items():
            out.append(f"    {g}  [{len(members)}]")
            for i in members:
                note = i.get("note") or (f"included by {', '.join(i['included_by'])}"
                                         if i.get("included_by") else "")
                if len(i["code"]) == 1 and not note:
                    out.append(f"      {i['where']:<26} |{i['code'][0]}")
                    continue
                out.append(f"      {i['where']}" + (f"  ({note})" if note else ""))
                out += [f"      {'':<26} |{c}" for c in i["code"]]
    for r in result.get("recompile", []):
        out.append(f"  recompile ({r['where']}): {', '.join(r['programs']) or r['what']}")
    if result.get("unchanged_uses"):
        out.append("  uses that need nothing")
        out += [f"     {u['where']}  {u['who']} {u['how']} {u['uses']}"
                for u in result["unchanged_uses"]]
    if result.get("ruled_out"):
        out.append("  ruled out (mention the name, not a use)")
        out += [f"     {r['why']}: {', '.join(r['where'])}"
                + (f" (+{r['count'] - len(r['where'])})" if r["count"] > len(r["where"]) else "")
                for r in result["ruled_out"]]
    if result.get("changed"):
        out.append("  changed")
        out += [f"     {p}: {lines}" for p, lines in result["changed"].items()]
    for n in result.get("notes", [])[:8]:
        out.append(f"  note {n['where']}  {n['rule']}: {n['what']}")
    cov = result.get("coverage")
    if cov:
        langs = ", ".join(f"{k} {v}" for k, v in cov["by_language"].items())
        out.append(f"  read {cov['files']} files ({langs})"
                   + (f"; not read: {'; '.join(cov['not_read'])}" if cov["not_read"] else
                      "; every file read"))
    elif result.get("not_read"):
        out.append(f"  not read: {'; '.join(result['not_read'])}")
    out.append(f"  next: {result['next']}")
    return "\n".join(out)
