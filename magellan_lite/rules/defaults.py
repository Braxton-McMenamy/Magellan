"""Mutable default arguments: the example rule. Copy its shape for a new one."""

from __future__ import annotations

import ast

from magellan_lite.findings import rule

_MUTABLE_CALLS = {"list", "dict", "set", "bytearray", "defaultdict", "deque", "OrderedDict"}


def _is_mutable(node: ast.expr) -> bool:
    if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else \
            node.func.attr if isinstance(node.func, ast.Attribute) else ""
        return name in _MUTABLE_CALLS
    return False


@rule("mutable-default-argument", "medium",
      fix="Default to None and build the container inside the function.")
def mutable_default_argument(tree: ast.Module, path: str):
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        defaults = fn.args.defaults + [d for d in fn.args.kw_defaults if d is not None]
        for d in defaults:
            if _is_mutable(d):
                name = getattr(fn, "name", "lambda")
                yield (d, f"{name}() has a mutable default argument `{ast.unparse(d)}`",
                       "It is created once, when the function is defined, and every call "
                       "that leaves the argument out shares it: state leaks between calls.")
