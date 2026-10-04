"""What breaks a COBOL caller.

``CALL 'X' USING A B C`` binds arguments by position only, and by reference unless it
says otherwise: the callee sees the caller's storage through its LINKAGE SECTION
layout. Nothing checks that the two agree. So:

* a parameter added to ``PROCEDURE DIVISION USING`` breaks every caller that passes
  fewer arguments -- the callee addresses storage that was never passed (an 0C4 abend
  on a mainframe, or silent garbage);
* a parameter removed or reordered shifts every later argument onto the wrong layout;
* a LINKAGE item whose layout changed (PIC, USAGE, OCCURS, REDEFINES, a field added)
  reinterprets the caller's bytes: hard, and silent, for every caller that passes it;
* BY REFERENCE -> BY VALUE (or back) changes what is passed at all.

CICS programs reached by LINK/XCTL get their COMMAREA through ``DFHCOMMAREA``; a count
mismatch there is normal (EIBCALEN tells the program), but a layout change is not.

Entries use ``test: "positional-count"`` with the argument counts they break (see
:func:`magellan_lite.polyglot.core.diff.affects`); omitting ``counts`` means every call site.
"""
from __future__ import annotations

MAX_ARGS = 64

#: how the brief treats the checks in :mod:`magellan_lite.polyglot.cobol.rules`
RULES = {
    "perform-thru-range-changed": "blocking",
    "copybook-layout-changed": "blocking",
    "call-using-mismatch": "blocking",
    "move-truncates": "blocking",
    "fall-through-changed": "change",
    "legacy-construct-introduced": "informational",
}


def findings(before, after, changeset, root) -> list:
    """COBOL checks that need both graphs (copybook layouts, THRU ranges, fall-through,
    CALL USING mismatches, EVALUATE over 88-levels); see :mod:`magellan_lite.polyglot.cobol.rules`."""
    from magellan_lite.polyglot.cobol.rules import cobol_findings
    return cobol_findings(before, after, changeset, root)


def _counts(lo: int, hi: int = MAX_ARGS) -> list[int]:
    return list(range(max(lo, 0), hi + 1))


def arity_breaks(old: dict, new: dict) -> list[dict]:
    out: list[dict] = []
    o_pos, n_pos = list(old.get("positional", [])), list(new.get("positional", []))
    o_lay, n_lay = list(old.get("layouts", [])), list(new.get("layouts", []))
    o_size, n_size = list(old.get("sizes", [])), list(new.get("sizes", []))
    o_by, n_by = list(old.get("by", [])), list(new.get("by", []))
    cics = bool(new.get("cics") or old.get("cics"))
    no, nn = len(o_pos), len(n_pos)

    if nn > no and not cics:
        added = ", ".join(n_pos[no:])
        out.append({"hard": True, "test": "positional-count", "counts": _counts(0, nn - 1),
                    "text": f"USING now takes {nn} parameters, was {no} (new: {added}) -- "
                            f"callers passing fewer leave the callee addressing storage "
                            f"that was never passed"})
    shifted_from = None
    for k in range(min(no, nn)):
        if o_pos[k] != n_pos[k] and o_pos[k] in n_pos:
            shifted_from = k
            break
    if nn < no and not cics:
        gone = [p for p in o_pos if p not in n_pos]
        out.append({"hard": True, "test": "positional-count", "counts": _counts(nn + 1),
                    "text": f"USING now takes {nn} parameters, was {no}"
                            + (f" (dropped: {', '.join(gone)})" if gone else "")
                            + " -- callers still passing the old list bind later arguments "
                              "to the wrong LINKAGE items"})
    if shifted_from is not None:
        out.append({"hard": True, "test": "positional-count",
                    "counts": _counts(shifted_from + 1),
                    "text": f"parameter {o_pos[shifted_from]} moved from position "
                            f"{shifted_from + 1} to {n_pos.index(o_pos[shifted_from]) + 1} -- "
                            f"arguments bind by position, so callers now pass the wrong "
                            f"record"})
    for k in range(min(no, nn)):
        if shifted_from is not None and k >= shifted_from:
            break
        name = n_pos[k]
        lay_changed = k < len(o_lay) and k < len(n_lay) and o_lay[k] != n_lay[k] \
            and "?" not in (o_lay[k], n_lay[k])
        if lay_changed:
            os_, ns_ = (o_size[k] if k < len(o_size) else None,
                        n_size[k] if k < len(n_size) else None)
            size = (f" (size {os_} -> {ns_} bytes)" if os_ != ns_ and None not in (os_, ns_)
                    else "")
            brk = {"hard": True, "test": "positional-count", "counts": _counts(k + 1),
                   "text": f"LINKAGE layout of {name} changed{size} -- passed BY "
                           f"{(n_by[k] if k < len(n_by) else 'REFERENCE')}, so every caller "
                           f"compiled against the old layout is reinterpreted silently"}
            if cics:
                brk["counts"] = _counts(1)
            out.append(brk)
        if k < len(o_by) and k < len(n_by) and o_by[k] != n_by[k]:
            out.append({"hard": True, "test": "positional-count", "counts": _counts(k + 1),
                        "text": f"{name} is now passed BY {n_by[k]}, was BY {o_by[k]}"})
    if (old.get("returning") or "") != (new.get("returning") or ""):
        out.append({"hard": True, "test": "positional-count",
                    "text": f"RETURNING changed: {old.get('returning') or 'none'} -> "
                            f"{new.get('returning') or 'none'}"})
    return out


def still_names(text: str, name: str) -> bool:
    """Does COBOL source ``text`` still use ``name`` as a word (any case)?

    Read the way the frontend reads it: sequence and identification areas, comment
    lines, inline ``*>`` comments and literals do not count. Sequence areas often
    carry the program name, and ``DISPLAY 'CALC-TAX'`` names nothing.
    """
    from magellan_lite.polyglot.cobol.source import read, tokenize
    up = name.upper()
    if up not in text.upper():
        return False
    return any(t.kind == "WORD" and t.up == up for t in tokenize(read(text)))
