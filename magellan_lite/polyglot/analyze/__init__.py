"""AST analysis passes. Nothing here imports or executes the code under study."""

from typing import Any

__all__ = ["build_graph"]


def __getattr__(name: str) -> Any:  # lazy, so submodules stay independently importable
    if name == "build_graph":
        from magellan_lite.polyglot.analyze.builder import build_graph

        return build_graph
    raise AttributeError(name)
