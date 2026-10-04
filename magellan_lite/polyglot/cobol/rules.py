"""COBOL checks that the language-neutral rules cannot see.

``perform-thru-range-changed``
    ``PERFORM A THRU C`` runs every paragraph between A and C in source order. A
    paragraph inserted (or moved) inside that range silently joins it; one removed
    silently leaves it. Reported per range, with every PERFORM site of it.

``copybook-layout-changed``
    A copybook's record layout changed so that existing fields moved, resized, changed
    type or disappeared (or the record length changed). Every program that COPYs it
    must be recompiled together, and data written with the old layout (files, COMMAREAs,
    CALL arguments) no longer matches. A field carved out of FILLER at the same record
    length is compatible and is not reported.

``call-using-mismatch``
    A CALL passes a different number of arguments than the callee's
    ``PROCEDURE DIVISION USING`` takes, or passes an argument shorter than the LINKAGE
    item it lands on. Nothing checks this at compile time; BY REFERENCE means the callee
    reads and writes the caller's storage with its own layout.

``unhandled-new-member`` / ``non-exhaustive-match``
    An 88-level condition was added to a field, and an EVALUATE that handled every
    previous condition of that field does not handle the new one (the Python rules'
    names, so the brief treats them the same way).

``fall-through-changed``
    A paragraph now runs off its end into a different paragraph than before (one was
    inserted after it, or it lost its GOBACK / STOP RUN / GO TO).

``legacy-construct-introduced``
    Changed code adds ALTER, GO TO, NEXT SENTENCE or another construct modernization
    work is trying to remove. Informational.
"""
from __future__ import annotations

from collections import defaultdict

from magellan_lite.polyglot.core.diff import ChangeKind, ChangeSet
from magellan_lite.polyglot.core.findings import Finding
from magellan_lite.polyglot.core.model import EdgeKind, Graph, Node, NodeKind

LEGACY = {
    "alter": "ALTER", "go-to": "GO TO", "go-to-depending": "GO TO ... DEPENDING ON",
    "alterable-go-to": "an ALTERable GO TO", "next-sentence": "NEXT SENTENCE",
    "examine": "EXAMINE", "transform": "TRANSFORM", "exhibit": "EXHIBIT",
    "ready-trace": "READY TRACE", "note": "NOTE", "enter": "ENTER",
}


def _cobol(n: Node | None) -> bool:
    return n is not None and n.meta.get("lang") == "cobol"


def cobol_findings(before: Graph | None, after: Graph, cs: ChangeSet,
                   root: str) -> list[Finding]:
    if before is None or not any(_cobol(n) for n in after.nodes.values()
                                 if n.kind is NodeKind.MODULE):
        return []
    edited_files = set(cs.files_modified + cs.files_added)
    changed = {c.node_id for c in cs.changes}
    out: list[Finding] = []
    out += thru_ranges(before, after)
    out += copybook_layouts(before, after, edited_files)
    out += call_mismatches(before, after, cs)
    out += evaluate_exhaustiveness(before, after, cs)
    out += fall_through(before, after, changed)
    out += legacy(before, after, cs)
    out += move_truncations(before, after, changed)
    return out


# --------------------------------------------------------------------------
def _thru_sites(g: Graph) -> dict[tuple, list]:
    sites: dict[tuple, list] = defaultdict(list)
    for e in g.edges:
        if e.kind is EdgeKind.CALLS and e.meta.get("thru") and not e.meta.get("via") \
                and _cobol(g.nodes.get(e.src)):
            module = g.nodes[e.src].module
            key = (module, tuple(e.meta["thru"]))
            sites[key].append(e)
    return sites


def thru_ranges(before: Graph, after: Graph) -> list[Finding]:
    old_sites, new_sites = _thru_sites(before), _thru_sites(after)
    performed = defaultdict(set)          # module -> paragraphs PERFORMed on their own
    for e in after.edges:
        if e.kind is EdgeKind.CALLS and e.meta.get("perform") and not e.meta.get("thru") \
                and e.dst in after.nodes:
            performed[after.nodes[e.dst].module].add(after.nodes[e.dst].name)
    out: list[Finding] = []
    for key, edges in sorted(new_sites.items()):
        if key not in old_sites:
            continue
        new_rng = list(edges[0].meta.get("range") or [])
        old_rng = list(old_sites[key][0].meta.get("range") or [])
        if new_rng == old_rng or not old_rng:
            continue
        module, (first, last) = key
        empty = {n.name for n in after.nodes.values() if n.module == module
                 and n.kind.is_callable and "paragraph" in n.tags
                 and not n.meta.get("statements")}
        joined = [p for p in new_rng if p not in old_rng and p not in empty]
        left = [p for p in old_rng if p not in new_rng]
        if not joined and not left:
            continue                       # reordered only: fall-through reports it
        also = [p for p in joined if p in performed[module]]
        severity = "high" if also or left else "medium"
        what = []
        if joined:
            what.append(f"now also runs {', '.join(joined)}")
        if left:
            what.append(f"no longer runs {', '.join(left)}")
        e0 = sorted(edges, key=lambda e: (e.path, e.lineno))[0]
        src = after.nodes[e0.src]
        detail = (f"PERFORM {first} THRU {last} executes every paragraph between them in "
                  f"source order. After this change the range {' and '.join(what)}.")
        if also:
            detail += (f" {', '.join(also)} is also PERFORMed on its own elsewhere, so it "
                       f"reads as a separate routine that was inserted inside the range by "
                       f"mistake -- it will now run twice on some paths.")
        out.append(Finding(
            rule="perform-thru-range-changed", severity=severity,
            title=f"PERFORM {first} THRU {last} {' and '.join(what)}",
            detail=detail, node_id=src.id, label=src.qualname, path=e0.path,
            lineno=e0.lineno,
            evidence=[f"range before: {' -> '.join(old_rng)}",
                      f"range after: {' -> '.join(new_rng)}"]
            + [f"PERFORM site: {after.nodes[e.src].qualname} at {e.path}:{e.lineno}"
               for e in sorted(edges, key=lambda e: (e.path, e.lineno))[:8]],
            suggestion=("Move the new paragraph outside the range (after the -EXIT "
                        "paragraph), or confirm it is meant to run as part of it."
                        if joined else "Confirm the paragraphs that left the range are "
                                       "still performed where they are needed."),
        ))
    return out


# --------------------------------------------------------------------------
def _layout(g: Graph, module: str) -> dict[str, dict]:
    out = {}
    for n in g.nodes.values():
        if n.module != module or not n.kind.is_data or "condition-name" in n.tags \
                or "file" in n.tags:
            continue
        out[n.id] = {"offset": n.meta.get("offset"), "size": n.meta.get("size"),
                     "pic": n.meta.get("pic", ""), "usage": n.meta.get("usage", ""),
                     "occurs": n.meta.get("occurs"), "root": n.parent == f"mod:{module}",
                     "name": n.name, "line": n.lineno, "layout": n.meta.get("layout", ""),
                     "redefines": n.meta.get("redefines", "")}
    return out


def _importers(g: Graph, mod_id: str) -> list[Node]:
    """Programs that COPY this copybook, directly or through another copybook."""
    seen, stack, progs = {mod_id}, [mod_id], []
    while stack:
        cur = stack.pop()
        for e in g.in_edges(cur):
            if e.kind is not EdgeKind.IMPORTS or e.src in seen:
                continue
            seen.add(e.src)
            n = g.nodes.get(e.src)
            if n is None:
                continue
            if "copybook" in n.tags:
                stack.append(e.src)
            else:
                progs.append(n)
    return sorted(progs, key=lambda n: n.qualname)


def copybook_layouts(before: Graph, after: Graph, edited: set[str]) -> list[Finding]:
    out: list[Finding] = []
    for mod in after.nodes.values():
        if mod.kind is not NodeKind.MODULE or "copybook" not in mod.tags or not _cobol(mod):
            continue
        old_mod = before.nodes.get(mod.id)
        if old_mod is None or old_mod.body_hash == mod.body_hash:
            continue
        old, new = _layout(before, mod.module), _layout(after, mod.module)
        if not old:
            continue
        problems: list[str] = []
        first_line = 0
        shape = lambda v: (v["offset"], v["size"], v["pic"], v["usage"],
                           str(v["occurs"]), v["root"])
        # a field renamed in place keeps its bytes: a compile problem, not a layout one
        renamed_to = {shape(v) for k, v in new.items() if k not in old}
        for nid, o in sorted(old.items(), key=lambda kv: kv[1]["line"]):
            if o["name"] == "FILLER":
                continue
            n = new.get(nid)
            if n is None and shape(o) in renamed_to:
                continue
            if n is None:
                problems.append(f"{o['name']} removed (was {o['size']} bytes at offset "
                                f"{o['offset']})")
                continue
            diffs = []
            if o["offset"] != n["offset"]:
                diffs.append(f"offset {o['offset']} -> {n['offset']}")
            if o["size"] != n["size"]:
                diffs.append(f"size {o['size']} -> {n['size']}")
            if (o["pic"], o["usage"]) != (n["pic"], n["usage"]):
                diffs.append(f"PIC {o['pic'] or '-'} {o['usage']} -> "
                             f"{n['pic'] or '-'} {n['usage']}".rstrip())
            if o["occurs"] != n["occurs"]:
                diffs.append(f"OCCURS {o['occurs']} -> {n['occurs']}")
            if o["redefines"] != n["redefines"]:
                diffs.append(f"REDEFINES {o['redefines'] or '-'} -> {n['redefines'] or '-'}")
            if diffs:
                problems.append(f"{n['name']}: " + ", ".join(diffs))
                first_line = first_line or n["line"]
        if not problems:
            continue
        progs = _importers(after, mod.id)
        if not progs:
            continue
        unedited = [p for p in progs if p.path not in edited]
        boundary = sorted({b for n in after.nodes.values() if n.module == mod.module
                           for b in n.meta.get("used_as", ())})
        severity = "high" if boundary and unedited else "medium"
        if not unedited:
            severity = "low"
        out.append(Finding(
            rule="copybook-layout-changed", severity=severity,
            title=(f"Record layout in copybook {mod.name} changed; {len(progs)} program(s) "
                   f"COPY it" + (f", {len(unedited)} not part of this change"
                                 if unedited else "")),
            detail=("Existing fields moved, resized or changed type. Every program that "
                    "COPYs this copybook must be recompiled with it"
                    + (f", and data exchanged in this layout ({', '.join(boundary)}) written "
                       f"by programs still on the old layout will be misread"
                       if boundary else "")
                    + ". A field added in place of FILLER at the same record length would "
                      "have been compatible."),
            node_id=mod.id, label=mod.qualname, path=mod.path, lineno=first_line or 1,
            evidence=problems[:8] + (["..."] if len(problems) > 8 else [])
            + [f"COPYed by {p.name} ({p.path})" + ("  (edited)" if p.path in edited else "")
               for p in progs[:12]]
            + ([f"... and {len(progs) - 12} more programs"] if len(progs) > 12 else []),
            suggestion=("Recompile every program listed, convert any stored data, and "
                        "check CALL/LINK partners pass the same layout; or keep existing "
                        "offsets and take new fields from FILLER."),
        ))
    return out


# --------------------------------------------------------------------------
def _mismatch(g: Graph, e) -> list[tuple[str, str, str]]:
    """``(key, severity, text)`` per problem; the key survives unrelated edits."""
    callee = g.nodes.get(e.dst)
    if callee is None or not _cobol(callee) or not callee.kind.is_callable:
        return []
    ar = callee.meta.get("arity") or {}
    if ar.get("cics") or e.meta.get("cics"):
        need = len(ar.get("positional", []))
        sizes = ar.get("sizes") or []
        passed = e.meta.get("arg_sizes") or []
        if need and passed and sizes and passed[0] is not None and sizes[0] is not None \
                and passed[0] < sizes[0]:
            return [("commarea", "medium",
                     f"COMMAREA passed is {passed[0]} bytes; {callee.name} maps "
                     f"{ar['positional'][0]} of {sizes[0]} bytes over it (reads past it "
                     f"unless it checks EIBCALEN; writes corrupt the caller)")]
        return []
    got, need = int(e.meta.get("args", 0)), len(ar.get("positional", []))
    out = []
    if "args" in e.meta and got != need:
        out.append(("count", "high", f"passes {got} argument(s); {callee.name} takes {need} "
                    f"(USING {' '.join(ar.get('positional', [])) or 'nothing'})"))
    passed = e.meta.get("arg_sizes") or []
    sizes = ar.get("sizes") or []
    for k in range(min(len(passed), len(sizes))):
        p, s = passed[k], sizes[k]
        if p is not None and s is not None and p < s and \
                (e.meta.get("by") or ["REFERENCE"] * (k + 1))[k] == "REFERENCE":
            using = (e.meta.get("using") or ["?"] * (k + 1))[k]
            out.append((f"size{k}", "high", f"argument {k + 1} ({using}) is {p} bytes but "
                        f"{ar['positional'][k]} is {s} bytes: the callee writes past it"))
    return out


def call_mismatches(before: Graph, after: Graph, cs: ChangeSet) -> list[Finding]:
    sig_changed = {c.node_id for c in cs.changes if c.kind is ChangeKind.SIGNATURE_CHANGED}
    old = defaultdict(set)
    for e in before.edges:
        if e.kind is EdgeKind.CALLS and "callee" in e.meta:
            for key, _sev, _text in _mismatch(before, e):
                old[(e.src, e.dst)].add(key)
    out: list[Finding] = []
    for e in after.edges:
        if e.kind is not EdgeKind.CALLS or "callee" not in e.meta or e.dynamic:
            continue
        if e.dst in sig_changed:
            continue                  # signature-break reports every site of a changed callee
        found = [p for p in _mismatch(after, e) if p[0] not in old.get((e.src, e.dst), ())]
        if not found:
            continue
        problems = [text for _k, _s, text in found]
        severity = "high" if any(s == "high" for _k, s, _t in found) else "medium"
        src, dst = after.nodes[e.src], after.nodes[e.dst]
        out.append(Finding(
            rule="call-using-mismatch", severity=severity,
            title=(f"{'EXEC CICS ' + e.meta['cics'].upper() if e.meta.get('cics') else 'CALL'} "
                   f"'{dst.name}' in {src.qualname} does not match what {dst.name} expects"),
            detail=("; ".join(problems) + ". Arguments bind by position and, BY REFERENCE, "
                    "the callee works on the caller's storage with its own LINKAGE layout: "
                    "nothing fails at compile time."),
            node_id=src.id, label=src.qualname, path=e.path, lineno=e.lineno,
            evidence=[f"callee: {dst.signature} at {dst.path}:{dst.lineno}",
                      f"call passes: {' '.join(e.meta.get('using') or []) or 'nothing'}"],
            suggestion="Pass exactly the records the callee's LINKAGE SECTION describes.",
        ))
    return out


# --------------------------------------------------------------------------
def _members(g: Graph) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for n in g.nodes.values():
        if "condition-name" in n.tags and n.parent and _cobol(n):
            out[n.parent].append(n.name)
    return out


def evaluate_exhaustiveness(before: Graph, after: Graph, cs: ChangeSet) -> list[Finding]:
    old_m, new_m = _members(before), _members(after)
    edited = {c.node_id for c in cs.changes
              if c.kind in (ChangeKind.ADDED, ChangeKind.BODY_CHANGED)}
    old_sites = {(n.id, s["field"]): s for n in before.nodes.values()
                 for s in n.meta.get("evaluates", ()) if _cobol(n)}
    out: list[Finding] = []
    for n in after.nodes.values():
        if not _cobol(n) or not n.kind.is_callable:
            continue
        for s in n.meta.get("evaluates", ()):
            fid = s["field"]
            members = new_m.get(fid, [])
            field = after.nodes.get(fid)
            if field is None or not members:
                continue
            covered = set(s["covered"])
            fname = field.name
            old = old_m.get(fid)
            added = [m for m in members if old is not None and m not in old]
            if added and set(old) <= covered and len(old) >= 2:
                missing = [m for m in added if m not in covered]
                if not missing:
                    continue
                sev = "medium" if s["other"] else "high"
                fate = ("takes WHEN OTHER, which may not be right for it" if s["other"] else
                        "matches no WHEN, so the EVALUATE does nothing -- silently")
                out.append(Finding(
                    rule="unhandled-new-member", severity=sev,
                    title=(f"{', '.join(missing)} added to {fname} is not handled by the "
                           f"EVALUATE in {n.qualname}"),
                    detail=(f"This change adds the 88-level condition(s) {', '.join(missing)} "
                            f"to {fname}. The EVALUATE at line {s['line']} handled every other "
                            f"condition of {fname}, so it was complete before; now the new "
                            f"value {fate}."),
                    node_id=n.id, label=n.qualname, path=s.get("path") or n.path,
                    lineno=s["line"],
                    evidence=[f"{fname} is defined at {field.path}:{field.lineno}",
                              f"handled: {', '.join(sorted(covered))}",
                              f"not handled: {', '.join(missing)}",
                              f"WHEN OTHER: {'yes' if s['other'] else 'no'}"],
                    suggestion=f"Add WHEN {missing[0]} (and a WHEN OTHER that fails loudly).",
                ))
                continue
            if n.id in edited and not s["other"] and len(covered) >= 2 and not s["complex"]:
                missing = [m for m in members if m not in covered]
                prior = old_sites.get((n.id, fid))
                if missing and not (prior is not None and not prior["other"]
                                    and set(missing) <= set(members) - set(prior["covered"])):
                    out.append(Finding(
                        rule="non-exhaustive-match", severity="medium",
                        title=(f"EVALUATE on {fname} in {n.qualname} misses "
                               f"{', '.join(missing)} and has no WHEN OTHER"),
                        detail=(f"It handles {len(covered)} of {len(members)} condition names "
                                f"of {fname}; any other value falls through silently."),
                        node_id=n.id, label=n.qualname, path=s.get("path") or n.path,
                        lineno=s["line"],
                        evidence=[f"not handled: {', '.join(missing)}"],
                        suggestion="Add the missing WHENs or a WHEN OTHER.",
                    ))
    return _one_per_site(out)


def _one_per_site(findings: list[Finding]) -> list[Finding]:
    """An EVALUATE in a procedure copybook is one site however many programs COPY it."""
    kept: dict[tuple, Finding] = {}
    for f in sorted(findings, key=lambda f: f.label):
        handled = next((e for e in f.evidence if e.startswith("not handled")), "")
        key = (f.rule, f.path, f.lineno, handled)
        if key in kept:
            kept[key].evidence.append(f"also COPYed into {f.label}")
        else:
            kept[key] = f
    return list(kept.values())


# --------------------------------------------------------------------------
def _falls(g: Graph) -> dict[str, str]:
    out = {}
    for e in g.edges:
        if e.kind is EdgeKind.CALLS and e.meta.get("fallthrough") \
                and not e.meta.get("structured") and not e.meta.get("abend_exit") \
                and _cobol(g.nodes.get(e.src)):
            out[e.src] = e.dst
    return out


def _lands_on(g: Graph, falls: dict[str, str], start: str, old: Graph) -> str:
    """Follow fall-through past paragraphs this change added with no statements."""
    cur, seen = start, set()
    while cur in falls and cur not in seen and cur not in old.nodes \
            and not g.nodes[cur].meta.get("statements"):
        seen.add(cur)
        cur = falls[cur]
    return cur


def fall_through(before: Graph, after: Graph, changed: set[str]) -> list[Finding]:
    old, new = _falls(before), _falls(after)
    out: list[Finding] = []
    for src, dst in sorted(new.items()):
        if src not in before.nodes or old.get(src) == dst:
            continue
        if src in old and _lands_on(after, new, dst, before) == old[src]:
            continue            # an empty paragraph (a label, an -END marker) was inserted
        if src in old and old[src] not in after.nodes and dst not in before.nodes:
            continue            # what followed was renamed or replaced in place
        s, d = after.nodes[src], after.nodes[dst]
        if src in old:
            prev = old[src]
            prev_name = before.nodes[prev].name if prev in before.nodes else prev
            why = (f"it used to fall into {prev_name}; {d.name} now sits between them"
                   if dst not in before.nodes else
                   f"it used to fall into {prev_name}")
        else:
            why = ("it used to end with GOBACK, STOP RUN, EXIT PROGRAM or an unconditional "
                   "GO TO" if src in changed else
                   "it is now reached by GO TO or from the paragraph above")
        out.append(Finding(
            rule="fall-through-changed", severity="medium",
            title=f"{s.qualname} now falls through into {d.name}",
            detail=(f"Control that reaches the end of {s.name} continues into the next "
                    f"paragraph in source order: {why}."),
            node_id=src, label=s.qualname, path=s.path, lineno=s.end_lineno or s.lineno,
            evidence=[f"{d.name} at {d.path}:{d.lineno}"],
            suggestion=f"End {s.name} explicitly (GOBACK / GO TO its exit) if it should not "
                       f"run {d.name}.",
        ))
    return out


def _only_exit_gotos(g: Graph, n: Node) -> bool:
    """Every GO TO in ``n`` targets an exit paragraph (``GO TO 1000-EXIT``): the idiom."""
    targets = [g.nodes.get(e.dst) for e in g.out_edges(n.id)
               if e.kind is EdgeKind.CALLS and e.meta.get("goto")]
    return bool(targets) and all(t is not None and "EXIT" in t.name for t in targets)


def legacy(before: Graph, after: Graph, cs: ChangeSet) -> list[Finding]:
    per_program: dict[str, list[tuple[Node, list[str]]]] = defaultdict(list)
    for c in cs.changes:
        if c.kind not in (ChangeKind.ADDED, ChangeKind.BODY_CHANGED):
            continue
        n = after.nodes.get(c.node_id)
        if not _cobol(n) or not n.kind.is_callable:
            continue
        o = before.nodes.get(c.node_id)
        new_tags = set(n.tags) - set(o.tags if o is not None else ())
        if "go-to" in new_tags and _only_exit_gotos(after, n):
            new_tags.discard("go-to")
        found = [LEGACY[t] for t in sorted(new_tags) if t in LEGACY]
        if found:
            per_program[n.module].append((n, found))
    out: list[Finding] = []
    for module, hits in sorted(per_program.items()):
        hits.sort(key=lambda h: (h[0].path, h[0].lineno))
        what = sorted({w for _n, f in hits for w in f})
        first = hits[0][0]
        out.append(Finding(
            rule="legacy-construct-introduced", severity="low",
            title=f"{module} gains {', '.join(what)} in {len(hits)} paragraph(s)",
            detail="Constructs modernization plans remove; structured PERFORM and EVALUATE "
                   "express the same control flow (GO TO an -EXIT paragraph is not counted).",
            node_id=first.id, label=first.qualname, path=first.path, lineno=first.lineno,
            evidence=[f"{n.name}: {', '.join(f)} ({n.path}:{n.lineno})" for n, f in hits[:10]],
        ))
    return out


# --------------------------------------------------------------------------
# MOVE into a field the change left too small
# --------------------------------------------------------------------------
def _digits(pic: str) -> int | None:
    """Integer digits of a numeric PIC (``9(11)V99`` -> 11), or None if not numeric."""
    import re
    p = pic.upper().replace(" ", "")
    if not p or any(c in p for c in "XAN"):
        return None
    integer = p.split("V", 1)[0]
    total = 0
    for m in re.finditer(r"([9ZP*])(?:\((\d+)\))?", integer):
        total += int(m.group(2) or 1)
    return total or None


def _capacity(n: Node) -> tuple[str, int] | None:
    """What a field holds: ("digits", n) for numeric, ("bytes", n) otherwise."""
    pic = n.meta.get("pic") or ""
    d = _digits(pic)
    if d is not None:
        return "digits", d
    size = n.meta.get("size")
    return ("bytes", int(size)) if isinstance(size, int) and size > 0 else None


def _moves(g: Graph) -> dict[tuple[str, str], list]:
    """(source field, target field) -> MOVE edges writing the target."""
    by_stmt: dict[str, dict[str, list]] = {}
    for e in g.edges:
        key = e.meta.get("move") if e.meta else None
        if not key:
            continue
        slot = by_stmt.setdefault(key, {"r": [], "w": []})
        slot["r" if e.kind is EdgeKind.READS else "w"].append(e)
    out: dict[tuple[str, str], list] = {}
    for slot in by_stmt.values():
        if len(slot["r"]) != 1:
            continue                          # a literal, or a subscripted/qualified source
        src = slot["r"][0].dst
        for w in slot["w"]:
            out.setdefault((src, w.dst), []).append(w)
    return out


def move_truncations(before: Graph, after: Graph, changed: set[str]) -> list[Finding]:
    """A MOVE whose sending field the change made larger than its receiving field.

    Widening a field (an account number from 9(11) to 9(13), a two-digit year to four)
    is the classic COBOL modernization, and every MOVE of it into a field nobody widened
    now silently drops high-order digits (numeric) or trailing characters (alphanumeric).
    No compiler reports it. Only MOVEs that existed before and truncate only because a
    field's size changed are listed: a MOVE written in this change is deliberate, and
    moving a long field into a short one is a common way to take its prefix
    (cobol-check: 50 such MOVEs in new generated code).
    """
    old_moves = _moves(before)
    out: list[Finding] = []
    for (s, t), edges in sorted(_moves(after).items()):
        if s not in changed and t not in changed:
            continue
        sn, tn = after.nodes.get(s), after.nodes.get(t)
        if sn is None or tn is None:
            continue
        sc, tc = _capacity(sn), _capacity(tn)
        if sc is None or tc is None or sc[0] != tc[0] or sc[1] <= tc[1]:
            continue
        so, to = before.nodes.get(s), before.nodes.get(t)
        if (s, t) not in old_moves or so is None or to is None:
            continue                          # a MOVE written in this change is deliberate
        osc, otc = _capacity(so), _capacity(to)
        if osc is None or otc is None or (osc[0] == otc[0] and osc[1] > otc[1]):
            continue                          # it already truncated before the change
        unit, what = sc[0], ("high-order digits" if sc[0] == "digits" else
                             "trailing characters")
        lost = sc[1] - tc[1]
        out.append(Finding(
            rule="move-truncates", severity="high",
            title=f"MOVE {sn.name} TO {tn.name} now drops {lost} {what}",
            detail=(f"{sn.name} holds {sc[1]} {unit} ({sn.meta.get('annotation', '')}) but "
                    f"{tn.name} holds {tc[1]} ({tn.meta.get('annotation', '')}); COBOL "
                    f"truncates the {what} without an error, so the value is silently "
                    f"wrong from here on."),
            node_id=tn.id, label=tn.qualname, path=edges[0].path, lineno=edges[0].lineno,
            evidence=[f"{after.nodes[e.src].qualname if e.src in after.nodes else e.src} at "
                      f"{e.path}:{e.lineno}" for e in edges[:8]],
            suggestion=f"Widen {tn.name} to match {sn.name} (and anything it is moved on to)."))
    return out

