"""complexity-regression: an edited function whose work now grows faster with its input.

From the full Magellan (``magellan/python/rules/complexity.py``). Not "is this slow" -- a
parser cannot see constant factors, I/O or memory -- but "did this change make it slower in
a way that gets worse as data grows": a loop added inside a loop, a list scanned with ``in``
for every item, a sort inside a loop, a helper that loops over its input called once per item
(the N+1 shape). Every estimate is made the same way on both versions, so only the change
shows up; a function that was already quadratic is not news.

The estimate is the shape, from the syntax tree:

* a loop over something that grows (a parameter, ``self.items``, ``range(n)``) is one level;
  ``range(10)``, a literal, an ``ALL_CAPS`` table or a parameter with a numeric default is not;
* a loop over a piece of an outer loop's element (``for cell in row`` inside ``for row in
  grid``) adds up to the total, not a product, so it is not a level;
* ``x in some_list`` (a list the function built, or a parameter annotated as one), ``.index``,
  ``.remove``, ``.insert(0, ...)``, ``sorted``/``.sort()``, ``sum``/``min``/``max`` over a
  collection each cost one more level where they run;
* a call to one of the project's functions costs what that function costs, multiplied by the
  loops around the call when it is handed something that grows (not a piece of the current
  item).

Reported when the edited function reaches quadratic or worse and is worse than it was.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from magellan_lite.findings import Finding, change_rule
from magellan_lite.rules.common import call_name, functions, old_graph

_PASSTHROUGH = {"enumerate", "reversed", "sorted", "list", "tuple", "set", "iter", "filter",
                "map", "zip", "frozenset"}
_VIEWS = {"items", "keys", "values"}
_SCANS = {"sum", "min", "max", "any", "all"}
_COPIES = {"list", "tuple", "set", "frozenset", "dict", "deepcopy"}
_LIST_SCANS = {"index", "count", "remove"}
_LIST_TYPES = ("list", "List", "Sequence", "tuple", "Tuple")
_HASHED = {"set", "frozenset", "dict", "defaultdict", "Counter", "OrderedDict"}
_MAX_DEPTH = 6


@dataclass
class Cost:
    degree: int = 0
    log: int = 0
    why: str = ""
    #: the parameters that make it grow; None when something else does (state, globals)
    drivers: set | None = field(default_factory=set)

    @property
    def rank(self) -> tuple[int, int]:
        return self.degree, self.log

    def notation(self) -> str:
        if not self.degree and not self.log:
            return "O(1)"
        n = {0: "", 1: "n"}.get(self.degree, f"n^{self.degree}")
        log = "log n" if self.log == 1 else f"log^{self.log} n" if self.log else ""
        return f"O({' '.join(p for p in (n, log) if p)})"


def _root(expr: ast.AST) -> str:
    """What an iterable or argument is a view of: ``xs`` for ``enumerate(xs)``,
    ``self.items`` for ``self.items.values()``; ``""`` when it cannot be told."""
    while True:
        if isinstance(expr, ast.Call):
            name = call_name(expr)
            if isinstance(expr.func, ast.Name) and name in _PASSTHROUGH and expr.args:
                expr = expr.args[0]
                continue
            if isinstance(expr.func, ast.Attribute) and name in _VIEWS:
                expr = expr.func.value
                continue
            return ""
        if isinstance(expr, (ast.Subscript, ast.Starred)):
            expr = expr.value
            continue
        break
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        return f"{expr.value.id}.{expr.attr}" if expr.value.id == "self" else expr.value.id
    return ""


class _Walker:
    """One function body: how deep its growing loops nest, and what else costs a level."""

    def __init__(self, fn, cls, project, owner: str, own: bool = False) -> None:
        self.fn, self.project, self.owner = fn, project, owner
        #: True for the function a finding is about: only what *it* multiplies counts, not a
        #: one-off call to something expensive (that function is responsible for itself)
        self.own = own
        args = fn.args
        self.params = [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
        if cls is not None and self.params[:1] in (["self"], ["cls"]):
            self.params = self.params[1:]
        self.fixed: set[str] = set()                # don't grow: numeric-default parameters
        self.lists: set[str] = set()                # known lists: `in` scans them
        self.hashed: set[str] = set()
        positional = [*args.posonlyargs, *args.args]
        defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
        for a, d in [*zip(positional, defaults), *zip(args.kwonlyargs, args.kw_defaults)]:
            ann = ast.unparse(a.annotation) if a.annotation is not None else ""
            if isinstance(d, ast.Constant) and isinstance(d.value, (int, float)) \
                    and not isinstance(d.value, bool):
                self.fixed.add(a.arg)
            if ann.startswith(_LIST_TYPES):
                self.lists.add(a.arg)
            if any(h in ann.lower() for h in ("set", "dict", "mapping", "counter")):
                self.hashed.add(a.arg)
        self.loops: list[str] = []                  # what each enclosing growing loop is over
        self.derived: set[str] = set()              # pieces of an enclosing loop's element
        self.best = Cost()
        self.drivers: set | None = set()

    # -- what grows ---------------------------------------------------------------------------
    def grows(self, expr: ast.AST | None) -> bool:
        if expr is None or isinstance(expr, (ast.Constant, ast.List, ast.Tuple, ast.Set,
                                             ast.Dict, ast.JoinedStr)):
            return False
        called = {id(n.func) for n in ast.walk(expr) if isinstance(n, ast.Call)}
        values = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)
                  and isinstance(n.ctx, ast.Load) and id(n) not in called}
        if all(v in self.derived or v in self.fixed or (v.isupper() and len(v) > 1)
               for v in values):
            return False            # made only of constants and pieces of the current item
        if isinstance(expr, ast.Call):
            name = call_name(expr)
            if name == "range" and isinstance(expr.func, ast.Name):
                return bool(expr.args) and self.grows(expr.args[-1] if len(expr.args) < 3
                                                      else expr.args[1])
            if name == "len" and expr.args:
                return self.grows(expr.args[0])
            if name == "get" and isinstance(expr.func, ast.Attribute):
                return False                        # one bucket of an index
        if isinstance(expr, ast.BinOp):
            return self.grows(expr.left) or self.grows(expr.right)
        if isinstance(expr, ast.UnaryOp):
            return self.grows(expr.operand)
        root = _root(expr)
        if not root and isinstance(expr, ast.Call):
            return bool(expr.args) and self.grows(expr.args[0])
        base = root.split(".")[0]
        if base in self.fixed or (base.isupper() and len(base) > 1) or base in self.derived:
            return False
        return True

    def _driver(self, expr: ast.AST) -> str:
        """The name whose size makes ``expr`` grow: ``n`` for ``range(n - 1)``, ``xs`` for
        ``len(xs)`` or ``enumerate(xs)``; ``""`` when it cannot be told."""
        for _ in range(8):
            if isinstance(expr, ast.Call):
                name = call_name(expr)
                if name == "range" and expr.args:
                    expr = expr.args[-1] if len(expr.args) < 3 else expr.args[1]
                    continue
                if name == "len" and expr.args:
                    expr = expr.args[0]
                    continue
                root = _root(expr)
                if root or not expr.args:
                    return root
                expr = expr.args[0]
            elif isinstance(expr, ast.BinOp):
                expr = expr.left if self.grows(expr.left) else expr.right
            elif isinstance(expr, ast.UnaryOp):
                expr = expr.operand
            else:
                return _root(expr)
        return ""

    def _note_driver(self, expr: ast.AST) -> None:
        root = self._driver(expr).split(".")[0]
        if self.drivers is not None:
            if root in self.params:
                self.drivers.add(root)
            else:
                self.drivers = None                 # state, a global, a local of unknown size

    def _inside(self) -> str:
        return f" inside a loop over {self.loops[-1]}" if self.loops else ""

    def bump(self, extra: int, log: int, why: str, line: int | None = None) -> None:
        degree = len(self.loops) + extra
        if (degree, log) > self.best.rank:
            self.best = Cost(degree, log, why if line is None else f"{why} (line {line})")

    # -- walking -------------------------------------------------------------------------------
    def run(self) -> Cost:
        self.block(self.fn.body)
        self.best.drivers = self.drivers
        return self.best

    def block(self, body) -> None:
        for st in body:
            self.stmt(st)

    def enter(self, iter_expr: ast.AST, target: ast.AST | None, line: int) -> bool:
        """Start a loop over ``iter_expr``; True when it is a growing level."""
        root = _root(iter_expr).split(".")[0]
        names = set() if target is None else \
            {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        if root and root in self.derived:
            self.derived |= names                   # a piece of a piece
            return False
        if not self.grows(iter_expr):
            return False
        self._note_driver(iter_expr)
        what = ast.unparse(iter_expr)
        what = what if len(what) <= 40 else what[:37] + "..."
        inner = f" inside a loop over {self.loops[-1]}" if self.loops else ""
        self.loops.append(what)
        self.bump(0, 0, f"loops over {what}{inner}", line)
        self.derived |= names
        return True

    def stmt(self, st: ast.stmt) -> None:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(st, (ast.For, ast.AsyncFor)):
            self.expr(st.iter)
            entered = self.enter(st.iter, st.target, st.lineno)
            self.block(st.body)
            self.block(st.orelse)
            if entered:
                self.loops.pop()
            return
        if isinstance(st, ast.While):
            self.expr(st.test)
            entered = False
            if not isinstance(st.test, ast.Constant) and not _geometric(st):
                bound = next((n.args[0] for n in ast.walk(st.test) if isinstance(n, ast.Call)
                              and call_name(n) == "len" and n.args), None)
                if bound is None:
                    bound = next((n for n in ast.walk(st.test) if isinstance(n, ast.Name)
                                  and n.id in self.params and n.id not in self.fixed), None)
                if bound is not None:
                    entered = self.enter(bound, None, st.lineno)
            self.block(st.body)
            self.block(st.orelse)
            if entered:
                self.loops.pop()
            return
        if isinstance(st, ast.Assign):
            self._assigned(st.targets, st.value)
        elif isinstance(st, ast.AnnAssign) and st.value is not None:
            self._assigned([st.target], st.value)
        for child in ast.iter_child_nodes(st):
            if isinstance(child, ast.expr):
                self.expr(child)
            elif isinstance(child, ast.stmt):
                self.stmt(child)
            elif isinstance(child, ast.withitem):
                self.expr(child.context_expr)
            elif isinstance(child, (ast.excepthandler, getattr(ast, "match_case", ()))):
                self.block(child.body)

    def _assigned(self, targets, value) -> None:
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        is_list = isinstance(value, (ast.List, ast.ListComp)) or (
            isinstance(value, ast.Call) and call_name(value) == "list"
            and isinstance(value.func, ast.Name))
        is_hashed = isinstance(value, (ast.Set, ast.Dict, ast.SetComp, ast.DictComp)) or (
            isinstance(value, ast.Call) and call_name(value) in _HASHED) or (
            isinstance(value, ast.BoolOp) and any(
                isinstance(v, (ast.Set, ast.Dict)) or (isinstance(v, ast.Call)
                                                       and call_name(v) in _HASHED)
                for v in value.values))
        # made from a piece of the current item (`tree = load(path) if ok else None`)
        from_piece = bool(self.loops) and bool(
            {n.id for n in ast.walk(value) if isinstance(n, ast.Name)
             and isinstance(n.ctx, ast.Load)} & self.derived)
        for n in names:
            self.lists.discard(n)
            self.hashed.discard(n)
            if is_list:
                self.lists.add(n)
            if is_hashed:
                self.hashed.add(n)
            if from_piece:
                self.derived.add(n)

    def expr(self, e: ast.AST) -> None:
        if isinstance(e, (ast.Lambda,)):
            return
        if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            entered = 0
            for gen in e.generators:
                self.expr(gen.iter)
                if self.enter(gen.iter, gen.target, e.lineno):
                    entered += 1
                for cond in gen.ifs:
                    self.expr(cond)
            for part in (getattr(e, "elt", None), getattr(e, "key", None),
                         getattr(e, "value", None)):
                if part is not None:
                    self.expr(part)
            if entered:
                del self.loops[-entered:]
            return
        if isinstance(e, ast.Compare):
            for op, comp in zip(e.ops, e.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) and isinstance(comp, ast.Name) \
                        and comp.id in self.lists and comp.id not in self.hashed \
                        and self.loops:
                    self._note_driver(comp)
                    self.bump(1, 0, f"checks `in {comp.id}` (a list, so each check scans "
                                    f"all of it) inside a loop over {self.loops[-1]}", e.lineno)
        if isinstance(e, ast.Call):
            self.call(e)
        for child in ast.iter_child_nodes(e):
            if isinstance(child, ast.expr):
                self.expr(child)

    def call(self, c: ast.Call) -> None:
        name = call_name(c)
        func = c.func
        recv = func.value if isinstance(func, ast.Attribute) else None
        arg = c.args[0] if c.args else None
        comprehension = isinstance(arg, (ast.GeneratorExp, ast.ListComp, ast.SetComp))
        if name == "sorted" and isinstance(func, ast.Name) and arg is not None \
                and not comprehension and self.grows(arg):
            self._note_driver(arg)
            self.bump(1, 1, f"sorts {ast.unparse(arg)}{self._inside()}", c.lineno)
        elif name == "sort" and recv is not None and self.grows(recv):
            self._note_driver(recv)
            self.bump(1, 1, f"sorts {ast.unparse(recv)}{self._inside()}", c.lineno)
        elif name in _SCANS | _COPIES and isinstance(func, ast.Name) and arg is not None \
                and not comprehension and isinstance(arg, (ast.Name, ast.Attribute)) \
                and self.grows(arg):
            self._note_driver(arg)
            self.bump(1, 0, f"calls {name}({ast.unparse(arg)}), which goes through all of it"
                            f"{self._inside()}", c.lineno)
        elif recv is not None and isinstance(recv, ast.Name) and recv.id in self.lists \
                and (name in _LIST_SCANS or (
                    name in ("insert", "pop") and c.args and isinstance(c.args[0], ast.Constant)
                    and c.args[0].value == 0)):
            self._note_driver(recv)
            self.bump(1, 0, f"calls {recv.id}.{name}(), which goes through the whole list"
                            f"{self._inside()}", c.lineno)
        for target in self.project.targets(self, c):
            callee = self.project.cost(target)
            if callee is None or not callee.rank > (0, 0):
                continue
            carries = bool(self.loops) and self._carries(c, target, callee)
            if self.own and not carries:
                continue
            extra = callee.degree if carries else max(callee.degree - len(self.loops), 0)
            if self.drivers is not None:
                for a in self._args_for(c, target, callee):
                    self._note_driver(a)
                if callee.drivers is None:
                    self.drivers = None
            where = f" inside a loop over {self.loops[-1]}" if carries else ""
            short = target.rsplit(".", 1)[-1]
            self.bump(extra, callee.log,
                      f"calls {short}(){where} (line {c.lineno}), and {short}() is "
                      f"{callee.notation()} by itself: it {callee.why}")

    def _args_for(self, c: ast.Call, target: str, callee: Cost) -> list[ast.AST]:
        _path, fn, cls = self.project.fns[target]
        names = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args]]
        if cls is not None and names[:1] in (["self"], ["cls"]):
            names = names[1:]
        bound = dict(zip(names, c.args))
        bound.update({k.arg: k.value for k in c.keywords if k.arg})
        return [bound[d] for d in (callee.drivers or ()) if d in bound]

    def _carries(self, c: ast.Call, target: str, callee: Cost) -> bool:
        """Does the call hand the callee the thing that makes it grow -- something that grows
        here too, and not a piece of the current item? Then its cost multiplies with the loops
        around the call. A callee that grows with something else (its object's state, a
        global) is not judged: how big that is cannot be told from here."""
        if not callee.drivers:
            return False
        return any(self.grows(a) for a in self._args_for(c, target, callee))


def _geometric(loop: ast.While) -> bool:
    """Does the loop halve or double what its condition reads, or bisect?"""
    test_names = {n.id for n in ast.walk(loop.test) if isinstance(n, ast.Name)}
    for st in ast.walk(loop):
        if isinstance(st, ast.AugAssign) and isinstance(st.target, ast.Name) \
                and st.target.id in test_names \
                and isinstance(st.op, (ast.FloorDiv, ast.Div, ast.RShift, ast.Mult, ast.LShift)):
            return True
        if isinstance(st, ast.Assign) and isinstance(st.value, ast.BinOp) \
                and isinstance(st.value.op, (ast.FloorDiv, ast.RShift)):
            return True                             # mid = (lo + hi) // 2
    return False


class _Project:
    """Costs of every function in one version, worked out on demand."""

    def __init__(self, snapshot, graph) -> None:
        self.fns = functions(snapshot)
        self.graph = graph
        self.costs: dict[str, Cost | None] = {}
        self.busy: list[str] = []

    def targets(self, walker: _Walker, c: ast.Call) -> list[str]:
        edges = [e for e in self.graph.uses(walker.owner) if e.kind == "calls" and not e.guess
                 and e.line == c.lineno and e.dst.rsplit(".", 1)[-1] == call_name(c)]
        return sorted({e.dst for e in edges if e.dst in self.fns})

    def cost(self, name: str) -> Cost | None:
        if name in self.costs:
            return self.costs[name]
        if name in self.busy or len(self.busy) >= _MAX_DEPTH or name not in self.fns:
            return None                             # recursion, or too deep to follow
        self.busy.append(name)
        _path, fn, cls = self.fns[name]
        self.costs[name] = _Walker(fn, cls, self, name).run()
        self.busy.pop()
        return self.costs[name]

    def own(self, name: str) -> Cost | None:
        """What the function's own loops and calls multiply, the part a change to it is
        responsible for."""
        if name not in self.fns:
            return None
        _path, fn, cls = self.fns[name]
        return _Walker(fn, cls, self, name, own=True).run()


def _steps(degree: int, log: int) -> str:
    n = 10_000
    value = n ** degree * (14 ** log)
    for size, word in ((10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"),
                       (10 ** 3, "thousand")):
        if value >= size:
            return f"{value // size:,} {word}"
    return str(value)


@change_rule("complexity-regression", "medium",
             fix="Move the inner work out of the loop: build a set or dict once and look up in "
                 "it, sort once before the loop, or fetch everything in one call instead of "
                 "one per item.")
def complexity_regression(ctx):
    edited = [c for c in ctx.changes if c.kind in ("body", "signature")
              and c.after is not None and c.after.kind in ("function", "method")
              and not c.after.lang]
    if not edited:
        return
    now = _Project(ctx.after, ctx.graph)
    then = None
    for c in edited:
        after = now.own(c.name)
        if after is None or after.degree < 2:
            continue
        if then is None:
            then = _Project(ctx.before, old_graph(ctx))
        before = then.own(c.before.name)
        if before is None or after.rank <= before.rank:
            continue
        severity = "high" if after.degree >= 3 and after.degree - before.degree >= 2 else "medium"
        yield Finding(
            "complexity-regression", severity,
            f"{c.after.short}() went from {before.notation()} to {after.notation()}: its work "
            f"now grows {'with the square' if after.degree == 2 else 'faster than the square'} "
            f"of its input",
            c.after.path, c.after.line,
            detail=(f"It now {after.why}. At 10,000 items that is about "
                    f"{_steps(after.degree, after.log)} steps where it used to be about "
                    f"{_steps(before.degree, before.log)}: fine in a test, slow or timing "
                    f"out on real data. Estimated from the shape of the code, not measured."))
