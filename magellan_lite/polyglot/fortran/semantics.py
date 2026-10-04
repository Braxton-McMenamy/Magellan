"""What counts as a breaking signature change in Fortran.

Two regimes, decided by the callee's ``arity["interface"]``:

* **implicit** (an F77-style external procedure with no interface in scope): the
  compiler checks nothing at the call site. Argument association is by position
  only, by reference, with no count, type, kind or rank check. Any change to the
  dummy argument list is therefore hard *and silent*: callers still compile and
  pass the wrong storage, which corrupts memory or reads garbage at run time.
* **explicit** (module and internal procedures, interface bodies): the compiler
  checks every call, keyword arguments are allowed, ``optional`` arguments may be
  omitted. A break is a compile error at each affected call site (hard, but loud),
  except for ``intent`` changes, which the compiler cannot see from the caller's side
  and which change behaviour.

Entries use the shared shape (``text``, ``hard``, ``test``) so
:func:`magellan_lite.polyglot.core.diff.affects` can decide per call site; ``positional-count`` is
used where only the number of actual arguments matters.
"""

from __future__ import annotations

import re

#: How the brief treats the rules only Fortran reports (``analyze.frontends.rule_classes``).
#: The three are silent at compile time and wrong at run time, so one a change introduces
#: (``high``) blocks; one that was already there is ``low``. ``reads-unset-local`` keeps the
#: default: listed when it points at an edited file, never the reason for a block.
RULES = {
    "common-layout-mismatch": "blocking",
    "implicit-interface-arg-mismatch": "blocking",
    "intent-out-read-before-write": "blocking",
}

SILENT = ("implicit interface: the compiler does not check this call, so it still "
          "compiles and passes the wrong storage at run time")
LOUD = "explicit interface: each affected call is a compile error"

#: (old, new) intent -> hard?  ("" = unspecified, which behaves like inout)
_INTENT_HARD = {
    ("in", "out"): True, ("inout", "out"): True, ("", "out"): True,
    ("out", "in"): True, ("inout", "in"): True, ("", "in"): False,
    ("in", "inout"): False, ("in", ""): False, ("out", "inout"): False,
    ("out", ""): False, ("inout", ""): False, ("", "inout"): False,
}


#: intent changes worth reporting when the body gives no stronger evidence. Adding an
#: intent to an argument that had none is what every modernization does; `in` is then
#: checked by the compiler against the body, `inout` is what unspecified meant anyway,
#: and `out` is only wrong if the body reads the argument first (handled separately).
_INTENT_REPORTED = {("in", "out"), ("inout", "out"), ("out", "in"), ("inout", "in"),
                    ("in", "inout"), ("in", ""), ("out", "inout"), ("out", "")}


def _counts_other_than(ok: set[int], upto: int) -> list[int]:
    return [c for c in range(0, max(upto, 1) + 16) if c not in ok]


def arity_breaks(old: dict, new: dict) -> list[dict]:
    out: list[dict] = []
    if not old or not new:
        return out
    implicit = new.get("interface") == "implicit" or old.get("interface") == "implicit"
    why = SILENT if implicit else LOUD
    o_pos, n_pos = list(old.get("positional", [])), list(new.get("positional", []))
    o_opt, n_opt = set(old.get("optional", [])), set(new.get("optional", []))
    o_types, n_types = old.get("types", {}), new.get("types", {})
    o_shapes, n_shapes = old.get("shapes", {}), new.get("shapes", {})
    o_int, n_int = old.get("intents", {}), new.get("intents", {})

    if old.get("kind") != new.get("kind") and old.get("kind") and new.get("kind"):
        out.append({"hard": True, "test": "always",
                    "text": f"changed from a {old['kind']} to a {new['kind']} -- every "
                            f"CALL or function reference is now wrong ({why})"})
        return out

    if old.get("bind") != new.get("bind") and (old.get("bind") is not None
                                               or new.get("bind") is not None):
        out.append({"hard": True, "test": "always",
                    "text": f"bind(C) name changed ({old.get('bind')!r} -> "
                            f"{new.get('bind')!r}) -- C callers link against the old symbol"})

    shift = _shift(o_pos, n_pos)
    if implicit:
        # positional only; the count and every slot's type/kind/rank must match exactly
        if len(o_pos) != len(n_pos):
            added = [p for p in n_pos if p not in o_pos]
            dropped = [p for p in o_pos if p not in n_pos]
            what = "; ".join(x for x in (
                f"added {', '.join(added)}" if added else "",
                f"dropped {', '.join(dropped)}" if dropped else "") if x)
            out.append({"hard": True, "test": "positional-count",
                        "counts": _counts_other_than({len(n_pos)}, max(len(o_pos), len(n_pos))),
                        "text": f"takes {len(n_pos)} arguments, was {len(o_pos)}"
                                + (f" ({what})" if what else "")
                                + f" -- callers passing {len(o_pos)} still compile ({SILENT})"})
        elif shift is not None:
            k, names = shift
            out.append({"hard": True, "test": "reaches-slot", "at": k + 1, "name": names[0],
                        "text": f"arguments reordered from position {k + 1} "
                                f"({', '.join(names)}) -- callers bind the wrong actuals "
                                f"({SILENT})"})
        for i, p in enumerate(n_pos):
            if i >= len(o_pos):
                break
            q = o_pos[i]
            if p != q and (p in o_pos or q in n_pos):
                continue                        # a shift, reported above
            ot, nt = o_types.get(q), n_types.get(p)
            if ot and nt and ot != nt and ot != "?" and nt != "?":
                out.append({"hard": True, "test": "reaches-slot", "at": i + 1, "name": p,
                            "text": f"argument {i + 1} ({p}) changed type {ot} -> {nt} -- "
                                    f"callers pass {ot} storage ({SILENT})"})
            os_, ns = o_shapes.get(q, "scalar"), n_shapes.get(p, "scalar")
            if os_ != ns and _shape_matters(os_, ns, implicit=True):
                out.append({"hard": True, "test": "reaches-slot", "at": i + 1, "name": p,
                            "text": f"argument {i + 1} ({p}) changed shape {os_} -> {ns}"
                                    + (" -- an assumed-shape dummy needs an explicit "
                                       "interface; implicit callers pass a bare address"
                                       if ns.startswith("assumed-shape") else
                                       f" -- {SILENT}")})
    else:
        n_req = int(new.get("required_positional", 0))
        o_req = int(old.get("required_positional", 0))
        if n_req > o_req:
            added = [p for p in n_pos[:n_req] if p not in o_pos or p in o_opt]
            out.append({"hard": True, "test": "fewer-positional", "required": n_pos[:n_req],
                        "text": f"requires {n_req} arguments, was {o_req}"
                                + (f" (new required: {', '.join(added)})" if added else "")
                                + f" -- {LOUD}"})
        # a dummy renamed in place: positional calls are fine, keyword calls are not
        renamed = {o_pos[i]: n_pos[i] for i in range(min(len(o_pos), len(n_pos)))
                   if o_pos[i] != n_pos[i] and o_pos[i] not in n_pos and n_pos[i] not in o_pos}
        for old_name, new_name in renamed.items():
            out.append({"hard": True, "test": "uses-keyword", "name": old_name,
                        "text": f"argument {old_name} renamed to {new_name} -- calls passing "
                                f"{old_name}= by keyword no longer compile"})
        for p in o_pos:
            if p not in n_pos and p not in renamed:
                out.append({"hard": True, "test": "uses-slot-or-keyword", "name": p,
                            "at": o_pos.index(p) + 1,
                            "text": f"dropped argument {p} -- calls passing it by keyword or "
                                    f"reaching it by position no longer match ({LOUD})"})
        if shift is not None:
            k, names = shift
            out.append({"hard": True, "test": "reaches-slot", "at": k + 1, "name": names[0],
                        "text": f"arguments from position {k + 1} moved ({', '.join(names)}) -- "
                                f"positional calls reaching them bind the wrong actual; keyword "
                                f"calls are unaffected"})
        for p in n_pos:
            if p in o_pos and p in o_opt and p not in n_opt:
                out.append({"hard": True, "test": "omits", "name": p,
                            "at": n_pos.index(p) + 1,
                            "text": f"{p} is no longer optional -- calls that omit it no "
                                    f"longer compile"})
        # a new optional argument inserted before existing ones shifts positional callers
        for i, p in enumerate(n_pos):
            if p not in o_pos and p in n_opt and any(q in o_pos for q in n_pos[i + 1:]):
                out.append({"hard": True, "test": "reaches-slot", "at": i + 1, "name": p,
                            "text": f"new optional argument {p} inserted at position {i + 1} "
                                    f"-- positional calls reaching it shift by one"})
        for p in n_pos:
            if p not in o_pos:
                continue
            ot, nt = o_types.get(p), n_types.get(p)
            if ot and nt and ot != nt and ot != "?" and nt != "?":
                poly = _polymorphic_change(ot, nt)
                if poly == "widened":
                    pass                        # type(t) -> class(t), class(t) -> class(*)
                elif poly == "narrowed":
                    out.append({"hard": False, "test": "uses-slot-or-keyword", "name": p,
                                "at": o_pos.index(p) + 1,
                                "text": f"argument {p} changed type {ot} -> {nt} -- callers "
                                        f"whose actual argument is not a {_tname(nt)} (or an "
                                        f"extension of it) no longer compile"})
                else:
                    out.append({"hard": True, "test": "uses-slot-or-keyword", "name": p,
                                "at": o_pos.index(p) + 1,
                                "text": f"argument {p} changed type {ot} -> {nt} ({LOUD})"})
            os_, ns = o_shapes.get(p, "scalar"), n_shapes.get(p, "scalar")
            if os_ != ns and _shape_matters(os_, ns, implicit=False):
                out.append({"hard": _rank(os_) != _rank(ns), "test": "uses-slot-or-keyword",
                            "name": p, "at": o_pos.index(p) + 1,
                            "text": f"argument {p} changed shape {os_} -> {ns}"
                                    + (f" ({LOUD})" if _rank(os_) != _rank(ns) else
                                       " -- same rank; sequence association (passing an "
                                       "element to start an array) and copy-in/out change")})
    rbw = set(new.get("reads_before_write", ()))
    for p in n_pos:
        if p not in o_pos:
            continue
        oi, ni = o_int.get(p, ""), n_int.get(p, "")
        if oi == ni:
            continue
        if ni == "out" and p in rbw:
            out.append({"hard": True, "test": "uses-slot-or-keyword", "name": p,
                        "at": n_pos.index(p) + 1,
                        "text": f"{p} became intent(out) but the procedure reads it before "
                                f"writing it -- the value callers pass is undefined on entry"})
            continue
        if (oi, ni) not in _INTENT_REPORTED:
            continue                 # e.g. unspecified -> in: the compiler checks the body
        if oi == "in" and ni in ("inout", "") and "writes" in new and p not in new["writes"]:
            continue                 # intent(in) dropped, but nothing in the body writes it
        if oi == "out" and ni in ("inout", "") and "reads_before_write" in new and p not in rbw:
            continue                 # now inout, but the body never reads the incoming value
        if oi in ("out", "inout") and ni == "in" and "writes" in old and p not in old["writes"]:
            continue                 # was never written (LAPACK xLAQZ1 SR1: a wrong intent fixed)
        hard = _INTENT_HARD.get((oi, ni), False)
        out.append({"hard": hard, "test": "uses-slot-or-keyword", "name": p,
                    "at": n_pos.index(p) + 1,
                    "text": f"intent of {p} changed {oi or 'unspecified'} -> "
                            f"{ni or 'unspecified'} -- " + _intent_text(oi, ni)})
    if "result" in old and "result" in new and old["result"] != new["result"]:
        out.append({"hard": True, "test": "always",
                    "text": f"function result changed {old['result']} -> {new['result']} -- "
                            + ("callers declare the result type themselves, so they read "
                               "the wrong bits (" + SILENT + ")" if implicit else LOUD)})
    return out


def _shift(o_pos: list[str], n_pos: list[str]) -> tuple[int, list[str]] | None:
    """First position where a surviving argument sits elsewhere, and the ones that moved."""
    moved = [p for p in o_pos if p in n_pos and o_pos.index(p) != n_pos.index(p)]
    if not moved:
        return None
    first = min(min(o_pos.index(p), n_pos.index(p)) for p in moved)
    return first, moved


def _rank(shape: str) -> int:
    try:
        return int(shape.rsplit("/", 1)[1])
    except (IndexError, ValueError):
        return 0


def _shape_matters(old: str, new: str, implicit: bool) -> bool:
    if old == new:
        return False
    if implicit:
        # explicit-shape and assumed-size are both "an address": interchangeable for callers
        o, n = old.split("/")[0], new.split("/")[0]
        if {o, n} <= {"explicit", "assumed-size"} and old != "scalar" and new != "scalar":
            return False
    return True


def _intent_text(old: str, new: str) -> str:
    if new == "out" and old == "in":
        return "the caller's variable, read-only until now, is overwritten"
    if new == "out":
        return "the incoming value is discarded; the argument is output only"
    if new == "in" and old in ("out", "inout", ""):
        return "results are no longer written back; callers reading them after the call break"
    if new in ("inout", "") and old == "in":
        return "the procedure may now modify the caller's variable"
    if new == "inout" and old == "out":
        return "the incoming value is now read; callers passing an undefined variable change"
    return "behaviour at the call site changes without an edit there"


def findings(before, after, cs, root) -> list:
    """Fortran hazards for ``check``/``brief`` (the shared ``language_findings`` hook).

    COMMON layout mismatches, implicit-interface argument-count mismatches, ``intent(out)``
    read before write and locals read before set; see :mod:`magellan_lite.polyglot.fortran.hazards`.
    """
    from magellan_lite.polyglot.fortran.hazards import findings as hazards
    return hazards(before, after, cs, root)


def still_names(text: str, name: str) -> bool:
    """Does Fortran source ``text`` (one unit's lines, or a file) still use ``name``?

    Read the way the frontend reads it: names are case-insensitive (``CALL DGEFA`` names
    ``dgefa``), and comments (``!``, ``C``/``*`` in column 1 of fixed form), string
    constants and sequence columns 73+ do not count. ``core.diff.surviving_references``
    calls this for an edited referrer of a removed procedure.
    """
    from magellan_lite.polyglot.fortran import source as fsrc
    src = fsrc.read(text, "", form=_form_of_slice(text))
    pat = re.compile(r"(?<![\w$])" + re.escape(name.lower()) + r"(?![\w$])")
    return any(pat.search(st.code) for st in src.stmts)


def _form_of_slice(text: str) -> str:
    """Fixed or free form, for a slice of a file whose suffix is not at hand.

    Fixed-form code starts in column 7 (a label and a continuation mark before it). A line
    with a letter in columns 1-5 is free form, unless column 1 makes it a fixed-form
    comment; a line that is only a ``!`` comment says nothing.
    """
    for raw in text.splitlines():
        if not raw.strip() or raw[0] in "cC*!dD#\t" or raw.lstrip().startswith("!"):
            continue
        if any(c.isalpha() or c == "&" for c in raw[:5]):
            return "free"
    return "fixed"


def _tname(t: str) -> str:
    """``class(foo_t)`` / ``type(foo_t)`` -> ``foo_t``."""
    return t[t.index("(") + 1:t.rindex(")")].strip() if "(" in t and ")" in t else t


def _polymorphic_change(ot: str, nt: str) -> str:
    """How a derived-type dummy changed: ``widened`` (every old actual still fits),
    ``narrowed`` (some actuals may not fit; which ones needs the caller's type), or ``""``.

    ``type(t)`` -> ``class(t)`` accepts t and its extensions, so it widens (fpm 2c948998);
    ``class(t)`` -> ``class(u)`` with u an extension of t narrows, and only callers that
    already pass a u still compile (fpm ed27f9bf passes one).
    """
    o, n = ot.lower().replace(" ", ""), nt.lower().replace(" ", "")
    if not (o.startswith(("type(", "class(")) and n.startswith(("type(", "class("))):
        return ""
    if o.startswith("type(") and n == "class(" + o[5:]:
        return "widened"
    if n == "class(*)":
        return "widened"
    if o.startswith("class(") or n.startswith("class("):
        return "narrowed"
    return ""


def rebound(old, new, edge, target) -> bool:
    """Does the referrer now read a same-named variable where it called ``target``?

    ``x = dpmpar(1)`` indexes an array exactly as it called a function, so replacing the
    function with a module variable of the same name (minpack 7a414a31) leaves the
    reference valid: the referrer's new edges say it reads the variable now.
    """
    from magellan_lite.polyglot.core.model import EdgeKind, NodeKind
    if not target.kind.is_callable:
        return False
    name = target.name.lower()
    for x in new.out_edges(edge.src):
        if x.kind is not EdgeKind.READS or x.dst == target.id:
            continue
        n = new.nodes.get(x.dst)
        if n is not None and n.name.lower() == name and n.kind in (
                NodeKind.GLOBAL, NodeKind.CLASS_ATTR):
            return True
    return False

