"""What counts as a breaking change in C++.

Two entry points:

``arity_breaks(old, new)``
    Called by :func:`magellan_lite.polyglot.core.diff.arity_breaks` for a callable whose id survived the
    change (same name, same parameter types) but whose ``sig_hash`` did not. A changed
    parameter list is a different id, so it arrives as a removal plus an addition and is
    judged by ``removed-still-referenced``; what reaches here is defaults, qualifiers,
    return type, ``virtual``/``static``/``= delete``/``noexcept`` and access.

``findings(before, after, cs, root)``
    Breaks that need both graphs, reported under the existing rule names so the brief
    ranks and gates them like any other:

    * ``signature-break`` (medium) -- a call in an unedited file now binds to a different
      overload: an overload was added, removed or retyped and overload resolution picked
      another function. It compiles; it calls something else.
    * ``signature-break`` (critical/high) -- a method stopped overriding because the base
      method was made non-virtual (critical where the derived method says ``override``,
      which no longer compiles; high where it silently stops being called through the
      base), or the base became ``final``. A base method removed or retyped is a removal:
      the ``overrides`` edge makes it ``removed-still-referenced``.
    * ``signature-break`` (critical) -- a constructor became ``explicit`` while unedited code
      relied on it for an implicit conversion.
    * ``unhandled-new-member`` / ``non-exhaustive-match`` -- an enum gained a member that a
      ``switch`` which handled every old member, with no ``default``, now falls through.

Default arguments are substituted at the call site, so a changed default is charged only to
calls that omit it (core's ``omits`` test), and a removed one only to calls that passed
fewer arguments than the new minimum.
"""

from __future__ import annotations

from magellan_lite.polyglot.core.findings import Finding

LANG = "cpp"
_ACCESS = {"public": 0, "": 0, "protected": 1, "private": 2}


# --------------------------------------------------------------------------
# per-signature breaks
# --------------------------------------------------------------------------
def arity_breaks(old: dict, new: dict) -> list[dict]:
    out: list[dict] = []
    if not old or not new:
        return out
    o_types, n_types = list(old.get("types", [])), list(new.get("types", []))
    n_pos = list(new.get("positional", []))
    if o_types != n_types:
        out.append({"hard": True, "test": "always",
                    "text": f"parameter types changed ({', '.join(o_types)}) -> "
                            f"({', '.join(n_types)}) -- existing calls bind differently or "
                            f"stop compiling"})
        return out

    o_req, n_req = int(old.get("required_positional", 0)), int(new.get("required_positional", 0))
    if n_req > o_req:
        lost = n_pos[o_req:n_req]
        out.append({"hard": True, "test": "positional-count", "counts": list(range(o_req, n_req)),
                    "text": f"default argument removed for {', '.join(lost)} -- calls passing "
                            f"fewer than {n_req} arguments no longer compile"})

    o_def = _defaults(old)
    n_def = _defaults(new)
    for i in sorted(set(o_def) & set(n_def)):
        if o_def[i] != n_def[i]:
            name = n_pos[i] if i < len(n_pos) else f"#{i + 1}"
            out.append({"hard": False, "test": "omits", "name": name, "at": i + 1,
                        "text": f"default value changed for {name}: {o_def[i]} -> {n_def[i]} -- "
                                f"defaults are substituted at the call site, so callers that "
                                f"omit it get the new value without being edited"})

    o_ret, n_ret = old.get("returns", ""), new.get("returns", "")
    same_type = old.get("returns_canonical", o_ret) == new.get("returns_canonical", n_ret)
    if not same_type and not old.get("ctor"):
        hard = n_ret == "void" and o_ret != "void"
        out.append({"hard": hard, "test": "always",
                    "text": f"return type changed {o_ret or '?'} -> {n_ret or '?'} -- callers "
                            + ("that use the result no longer compile" if hard else
                               "that use the result convert it or stop compiling")})

    if old.get("static") != new.get("static"):
        out.append({"hard": True, "test": "always",
                    "text": ("became static" if new.get("static") else "no longer static")
                            + " -- calls through an object or the class name no longer match"})
    if new.get("deleted") and not old.get("deleted"):
        out.append({"hard": True, "test": "always",
                    "text": "now `= delete` -- every call that selects it is an error"})
    if _ACCESS.get(new.get("access", ""), 0) > _ACCESS.get(old.get("access", ""), 0):
        out.append({"hard": True, "test": "always",
                    "text": f"access narrowed {old.get('access') or 'public'} -> "
                            f"{new.get('access')} -- calls from outside the class (or its "
                            f"subclasses) no longer compile"})
    if old.get("virtual") and not new.get("virtual"):
        out.append({"hard": False, "test": "always",
                    "text": "no longer virtual -- calls through a base pointer or reference "
                            "now run this version, not the override"})
    if old.get("const") and not new.get("const"):
        out.append({"hard": False, "test": "always",
                    "text": "lost its const qualifier -- calls on const objects or through "
                            "const references stop compiling"})
    if old.get("ref", "") != new.get("ref", ""):
        out.append({"hard": False, "test": "always",
                    "text": f"ref-qualifier changed '{old.get('ref', '')}' -> "
                            f"'{new.get('ref', '')}' -- calls on lvalues or temporaries may "
                            f"stop matching"})
    if old.get("noexcept", "") != new.get("noexcept", ""):
        if new.get("noexcept") and not old.get("noexcept"):
            text = ("became noexcept -- an exception escaping it now calls std::terminate "
                    "instead of reaching the caller's handler")
        elif old.get("noexcept") and not new.get("noexcept"):
            text = ("no longer noexcept -- callers and containers that relied on it "
                    "(move_if_noexcept, noexcept(expr) checks, noexcept function pointers) "
                    "change behaviour or stop compiling")
        else:
            text = (f"exception specification changed {old.get('noexcept')} -> "
                    f"{new.get('noexcept')}")
        out.append({"hard": False, "test": "always", "text": text})
    if old.get("star_args") and not new.get("star_args"):
        out.append({"hard": True, "test": "reaches-slot", "at": len(n_pos) + 1,
                    "text": "no longer variadic -- calls passing extra arguments no longer "
                            "compile"})
    return out


def _defaults(arity: dict) -> dict[int, str]:
    req = int(arity.get("required_positional", 0))
    return {req + i: d for i, d in enumerate(arity.get("defaults", []))}


# --------------------------------------------------------------------------
# breaks that need both graphs
# --------------------------------------------------------------------------
def findings(before, after, cs, _root=None) -> list[Finding]:
    if before is None or after is None:
        return []
    edited = set(cs.files_modified + cs.files_added)
    out: list[Finding] = []
    out += _rebinds(before, after, edited)
    out += _lost_overrides(before, after, edited)
    out += _explicit(before, after, cs, edited)
    out += _exhaustive(before, after, cs)
    return out


def _is_cpp(node) -> bool:
    return node is not None and node.meta.get("lang") == LANG


def _sites(graph) -> dict[tuple, str]:
    """``(caller, overload set, file, line, column) -> callee id`` for resolved calls."""
    from magellan_lite.polyglot.core.model import EdgeKind
    out: dict[tuple, str] = {}
    for e in graph.edges:
        if e.kind is not EdgeKind.CALLS or e.dynamic or e.meta.get("via"):
            continue
        dst = graph.nodes.get(e.dst)
        src = graph.nodes.get(e.src)
        if not _is_cpp(dst) or src is None or "overload_key" not in dst.meta:
            continue
        out[(e.src, dst.meta["overload_key"], e.path, e.lineno, e.col)] = e.dst
    return out


def _rebinds(before, after, edited: set[str]) -> list[Finding]:
    old, new = _sites(before), _sites(after)
    groups: dict[str, list[tuple]] = {}
    for key, dst in new.items():
        was = old.get(key)
        if was is None or was == dst:
            continue
        src = after.nodes.get(key[0])
        if src is None or key[2] in edited:
            continue
        o, n = before.nodes.get(was), after.nodes.get(dst)
        if o is None or n is None:
            continue
        if was not in after.nodes and dst not in before.nodes:
            # the overload was edited in place (`T&` -> `T&&`, `int` -> `long`, a defaulted
            # parameter added or dropped): the calls follow their function, they are not
            # captured by a different one. Whether they still compile is for the removal
            # rules, which see the calls that no longer resolve.
            continue
        groups.setdefault(dst, []).append((src, (key[2], key[3]), o, n))
    out: list[Finding] = []
    for dst, hits in sorted(groups.items()):
        n = after.nodes[dst]
        olds = sorted({o.id for _s, _l, o, _n in hits})
        why = []
        if dst not in before.nodes:
            why.append(f"{n.qualname.split('@', 1)[-1]} is a new overload")
        for oid in olds:
            if oid not in after.nodes:
                why.append(f"{oid.split('@', 1)[-1]} was removed or retyped")
        hits.sort(key=lambda h: h[1])
        ev = [f"{s.qualname} at {where[0]}:{where[1]} -- was {o.qualname.split('@', 1)[-1]}, now "
              f"{nn.qualname.split('@', 1)[-1]}" for s, where, o, nn in hits[:12]]
        if len(hits) > 12:
            ev.append(f"... and {len(hits) - 12} more call sites")
        to_bool = _takes_only_bool(n) and not any(_takes_only_bool(o) for _s, _l, o, _n in hits)
        out.append(Finding(
            rule="signature-break", severity="high" if to_bool else "medium",
            title=(f"{len(hits)} unedited call(s) now bind to a different overload of "
                   f"{n.name}" + (" (taking bool: pointers and strings convert silently)"
                                  if to_bool else "")),
            detail=("Overload resolution picks a different function for these calls"
                    + (f" ({'; '.join(why)})" if why else "")
                    + ". They compile unchanged and silently call something else, with "
                      "whatever conversions the new match needs."),
            node_id=dst, label=n.qualname, path=n.path, lineno=n.lineno, evidence=ev,
            suggestion="Check each call still wants the overload it now gets; if not, make "
                       "the new overload harder to select (explicit types, a different "
                       "name) or cast at the call site."))
    return out


def _lost_overrides(before, after, edited: set[str]) -> list[Finding]:
    from magellan_lite.polyglot.core.model import EdgeKind
    groups: dict[str, list] = {}
    for e in before.edges:
        if e.kind is not EdgeKind.OVERRIDES:
            continue
        d_old, b_old = before.nodes.get(e.src), before.nodes.get(e.dst)
        if not _is_cpp(d_old) or not _is_cpp(b_old):
            continue
        d_new = after.nodes.get(e.src)
        if d_new is None:
            continue                            # the override itself went: its own removal
        b_new = after.nodes.get(e.dst)
        if b_new is None:
            continue                            # base removed or retyped: removed-still-referenced
        still = any(x.kind is EdgeKind.OVERRIDES for x in after.out_edges(d_new.id))
        made_final = "final" in b_new.tags and "final" not in b_old.tags
        if still and not made_final:
            continue
        if not made_final and "virtual" in b_new.tags:
            continue                            # base unchanged: the derived side changed
        why = "made final" if made_final else "no longer virtual"
        # clang drops an `override` that no longer overrides anything, so ask both sides
        marked = "override" in d_old.tags or "override" in d_new.tags
        groups.setdefault(e.dst, []).append((d_new, why, marked))
    out: list[Finding] = []
    for bid, hits in sorted(groups.items()):
        b = after.nodes.get(bid) or before.nodes[bid]
        loud = [d for d, why, marked in hits if marked or why == "made final"]
        unedited = [d for d, _w, _m in hits if d.path not in edited]
        severity = "critical" if any(d in unedited for d in loud) else \
            "high" if unedited else "medium"
        why = hits[0][1]
        ev = [f"{d.qualname} at {d.path}:{d.lineno}"
              + (" -- marked `override`: no longer compiles" if d in loud and why != "made final"
                 else " -- overriding a final function: no longer compiles"
                 if why == "made final" else
                 " -- silently stops being called through the base")
              + ("  (file was edited)" if d.path in edited else "")
              for d in sorted({d.id: d for d, _w, _m in hits}.values(), key=lambda d: d.id)][:12]
        out.append(Finding(
            rule="signature-break", severity=severity,
            title=f"{b.qualname.split('@', 1)[-1]} was {why}; {len(hits)} override(s) no longer "
                  f"override it",
            detail=("Derived classes overrode this virtual method. After the change their "
                    "versions no longer override it: calls through a base pointer or reference "
                    "run the base version (or fail to compile where `override`/`final` says "
                    "otherwise)."),
            node_id=bid, label=b.qualname, path=b.path, lineno=b.lineno, evidence=ev,
            suggestion="Update the overrides to the new signature, or keep the old virtual "
                       "method (possibly forwarding to the new one)."))
    return out


def _takes_only_bool(fn) -> bool:
    """An overload whose one parameter is a bool: anything pointer-like converts to it."""
    types = (fn.meta.get("arity") or {}).get("types") or []
    return len(types) == 1 and types[0].replace("const ", "").strip() == "bool"


def _explicit(before, after, cs, edited: set[str]) -> list[Finding]:
    from magellan_lite.polyglot.core.diff import ChangeKind
    from magellan_lite.polyglot.core.model import EdgeKind
    out: list[Finding] = []
    for ch in cs.changes:
        if ch.kind is not ChangeKind.SIGNATURE_CHANGED:
            continue
        o = (ch.before or {}).get("arity") or {}
        n = (ch.after or {}).get("arity") or {}
        if o.get("lang") != LANG or not n.get("explicit") or o.get("explicit"):
            continue
        # The after graph says what each site does now. Still bound to this constructor:
        # direct initialization (`Value("x")`), which explicit allows. Bound to another
        # overload: it compiles and silently converts differently -- _rebinds reports that
        # (jsoncpp: `v = "foo"` fell through to Value(bool)). Only a site that binds to
        # nothing any more fails to compile.
        key = (before.nodes[ch.node_id].meta.get("overload_key")
               if ch.node_id in before.nodes else None)
        now = _sites(after)
        sites = [e for e in before.in_edges(ch.node_id)
                 if e.kind is EdgeKind.CALLS and e.meta.get("implicit") and e.path not in edited
                 and (e.src, key, e.path, e.lineno, e.col) not in now]
        if not sites:
            continue
        ev = [f"{before.nodes[e.src].qualname} at {e.path}:{e.lineno}" for e in sites[:12]]
        out.append(Finding(
            rule="signature-break", severity="critical",
            title=f"Constructor {ch.label.split('@', 1)[-1]} became explicit; {len(sites)} "
                  f"implicit conversion(s) no longer compile",
            detail="These calls relied on the constructor converting an argument implicitly; "
                   "an explicit constructor is not considered for that.",
            node_id=ch.node_id, label=ch.label, path=ch.path, lineno=ch.lineno, evidence=ev,
            suggestion="Construct the value explicitly at each site, or drop `explicit`."))
    return out


def _enum_sets(graph) -> dict[str, tuple[str, ...]]:
    from magellan_lite.polyglot.core.model import NodeKind
    return {n.id: tuple(n.meta["enum_members"]) for n in graph.of_kind(NodeKind.CLASS)
            if _is_cpp(n) and n.meta.get("enum_members")}


def _exhaustive(before, after, cs) -> list[Finding]:
    from magellan_lite.polyglot.core.diff import ChangeKind
    old_sets, new_sets = _enum_sets(before), _enum_sets(after)
    if not new_sets:
        return []
    grown = {sid: tuple(m for m in ms if m not in old_sets[sid])
             for sid, ms in new_sets.items() if sid in old_sets}
    grown = {k: v for k, v in grown.items() if v}
    edited_fns = {c.node_id for c in cs.changes
                  if c.kind in (ChangeKind.ADDED, ChangeKind.BODY_CHANGED,
                                ChangeKind.SIGNATURE_CHANGED)}
    out: list[Finding] = []
    for node in after.nodes.values():
        sws = node.meta.get("switches") if _is_cpp(node) else None
        if not sws:
            continue
        for sw in sws:
            sid = sw["enum"]
            members = new_sets.get(sid)
            if members is None:
                continue
            covered = set(sw["covered"])
            name = sid.split("@", 1)[-1]
            where = f"the switch on `{sw['subject']}` in {node.qualname.split('@', 1)[-1]}"
            if sid in grown and set(old_sets[sid]) <= covered:
                missing = [m for m in grown[sid] if m not in covered]
                if not missing:
                    continue
                names = ", ".join(f"{name}::{m}" for m in missing)
                silent = sw["default"] == "none"
                out.append(Finding(
                    rule="unhandled-new-member", severity="high" if silent else "medium",
                    title=f"{names} added to {name} is not handled by {where}",
                    detail=(f"This change adds {names}. The switch handled every other member, "
                            f"so it was complete before; now "
                            + ("the new value matches no case and falls through silently."
                               if silent else "the new value takes the default branch, which "
                                              "may or may not be right for it.")),
                    node_id=node.id, label=node.qualname, path=node.path, lineno=sw["line"],
                    evidence=[f"handled here: {', '.join(sorted(covered))}",
                              f"not handled: {', '.join(missing)}", f"default: {sw['default']}"],
                    suggestion=f"Add a case for {names} (and build with -Wswitch-enum so the "
                               f"next member is caught by the compiler)."))
            elif node.id in edited_fns and sw["default"] == "none" and sid not in grown:
                missing = [m for m in members if m not in covered]
                if not missing or len(covered) < 2:
                    continue
                prior = before.nodes.get(node.id)
                prior_sw = [p for p in (prior.meta.get("switches") or [])
                            if p["enum"] == sid and p["subject"] == sw["subject"]] \
                    if prior is not None else []
                if any(p["default"] == "none" and set(missing) <= set(members) - set(p["covered"])
                       for p in prior_sw):
                    continue
                out.append(Finding(
                    rule="non-exhaustive-match", severity="medium",
                    title=f"{where[0].upper() + where[1:]} misses {', '.join(missing)} and has "
                          f"no default",
                    detail=(f"It handles {len(covered)} of {len(members)} members of {name}; a "
                            f"missing one matches no case and does nothing."),
                    node_id=node.id, label=node.qualname, path=node.path, lineno=sw["line"],
                    evidence=[f"not handled: {', '.join(missing)}"],
                    suggestion="Add the missing cases, or a default that fails loudly."))
    return out
