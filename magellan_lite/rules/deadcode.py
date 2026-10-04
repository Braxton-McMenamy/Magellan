"""Code that can never run, in what the change touched.

From the full Magellan (``rule_unreachable_statements`` and ``rule_constant_condition`` in
``magellan/python/rules/reachability.py``). New code that never runs is a change that does
not do what it looks like it does: the logging added after a ``return``, the cleanup after a
``raise``, a branch switched off with ``if False:`` while debugging and committed that way.

* ``unreachable-code``: statements after a ``return``, ``raise``, ``break`` or ``continue`` in
  the same block. (C's ``goto fail`` kind is ``unreachable-statement``, a C rule.) Left
  alone: ``return`` followed by ``yield`` (the idiom that makes an empty generator), and
  leftovers that do nothing (``pass``, ``...``).
* ``constant-condition``: an ``if`` or ``while`` whose condition is a literal, so one side
  never runs -- ``if False:``, ``if 0:``, ``while False:``, or the ``else`` of ``if True:``.
  ``while True:`` is a loop on purpose, and is never reported.
"""

from __future__ import annotations

import ast

from magellan_lite.findings import rule

_TERMINATORS = {ast.Return: "return", ast.Raise: "raise", ast.Break: "break",
                ast.Continue: "continue"}


def _inert(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Pass) or (isinstance(stmt, ast.Expr)
                                          and isinstance(stmt.value, ast.Constant))


@rule("unreachable-code", "medium",
      fix="Move the statements above the line that leaves the block, or delete them.")
def unreachable_code(tree: ast.Module, path: str):
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block[:-1]):
                kind = _TERMINATORS.get(type(stmt))
                if kind is None:
                    continue
                rest = block[i + 1:]
                if all(_inert(s) for s in rest):
                    break
                if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for s in rest
                       for n in ast.walk(s)):
                    break                       # `return` then `yield`: an empty generator
                yield (rest[0],
                       f"line {rest[0].lineno} never runs: the {kind} on line {stmt.lineno} "
                       f"leaves the block first",
                       f"Everything after a {kind} in the same block is skipped every time"
                       + (f" ({len(rest)} statements here)" if len(rest) > 1 else "")
                       + ". If this change added it, the change does not do what it looks "
                         "like it does.")
                break


def _literal(test: ast.AST):
    """``(True, value)`` for a literal condition (numbers, True/False/None), else ``(False,
    None)``. Strings and ``...`` are left alone: nobody writes them as a switch."""
    if isinstance(test, ast.Constant) and not isinstance(test.value, (str, bytes)) \
            and test.value is not Ellipsis:
        return True, test.value
    return False, None


@rule("constant-condition", "low",
      fix="Put back the condition that was meant, or remove the branch that can never run.")
def constant_condition(tree: ast.Module, path: str):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.While)):
            continue
        literal, value = _literal(node.test)
        if not literal:
            continue
        written = ast.unparse(node.test)
        keyword = "if" if isinstance(node, ast.If) else "while"
        if not value:
            yield (node, f"the body of `{keyword} {written}:` never runs",
                   "The condition is a literal that is always false, so the code under it is "
                   "switched off -- often a debugging change that was committed.")
        elif isinstance(node, ast.If) and node.orelse:
            yield (node.orelse[0], f"the else of `if {written}:` never runs",
                   "The condition is a literal that is always true, so the other branch is "
                   "dead code.")
