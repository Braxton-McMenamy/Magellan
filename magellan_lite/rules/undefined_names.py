"""undefined-name: a name the change left undefined -- the NameError waiting for its line.

From the full Magellan (``magellan/python/rules/names.py``). The typical case is not a typo
but a removal: an import deleted while code three screens down still uses it, a module
renamed (``import httplib`` -> ``import http.client``) with ``httplib.HTTPS`` left behind, a
helper renamed in one place only. Nothing points at the old name any more, so a call graph
cannot see it; it has to be read off the source.

Scopes come from the standard library's ``symtable``, which applies Python's own rules
(class bodies are not visible to their methods, comprehensions have their own scope,
``global`` and ``nonlocal``). A name is undefined when some scope reads it as a global and
nothing defines it: not the module, not a builtin. Only names undefined after the change and
not before are reported, so old problems stay a linter's business, and a file whose names
cannot be known (``from x import *``, ``globals()[...] = ...``, ``exec``) is left alone.
"""

from __future__ import annotations

import ast
import builtins
import symtable

from magellan_lite.findings import Finding, change_rule
from magellan_lite.rules.common import changed_files
from magellan_lite.source import module_name

#: names a module (or a class body) always has at run time
_IMPLICIT = frozenset({
    "__name__", "__file__", "__doc__", "__package__", "__spec__", "__loader__",
    "__builtins__", "__path__", "__annotations__", "__dict__", "__debug__", "__cached__",
    "__module__", "__qualname__", "__class__",
})
#: builtins of other Pythons and platforms than this one, and gettext's installed `_`
_ELSEWHERE = frozenset({
    "WindowsError", "ExceptionGroup", "BaseExceptionGroup", "EncodingWarning", "aiter",
    "anext", "PythonFinalizationError", "_IncompleteInputError", "reveal_type", "_",
})
_KNOWN = frozenset(dir(builtins)) | _IMPLICIT | _ELSEWHERE
#: calls that can define module names nothing in the source spells out
_DYNAMIC = frozenset({"globals", "exec", "execfile", "vars", "locals", "__import__"})


def undefined_names(source: str, path: str = "<module>") -> dict[str, list[int]] | None:
    """``{name: [lines that read it]}`` for globals nothing defines, or None when that cannot
    be known (it does not parse as Python 3, a star import, names made at run time)."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return None                     # Python 2, or broken: not this rule's business
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            return None                 # a star import can supply anything
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _DYNAMIC:
            return None
    try:
        top = symtable.symtable(source, path, "exec")
    except (SyntaxError, ValueError):
        return None
    defined: set[str] = set()
    read: set[str] = set()

    def walk(table) -> None:
        module = table.get_type() == "module"
        for sym in table.get_symbols():
            name = sym.get_name()
            if module and (sym.is_assigned() or sym.is_imported() or sym.is_namespace()):
                defined.add(name)
            if sym.is_declared_global() and sym.is_assigned():
                defined.add(name)                 # global x; x = ... inside a function
            if sym.is_referenced() and (module or (sym.is_global() and not sym.is_local())):
                read.add(name)
        for child in table.get_children():
            walk(child)

    walk(top)
    missing = read - defined - _KNOWN
    if not missing:
        return {}
    annotations_only = _future_annotations(tree)
    in_annotation = _annotation_names(tree) if annotations_only else set()
    lines: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in missing \
                and id(node) not in in_annotation:
            lines.setdefault(node.id, []).append(node.lineno)
    return {n: sorted(set(ls)) for n, ls in lines.items()}


def _future_annotations(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
               and any(a.name == "annotations" for a in n.names) for n in tree.body)


def _annotation_names(tree: ast.Module) -> set[int]:
    """ids of the Name nodes inside annotations, which ``from __future__ import annotations``
    never evaluates."""
    out: set[int] = set()
    for node in ast.walk(tree):
        anns = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            anns += [x.annotation for x in [*a.posonlyargs, *a.args, *a.kwonlyargs,
                                            a.vararg, a.kwarg] if x is not None]
            anns.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            anns.append(node.annotation)
        for ann in anns:
            if ann is not None:
                out |= {id(n) for n in ast.walk(ann) if isinstance(n, ast.Name)}
    return out


def _runs_at_import(tree: ast.Module, lines: list[int]) -> list[int]:
    """The lines among ``lines`` outside every def and lambda: they run on import."""
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            inside.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return [ln for ln in lines if ln not in inside]


def _bound_names(source: str) -> set[str]:
    """Every name the old version imported, defined or assigned somewhere."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
    return out


def _reported_elsewhere(ctx) -> set[tuple[str, int]]:
    """Lines removed-still-referenced already reports (a call to a deleted function): one
    finding per line is enough."""
    out: set[tuple[str, int]] = set()
    for c in ctx.changes:
        if c.kind in ("removed", "renamed") and c.before is not None \
                and c.before.name not in ctx.after_defs:
            out |= {(e.path, e.line) for e in ctx.graph.callers(c.before.name)}
    return out


@change_rule("undefined-name", "critical", blocking=True,
             fix="Import or define the name again, or update the code that still uses it.")
def undefined_name(ctx):
    skip = None
    for path in sorted(changed_files(ctx)):
        after = ctx.after.files.get(path)
        if after is None:
            continue
        now = undefined_names(after, path)
        if not now:
            continue
        before = ctx.before.files.get(path)
        was = undefined_names(before, path) if before is not None else {}
        if was is None:
            continue                                # unknowable before: no comparison
        if skip is None:
            skip = _reported_elsewhere(ctx)
        tree = ast.parse(after)
        for name, lines in sorted(now.items()):
            if name in was:
                continue                            # already undefined: not this change
            lines = [ln for ln in lines if (path, ln) not in skip]
            if not lines:
                continue
            at_import = _runs_at_import(tree, lines)
            had = before is not None and name in _bound_names(before)
            where = (f"line {lines[0]}" if len(lines) == 1
                     else f"lines {', '.join(map(str, lines[:6]))}"
                     + (" and more" if len(lines) > 6 else ""))
            yield Finding(
                "undefined-name", "critical" if at_import else "high",
                f"{name} is used in {module_name(path)} but nothing defines it"
                + (" any more" if had else ""),
                path, lines[0],
                detail=(f"Read at {where}. "
                        + (f"Line {at_import[0]} runs when the module is imported, so the "
                           f"import itself fails with NameError. " if at_import else
                           "The first call that reaches it raises NameError. ")
                        + (f"The previous version defined or imported {name}; this change "
                           f"removed or renamed it." if had else
                           "No import, assignment or definition in the module provides it, "
                           "and it is not a builtin.")))
