"""assert-on-tuple: `assert (x, "message")` -- an assert that can never fail.

TODO(starter): write this rule. About 5 lines of code.

Why it matters: the parentheses turn the condition and the message into one tuple, and a
tuple with something in it is always true. The assert passes even when x is False, so the
check you meant to have is not there. (Python 3.8+ warns about it, but only when it runs.)
The intended code is `assert x, "message"` -- no parentheses.

Steps:
  1. Uncomment the `@rule(...)` line below.
  2. Loop over every node: `for node in ast.walk(tree):`
  3. Keep the nodes that are `ast.Assert` whose `.test` is an `ast.Tuple` with at least one
     element (`len(node.test.elts) > 0`). See it with:
         import ast; print(ast.dump(ast.parse('assert (x, "msg")')))
  4. For each one: `yield node, "this assert checks a tuple, which is always true"`
  5. Delete the `@unittest.skip(...)` line in tests/rules/test_assert_tuple.py and run
         python -m unittest tests.rules.test_assert_tuple
"""

import ast

from magellan_lite.findings import rule  # noqa: F401


@rule("assert-on-tuple", "medium", fix='Drop the parentheses: `assert x, "message"`.')
def assert_on_tuple(tree: ast.Module, path: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Tuple) and node.test.elts:
            yield node, "This assert checks a tuple, which is always true"
