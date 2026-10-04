"""compare-to-none: `x == None` or `x != None` instead of `x is None`.

Why it matters: `==` asks the object whether it equals None, and a class can answer
anything (numpy arrays and some ORM columns do). `is None` always means what it says.
"""

import ast

from magellan_lite.findings import rule  # noqa: F401


@rule("compare-to-none", "low", fix="Use `is None` / `is not None`.")
def compare_to_none(tree: ast.Module, path: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(right, ast.Constant) and right.value is None:
                    yield node, "compare with None using `is` / `is not`, not `==` / `!=`"

