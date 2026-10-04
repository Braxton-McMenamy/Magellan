"""Structural hashing: how Magellan decides that something actually changed.

Three hashes per node, each answering a different question:

``sig_hash``
    The contract. For a callable: name, parameters, defaults, annotations,
    decorators, async-ness. For a class: name, bases, keywords. For a variable:
    its annotation. A change here can break callers who were never edited.

``body_hash``
    The implementation, *excluding* anything that is itself a node. A method's
    body change must not also mark its class as changed, or every report would
    drown in duplicates. Containers therefore hash a "shell": their own
    statements, with nested defs collapsed to a name placeholder.

``doc_hash``
    Docstrings, hashed separately so a documentation edit is visible but scores
    as cosmetic.

All hashes are computed from ``ast.dump`` with ``include_attributes=False``, so
moving code up or down a file, or reformatting it, changes nothing.
"""

from __future__ import annotations

import ast
import hashlib

_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _dump(value: object) -> str:
    """Canonical text for an AST value, stubbing any nested definition.

    Equivalent in spirit to ``ast.dump(annotate_fields=True,
    include_attributes=False)``, but it stubs nested ``def``/``class`` bodies as
    it goes instead of deep-copying the tree first and mutating the copy. The
    copy was costing six million ``deepcopy`` calls on a 65k-line project --
    about 85% of total analysis time -- to produce a string that is thrown away
    after being hashed.

    A nested definition becomes ``<def name>``, so adding, removing or
    reordering members still changes the container's hash while editing a
    member's insides does not.
    """
    if isinstance(value, ast.AST):
        if isinstance(value, _DEF_TYPES):
            kind = "class" if isinstance(value, ast.ClassDef) else "def"
            return f"<{kind} {value.name}>"
        parts = []
        for field in value._fields:
            try:
                sub = getattr(value, field)
            except AttributeError:
                continue
            parts.append(f"{field}={_dump(sub)}")
        return f"{type(value).__name__}({', '.join(parts)})"
    if isinstance(value, list):
        return "[" + ", ".join(_dump(v) for v in value) + "]"
    return repr(value)


def strip_docstring(body: list[ast.stmt]) -> tuple[list[ast.stmt], str | None]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:], body[0].value.value
    return body, None


def body_hash(node: ast.AST) -> str:
    """Shell hash for a module/class/function: own statements, nested defs elided."""
    body = list(getattr(node, "body", []))
    body, _ = strip_docstring(body)
    return _h("\n".join(_dump(s) for s in body))


def doc_hash(node: ast.AST) -> str:
    body = list(getattr(node, "body", []))
    _, doc = strip_docstring(body)
    return _h(doc) if doc else ""


def signature_text(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Human-readable signature, also used as the signature hash input."""
    a = fn.args
    parts: list[str] = []

    def one(arg: ast.arg, default: ast.expr | None = None) -> str:
        s = arg.arg
        if arg.annotation is not None:
            s += f": {ast.unparse(arg.annotation)}"
        if default is not None:
            s += f" = {ast.unparse(default)}"
        return s

    pos = list(a.posonlyargs) + list(a.args)
    defaults: list[ast.expr | None] = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    for i, arg in enumerate(pos):
        parts.append(one(arg, defaults[i]))
        if a.posonlyargs and i == len(a.posonlyargs) - 1:
            parts.append("/")
    if a.vararg is not None:
        parts.append("*" + one(a.vararg))
    elif a.kwonlyargs:
        parts.append("*")
    for arg, kd in zip(a.kwonlyargs, a.kw_defaults):
        parts.append(one(arg, kd))
    if a.kwarg is not None:
        parts.append("**" + one(a.kwarg))
    ret = f" -> {ast.unparse(fn.returns)}" if fn.returns is not None else ""
    prefix = "async def " if isinstance(fn, ast.AsyncFunctionDef) else "def "
    return f"{prefix}{fn.name}({', '.join(parts)}){ret}"


def sig_hash_function(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    decs = sorted(ast.unparse(d) for d in fn.decorator_list)
    return _h(signature_text(fn) + "|" + ";".join(decs))


def sig_hash_class(cls: ast.ClassDef) -> str:
    bases = [ast.unparse(b) for b in cls.bases]
    kws = sorted(f"{k.arg}={ast.unparse(k.value)}" for k in cls.keywords)
    decs = sorted(ast.unparse(d) for d in cls.decorator_list)
    return _h(f"class {cls.name}({', '.join(bases)}|{';'.join(kws)})|{';'.join(decs)}")


def sig_hash_text(text: str) -> str:
    return _h(text)


def arity(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, object]:
    """Call-compatibility facts, used to judge how breaking a signature change is."""
    a = fn.args
    pos = list(a.posonlyargs) + list(a.args)
    required_pos = len(pos) - len(a.defaults)
    required_kw = [k.arg for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
    return {
        "positional_only": [x.arg for x in a.posonlyargs],
        "positional": [x.arg for x in pos],
        "required_positional": required_pos,
        "keyword_only": [x.arg for x in a.kwonlyargs],
        "required_keyword_only": sorted(required_kw),
        "star_args": a.vararg is not None,
        "star_kwargs": a.kwarg is not None,
        "defaults": [ast.unparse(d) for d in a.defaults],
    }


def file_hash(source: str) -> str:
    # line endings are not a change: git on Windows checks out CRLF but `git show` gives LF
    source = source.replace("\r\n", "\n")
    return hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()[:16]
