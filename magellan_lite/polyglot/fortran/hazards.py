"""Standing Fortran hazards that need no diff to be true.

Two F77 failure modes are invisible to the compiler and silent at run time:

* ``common-layout-mismatch``: two routines declare the same COMMON block with a
  different sequence of types or a different size. Storage is shared by position,
  so one of them reads the other's bytes as the wrong variable. This is the most
  common way an F77 upgrade breaks: one routine gets a member added or retyped and
  the others do not.
* ``implicit-interface-arg-mismatch``: a call to an external procedure with no
  interface in scope passes a different number of arguments than it has dummies.

And one that modernization introduces:

* ``intent-out-read-before-write``: an ``intent(out)`` dummy is read before it is
  assigned (its value is undefined on entry). minpack shipped this for IFLAG in
  FDJAC1/FDJAC2 when intents were added to the F77 code (fixed in fb8c03b).

Both are reported for the whole project when there is no "before" graph. Against a
baseline, a mismatch that already existed is ``low`` (still listed where it is
relevant to the change); one the change introduces is ``high`` and blocks.
"""

from __future__ import annotations

import json

from magellan_lite.polyglot.core.findings import Finding
from magellan_lite.polyglot.core.model import EdgeKind, Graph, NodeKind


def _common_mismatches(graph: Graph) -> dict[str, tuple]:
    out = {}
    for n in graph.nodes.values():
        if n.kind is NodeKind.GLOBAL and n.meta.get("lang") == "fortran" \
                and n.meta.get("layout_mismatch"):
            key = json.dumps(sorted(n.meta["layout_mismatch"]))
            out[n.id] = (n, key)
    return out


def _arg_mismatches(graph: Graph) -> dict[tuple, tuple]:
    out = {}
    for e in graph.edges:
        if e.kind is not EdgeKind.CALLS or e.dynamic or e.confidence < 0.9:
            continue
        if "args" not in e.meta or e.meta.get("binding") or e.meta.get("entry"):
            continue
        callee = graph.nodes.get(e.dst)
        caller = graph.nodes.get(e.src)
        if callee is None or caller is None or callee.meta.get("lang") != "fortran":
            continue
        ar = callee.meta.get("arity") or {}
        if ar.get("interface") != "implicit" or ar.get("bind") is not None:
            continue
        n = len(ar.get("positional", []))
        if e.meta["args"] == n:
            continue
        key = (caller.qualname, callee.id, e.meta["args"], n)
        out.setdefault(key, (caller, callee, e, n))
    return out


def _unset(graph: Graph) -> dict[tuple, tuple]:
    return {(n.id, v): (n, v, v in n.meta.get("reads_unset_in_loop", ()))
            for n in graph.nodes.values()
            if n.meta.get("lang") == "fortran" for v in n.meta.get("reads_unset", ())}


def _intent_out_reads(graph: Graph) -> dict[tuple, tuple]:
    out = {}
    for n in graph.nodes.values():
        if n.meta.get("lang") != "fortran" or n.kind is not NodeKind.FUNCTION:
            continue
        ar = n.meta.get("arity") or {}
        for p in ar.get("reads_before_write", ()):
            if ar.get("intents", {}).get(p) == "out":
                out[(n.id, p)] = (n, p)
    return out


def findings(before: Graph | None, after: Graph, changeset=None, _root=None) -> list[Finding]:
    out: list[Finding] = []
    changed = set()
    sig_changed = set()
    if changeset is not None:
        changed = set(changeset.files_modified + changeset.files_added)
        sig_changed = {c.node_id for c in changeset.changes
                       if getattr(c.kind, "value", c.kind) == "signature_changed"}

    old_common = _common_mismatches(before) if before is not None else {}
    for nid, (n, key) in sorted(_common_mismatches(after).items()):
        new = before is None or nid not in old_common or old_common[nid][1] != key
        layouts: dict = n.meta.get("layouts", {})
        where_path, where_line = n.path, n.lineno
        for q in layouts:
            dn = after.nodes.get(f"fn:{q}")
            if dn is not None and dn.path in changed:
                where_path, where_line = dn.path, dn.lineno
                break
        out.append(Finding(
            rule="common-layout-mismatch",
            severity="high" if new else "low",
            title=f"COMMON {n.name} is declared with different layouts"
                  + ("" if new else " (already before this change)"),
            detail=("Storage in a COMMON block is shared by position, not by name, so a "
                    "routine whose declaration differs reads another routine's bytes as the "
                    "wrong variable. The compiler does not check this across units. "
                    + "; ".join(n.meta["layout_mismatch"][:4])),
            node_id=n.id, label=n.name, path=where_path, lineno=where_line,
            evidence=[f"{who}: {lay}" for who, lay in sorted(layouts.items())][:12],
            suggestion=("Make every declaration identical, ideally by moving the COMMON "
                        "block into one INCLUDE file or replacing it with module variables."),
        ))

    old_reads = _intent_out_reads(before) if before is not None else {}
    for key, (n, p) in sorted(_intent_out_reads(after).items()):
        new = before is None or key not in old_reads
        out.append(Finding(
            rule="intent-out-read-before-write",
            severity="high" if new else "low",
            title=f"{n.qualname} reads intent(out) argument {p} before writing it"
                  + ("" if new else " (already before this change)"),
            detail=(f"An intent(out) dummy is undefined on entry, whatever the caller passed. "
                    f"{n.name} uses {p} before assigning it, so it depends on a value the "
                    "language says is gone (compilers may keep it, or not). Adding intents "
                    "while modernizing F77 code is where this appears."),
            node_id=n.id, label=n.qualname, path=n.path, lineno=n.lineno,
            evidence=[n.signature or n.name],
            suggestion=f"Declare {p} intent(inout), or assign it before its first use.",
        ))

    old_unset = _unset(before) if before is not None else {}
    for key, (n, v, in_loop) in sorted(_unset(after).items()):
        new = before is None or key not in old_unset
        out.append(Finding(
            rule="reads-unset-local",
            severity="medium" if new and not in_loop else "low",
            title=f"{n.qualname} reads local {v} before setting it"
                  + (" (or reads what an earlier loop iteration set)" if in_loop else "")
                  + ("" if new else " (already before this change)"),
            detail=(f"Nothing assigns {v} before its first use in {n.name}. F77 compilers "
                    "kept locals in static storage, so code like this silently used the value "
                    "left by the previous call; modern compilers, -frecursive and OpenMP do "
                    "not. ODEPACK fixed three of these (FREE in MDP, IHIT, LENWM)."
                    + (f" {v} is assigned later in the same loop, so the read is fine if "
                       "every path to it passes that assignment on an earlier iteration; "
                       "only the first iteration can be at fault." if in_loop else "")),
            node_id=n.id, label=n.qualname, path=n.path, lineno=n.lineno,
            evidence=[n.signature or n.name],
            suggestion=(f"Initialize {v}, or give it the SAVE attribute with an initial value "
                        "if it must persist between calls."),
        ))

    old_args = _arg_mismatches(before) if before is not None else {}
    for key, (caller, callee, e, n) in sorted(_arg_mismatches(after).items(),
                                               key=lambda kv: (kv[1][2].path, kv[1][2].lineno)):
        if callee.id in sig_changed:
            continue                           # signature-break already reports these calls
        new = before is None or key not in old_args
        out.append(Finding(
            rule="implicit-interface-arg-mismatch",
            severity="high" if new else "low",
            title=f"{caller.qualname} calls {callee.name} with {e.meta['args']} argument(s); "
                  f"it has {n}" + ("" if new else " (already before this change)"),
            detail=(f"{callee.name} is an external procedure with an implicit interface, so "
                    "the compiler does not check the call. The mismatch compiles and passes "
                    "the wrong storage at run time."),
            node_id=callee.id, label=callee.qualname, path=e.path, lineno=e.lineno,
            evidence=[f"{callee.signature or callee.name} at {callee.path}:{callee.lineno}"],
            suggestion=(f"Fix the call, and give {callee.name} an explicit interface (move it "
                        "into a module or declare an INTERFACE block) so the compiler checks "
                        "every caller."),
        ))
    return out
