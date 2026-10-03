"""debug-leftover: print(), breakpoint() and pdb.set_trace() left in changed code.

TODO(starter): write this rule. About 15 lines; do the other starter rules first.

Why it matters: a stray breakpoint() hangs a server waiting for a debugger nobody attached;
a stray print() fills logs or corrupts output another program reads.

Steps:
  1. Uncomment the `@rule(...)` line below.
  2. Loop over every node: `for node in ast.walk(tree):` and keep the `ast.Call` nodes.
  3. A call's `.func` says what is called:
       - `breakpoint()` and `print()`: `node.func` is an `ast.Name`; its `.id` is the name.
       - `pdb.set_trace()`: `node.func` is an `ast.Attribute` with `.attr == "set_trace"`,
         and `node.func.value` is an `ast.Name` with `.id == "pdb"` (or "ipdb").
     Yield `node, "breakpoint() left in"` (or print / pdb.set_trace).
  4. Don't flag print() in a program's main block, where printing is the point:
         if __name__ == "__main__":
             print(...)
     Hint: first collect the line ranges of those `if` blocks (an `ast.If` whose test is
     the comparison `__name__ == "__main__"`), then skip any print whose `.lineno` falls
     inside one (`if_node.lineno <= line <= if_node.end_lineno`).
  5. Delete the `@unittest.skip(...)` line in tests/rules/test_debug_leftover.py and run
         python -m unittest tests.rules.test_debug_leftover
"""

import ast

from magellan_lite.findings import rule  # noqa: F401


# @rule("debug-leftover", "low", fix="Remove it before committing (use logging for output).")
def debug_leftover(tree: ast.Module, path: str):
    return []   # TODO(starter): replace this line (see the steps above)
