"""Name and type resolution for Java: the project index and the body walker.

:class:`Index` knows every type the project declares (top-level, nested, member
types of supertypes), each type's methods, constructors and fields, and each file's
imports. It resolves a type name the way ``javac`` does, in order: type variables,
member types of the enclosing classes and their supertypes, local classes, types of
the same file, single-type imports, the same package, on-demand imports, and
``java.lang``. Fully qualified names resolve directly.

:class:`BodyWalker` walks one method, constructor or initializer and reports what
it references: calls (with overload resolution by argument count and, where the
argument types are known, by type), object creation, field reads and writes,
collection growth, thrown and caught exceptions, type uses, and switch statements
over enums and sealed types (for exhaustiveness). Receivers are typed from
declarations: locals, parameters, fields, ``var`` initializers, casts, method
return types and ``new``. Lambdas, anonymous classes and local classes are folded
into the enclosing callable.

Confidence follows what is known. A call bound by the static types is 1.0; a tie
between overloads is 0.5 for each; a call on a receiver of unknown type is linked
by name only when the name is rare and arity-compatible, at 0.3 and ``dynamic``.
Virtual dispatch to overriding methods is added later by the frontend, at 0.5 (below
the 0.6 recursion-cycle floor).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from magellan_lite.polyglot.java import jdk
from magellan_lite.polyglot.java import parser as P

LANG = "java"
PRIM_WIDEN = {
    "byte": ("short", "int", "long", "float", "double"),
    "short": ("int", "long", "float", "double"),
    "char": ("int", "long", "float", "double"),
    "int": ("long", "float", "double"),
    "long": ("float", "double"),
    "float": ("double",),
    "double": (),
    "boolean": (),
}
BOXES = {"int": "Integer", "long": "Long", "short": "Short", "byte": "Byte", "char": "Character",
         "boolean": "Boolean", "float": "Float", "double": "Double"}
UNBOX = {v: k for k, v in BOXES.items()}
#: method names too common to link by name alone when the receiver's type is unknown
COMMON_NAMES = frozenset("""
get set add put size remove contains equals hashCode toString apply accept test run call close
next hasNext iterator length isEmpty clear append write read compareTo getName getValue getKey
value name of valueOf stream map filter forEach collect build create init start stop handle
execute process parse format print println clone keySet values entrySet addAll getClass toArray
flush reset update load save find list copy type key id getId getType setValue indexOf charAt
substring trim split replace matches join wait notify notifyAll invoke newInstance first last
peek poll offer push pop sort compare max min sum count visit text html attr parent children
""".split())


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------
class JType:
    """A static type as far as we know it."""
    __slots__ = ("kind", "name", "dims", "args", "ti")

    def __init__(self, kind: str, name: str, dims: int = 0, args: tuple = (),
                 ti: "TypeInfo | None" = None) -> None:
        self.kind = kind            # proj ext prim tvar null static pkg pseudo
        self.name = name            # fqn / dotted / primitive name
        self.dims = dims
        self.args = args
        self.ti = ti

    @property
    def simple(self) -> str:
        return self.name.rsplit(".", 1)[-1] + "[]" * self.dims

    def elem(self) -> "JType | None":
        if self.dims:
            return JType(self.kind, self.name, self.dims - 1, self.args, self.ti)
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"JType({self.kind}:{self.name}{'[]' * self.dims})"


STRING = JType("ext", "java.lang.String")
NULL = JType("null", "null")


@dataclass(eq=False)
class FileInfo:
    path: str
    module_qual: str
    module_id: str
    package: str
    cu: P.CompilationUnit
    src: str = ""
    single: dict[str, str] = field(default_factory=dict)          # simple -> dotted
    ondemand: list[str] = field(default_factory=list)
    static_single: dict[str, list[str]] = field(default_factory=dict)
    static_ondemand: list[str] = field(default_factory=list)
    top: dict[str, str] = field(default_factory=dict)             # simple -> fqn
    types: list["TypeInfo"] = field(default_factory=list)


@dataclass(eq=False)
class MethodInfo:
    id: str | None
    name: str
    owner: "TypeInfo"
    decl: P.MethodDecl | None
    params: list[P.TypeRef]
    erased: list[str]
    varargs: bool = False
    static: bool = False
    abstract: bool = False
    default: bool = False
    final: bool = False
    native: bool = False
    ctor: bool = False
    synthetic: bool = False
    visibility: str = "public"
    ret: P.TypeRef | None = None
    tparams: dict[str, P.TypeRef | None] = field(default_factory=dict)
    overrides: list["MethodInfo"] = field(default_factory=list)
    overriders: list["MethodInfo"] = field(default_factory=list)

    @property
    def sig(self) -> str:
        return f"{self.name}({','.join(self.erased)})"


@dataclass(eq=False)
class FieldInfo:
    id: str | None
    name: str
    owner: "TypeInfo"
    type: P.TypeRef | None
    static: bool = False
    final: bool = False
    visibility: str = "public"
    enum_const: bool = False
    decl: Any = None
    line: int = 0


@dataclass(eq=False)
class TypeInfo:
    fqn: str
    id: str | None
    kind: str                                   # class interface enum record annotation
    decl: P.TypeDecl
    file: FileInfo
    outer: "TypeInfo | None" = None
    nested: dict[str, "TypeInfo"] = field(default_factory=dict)
    methods: dict[str, list[MethodInfo]] = field(default_factory=dict)
    ctors: list[MethodInfo] = field(default_factory=list)
    fields: dict[str, FieldInfo] = field(default_factory=dict)
    tparams: dict[str, P.TypeRef | None] = field(default_factory=dict)
    visibility: str = "public"
    static: bool = False
    pseudo: bool = False                        # anonymous or local: folded, no node
    explicit_supers: list[JType] | None = None  # anonymous classes: the type after `new`
    _supers: list[JType] | None = None
    _resolving: bool = False
    # filled once the hierarchy is frozen (Index.freeze): see ancestors / member_type
    _ancestors: list["TypeInfo"] | None = None
    _member_types: dict[str, "TypeInfo | None"] = field(default_factory=dict)

    @property
    def simple(self) -> str:
        return self.decl.name

    @property
    def is_interface(self) -> bool:
        return self.kind in ("interface", "annotation")


def visibility_of(mods: set[str], default: str = "package") -> str:
    for v in ("public", "protected", "private"):
        if v in mods:
            return v
    return default


def erase(ref: P.TypeRef | None, tvars: dict[str, P.TypeRef | None], depth: int = 0) -> str:
    """The erased simple name used in ids: ``List<String>`` -> ``List``, ``T`` -> its bound."""
    if ref is None:
        return "void"
    dims = ref.dims + (1 if ref.varargs else 0)
    base = ref.name
    if base in tvars and "." not in base and depth < 5:
        bound = tvars[base]
        inner = erase(bound, tvars, depth + 1) if bound is not None else "Object"
        return inner + "[]" * dims
    return base.rsplit(".", 1)[-1] + "[]" * dims


# --------------------------------------------------------------------------
# index
# --------------------------------------------------------------------------
class Index:
    def __init__(self) -> None:
        self.files: list[FileInfo] = []
        self.types: dict[str, TypeInfo] = {}
        self.packages: dict[str, dict[str, str]] = {}
        self.methods_by_name: dict[str, list[MethodInfo]] = {}
        self.const_index: dict[str, list[TypeInfo]] = {}
        self.diagnostics: list[str] = []
        self.frozen = False

    def freeze(self) -> None:
        """Resolve every type's supertypes; from here on the hierarchy cannot change, so
        ancestors and member-type lookups are memoized on each TypeInfo."""
        for ti in list(self.types.values()):
            self.supers(ti)
        self.frozen = True

    # ---- registration ----------------------------------------------------
    def add_file(self, fi: FileInfo) -> None:
        self.files.append(fi)
        pkg_types = self.packages.setdefault(fi.package, {})
        for td in fi.cu.types:
            fqn = f"{fi.package}.{td.name}" if fi.package else td.name
            if fqn in self.types:
                self.diagnostics.append(f"java: {fqn} is declared twice ({fi.path} and "
                                        f"{self.types[fqn].file.path}); the second is ignored")
                continue
            ti = self._register(td, fi, fqn, None)
            fi.top[td.name] = fqn
            pkg_types.setdefault(td.name, fqn)
        for imp in fi.cu.imports:
            simple = imp.name.rsplit(".", 1)[-1]
            if imp.static:
                owner = imp.name if imp.wildcard else imp.name.rsplit(".", 1)[0]
                if imp.wildcard:
                    fi.static_ondemand.append(owner)
                else:
                    fi.static_single.setdefault(simple, []).append(owner)
            elif imp.wildcard:
                fi.ondemand.append(imp.name)
            else:
                fi.single[simple] = imp.name

    def _register(self, td: P.TypeDecl, fi: FileInfo, fqn: str, outer: TypeInfo | None,
                  pseudo: bool = False) -> TypeInfo:
        tid = None if pseudo else f"cls:{LANG}@{fqn}"
        in_iface = outer is not None and outer.is_interface
        vis = visibility_of(td.modifiers, "public" if in_iface else "package")
        ti = TypeInfo(fqn=fqn, id=tid, kind=td.kind, decl=td, file=fi, outer=outer,
                      visibility=vis, pseudo=pseudo,
                      static=("static" in td.modifiers or td.kind in ("enum", "record",
                                                                      "interface", "annotation")
                              or in_iface))
        for tp in td.type_params:
            ti.tparams[tp] = None
        if td.type_params_text:
            ti.tparams.update(_bounds(td.type_params_text))
        if not pseudo:
            self.types[fqn] = ti
            fi.types.append(ti)
        for m in td.members:
            if isinstance(m, P.TypeDecl):
                nfqn = f"{fqn}.{m.name}" if fqn else m.name
                ti.nested[m.name] = self._register(m, fi, nfqn, ti, pseudo)
        self._members(ti)
        return ti

    def register_pseudo(self, td: P.TypeDecl, fi: FileInfo, outer: TypeInfo | None,
                        supers: list[JType] | None = None) -> TypeInfo:
        ti = self._register(td, fi, "", outer, pseudo=True)
        ti.explicit_supers = supers
        return ti

    def _all_tvars(self, ti: TypeInfo) -> dict[str, P.TypeRef | None]:
        out: dict[str, P.TypeRef | None] = {}
        chain = []
        cur: TypeInfo | None = ti
        while cur is not None:
            chain.append(cur)
            cur = cur.outer
        for c in reversed(chain):
            out.update(c.tparams)
        return out

    def _members(self, ti: TypeInfo) -> None:
        td = ti.decl
        iface = ti.is_interface
        prefix = ti.fqn
        tv = self._all_tvars(ti)
        explicit_sigs: set[str] = set()
        for m in td.members:
            if isinstance(m, P.MethodDecl):
                mtv = dict(tv)
                mtv.update({t: None for t in m.type_params})
                if m.type_params_text:
                    mtv.update(_bounds(m.type_params_text))
                erased = [erase(p.type, mtv) for p in m.params]
                name = m.name
                mi = MethodInfo(
                    id=None, name=name, owner=ti, decl=m, params=[p.type for p in m.params],
                    erased=erased, varargs=bool(m.params and m.params[-1].varargs),
                    static="static" in m.modifiers,
                    abstract=("abstract" in m.modifiers) or (
                        iface and m.body is None and not m.ctor
                        and not ({"static", "default", "private"} & m.modifiers)),
                    default="default" in m.modifiers, final="final" in m.modifiers,
                    native="native" in m.modifiers, ctor=m.ctor,
                    visibility=visibility_of(m.modifiers, "public" if iface else "package"),
                    ret=m.ret, tparams=mtv)
                if ti.kind == "enum" and m.ctor:
                    mi.visibility = "private"
                if mi.sig in explicit_sigs:
                    # two overloads whose parameter types share a simple name
                    # (org.x.Consumer vs java.util.function.Consumer): the one written
                    # qualified keeps the qualified name in its id
                    mi.erased = [erase(p.type, mtv) if "." not in p.type.name
                                 else p.type.name + "[]" * (p.type.dims + p.varargs)
                                 for p in m.params]
                if not ti.pseudo:
                    mi.id = f"fn:{LANG}@{prefix}.{mi.sig}"
                explicit_sigs.add(mi.sig)
                if m.ctor:
                    ti.ctors.append(mi)
                else:
                    ti.methods.setdefault(name, []).append(mi)
                    if not ti.pseudo:
                        self.methods_by_name.setdefault(name, []).append(mi)
            elif isinstance(m, P.FieldDecl):
                static = "static" in m.modifiers or iface
                for name, dims, _init, line in m.names:
                    ftype = m.type
                    if dims and ftype is not None:
                        ftype = P.TypeRef(ftype.name, ftype.args, ftype.dims + dims)
                    kind = "attr" if static else "iattr"
                    fid = None if ti.pseudo else f"{kind}:{LANG}@{prefix}.{name}"
                    ti.fields[name] = FieldInfo(
                        id=fid, name=name, owner=ti, type=ftype, static=static,
                        final="final" in m.modifiers or iface,
                        visibility=visibility_of(m.modifiers, "public" if iface else "package"),
                        decl=m, line=line)
        if ti.kind == "enum":
            for c in td.constants:
                fid = None if ti.pseudo else f"attr:{LANG}@{prefix}.{c.name}"
                ti.fields[c.name] = FieldInfo(id=fid, name=c.name, owner=ti,
                                              type=P.TypeRef(prefix or td.name), static=True,
                                              final=True, visibility="public", enum_const=True,
                                              decl=c, line=c.line)
                if not ti.pseudo:
                    self.const_index.setdefault(c.name, []).append(ti)
        if not ti.ctors and ti.kind in ("class", "enum") and not ti.pseudo:
            # the implicit default constructor: `new Foo()` binds to it, and declaring it
            # explicitly later must not read as "a constructor appeared"
            vis = "private" if ti.kind == "enum" else (
                ti.visibility if ti.visibility != "package" else "package")
            ti.ctors.append(MethodInfo(id=f"fn:{LANG}@{prefix}.<init>()", name="<init>",
                                       owner=ti, decl=None, params=[], erased=[], ctor=True,
                                       synthetic=True, visibility=vis, tparams=tv))
        if ti.kind == "record":
            comps = td.components
            erased = [erase(p.type, tv) for p in comps]
            canon = f"<init>({','.join(erased)})"
            if canon not in explicit_sigs:
                mi = MethodInfo(id=None if ti.pseudo else f"fn:{LANG}@{prefix}.{canon}",
                                name="<init>", owner=ti, decl=None,
                                params=[p.type for p in comps], erased=erased,
                                varargs=bool(comps and comps[-1].varargs), ctor=True,
                                synthetic=True, visibility=ti.visibility, tparams=tv)
                ti.ctors.append(mi)
            for p in comps:
                acc = f"{p.name}()"
                if acc not in explicit_sigs:
                    mi = MethodInfo(id=None if ti.pseudo else f"fn:{LANG}@{prefix}.{acc}",
                                    name=p.name, owner=ti, decl=None, params=[], erased=[],
                                    synthetic=True, visibility="public", ret=p.type, tparams=tv)
                    ti.methods.setdefault(p.name, []).append(mi)
                    if not ti.pseudo:
                        self.methods_by_name.setdefault(p.name, []).append(mi)
                if p.name not in ti.fields:
                    ti.fields[p.name] = FieldInfo(
                        id=None if ti.pseudo else f"iattr:{LANG}@{prefix}.{p.name}",
                        name=p.name, owner=ti, type=p.type, final=True,
                        visibility="private", decl=p, line=p.line)

    # ---- type names ------------------------------------------------------
    def member_type(self, ti: TypeInfo, simple: str, seen: set[int] | None = None
                    ) -> TypeInfo | None:
        """A member type named ``simple`` declared in ``ti`` or inherited from a supertype."""
        if simple in ti.nested:
            return ti.nested[simple]
        if seen is None and self.frozen:
            if simple not in ti._member_types:
                ti._member_types[simple] = self.member_type(ti, simple, set())
            return ti._member_types[simple]
        seen = seen if seen is not None else set()
        if id(ti) in seen:
            return None
        seen.add(id(ti))
        for s in self.supers(ti):
            sti = self.type_info(s)
            if sti is not None:
                hit = self.member_type(sti, simple, seen)
                if hit is not None:
                    return hit
        return None

    def type_info(self, t: JType | None) -> TypeInfo | None:
        if t is None or t.dims:
            return None
        if t.kind == "proj":
            return self.types.get(t.name)
        if t.kind == "pseudo":
            return t.ti
        if t.kind == "static" and t.ti is not None:
            return t.ti
        return None

    def jtype_of(self, ti: TypeInfo, dims: int = 0, args: tuple = ()) -> JType:
        if ti.pseudo:
            return JType("pseudo", ti.decl.name or "<anonymous>", dims, args, ti)
        return JType("proj", ti.fqn, dims, args, ti)

    def resolve_name(self, name: str, fi: FileInfo, chain: list[TypeInfo],
                     tvars: dict | None = None,
                     local_types: Iterable[dict[str, TypeInfo]] = ()) -> JType | None:
        """Resolve a (possibly dotted) type name as written in ``fi`` inside ``chain``."""
        if name in P.PRIMITIVES:
            return JType("prim", name)
        parts = name.split(".")
        head = parts[0]
        found: JType | None = None
        if tvars and head in tvars and len(parts) == 1:
            return JType("tvar", head)
        for ti in chain:
            if head in ti.tparams and len(parts) == 1:
                return JType("tvar", head)
        for scope in local_types:
            if head in scope:
                found = self.jtype_of(scope[head])
                break
        if found is None:
            for ti in chain:
                if ti.pseudo and ti.decl.name == head and not ti.decl.anonymous:
                    found = self.jtype_of(ti)
                    break
                mt = self.member_type(ti, head)
                if mt is not None:
                    found = self.jtype_of(mt)
                    break
        if found is None:
            found = self._resolve_head(head, fi)
        if found is None:
            # a fully qualified name: the longest prefix naming a type
            for k in range(len(parts), 0, -1):
                cand = ".".join(parts[:k])
                if cand in self.types:
                    found = JType("proj", cand, ti=self.types[cand])
                    parts = [cand] + parts[k:]
                    break
            else:
                if len(parts) > 1 and head[:1].islower():
                    # external fully qualified: java.util.List, org.junit.Test
                    return JType("ext", name)
                return JType("ext", name) if len(parts) > 1 else JType("ext", head)
        for seg in parts[1:]:
            ti = self.type_info(found)
            if ti is not None:
                mt = self.member_type(ti, seg)
                if mt is None:
                    return None
                found = self.jtype_of(mt)
            else:
                found = JType("ext", f"{found.name}.{seg}")
        return found

    def _resolve_head(self, head: str, fi: FileInfo) -> JType | None:
        if head in fi.top:
            fqn = fi.top[head]
            return JType("proj", fqn, ti=self.types.get(fqn))
        if head in fi.single:
            dotted = fi.single[head]
            return self._dotted_type(dotted)
        pkg = self.packages.get(fi.package, {})
        if head in pkg:
            return JType("proj", pkg[head], ti=self.types.get(pkg[head]))
        for od in fi.ondemand:
            if od in self.packages and head in self.packages[od]:
                fqn = self.packages[od][head]
                return JType("proj", fqn, ti=self.types.get(fqn))
            if od in self.types:
                mt = self.member_type(self.types[od], head)
                if mt is not None:
                    return self.jtype_of(mt)
        if head in jdk.JAVA_LANG:
            return JType("ext", f"java.lang.{head}")
        for od in fi.ondemand:
            if head in jdk.ON_DEMAND.get(od, ()):
                return JType("ext", f"{od}.{head}")
        ext_od = [od for od in fi.ondemand if od not in self.packages and od not in self.types]
        if len(ext_od) == 1 and head[:1].isupper():
            return JType("ext", f"{ext_od[0]}.{head}")
        return None

    def _dotted_type(self, dotted: str) -> JType:
        if dotted in self.types:
            return JType("proj", dotted, ti=self.types[dotted])
        parts = dotted.split(".")
        for k in range(len(parts) - 1, 0, -1):
            cand = ".".join(parts[:k])
            if cand in self.types:
                ti: TypeInfo | None = self.types[cand]
                for seg in parts[k:]:
                    ti = self.member_type(ti, seg) if ti is not None else None
                if ti is not None:
                    return self.jtype_of(ti)
                break
        return JType("ext", dotted)

    def resolve_ref(self, ref: P.TypeRef | None, fi: FileInfo, chain: list[TypeInfo],
                    tvars: dict | None = None,
                    local_types: Iterable[dict[str, TypeInfo]] = ()) -> JType | None:
        if ref is None or ref.wildcard:
            return None
        if ref.name == "var":
            return None
        base = self.resolve_name(ref.name, fi, chain, tvars, local_types)
        if base is None:
            return None
        args = tuple(self.resolve_ref(a, fi, chain, tvars, local_types) for a in ref.args)
        dims = ref.dims + (1 if ref.varargs else 0)
        return JType(base.kind, base.name, dims, args, base.ti)

    # ---- hierarchy -------------------------------------------------------
    def outer_chain(self, ti: TypeInfo) -> list[TypeInfo]:
        out = []
        cur = ti.outer
        while cur is not None:
            out.append(cur)
            cur = cur.outer
        return out

    def supers(self, ti: TypeInfo) -> list[JType]:
        """Direct supertypes, resolved once (superclass first)."""
        if ti._supers is not None:
            return ti._supers
        if ti._resolving:
            return []
        ti._resolving = True
        out: list[JType] = []
        if ti.explicit_supers is not None:
            out = [s for s in ti.explicit_supers if s is not None]
        else:
            d = ti.decl
            chain = self.outer_chain(ti)
            tv = self._all_tvars(ti)
            for ref in d.extends + d.implements:
                t = self.resolve_ref(ref, ti.file, chain, tv)
                if t is not None and t.kind in ("proj", "ext", "pseudo"):
                    out.append(t)
            if ti.kind == "enum":
                out.append(JType("ext", "java.lang.Enum"))
            elif ti.kind == "record":
                out.append(JType("ext", "java.lang.Record"))
            elif ti.kind == "annotation":
                out.append(JType("ext", "java.lang.annotation.Annotation"))
        ti._supers = out
        ti._resolving = False
        return out

    def ancestors(self, ti: TypeInfo) -> list[TypeInfo]:
        """All project supertypes, nearest first, each once. Callers must not mutate it."""
        if ti._ancestors is not None:
            return ti._ancestors
        out: list[TypeInfo] = []
        seen = {id(ti)}
        queue = [ti]
        while queue:
            cur = queue.pop(0)
            for s in self.supers(cur):
                sti = self.type_info(s)
                if sti is not None and id(sti) not in seen:
                    seen.add(id(sti))
                    out.append(sti)
                    queue.append(sti)
        if self.frozen:
            ti._ancestors = out
        return out

    def ext_ancestors(self, ti: TypeInfo) -> list[str]:
        out = []
        for t in [ti] + self.ancestors(ti):
            for s in self.supers(t):
                if s.kind == "ext":
                    out.append(s.name)
        return out

    def is_subtype(self, ti: TypeInfo, name: str) -> bool:
        """Is ``ti`` the type ``name`` (fqn or simple) or one of its subtypes?"""
        simple = name.rsplit(".", 1)[-1]
        for t in [ti] + self.ancestors(ti):
            if t.fqn == name or t.decl.name == simple:
                return True
        return any(e == name or e.rsplit(".", 1)[-1] == simple for e in self.ext_ancestors(ti))

    def superclass(self, ti: TypeInfo) -> JType | None:
        if ti.is_interface:
            return None
        for s in self.supers(ti):
            sti = self.type_info(s)
            if sti is not None and not sti.is_interface:
                return s
            if s.kind == "ext" and ti.decl.extends and \
                    s.name.rsplit(".", 1)[-1] == ti.decl.extends[0].name.rsplit(".", 1)[-1]:
                return s
        return None

    def find_methods(self, ti: TypeInfo, name: str) -> list[MethodInfo]:
        """Methods named ``name`` visible in ``ti``: its own, then inherited ones it does not
        override."""
        out: list[MethodInfo] = []
        seen: set[str] = set()
        for t in [ti] + self.ancestors(ti):
            for m in t.methods.get(name, ()):
                key = ",".join(m.erased)
                if key in seen:
                    continue
                seen.add(key)
                out.append(m)
        return out

    def find_field(self, ti: TypeInfo, name: str) -> FieldInfo | None:
        for t in [ti] + self.ancestors(ti):
            f = t.fields.get(name)
            if f is not None:
                return f
        return None

    def link_overrides(self) -> None:
        """Record which methods override which, by name and erased parameters.

        A parameter typed by a type variable of the overridden method's class matches
        anything (``compareTo(T)`` is overridden by ``compareTo(Money)``).
        """
        for ti in list(self.types.values()):
            anc = self.ancestors(ti)
            if not anc:
                continue
            for name, ms in ti.methods.items():
                for m in ms:
                    if m.static or m.ctor or m.visibility == "private":
                        continue
                    for a in anc:
                        hit = None
                        for cand in a.methods.get(name, ()):
                            if cand.static or cand.visibility == "private" or \
                                    len(cand.params) != len(m.params):
                                continue
                            if all(ce == me or _is_tvar(cp, cand) for ce, me, cp in
                                   zip(cand.erased, m.erased, cand.params)):
                                hit = cand
                                break
                        if hit is not None and hit not in m.overrides:
                            m.overrides.append(hit)
                            hit.overriders.append(m)


def _is_tvar(ref: P.TypeRef, m: MethodInfo) -> bool:
    return "." not in ref.name and (ref.name in m.tparams or ref.name in m.owner.tparams) \
        and ref.dims == 0


def _bounds(text: str) -> dict[str, P.TypeRef | None]:
    """``<T extends Comparable<T>, U>`` -> {T: Comparable<T>, U: None}."""
    try:
        p = P.Parser(text)
        p.expect("<")
        out: dict[str, P.TypeRef | None] = {}
        while True:
            p.annotations()
            name = p.ident()
            bound = None
            if p.accept("extends"):
                bound = p.type_ref()
                while p.accept("&"):
                    p.type_ref()
            out[name] = bound
            if p.accept(","):
                continue
            break
        return out
    except P.ParseError:
        return {}


# --------------------------------------------------------------------------
# the body walker
# --------------------------------------------------------------------------
@dataclass
class EdgeRec:
    src: str
    kind: str                  # calls instantiates reads writes mutates raises handles annotates
    dst: str                   # node id, or "ext:<dotted>" for outside the project
    line: int
    col: int = 0
    confidence: float = 1.0
    conditional: bool = False
    dynamic: bool = False
    meta: dict = field(default_factory=dict)


@dataclass
class Facts:
    """What a walk learned about its callable, beyond edges."""
    statements: int = 0
    returns_value: bool = False
    calls_exit: bool = False
    calls_open: bool = False
    switches: list[dict] = field(default_factory=list)
    reflective: int = 0


class BodyWalker:
    def __init__(self, idx: Index, fi: FileInfo, owner: TypeInfo, src_id: str,
                 emit: Callable[[EdgeRec], None], static: bool = False,
                 tvars: dict | None = None, handled: tuple[str, ...] = (),
                 duck: bool = True) -> None:
        self.idx = idx
        self.fi = fi
        self.chain: list[TypeInfo] = [owner] + idx.outer_chain(owner)
        self.src = src_id
        self.emit_cb = emit
        self.static = static
        self.tvars = dict(tvars or {})
        self.locals: list[dict[str, JType | None]] = [{}]
        self.local_types: list[dict[str, TypeInfo]] = [{}]
        self.cond = False
        self.loop = False
        self.handled = handled
        self.duck = duck
        self.facts = Facts()

    # ---- emission --------------------------------------------------------
    def emit(self, kind: str, dst: str | None, node: Any, confidence: float = 1.0,
             dynamic: bool = False, **meta: Any) -> None:
        if not dst:
            return
        self.emit_cb(EdgeRec(self.src, kind, dst, getattr(node, "line", 0),
                             getattr(node, "col", 0), confidence, self.cond, dynamic,
                             {k: v for k, v in meta.items() if v is not None}))

    def ext_id(self, t: JType | None, member: str = "") -> str | None:
        if t is None or t.kind != "ext" or "." not in t.name:
            return None
        return f"ext:{LANG}@{t.name}" + (f".{member}" if member else "")

    def type_use(self, t: JType | None, node: Any, how: str) -> None:
        ti = self.idx.type_info(t)
        if ti is not None and ti.id:
            self.emit("annotates", ti.id, node, use=how)

    # ---- scopes ----------------------------------------------------------
    def push(self) -> None:
        self.locals.append({})
        self.local_types.append({})

    def pop(self) -> None:
        self.locals.pop()
        self.local_types.pop()

    def declare(self, name: str, t: JType | None) -> None:
        self.locals[-1][name] = t

    def lookup_local(self, name: str) -> tuple[bool, JType | None]:
        for scope in reversed(self.locals):
            if name in scope:
                return True, scope[name]
        return False, None

    def resolve_ref(self, ref: P.TypeRef | None) -> JType | None:
        return self.idx.resolve_ref(ref, self.fi, self.chain, self.tvars,
                                    reversed(self.local_types))

    def resolve_in(self, ref: P.TypeRef | None, m: MethodInfo | FieldInfo) -> JType | None:
        """A type as written in another member's declaration (its return or field type)."""
        owner = m.owner
        tv = dict(getattr(m, "tparams", {}) or {})
        chain = [owner] + self.idx.outer_chain(owner)
        t = self.idx.resolve_ref(ref, owner.file, chain, tv)
        if t is not None and t.kind == "tvar":
            return None
        return t

    def flags(self, cond: bool | None = None, loop: bool | None = None,
              handled: tuple | None = None) -> tuple:
        old = (self.cond, self.loop, self.handled)
        if cond is not None:
            self.cond = self.cond or cond
        if loop is not None:
            self.loop = self.loop or loop
        if handled is not None:
            self.handled = handled
        return old

    def restore(self, old: tuple) -> None:
        self.cond, self.loop, self.handled = old

    # ---- entry points ----------------------------------------------------
    def walk_method(self, m: P.MethodDecl, throws: Iterable[str] = ()) -> Facts:
        self.push()
        for p in m.params:
            self.declare(p.name, self.resolve_ref(p.type))
        self.handled = tuple(throws)
        if m.body is not None:
            self.block(m.body)
        self.pop()
        return self.facts

    def walk_expr(self, e: Any) -> Facts:
        self.ex(e)
        return self.facts

    def walk_block(self, b: P.Block) -> Facts:
        self.block(b)
        return self.facts

    def walk_type_body(self, td: P.TypeDecl, supers: list[JType] | None = None) -> None:
        """Fold an anonymous or local class body into the current callable."""
        pseudo = self.idx.register_pseudo(td, self.fi, self.chain[0], supers)
        saved_chain, saved_static = self.chain, self.static
        self.chain = [pseudo] + self.chain
        self.static = False
        old = self.flags(cond=True)
        try:
            self._walk_members(td)
        finally:
            self.restore(old)
            self.chain, self.static = saved_chain, saved_static

    def _walk_members(self, td: P.TypeDecl) -> None:
        for c in td.constants:
            for a in c.args:
                self.ex(a)
        for m in td.members:
            if isinstance(m, P.MethodDecl):
                saved = self.handled
                self.push()
                for p in m.params:
                    self.declare(p.name, self.resolve_ref(p.type))
                self.handled = tuple(t.name.rsplit(".", 1)[-1] for t in m.throws)
                if m.body is not None:
                    self.block(m.body)
                self.pop()
                self.handled = saved
            elif isinstance(m, P.FieldDecl):
                for _n, _d, init, _l in m.names:
                    if init is not None:
                        self.ex(init)
            elif isinstance(m, P.Initializer) and m.body is not None:
                self.block(m.body)
            elif isinstance(m, P.TypeDecl):
                self.walk_type_body(m)

    # ---- statements ------------------------------------------------------
    def block(self, b: P.Block) -> None:
        self.push()
        for s in b.stmts:
            self.stmt(s)
        self.pop()

    def stmt(self, s: Any) -> None:
        if s is None:
            return
        self.facts.statements += 1
        if isinstance(s, P.Block):
            self.facts.statements -= 1
            self.block(s)
        elif isinstance(s, P.ExprStmt):
            self.ex(s.expr)
        elif isinstance(s, P.LocalVar):
            self.local_var(s)
        elif isinstance(s, P.If):
            self.ex(s.cond)
            old = self.flags(cond=True)
            self.stmt(s.then)
            self.stmt(s.other)
            self.restore(old)
        elif isinstance(s, P.Loop):
            self.loop_stmt(s)
        elif isinstance(s, P.Return):
            if s.expr is not None:
                self.facts.returns_value = True
                self.ex(s.expr)
        elif isinstance(s, P.Throw):
            t = self.ex(s.expr)
            ti = self.idx.type_info(t)
            if ti is not None and ti.id:
                self.emit("raises", ti.id, s)
            elif t is not None and t.kind == "ext":
                self.emit("raises", self.ext_id(t), s)
        elif isinstance(s, P.Yield):
            self.ex(s.expr)
        elif isinstance(s, P.Try):
            self.try_stmt(s)
        elif isinstance(s, P.Switch):
            self.switch(s)
        elif isinstance(s, P.Sync):
            self.ex(s.lock)
            if s.body is not None:
                self.block(s.body)
        elif isinstance(s, P.Labeled):
            self.facts.statements -= 1
            self.stmt(s.stmt)
        elif isinstance(s, P.Assert):
            old = self.flags(cond=True)
            self.ex(s.cond)
            self.ex(s.msg)
            self.restore(old)
        elif isinstance(s, P.LocalClass):
            if s.decl is not None:
                pseudo = self.idx.register_pseudo(s.decl, self.fi, self.chain[0])
                self.local_types[-1][s.decl.name] = pseudo
                saved_chain = self.chain
                self.chain = [pseudo] + self.chain
                old = self.flags(cond=True)
                try:
                    self._walk_members(s.decl)
                finally:
                    self.restore(old)
                    self.chain = saved_chain
        elif isinstance(s, P.Unparsed):
            self.unparsed(s)

    def local_var(self, s: P.LocalVar) -> None:
        declared = self.resolve_ref(s.type) if s.type is not None else None
        if declared is not None:
            self.type_use(declared, s, "local")
        for name, dims, init in s.names:
            t = declared
            if t is not None and dims:
                t = JType(t.kind, t.name, t.dims + dims, t.args, t.ti)
            if init is not None:
                it = self.ex(init)
                if s.type is None:
                    t = it
            self.declare(name, t)

    def loop_stmt(self, s: P.Loop) -> None:
        self.push()
        if s.kind == "foreach":
            it = self.ex(s.iter)
            elem = None
            if it is not None:
                if it.dims:
                    elem = it.elem()
                elif it.args and it.kind in ("ext", "proj") and len(it.args) == 1:
                    elem = it.args[0]
            if s.var is not None:
                declared = self.resolve_ref(s.var.type) if s.var.type is not None else None
                for name, dims, _ in s.var.names:
                    self.declare(name, declared if declared is not None else elem)
        else:
            for i in s.init:
                if isinstance(i, P.LocalVar):
                    self.local_var(i)
                else:
                    self.ex(i)
            self.ex(s.cond)
        old = self.flags(cond=True, loop=True)
        for u in s.update:
            self.ex(u)
        self.stmt(s.body)
        self.restore(old)
        self.pop()

    def try_stmt(self, s: P.Try) -> None:
        self.push()
        caught: list[str] = []
        for c in s.catches:
            for t in c.types:
                caught.append(t.name.rsplit(".", 1)[-1])
        old = self.flags(handled=self.handled + tuple(caught))
        for r in s.resources:
            if isinstance(r, P.LocalVar):
                self.local_var(r)
            else:
                self.ex(r)
        if s.body is not None:
            self.block(s.body)
        self.restore(old)
        for c in s.catches:
            old = self.flags(cond=True)
            self.push()
            first = None
            for t in c.types:
                jt = self.resolve_ref(t)
                first = first or jt
                ti = self.idx.type_info(jt)
                if ti is not None and ti.id:
                    self.emit("handles", ti.id, c)
                elif jt is not None:
                    self.emit("handles", self.ext_id(jt), c)
            self.declare(c.name, first if len(c.types) == 1 else None)
            if c.body is not None:
                self.block(c.body)
            self.pop()
            self.restore(old)
        if s.final is not None:
            self.block(s.final)
        self.pop()

    def switch(self, s: P.Switch) -> JType | None:
        sel = self.ex(s.selector)
        sti = self.idx.type_info(sel)
        labels: list[str] = []
        patterns = False
        has_default = False
        old = self.flags(cond=True)
        self.push()
        for c in s.cases:
            if c.default:
                has_default = True
            for lab in c.labels:
                if isinstance(lab, P.Pattern):
                    patterns = True
                    pt = self.resolve_ref(lab.type)
                    pti = self.idx.type_info(pt)
                    labels.append(pti.fqn if pti is not None else (lab.type.name if lab.type else "?"))
                    self.type_use(pt, lab, "pattern")
                    self.bind_pattern(lab)
                    continue
                if isinstance(lab, P.Literal) and lab.kind == "null":
                    continue
                name = lab.name if isinstance(lab, P.Name) else (
                    lab.name if isinstance(lab, P.FieldAccess) else "")
                if name:
                    enum_ti = sti if sti is not None and sti.kind == "enum" else None
                    if enum_ti is None and isinstance(lab, P.Name):
                        cands = self.idx.const_index.get(name, [])
                        if len(cands) == 1 and sel is None:
                            enum_ti = cands[0]
                            sti = enum_ti
                    if enum_ti is not None and isinstance(lab, P.Name):
                        f = enum_ti.fields.get(name)
                        if f is not None and f.id:
                            self.emit("reads", f.id, lab, case=True)
                        labels.append(name)
                        continue
                    labels.append(name)
                    if isinstance(lab, P.FieldAccess):
                        self.ex(lab)
                    continue
                self.ex(lab)
            if c.guard is not None:
                self.ex(c.guard)
            if c.arrow:
                self.push()
            for st in c.body:
                self.stmt(st)
            if c.arrow:
                self.pop()
        self.pop()
        self.restore(old)
        if sti is not None and sti.id and (sti.kind == "enum" or patterns or
                                            "sealed" in sti.decl.modifiers):
            self.facts.switches.append({
                "line": s.line, "end": s.end_line, "subject": sti.id, "labels": labels,
                "default": has_default, "expr": s.is_expr, "patterns": patterns,
            })
        return None

    def bind_pattern(self, p: P.Pattern) -> None:
        if p.binding:
            self.declare(p.binding, self.resolve_ref(p.type))
        for sub in p.subpatterns:
            if isinstance(sub, P.Pattern):
                self.bind_pattern(sub)

    def unparsed(self, s: P.Unparsed) -> None:
        """A statement the parser skipped: link unqualified calls it contains by name."""
        toks = s.toks
        for i, t in enumerate(toks[:-1]):
            if t.kind == "ident" and toks[i + 1].text == "(" and (
                    i == 0 or toks[i - 1].text not in (".", "new", "::")):
                for ti in self.chain:
                    ms = self.idx.find_methods(ti, t.text)
                    if ms:
                        for m in ms:
                            self.emit("calls", m.id, t, confidence=0.5, callee=t.text)
                        break

    # ---- expressions -----------------------------------------------------
    def ex(self, e: Any) -> JType | None:
        if e is None:
            return None
        m = getattr(self, "x_" + type(e).__name__, None)
        if m is None:
            for c in P.children(e):
                self.ex(c)
            return None
        return m(e)

    def x_Literal(self, e: P.Literal) -> JType | None:
        if e.kind == "str":
            return STRING
        if e.kind == "null":
            return NULL
        if e.kind == "boolean":
            return JType("prim", "boolean")
        return JType("prim", e.kind or "int")

    def x_Name(self, e: P.Name) -> JType | None:
        return self.name_value(e, "reads")

    def name_value(self, e: P.Name, how: str, **meta: Any) -> JType | None:
        found, t = self.lookup_local(e.name)
        if found:
            return t
        f = self.field_in_scope(e.name)
        if f is not None:
            if f.id:
                self.emit(how, f.id, e, **meta)
            return self.resolve_in(f.type, f)
        tt = self.idx.resolve_name(e.name, self.fi, self.chain, self.tvars,
                                   reversed(self.local_types))
        if tt is not None and tt.kind in ("proj", "pseudo") or (
                tt is not None and tt.kind == "ext" and e.name[:1].isupper()):
            return JType("static", tt.name, ti=self.idx.type_info(tt))
        return JType("pkg", e.name)

    def field_in_scope(self, name: str) -> FieldInfo | None:
        for ti in self.chain:
            f = self.idx.find_field(ti, name)
            if f is not None:
                return f
        for owner in self.fi.static_single.get(name, ()):
            oti = self.idx.types.get(owner)
            if oti is not None:
                f = self.idx.find_field(oti, name)
                if f is not None:
                    return f
        for owner in self.fi.static_ondemand:
            oti = self.idx.types.get(owner)
            if oti is not None:
                f = self.idx.find_field(oti, name)
                if f is not None and f.static:
                    return f
        return None

    def x_FieldAccess(self, e: P.FieldAccess, how: str = "reads", **meta: Any) -> JType | None:
        t = self.ex(e.target)
        return self.member_value(t, e, how, **meta)

    def member_value(self, t: JType | None, e: P.FieldAccess, how: str,
                     **meta: Any) -> JType | None:
        if t is None:
            return None
        if t.kind == "pkg":
            dotted = f"{t.name}.{e.name}"
            if dotted in self.idx.types:
                return JType("static", dotted, ti=self.idx.types[dotted])
            return JType("pkg", dotted)
        if t.dims and e.name == "length":
            return JType("prim", "int")
        ti = self.idx.type_info(t)
        if t.kind == "static":
            if ti is not None:
                nested = self.idx.member_type(ti, e.name)
                if nested is not None:
                    return JType("static", nested.fqn, ti=nested)
                f = self.idx.find_field(ti, e.name)
                if f is not None:
                    if f.id:
                        self.emit(how, f.id, e, **meta)
                    return self.resolve_in(f.type, f)
                return None
            # an external class: System.out, Integer.MAX_VALUE, Map.Entry
            if e.name[:1].isupper() and not e.name.isupper():
                return JType("static", f"{t.name}.{e.name}")
            return None
        if ti is not None:
            f = self.idx.find_field(ti, e.name)
            if f is not None:
                if f.id:
                    self.emit(how, f.id, e, **meta)
                return self.resolve_in(f.type, f)
        return None

    def x_This(self, e: P.This) -> JType | None:
        if e.qualifier:
            simple = e.qualifier.rsplit(".", 1)[-1]
            for ti in self.chain:
                if ti.decl.name == simple:
                    return self.idx.jtype_of(ti)
        return self.idx.jtype_of(self.chain[0])

    def x_Super(self, e: P.Super) -> JType | None:
        return self.idx.superclass(self.chain[0])

    def x_Assign(self, e: P.Assign) -> JType | None:
        vt = self.ex(e.value)
        how = "writes" if e.op == "=" else "mutates"
        meta = {} if e.op == "=" else {"op": e.op}
        return self.target(e.target, how, **meta) or vt

    def target(self, t: Any, how: str, **meta: Any) -> JType | None:
        if isinstance(t, P.Name):
            return self.name_value(t, how, **meta)
        if isinstance(t, P.FieldAccess):
            base = self.ex(t.target)
            return self.member_value(base, t, how, **meta)
        if isinstance(t, P.ArrayAccess):
            self.ex(t.index)
            at = self.target(t.array, "mutates", op="[]=")
            return at.elem() if at is not None else None
        return self.ex(t)

    def x_Unary(self, e: P.Unary) -> JType | None:
        if e.op in ("++", "--"):
            return self.target(e.expr, "mutates", op=e.op)
        t = self.ex(e.expr)
        if e.op == "!":
            return JType("prim", "boolean")
        return t

    def x_Binary(self, e: P.Binary) -> JType | None:
        lt = self.ex(e.left)
        old = self.flags(cond=True) if e.op in ("&&", "||") else None
        rt = self.ex(e.right)
        if old is not None:
            self.restore(old)
        if e.op in ("==", "!=", "<", ">", "<=", ">=", "&&", "||"):
            return JType("prim", "boolean")
        if e.op == "+" and (_is_string(lt) or _is_string(rt)):
            return STRING
        if lt is not None and lt.kind == "prim" and rt is not None and rt.kind == "prim":
            order = ["int", "long", "float", "double"]
            a = lt.name if lt.name in order else "int"
            b = rt.name if rt.name in order else "int"
            return JType("prim", order[max(order.index(a), order.index(b))])
        return lt if lt is not None and lt.kind == "prim" else None

    def x_Conditional(self, e: P.Conditional) -> JType | None:
        self.ex(e.cond)
        old = self.flags(cond=True)
        a = self.ex(e.then)
        b = self.ex(e.other)
        self.restore(old)
        return a if a is not None and a.kind != "null" else b

    def x_Cast(self, e: P.Cast) -> JType | None:
        t = self.resolve_ref(e.type)
        self.type_use(t, e, "cast")
        self.ex(e.expr)
        return t

    def x_InstanceOf(self, e: P.InstanceOf) -> JType | None:
        self.ex(e.expr)
        t = self.resolve_ref(e.type)
        self.type_use(t, e, "instanceof")
        if e.pattern is not None:
            self.bind_pattern(e.pattern)
        elif e.binding:
            self.declare(e.binding, t)
        return JType("prim", "boolean")

    def x_ClassLit(self, e: P.ClassLit) -> JType | None:
        t = self.resolve_ref(e.type)
        self.type_use(t, e, "class-literal")
        return JType("ext", "java.lang.Class", args=(t,) if t is not None else ())

    def x_ArrayAccess(self, e: P.ArrayAccess) -> JType | None:
        t = self.ex(e.array)
        self.ex(e.index)
        return t.elem() if t is not None else None

    def x_ArrayInit(self, e: P.ArrayInit) -> JType | None:
        for i in e.items:
            self.ex(i)
        return None

    def x_NewArray(self, e: P.NewArray) -> JType | None:
        for d in e.dims:
            self.ex(d)
        if e.init is not None:
            self.ex(e.init)
        t = self.resolve_ref(e.type)
        self.type_use(t, e, "array")
        return t

    def x_Lambda(self, e: P.Lambda) -> JType | None:
        old = self.flags(cond=True, handled=())
        self.push()
        for p in e.params:
            self.declare(p.name, self.resolve_ref(p.type) if p.type.name != "var" else None)
        if isinstance(e.body, P.Block):
            saved = self.facts.returns_value
            self.block(e.body)
            self.facts.returns_value = saved
        else:
            self.ex(e.body)
        self.pop()
        self.restore(old)
        return None

    def x_Switch(self, e: P.Switch) -> JType | None:
        return self.switch(e)

    def x_MethodRef(self, e: P.MethodRef) -> JType | None:
        if isinstance(e.target, P.TypeRef):
            t = self.resolve_ref(e.target)
            static_ref = True
        else:
            t = self.ex(e.target)
            static_ref = t is not None and t.kind == "static"
        ti = self.idx.type_info(t)
        if ti is None:
            if t is not None and t.kind in ("ext", "static") and "." in t.name:
                if e.name == "new":
                    self.emit("instantiates", self.ext_id(JType("ext", t.name)), e, ref=True)
                else:
                    self.emit("calls", self.ext_id(JType("ext", t.name), e.name), e,
                              callee=e.name, ref=True)
            return None
        if e.name == "new":
            if ti.id:
                self.emit("instantiates", ti.id, e, ref=True)
            ctors = [c for c in ti.ctors if c.id]
            for c in ctors:
                self.emit("calls", c.id, e, confidence=1.0 if len(ctors) == 1 else 0.5,
                          callee="<init>", ref=True)
            return None
        ms = [m for m in self.idx.find_methods(ti, e.name) if m.id]
        for m in ms:
            self.emit("calls", m.id, e, confidence=1.0 if len(ms) == 1 else 0.5,
                      callee=e.name, ref=True, static_ref=static_ref or None)
        return None

    def x_New(self, e: P.New) -> JType | None:
        if e.outer is not None:
            ot = self.ex(e.outer)
            oti = self.idx.type_info(ot)
            t = None
            if oti is not None and e.type is not None:
                mt = self.idx.member_type(oti, e.type.name.rsplit(".", 1)[-1])
                t = self.idx.jtype_of(mt) if mt is not None else None
        else:
            t = self.resolve_ref(e.type)
        arg_types = [self.arg(a) for a in e.args]
        ti = self.idx.type_info(t)
        meta = {"args": len(e.args), "callee": e.type.name if e.type else "?",
                "anonymous": True if e.body is not None else None,
                "defines": sorted(f"{m.name}({','.join(erase(p.type, {}) for p in m.params)})"
                                  for m in e.body.members if isinstance(m, P.MethodDecl))
                if e.body is not None else None}
        if ti is not None:
            if ti.id:
                self.emit("instantiates", ti.id, e, **meta)
            if not ti.is_interface and ti.ctors:
                chosen = self.choose(ti.ctors, arg_types, len(e.args))
                for m, conf in chosen:
                    self.emit("calls", m.id, e, confidence=conf, **self.call_meta(meta))
        elif t is not None and t.kind == "ext":
            self.emit("instantiates", self.ext_id(t), e, **meta)
            if t.name.startswith(jdk.IO_PREFIXES):
                self.facts.calls_open = True
        if e.body is not None:
            self.walk_type_body(e.body, [t] if t is not None else [])
        return t

    def call_meta(self, meta: dict) -> dict:
        out = dict(meta)
        if self.handled:
            out["handled"] = sorted(set(self.handled))
        return out

    def arg(self, a: Any) -> JType | None:
        if isinstance(a, (P.Lambda, P.MethodRef)):
            self.ex(a)
            return JType("lambda", "")
        return self.ex(a)

    def x_MethodCall(self, e: P.MethodCall) -> JType | None:
        name = e.name
        meta: dict = {"args": len(e.args), "callee": name}
        if name in ("this", "super") and e.target is None:
            arg_types = [self.arg(a) for a in e.args]
            owner = self.chain[0]
            ti = owner if name == "this" else self.idx.type_info(self.idx.superclass(owner))
            if ti is not None:
                chosen = self.choose(ti.ctors, arg_types, len(e.args))
                for m, conf in chosen:
                    self.emit("calls", m.id, e, confidence=conf, **self.call_meta(meta))
            return None
        recv: JType | None = None
        is_super = isinstance(e.target, P.Super)
        if e.target is not None:
            recv = self.ex(e.target)
        arg_types = [self.arg(a) for a in e.args]
        if e.target is None:
            for ti in self.chain:
                ms = self.idx.find_methods(ti, name)
                if ms:
                    return self.bind(ms, arg_types, e, meta)
            owners = list(self.fi.static_single.get(name, ())) + list(self.fi.static_ondemand)
            for owner in owners:
                oti = self.idx.types.get(owner)
                if oti is not None:
                    ms = [m for m in self.idx.find_methods(oti, name) if m.static]
                    if ms:
                        return self.bind(ms, arg_types, e, meta)
                elif owner in self.fi.static_single.get(name, ()):
                    self.emit("calls", f"ext:{LANG}@{owner}.{name}", e, **self.call_meta(meta))
                    return self.ext_return(owner, name)
            return None
        ti = self.idx.type_info(recv)
        if ti is not None:
            ms = self.idx.find_methods(ti, name)
            if recv is not None and recv.kind == "static":
                statics = [m for m in ms if m.static]
                ms = statics or ms
            if ms:
                if is_super:
                    meta["super"] = True
                return self.bind(ms, arg_types, e, meta)
            return None                          # inherited from a library class, or unknown
        if recv is not None and recv.kind in ("ext", "static") and "." in recv.name \
                and not recv.dims:
            full = f"{recv.name}.{name}"
            self.emit("calls", f"ext:{LANG}@{full}", e, **self.call_meta(meta))
            if full in jdk.EXIT_CALLS:
                self.facts.calls_exit = True
            if full.startswith(jdk.IO_PREFIXES) or recv.name.startswith(jdk.IO_PREFIXES):
                self.facts.calls_open = True
            self.reflection(recv, name, e)
            return self.ext_return(recv.name, name, recv)
        if recv is None or recv.kind in ("tvar", "pkg", "lambda"):
            self.guess(name, e, meta)
        return None

    def ext_return(self, owner: str, name: str, recv: JType | None = None) -> JType | None:
        if owner == "java.lang.String" and name in ("trim", "strip", "substring", "toLowerCase",
                                                    "toUpperCase", "replace", "concat",
                                                    "format", "valueOf", "repeat", "intern",
                                                    "join", "formatted", "stripLeading",
                                                    "stripTrailing", "replaceAll"):
            return STRING
        if name == "toString":
            return STRING
        if name in ("length", "size", "indexOf", "hashCode", "compareTo", "ordinal"):
            return JType("prim", "int")
        if name.startswith("is") or name in ("equals", "contains", "isEmpty", "hasNext",
                                              "startsWith", "endsWith", "matches"):
            return JType("prim", "boolean")
        if recv is not None and recv.kind == "ext" and name in ("getClass",):
            return JType("ext", "java.lang.Class")
        return None

    def reflection(self, recv: JType, name: str, e: P.MethodCall) -> None:
        """``Class.forName("a.B")`` and ``B.class.getMethod("m")`` with literal names."""
        if not e.args or not isinstance(e.args[0], P.Literal) or e.args[0].kind != "str":
            return
        lit = e.args[0].value.strip('"')
        if recv.name == "java.lang.Class" and name == "forName":
            ti = self.idx.types.get(lit)
            if ti is not None and ti.id:
                self.facts.reflective += 1
                self.emit("instantiates", ti.id, e, confidence=0.5, dynamic=True,
                          reflection="forName")
        elif recv.name == "java.lang.Class" and name in ("getMethod", "getDeclaredMethod"):
            cls = recv.args[0] if recv.args else None
            ti = self.idx.type_info(cls)
            if ti is not None:
                for m in self.idx.find_methods(ti, lit):
                    if m.id:
                        self.facts.reflective += 1
                        self.emit("calls", m.id, e, confidence=0.4, dynamic=True,
                                  reflection=name, callee=lit)

    def bind(self, ms: list[MethodInfo], arg_types: list, e: P.MethodCall,
             meta: dict) -> JType | None:
        chosen = self.choose(ms, arg_types, len(e.args))
        ret: JType | None = None
        for m, conf in chosen:
            if m.id:
                self.emit("calls", m.id, e, confidence=conf, **self.call_meta(meta))
            if ret is None and m.ret is not None:
                ret = self.resolve_in(m.ret, m)
                if ret is None and conf == 1.0:
                    ret = _infer_generic_return(m, arg_types)
        return ret

    def guess(self, name: str, e: P.MethodCall, meta: dict) -> None:
        if not self.duck or name in COMMON_NAMES:
            return
        cands = [m for m in self.idx.methods_by_name.get(name, ()) if m.id and
                 _arity_ok(m, len(e.args))]
        roots = {id(m) for m in cands if not m.overrides}
        if not cands or len(roots) > 2:
            return
        for m in cands:
            self.emit("calls", m.id, e, confidence=0.3, dynamic=True,
                      **self.call_meta(dict(meta, guess=True)))

    # ---- overload resolution --------------------------------------------
    def choose(self, ms: list[MethodInfo], arg_types: list, nargs: int
               ) -> list[tuple[MethodInfo, float]]:
        cands = [m for m in ms if _arity_ok(m, nargs)]
        if not cands:
            return []
        if len(cands) == 1:
            return [(cands[0], 1.0)]
        exact = [m for m in cands if len(m.params) == nargs]
        pool = exact or cands
        scored: list[tuple[int, MethodInfo]] = []
        for m in pool:
            s = self.score(m, arg_types)
            if s is not None:
                scored.append((s, m))
        if not scored:
            scored = [(0, m) for m in pool]
        best = max(s for s, _ in scored)
        top = [m for s, m in scored if s == best]
        if len(top) == 1:
            return [(top[0], 1.0)]
        return [(m, 0.5) for m in top]

    def score(self, m: MethodInfo, arg_types: list) -> int | None:
        total = 0
        for i, at in enumerate(arg_types):
            if i < len(m.params):
                pref = m.params[i]
            elif m.params:
                pref = m.params[-1]
            else:
                return None
            vararg_elem = m.varargs and i >= len(m.params) - 1 and not (
                len(arg_types) == len(m.params) and at is not None and at.dims)
            s = self.compat(at, pref, m, vararg_elem)
            if s is None:
                return None
            total += s
        return total

    def compat(self, at: JType | None, pref: P.TypeRef, m: MethodInfo,
               vararg_elem: bool) -> int | None:
        if at is None:
            return 0
        pdims = pref.dims + (1 if pref.varargs and not vararg_elem else 0)
        pname = pref.name
        is_tvar = "." not in pname and (pname in m.tparams or pname in m.owner.tparams)
        psimple = pname.rsplit(".", 1)[-1]
        if at.kind == "lambda":
            return 0 if (pdims == 0 and pname not in P.PRIMITIVES) else None
        if at.kind == "null":
            return 1 if (pname not in P.PRIMITIVES or pdims) else None
        if is_tvar:
            return 1
        if psimple == "Object" and pdims == 0:
            return 1
        if at.dims != pdims:
            return None if at.kind in ("prim", "proj", "ext") else 0
        if at.kind == "prim":
            if pname == at.name:
                return 4
            if pname in PRIM_WIDEN.get(at.name, ()):
                return 3
            if psimple == BOXES.get(at.name) or psimple in ("Number", "Comparable",
                                                            "Serializable"):
                return 2
            return None
        asimple = at.name.rsplit(".", 1)[-1]
        if psimple == asimple:
            return 4
        if pname in P.PRIMITIVES:
            unboxed = UNBOX.get(asimple)
            if unboxed == pname or (unboxed and pname in PRIM_WIDEN.get(unboxed, ())):
                return 2
            return None if at.kind in ("proj", "ext") and asimple not in UNBOX else 0
        ati = self.idx.type_info(at)
        if ati is not None:
            if self.idx.is_subtype(ati, pname):
                return 3
            pt = self.resolve_in(pref, m)
            pti = self.idx.type_info(pt)
            if pti is not None:
                return None                      # two unrelated project types
            return 0
        if at.kind == "ext" and asimple == "String" and psimple in ("CharSequence",
                                                                    "Comparable",
                                                                    "Serializable"):
            return 3
        if at.kind == "ext" and asimple == "String":
            pt = self.resolve_in(pref, m)
            if pt is not None and pt.kind in ("proj", "prim"):
                return None
        return 0


def _infer_generic_return(m: MethodInfo, arg_types: list) -> JType | None:
    """``<T> T same(T t)`` called with an ``Object`` returns an ``Object``."""
    ret = m.ret
    if ret is None or ret.dims or ret.args or ret.name not in m.tparams:
        return None
    for p, at in zip(m.params, arg_types):
        if p.name == ret.name and not p.dims and not p.varargs and at is not None \
                and at.kind in ("proj", "ext", "pseudo"):
            return at
    return None


def _arity_ok(m: MethodInfo, nargs: int) -> bool:
    n = len(m.params)
    if m.varargs:
        return nargs >= n - 1
    return nargs == n


def _is_string(t: JType | None) -> bool:
    return t is not None and t.kind == "ext" and t.name == "java.lang.String" and not t.dims
