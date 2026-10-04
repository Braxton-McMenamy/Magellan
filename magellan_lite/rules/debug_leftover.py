"""debug-leftover: print(), breakpoint() and pdb.set_trace() left in changed code.

Why it matters: a stray breakpoint() hangs a server waiting for a debugger nobody attached;
a stray print() fills logs or corrupts output another program reads.

But print() is also how a command-line tool talks to its user, so it is left alone where it
is the program's output: in a file that is a command-line program (named cli.py or
__main__.py, or with an ``if __name__ == "__main__":`` block, a ``main()`` function or an
argparse command line), inside a function whose name says it prints (``main``,
``print_report``, ``show_usage``), and when it says where it writes
(``print(..., file=sys.stderr)``).
"""

import ast
import re

from magellan_lite.findings import rule  # noqa: F401

#: a file with one of these names is a program's entry point
_PROGRAM_FILES = {"cli.py", "__main__.py"}
#: modules that read a command line: a file that imports one is a command-line program
_COMMAND_LINE = {"argparse", "optparse", "getopt", "click", "typer"}
#: words in a function's name that say printing is its job
_OUTPUT_WORDS = {"main", "cli", "print", "pprint", "show", "display", "output", "echo",
                 "report", "dump", "usage", "help", "banner"}


def _is_main_check(test: ast.expr) -> bool:
    """``__name__ == "__main__"``, either way round."""
    if not (isinstance(test, ast.Compare) and len(test.comparators) == 1):
        return False
    sides = [test.left, test.comparators[0]]
    return (any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
            and any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides))


def _is_program(tree: ast.Module, path: str) -> bool:
    """Is this file a command-line program, whose print() calls are its output?"""
    if path.rsplit("/", 1)[-1] in _PROGRAM_FILES:
        return True
    for node in tree.body:
        if isinstance(node, ast.If) and _is_main_check(node.test):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main":
            return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] in _COMMAND_LINE for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if (node.module or "").split(".")[0] in _COMMAND_LINE:
                return True
    return False


def _prints_by_name(name: str) -> bool:
    """``print_report``, ``showUsage``, ``main``: a name that says it prints."""
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", name)
    return any(w.lower() in _OUTPUT_WORDS for w in words)


@rule("debug-leftover", "low", fix="Remove it before committing (use logging for output).")
def debug_leftover(tree: ast.Module, path: str):
    # 1. a command-line program: its print() calls are what it is for
    program = _is_program(tree, path)

    # 2. where else print() is fine: inside functions whose name says they print, and inside
    #    `if __name__ == "__main__":` blocks wherever they are
    output_spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _prints_by_name(node.name):
            output_spans.append((node.lineno, node.end_lineno))
        elif isinstance(node, ast.If) and _is_main_check(node.test):
            output_spans.append((node.lineno, node.end_lineno))

    # 3. every call: is it one of the debugging leftovers?
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue                      # not a call: skip to the next node
        func = node.func
        if isinstance(func, ast.Name) and func.id == "breakpoint":
            yield node, "breakpoint() left in"
        elif isinstance(func, ast.Name) and func.id == "print":
            if program or any(k.arg == "file" for k in node.keywords):
                continue                  # the program's output, or written somewhere on purpose
            if not any(start <= node.lineno <= end for start, end in output_spans):
                yield node, "print() left in"
        elif (isinstance(func, ast.Attribute) and func.attr == "set_trace"
              and isinstance(func.value, ast.Name) and func.value.id in ("pdb", "ipdb")):
            yield node, "pdb.set_trace() left in"
