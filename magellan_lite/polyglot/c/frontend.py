"""C frontend: ``.c`` and ``.h`` files into the shared graph.

Two backends produce the same :class:`magellan_lite.polyglot.c.extract.Extraction`:

* **libclang** through a hand-written ctypes binding (:mod:`magellan_lite.polyglot.c.clang`), used
  when a ``libclang`` shared library is found (``MAGELLAN_LIBCLANG`` or the usual
  LLVM locations). Names are resolved by the compiler, types are canonical, struct
  sizes and field offsets are real. Flags come from ``compile_commands.json``.
* a **lexical parser** in pure Python (:mod:`magellan_lite.polyglot.c.lexical`), used otherwise.
  It reads declarations and resolves calls by name (static in the same file first,
  then extern). A diagnostic says which backend ran.

``available()`` is always true because the lexical path needs nothing; it never raises.

Mapping onto the model
----------------------
==========================  =================================================
C                           Magellan
==========================  =================================================
``.c``/``.h`` pair          MODULE ``mod:c@src.util`` (both files; same stem)
function                    FUNCTION ``fn:c@src.util.parse``
``static`` function         FUNCTION in the module of its own file (file-scoped)
prototype only              FUNCTION tagged ``declared-only`` (``abi_import``)
function-like macro         FUNCTION tagged ``macro``
object-like macro           GLOBAL tagged ``macro`` (a constant)
global variable             GLOBAL ``var:c@src.util.counter``
struct / union / typedef    CLASS ``cls:c@src.util.point`` (tag ``struct``...)
struct field                CLASS_ATTR ``attr:c@src.util.point.x``
enum                        CLASS tagged ``enum``
enumerator                  CLASS_ATTR of the enum, ``meta["value"]`` its value
``#include "x.h"``          IMPORTS module -> module
call                        CALLS (``meta["args"]`` = argument count, ``col``)
call through a pointer      CALLS ``dynamic=True``, confidence 0.5, to every
                            address-taken function of the same type (<= 8)
macro use                   CALLS (function-like) or READS (object-like)
global read / assignment    READS / WRITES
``g.f = x``, ``g[i] = x``,  MUTATES the global (``&g`` escaping: MUTATES at 0.5,
``*gp = x``                 ``dynamic``); the field is WRITES
type use (params, locals,   ANNOTATES function -> struct/typedef/enum
casts, fields)
function address taken      READS (``meta["address_taken"]``)
==========================  =================================================

Anything inside ``#if``/``#ifdef`` (not the include guard) gives ``conditional``
edges and a ``conditional`` tag on nodes. The header prototype is the contract: a
definition in ``util.c`` and its prototype in ``util.h`` are one node whose
``sig_hash`` comes from the prototype. Every non-static function carries
``meta["abi_exports"] = [symbol]``, so interop links ``Java_*`` JNI exports and C
symbols to other languages.

Hashes: ``sig_hash`` covers what callers compile against (canonical parameter and
return types, variadic, linkage; a struct's field names, types and size; a macro's
parameter list). ``body_hash`` is the token stream of the body with comments and
whitespace removed. ``doc_hash`` is the comment block above the declaration.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from magellan_lite.polyglot.c.extract import (SKIP_DIRS, Decl, Extraction, h, leading_comment,
                                macro_call_args, module_of, norm_hash, normalize_type,
                                source_files, strip_comments,
                                identity_macros, blank_identity_calls)
from magellan_lite.polyglot.core.hashing import file_hash
from magellan_lite.polyglot.core.model import Edge, EdgeKind, FileRecord, Graph, Node, NodeKind

SOURCE_SUFFIXES = (".c", ".h")
CONFIG_FILES = ("compile_commands.json", "compile_flags.txt")
LANG = "c"

_REF_KINDS = {"calls": EdgeKind.CALLS, "reads": EdgeKind.READS, "writes": EdgeKind.WRITES,
              "mutates": EdgeKind.MUTATES, "annotates": EdgeKind.ANNOTATES}
_EXIT = {"exit", "abort", "_exit", "_Exit", "quick_exit"}
_IO = {"fopen", "open", "socket", "connect", "fread", "fwrite", "read", "write", "recv", "send",
       "fgets", "fputs", "printf", "fprintf", "puts", "opendir", "popen", "freopen", "getline",
       "accept", "bind", "listen", "sendto", "recvfrom", "mmap"}
MAX_DYNAMIC_TARGETS = 8


# --------------------------------------------------------------------------
# frontend contract
# --------------------------------------------------------------------------
def has_sources(root: str | Path) -> bool:
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        if any(n.endswith(".c") for n in names):
            return True
    return False


def backend() -> str:
    """``"libclang"`` or ``"lexical"``: which parser :func:`merge` will use."""
    try:
        if os.environ.get("MAGELLAN_C_BACKEND") == "lexical":
            return "lexical"
        from magellan_lite.polyglot.c import clang
        return "libclang" if clang.load() is not None else "lexical"
    except Exception:
        return "lexical"


def available() -> bool:
    """Always usable: the lexical parser needs nothing. Never raises."""
    return True


def extract(root: str | Path, files: list[str] | None = None) -> Extraction:
    root = str(root)
    files = files if files is not None else source_files(root)
    if backend() == "libclang":
        try:
            from magellan_lite.polyglot.c import clang, clang_extract
            lc = clang.load()
            if lc is not None:
                return clang_extract.extract(root, lc, files)
        except Exception as exc:                 # a binding failure must not lose the language
            ex = _lexical(root, files)
            ex.diagnostics.append(f"c: libclang failed ({exc!r}); used the lexical parser")
            return ex
    return _lexical(root, files)


def _lexical(root: str, files: list[str]) -> Extraction:
    from magellan_lite.polyglot.c import lexical
    ex = lexical.extract(root, files)
    ex.diagnostics.append("c: libclang not found (set MAGELLAN_LIBCLANG to the path of "
                          "libclang.so, libclang.dylib or libclang.dll); "
                          "used the lexical parser: names resolved by spelling, "
                          "no struct sizes, macros not expanded")
    return ex


def merge(state, root: str | Path) -> set[str]:
    graph: Graph = state.graph
    if not has_sources(root):
        return set()
    try:
        ex = extract(root)
    except Exception as exc:                     # degrade, don't fail
        graph.diagnostics.append(f"c: extraction failed ({exc!r}); C sources skipped")
        return set()
    added = assemble(graph, ex, str(root))
    _signals(state, graph, added)
    return added


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------
class _Index:
    """Key -> node ids, with the rule for picking one when C allows several."""

    def __init__(self) -> None:
        self.by_key: dict[str, list[tuple[str, str, str]]] = {}   # key -> [(id, path, tu)]

    def add(self, key: str, nid: str, path: str, tu: str) -> None:
        lst = self.by_key.setdefault(key, [])
        if all(x[0] != nid for x in lst):
            lst.append((nid, path, tu))

    def resolve(self, key: str, path: str = "", tu: str = "") -> tuple[str | None, float]:
        """The node a reference from ``path`` (in translation unit ``tu``) reaches.

        Several nodes share a key when two programs in one tree each define ``main``,
        or two ``.c`` files each define their own ``struct node``: prefer the one in
        the same file, then the same translation unit, then a header.
        """
        cands = self.by_key.get(key)
        if not cands:
            return None, 0.0
        if len(cands) == 1:
            return cands[0][0], 1.0
        for test in (lambda c: c[1] == path, lambda c: tu and c[2] == tu,
                     lambda c: tu and c[1] == tu, lambda c: c[1].endswith(".h")):
            hit = [c for c in cands if test(c)]
            if len(hit) == 1:
                return hit[0][0], 1.0
        return sorted(cands)[0][0], 0.5

    def resolve_all(self, key: str, path: str = "", tu: str = "") -> list[tuple[str, float]]:
        """Like :meth:`resolve`, but an unresolved choice returns every candidate.

        Several programs in one tree each defining ``setUp`` or ``main``: which one a
        call binds to is decided by the link line, not the source, so each is a
        possible target at low confidence (none past :data:`MAX_DYNAMIC_TARGETS`).
        """
        one, conf = self.resolve(key, path, tu)
        if one is None or conf == 1.0:
            return [(one, conf)] if one else []
        cands = sorted(c[0] for c in self.by_key.get(key, ()))
        if len(cands) > MAX_DYNAMIC_TARGETS:
            return []
        return [(c, 0.5 if len(cands) == 2 else 0.3) for c in cands]


def _group(decls: list[Decl]) -> dict[str, list[Decl]]:
    out: dict[str, list[Decl]] = {}
    seen: set[tuple] = set()
    for d in decls:
        k = (d.key, d.path, d.line, d.is_def)
        if k in seen:
            continue
        seen.add(k)
        out.setdefault(d.key, []).append(d)
    return out


def _body(text: str, d: Decl) -> str:
    """The part of a definition after its signature (``{`` onward for a function)."""
    src = text[d.start:d.end] if d.end > d.start else ""
    if d.kind == "function":
        i = src.find("{")
        return src[i:] if i >= 0 else ""
    return src


def _sig_text(d: Decl) -> str:
    if d.kind == "macro":
        ps = [p for p, _ in d.params] + (["..."] if d.variadic else [])
        return f"#define {d.name}({', '.join(ps)})" if d.function_like else f"#define {d.name}"
    ps = [f"{t} {n}".strip() if n else t for n, t in d.params]
    if d.variadic:
        ps.append("...")
    if not ps and not d.noproto:
        ps = ["void"]
    return f"{'static ' if d.static else ''}{d.ret} {d.name}({', '.join(ps)})".strip()


def _enum_names(decls: list[Decl]) -> frozenset[str]:
    """Names that denote an enum type when spelled alone: tags and typedefs of enums.

    ``typedef enum { ... } err_t;`` is spelled ``err_t`` even in canonical form, and an
    enum converts implicitly to and from any integer type; without knowing ``err_t`` is
    an enum, ``int -> err_t`` read as an incompatible type.
    """
    out = {d.name for d in decls if d.kind == "enum"}
    out |= {d.name for d in decls if d.kind == "typedef" and d.value.startswith("enum ")}
    return frozenset(out)


def _enum_type(t: str, enums: frozenset[str]) -> str:
    """``err_t`` -> ``enum err_t`` (qualifiers kept) when ``err_t`` names an enum."""
    words = t.split()
    bare = [w for w in words if w not in ("const", "volatile")]
    if len(bare) == 1 and bare[0] in enums:
        return " ".join(words[:-1] + ["enum", words[-1]]) if words[-1] == bare[0] else t
    return t


def _arity(d: Decl, enums: frozenset[str] = frozenset()) -> dict:
    names = [n or f"arg{i}" for i, (n, _t) in enumerate(d.params)]
    types = list(d.ptypes) if d.ptypes else [normalize_type(t) for _n, t in d.params]
    return {"lang": LANG, "positional": names, "required_positional": len(names),
            "keyword_only": [], "required_keyword_only": [], "star_args": d.variadic,
            "star_kwargs": False, "defaults": [],
            "types": [_enum_type(t, enums) for t in types],
            "ret": _enum_type(normalize_type(d.ret), enums), "static": d.static,
            "noproto": d.noproto, "macro": d.kind == "macro"}


def _contract(group: list[Decl], def_: Decl | None) -> Decl:
    """The declaration callers compile against: a header prototype if there is one."""
    protos = [d for d in group if not d.is_def]
    for d in protos:
        if d.path.endswith(".h"):
            return d
    if def_ is not None:
        if def_.noproto and not def_.params:
            # `local void f()` after `local void f OF((void));`: the prototype is the
            # contract; with none, a definition's `()` takes no parameters, as `(void)`
            # does (and C23 says so), so a K&R -> ANSI rewrite is not a signature change
            typed = [d for d in protos if not d.noproto]
            if typed:
                return typed[0]
            def_.noproto = False
        return def_
    return protos[0] if protos else group[0]


def assemble(graph: Graph, ex: Extraction, root: str) -> set[str]:
    added: set[str] = set()
    index = _Index()
    # hash what the compiler sees: an identity macro around a prototype is not a change
    identity = identity_macros(ex.files.values())
    if identity:
        ex.files = {rel: blank_identity_calls(t, identity) for rel, t in ex.files.items()}
    texts = ex.files
    modules: dict[str, str] = {}
    conditional_files: set[str] = set()

    def add(node: Node) -> Node:
        got = graph.add_node(node)
        if got is node:
            added.add(node.id)
        return got

    def contains(parent: str, child: Node) -> None:
        if parent in graph.nodes and parent != child.id:
            graph.add_edge(Edge(src=parent, dst=child.id, kind=EdgeKind.CONTAINS,
                                lineno=child.lineno, path=child.path))

    # files and modules
    for rel in sorted(texts):
        mod = module_of(rel)
        text = texts[rel]
        graph.files[rel] = FileRecord(path=rel, module=mod, sha256=file_hash(text),
                                      lines=text.count("\n") + 1, source_root="")
        mid = f"mod:{mod}"
        # the set of headers included, however spelled: `#  include` inside an #if and a
        # second `#include <stdio.h>` change nothing (zlib's K&R rewrite did both)
        incs = " ".join(sorted({re.sub(r"\s+", " ", re.sub(r"^#\s*include\s*", "#include ",
                                                             ln.strip().split("//")[0]
                                                             .split("/*")[0].strip()))
                                for ln in text.splitlines()
                                if ln.lstrip().startswith("#") and "include" in ln}))
        if mid not in graph.nodes:
            add(Node(id=mid, kind=NodeKind.MODULE, name=mod.rsplit(".", 1)[-1], qualname=mod,
                     module=mod, path=rel, lineno=1, end_lineno=text.count("\n") + 1,
                     body_hash=h(incs), meta={"lang": LANG, "files": [rel]}))
        else:
            m = graph.nodes[mid]
            m.meta.setdefault("files", []).append(rel)
            m.body_hash = h(m.body_hash, incs)
            if rel.endswith(".c"):
                m.path = rel
        modules[rel] = mid

    groups = _group(ex.decls)
    enums = _enum_names(ex.decls)
    parents: dict[str, list[tuple[str, str]]] = {}     # container key -> [(node id, qualname)]

    # containers and top-level declarations first, then members
    order = {"struct": 0, "union": 0, "enum": 0, "typedef": 1, "function": 2, "global": 2,
             "macro": 2, "field": 3, "enumerator": 3}
    for key, group in sorted(groups.items(), key=lambda kv: (order.get(kv[1][0].kind, 9), kv[0])):
        kind = group[0].kind
        if kind in ("field", "enumerator"):
            continue
        defs = [d for d in group if d.is_def]
        if kind == "function":
            targets = _dedup_paths(defs) or [None]
            for def_ in targets:
                c = _contract(group, def_)
                home = def_ or c
                mod = module_of(home.path)
                qn = f"{mod}.{home.name}"
                text = texts.get(home.path, "")
                meta = {"lang": LANG, "arity": _arity(c, enums), "declared_in": sorted(
                    {d.path for d in group if not d.is_def})}
                if c is not def_ and def_ is not None and c.noproto and def_.params:
                    # callers compile against an unprototyped `int f();`, but the definition
                    # (K&R or ANSI) says what it takes: what a port has to keep
                    meta["definition_arity"] = _arity(def_, enums)
                tags = ["function"]
                if home.static:
                    tags.append("static")
                else:
                    meta["symbol"] = home.name          # callers link by symbol, not by file
                if def_ is None:
                    tags.append("declared-only")
                    if not c.static:
                        meta["abi_import"] = c.name
                elif not home.static:
                    meta["abi_exports"] = [home.name]
                    if home.name.startswith("Java_"):
                        tags.append("jni")
                if home.conditional:
                    tags.append("conditional")
                local = ex.local_names.get((key, home.path)) if def_ is not None else None
                if local:
                    # the name a parameter or local shadows is not the file-scope one
                    meta["local_names"] = sorted(n for n in local if n)
                if def_ is not None and c is not def_ and _canon_sig(c) != _canon_sig(def_):
                    meta["prototype_mismatch"] = {"prototype": f"{c.path}:{c.line}",
                                                  "definition": f"{def_.path}:{def_.line}"}
                node = add(Node(
                    id=f"fn:{qn}", kind=NodeKind.FUNCTION, name=home.name, qualname=qn,
                    module=mod, path=home.path, lineno=home.line, end_lineno=home.end_line,
                    signature=_sig_text(c), public=not home.static, tags=sorted(tags),
                    sig_hash=h("fn", _canon_sig(c), c.static),
                    body_hash=norm_hash(_body(text, home)) if def_ else "",
                    doc_hash=norm_hash(leading_comment(texts.get(c.path, ""), c.start)),
                    meta=meta))
                contains(modules.get(home.path, ""), node)
                index.add(key, node.id, home.path, home.tu)
        elif kind == "macro":
            for d in _dedup_paths(group):
                mod = module_of(d.path)
                qn = f"{mod}.{d.name}"
                text = texts.get(d.path, "")
                tags = ["macro"] + (["conditional"] if d.conditional else [])
                if d.function_like:
                    node = add(Node(
                        id=f"fn:{qn}", kind=NodeKind.FUNCTION, name=d.name, qualname=qn,
                        module=mod, path=d.path, lineno=d.line, end_lineno=d.end_line,
                        signature=_sig_text(d), tags=sorted(tags + ["function-like"]),
                        sig_hash=h("macro", [p for p, _ in d.params], d.variadic),
                        body_hash=norm_hash(d.value) or h("empty"),
                        doc_hash=norm_hash(leading_comment(text, d.start)),
                        meta={"lang": LANG, "arity": _arity(d), "value": d.value}))
                else:
                    node = add(Node(
                        id=f"var:{qn}", kind=NodeKind.GLOBAL, name=d.name, qualname=qn,
                        module=mod, path=d.path, lineno=d.line, end_lineno=d.end_line,
                        signature=_sig_text(d), tags=sorted(tags + ["constant"]),
                        sig_hash=h("macro-obj"), body_hash=norm_hash(d.value) or h("empty"),
                        doc_hash=norm_hash(leading_comment(text, d.start)),
                        meta={"lang": LANG, "value": d.value, "annotation": "#define",
                              "constant": True}))
                contains(modules.get(d.path, ""), node)
                index.add(key, node.id, d.path, d.tu)
        elif kind in ("struct", "union", "enum", "typedef"):
            for d in _dedup_paths(defs or group):
                mod = module_of(d.path)
                qn = f"{mod}.{d.name}"
                text = texts.get(d.path, "")
                meta = {"lang": LANG, "c_kind": kind}
                if kind in ("struct", "union"):
                    meta["layout"] = d.value.split("; ") if d.value else []
                    if d.size >= 0:
                        meta["size"] = d.size
                    sig = h(kind, d.value, d.size)
                elif kind == "enum":
                    sig = h(kind, d.value)
                    meta["enum_members"] = [x.split("=")[0] for x in d.value.split(", ") if x]
                else:
                    sig = h(kind, d.value)
                    meta["target"] = d.type
                    if d.fnptr:
                        meta["fnptr"] = d.fnptr
                tags = [kind] + (["conditional"] if d.conditional else [])
                node = add(Node(
                    id=f"cls:{qn}", kind=NodeKind.CLASS, name=d.name, qualname=qn, module=mod,
                    path=d.path, lineno=d.line, end_lineno=d.end_line,
                    signature=f"{kind} {d.name}" if kind != "typedef" else f"typedef {d.type} {d.name}",
                    bases=[d.type] if kind == "typedef" and d.type else [],
                    tags=sorted(tags), sig_hash=sig,
                    # a plain typedef's body is the type it names (canonical: macro
                    # spelling such as zlib's OF((...)) does not count); records keep text
                    body_hash=(h("typedef", d.value) if kind == "typedef" and d.value else
                               norm_hash(text[d.start:d.end]) if d.end > d.start else ""),
                    doc_hash=norm_hash(leading_comment(text, d.start)), meta=meta))
                contains(modules.get(d.path, ""), node)
                index.add(key, node.id, d.path, d.tu)
                parents.setdefault(key, []).append((node.id, node.qualname))
        elif kind == "global":
            for d in _dedup_paths(defs) or [_contract(group, None)]:
                mod = module_of(d.path)
                qn = f"{mod}.{d.name}"
                text = texts.get(d.path, "")
                tags = (["static"] if d.static else []) + (["const"] if d.const else []) + \
                       (["conditional"] if d.conditional else []) + \
                       ([] if d.is_def else ["declared-only"])
                meta = {"lang": LANG, "annotation": d.type, "value": d.value,
                        "constant": d.const}
                if d.fnptr:
                    meta["fnptr"] = d.fnptr
                if not d.static:
                    meta["symbol"] = d.name
                    if d.is_def:
                        meta["abi_exports"] = [d.name]
                node = add(Node(
                    id=f"var:{qn}", kind=NodeKind.GLOBAL, name=d.name, qualname=qn, module=mod,
                    path=d.path, lineno=d.line, end_lineno=d.end_line,
                    signature=f"{d.type} {d.name}", public=not d.static, tags=sorted(tags),
                    sig_hash=h("var", normalize_type(d.type), d.static),
                    body_hash=norm_hash(d.value), meta=meta,
                    doc_hash=norm_hash(leading_comment(text, d.start))))
                contains(modules.get(d.path, ""), node)
                index.add(key, node.id, d.path, d.tu)

    # members: fields and enumerators, under each node of their container
    for key, group in sorted(groups.items()):
        d = group[0]
        if d.kind not in ("field", "enumerator"):
            continue
        for pid, pq in parents.get(d.parent, ()):
            owner = graph.nodes.get(pid)
            if owner is None:
                continue
            m = next((x for x in group if x.path == owner.path), d)
            qn = f"{pq}.{m.name}"
            if m.kind == "field":
                meta = {"lang": LANG, "annotation": m.type, "offset": m.offset,
                        "per_instance": True}
                if m.fnptr:
                    meta["fnptr"] = m.fnptr
                node = add(Node(
                    id=f"attr:{qn}", kind=NodeKind.CLASS_ATTR, name=m.name, qualname=qn,
                    module=owner.module, path=m.path, lineno=m.line, end_lineno=m.end_line,
                    parent=pid, signature=f"{m.type} {m.name}", tags=["field"],
                    sig_hash=h("field", m.value or normalize_type(m.type)), meta=meta,
                    doc_hash=norm_hash(leading_comment(texts.get(m.path, ""), m.start))))
            else:
                node = add(Node(
                    id=f"attr:{qn}", kind=NodeKind.CLASS_ATTR, name=m.name, qualname=qn,
                    module=owner.module, path=m.path, lineno=m.line, end_lineno=m.end_line,
                    parent=pid, signature=f"{m.name} = {m.value}", tags=["enumerator"],
                    body_hash=h("enumerator", m.value),
                    meta={"lang": LANG, "value": m.value, "constant": True}))
            contains(pid, node)
            index.add(key, node.id, m.path, m.tu)

    _header_api(graph, added, ex.files)
    _edges(graph, ex, index, modules, added)
    _macro_refs(graph, ex, index, modules)
    graph.diagnostics.append(
        f"c: {ex.backend}: {len(texts)} files, {len({d.key for d in ex.decls})} declarations")
    graph.diagnostics.extend(ex.diagnostics[:20])
    if len(ex.diagnostics) > 20:
        graph.diagnostics.append(f"c: ... {len(ex.diagnostics) - 20} more diagnostics")
    return added


def _header_api(graph: Graph, added: set[str], texts: dict[str, str]) -> None:
    """A module with a header exports what the header declares: its ``__all__``.

    ``cJSON_GetArraySize`` lost its only in-tree caller and was "newly orphaned" (high,
    a block); a function a public header declares is called from outside the tree.
    "Declares" is read from the header text, so a prototype in an inactive ``#if``
    (lz4hc.h's ``LZ4_HC_STATIC_LINKING_ONLY`` section, which libclang does not see) or
    behind a header macro counts. Only modules with something declared in a header get
    the list; in the others every extern stays public.
    """
    in_headers: set[str] = set()
    for rel, text in texts.items():
        if rel.endswith(".h"):
            in_headers.update(_IDENT.findall(_STRING.sub('""', strip_comments(text))))
    api: dict[str, set[str]] = {}
    for nid in added:
        n = graph.nodes.get(nid)
        if n is None or n.meta.get("lang") != LANG or n.kind is NodeKind.MODULE \
                or n.kind is NodeKind.CLASS_ATTR or "static" in n.tags or not n.path:
            continue
        if n.path.endswith(".h") or n.name in in_headers:
            api.setdefault(f"mod:{n.module}", set()).add(n.name)
    for mid, names in api.items():
        mod = graph.nodes.get(mid)
        if mod is not None:
            mod.meta["__all__"] = sorted(names)


def _dedup_paths(decls: list[Decl]) -> list[Decl]:
    """One declaration per file (the first); several in one file are #if alternatives."""
    out: dict[str, Decl] = {}
    for d in decls:
        out.setdefault(d.path, d)
    return list(out.values())


def _canon_sig(d: Decl) -> tuple:
    types = tuple(d.ptypes) if d.ptypes else tuple(normalize_type(t) for _n, t in d.params)
    return (normalize_type(d.ret), types, d.variadic, d.noproto)


def _external(graph: Graph, added: set[str], name: str) -> str:
    eid = f"ext:c@{name}"
    if eid not in graph.nodes:
        graph.add_node(Node(id=eid, kind=NodeKind.EXTERNAL, name=name, qualname=f"c@{name}",
                            module="", meta={"lang": LANG}))
        added.add(eid)
    return eid


def _edges(graph: Graph, ex: Extraction, index: _Index, modules: dict[str, str],
           added: set[str]) -> None:
    for src, dst, line in ex.includes:
        a, b = modules.get(src), modules.get(dst)
        if a and b and a != b:
            graph.add_edge(Edge(src=a, dst=b, kind=EdgeKind.IMPORTS, lineno=line, path=src))

    # function-pointer targets by canonical type
    by_type: dict[str, list[str]] = {}
    for key, fntype in ex.address_taken.items():
        nid, _ = index.resolve(key)
        if nid and fntype:
            by_type.setdefault(fntype, []).append(nid)

    for r in ex.refs:
        src, _ = index.resolve(r.src, r.path, r.tu)
        if src is None:
            src = modules.get(r.path)
        if src is None or src not in graph.nodes:
            continue
        ctx = graph.nodes[src].qualname
        if r.kind == "macro":
            dst, conf = index.resolve(r.dst, r.path, r.tu)
            if dst is None:
                continue
            is_fn = graph.nodes[dst].kind is NodeKind.FUNCTION
            meta = {"macro": True}
            if is_fn and r.args is not None:
                meta["args"] = r.args
            graph.add_edge(Edge(src=src, dst=dst, kind=EdgeKind.CALLS if is_fn else EdgeKind.READS,
                                lineno=r.line, col=r.col, path=r.path, confidence=conf,
                                conditional=r.conditional, context=ctx, meta=meta))
            continue
        kind = _REF_KINDS.get(r.kind)
        if kind is None:
            continue
        meta = dict(r.meta)
        if r.args is not None:
            meta["args"] = r.args
        if r.kind == "calls" and r.dynamic and not r.dst:
            targets = by_type.get(r.meta.get("fntype", ""), [])
            if not targets or len(targets) > MAX_DYNAMIC_TARGETS:
                continue
            conf = min(r.confidence, 0.5 if len(targets) == 1 else 0.3)
            # The call names a pointer, not the function: deleting a candidate breaks
            # the code that took its address (a READS edge), never this call.
            meta["indirect"] = True
            for t in targets:
                graph.add_edge(Edge(src=src, dst=t, kind=kind, lineno=r.line, col=r.col,
                                    path=r.path, confidence=conf, conditional=r.conditional,
                                    dynamic=True, context=ctx, meta=meta))
            continue
        if not r.dst:
            targets = []
        elif r.kind == "calls":
            targets = index.resolve_all(r.dst, r.path, r.tu)
        else:
            one = index.resolve(r.dst, r.path, r.tu)
            targets = [one] if one[0] else []
        if not targets and r.ext and r.kind == "calls":
            # Declared outside the project, or not at all (a generated header such as
            # json-c's json.h is missing, so the call is an implicit declaration): the
            # linker still binds it to the project's extern definition of that symbol.
            targets = index.resolve_all(f"c:@F@{r.ext}", r.path, r.tu) \
                or [(_external(graph, added, r.ext), 1.0)]
        if not targets:
            continue
        for dst, conf in targets:
            graph.add_edge(Edge(src=src, dst=dst, kind=kind, lineno=r.line, col=r.col,
                                path=r.path, confidence=min(conf, r.confidence),
                                conditional=r.conditional, dynamic=r.dynamic, context=ctx,
                                meta=meta))

    # switch statements over enums: recorded on the function for the C rules
    for s in ex.switches:
        fid, _ = index.resolve(s.fn, s.path)
        eid, _ = index.resolve(s.enum, s.path)
        if fid is None or eid is None or fid not in graph.nodes:
            continue
        graph.nodes[fid].meta.setdefault("switches", []).append({
            "enum": eid, "line": s.line, "covered": s.covered, "default": s.default,
            "subject": s.subject, "path": s.path})


# --------------------------------------------------------------------------
# macros, read from the text (both backends)
# --------------------------------------------------------------------------
_IDENT = re.compile(r"[A-Za-z_]\w*")
_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')


def _macro_refs(graph: Graph, ex: Extraction, index: _Index, modules: dict[str, str]) -> None:
    """Edges a compiler's AST cannot show: what macros use, and macros used outside code.

    * a ``#define`` body names functions, other macros and globals: edges from the
      macro, so a function called only through ``ASSERT(...)`` still has its caller,
      and removing it while the macro still names it is caught;
    * a macro named in a struct, union, enum or typedef (``char buf[MAX_LEN];``) is
      used by that type; in ``#if`` conditions or other file-scope declarations
      (``API_EXPORT int f(void);``), by the module;
    * a macro named inside a function body the backend did not report (inside an
      ``#if`` there, or by the lexical parser) is used by that function.
    """
    macros: dict[str, list[tuple[str, str]]] = {}
    defines: dict[str, list[tuple[int, int, str]]] = {}
    for d in ex.decls:
        if d.kind != "macro":
            continue
        nid, _ = index.resolve(d.key, d.path)
        if nid and nid in graph.nodes:
            cands = macros.setdefault(d.name, [])
            if (nid, d.path) not in cands:       # #if alternatives in one file: one node
                cands.append((nid, d.path))
            defines.setdefault(d.path, []).append((d.line, d.end_line, nid))
    if not macros:
        return
    named: dict[str, list[tuple[str, str]]] = {}
    ranges: dict[str, list[tuple[int, int, str]]] = {}
    for n in graph.nodes.values():
        if n.meta.get("lang") != LANG or not n.path or "macro" in n.tags:
            continue
        if n.kind in (NodeKind.FUNCTION, NodeKind.GLOBAL):
            named.setdefault(n.name, []).append((n.id, n.path))
        # A type's own node, not the module, holds what its declaration names: the
        # module's hashes cover only its #includes, so a module "reading" a removed
        # macro looked unchanged, and the reference survived its own deletion.
        if n.kind in (NodeKind.FUNCTION, NodeKind.GLOBAL, NodeKind.CLASS) and n.body_hash:
            ranges.setdefault(n.path, []).append((n.lineno, n.end_lineno or n.lineno, n.id))
    closure = _include_closure(ex)

    def pick(cands: list[tuple[str, str]], path: str) -> tuple[str, float]:
        if len(cands) == 1:
            return cands[0][0], 1.0
        for test in (lambda c: c[1] == path, lambda c: c[1] in closure.get(path, ())):
            hit = [c for c in cands if test(c)]
            if len(hit) == 1:
                return hit[0][0], 1.0
        return sorted(cands)[0][0], 0.5

    def visible(cands: list[tuple[str, str]] | None, path: str) -> list[tuple[str, str]]:
        """A ``static`` definition is only visible in its own file and files including it.

        inih's ``#define HANDLER(...) handler(...)`` names a parameter; it used to reach
        a static ``handler`` in an unrelated example program.
        """
        return [c for c in cands or () if "static" not in graph.nodes[c[0]].tags
                or c[1] == path or c[1] in closure.get(path, ())]

    have = {(e.src, e.dst) for e in graph.edges if e.kind in (EdgeKind.CALLS, EdgeKind.READS)}

    def edge(src: str, dst: str, rel: str, line: int, col: int, text: str, conf: float) -> None:
        if src == dst or (src, dst) in have:
            return
        tgt = graph.nodes[dst]
        name = _IDENT.match(text, col)
        call = tgt.kind is NodeKind.FUNCTION and name is not None and \
            text[name.end():].lstrip().startswith("(")
        meta = {"macro": True}
        if call:
            args = macro_call_args(text, col)
            if args is not None:
                meta["args"] = args
        have.add((src, dst))
        graph.add_edge(Edge(src=src, dst=dst, kind=EdgeKind.CALLS if call else EdgeKind.READS,
                            lineno=line, col=col + 1, path=rel, confidence=conf,
                            context=graph.nodes[src].qualname, meta=meta,
                            dynamic=False, conditional=False))

    for rel, text in ex.files.items():
        lines = _STRING.sub('""', strip_comments(text)).split("\n")
        mod = modules.get(rel)
        definer_at = _line_owner(defines.get(rel, ()), first_wins=True)
        holder_at = _line_owner(ranges.get(rel, ()), first_wins=False)
        for i, ln in enumerate(lines, 1):
            definer = definer_at.get(i)
            if definer is not None:
                dnode = graph.nodes[definer]
                skip = {dnode.name, "define", "defined", "__VA_ARGS__"} | set(
                    dnode.meta.get("arity", {}).get("positional", []))
                for m in _IDENT.finditer(ln):
                    w = m.group()
                    if w in skip or w in _C_WORDS:
                        continue
                    cands = macros.get(w) or visible(named.get(w), rel)
                    if cands:
                        dst, conf = pick(cands, rel)
                        edge(definer, dst, rel, i, m.start(), ln, conf)
                continue
            if "#" not in ln and not any(w in macros for w in _IDENT.findall(ln)):
                continue
            holder = holder_at.get(i, mod)
            if holder is None:
                continue
            for m in _IDENT.finditer(ln):
                w = m.group()
                if w in macros:
                    dst, conf = pick(macros[w], rel)
                    edge(holder, dst, rel, i, m.start(), ln, conf)


def _line_owner(spans, first_wins: bool) -> dict[int, str]:
    """``{line: id}`` for ``(first, last, id)`` spans, in one pass instead of a scan per line.

    ``first_wins``: the first span listed that covers a line owns it. Otherwise the
    smallest span does (the innermost declaration), ties going to the first listed.
    """
    order = list(spans) if first_wins else sorted(spans, key=lambda r: r[1] - r[0])
    out: dict[int, str] = {}
    for a, b, nid in reversed(order):          # later assignments win: walk back to front
        for i in range(a, b + 1):
            out[i] = nid
    return out


_C_WORDS = frozenset("""if else for while do switch case default return break continue goto
sizeof typedef struct union enum const volatile static extern inline int char short long
float double void signed unsigned""".split())


def _include_closure(ex: Extraction) -> dict[str, set[str]]:
    direct: dict[str, set[str]] = {}
    for a, b, _ in ex.includes:
        direct.setdefault(a, set()).add(b)
    out: dict[str, set[str]] = {}
    for f in ex.files:
        seen, todo = set(), list(direct.get(f, ()))
        while todo:
            x = todo.pop()
            if x not in seen:
                seen.add(x)
                todo.extend(direct.get(x, ()))
        out[f] = seen
    return out


# --------------------------------------------------------------------------
# labelling signals
# --------------------------------------------------------------------------
def _signals(state, graph: Graph, added: set[str]) -> None:
    for nid in added:
        node = graph.nodes.get(nid)
        if node is None or node.kind is NodeKind.EXTERNAL:
            continue
        module_parts = tuple(node.module.removeprefix("c@").split("."))
        path_parts = tuple(node.path.split("/")) if node.path else ()
        if any(p.lower().startswith("test") for p in path_parts):
            path_parts += ("tests",)
        sig = state.sig(nid)
        sig.module_parts, sig.path_parts = module_parts, path_parts
        if node.kind is NodeKind.MODULE:
            sig.has_main_guard = f"fn:{node.qualname}.main" in graph.nodes
            # a module with code in it is ordinary (domain) code unless something says
            # otherwise; without a size, every C module labelled "unknown"
            sig.own_statement_count = sum(1 for e in graph.out_edges(nid)
                                          if e.kind is EdgeKind.CONTAINS
                                          and e.dst in graph.nodes
                                          and graph.nodes[e.dst].kind.is_callable)
            continue
        if node.kind.is_callable:
            edges = graph.out_edges(nid)
            names = {graph.nodes[e.dst].name for e in edges
                     if e.kind is EdgeKind.CALLS and e.dst in graph.nodes}
            ar = node.meta.get("arity", {})
            sig.returns_value = ar.get("ret", "void") not in ("void", "")
            sig.has_params = bool(ar.get("positional"))
            sig.call_count = sum(1 for e in edges if e.kind is EdgeKind.CALLS)
            sig.own_statement_count = max(1, (node.end_lineno or node.lineno) - node.lineno)
            sig.calls_exit = bool(names & _EXIT)
            sig.calls_open = bool(names & _IO)
            for e in edges:
                tgt = graph.nodes.get(e.dst)
                if tgt is None or tgt.kind is not NodeKind.GLOBAL:
                    continue
                if e.kind in (EdgeKind.WRITES, EdgeKind.MUTATES):
                    sig.mutates_state = sig.writes_global = True
                elif e.kind is EdgeKind.READS:
                    sig.reads_global = True
        elif node.kind is NodeKind.GLOBAL:
            sig.is_constant = bool(node.meta.get("constant"))
