"""The call graph: which definition calls, constructs or reads which.

Built from syntax trees alone; nothing is imported or run. A call is resolved by name:

- ``f()``: a function in the same module, or one imported (``from a.b import f``);
- ``mod.f()``: after ``import mod`` (or ``import a.b as mod``);
- ``self.m()`` / ``cls.m()``: a method of the class the call is in;
- ``Ledger.post(...)``, ``Ledger(...)``: a class of this module and its methods.

A method call on anything else (``order.update()``) cannot be resolved without types. When
exactly one method in the project has that name, it is linked as a *guess*, which the blast
radius weighs less; otherwise it is left out. Edges to definitions the change deleted are kept
(``dangling``), so code still calling them can be found.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass

from magellan_lite.defs import Definition
from magellan_lite.source import Snapshot, module_name

#: method names too common to guess at: `d.get()` is almost never the project's `get`
_NO_GUESS = frozenset({
    "get", "set", "add", "append", "extend", "insert", "pop", "remove", "clear", "copy",
    "update", "items", "keys", "values", "join", "split", "strip", "format", "replace",
    "read", "write", "close", "open", "run", "send", "start", "stop", "sort",
    "count", "index", "find", "encode", "decode", "lower", "upper", "startswith",
    "endswith", "match", "search", "sub", "load", "dump", "loads", "dumps",
})


@dataclass(frozen=True)
class Edge:
    src: str                       # the definition that calls or reads
    dst: str                       # what it calls or reads
    kind: str                      # "calls" | "reads"
    path: str
    line: int
    guess: bool = False            # matched by method name alone
    bound: bool = False            # obj.method(...): `self` is passed for you
    positional: int = 0            # positional arguments at the call site
    keywords: tuple[str, ...] = ()
    unpacked: bool = False         # *args or **kwargs at the call site: the count is unknown


@dataclass(frozen=True)
class ImportSite:
    path: str
    line: int
    target: str                    # the dotted name an import statement binds


class CallGraph:
    def __init__(self, edges: list[Edge], imports: list[ImportSite]) -> None:
        self.edges = edges
        self.imports = imports
        self._in: dict[str, list[Edge]] = defaultdict(list)
        self._out: dict[str, list[Edge]] = defaultdict(list)
        for e in edges:
            self._in[e.dst].append(e)
            self._out[e.src].append(e)

    def callers(self, name: str) -> list[Edge]:
        """Edges into ``name``: who calls or reads it."""
        return self._in.get(name, [])

    def uses(self, name: str) -> list[Edge]:
        """Edges out of ``name``: what it calls or reads."""
        return self._out.get(name, [])


def build_graph(snapshot: Snapshot, defs: dict[str, Definition],
                old_defs: dict[str, Definition] | None = None) -> CallGraph:
    """The graph of ``snapshot``. ``old_defs`` (the previous version's map) keeps edges to
    definitions that no longer exist, so the code still using them can be reported."""
    # what each name is, in either version: a read of a deleted constant still counts
    kinds = {n: d.kind for n, d in (old_defs or {}).items()}
    kinds.update({n: d.kind for n, d in defs.items()})
    known = set(kinds)
    methods: dict[str, list[str]] = defaultdict(list)
    for d in defs.values():
        if d.kind == "method":
            methods[d.short].append(d.name)

    edges: list[Edge] = []
    import_sites: list[ImportSite] = []
    for path in sorted(snapshot.files):
        tree = snapshot.tree(path)
        if tree is None:
            continue
        mod = module_name(path)
        package = mod if path.endswith("__init__.py") else mod.rpartition(".")[0]
        table, sites = _imports(tree, package, path)
        import_sites += sites
        for owner, fn, cls in _callables(tree, mod):
            edges += _Resolver(owner, fn, cls, mod, path, table, kinds, methods).edges()
    return CallGraph(edges, import_sites)


def _imports(tree: ast.Module, package: str, path: str) -> tuple[dict[str, str], list[ImportSite]]:
    """``{bound name: dotted target}`` for every import in the module, and where each is."""
    table: dict[str, str] = {}
    sites: list[ImportSite] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    table[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    table[top] = top
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:                              # relative: from . / .. / .mod import
                parts = package.split(".") if package else []
                if node.level > 1:
                    parts = parts[:len(parts) - (node.level - 1)]
                base = ".".join(parts + ([node.module] if node.module else []))
            for a in node.names:
                if a.name == "*":
                    continue
                target = f"{base}.{a.name}" if base else a.name
                table[a.asname or a.name] = target
                sites.append(ImportSite(path, node.lineno, target))
    return table, sites


def _callables(tree: ast.Module, mod: str):
    """``(definition name, function node, enclosing class name or None)``, named as the map
    names them (defs.py)."""
    def visit(body, prefix, cls):
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield f"{prefix}.{stmt.name}", stmt, cls
            elif isinstance(stmt, ast.ClassDef):
                qual = f"{prefix}.{stmt.name}"
                yield from visit(stmt.body, qual, qual)
    yield from visit(tree.body, mod, None)


class _Resolver:
    """The edges out of one function."""

    def __init__(self, owner, fn, cls, mod, path, imports, kinds, methods) -> None:
        self.owner, self.fn, self.cls, self.mod, self.path = owner, fn, cls, mod, path
        self.imports, self.kinds, self.methods = imports, kinds, methods
        self.known = kinds.keys()
        self.locals = self._local_names(fn)

    @staticmethod
    def _local_names(fn) -> set[str]:
        """Parameters and names assigned in the function: a call to one of them is a call to
        whatever was passed in, not to a module-level definition of the same name."""
        a = fn.args
        names = {arg.arg for arg in a.posonlyargs + a.args + a.kwonlyargs}
        names |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
        return names

    def _name(self, ident: str) -> str | None:
        """The definition a bare name refers to, if it is one of ours."""
        if ident in self.locals:
            return None
        if ident in self.imports and self.imports[ident] in self.known:
            return self.imports[ident]
        here = f"{self.mod}.{ident}"
        return here if here in self.known else None

    def _attribute(self, node: ast.Attribute) -> tuple[str | None, bool, bool]:
        """``(target, bound, guess)`` for ``X.attr``."""
        attr = node.attr
        if isinstance(node.value, ast.Name):
            base = node.value.id
            if base in ("self", "cls") and self.cls:
                target = f"{self.cls}.{attr}"
                if target in self.known:
                    return target, True, False
            elif base not in self.locals:
                if base in self.imports:
                    target = f"{self.imports[base]}.{attr}"
                    if target in self.known:
                        return target, False, False
                target = f"{self.mod}.{base}.{attr}"     # a class of this module
                if target in self.known:
                    return target, False, False
        candidates = self.methods.get(attr, [])
        if len(candidates) == 1 and attr not in _NO_GUESS:
            return candidates[0], True, True
        return None, False, False

    def edges(self) -> list[Edge]:
        out: list[Edge] = []
        for node in ast.walk(self.fn):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    target, bound, guess = self._name(node.func.id), False, False
                elif isinstance(node.func, ast.Attribute):
                    target, bound, guess = self._attribute(node.func)
                else:
                    continue
                if target and target != self.owner:
                    out.append(Edge(
                        self.owner, target, "calls", self.path, node.lineno, guess, bound,
                        positional=sum(not isinstance(a, ast.Starred) for a in node.args),
                        keywords=tuple(k.arg for k in node.keywords if k.arg),
                        unpacked=any(isinstance(a, ast.Starred) for a in node.args)
                        or any(k.arg is None for k in node.keywords)))
            elif isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
                if isinstance(node, ast.Name):
                    target = self._name(node.id)
                else:
                    target, _bound, guess = self._attribute(node)
                    target = None if guess else target       # never guess at a read
                if target and target != self.owner and self.kinds.get(target) == "constant":
                    out.append(Edge(self.owner, target, "reads", self.path, node.lineno))
        return out
