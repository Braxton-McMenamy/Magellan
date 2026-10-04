"""C rules the shared ones cannot express. Run through ``semantics.findings``.

``signature-break`` (reused, so the verdict treats it like any broken contract)
    * a struct field changed type incompatibly and unedited code reads or writes it;
    * fields were reordered or inserted and unedited code initializes the struct
      positionally (``struct pt p = {1, 2};`` now binds the wrong fields);
    * a function's header prototype no longer matches its definition (callers
      compiled against the header pass arguments the definition does not expect).

``struct-layout-change``
    A struct or union declared in a header changed size or field offsets. Sources
    in the repository recompile, but anything compiled against the old layout
    (a shared library's users, plugins, data written to disk) reads the wrong
    bytes. Medium: it is a review item, not proof of a bug.

``unhandled-new-member`` / ``non-exhaustive-match`` (reused from the Python rule)
    An enum gained a member and a ``switch`` that handled every old member has no
    ``case`` for it; or new/edited code switches over an enum, misses members, and
    has no ``default``. Without a ``default`` the value silently does nothing.

``enum-values-shifted``
    A member was inserted before others, so existing members' numeric values
    changed; anything persisted or exchanged as a number now means something else.
"""

from __future__ import annotations

import re

from magellan_lite.polyglot.c.public import is_public
from magellan_lite.polyglot.c.semantics import convert
from magellan_lite.polyglot.core.diff import ChangeKind, ChangeSet
from magellan_lite.polyglot.core.findings import Finding
from magellan_lite.polyglot.core.model import EdgeKind, Graph, Node, NodeKind

_EDITED = (ChangeKind.ADDED, ChangeKind.BODY_CHANGED, ChangeKind.SIGNATURE_CHANGED)
_ACCESS = (EdgeKind.READS, EdgeKind.WRITES, EdgeKind.MUTATES)


def findings(before: Graph | None, after: Graph, cs: ChangeSet | None, root: str) -> list[Finding]:
    if before is None or cs is None:
        return []
    if not any(n.meta.get("lang") == "c" for n in after.nodes.values()):
        return []
    edited = set(cs.files_modified + cs.files_added)
    out: list[Finding] = []
    changed = {c.node_id for c in cs.changes}
    for nid in sorted(changed):
        o, n = before.nodes.get(nid), after.nodes.get(nid)
        if o is None or n is None or n.meta.get("lang") != "c":
            continue
        ck = n.meta.get("c_kind")
        if ck in ("struct", "union") and o.meta.get("layout") != n.meta.get("layout") \
                or ck in ("struct", "union") and o.meta.get("size") != n.meta.get("size"):
            out += _layout(before, after, o, n, edited, root)
        elif ck == "enum":
            out += _enum(before, after, o, n, edited)
        elif n.kind is NodeKind.FUNCTION and n.meta.get("prototype_mismatch") \
                and not o.meta.get("prototype_mismatch"):
            out.append(_mismatch(n))
    edited_fns = {c.node_id for c in cs.changes if c.kind in _EDITED}
    out += _new_non_exhaustive(before, after, edited_fns)
    for nid in sorted(changed - set(before.nodes)):
        n = after.nodes.get(nid)
        if n is not None and n.kind is NodeKind.FUNCTION and n.meta.get("prototype_mismatch"):
            out.append(_mismatch(n))
    return out


# --------------------------------------------------------------------------
# struct layout
# --------------------------------------------------------------------------
def _fields(layout: list[str]) -> list[tuple[str, str]]:
    out = []
    for item in layout or []:
        name, _, typ = item.partition(":")
        out.append((name, typ))
    return out


def _users(graph: Graph, cls: Node) -> dict[str, Node]:
    """Functions and globals whose code depends on ``cls``'s layout.

    Through typedefs of it and structs that embed it by value (their layout moves
    with it). A pointer to it does not depend on its layout, but a function using the
    pointer's fields does, and those come in through the field edges.
    """
    out: dict[str, Node] = {}
    todo, seen = [cls], {cls.id}
    while todo:
        c = todo.pop()
        fields = {e.dst for e in graph.out_edges(c.id) if e.kind is EdgeKind.CONTAINS}
        incoming = graph.in_edges(c.id) + [e for f in fields for e in graph.in_edges(f)
                                           if e.kind in _ACCESS]
        for e in incoming:
            src = graph.nodes.get(e.src)
            if src is None or src.id in seen:
                continue
            if e.kind is EdgeKind.ANNOTATES and src.kind is NodeKind.CLASS:
                embeds = src.meta.get("c_kind") == "typedef" or any(
                    t.strip() in (f"struct {c.name}", f"union {c.name}", c.name)
                    for _n, t in _fields(src.meta.get("layout", [])))
                if embeds:
                    seen.add(src.id)
                    todo.append(src)
                continue
            if src.kind in (NodeKind.FUNCTION, NodeKind.GLOBAL) and e.kind in (
                    EdgeKind.ANNOTATES,) + _ACCESS:
                out[src.id] = src
    return out


def _layout(before: Graph, after: Graph, o: Node, n: Node, edited: set[str],
            root: str | None = None) -> list[Finding]:
    out: list[Finding] = []
    of, nf = _fields(o.meta.get("layout", [])), _fields(n.meta.get("layout", []))
    old_t, new_t = dict(of), dict(nf)
    common = [f for f, _ in of if f in new_t]
    reordered = common != [f for f, _ in nf if f in old_t]
    added = [f for f, _ in nf if f not in old_t]
    inserted = any(f in added for f, _ in nf[:len(nf) - len(added)]) or reordered
    retyped = [(f, old_t[f], new_t[f]) for f in common if old_t[f] != new_t[f]]
    what = f"{n.meta.get('c_kind', 'struct')} {n.name}"

    for fname, t_old, t_new in retyped:
        hard, why = convert(t_old, t_new)
        if hard is None:
            continue
        fid = f"attr:{n.qualname}.{fname}"
        users = sorted({e.src for e in after.in_edges(fid) if e.kind in _ACCESS})
        stale = [after.nodes[u] for u in users if u in after.nodes
                 and after.nodes[u].path not in edited]
        if not stale:
            continue
        out.append(Finding(
            rule="signature-break", severity="high" if hard else "medium",
            title=f"Field {fname} of {what} changed type with {len(stale)} unedited user(s)",
            detail=f"{t_old} -> {t_new}. {why}.",
            node_id=fid, label=f"{n.qualname}.{fname}", path=n.path, lineno=n.lineno,
            evidence=[f"{u.qualname} at {u.path}:{u.lineno}" for u in stale[:12]],
            suggestion="Update the code that reads or writes the field, or keep its type."))

    if inserted:
        inits = [g for g in _users(after, n).values()
                 if g.kind is NodeKind.GLOBAL and g.path not in edited
                 and _positional_init(str(g.meta.get("value", "")))]
        if inits:
            out.append(Finding(
                rule="signature-break", severity="high",
                title=f"Fields of {what} were reordered or inserted; {len(inits)} unedited "
                      f"positional initializer(s) now bind the wrong fields",
                detail=("An initializer without designators (`{1, 2}`) assigns fields in "
                        "declaration order. After the reorder it still compiles when the "
                        "types line up, and fills the wrong members."),
                node_id=n.id, label=n.qualname, path=n.path, lineno=n.lineno,
                evidence=[f"{g.qualname} at {g.path}:{g.lineno}" for g in inits[:12]],
                suggestion="Use designated initializers (`.x = 1`), or keep the field order."))

    size_changed = o.meta.get("size") not in (None, -1) and n.meta.get("size") not in (None, -1) \
        and o.meta.get("size") != n.meta.get("size")
    moved = inserted or bool(retyped) or size_changed or \
        [f for f, _ in of] != [f for f, _ in nf]
    if moved and n.path.endswith(".h"):
        users = _users(after, n)
        detail = []
        if size_changed:
            detail.append(f"size {o.meta['size']} -> {n.meta['size']} bytes")
        if added:
            detail.append(f"added {', '.join(added)}")
        removed = [f for f, _ in of if f not in new_t]
        if removed:
            detail.append(f"removed {', '.join(removed)}")
        if reordered:
            detail.append("fields reordered")
        if retyped:
            detail.append("retyped " + ", ".join(f for f, _a, _b in retyped))
        public = is_public(root, n.path)
        out.append(Finding(
            rule="struct-layout-change", severity="high" if public else "medium",
            title=f"Layout of {what} changed ({'; '.join(detail) or 'field offsets'})"
                  + (" in an installed header" if public else ""),
            detail=(("The build installs this header, so programs compiled against the old "
                     "layout read and write fields at the wrong offsets until they are "
                     "rebuilt: an ABI break for every consumer of the library."
                     if public else
                     "The declaration is in a header, so code outside this change may be "
                     "compiled against it. Everything here recompiles, but already-built "
                     "users (plugins, data written to disk or the wire) read fields at the "
                     "old offsets.")),
            node_id=n.id, label=n.qualname, path=n.path, lineno=n.lineno,
            evidence=[f"{len(users)} function(s)/global(s) here depend on the layout"]
                     + [f"{u.qualname} at {u.path}:{u.lineno}" for u in
                        sorted(users.values(), key=lambda u: (u.path, u.lineno))[:6]],
            suggestion=("If this is a public ABI, bump the soname/major version, or append "
                        "fields at the end and keep existing ones in place.")))
    return out


_DESIGNATOR = re.compile(r"[{,]\s*(?:\.\s*[A-Za-z_]\w*|\[[^\]]*\])")


def _positional_init(value: str) -> bool:
    """``{1, 2}`` binds fields by position; ``{.x = 1}`` and ``{0}`` do not depend on order.

    A designator is ``.name`` or ``[index]`` right after ``{`` or ``,``; a ``.`` inside
    a number (``{1.5, 2.5}``) is not one.
    """
    v = value.strip()
    if not v.startswith("{") or re.fullmatch(r"\{\s*0?\s*\}", v):
        return False
    return not _DESIGNATOR.search(v.split("}")[0])


# --------------------------------------------------------------------------
# enums and switch
# --------------------------------------------------------------------------
def _switches(graph: Graph, enum_id: str) -> list[tuple[Node, dict]]:
    out = []
    for fn in graph.of_kind(NodeKind.FUNCTION):
        for s in fn.meta.get("switches", ()) or ():
            if s.get("enum") == enum_id:
                out.append((fn, s))
    return out


def _enum(before: Graph, after: Graph, o: Node, n: Node, edited: set[str]) -> list[Finding]:
    out: list[Finding] = []
    old_m = list(o.meta.get("enum_members") or [])
    new_m = list(n.meta.get("enum_members") or [])
    added = [m for m in new_m if m not in old_m]
    if added and len(old_m) >= 2:
        for fn, s in _switches(after, n.id):
            covered = set(s.get("covered", ()))
            if not set(old_m) <= covered:
                continue
            missing = [m for m in added if m not in covered]
            if not missing:
                continue
            names = ", ".join(missing)
            fate = ("matches no case and the switch does nothing -- silently" if not s["default"]
                    else "takes the default branch, which may or may not be right for it")
            out.append(Finding(
                rule="unhandled-new-member", severity="high" if not s["default"] else "medium",
                title=f"{names} added to enum {n.name} is not handled by the switch in {fn.name}",
                detail=(f"This change adds {names} to enum {n.name}. The switch on "
                        f"`{s.get('subject', '?')}` in {fn.qualname} handled every other member, "
                        f"so it was complete before; now {names} {fate}."),
                node_id=fn.id, label=fn.qualname, path=s.get("path", fn.path),
                lineno=s.get("line", fn.lineno),
                evidence=[f"enum {n.name} is defined at {n.path}:{n.lineno}",
                          f"handled here: {', '.join(sorted(covered))}",
                          f"not handled: {names}", f"default: {'yes' if s['default'] else 'none'}"],
                suggestion=f"Add a case for {names}"
                           + ("" if s["default"] else ", and compile with -Wswitch-enum") + "."))
    shifted = []
    for m in old_m:
        if m not in new_m:
            continue
        a = before.nodes.get(f"attr:{o.qualname}.{m}")
        b = after.nodes.get(f"attr:{n.qualname}.{m}")
        if a is not None and b is not None and a.meta.get("value") != b.meta.get("value"):
            shifted.append(f"{m} {a.meta.get('value')} -> {b.meta.get('value')}")
    if shifted and n.path.endswith(".h"):
        out.append(Finding(
            rule="enum-values-shifted", severity="medium",
            title=f"Values of {len(shifted)} existing member(s) of enum {n.name} changed",
            detail=("A member was inserted or a value edited, so existing names now stand "
                    "for different numbers. Compiled users, stored data and wire formats that "
                    "carry the number read the wrong member."),
            node_id=n.id, label=n.qualname, path=n.path, lineno=n.lineno,
            evidence=shifted[:8],
            suggestion="Append new members at the end, or give members explicit values."))
    return out


def _new_non_exhaustive(before: Graph, after: Graph, edited_fns: set[str]) -> list[Finding]:
    out: list[Finding] = []
    for fid in sorted(edited_fns):
        fn = after.nodes.get(fid)
        if fn is None or fn.meta.get("lang") != "c":
            continue
        prior = before.nodes.get(fid)
        old = {(s.get("enum"), s.get("subject")): s for s in
               (prior.meta.get("switches", ()) if prior is not None else ())}
        for s in fn.meta.get("switches", ()) or ():
            if s.get("default"):
                continue
            enum = after.nodes.get(s.get("enum", ""))
            if enum is None:
                continue
            members = list(enum.meta.get("enum_members") or [])
            missing = [m for m in members if m not in set(s.get("covered", ()))]
            if not missing:
                continue
            p = old.get((s.get("enum"), s.get("subject")))
            if p is not None and not p.get("default") and \
                    set(missing) <= set(members) - set(p.get("covered", ())):
                continue                           # it was already this incomplete
            names = ", ".join(missing[:6]) + (" ..." if len(missing) > 6 else "")
            out.append(Finding(
                rule="non-exhaustive-match", severity="medium",
                title=f"The switch on `{s.get('subject', '?')}` in {fn.name} misses {names} "
                      f"and has no default",
                detail=(f"It handles {len(s.get('covered', ()))} of {len(members)} members of "
                        f"enum {enum.name}; a missing one runs no case."),
                node_id=fid, label=fn.qualname, path=s.get("path", fn.path),
                lineno=s.get("line", fn.lineno),
                evidence=[f"enum {enum.name} is defined at {enum.path}:{enum.lineno}",
                          f"not handled: {', '.join(missing)}"],
                suggestion="Add the missing cases, or a default that fails loudly."))
    return out


# --------------------------------------------------------------------------
def _mismatch(n: Node) -> Finding:
    pm = n.meta["prototype_mismatch"]
    return Finding(
        rule="signature-break", severity="high",
        title=f"The prototype of {n.name} no longer matches its definition",
        detail=(f"Declared at {pm['prototype']}, defined at {pm['definition']} with a different "
                f"signature. Callers compile against the prototype and pass arguments the "
                f"definition reads differently; if the definition's file does not include the "
                f"header, no compiler notices."),
        node_id=n.id, label=n.qualname, path=n.path, lineno=n.lineno,
        evidence=[f"prototype: {pm['prototype']}", f"definition: {pm['definition']}"],
        suggestion="Make the header prototype and the definition agree, and include the "
                   "header in the file that defines the function.")
