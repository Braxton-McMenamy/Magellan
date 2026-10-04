"""debug-leftover: print(), breakpoint() and pdb.set_trace() left in changed code.

Why it matters: a stray breakpoint() hangs a server waiting for a debugger nobody attached;
a stray print() fills logs or corrupts output another program reads.
"""

import ast

from magellan_lite.findings import rule  # noqa: F401


@rule("debug-leftover", "low", fix="Remove it before committing (use logging for output).")
def debug_leftover(tree: ast.Module, path: str):
    # 1. where print() is fine: inside `if __name__ == "__main__":` blocks
    main_blocks = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"):
            main_blocks.append((node.lineno, node.end_lineno))

    # 2. every call: is it one of the debugging leftovers?
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue                      # not a call: skip to the next node
        func = node.func
        if isinstance(func, ast.Name) and func.id == "breakpoint":
            yield node, "breakpoint() left in"
        elif isinstance(func, ast.Name) and func.id == "print":
            if not any(start <= node.lineno <= end for start, end in main_blocks):
                yield node, "print() left in"
        elif (isinstance(func, ast.Attribute) and func.attr == "set_trace"
              and isinstance(func.value, ast.Name) and func.value.id in ("pdb", "ipdb")):
            yield node, "pdb.set_trace() left in"

