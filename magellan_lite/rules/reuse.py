"""reused-value: a constant's value handed to a new meaning.

Knight Capital, 2012: the flag that used to switch on Power Peg (unused since 2003) was reused
for new RLP code. One of eight servers kept the old build; orders carrying the reused flag
woke Power Peg up there, and Knight lost over $460 million in 45 minutes. Inside the
repository nothing looked wrong: one constant deleted, another added, and the dispatch now
called the new code.

The signal: a constant's value goes away (the constant is deleted, or given a new value)
while a new constant in the same scope takes that exact value, and a function that read the
old one now reads the new one *and calls different things*. A plain rename leaves the calls
alone, and stays quiet.
"""

from __future__ import annotations

import ast
import re

from magellan_lite.findings import Finding, change_rule


def _value(text: str):
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None


def _as_written(source: str | None, name: str, line: int, fallback: str) -> str:
    """The value as the code spells it (``0x08``, not ``8``)."""
    lines = (source or "").splitlines()
    if 0 < line <= len(lines):
        m = re.match(rf"\s*{re.escape(name)}\s*(?::[^=]+)?=\s*(.+?)\s*(#.*)?$", lines[line - 1])
        if m:
            return m.group(1)
    return fallback


def _calls(graph, fn: str) -> set[str]:
    return {e.dst for e in graph.uses(fn) if e.kind == "calls"}


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


@change_rule("reused-value", "high",
             fix="Give the new meaning a value of its own, and retire the old one: reject it "
                 "(or log it and refuse), so whatever still sends it fails loudly instead of "
                 "running the new code.")
def reused_value(ctx):
    from magellan_lite.graph import build_graph
    old_graph = None
    for gone, old in ctx.before_defs.items():
        if old.kind != "constant" or _value(old.value) is None:
            continue
        now = ctx.after_defs.get(gone)
        if now is not None and now.value == old.value:
            continue                                    # still there, same value
        scope = gone.rsplit(".", 1)[0]
        heirs = [d for n, d in ctx.after_defs.items()
                 if d.kind == "constant" and n != gone and n.rsplit(".", 1)[0] == scope
                 and _value(d.value) == _value(old.value) and type(_value(d.value)) is type(_value(old.value))
                 and (n not in ctx.before_defs or ctx.before_defs[n].value != d.value)]
        if not heirs:
            continue
        if old_graph is None:
            old_graph = build_graph(ctx.before, ctx.before_defs)
        old_readers = {e.src for e in old_graph.callers(gone) if e.kind == "reads"}
        written = _as_written(ctx.before.files.get(old.path), _short(gone), old.line, old.value)
        for heir in heirs:
            for e in ctx.graph.callers(heir.name):
                if e.kind != "reads" or e.src not in old_readers:
                    continue
                before, after = _calls(old_graph, e.src), _calls(ctx.graph, e.src)
                if before == after:
                    continue                            # a rename: same value, same behaviour
                lost = _and(sorted(_short(c) + "()" for c in before - after)) or "nothing"
                new = _and(sorted(_short(c) + "()" for c in after - before)) or "nothing"
                yield Finding(
                    "reused-value", "high",
                    f"{_short(heir.name)} takes over the value {written} that "
                    f"{_short(gone)} had: {_short(e.src)}() used to call {lost} for it, and "
                    f"now calls {new}",
                    e.path, e.line,
                    detail=f"Anything that still sends, stores or runs with the old meaning "
                           f"of {written} -- another service, saved data, a server still on "
                           f"the previous build -- now gets the new behaviour. Knight "
                           f"Capital, 2012: a reused flag woke dead code on one server and "
                           f"lost $460 million in 45 minutes.")
