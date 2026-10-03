"""compare-to-none: `x == None` or `x != None` instead of `x is None`.

TODO(starter): write this rule. About 8 lines of code.

Why it matters: `==` asks the object whether it equals None, and a class can answer
anything (numpy arrays and some ORM columns do). `is None` always means what it says.

Steps:
  1. Uncomment the `@rule(...)` line below.
  2. Loop over every node: `for node in ast.walk(tree):`
  3. Keep the nodes that are `ast.Compare`. A compare has `.ops` (the operators, like
     `ast.Eq()` for `==` and `ast.NotEq()` for `!=`) and `.comparators` (the right-hand
     sides). See one with:
         import ast; print(ast.dump(ast.parse("x == None")))
  4. Walk the two lists together: `for op, right in zip(node.ops, node.comparators):`
     If `op` is an `ast.Eq` or `ast.NotEq` AND `right` is an `ast.Constant` whose `.value`
     is None, yield it:
         yield node, "compare with None using `is` / `is not`, not `==` / `!=`"
  5. Bonus: `None == x` (None on the left, in `node.left`) counts too.
  6. Delete the `@unittest.skip(...)` line in tests/rules/test_compare_none.py and run
         python -m unittest tests.rules.test_compare_none
"""

import ast

from magellan_lite.findings import rule  # noqa: F401


# @rule("compare-to-none", "low", fix="Use `is None` / `is not None`.")
def compare_to_none(tree: ast.Module, path: str):
    return []   # TODO(starter): replace this line (see the steps above)
