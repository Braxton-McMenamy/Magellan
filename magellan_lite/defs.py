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
class Params:
    """What a function accepts, to check a call site against it."""
    positional: tuple[str, ...]          # positional parameters, in order (self included)
    defaults: int                        # how many of the last positional ones have defaults
    positional_only: int                 # how many lead the list before a `/`
    varargs: bool                        # *args
    keyword_only: tuple[str, ...]
    keyword_required: tuple[str, ...]    # keyword-only parameters without a default
    varkw: bool                          # **kwargs
    binds_first: bool                    # a method (not a staticmethod): `self` is passed for you

    def bound(self) -> "Params":
        """As seen through ``obj.method(...)``: the first parameter is filled already."""
        if not self.binds_first or not self.positional:
            return self
        return Params(self.positional[1:], min(self.defaults, len(self.positional) - 1),
                      max(self.positional_only - 1, 0), self.varargs, self.keyword_only,
                      self.keyword_required, self.varkw, False)

    def problems(self, n_positional: int, keywords: tuple[str, ...]) -> list[str]:
        """Why a call with these arguments raises TypeError; empty when it fits."""
        out: list[str] = []
        if not self.varargs and n_positional > len(self.positional):
            out.append(f"takes at most {len(self.positional)} positional argument(s), the call "
                       f"passes {n_positional}")
        required = self.positional[:len(self.positional) - self.defaults]
        missing = [p for i, p in enumerate(required)
                   if i >= n_positional and (i < self.positional_only or p not in keywords)]
        missing += [k for k in self.keyword_required if k not in keywords]
        if missing:
            out.append(f"requires {', '.join(missing)}, which the call does not pass")
        accepted = set(self.positional[self.positional_only:]) | set(self.keyword_only)
        unknown = [k for k in keywords if k not in accepted]
        if unknown and not self.varkw:
            out.append(f"has no parameter named {', '.join(unknown)}")
        return out


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
    params: Params | None = None     # callables: what a call must pass
    lang: str = ""          # "" for Python; otherwise the language (languages.py)
    label: str = ""         # how a person reads the name, when it isn't the last dotted part

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


def _params(fn, kind: str) -> Params:
    a = fn.args
    decorators = {d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
                  for d in fn.decorator_list}
    kw_required = tuple(arg.arg for arg, default in zip(a.kwonlyargs, a.kw_defaults)
                        if default is None)
    return Params(
        positional=tuple(arg.arg for arg in a.posonlyargs + a.args),
        defaults=len(a.defaults), positional_only=len(a.posonlyargs),
        varargs=a.vararg is not None, keyword_only=tuple(arg.arg for arg in a.kwonlyargs),
        keyword_required=kw_required, varkw=a.kwarg is not None,
        binds_first=kind == "method" and "staticmethod" not in decorators)


def _function(fn, qual: str, kind: str, path: str) -> Definition:
    return Definition(
        name=qual, kind=kind, path=path, line=fn.lineno, end_line=fn.end_lineno or fn.lineno,
        signature=_signature(fn),
        body=_hash(_dump(fn.decorator_list), _dump(_without_docstring(fn.body))),
        params=_params(fn, kind))


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
    """``{name: Definition}`` for the whole snapshot. A name defined twice keeps the last.
    Python is read here; the other languages by their own frontends (languages.py)."""
    out: dict[str, Definition] = {}
    for path in sorted(snapshot.files):
        tree = snapshot.tree(path) if path.endswith(".py") else None
        if tree is not None:
            for d in definitions_in(tree, path):
                out[d.name] = d
    if any(not p.endswith(".py") for p in snapshot.files):
        from magellan_lite import languages
        out.update(languages.definitions(snapshot))
    return out
