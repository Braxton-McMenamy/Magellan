"""The other languages: C, C++, Java, Fortran, COBOL and TypeScript/JavaScript.

Vendored from the full Magellan (commit 66387d3 and its work in progress at the time),
with its imports pointed here. Magellan Lite uses it through ``magellan_lite/languages.py``,
which runs each language's frontend on a snapshot and turns what it finds into Lite's
definitions, call edges and findings. Fix bugs in the full Magellan first, then copy them
here: this copy is not where those parsers are developed.

Standard library only, like the rest of Lite. C++ also needs libclang (``pip install
libclang``), and TypeScript/JavaScript Node.js with the ``typescript`` package; without them
those two languages are skipped, and so are they in the browser.
"""

__version__ = "0.1.0"

from magellan_lite.polyglot.core.model import Edge, EdgeKind, Graph, Node, NodeKind

__all__ = ["Edge", "EdgeKind", "Graph", "Node", "NodeKind", "__version__"]
