"""leap-day-date: a date rebuilt with another year, which fails on February 29.

Windows Azure, February 29 2012: every new VM's guest agent made a certificate valid until
"the same date next year" by adding one to the year. February 29 2013 does not exist, the
certificate could not be created, and the failure cascaded through Azure's clusters for 34
hours.

Python fails the same way: ``date(2025, 2, 29)`` raises ValueError. So
``d.replace(year=d.year + 1)`` and ``date(d.year + 1, d.month, d.day)`` pass every test on
every day but one in four years. Quiet when the year moves by a multiple of 4 (a leap year
again), inside a ``try`` that catches ValueError, and in a function that already deals with
the day: it compares something with 29 or a ``.month`` with 2, or asks ``isleap`` or
``monthrange``.
"""

from __future__ import annotations

import ast

from magellan_lite.findings import rule

_CONSTRUCTORS = {"date", "datetime"}
_CATCHES = {"ValueError", "Exception", "BaseException"}
_LEAP_AWARE = {"isleap", "monthrange", "leapdays"}


def _year_offset(expr: ast.expr | None):
    """``x.year + n``, ``n + x.year`` or ``x.year - n``: the offset (an int, or the
    expression when it is not a number); ``None`` when ``expr`` is not a year moved."""
    if not isinstance(expr, ast.BinOp) or not isinstance(expr.op, (ast.Add, ast.Sub)):
        return None
    is_year = lambda e: isinstance(e, ast.Attribute) and e.attr == "year"   # noqa: E731
    if is_year(expr.left):
        n = expr.right
    elif is_year(expr.right) and isinstance(expr.op, ast.Add):
        n = expr.left
    else:
        return None
    if isinstance(n, ast.Constant) and type(n.value) is int:
        return n.value if isinstance(expr.op, ast.Add) else -n.value
    return n


def _arg(call: ast.Call, index: int, name: str) -> ast.expr | None:
    if len(call.args) > index:
        return call.args[index]
    return next((k.value for k in call.keywords if k.arg == name), None)


def _risky(call: ast.Call):
    """The year offset when ``call`` keeps a date's month and day but moves its year."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "replace" and not call.args:
        # d.replace(year=...): the keyword makes it a date's replace, not a string's. Setting
        # the day or month too (day=28) picks a date that exists, unless it copies them.
        for k in call.keywords:
            if k.arg in ("day", "month") and not (isinstance(k.value, ast.Attribute)
                                                  and k.value.attr == k.arg):
                return None
        return _year_offset(next((k.value for k in call.keywords if k.arg == "year"), None))
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    day = _arg(call, 2, "day")
    if name in _CONSTRUCTORS and isinstance(day, ast.Attribute) and day.attr == "day":
        return _year_offset(_arg(call, 0, "year"))
    return None


def _handles_leap_day(scope: ast.AST) -> bool:
    """Does this function already think about February 29?"""
    for node in ast.walk(scope):
        if isinstance(node, ast.Compare):
            parts = [node.left, *node.comparators]
            numbers = {p.value for p in parts if isinstance(p, ast.Constant)}
            if 29 in numbers:
                return True
            if 2 in numbers and any(isinstance(p, ast.Attribute) and p.attr == "month" for p in parts):
                return True
        if isinstance(node, ast.Call):
            f = node.func
            if (f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")) in _LEAP_AWARE:
                return True
    return False


def _catches_value_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any((t.id if isinstance(t, ast.Name) else getattr(t, "attr", "")) in _CATCHES
               for t in types)


def _walk(node: ast.AST, scope: ast.AST, guarded: bool):
    """Every call, with the function it is in and whether a try around it catches
    ValueError."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        scope, guarded = node, False
    if isinstance(node, ast.Call):
        yield node, scope, guarded
    if isinstance(node, ast.Try):
        caught = guarded or any(_catches_value_error(h) for h in node.handlers)
        for child in node.body:
            yield from _walk(child, scope, caught)
        for child in [*node.handlers, *node.orelse, *node.finalbody]:
            yield from _walk(child, scope, guarded)
        return
    for child in ast.iter_child_nodes(node):
        yield from _walk(child, scope, guarded)


@rule("leap-day-date", "high", blocking=True,
      fix="Add a timedelta instead (days=365), or handle February 29 yourself (fall back to "
          "February 28).")
def leap_day_date(tree: ast.Module, path: str):
    for call, scope, guarded in _walk(tree, tree, False):
        offset = _risky(call)
        if offset is None or guarded or (isinstance(offset, int) and offset % 4 == 0):
            continue
        if _handles_leap_day(scope):
            continue
        if isinstance(offset, int):
            n = abs(offset)
            when = (f"{'a year' if n == 1 else f'{n} years'} "
                    f"{'later' if offset > 0 else 'earlier'} there is no February 29")
        else:
            when = f"{ast.unparse(offset)} years later there may be no February 29"
        yield (call, f"`{ast.unparse(call)}` raises ValueError on February 29: {when}",
               "It works on every other day, so every test passes. Windows Azure, 2012: "
               "certificates dated this way failed on leap day and took services down for "
               "34 hours.")
