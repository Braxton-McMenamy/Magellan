"""assert-on-tuple: `assert (x, "message")` -- an assert that can never fail.

Why it matters: the parentheses turn the condition and the message into one tuple, and a
tuple with something in it is always true. The assert passes even when x is False, so the
check you meant to have is not there. (Python 3.8+ warns about it, but only when it runs.)
The intended code is `assert x, "message"` -- no parentheses.
"""

import ast

from magellan_lite.findings import rule  # noqa: F401


@rule("assert-on-tuple", "medium", fix='Drop the parentheses: `assert x, "message"`.')
def assert_on_tuple(tree: ast.Module, path: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Tuple) and node.test.elts:
            yield node, "This assert checks a tuple, which is always true"
