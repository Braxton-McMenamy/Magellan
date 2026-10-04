"""Cross-language linking by ABI symbol.

Frontends do not know about each other. Each one declares, in node metadata:

- ``meta["abi_exports"]``: symbols this definition is reachable by from other
  languages (a C function ``parse``; ``Java_pkg_Cls_m`` for a JNI native; a
  Fortran ``bind(C, name="x")``; a COBOL ``PROGRAM-ID``);
- a call into another language as an edge to ``ext:abi:<symbol>`` whose node
  carries ``meta["abi_import"] = "<symbol>"`` (a Java ``native`` method, Python
  ``ctypes``/``cffi`` access, a COBOL ``CALL "X"``, C++ ``extern "C"`` use).

After all frontends ran, :func:`link` adds an edge from every referrer of an
``ext:abi:`` node to each definition exporting that symbol, with the original
edge's kind and reduced confidence (name-matched, not proven by a linker).
The external node stays, so an unmatched symbol is still an endpoint.
"""
from __future__ import annotations

from collections import defaultdict

from magellan_lite.polyglot.core.model import Edge, Graph

#: a symbol match is a strong hint, not proof (build flags, dlopen, name mangling)
LINK_CONFIDENCE = 0.8


def link(graph: Graph) -> int:
    """Add cross-language edges; returns how many were added."""
    exporters: dict[str, list[str]] = defaultdict(list)
    for node in graph.nodes.values():
        for sym in node.meta.get("abi_exports", ()) or ():
            exporters[sym].append(node.id)
    if not exporters:
        return 0
    added = 0
    for node in list(graph.nodes.values()):
        sym = node.meta.get("abi_import")
        if not sym or sym not in exporters:
            continue
        for e in list(graph.in_edges(node.id)):
            for dst in exporters[sym]:
                if dst == e.src:
                    continue
                meta = dict(e.meta)
                meta["interop"] = sym
                if graph.add_edge(Edge(src=e.src, dst=dst, kind=e.kind, lineno=e.lineno,
                                       path=e.path, confidence=min(e.confidence, LINK_CONFIDENCE),
                                       conditional=e.conditional, dynamic=e.dynamic,
                                       context=e.context, meta=meta)) is not None:
                    added += 1
    if added:
        graph.diagnostics.append(f"interop: {added} cross-language edge(s) linked by symbol")
    return added
