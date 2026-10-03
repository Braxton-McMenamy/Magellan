"""The map: every function, method, class and constant in a snapshot, from its syntax tree.

Each definition gets a stable name (``pkg.mod.Class.method``, no line numbers) and hashes of
what it promises and what it does, so comparing two versions finds real changes: moving code,
reformatting it, editing a docstring or switching line endings changes nothing.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass

from magellan_lite.source import Snapshot, module_name

CALLABLE_KINDS = ("function", "method")


@dataclass(frozen=True)
class Definition:
    name: str               # pkg.mod.Class.method: stable across moves and edits
    kind: str               # function | method | class | constant
    path: str
    line: int
    end_line: int
    signature: str = ""     # callables: parameters and return type; classes: bases
    body: str = ""          # hash of the body's syntax tree
    value: str = ""         # constants: the value's source

    @property
    def short(self) -> str:
        return self.name.rsplit(".", 1)[-1]


def _hash(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()[:12]


def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _dump(nodes: list[ast.AST]) -> str:
    return "\n".join(ast.dump(n, include_attributes=False) for n in nodes)


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    sig = f"({ast.unparse(fn.args)})"
    if fn.returns is not None:
        sig += f" -> {ast.unparse(fn.returns)}"
    return ("async " if isinstance(fn, ast.AsyncFunctionDef) else "") + sig


def _function(fn, qual: str, kind: str, path: str) -> Definition:
    return Definition(
        name=qual, kind=kind, path=path, line=fn.lineno, end_line=fn.end_lineno or fn.lineno,
        signature=_signature(fn),
        body=_hash(_dump(fn.decorator_list), _dump(_without_docstring(fn.body))))


def _constants(stmt: ast.stmt, prefix: str, path: str) -> list[Definition]:
    """``NAME = value`` / ``NAME: T = value`` at module or class level."""
    if isinstance(stmt, ast.Assign):
        targets, value = stmt.targets, stmt.value
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        targets, value = [stmt.target], stmt.value
    else:
        return []
    text = ast.unparse(value)
    return [Definition(name=f"{prefix}.{t.id}", kind="constant", path=path, line=stmt.lineno,
                       end_line=stmt.end_lineno or stmt.lineno, body=_hash(text), value=text)
            for t in targets if isinstance(t, ast.Name)]


def _class(cls: ast.ClassDef, qual: str, path: str) -> list[Definition]:
    members = [s for s in cls.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
    out = [Definition(
        name=qual, kind="class", path=path, line=cls.lineno, end_line=cls.end_lineno or cls.lineno,
        signature=", ".join(ast.unparse(b) for b in cls.bases),
        body=_hash(_dump(cls.decorator_list), _dump(_without_docstring(members))))]
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(_function(stmt, f"{qual}.{stmt.name}", "method", path))
        elif isinstance(stmt, ast.ClassDef):
            out += _class(stmt, f"{qual}.{stmt.name}", path)
        else:
            out += _constants(stmt, qual, path)
    return out


def definitions_in(tree: ast.Module, path: str) -> list[Definition]:
    mod = module_name(path)
    out: list[Definition] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(_function(stmt, f"{mod}.{stmt.name}", "function", path))
        elif isinstance(stmt, ast.ClassDef):
            out += _class(stmt, f"{mod}.{stmt.name}", path)
        else:
            out += _constants(stmt, mod, path)
    return out


def definitions(snapshot: Snapshot) -> dict[str, Definition]:
    """``{name: Definition}`` for the whole snapshot. A name defined twice keeps the last."""
    out: dict[str, Definition] = {}
    for path in sorted(snapshot.files):
        tree = snapshot.tree(path)
        if tree is not None:
            for d in definitions_in(tree, path):
                out[d.name] = d
    return out
