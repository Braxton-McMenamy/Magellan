"""import-cycle: the change makes modules import each other when they load.

From the full Magellan (``rule_import_cycle`` in ``magellan/rules/risk.py``), narrowed to the
case a commit can be blamed for: a cycle that did not exist before. When ``a`` imports ``b``
while ``b`` imports ``a``, whichever is imported first sees the other half-built. ``from a
import name`` then fails with ``ImportError: cannot import name ... (most likely due to a
circular import)`` -- or works, depending on which module some entry point happens to import
first, which is how it passes every test and breaks in production.

Only imports that run when the module loads count: top-level ones, in class bodies, under
``if``/``try``/``with`` -- not inside functions, and not under ``if TYPE_CHECKING:`` or ``if
__name__ == "__main__":``. A cycle is reported when it is new, it runs through a file the
change edited, and at least one of its imports takes a name out of a half-built module
(``from a import name``); cycles of plain ``import a`` (used later, as ``a.name``) are left
alone, since those load fine.
"""

from __future__ import annotations

import ast

from magellan_lite.findings import Finding, change_rule
from magellan_lite.rules.common import changed_files
from magellan_lite.source import module_name


def _skipped_if(node: ast.If) -> bool:
    test = ast.unparse(node.test)
    return "TYPE_CHECKING" in test or "__name__" in test


def _load_time(body):
    """Import statements that run when the module is imported."""
    for stmt in body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            yield stmt
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        elif isinstance(stmt, ast.If):
            if not _skipped_if(stmt):
                yield from _load_time(stmt.body)
                yield from _load_time(stmt.orelse)
        elif isinstance(stmt, ast.ClassDef):
            yield from _load_time(stmt.body)
        else:
            for field in ("body", "orelse", "finalbody"):
                yield from _load_time(getattr(stmt, field, []) or [])
            for h in getattr(stmt, "handlers", []) or []:
                yield from _load_time(h.body)


def import_edges(snapshot) -> dict[tuple[str, str], tuple[str, str, int]]:
    """``{(module, imported module): (how, path, line)}`` for load-time imports between the
    project's own modules. ``how`` is ``from`` when a name is taken out of the module, else
    ``import``."""
    modules = {module_name(p): p for p in snapshot.files if p.endswith(".py")}
    out: dict[tuple[str, str], tuple[str, str, int]] = {}
    for mod, path in sorted(modules.items()):
        tree = snapshot.tree(path)
        if tree is None:
            continue
        package = mod if path.endswith("__init__.py") else mod.rpartition(".")[0]
        for node in _load_time(tree.body):
            if isinstance(node, ast.Import):
                for a in node.names:
                    parts = a.name.split(".")
                    target = next((".".join(parts[:i]) for i in range(len(parts), 0, -1)
                                   if ".".join(parts[:i]) in modules), None)
                    if target and target != mod:
                        out.setdefault((mod, target), ("import", path, node.lineno))
                continue
            base = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                if node.level > 1:
                    parts = parts[:len(parts) - (node.level - 1)]
                base = ".".join(parts + ([node.module] if node.module else []))
            for a in node.names:
                sub = f"{base}.{a.name}" if base else a.name
                if sub in modules:
                    if sub != mod:
                        out.setdefault((mod, sub), ("import", path, node.lineno))
                elif base in modules and base != mod:
                    key = (mod, base)
                    if out.get(key, ("import",))[0] != "from":
                        out[key] = ("from", path, node.lineno)
    return out


def _sccs(edges) -> list[set[str]]:
    adj: dict[str, set[str]] = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[set[str]] = []
    counter = 0
    for root in sorted(adj):
        if root in index:
            continue
        work = [(root, iter(sorted(adj.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(adj.get(w, ())))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = set()
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.add(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(comp)
    return out


def _ring(start: str, comp: set[str], edges) -> list[str]:
    """A shortest way round the cycle from ``start`` back to it."""
    prev: dict[str, str] = {}
    todo = [start]
    while todo:
        nxt = []
        for v in todo:
            for (a, b) in sorted(edges):
                if a != v or b not in comp:
                    continue
                if b == start:
                    path = [v]
                    while path[-1] != start:
                        path.append(prev[path[-1]])
                    return list(reversed(path))
                if b not in prev:
                    prev[b] = v
                    nxt.append(b)
        todo = nxt
    return [start]


@change_rule("import-cycle", "medium",
             fix="Move one of the imports into the function that uses it, or move what both "
                 "modules need into a third module that imports neither.")
def import_cycle(ctx):
    files = changed_files(ctx)
    if not files:
        return
    after = import_edges(ctx.after)
    cycles = [c for c in _sccs(after) if any(module_name(f) in c for f in files)]
    if not cycles:
        return
    before = import_edges(ctx.before)
    old = {}
    for i, comp in enumerate(_sccs(before)):
        for m in comp:
            old[m] = i
    for comp in cycles:
        if len({old.get(m, -1 - k) for k, m in enumerate(sorted(comp))}) == 1:
            continue                                # the same cycle was there before
        inside = {k: v for k, v in after.items() if k[0] in comp and k[1] in comp}
        if not any(how == "from" for how, _p, _l in inside.values()):
            continue                                # plain `import a` both ways loads fine
        fresh = sorted((k for k in inside if k not in before and inside[k][1] in files),
                       key=lambda k: (inside[k][1], inside[k][2]))
        if not fresh:
            continue
        a, b = fresh[0]
        _how, path, line = inside[(a, b)]
        ring = _ring(a, comp, inside)
        shown = " -> ".join(ring + [ring[0]])
        names = sorted(comp)
        yield Finding(
            "import-cycle", "medium",
            f"this import closes a circle: {shown}",
            path, line,
            detail=(f"{_and(names)} now import each other while they load. Whichever is "
                    f"imported first sees the other half-built, so `from ... import name` "
                    f"fails with ImportError (\"most likely due to a circular import\") -- or "
                    f"works, depending on which module an entry point happens to import "
                    f"first. Before this change there was no such circle."))


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
