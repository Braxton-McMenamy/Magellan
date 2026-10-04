"""Helpers for the rules ported from the full Magellan. Not a rule: nothing here registers.

They answer the questions several change rules ask: which definitions this change touched,
the old version's call graph (built once per check), where a definition's syntax tree is,
and whether the check is looking at a real project on disk (the command line, the editor) or
at code handed over as text (the website, the local server, tests). Rules that read files
other than source (manifests) or ask git (history) must stay quiet in the second case: there
is no disk and no git behind ``ctx.root`` then, and in the browser no subprocess either.
"""

from __future__ import annotations

import ast
from pathlib import Path

from magellan_lite.source import decode, module_name

#: change kinds that leave a definition in the new version
_LIVE = ("added", "body", "signature", "value", "renamed")


def edited(ctx) -> set[str]:
    """Names of the definitions this change added or edited, as the new version names them."""
    return {c.name for c in ctx.changes if c.kind in _LIVE and c.after is not None}


def changed_files(ctx, suffix: str = ".py") -> set[str]:
    """Files (ending in ``suffix``) whose text differs between the two versions, both ways."""
    b, a = ctx.before.files, ctx.after.files
    return {p for p in set(b) | set(a)
            if p.endswith(suffix) and _norm(b.get(p)) != _norm(a.get(p))}


def _norm(text: str | None) -> str | None:
    return None if text is None else text.replace("\r\n", "\n")


def old_graph(ctx):
    """The previous version's call graph, built once per check whichever rule asks first."""
    graph = getattr(ctx, "_lite_old_graph", None)
    if graph is None:
        from magellan_lite.graph import build_graph
        graph = build_graph(ctx.before, ctx.before_defs)
        ctx._lite_old_graph = graph
    return graph


def is_test_path(path: str) -> bool:
    """A test file: under a tests/ or test/ directory, or named test_*.py / *_test.py."""
    parts = path.split("/")
    name = parts[-1]
    return (any(p in ("tests", "test", "testing") for p in parts[:-1])
            or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py")


def callables(tree: ast.Module, path: str):
    """``(definition name, function node, enclosing class name or None)`` for every function
    and method, named as defs.py names them (``pkg.mod.Class.method``)."""
    def visit(body, prefix, cls):
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield f"{prefix}.{stmt.name}", stmt, cls
            elif isinstance(stmt, ast.ClassDef):
                qual = f"{prefix}.{stmt.name}"
                yield from visit(stmt.body, qual, qual)
    yield from visit(tree.body, module_name(path), None)


def functions(snapshot, paths=None) -> dict[str, tuple[str, ast.AST, str | None]]:
    """``{definition name: (path, function node, class name or None)}`` for Python files."""
    out: dict[str, tuple[str, ast.AST, str | None]] = {}
    for path in sorted(paths if paths is not None else snapshot.files):
        if not path.endswith(".py") or path not in snapshot.files:
            continue
        tree = snapshot.tree(path)
        if tree is not None:
            for name, fn, cls in callables(tree, path):
                out[name] = (path, fn, cls)
    return out


def dotted(node: ast.AST) -> str:
    """``os.path.join`` for an attribute chain on a name; ``""`` for anything else."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return ""


def call_name(call: ast.Call) -> str:
    """The last part of what a call calls: ``run`` for ``subprocess.run(...)``."""
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def own_nodes(fn: ast.AST):
    """Every node in a function's body, without going into nested functions, lambdas or
    classes (they run later, if at all)."""
    todo = list(getattr(fn, "body", []))
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                 ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(node))


def names_in(an: ast.AST, *, store: bool = False) -> set[str]:
    """Names an expression or statement reads (``store``: assigns)."""
    want = ast.Store if store else ast.Load
    return {n.id for n in ast.walk(an) if isinstance(n, ast.Name) and isinstance(n.ctx, want)}


def under_import_guard(tree: ast.Module) -> set[int]:
    """Line numbers of imports that may fail on purpose: inside ``try`` blocks that catch
    ImportError (or anything broader), and under ``if TYPE_CHECKING:``."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(_catches_import_error(h) for h in node.handlers):
            for stmt in node.body:
                lines |= {n.lineno for n in ast.walk(stmt)
                          if isinstance(n, (ast.Import, ast.ImportFrom))}
        elif isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            for stmt in node.body:
                lines |= {n.lineno for n in ast.walk(stmt)
                          if isinstance(n, (ast.Import, ast.ImportFrom))}
    return lines


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(dotted(n).rsplit(".", 1)[-1] in ("ImportError", "ModuleNotFoundError",
                                               "Exception", "BaseException") for n in names)


# -- the project on disk, and git ----------------------------------------------------------
def on_disk(ctx) -> Path | None:
    """``ctx.root`` when the new version is the project as it is on disk there: what
    ``magellan-lite check`` and the editor look at. ``None`` for code handed over as text
    (the website's Try it, the local server, the team check's combinations, tests that use
    ``check_files``): nothing on disk belongs to it."""
    label = getattr(ctx.after, "label", "")
    return ctx.root if label and label == str(ctx.root) and ctx.root.is_dir() else None


def baseline(ctx) -> tuple[str, str | Path] | None:
    """Where the old version came from: ``("git", rev)``, ``("dir", path)``, or None when it
    was handed over as text. Only meaningful when ``on_disk(ctx)`` is not None."""
    label = getattr(ctx.before, "label", "") or ""
    if label.startswith("git "):
        return "git", label[4:]
    p = Path(label)
    if label and p.is_absolute() and p.is_dir():
        return "dir", p
    return None


def git(root: Path, *args: str, timeout: float = 10.0) -> str | None:
    """git's output decoded as UTF-8, or None when git is missing, slow or says no."""
    import subprocess                               # only here: the website's copy has no git
    try:
        r = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=root,
                           capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return decode(r.stdout) if r.returncode == 0 else None
