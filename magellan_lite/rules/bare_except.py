"""bare-except: an `except:` with no exception type.

TODO(starter): write this rule. About 5 lines of code; you only need Python's `ast` module.

Why it matters: a bare `except:` also catches KeyboardInterrupt and SystemExit, so Ctrl+C
stops working, and any real error inside the `try` disappears without a trace.

Steps:
  1. Uncomment the `@rule(...)` line below.
  2. Replace `return []` with a loop over every node in the tree:
         for node in ast.walk(tree):
  3. Keep the nodes that are `ast.ExceptHandler` AND whose `.type` is None. That is what a
     bare `except:` looks like. To see it for yourself, run this in a Python shell:
         import ast; print(ast.dump(ast.parse("try:\n    x()\nexcept:\n    pass")))
  4. For each one: `yield node, "a bare `except:` also catches Ctrl+C and SystemExit"`
  5. Open tests/rules/test_bare_except.py, delete the `@unittest.skip(...)` line, and run
         python -m unittest tests.rules.test_bare_except
     until every test passes. Then try `magellan-lite rules`: your rule is in the list.
"""

import ast

from magellan_lite.findings import rule  # noqa: F401


@rule("bare-except", "low", fix="Catch what you expect, e.g. `except ValueError:`.")
def bare_except(tree: ast.Module, path: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            yield node, "a bare `except:` also catches Ctrl+C and SystemExit"


