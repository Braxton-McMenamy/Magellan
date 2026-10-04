"""swallowed-exception: a broad handler that throws the error away.

From the full Magellan (``rule_swallowed_exception`` in ``magellan/rules/risk.py``).
``except Exception: pass`` catches everything that can go wrong in the ``try`` -- a typo, a
failing disk, the bug this very change brings in -- and leaves no trace: no log, no failure,
just wrong results later, found by someone else. ``with suppress(Exception):`` does the same.

Reported in code the change touched, when the handler catches ``Exception`` or
``BaseException`` and does nothing (``pass``, ``continue``, ``...``). A handler for one
specific exception is a decision and is left alone; a bare ``except:`` is ``bare-except``.
"""

from __future__ import annotations

import ast

from magellan_lite.findings import rule
from magellan_lite.rules.common import call_name, dotted

_BROAD = {"Exception", "BaseException"}


def _broad(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Tuple):
        return any(_broad(e) for e in node.elts)
    return node is not None and dotted(node).rsplit(".", 1)[-1] in _BROAD


def _does_nothing(body: list[ast.stmt]) -> bool:
    return all(isinstance(s, (ast.Pass, ast.Continue)) or (
        isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in body)


@rule("swallowed-exception", "low",
      fix="Log it, catch only the exception you expect, or let it propagate.")
def swallowed_exception(tree: ast.Module, path: str):
    why = ("Whatever goes wrong inside -- including a bug this change brings in -- disappears "
           "without a trace: no log, no failure, just wrong results later.")
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None \
                and _broad(node.type) and _does_nothing(node.body):
            first = ast.unparse(node.body[0]).splitlines()[0]
            yield (node, f"`except {ast.unparse(node.type)}: {first}` throws the error away",
                   why)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                c = item.context_expr
                if isinstance(c, ast.Call) and call_name(c) == "suppress" \
                        and any(_broad(a) for a in c.args):
                    yield (c, f"`{ast.unparse(c)}` throws every error in this block away",
                           why)
