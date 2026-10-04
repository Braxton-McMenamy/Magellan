"""bare-except: an `except:` with no exception type.

Why it matters: a bare `except:` also catches KeyboardInterrupt and SystemExit, so Ctrl+C
stops working, and any real error inside the `try` disappears without a trace.
"""

import ast

from magellan_lite.findings import rule  # noqa: F401


@rule("bare-except", "low", fix="Catch what you expect, e.g. `except ValueError:`.")
def bare_except(tree: ast.Module, path: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            yield node, "a bare `except:` also catches Ctrl+C and SystemExit"


