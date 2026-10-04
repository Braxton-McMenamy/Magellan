"""unsynchronized-shared-state: two threads share an object's fields with no lock between them.

Therac-25, 1985-1987: the operator could correct the prescription while the treatment task was
still setting up the magnets. The two tasks shared the beam mode and energy without
synchronisation, so a quick edit was only half seen, and the machine fired a beam about 100
times the intended dose. At least six accidents; three patients died of their overdoses.

The rule looks inside each class for methods started as threads (``Thread(target=self.m)``,
``Timer(t, self.m)``, ``executor.submit(self.m)``). When one of them writes a ``self.`` field
that another reads or writes, and neither holds a lock at that point (``with self.lock:``,
or any field assigned a ``threading`` Lock, RLock, Condition or Semaphore), the two can
interleave halfway through an update.

A method started more than once (in a loop, ``for _ in range(4): Thread(target=self.work)``,
or from two places) races with itself. It is reported when it updates a field from that
field's old value with no lock held (``self.done += 1``, ``self.total = self.total + n``):
two copies can read the same old value, and one update is lost. Quiet when the loop joins
each thread before starting the next and for a ``Timer`` that re-arms itself (those copies
run one after another), and for fields it only sets (``self.running = True``) or only calls
methods on (a ``queue.Queue`` locks itself).
"""

from __future__ import annotations

import ast

from magellan_lite.findings import rule

_LOCK_TYPES = {"Lock", "RLock", "Condition", "Semaphore", "BoundedSemaphore"}
_LOOPS = (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp, ast.DictComp,
          ast.GeneratorExp)


def _self_attr(node: ast.AST) -> str | None:
    """``self.x`` -> ``x``."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr
    return None


def _name(func: ast.AST) -> str:
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _started(call: ast.Call) -> str | None:
    """The method a ``Thread``, ``Timer`` or ``submit`` call starts: ``m`` for ``self.m``."""
    name = _name(call.func)
    target = None
    if name == "Thread":
        target = next((k.value for k in call.keywords if k.arg == "target"), None)
        if target is None and len(call.args) > 1:
            target = call.args[1]                       # Thread(group, target, ...)
    elif name == "Timer" and len(call.args) > 1:
        target = call.args[1]
    elif name == "submit" and call.args:
        target = call.args[0]
    return _self_attr(target) if target is not None else None


def _joins(call: ast.AST) -> bool:
    """``t.join()`` or ``t.join(5)``: waits for a thread (not ``", ".join(names)``)."""
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "join" and not isinstance(call.func.value, ast.Constant)):
        return False
    if not call.args and not call.keywords:
        return True                                     # t.join()
    if len(call.args) + len(call.keywords) > 1 or any(k.arg != "timeout" for k in call.keywords):
        return False
    wait = call.args[0] if call.args else call.keywords[0].value
    return isinstance(wait, ast.Constant) or call.keywords != [] or _name(wait) == "timeout"


def _thread_starts(cls: ast.ClassDef) -> dict[str, list[tuple[int, bool, bool]]]:
    """Methods of ``cls`` started on a thread of their own: ``(line, in a loop, from the
    method itself)`` for each place that starts one. A loop that also waits for a thread
    (``t.join()``) runs them one at a time, so it does not count as a loop here; nor does a
    method that starts itself again (a ``Timer`` that re-arms: one run after another)."""
    out: dict[str, list[tuple[int, bool, bool]]] = {}

    def visit(node: ast.AST, looping: bool, current: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            inside, within = looping, current
            if isinstance(child, _LOOPS):
                inside = looping or not any(_joins(n) for n in ast.walk(child))
            elif node is cls and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                within = child.name
            if isinstance(child, ast.Call) and (method := _started(child)):
                out.setdefault(method, []).append((child.lineno, looping, method == current))
            visit(child, inside, within)

    visit(cls, False, None)
    return out


def _locks(cls: ast.ClassDef) -> set[str]:
    """Fields that hold a lock: assigned a threading lock, or named like one."""
    out = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            f = node.value.func
            kind = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if kind in _LOCK_TYPES:
                out |= {a for t in node.targets if (a := _self_attr(t))}
        if isinstance(node, ast.Attribute) and "lock" in node.attr.lower():
            if _self_attr(node):
                out.add(node.attr)
    return out


def _unlocked(fn: ast.AST, locks: set[str]):
    """Every node of a method that runs with no lock held, without going into nested
    functions (they run later, maybe elsewhere)."""
    def visit(node: ast.AST, locked: bool):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and node is not fn:
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            holds = any(_self_attr(i.context_expr) in locks for i in node.items)
            for i in node.items:
                yield from visit(i.context_expr, locked)
            for child in node.body:
                yield from visit(child, locked or holds)
            return
        if not locked:
            yield node
        for child in ast.iter_child_nodes(node):
            yield from visit(child, locked)
    yield from visit(fn, False)


def _accesses(fn: ast.AST, locks: set[str]):
    """``(field, "write" or "read", line)`` for every ``self.`` field touched with no lock
    held. Writes include ``self.x += 1`` and calling a method on the field."""
    for node in _unlocked(fn, locks):
        attr = _self_attr(node)
        if attr and attr not in locks:                  # self.x += 1 is a Store too
            yield attr, "write" if isinstance(node.ctx, (ast.Store, ast.Del)) else "read", node.lineno
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (attr := _self_attr(node.func.value)) and attr not in locks:
                yield attr, "write", node.lineno        # self.items.append(...) changes it


def _lost_updates(fn: ast.AST, locks: set[str]):
    """``(field, line)`` for every update of a ``self.`` field from its own old value with no
    lock held: ``self.n += 1``, ``self.counts[key] += 1``, ``self.total = self.total + n``."""
    for node in _unlocked(fn, locks):
        if isinstance(node, ast.AugAssign):
            target = node.target.value if isinstance(node.target, ast.Subscript) else node.target
            if (attr := _self_attr(target)) and attr not in locks:
                yield attr, node.lineno
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            old = {_self_attr(n) for n in ast.walk(node.value)}
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if (attr := _self_attr(target)) and attr in old and attr not in locks:
                    yield attr, node.lineno


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


@rule("unsynchronized-shared-state", "high",
      fix="Hold one lock around every read and write of the shared fields "
          "(`with self.lock:` in both methods), so one thread never sees half an update.")
def unsynchronized_shared_state(tree: ast.Module, path: str):
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        methods = {f.name: f for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        starts = {m: s for m, s in _thread_starts(cls).items() if m in methods}
        if not starts:
            continue
        targets = {m: s[0][0] for m, s in starts.items()}   # the first line that starts each
        locks = _locks(cls)
        reported: set[str] = set()                      # one finding per writer is enough

        # two methods on two threads, one writing what the other uses
        touched = {m: list(_accesses(methods[m], locks)) for m in targets}
        for writer, uses in touched.items():
            writes = {}
            for attr, how, line in uses:
                if how == "write":
                    writes.setdefault(attr, line)
            for other, their in touched.items():
                if other == writer:
                    continue
                shared = sorted(a for a in writes if any(a == b for b, _h, _l in their))
                if not shared:
                    continue
                first = min(writes[a] for a in shared)
                reported.add(writer)
                yield (first,
                       f"{writer}() writes {_and([f'self.{a}' for a in shared])} while "
                       f"{other}() uses {'it' if len(shared) == 1 else 'them'} on another "
                       f"thread, with no lock held",
                       f"Both are started as threads (lines {targets[writer]} and "
                       f"{targets[other]}), so {other}() can run halfway through {writer}()'s "
                       f"update and see a mix of old and new values. Therac-25: a quick edit "
                       f"was half seen by the setup task, and patients received about 100 "
                       f"times the intended dose.")
                break

        # one method on several threads at once, racing with itself
        for method, lines in starts.items():
            again = [(n, in_loop) for n, in_loop, itself in lines if not itself]
            if method in reported or not (len(again) > 1 or any(loop for _, loop in again)):
                continue
            lost: dict[str, int] = {}
            for attr, line in _lost_updates(methods[method], locks):
                lost.setdefault(attr, line)
            if not lost:
                continue
            looped = next((n for n, in_loop in again if in_loop), None)
            how = (f"in a loop (line {looped})" if looped is not None else
                   f"from {len(again)} places (lines {_and([str(n) for n, _ in again])})")
            yield (min(lost.values()),
                   f"{method}() updates {_and([f'self.{a}' for a in lost])} from the "
                   f"old value with no lock held, and it runs on several threads at once",
                   f"It is started as a thread {how}, so two copies can read the same old "
                   f"value and one of the updates is lost. Therac-25: tasks that shared the "
                   f"machine's settings with no lock between them let it fire a beam about "
                   f"100 times the intended dose.")
