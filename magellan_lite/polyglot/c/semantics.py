"""What breaks a C caller when a function (or function-like macro) signature changes.

C has no default arguments, no keyword arguments and no overloads, so every call
passes every parameter by position. Consequences, each one an entry in the shape
:func:`magellan_lite.polyglot.core.diff.arity_breaks` returns:

* **Any change in the number of parameters is hard for every caller** that passed
  the old number (``test: "positional-count"`` with ``counts`` = those numbers).
  Calls that already passed another number were already broken.
* **A changed parameter type is hard unless the old argument converts implicitly**
  to the new type (compared position by position only when the count is unchanged).
  Arithmetic types, enums included, convert to each other (possibly narrowing: soft,
  and the text says so; ``int`` <-> ``enum`` is no change at all). ``void *`` accepts any object pointer. Adding ``const`` to a
  pointee is compatible. Anything else (another struct, pointer <-> integer, dropping
  ``const``, a struct passed by value) is hard: C compilers reject it or warn about an
  incompatible pointer, and the program is wrong either way.
* **Return type:** ``void`` -> value is harmless; value -> ``void`` is hard for callers
  that use the result; other changes follow the same conversion rules, reversed.
* **Variadic** (``...``): the fixed part is checked as above, and dropping the
  ``...`` breaks calls that passed extra arguments. Becoming variadic keeps sources
  compiling but changes the calling convention for already-built callers (soft).
* **``static``**: a function that became ``static`` disappears for callers in
  other files (they no longer link).
* **K&R** ``int f()`` accepts anything; gaining a prototype breaks calls whose
  count differs.
* **Macros** have no types; only the parameter count (and ``...``) matter, and a
  mismatch fails at expansion.

Types are compared in canonical form (typedefs resolved) when libclang ran, so
renaming a typedef or spelling ``size_t`` as ``unsigned long`` is not a change.
"""

from __future__ import annotations

import re

_QUALS = {"const", "volatile", "restrict", "__restrict", "__restrict__"}
_ARITH = {"char", "short", "int", "long", "unsigned", "signed", "float", "double", "_Bool",
          "bool", "__int128", "_Complex", "size_t", "ssize_t", "ptrdiff_t", "int8_t", "int16_t",
          "int32_t", "int64_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t", "intptr_t",
          "uintptr_t", "wchar_t", "off_t", "time_t"}
_RANK = {"_Bool": 0, "bool": 0, "char": 1, "short": 2, "int": 3, "long": 4, "long long": 5,
         "float": 6, "double": 7, "long double": 8}
MAX_EXTRA = 32


def _words(t: str) -> list[str]:
    return re.findall(r"[A-Za-z_]\w*|\*|\[|\]|\(|\)", t)


def _kind(t: str) -> str:
    """"void", "arith", "pointer", "record", "func" or "other" for a canonical type."""
    t = t.strip()
    if not t:
        return "other"
    if "(" in t and "*" not in t.split("(")[0] and t.endswith(")"):
        return "func"
    if "*" in t or "[" in t:
        return "pointer"
    w = [x for x in _words(t) if x not in _QUALS]
    if w == ["void"]:
        return "void"
    if w and (w[0] == "enum" or all(x in _ARITH for x in w)):
        return "arith"
    if w and w[0] in ("struct", "union"):
        return "record"
    return "other"


def _unqual(t: str) -> str:
    return " ".join(x for x in _words(t) if x not in _QUALS)


def _is_enum(t: str) -> bool:
    w = [x for x in _words(t) if x not in _QUALS]
    return bool(w) and w[0] == "enum"


def _rank(t: str) -> int:
    if _is_enum(t):
        return _RANK["int"]                         # an enum's values are ints (C11 6.4.4.3)
    w = [x for x in _words(t) if x not in _QUALS and x not in ("unsigned", "signed")]
    return _RANK.get(" ".join(w), 3 if not w else 4)


def _pointee(t: str) -> tuple[str, bool]:
    """``(pointee without qualifiers, pointee is const)`` for ``T *`` / ``T[]``."""
    t = t.strip()
    if t.endswith("]"):
        base = t[:t.index("[")]
    else:
        base = t[:t.rfind("*")] if "*" in t else t
    ws = _words(base)
    return " ".join(x for x in ws if x not in _QUALS), "const" in ws


def convert(old: str, new: str) -> tuple[bool | None, str]:
    """Does a value of type ``old`` pass implicitly where ``new`` is expected?

    ``(None, "")`` when nothing changes for the caller, ``(False, why)`` for a soft
    change (converts, maybe lossy), ``(True, why)`` for a hard one.
    """
    if _unqual(old) == _unqual(new) and _kind(old) != "pointer":
        return None, ""
    ko, kn = _kind(old), _kind(new)
    if ko == kn == "arith":
        lossy = _rank(new) < _rank(old) or ("unsigned" in old) != ("unsigned" in new) \
            or ("float" in new and "double" in old)
        if not lossy and _rank(new) == _rank(old) and (_is_enum(old) != _is_enum(new)):
            return None, ""                         # int <-> enum: the same value either way
        if lossy:
            return False, (f"{old} -> {new} converts implicitly but may truncate or change "
                           f"sign for existing arguments")
        return False, f"{old} -> {new} widens; existing arguments convert implicitly"
    if ko == kn == "pointer":
        po, co = _pointee(old)
        pn, cn = _pointee(new)
        if po == pn:
            if co and not cn:
                return True, (f"{old} -> {new} drops const: callers passing a const pointer "
                              f"violate a constraint (discarded qualifier)")
            return None, ""                 # const added, or only the pointer's own qualifiers
        if pn == "void":
            return None, ""
        if po == "void":
            return True, (f"{old} -> {new}: callers passing pointers other than {new} no "
                          f"longer convert implicitly")
        return True, (f"{old} -> {new}: incompatible pointer types (a hard error since "
                      f"GCC 14 and C23; undefined behaviour before)")
    if ko == "record" or kn == "record":
        return True, f"{old} -> {new}: a struct passed by value cannot convert"
    return True, f"{old} -> {new} does not convert implicitly"


def _counts(lo: int, variadic: bool) -> list[int]:
    return list(range(lo, lo + MAX_EXTRA + 1)) if variadic else [lo]


def arity_breaks(old: dict, new: dict) -> list[dict]:
    out: list[dict] = []
    if not old or not new:
        return out
    macro = bool(new.get("macro"))
    what = "macro" if macro else "function"
    o_types: list[str] = list(old.get("types") or [""] * len(old.get("positional", [])))
    n_types: list[str] = list(new.get("types") or [""] * len(new.get("positional", [])))
    no, nn = len(old.get("positional", [])), len(new.get("positional", []))
    ov, nv = bool(old.get("star_args")), bool(new.get("star_args"))

    if old.get("macro") != new.get("macro"):
        out.append({"hard": False, "test": "always",
                    "text": ("became a function-like macro -- arguments are no longer "
                             "type-checked and may be evaluated more than once" if macro else
                             "was a macro, now a function -- arguments are now type-checked "
                             "and evaluated once; code that relied on textual expansion "
                             "changes")})

    if not old.get("static") and new.get("static"):
        out.append({"hard": True, "test": "always",
                    "text": "became static -- callers in other files no longer link "
                            "(undefined reference)"})

    if old.get("noproto") and not new.get("noproto"):
        bad = [c for c in range(0, MAX_EXTRA + 1)
               if c != nn and not (nv and c >= nn)]
        out.append({"hard": True, "test": "positional-count", "counts": bad,
                    "text": f"gained a prototype with {nn} parameter(s) -- calls passing a "
                            f"different number no longer compile"})
        return out
    if new.get("noproto"):
        return out                                    # a K&R declaration checks nothing

    if no != nn:
        more = nn > no
        out.append({"hard": True, "test": "positional-count",
                    "counts": [c for c in _counts(no, ov) if not (nv and c >= nn) and c != nn],
                    "text": f"{what} takes {nn} parameter(s), was {no} -- every call passing "
                            f"{no}{' or more' if ov else ''} argument(s) "
                            + ("fails to expand" if macro else "fails to compile")
                            + (" (C has no default arguments)" if more and not macro else "")})
    if ov and not nv:
        out.append({"hard": True, "test": "positional-count",
                    "counts": list(range(nn + 1, nn + MAX_EXTRA + 1)),
                    "text": "no longer variadic -- calls passing extra arguments fail to "
                            + ("expand" if macro else "compile")})
    elif nv and not ov and not macro:
        out.append({"hard": False, "test": "always",
                    "text": "became variadic -- sources still compile, but already-built "
                            "callers use the non-variadic calling convention; rebuild them"})

    if not macro:
        # Positions only line up when the count did not change; after a parameter was
        # added or dropped, the count break already covers every call written for the
        # old list, and calls written for the new one are not affected by the old types.
        for i in range(min(no, nn) if no == nn else 0):
            o, n = o_types[i] if i < len(o_types) else "", n_types[i] if i < len(n_types) else ""
            if not o or not n:
                continue
            hard, why = convert(o, n)
            if hard is None:
                continue
            name = (new.get("positional") or [])[i] if i < nn else f"#{i + 1}"
            out.append({"hard": hard, "test": "always", "at": i + 1, "name": name,
                        "text": f"parameter {i + 1} ({name}) type changed: {why}"})
        o_ret, n_ret = old.get("ret", ""), new.get("ret", "")
        if o_ret and n_ret and _unqual(o_ret) != _unqual(n_ret):
            if _kind(n_ret) == "void":
                out.append({"hard": True, "test": "always",
                            "text": f"now returns void (was {o_ret}) -- callers that use the "
                                    f"result fail to compile"})
            elif _kind(o_ret) != "void":
                hard, why = convert(n_ret, o_ret)
                if hard is not None:
                    out.append({"hard": hard, "test": "always",
                                "text": f"return type changed ({o_ret} -> {n_ret}): "
                                        + why.split(": ", 1)[-1]})
    return out


def findings(before, after, cs, root) -> list:
    """Breaks that need both graphs: struct layout, enum/switch, prototype drift; and
    statements an unconditional jump makes unreachable in the functions the change edited.

    Called once per check by :func:`magellan_lite.polyglot.analyze.frontends.language_findings`;
    the rules live in :mod:`magellan_lite.polyglot.c.rules` and :mod:`magellan_lite.polyglot.c.flow`.
    """
    from magellan_lite.polyglot.c.flow import findings as flow_findings
    from magellan_lite.polyglot.c.rules import findings as c_findings
    return c_findings(before, after, cs, str(root)) + flow_findings(before, after, cs, str(root))
