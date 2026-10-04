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
"""

from __future__ import annotations

import ast

from magellan_lite.findings import rule

_LOCK_TYPES = {"Lock", "RLock", "Condition", "Semaphore", "BoundedSemaphore"}


def _self_attr(node: ast.AST) -> str | None:
    """``self.x`` -> ``x``."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr
    return None


def _thread_targets(cls: ast.ClassDef) -> dict[str, int]:
    """Methods of ``cls`` started on a thread of their own, and the line that starts each."""
    out: dict[str, int] = {}
    for call in ast.walk(cls):
        if not isinstance(call, ast.Call):
            continue
        f = call.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        target = None
        if name == "Thread":
            target = next((k.value for k in call.keywords if k.arg == "target"), None)
            if target is None and len(call.args) > 1:
                target = call.args[1]                   # Thread(group, target, ...)
        elif name == "Timer" and len(call.args) > 1:
            target = call.args[1]
        elif name == "submit" and call.args:
            target = call.args[0]
        method = _self_attr(target) if target is not None else None
        if method:
            out.setdefault(method, call.lineno)
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


def _accesses(fn: ast.AST, locks: set[str]):
    """``(field, "write" or "read", line)`` for every ``self.`` field touched with no lock
    held. Writes include ``self.x += 1`` and calling a method on the field."""
    def visit(node: ast.AST, locked: bool):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and node is not fn:
            return                                      # runs later, maybe elsewhere
        if isinstance(node, (ast.With, ast.AsyncWith)):
            holds = any(_self_attr(i.context_expr) in locks for i in node.items)
            for i in node.items:
                yield from visit(i.context_expr, locked)
            for child in node.body:
                yield from visit(child, locked or holds)
            return
        if not locked:
            attr = _self_attr(node)
            if attr and attr not in locks:              # self.x += 1 is a Store too
                yield attr, "write" if isinstance(node.ctx, (ast.Store, ast.Del)) else "read", node.lineno
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (attr := _self_attr(node.func.value)) and attr not in locks:
                    yield attr, "write", node.lineno    # self.items.append(...) changes it
        for child in ast.iter_child_nodes(node):
            yield from visit(child, locked)
    yield from visit(fn, False)


@rule("unsynchronized-shared-state", "high",
      fix="Hold one lock around every read and write of the shared fields "
          "(`with self.lock:` in both methods), so one thread never sees half an update.")
def unsynchronized_shared_state(tree: ast.Module, path: str):
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        targets = _thread_targets(cls)
        if len(targets) < 2:
            continue
        methods = {f.name: f for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        locks = _locks(cls)
        touched = {m: list(_accesses(methods[m], locks)) for m in targets if m in methods}
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
                names = [f"self.{a}" for a in shared]
                fields = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
                yield (first,
                       f"{writer}() writes {fields} while {other}() uses "
                       f"{'it' if len(shared) == 1 else 'them'} on another thread, with no "
                       f"lock held",
                       f"Both are started as threads (lines {targets[writer]} and "
                       f"{targets[other]}), so {other}() can run halfway through {writer}()'s "
                       f"update and see a mix of old and new values. Therac-25: a quick edit "
                       f"was half seen by the setup task, and patients received about 100 "
                       f"times the intended dose.")
                break                                   # one finding per writer is enough
