"""loop-without-progress: a ``while`` loop that nothing inside it can stop.

* Knight Capital: Power Peg sent child orders ``while order.filled < order.qty``. A 2005
  change moved the fill counting out of the loop, so nothing in it changed ``order.filled``
  any more. Woken up in 2012, it sent orders without end: $460 million in 45 minutes.
* Microsoft Zune, December 31 2008: ``while days > 365`` subtracted 366 in a leap year only
  ``if days > 366``. On day 366 of a leap year no path changed anything, and every Zune 30
  froze at boot.

One finding per loop, for loops in code the change touched:

* the whole loop: nothing in the body changes what the condition reads, hands it to a call
  that could change it, or leaves the loop (break, return, raise, sys.exit);
* one path: some way through the body's if/else changes nothing that the condition or that
  path's own tests read, and does not leave -- and simple integer reasoning over those tests
  says the path can be taken (``days == 366``).

Precision first, because a gate people learn to ignore is worthless. Quiet for
``while True``, conditions that call something (polling), loops that sleep, wait, await or
yield (something else moves them on), conditions on globals when the body calls anything, and
bodies too tangled to enumerate.
"""

from __future__ import annotations

import ast

from magellan_lite.findings import rule

MAX_PATHS = 64
_EXITS = {"exit", "_exit", "quit", "abort"}
_WAITS = {"sleep", "wait", "join", "select", "poll", "acquire", "recv", "accept"}
_IMMUTABLE_TYPES = {"int", "float", "str", "bool", "bytes", "complex"}
_NUMERIC_CALLS = {"int", "float", "len", "abs", "round", "min", "max", "sum", "ord", "str"}


def _dotted(node: ast.AST) -> str | None:
    """``order.filled`` for an attribute chain on a name, ``days`` for a name."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


def _name(func: ast.AST) -> str:
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _reads(expr: ast.AST) -> set[str]:
    """What an expression reads: the outermost name or attribute chains in it."""
    if (d := _dotted(expr)) is not None:
        return {d}
    out = set()
    if isinstance(expr, ast.Call):                      # is_leap_year(year) reads year
        children = [*expr.args, *(k.value for k in expr.keywords)]
        if isinstance(expr.func, ast.Attribute):
            children.append(expr.func.value)            # q.empty() reads q
    else:
        children = list(ast.iter_child_nodes(expr))
    for child in children:
        out |= _reads(child)
    return out


def _nodes(stmts: list[ast.AST]):
    """Every node under ``stmts``, without going into nested functions or classes."""
    todo = list(stmts)
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(node))


class _Function:
    """What the loop's function says about its own names."""

    def __init__(self, fn: ast.AST | None) -> None:
        self.locals: set[str] = set()
        self.immutable: set[str] = set()
        #: names that are another way to reach an object: ``pop = queue.popleft`` -> queue
        self.aliases: dict[str, str] = {}
        if fn is None:
            return
        args = fn.args
        assigned: dict[str, list] = {}
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if a is None:
                continue
            self.locals.add(a.arg)
            if isinstance(a.annotation, ast.Name) and a.annotation.id in _IMMUTABLE_TYPES:
                self.immutable.add(a.arg)
        shared: set[str] = set()
        for node in _nodes(fn.body):
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                shared |= set(node.names)               # assigned here, but not ours
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        self.locals.add(t.id)
                        assigned.setdefault(t.id, []).append(node)
                value = getattr(node, "value", None)
                obj = value.value if isinstance(value, ast.Attribute) else value
                if isinstance(node, ast.Assign) and (d := _dotted(obj) if obj is not None else None):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            self.aliases[t.id] = d
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                self.locals |= {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
            elif isinstance(node, ast.NamedExpr):
                self.locals.add(node.target.id)
        self.locals -= shared
        for name, nodes in assigned.items():
            if name not in shared and all(self._numeric(n) for n in nodes):
                self.immutable.add(name)

    @staticmethod
    def _numeric(node: ast.AST) -> bool:
        """Is this assignment's value a number or a string (which no call can change)?"""
        if isinstance(node, ast.AugAssign):
            return True
        value = node.value
        if value is None:
            return False
        if isinstance(value, ast.Constant):
            return not isinstance(value.value, (list, dict, set))
        if isinstance(value, (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.JoinedStr)):
            return True
        if isinstance(value, ast.Name):
            return value.id.isupper()                   # ORIGIN_YEAR: a module constant
        if isinstance(value, ast.Call):
            return _name(value.func) in _NUMERIC_CALLS
        return False


def _changes(nodes: list[ast.AST], fn: _Function) -> set[str]:
    """What these statements (and tests) may change. ``*`` for anything."""
    out: set[str] = set()

    def target(t: ast.AST) -> None:
        if isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                target(e)
        elif isinstance(t, ast.Starred):
            target(t.value)
        elif isinstance(t, ast.Subscript):
            target(t.value)                             # d[k] = v changes d
        elif (d := _dotted(t)) is not None:
            out.add(d)

    for node in _nodes(nodes):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            out.add("*")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                target(t)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            target(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            target(node.target)
        elif isinstance(node, ast.Delete):
            for t in node.targets:
                target(t)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            target(node.optional_vars)
        elif isinstance(node, ast.NamedExpr):
            target(node.target)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and (d := _dotted(node.func.value)):
                out.add(d)                              # obj.method() may change obj
            if isinstance(node.func, ast.Name) and node.func.id in fn.aliases:
                out.add(node.func.id)                   # pop() where pop = queue.popleft
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                if isinstance(arg, ast.Starred):
                    arg = arg.value
                d = _dotted(arg)
                if d is not None and d not in fn.immutable:
                    out.add(d)                          # a call can change what it is given
    # a change through an alias is a change to what it stands for
    out |= {fn.aliases[c.split(".")[0]] for c in out if c.split(".")[0] in fn.aliases}
    return out


def _affects(changed: set[str], read: str) -> bool:
    return "*" in changed or any(
        read == c or read.startswith(c + ".") or c.startswith(read + ".") for c in changed)


def _leaves(node: ast.AST, inner: bool = False) -> bool:
    """Can this leave the loop: break (not an inner loop's), return, raise, sys.exit()?"""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
        return False
    if isinstance(node, (ast.Return, ast.Raise)):
        return True
    if isinstance(node, ast.Break):
        return not inner
    if isinstance(node, ast.Call) and _name(node.func) in _EXITS:
        return True
    inner = inner or isinstance(node, (ast.For, ast.AsyncFor, ast.While))
    return any(_leaves(c, inner) for c in ast.iter_child_nodes(node))


def _paths(stmts: list[ast.stmt]):
    """Every way through ``stmts`` along its if/else: ``(tests, statements)`` with each test
    as ``(expression, taken)``. ``None`` past MAX_PATHS."""
    paths = [([], [])]
    for s in stmts:
        if isinstance(s, ast.If):
            yes, no = _paths(s.body), _paths(s.orelse)
            if yes is None or no is None:
                return None
            paths = [(t + [(s.test, True)] + t2, b + b2) for t, b in paths for t2, b2 in yes] + \
                    [(t + [(s.test, False)] + t2, b + b2) for t, b in paths for t2, b2 in no]
            if len(paths) > MAX_PATHS:
                return None
        else:
            paths = [(t, b + [s]) for t, b in paths]
    return paths


_FLIP = {ast.Lt: ast.GtE, ast.LtE: ast.Gt, ast.Gt: ast.LtE, ast.GtE: ast.Lt, ast.Eq: ast.NotEq,
         ast.NotEq: ast.Eq}
_MIRROR = {ast.Lt: ast.Gt, ast.LtE: ast.GtE, ast.Gt: ast.Lt, ast.GtE: ast.LtE, ast.Eq: ast.Eq,
           ast.NotEq: ast.NotEq}


def _bounds(tests) -> dict[str, list] | None:
    """Integer ranges the tests allow for plain names (``days > 365`` and not
    ``days > 366`` leave 366..366). ``None`` when no value can satisfy them."""
    env: dict[str, list] = {}

    def constrain(e: ast.AST, taken: bool) -> None:
        if isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.Not):
            return constrain(e.operand, not taken)
        if isinstance(e, ast.BoolOp) and isinstance(e.op, ast.And) == taken:
            for v in e.values:                          # a and b true; a or b false
                constrain(v, taken)
            return
        if not (isinstance(e, ast.Compare) and len(e.ops) == 1 and type(e.ops[0]) in _FLIP):
            return
        left, right, op = e.left, e.comparators[0], type(e.ops[0])
        if isinstance(left, ast.Constant) and isinstance(right, ast.Name):
            left, right, op = right, left, _MIRROR[op]
        if not (isinstance(left, ast.Name) and isinstance(right, ast.Constant)
                and type(right.value) is int):
            return
        if not taken:
            op = _FLIP[op]
        c = right.value
        lo, hi = env.setdefault(left.id, [float("-inf"), float("inf")])
        lo, hi = {ast.Lt: (lo, min(hi, c - 1)), ast.LtE: (lo, min(hi, c)),
                  ast.Gt: (max(lo, c + 1), hi), ast.GtE: (max(lo, c), hi),
                  ast.Eq: (max(lo, c), min(hi, c)), ast.NotEq: (lo, hi)}[op]
        if op is ast.NotEq and lo == hi == c:
            lo, hi = 1, 0
        env[left.id] = [lo, hi]

    for e, taken in tests:
        constrain(e, taken)
    return None if any(lo > hi for lo, hi in env.values()) else env


def _list(names) -> str:
    names = sorted(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _stuck(loop: ast.While, fn: _Function):
    """``(message)`` when some way through the loop can never end it, else ``None``."""
    cond = loop.test
    if isinstance(cond, ast.Constant):
        return None                                     # while True: leaves some other way
    if any(isinstance(n, (ast.Call, ast.Await, ast.NamedExpr)) for n in ast.walk(cond)):
        return None                                     # polling: the call can change
    reads = _reads(cond)
    body = list(_nodes(loop.body))
    if not reads or any(isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await)) for n in body):
        return None
    calls = [n for n in body if isinstance(n, ast.Call)]
    if any(_name(c.func) in _WAITS for c in calls):
        return None                                     # waits for another thread
    roots = {r.split(".")[0] for r in reads}
    if calls and any(r not in fn.locals and r != "self" for r in roots):
        return None                                     # a global any call might change
    heap = any("." in r for r in reads) or any(isinstance(n, ast.Subscript) for n in ast.walk(cond))
    if heap and any(isinstance(c.func, ast.Attribute) for c in calls):
        return None     # node.remove() can change parent.children: other references, unseen
    paths = _paths(loop.body)
    if paths is None:
        return None
    for tests, stmts in paths:
        if any(_leaves(s) for s in stmts):
            continue
        watched = reads | {r for e, _ in tests for r in _reads(e)}
        changed = _changes([*stmts, *(e for e, _ in tests)], fn)
        if any(_affects(changed, r) for r in watched):
            continue
        env = _bounds([(cond, True), *tests])
        if env is None:
            continue                                    # this path cannot be taken
        what = _list(watched)
        if len(paths) == 1:
            return (f"this loop never ends once it starts: nothing in it changes {what}, and "
                    f"nothing leaves it")
        witness = [f"{n} == {int(lo if lo != float('-inf') else hi)}"
                   for n, (lo, hi) in sorted(env.items())
                   if n in reads and (lo != float("-inf") or hi != float("inf"))]
        where = " and ".join(ast.unparse(e) if taken else f"not {ast.unparse(e)}"
                             for e, taken in tests)
        return (f"this loop never ends when {where}"
                f"{' (' + ', '.join(witness) + ')' if witness else ''}: nothing on that path "
                f"changes {what}, and nothing leaves the loop")
    return None


@rule("loop-without-progress", "high", blocking=True,
      fix="Make every way through the loop change what its condition reads, or leave the "
          "loop (break, return or raise) where it cannot.")
def loop_without_progress(tree: ast.Module, path: str):
    def visit(node: ast.AST, fn: _Function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from visit(child, _Function(child))
            elif isinstance(child, ast.While):
                message = _stuck(child, fn)
                if message:
                    yield child, message
                yield from visit(child, fn)
            else:
                yield from visit(child, fn)

    for loop, message in visit(tree, _Function(None)):
        yield (loop, message,
               "Once it runs, the program hangs or keeps doing the same thing forever. Knight "
               "Capital, 2012: a loop like this sent orders without end ($460 million in 45 "
               "minutes). Zune, 2008: one like it froze every Zune 30 on the last day of a "
               "leap year.")
