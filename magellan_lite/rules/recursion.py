"""recursive-cycle: recursion the change creates, with nothing visible to stop it.

From the full Magellan (``rule_recursive_cycle`` in ``magellan/rules/risk.py``). Mutual
recursion is the dangerous kind: ``a -> b -> c -> a`` reads as three ordinary functions, and
nothing at any one of them says "this is a loop". Adding one call to the wrong one of them is
how an edit that looked local turns into a RecursionError (or a hang) in production.

Two findings, both only for code this change added or edited:

* high (blocks the commit): a function that now calls itself on *every* path -- before any
  ``return`` or ``raise`` could leave, outside every ``if`` and loop. It can never return:
  ``return self.name`` inside the ``name`` property, ``self.x = v`` inside ``__setattr__``,
  ``def get(self, k): return self.get(k)`` meant for ``self._data.get``.
* medium: functions that existed before and now call each other in a circle, when none of
  them takes a depth, limit, budget or visited parameter. Recursion designed as recursion (a
  parser written today) is left alone: only a cycle closed between existing functions is
  reported, and only when it is new. So is a walk over a structure, where every call hands on
  a smaller piece (``node.children``, ``items[1:]``, a loop's element, ``n - 1``): it ends
  when the data does.
"""

from __future__ import annotations

import ast

from magellan_lite.findings import Finding, change_rule
from magellan_lite.rules.common import edited, functions, old_graph
from magellan_lite.source import module_name

_GUARDS = ("depth", "level", "seen", "visited", "budget", "limit", "remaining", "max_",
           "memo", "cache", "ttl", "retries", "attempt")


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1]


# -- a function that always calls itself ----------------------------------------------------
def _decorators(fn) -> set[str]:
    out = set()
    for d in fn.decorator_list:
        d = d.func if isinstance(d, ast.Call) else d
        out.add(d.attr if isinstance(d, ast.Attribute) else getattr(d, "id", ""))
    return out


def _is_self(node: ast.AST, fn, cls_name: str | None, method: bool) -> bool:
    """Does this node refer to the function it is in?"""
    name = fn.name
    if method:
        decorators = _decorators(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == name and isinstance(node.func.value, ast.Name) \
                and node.func.value.id in ("self", "cls", cls_name):
            return True                                         # self.m(...) in m
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self":
            if "property" in decorators or "cached_property" in decorators:
                return node.attr == name and isinstance(node.ctx, ast.Load)
            if "setter" in decorators:
                return node.attr == name and isinstance(node.ctx, ast.Store)
            if name == "__setattr__":
                return isinstance(node.ctx, ast.Store)          # self.x = v in __setattr__
        return False
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
        and node.func.id == name


def _always(expr: ast.AST):
    """The parts of an expression that are evaluated every time it is."""
    yield expr
    if isinstance(expr, ast.IfExp):
        yield from _always(expr.test)
        return
    if isinstance(expr, ast.BoolOp):
        yield from _always(expr.values[0])
        return
    if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        yield from _always(expr.generators[0].iter)
        return
    if isinstance(expr, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return
    for child in ast.iter_child_nodes(expr):
        yield from _always(child)


def _may_leave(stmt: ast.stmt) -> bool:
    """Can control leave the function (or skip ahead) somewhere in this statement?"""
    todo = [stmt]
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.Return, ast.Raise, ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                 ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(node))
    return False


def _unconditional(body: list[ast.stmt], is_self) -> ast.AST | None:
    """The first reference to itself that every run of ``body`` reaches, if any."""
    for stmt in body:
        if isinstance(stmt, (ast.Expr, ast.Return, ast.Assign, ast.AugAssign, ast.AnnAssign)):
            parts = [stmt.value] if stmt.value is not None else []
            if isinstance(stmt, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                parts += stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for part in parts:
                hit = next((n for n in _always(part) if is_self(n)), None)
                if hit is not None:
                    return hit
        elif isinstance(stmt, (ast.If, ast.While)):
            hit = next((n for n in _always(stmt.test) if is_self(n)), None)
            if hit is not None:
                return hit
        elif isinstance(stmt, ast.For):
            hit = next((n for n in _always(stmt.iter) if is_self(n)), None)
            if hit is not None:
                return hit
        elif isinstance(stmt, ast.With):
            for item in stmt.items:
                hit = next((n for n in _always(item.context_expr) if is_self(n)), None)
                if hit is not None:
                    return hit
            hit = _unconditional(stmt.body, is_self)
            if hit is not None:
                return hit
        elif isinstance(stmt, (ast.Try, ast.AsyncWith, ast.AsyncFor)) \
                or isinstance(stmt, getattr(ast, "TryStar", ())):
            return None                       # a handler may catch the RecursionError
        if _may_leave(stmt):
            return None
    return None


def _rebinds(tree: ast.Module, fn, cls: ast.ClassDef | None) -> bool:
    """Is the name rebound elsewhere (``self.run = ...``, ``f = other``)? Then the call may
    not land on this function at all."""
    scope = cls if cls is not None else tree
    for node in ast.walk(scope):
        if cls is not None and isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) \
                and node.attr == fn.name and isinstance(node.value, ast.Name) \
                and node.value.id == "self" and "setter" not in _decorators(fn):
            return True
        if cls is None and isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) \
                and node.id == fn.name:
            return True
    return False


def _self_recursion(fn, cls_node, tree) -> ast.AST | None:
    """Where ``fn`` calls itself on every path, or None."""
    if isinstance(fn, ast.AsyncFunctionDef) \
            or any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(fn)):
        return None                           # calling it does not run the body yet
    method = cls_node is not None
    args = fn.args
    local = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    if not method and fn.name in local:
        return None
    cls_name = cls_node.name if cls_node is not None else None
    hit = _unconditional(fn.body, lambda n: _is_self(n, fn, cls_name, method))
    if hit is not None and _rebinds(tree, fn, cls_node):
        return None
    return hit


# -- functions that call each other in a circle -----------------------------------------------
def _sccs(graph, defs) -> list[list[str]]:
    """Strongly connected call components of two or more Python functions."""
    adj: dict[str, set[str]] = {}
    for e in graph.edges:
        if e.kind != "calls" or e.guess:
            continue
        a, b = defs.get(e.src), defs.get(e.dst)
        if a and b and not a.lang and not b.lang and a.kind in ("function", "method") \
                and b.kind in ("function", "method"):
            adj.setdefault(e.src, set()).add(e.dst)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
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
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
    return out


def _guarded(members: list[str], fns: dict) -> str:
    for name in members:
        if name not in fns:
            continue
        _path, fn, _cls = fns[name]
        a = fn.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
            if any(g in arg.arg.lower() for g in _GUARDS):
                return arg.arg
        if _decorators(fn) & {"lru_cache", "cache"}:
            return "a cache"
    return ""


def _smaller(arg: ast.AST, loop_names: set[str]) -> bool:
    """Is this argument a smaller piece of the input: ``node.left``, ``items[1:]``, a loop's
    element, ``n - 1``? Recursion that hands on smaller pieces walks a structure and ends."""
    if isinstance(arg, ast.Starred):
        arg = arg.value
    if isinstance(arg, (ast.Attribute, ast.Subscript)):
        return True
    if isinstance(arg, ast.Name):
        return arg.id in loop_names
    return isinstance(arg, ast.BinOp) and isinstance(arg.op, (ast.Sub, ast.FloorDiv,
                                                              ast.RShift)) \
        and isinstance(arg.right, ast.Constant)


def _walks_a_structure(ring: list[str], graph, fns) -> bool:
    """Does every call around the cycle hand on a smaller piece of what it was given?"""
    for here, there in zip(ring, ring[1:] + ring[:1]):
        if here not in fns:
            return False
        fn = fns[here][1]
        loop_names = {n.id for node in ast.walk(fn)
                      if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension))
                      for n in ast.walk(node.target) if isinstance(n, ast.Name)}
        lines = {e.line for e in graph.uses(here) if e.dst == there and e.kind == "calls"}
        calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and c.lineno in lines
                 and _short(there) in ast.unparse(c.func)]
        if not calls or not all(any(_smaller(a, loop_names)
                                    for a in [*c.args, *(k.value for k in c.keywords)])
                                for c in calls):
            return False
    return True


def _order(members: list[str], graph) -> list[str]:
    """The cycle in call order, starting from its first member."""
    ring, here = [members[0]], members[0]
    inside = set(members)
    while True:
        nxt = sorted(e.dst for e in graph.uses(here)
                     if e.kind == "calls" and not e.guess and e.dst in inside)
        nxt = [n for n in nxt if n not in ring] or [n for n in nxt if n == ring[0]]
        if not nxt or nxt[0] == ring[0]:
            return ring
        ring.append(nxt[0])
        here = nxt[0]


@change_rule("recursive-cycle", "high", blocking=True,
             fix="Give the recursion an end every path reaches: a base case before the call, "
                 "or a depth/visited argument passed through every call.")
def recursive_cycle(ctx):
    touched = edited(ctx)
    if not touched:
        return
    fns = functions(ctx.after, {ctx.after_defs[n].path for n in touched if n in ctx.after_defs
                                and not ctx.after_defs[n].lang})
    old_fns = None
    trees = {}

    # 1. a function that calls itself on every path
    for name in sorted(touched & set(fns)):
        path, fn, cls = fns[name]
        tree = trees.setdefault(path, ctx.after.tree(path))
        cls_node = _class_node(tree, cls, path)
        hit = _self_recursion(fn, cls_node, tree)
        if hit is None:
            continue
        if old_fns is None:
            old_fns = functions(ctx.before)
        if name in old_fns:
            opath, ofn, ocls = old_fns[name]
            otree = ctx.before.tree(opath)
            if _self_recursion(ofn, _class_node(otree, ocls, opath), otree) is not None:
                continue                                   # it already did: not this change
        what = ("reads itself" if isinstance(hit, ast.Attribute) and isinstance(hit.ctx, ast.Load)
                else "sets itself" if isinstance(hit, ast.Attribute) else "calls itself")
        yield Finding(
            "recursive-cycle", "high",
            f"{_short(name)}() {what} on every path (line {hit.lineno}): it can never return",
            path, hit.lineno,
            detail=(f"Nothing before line {hit.lineno} can return or raise, and the line is not "
                    f"inside an if or a loop, so every call to {_short(name)}() starts another "
                    f"one until Python gives up with RecursionError."))

    # 2. existing functions that now call each other in a circle
    old_comp: dict[str, int] = {}
    cycles = [c for c in _sccs(ctx.graph, ctx.after_defs) if set(c) & touched]
    if not cycles:
        return
    for i, comp in enumerate(_sccs(old_graph(ctx), ctx.before_defs)):
        for n in comp:
            old_comp[n] = i
    for members in cycles:
        existed = [m for m in members if m in ctx.before_defs]
        if len(existed) < 2:
            continue                                # recursion designed as recursion
        if len({old_comp.get(m, -1 - k) for k, m in enumerate(members)}) == 1:
            continue                                # the same loop was there before
        if not all(m in fns for m in members):
            fns = functions(ctx.after)
        if _guarded(members, fns):
            continue
        ring = _order(members, ctx.graph)
        if len(ring) == len(members) and _walks_a_structure(ring, ctx.graph, fns):
            continue                                # node -> node.children: ends with the data
        names = [f"{_short(m)}()" for m in ring]
        first = ctx.after_defs[sorted(set(members) & touched)[0]]
        yield Finding(
            "recursive-cycle", "medium",
            f"this change makes {', '.join(names[:-1])} and {names[-1]} call each other in a "
            f"loop",
            first.path, first.line,
            detail=(f"{' -> '.join(names + [names[0]])}. Before this change they did not call "
                    f"each other in a circle. None of them takes a depth, limit or visited "
                    f"argument, so nothing visible stops the loop: input that keeps it going "
                    f"ends in RecursionError, or a hang."),
            fix="Make sure the loop ends: pass a depth or visited set through every call, or "
                "break the cycle.")


def _class_node(tree, cls: str | None, path: str):
    if cls is None or tree is None:
        return None
    mod = module_name(path)
    node, body = None, tree.body
    for part in cls[len(mod) + 1:].split("."):
        node = next((s for s in body if isinstance(s, ast.ClassDef) and s.name == part), None)
        if node is None:
            return None
        body = node.body
    return node
