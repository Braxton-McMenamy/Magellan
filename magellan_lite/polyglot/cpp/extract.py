"""Out-of-process C++ extractor: libclang ASTs in, plain JSON records out.

Run as ``python -m magellan_lite.polyglot.cpp.extract`` with a job on stdin::

    {"root": "/abs/root", "owned": ["src/a.cpp", ...],
     "tus": [{"file": "/abs/root/src/a.cpp", "args": [...]}, ...],
     "headers": [{"file": ..., "args": [...]}, ...], "jobs": 8}

``tus`` are parsed first; ``headers`` only if no translation unit reached them. The
result on stdout is ``{"decls": [...], "refs": [...], "includes": [...], ...}``, keyed by
clang USRs. :mod:`magellan_lite.polyglot.cpp.frontend` turns USRs into Magellan ids.

It runs in a child process because libclang is native code reached through ctypes: a
crash in it must cost the C++ part of the graph, not the whole analysis. Units are spread
over a process pool, and every project file is walked once, in a unit that does not depend
on the pool's scheduling (see :func:`run`): the output is the same for any worker count.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor

from magellan_lite.polyglot.cpp import libclang as L
from magellan_lite.polyglot.cpp.libclang import K

_ROOT = ""
_OWNED: frozenset[str] = frozenset()
_REL: dict[str, str | None] = {}
_EXT: dict[str, dict | None] = {}
_TEXT: dict[str, list[str]] = {}
_index = None

_COND = frozenset({K.IF_STMT, K.WHILE_STMT, K.DO_STMT, K.FOR_STMT, K.SWITCH_STMT,
                   K.CONDITIONAL_OPERATOR, K.CXX_TRY_STMT, K.CXX_CATCH_STMT,
                   K.CXX_FOR_RANGE_STMT, K.CASE_STMT, K.DEFAULT_STMT})
_CALLABLE = K.FUNCTIONS
_DATA_TARGETS = frozenset({K.VAR_DECL, K.FIELD_DECL, K.ENUM_CONSTANT_DECL})
GROWERS = frozenset({"push_back", "emplace_back", "push_front", "emplace_front", "insert",
                     "emplace", "push", "append", "try_emplace", "insert_or_assign",
                     "emplace_hint", "resize"})
SHRINKERS = frozenset({"pop_back", "pop_front", "pop", "erase", "clear", "remove",
                       "remove_if", "extract", "shrink_to_fit"})
_WS = re.compile(r"\s+")


def norm_type(t: str) -> str:
    """Canonical spacing for a spelled type: ``const std::string &`` -> ``const std::string&``."""
    t = _WS.sub(" ", t).strip()
    t = re.sub(r"\s+([&*])", r"\1", t)
    return re.sub(r"\s*([,<>()\[\]])\s*", r"\1", t)


def short_hash(*parts) -> str:
    """16 hex digits of sha256 over ``parts`` (shared with :mod:`magellan_lite.polyglot.cpp.frontend`)."""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# worker state
# --------------------------------------------------------------------------
def _init(root: str, owned: list[str]) -> None:
    """Per-process setup. Called again for the header batch: the index is reused."""
    global _ROOT, _OWNED, _index
    _ROOT = root
    _OWNED = frozenset(owned)
    if _index is None:
        _index = L.Index()


def rel_of(fname: str) -> str | None:
    if not fname:
        return None
    r = _REL.get(fname, "")
    if r != "":
        return r
    p = os.path.normpath(fname)
    try:
        rel = os.path.relpath(p, _ROOT).replace(os.sep, "/") if os.path.isabs(p) else p
    except ValueError:                  # Windows: a system header on another drive
        rel = None
    out = rel if rel in _OWNED else None
    _REL[fname] = out
    return out


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------
def _record_name(c: L.Cursor) -> str:
    k = c.kind
    if k == K.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION or (
            k in (K.STRUCT_DECL, K.CLASS_DECL, K.UNION_DECL)
            and not c.specialized_template.is_null):
        return norm_type(c.displayname)
    name = c.spelling
    if not name or "(unnamed" in name or "(anonymous" in name or c.is_anonymous:
        return ""
    return name


def scope_of(c: L.Cursor) -> tuple[list[str], bool, bool, L.Cursor | None]:
    """``(scope parts, internal linkage, is local, enclosing record cursor)``.

    Inline namespaces (``fmt::v11``, ``std::__cxx11``) are left out: they version an ABI and
    are invisible in source, so keeping them would rename every node on a version bump.
    """
    parts: list[str] = []
    internal = local = False
    record = None
    p = c.semantic_parent
    first = True
    while not p.is_null and p.kind != K.TRANSLATION_UNIT:
        k = p.kind
        if k == K.NAMESPACE:
            if not p.spelling:
                internal = True
                parts.append("(anonymous)")
            elif not p.is_inline_namespace:
                parts.append(p.spelling)
        elif k in K.RECORDS:
            name = _record_name(p)
            if not name:
                local = True       # members of an unnamed struct: not addressable by name
                break
            parts.append(name)
            if first:
                record = p
        elif k == K.ENUM_DECL:
            parts.append(p.spelling)
        elif k in _CALLABLE or k == K.LAMBDA_EXPR:
            local = True
            break
        elif k == K.LINKAGE_SPEC:
            pass
        else:
            if p.spelling:
                parts.append(p.spelling)
        first = False
        p = p.semantic_parent
    parts.reverse()
    return parts, internal, local, record


def _ext_info(ref: L.Cursor) -> dict | None:
    """Name (and C symbol, if any) of a function outside the project."""
    usr = ref.usr
    if usr in _EXT:
        return _EXT[usr]
    parts, _i, local, _r = scope_of(ref)
    out = None
    if not local:
        out = {"name": ".".join(parts + [ref.spelling])}
        if ref.kind == K.FUNCTION_DECL and _is_c_linkage(ref):
            out["abi"] = ref.spelling
    _EXT[usr] = out
    return out


def _is_c_linkage(c: L.Cursor) -> bool:
    if c.kind != K.FUNCTION_DECL or c.linkage != L.Linkage.EXTERNAL:
        return False
    name = c.spelling
    return bool(name) and name != "main" and c.mangled == name


def _candidates(ref: L.Cursor) -> list[str]:
    """USRs a reference may land on: the entity, then the template it was instantiated from."""
    out = [ref.usr]
    t = ref.specialized_template
    if not t.is_null:
        out.append(t.usr)
        tt = t.specialized_template          # member of a partial specialization
        if not tt.is_null:
            out.append(tt.usr)
    elif ref.kind in (K.FIELD_DECL, K.VAR_DECL):
        # a data member of an instantiation (`Box<int>::v`) names the template's member
        owner = ref.semantic_parent
        if not owner.is_null and owner.kind in K.RECORDS:
            ot = owner.specialized_template
            if not ot.is_null and ot.usr:
                out.append(ot.usr + ("@FI@" if ref.kind == K.FIELD_DECL else "@") + ref.spelling)
    return [u for u in out if u]


# --------------------------------------------------------------------------
# source text
# --------------------------------------------------------------------------
def _lines(path: str) -> list[str]:
    lines = _TEXT.get(path)
    if lines is None:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")
        except OSError:
            lines = []
        _TEXT[path] = lines
    return lines


def _comment_above(path: str, line: int) -> str:
    """The comment lines directly above ``line`` (1-based), stripped.

    Read from the source rather than asked of clang: clang returns the comment of *any*
    redeclaration, and which one depends on what was asked earlier in the same parse, so
    the answer changed with the order of the walk.
    """
    lines = _lines(path)
    i = min(line, len(lines) + 1) - 2
    out: list[str] = []
    while i >= 0:
        t = lines[i].strip()
        if t.startswith("//"):
            out.append(t)
            i -= 1
        elif t.endswith("*/"):
            j = i
            while j >= 0 and "/*" not in lines[j]:
                j -= 1
            if j < 0:
                break
            out.extend(x.strip() for x in reversed(lines[j:i + 1]))
            i = j - 1
        else:
            break
    return "\n".join(reversed(out))


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------
def _after_params(toks: list[tuple[int, str, int, int]], name_line: int,
                  name_col: int) -> int:
    """Index of the first token after the parameter list that follows the name."""
    i = 0
    n = len(toks)
    while i < n and (toks[i][2], toks[i][3]) < (name_line, name_col):
        i += 1
    while i < n and toks[i][1] != "(":
        i += 1
    depth = 0
    while i < n:
        s = toks[i][1]
        if s == "(":
            depth += 1
        elif s == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _default_text(param: L.Cursor) -> str | None:
    if not any(100 <= ch.kind < 200 for ch in param.children()):
        return None
    toks = [t[1] for t in param.tokens()]
    depth = 0
    for i, s in enumerate(toks):
        if s in ("(", "<", "[", "{"):
            depth += 1
        elif s in (")", ">", "]", "}"):
            depth -= 1
        elif s == "=" and depth == 0:
            return " ".join(toks[i + 1:])
    return None


def _init_text(c: L.Cursor) -> str:
    toks = [t[1] for t in c.tokens()]
    for i, s in enumerate(toks):
        if s in ("=", "{") and i:
            return " ".join(toks[i + (s == "="):])[:200]
    return ""


# --------------------------------------------------------------------------
# the walk
# --------------------------------------------------------------------------
class TU:
    """The walk of one parsed unit, limited to the project files in ``walk``."""

    def __init__(self, walk: set[str]) -> None:
        self.walk = walk
        self.decls: list[dict] = []
        self.refs: list[dict] = []
        self.visited: set[str] = set()

    # -- scopes -------------------------------------------------------------
    def scope(self, cursor: L.Cursor) -> None:
        for ch in cursor.children():
            k = ch.kind
            if k not in _WANTED:
                continue
            rel = rel_of(ch.location[0])
            if rel not in self.walk:
                continue
            self.visited.add(rel)
            if k in (K.NAMESPACE, K.LINKAGE_SPEC, K.UNEXPOSED_DECL):
                self.scope(ch)
            elif k in K.RECORDS or k == K.ENUM_DECL:
                self.record(ch, rel)
            elif k in _CALLABLE:
                self.function(ch, rel)
            elif k == K.VAR_DECL:
                self.var(ch, rel)

    def _base(self, c: L.Cursor, rel: str, kind: str, name: str, parts: list[str],
              internal: bool, record: L.Cursor | None) -> dict:
        (_f, line, col, _o) = c.location
        start, end = c.extent
        return {"usr": c.usr, "nk": kind, "name": name, "scope": parts, "internal": internal,
                "file": rel, "line": line, "col": col,
                "start": start[1], "end": end[1],
                "parent": record.usr if record is not None else None,
                "is_def": c.is_definition, "doc": _comment_above(start[0], start[1]),
                "access": {1: "public", 2: "protected", 3: "private"}.get(c.access, "")}

    # -- classes -------------------------------------------------------------
    def record(self, c: L.Cursor, rel: str) -> None:
        name = _record_name(c) if c.kind != K.ENUM_DECL else c.spelling
        if not name or "(unnamed" in name or "(anonymous" in name:
            return
        parts, internal, local, record = scope_of(c)
        if local:
            return
        d = self._base(c, rel, "class", name, parts, internal, record)
        k = c.kind
        tags = {K.STRUCT_DECL: ["struct"], K.UNION_DECL: ["union"], K.CLASS_DECL: [],
                K.CLASS_TEMPLATE: ["template"],
                K.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION: ["template", "specialization"],
                K.ENUM_DECL: ["enum"]}[k]
        if k in (K.STRUCT_DECL, K.CLASS_DECL, K.UNION_DECL) and not c.specialized_template.is_null:
            tags.append("specialization")
            d["template_of"] = c.specialized_template.usr
        if k == K.CLASS_TEMPLATE and any(t[1] == "struct" for t in c.tokens()[:40]):
            tags.append("struct")
        d["tags"] = tags
        if not d["is_def"]:
            self.decls.append(d)
            return
        bases, fields, virtuals, tparams, members = [], [], [], [], []
        if k == K.ENUM_DECL:
            d["scoped"] = c.is_scoped_enum
            d["underlying"] = ""
            for ch in c.children():
                if ch.kind == K.ENUM_CONSTANT_DECL:
                    members.append((ch.spelling, ch.enum_value))
                    self.enumerator(ch, rel, parts + [name], internal, c)
            d["enum_members"] = [m for m, _v in members]
            d["sig"] = ["enum", d["scoped"], name]
            d["body"] = short_hash(members)
            if d["scoped"]:
                tags.append("enum_class")
            self.decls.append(d)
            return
        for ch in c.children():
            ck = ch.kind
            if ck == K.CXX_BASE_SPECIFIER:
                decl = ch.type.declaration
                bases.append({"name": norm_type(ch.type.spelling),
                              "usrs": _candidates(decl) if not decl.is_null else [],
                              "virtual": ch.is_virtual_base,
                              "access": {1: "public", 2: "protected", 3: "private"}.get(ch.access, "")})
            elif ck == K.FIELD_DECL:
                fields.append((ch.spelling, norm_type(ch.type.spelling)))
                self.var(ch, rel, parts + [name], internal, c)
            elif ck == K.VAR_DECL:
                self.var(ch, rel, parts + [name], internal, c)
            elif ck in _CALLABLE:
                if ck in (K.CXX_METHOD, K.DESTRUCTOR, K.FUNCTION_TEMPLATE) and ch.is_virtual:
                    virtuals.append(ch.displayname)
                self.function(ch, rel)
            elif ck in K.RECORDS or ck == K.ENUM_DECL:
                self.record(ch, rel)
            elif ck == K.CXX_FINAL_ATTR:
                tags.append("final")
            elif ck in (K.TEMPLATE_TYPE_PARAMETER, K.NON_TYPE_TEMPLATE_PARAMETER,
                        K.TEMPLATE_TEMPLATE_PARAMETER):
                tparams.append(ch.spelling)
            elif ck == K.FRIEND_DECL:
                for f in ch.children():
                    if f.kind in _CALLABLE and f.is_definition:
                        self.function(f, rel)
        if k != K.UNION_DECL and c.is_abstract:
            tags.append("abstract")
        if virtuals:
            tags.append("polymorphic")
        d["bases"] = bases
        d["template_params"] = tparams
        d["fields"] = fields
        d["sig"] = [k, name, tparams, [(b["name"], b["virtual"], b["access"]) for b in bases],
                    "final" in tags, bool(virtuals)]
        d["body"] = short_hash(fields, virtuals)
        self.decls.append(d)

    def enumerator(self, c: L.Cursor, rel: str, parts: list[str], internal: bool,
                   owner: L.Cursor) -> None:
        d = self._base(c, rel, "class_attr", c.spelling, parts, internal, owner)
        d["value"] = str(c.enum_value)
        d["type"] = parts[-1]
        d["sig"] = ["enumerator"]
        d["body"] = short_hash(c.enum_value)
        d["tags"] = ["enumerator"]
        d["is_def"] = True
        self.decls.append(d)

    # -- data ----------------------------------------------------------------
    def var(self, c: L.Cursor, rel: str, parts: list[str] | None = None,
            internal: bool | None = None, owner: L.Cursor | None = None) -> None:
        if parts is None:
            parts, internal, local, owner = scope_of(c)
            if local:
                return
            if c.storage_class == 3:          # static at namespace scope
                internal = True
        if not c.spelling:
            return
        kind = ("instance_attr" if c.kind == K.FIELD_DECL
                else "class_attr" if owner is not None else "global_var")
        d = self._base(c, rel, kind, c.spelling, parts, bool(internal), owner)
        t = c.type
        d["type"] = norm_type(t.spelling)
        toks = c.tokens()
        words = {s for _k, s, _l, _c in toks[:12]}
        d["const"] = t.is_const or "constexpr" in words
        d["value"] = _init_text(c) if any(100 <= ch.kind < 200 for ch in c.children()) else ""
        d["sig"] = [kind, d["type"], "static" in words]
        d["body"] = short_hash(d["value"])
        d["tags"] = (["const"] if d["const"] else []) + (["static"] if "static" in words else [])
        self.decls.append(d)
        src = owner.usr if owner is not None else "file:" + rel
        if d["value"]:
            self.body(c, src, rel)

    # -- callables -------------------------------------------------------------
    def function(self, c: L.Cursor, rel: str) -> None:
        parts, internal, local, record = scope_of(c)
        if local:
            return
        k = c.kind
        name = c.spelling
        if not name:
            return
        if record is None and c.linkage == L.Linkage.INTERNAL:
            internal = True
        nk = "method" if record is not None else "function"
        d = self._base(c, rel, nk, name, parts, internal, record)
        params = []
        for ch in c.children():
            if ch.kind == K.PARM_DECL:
                params.append({"name": ch.spelling, "type": norm_type(ch.type.spelling),
                               "default": _default_text(ch)})
        tags = []
        tparams = [ch.spelling for ch in c.children()
                   if ch.kind in (K.TEMPLATE_TYPE_PARAMETER, K.NON_TYPE_TEMPLATE_PARAMETER,
                                  K.TEMPLATE_TEMPLATE_PARAMETER)]
        if k == K.FUNCTION_TEMPLATE:
            tags.append("template")
        if k == K.CONSTRUCTOR:
            tags.append("ctor")
        elif k == K.DESTRUCTOR:
            tags.append("dtor")
        if name.startswith("operator") and not name[8:9].isalnum() and name[8:9] != "_":
            tags.append("operator")
        if k in (K.DESTRUCTOR, K.CONVERSION_FUNCTION) or record is not None and (
                k == K.CONSTRUCTOR or name == "operator=") and _copy_or_move(c, record):
            tags.append("called_implicitly")     # by `return x;`, a container, scope exit
        is_method = k in (K.CXX_METHOD, K.CONSTRUCTOR, K.DESTRUCTOR, K.CONVERSION_FUNCTION) \
            or (k == K.FUNCTION_TEMPLATE and record is not None)
        flags = {}
        toks = None
        if is_method:
            flags = {"virtual": c.is_virtual, "pure": c.is_pure_virtual,
                     "static": c.is_static_method, "const": c.is_const_method,
                     "ref": c.type.ref_qualifier, "explicit": c.is_explicit,
                     "deleted": c.is_deleted, "defaulted": c.is_defaulted}
        else:
            toks = c.tokens()
            flags = {"deleted": _deleted_free(toks)}
        for ch in c.children():
            if ch.kind == K.CXX_OVERRIDE_ATTR:
                flags["override"] = True
            elif ch.kind == K.CXX_FINAL_ATTR:
                flags["final"] = True
        es = c.exception_spec
        flags["noexcept"] = ("noexcept" if es in (L.ExceptionSpec.BASIC_NOEXCEPT,
                                                   L.ExceptionSpec.DYNAMIC_NONE,
                                                   L.ExceptionSpec.NOTHROW)
                             else "noexcept(expr)" if es in (L.ExceptionSpec.COMPUTED_NOEXCEPT,
                                                             L.ExceptionSpec.UNINSTANTIATED,
                                                             L.ExceptionSpec.UNEVALUATED)
                             else "")
        for key in ("virtual", "pure", "static", "explicit", "deleted", "override", "final"):
            if flags.get(key):
                tags.append({"pure": "pure_virtual", "static": "staticmethod"}.get(key, key))
        if flags["noexcept"]:
            tags.append("noexcept")
        ret = same = ""
        if k not in (K.CONSTRUCTOR, K.DESTRUCTOR):
            rt = c.result_type
            ret, same = norm_type(rt.spelling), norm_type(rt.canonical.spelling)
        d.update(params=params, ret=ret, ret_canonical=same, flags=flags, tags=tags,
                 template_params=tparams,
                 variadic=c.is_variadic if k != K.FUNCTION_TEMPLATE else False,
                 overridden=[u for o in c.overridden() for u in _candidates(o)[:1]])
        if k == K.FUNCTION_DECL and _is_c_linkage(c):
            d["c_symbol"] = name
        if d["is_def"] or flags.get("deleted") or flags.get("defaulted"):
            toks = c.tokens() if toks is None else toks
            _f, nl, nc, _o = c.location
            cut = _after_params(toks, nl, nc)
            d["body"] = short_hash(" ".join(t[1] for t in toks[cut:] if t[0] != L.TokenKind.COMMENT))
            d["body_tokens"] = len(toks) - cut
        if d["is_def"]:
            d["switches"] = []
            d["locals"] = []
            self.body(c, d["usr"], rel, d)
        self.decls.append(d)

    # -- references --------------------------------------------------------------
    def body(self, root: L.Cursor, src: str, rel: str, fn: dict | None = None) -> None:
        callees: set[tuple] = set()
        lhs: dict[tuple, str] = {}
        # `lam`: inside a lambda. Its calls stay with the enclosing function (it usually
        # runs them), but a `throw` there leaves whoever invokes the lambda, not this
        # function: CLI11's set_help_flag installs `[] { throw CallForHelp(); }`.
        stack = [(ch, False, False) for ch in reversed(root.children())]
        while stack:
            n, cond, lam = stack.pop()
            k = n.kind
            if k == K.CALL_EXPR:
                self.call(n, src, rel, cond, callees)
            elif k == K.UNEXPOSED_EXPR:
                self.recovery(n, src, rel, cond, callees)
            elif k in (K.DECL_REF_EXPR, K.MEMBER_REF_EXPR):
                self.ref(n, src, rel, cond, callees, lhs)
            elif k in (K.BINARY_OPERATOR, K.COMPOUND_ASSIGN_OPERATOR):
                op = n.binary_operator
                if op == "=" or (k == K.COMPOUND_ASSIGN_OPERATOR) or op.endswith("=") \
                        and op not in ("==", "!=", "<=", ">=", "<=>"):
                    kids = n.children()
                    if kids:
                        tgt = _lvalue(kids[0])
                        if tgt is not None:
                            lhs[_key(tgt)] = "writes"
            elif k == K.UNARY_OPERATOR:
                if n.unary_operator_kind in (1, 2, 3, 4):
                    kids = n.children()
                    if kids:
                        tgt = _lvalue(kids[0])
                        if tgt is not None:
                            lhs[_key(tgt)] = "writes"
            elif k == K.CXX_THROW_EXPR and not lam:
                kids = n.children()
                if kids:
                    decl = kids[0].type.canonical.declaration
                    if not decl.is_null and decl.kind in K.RECORDS:
                        self._edge(src, "raises", _candidates(decl), rel, n, cond)
            elif k == K.CXX_CATCH_STMT and not lam:
                kids = n.children()
                if kids and kids[0].kind == K.VAR_DECL:
                    t = kids[0].type
                    if t.kind in (103, 104, 101):
                        t = t.pointee
                    decl = t.canonical.declaration
                    if not decl.is_null and decl.kind in K.RECORDS:
                        self._edge(src, "handles", _candidates(decl), rel, n, cond)
            elif k == K.SWITCH_STMT and fn is not None:
                sw = _switch(n)
                if sw is not None:
                    fn["switches"].append(sw)
            elif k == K.VAR_DECL:
                if fn is not None and n.spelling:
                    fn["locals"].append(n.spelling)
                t = n.type.canonical
                if t.kind == 105:                        # CXType_Record: a value, not a ref
                    decl = t.declaration
                    if not decl.is_null and rel_of(decl.location[0]) is not None:
                        self._edge(src, "instantiates", _candidates(decl), rel, n, cond,
                                   {"instantiation": norm_type(n.type.spelling)}
                                   if not decl.specialized_template.is_null else None)
            sub = cond or k in _COND
            inner = lam or k == K.LAMBDA_EXPR
            kids = n.children()
            for ch in reversed(kids):
                stack.append((ch, sub, inner))

    def _edge(self, src: str, kind: str, dsts: list[str], rel: str, n: L.Cursor,
              cond: bool, meta: dict | None = None, *, conf: float = 1.0,
              dynamic: bool = False, ext: dict | None = None) -> dict:
        _f, line, col, _o = n.location
        r = {"src": src, "kind": kind, "dst": dsts, "file": rel_of(_f) or rel, "line": line,
             "col": col, "cond": cond}
        if meta:
            r["meta"] = meta
        if conf != 1.0:
            r["conf"] = conf
        if dynamic:
            r["dyn"] = True
        if ext:
            r["ext"] = ext
        self.refs.append(r)
        return r

    def call(self, n: L.Cursor, src: str, rel: str, cond: bool, callees: set) -> None:
        ref = n.referenced
        loc = n.location
        if ref.is_null or ref.kind == K.OVERLOADED_DECL_REF:
            cands = _overload_candidates(n)
            if cands:
                callees.add((loc[1], loc[2]))
                args = _explicit_args(n)
                for c in cands:
                    self._edge(src, "calls", _candidates(c), rel, n, cond,
                               {"args": args, "dependent": True}, conf=0.6, dynamic=True)
            return
        if ref.kind not in _CALLABLE:
            return
        callees.add((loc[1], loc[2]))
        kids = n.children()
        if kids:
            head = kids[0]
            while head.kind == K.UNEXPOSED_EXPR and head.children():
                head = head.children()[0]
            hl = head.location
            callees.add((hl[1], hl[2]))
        args = _explicit_args(n)
        name = ref.spelling
        if ref.kind == K.CXX_METHOD and name.startswith("operator") \
                and args == ref.num_arguments + 1:
            args -= 1                                    # `a + b`: the object is not an argument
        meta = {"args": args, "callee": name}
        target_rel = rel_of(ref.location[0])
        if not ref.specialized_template.is_null:
            meta["instantiation"] = norm_type(ref.displayname)
        if n.is_dynamic_call:
            meta["virtual"] = True
        ext = None
        if target_rel is None:
            ext = _ext_info(ref)
            if ext is None:
                return
        if ref.kind == K.CONSTRUCTOR:
            cls = ref.semantic_parent
            if _implicit_conversion(n):
                meta["implicit"] = True
            if target_rel is not None or rel_of(cls.location[0]) is not None:
                if not _copy_or_move(ref, cls):
                    inst = dict(meta)
                    if not cls.specialized_template.is_null:
                        inst["instantiation"] = norm_type(cls.type.spelling)
                    self._edge(src, "instantiates", _candidates(cls), rel, n, cond, inst)
            if target_rel is None:
                return
        self._edge(src, "calls", _candidates(ref), rel, n, cond, meta, ext=ext)
        # `items.push_back(x)` grows the member or global `items`
        if name in GROWERS or name in SHRINKERS:
            kids = n.children()
            if kids and kids[0].kind == K.MEMBER_REF_EXPR:
                obj = kids[0].children()
                tgt = _lvalue(obj[0]) if obj else None
                if tgt is not None:
                    r = tgt.referenced
                    if not r.is_null and r.kind in (K.FIELD_DECL, K.VAR_DECL) \
                            and rel_of(r.location[0]) is not None \
                            and (r.kind == K.FIELD_DECL or r.has_global_storage):
                        self._edge(src, "mutates", [r.usr], rel, tgt, cond,
                                   {"method": name, "grows": name in GROWERS})

    def recovery(self, n: L.Cursor, src: str, rel: str, cond: bool, callees: set) -> None:
        """A call clang could not resolve (no viable overload, too few arguments...).

        Kept as a low-confidence edge to every candidate so a signature change that broke
        this call still finds it: without it a precise resolver hides exactly the calls
        that a change broke.
        """
        kids = n.children()
        if not kids:
            return
        head = kids[0]
        while head.kind == K.UNEXPOSED_EXPR and head.children():
            head = head.children()[0]
        if head.kind not in (K.DECL_REF_EXPR, K.MEMBER_REF_EXPR, K.OVERLOADED_DECL_REF):
            return
        (_s0, _e0) = n.extent
        (_s1, e1) = head.extent
        if (_e0[1], _e0[2]) <= (e1[1], e1[2]):
            return                                  # not call syntax: a plain wrapped reference
        ref = head.referenced
        cands = []
        if not ref.is_null and ref.kind in _CALLABLE:
            cands = [ref]
        elif not ref.is_null and ref.kind == K.OVERLOADED_DECL_REF:
            cands = [c for c in ref.overloaded_decls() if c.kind in _CALLABLE]
        else:
            for ch in head.children():
                if ch.kind == K.OVERLOADED_DECL_REF:
                    cands = [c for c in ch.overloaded_decls() if c.kind in _CALLABLE]
        cands = [c for c in cands if rel_of(c.location[0]) is not None]
        if not cands:
            return
        hl = head.location
        callees.add((hl[1], hl[2]))
        args = sum(1 for ch in kids[1:])
        for c in cands:
            self._edge(src, "calls", _candidates(c), rel, head, cond,
                       {"args": args, "unresolved": True, "callee": c.spelling},
                       conf=0.5, dynamic=True)

    def ref(self, n: L.Cursor, src: str, rel: str, cond: bool, callees: set,
            lhs: dict) -> None:
        r = n.referenced
        if r.is_null:
            return
        rk = r.kind
        loc = n.location
        if rk in _CALLABLE:
            if (loc[1], loc[2]) in callees:
                return
            if rel_of(r.location[0]) is None:
                return
            # a function named but not called here: passed as a callback, address taken
            self._edge(src, "calls", _candidates(r), rel, n, cond, {"ref": True},
                       conf=0.7, dynamic=True)
            return
        if rk not in _DATA_TARGETS:
            return
        if rk == K.VAR_DECL and not r.has_global_storage:
            return
        if rel_of(r.location[0]) is None:
            return
        kind = lhs.pop(_key(n), "reads")
        self._edge(src, kind, _candidates(r), rel, n, cond)


_WANTED = K.RECORDS | K.FUNCTIONS | frozenset({K.NAMESPACE, K.LINKAGE_SPEC, K.UNEXPOSED_DECL,
                                              K.ENUM_DECL, K.VAR_DECL})


def _deleted_free(toks: list[tuple[int, str, int, int]]) -> bool:
    return len(toks) >= 3 and toks[-1][1] == "delete" and toks[-2][1] == "="


def _key(n: L.Cursor) -> tuple:
    f, line, col, _o = n.location
    return (f, line, col, n.spelling)


def _lvalue(n: L.Cursor) -> L.Cursor | None:
    """The DeclRef/MemberRef an assignment target names (through casts and parens)."""
    seen = 0
    while n.kind == K.UNEXPOSED_EXPR or n.kind == 111:        # 111: ParenExpr
        kids = n.children()
        if not kids or seen > 8:
            return None
        n = kids[0]
        seen += 1
    return n if n.kind in (K.DECL_REF_EXPR, K.MEMBER_REF_EXPR) else None


def _explicit_args(n: L.Cursor) -> int:
    """Arguments written at the call: clang also lists the defaults it filled in."""
    return sum(1 for a in n.arguments() if a.location[1] > 0)


def _overload_candidates(n: L.Cursor) -> list[L.Cursor]:
    """The overload set a dependent call names, looked for in its callee only.

    The whole call used to be searched, so ``T{to_string(x)}`` (a dependent construction,
    no callee of its own) took the overload set of the ``to_string`` call in its argument,
    with an argument count of 0.
    """
    kids = n.children()
    stack = kids[:1]
    while stack:
        d = stack.pop()
        if d.kind == K.OVERLOADED_DECL_REF:
            return [c for c in d.overloaded_decls()
                    if c.kind in _CALLABLE and rel_of(c.location[0]) is not None]
        if d.kind not in (K.CALL_EXPR, K.COMPOUND_STMT, K.LAMBDA_EXPR):
            stack.extend(d.children())
    return []


def _implicit_conversion(n: L.Cursor) -> bool:
    """A constructor call with no syntax of its own: an implicit conversion of an argument."""
    args = [a for a in n.arguments() if a.location[1] > 0]
    if len(args) != 1:
        return False
    s0, e0 = n.extent
    s1, e1 = args[0].extent
    return (s0[1], s0[2], e0[1], e0[2]) == (s1[1], s1[2], e1[1], e1[2])


def _copy_or_move(ctor: L.Cursor, cls: L.Cursor) -> bool:
    params = [ch for ch in ctor.children() if ch.kind == K.PARM_DECL]
    if len(params) != 1:
        return False
    t = params[0].type
    if t.kind not in (103, 104):
        return False
    decl = t.pointee.canonical.declaration
    return not decl.is_null and decl.usr == cls.usr


def _switch(n: L.Cursor) -> dict | None:
    kids = n.children()
    if len(kids) < 2:
        return None
    cond = kids[-2]
    decl = cond.type.canonical.declaration
    if decl.is_null or decl.kind != K.ENUM_DECL:
        return None
    covered: set[str] = set()
    default = "none"
    stack = [kids[-1]]
    while stack:
        c = stack.pop()
        k = c.kind
        if k == K.SWITCH_STMT:
            continue
        if k == K.CASE_STMT:
            ck = c.children()
            if ck:
                for d in ck[0].walk():
                    if d.kind == K.DECL_REF_EXPR:
                        r = d.referenced
                        if not r.is_null and r.kind == K.ENUM_CONSTANT_DECL:
                            covered.add(r.spelling)
                            break
        elif k == K.DEFAULT_STMT:
            default = "fallback"
        stack.extend(c.children())
    _f, line, col, _o = n.location
    subject = " ".join(t[1] for t in cond.tokens())[:80]
    return {"enum": decl.usr, "line": line, "col": col, "covered": sorted(covered),
            "default": default, "subject": subject}


# --------------------------------------------------------------------------
# one translation unit
# --------------------------------------------------------------------------
def _shapes(root: L.Cursor, skip: set[str]) -> dict[str, str]:
    """Per project file: a hash of the declarations this unit sees in it.

    Only declarations are visited (namespaces and classes are entered, bodies are not), so
    this is cheap next to the walk. Two units that see different declarations in one header
    (``#if CLI11_HAS_FILESYSTEM``) give it different shapes, and each shape is walked.
    """
    seen: dict[str, list[str]] = {}
    stack = root.children()
    while stack:
        c = stack.pop()
        k = c.kind
        if k not in _WANTED and k not in (K.FIELD_DECL, K.ENUM_CONSTANT_DECL):
            continue
        rel = rel_of(c.location[0])
        if rel is None or rel in skip:
            continue
        seen.setdefault(rel, []).append(f"{k}:{c.usr}")
        if k in (K.NAMESPACE, K.LINKAGE_SPEC, K.UNEXPOSED_DECL, K.ENUM_DECL) or k in K.RECORDS:
            stack.extend(c.children())
    return {rel: short_hash(*sorted(v)) for rel, v in seen.items()}


def run_tu(job: dict) -> dict:
    """Parse one unit and walk the project files in ``job["walk"]``.

    With ``job["closure"]`` the result also lists the project files the unit includes and
    the shape of each (:func:`_shapes`).
    """
    path, args = job["file"], job["args"]
    out = {"file": path, "decls": [], "refs": [], "includes": [], "reached": [], "shapes": {},
           "visited": [], "errors": [], "failed": False}
    try:
        tu = _index.parse(path, args, L.TU_KEEP_GOING)
    except Exception as exc:                   # pragma: no cover - ctypes failure
        out["failed"] = True
        out["errors"] = [f"{path}: {exc}"]
        return out
    if tu is None:
        out["failed"] = True
        out["errors"] = [f"{path}: libclang could not parse it"]
        return out
    try:
        out["errors"] = tu.diagnostics(3)[:5]
        walker = TU(set(job["walk"]))
        walker.scope(tu.cursor)
        if job.get("closure"):
            reached = set()
            for src, dst, line in tu.inclusions():
                a, b = rel_of(src), rel_of(dst)
                if b is not None:
                    reached.add(b)
                    if a is not None:
                        out["includes"].append((a, b, line))
            out["reached"] = sorted(reached)
            out["shapes"] = _shapes(tu.cursor, set(job["walk"]))
        out["decls"] = walker.decls
        out["refs"] = walker.refs
        out["visited"] = sorted(walker.visited)
    finally:
        tu.close()
    return out


def _chunks(files: list[str], n: int) -> list[list[str]]:
    """``files`` in sorted order, cut into at most ``n`` runs of near-equal length.

    By path, not size: an edit that grows one header must not move others to another
    chunk, since libclang's answers (which redeclaration a doc comment comes from) can
    depend on what was asked before in the same parse.
    """
    files = sorted(files)
    n = max(1, min(n, len(files)))
    step, extra = divmod(len(files), n)
    out, at = [], 0
    for i in range(n):
        size = step + (i < extra)
        out.append(files[at:at + size])
        at += size
    return [c for c in out if c]


def run(job: dict) -> dict:
    """Walk every project file exactly once, in a unit chosen independently of scheduling.

    1. Each translation unit is parsed and its own file walked; its include closure is
       recorded.
    2. A header is walked in the *first* unit (in job order) that includes it, and again in
       the first unit of every other shape it has (units whose macros show it different
       declarations): those units are parsed again, their headers split over the workers.
       What a header's code resolves to depends on the unit it is parsed in (which
       overloads are visible to a template, which macros are set), so picking the unit by
       whichever worker got there first made two builds of one tree disagree -- and a
       caller that came and went between them read as a function "newly orphaned".
    3. Headers no unit reached are parsed on their own.
    """
    root = job["root"]
    owned = job["owned"]
    jobs = max(1, int(job.get("jobs") or 1))
    result = {"decls": [], "refs": [], "includes": [], "visited": [], "errors": [],
              "failed": [], "tus": 0, "tu_errors": 0, "version": L.version(),
              "library": L.library_path()}

    def absorb(r: dict, unit: bool = True) -> None:
        result["decls"] += r["decls"]
        result["refs"] += r["refs"]
        result["includes"] += r["includes"]
        result["visited"] += r["visited"]
        if not unit:
            return
        result["tus"] += 1
        if r["errors"]:
            result["tu_errors"] += 1
            if len(result["errors"]) < 20:
                result["errors"] += r["errors"][:2]
        if r["failed"]:
            result["failed"].append(r["file"])

    def rel(path: str) -> str:
        return os.path.relpath(path, root).replace(os.sep, "/")

    pool = None
    if jobs > 1:
        pool = ProcessPoolExecutor(max_workers=jobs, initializer=_init, initargs=(root, owned))
    else:
        _init(root, owned)

    def batch(units: list[dict]) -> list[dict]:
        if pool is None or len(units) <= 1:
            if units and _index is None:
                _init(root, owned)
            return [run_tu(u) for u in units]
        return list(pool.map(run_tu, units, chunksize=1))

    try:
        tus = job.get("tus", [])
        mains = {rel(t["file"]) for t in tus}
        first = batch([dict(t, walk=[rel(t["file"])], closure=True) for t in tus])
        owners: dict[str, dict[str, int]] = {}      # header -> shape -> first unit
        for i, r in enumerate(first):
            absorb(r)
            for b in r["reached"]:
                shape = r["shapes"].get(b)
                if b not in mains and shape is not None:
                    owners.setdefault(b, {}).setdefault(shape, i)
        by_unit: dict[int, list[str]] = {}
        for header, units in owners.items():
            for i in units.values():
                by_unit.setdefault(i, []).append(header)
        # each extra chunk costs a parse of its unit: split only big groups
        per = max(1, jobs // max(1, len(by_unit)))
        second = [dict(tus[i], walk=chunk) for i, headers in sorted(by_unit.items())
                  for chunk in _chunks(headers, min(per, -(-len(headers) // 4)))]
        for r in batch(second):
            absorb(r, unit=False)

        seen = set(result["visited"])
        rest = [h for h in job.get("headers", []) if rel(h["file"]) not in seen]
        for r in batch([dict(h, walk=[rel(h["file"])], closure=True) for h in rest]):
            absorb(r)
    finally:
        if pool is not None:
            pool.shutdown()
    result["visited"] = sorted(set(result["visited"]))
    return result


def main() -> int:
    job = json.load(sys.stdin)
    if L.load() is None:
        print(json.dumps({"error": "libclang not found"}))
        return 2
    json.dump(run(job), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
