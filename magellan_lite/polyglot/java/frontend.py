"""Java frontend: ``.java`` sources to the shared node/edge model.

Backend
-------
A pure-Python parser (:mod:`magellan_lite.polyglot.java.parser`) and resolver
(:mod:`magellan_lite.polyglot.java.resolve`). No JDK, no build, no classpath: it runs on code that
does not compile yet, on Java 1.2 through 21 alike (raw types, anonymous classes,
generics, lambdas, records, sealed types, switch expressions and patterns, text
blocks, ``var``). :func:`available` is therefore always true. The parser was checked
against ``javac``'s own trees (``com.sun.source``) on gson, jsoup and commons-cli.

Mapping
-------
=============================  ==============================================================
Java                           Magellan
=============================  ==============================================================
package ``com.acme``           PACKAGE ``pkg:java@com.acme`` (parents ``pkg:java@com`` ...)
file ``com/acme/Parser.java``  MODULE ``mod:java@com.acme.Parser``
class, interface, enum,        CLASS ``cls:java@com.acme.Parser`` (nested: ``...Parser.Inner``),
record, ``@interface``         tagged ``interface`` / ``enum`` / ``record`` / ``annotation``,
                               ``abstract``, ``final``, ``sealed``
method, constructor            METHOD ``fn:java@com.acme.Parser.parse(int,String)``: the erased
                               parameter types are part of the id, so overloads are distinct.
                               Constructors are named ``<init>``. Records get their canonical
                               constructor and accessors synthesized (tag ``synthetic``).
static field, enum constant    CLASS_ATTR ``attr:java@com.acme.Parser.MAX`` (``enum-constant``)
instance field, record comp.   INSTANCE_ATTR ``iattr:java@com.acme.Parser.count``
anonymous/local class, lambda  folded into the enclosing callable (their calls are its calls)
``extends`` / ``implements``   INHERITS (to ``ext:java@<fqn>`` for library supertypes)
override                       OVERRIDES (name + erased parameters; type variables match)
call                           CALLS with ``args``/``col``/``callee``, and ``handled``: the
                               exception types caught or declared around the site
virtual dispatch               CALLS to each overriding method, confidence 0.5, ``dynamic``,
                               ``meta["dispatch"]`` (below the 0.6 recursion-cycle floor)
``new T(...)``                 INSTANTIATES the class, and CALLS the chosen constructor
field access                   READS / WRITES / MUTATES (``grows`` for ``add``/``put``...)
``throw`` / ``catch``          RAISES / HANDLES
type in a signature or body    ANNOTATES (``use``: param, return, field, cast, local, ...)
annotation type used           ANNOTATES to the annotation type (``use``: annotation)
``import``                     IMPORTS between files (``deferred``: Java imports run nothing)
``native`` method              CALLS ``ext:abi:Java_<pkg>_<Cls>_<m>`` (JNI mangling,
                               ``meta["abi_import"]``); overloaded natives use the long form
reflection with literals       ``Class.forName``/``getMethod``: INSTANTIATES/CALLS at 0.5/0.4
=============================  ==============================================================

Tests: JUnit 3 (``testX`` in a ``TestCase``), JUnit 4/5 and TestNG ``@Test`` (and
``@ParameterizedTest``...) methods are tagged ``test`` and their files labelled test.
Framework annotations (Spring, JAX-RS, CDI) mark handlers for labelling and keep them
out of dead-code reports. Break semantics live in :mod:`magellan_lite.polyglot.java.semantics`.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path

from magellan_lite.polyglot.core.hashing import file_hash
from magellan_lite.polyglot.core.model import Edge, EdgeKind, FileRecord, Graph, Node, NodeKind
from magellan_lite.polyglot.java import jdk
from magellan_lite.polyglot.java import parser as P
from magellan_lite.polyglot.java.resolve import (
    LANG, BodyWalker, EdgeRec, FieldInfo, FileInfo, Index, JType, MethodInfo, TypeInfo, erase,
)

SOURCE_SUFFIXES = (".java",)
CONFIG_FILES = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
                "settings.gradle.kts")
SKIP_DIRS = {"target", "build", "out", ".gradle", ".mvn", ".idea", "node_modules", ".git",
             ".magellan", "__pycache__", ".venv", "venv", "generated", "generated-sources"}
DISPATCH_CONFIDENCE = 0.5
_WORD = re.compile(r"[A-Za-z_$][\w$]*")

_EDGE_KINDS = {
    "calls": EdgeKind.CALLS, "instantiates": EdgeKind.INSTANTIATES, "reads": EdgeKind.READS,
    "writes": EdgeKind.WRITES, "mutates": EdgeKind.MUTATES, "raises": EdgeKind.RAISES,
    "handles": EdgeKind.HANDLES, "annotates": EdgeKind.ANNOTATES,
}


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _walk_files(root: Path, excludes: set[str] = frozenset()):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and d not in excludes
                             and not d.startswith("."))
        for fn in sorted(filenames):
            if fn.endswith(".java") and fn not in ("module-info.java", "package-info.java"):
                yield Path(dirpath) / fn


def has_sources(root: str | Path) -> bool:
    for _ in _walk_files(Path(root)):
        return True
    return False


def available() -> bool:
    """The parser is pure Python, so the toolchain is always here."""
    try:
        return P.parse("class A {}").types[0].name == "A"
    except Exception:                      # never raise: degrade to "unavailable"
        return False


# --------------------------------------------------------------------------
def merge(state, root: str | Path) -> set[str]:
    """Add the Java sources under ``root`` to ``state.graph``; return the ids added."""
    graph: Graph = state.graph
    root = Path(root)
    excludes = set(getattr(getattr(state, "config", None), "excludes", ()) or ())
    files = list(_walk_files(root, excludes))
    if not files:
        return set()
    if not available():
        graph.diagnostics.append("Java sources found but the Java parser failed to load; skipped")
        return set()
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 20000))
    try:
        return _Builder(state, graph, root, files).run()
    finally:
        sys.setrecursionlimit(limit)


class _Builder:
    def __init__(self, state, graph: Graph, root: Path, files: list[Path]) -> None:
        self.state = state
        self.graph = graph
        self.root = root
        self.paths = files
        self.idx = Index()
        self.added: set[str] = set()
        self.decl_line: dict[str, int] = {}
        self.facts: dict[str, dict] = {}
        self.duck = bool(getattr(getattr(state, "config", None), "duck_typing", True))
        self.parse_errors = 0
        self.partial = 0
        self._unchecked_memo: dict[str, bool] = {}

    # ---- nodes -----------------------------------------------------------
    def node(self, node: Node) -> Node:
        got = self.graph.add_node(node)
        if got is node:
            self.added.add(node.id)
        return got

    def contains(self, parent: str | None, child: Node) -> None:
        if parent and parent in self.graph.nodes:
            self.graph.add_edge(Edge(src=parent, dst=child.id, kind=EdgeKind.CONTAINS,
                                     lineno=child.lineno, path=child.path))

    def ext(self, dotted_id: str) -> str:
        """``ext:java@java.util.List.add`` -> make sure the external node exists."""
        if dotted_id not in self.graph.nodes:
            q = dotted_id[4:]
            name = q.split("@", 1)[-1].rsplit(".", 1)[-1]
            self.node(Node(id=dotted_id, kind=NodeKind.EXTERNAL, name=name, qualname=q,
                           module="", meta={"lang": LANG}))
        return dotted_id

    def edge(self, src: str, dst: str, kind: EdgeKind, line: int, path: str, **kw) -> None:
        if src not in self.graph.nodes or dst not in self.graph.nodes:
            return
        ctx = self.graph.nodes[src].qualname
        self.graph.add_edge(Edge(src=src, dst=dst, kind=kind, lineno=line, path=path,
                                 context=ctx, **kw))

    # ---- driver ----------------------------------------------------------
    def run(self) -> set[str]:
        infos: list[FileInfo] = []
        seen_modules: dict[str, int] = {}
        for p in self.paths:
            rel = p.relative_to(self.root).as_posix()
            try:
                src = p.read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            rec = FileRecord(path=rel, module="", sha256=file_hash(src),
                             lines=src.count("\n") + 1, source_root="")
            cu = _parse(src, f"{rec.sha256}:{len(src)}")
            if isinstance(cu, str):
                self.parse_errors += 1
                rec.parse_error = cu
                cu = None
            pkg = cu.package if cu is not None else _guess_package(src)
            stem = p.stem
            qual = f"{LANG}@{pkg}.{stem}" if pkg else f"{LANG}@{stem}"
            n = seen_modules.get(qual, 0)
            seen_modules[qual] = n + 1
            if n:
                qual = f"{qual}~{n + 1}"
            rec.module = qual
            self.graph.files[rel] = rec
            self.module_node(rel, qual, pkg, stem, src, cu)
            if cu is None:
                continue
            if cu.errors:
                self.partial += 1
            fi = FileInfo(path=rel, module_qual=qual, module_id=f"mod:{qual}", package=pkg,
                          cu=cu, src=src)
            self.idx.add_file(fi)
            infos.append(fi)
        self.idx.freeze()
        self.idx.link_overrides()
        for fi in infos:
            for ti in fi.types:
                self.class_node(ti)
        for fi in infos:
            self.imports(fi)
            for ti in fi.types:
                self.structure(ti)
        for fi in infos:
            for ti in fi.types:
                self.bodies(ti)
        self.dispatch()
        self.jni()
        self.signals()
        self.graph.diagnostics.extend(self.idx.diagnostics)
        note = f"java: {len(self.paths)} files, {len(self.idx.types)} types"
        if self.parse_errors:
            note += f", {self.parse_errors} unparsable"
        if self.partial:
            note += f", {self.partial} with statements skipped by error recovery"
        self.graph.diagnostics.append(note)
        return self.added

    # ---- modules and packages -------------------------------------------
    def package_nodes(self, pkg: str) -> str | None:
        if not pkg:
            return None
        parts = pkg.split(".")
        parent = None
        for i in range(1, len(parts) + 1):
            q = ".".join(parts[:i])
            pid = f"pkg:{LANG}@{q}"
            if pid not in self.graph.nodes:
                n = self.node(Node(id=pid, kind=NodeKind.PACKAGE, name=parts[i - 1],
                                   qualname=f"{LANG}@{q}", module=f"{LANG}@{q}", parent=parent,
                                   meta={"lang": LANG}))
                self.contains(parent, n)
            parent = pid
        return parent

    def module_node(self, rel: str, qual: str, pkg: str, stem: str, src: str,
                    cu: P.CompilationUnit | None) -> None:
        parent = self.package_nodes(pkg)
        imports = [("static " if i.static else "") + i.name + (".*" if i.wildcard else "")
                   for i in (cu.imports if cu else [])]
        shell = "package " + pkg + ";" + ";".join(sorted(imports))
        node = self.node(Node(
            id=f"mod:{qual}", kind=NodeKind.MODULE, name=stem, qualname=qual, module=qual,
            path=rel, lineno=1, end_lineno=src.count("\n") + 1, parent=parent,
            sig_hash=_h("package " + pkg), body_hash=_h(shell), doc_hash="",
            meta={"lang": LANG, "package": pkg, "imports": imports,
                  **({"parse_error": True} if cu is None else {}),
                  **({"skipped_statements": cu.errors} if cu is not None and cu.errors else {})}))
        self.contains(parent, node)

    # ---- classes and members --------------------------------------------
    def class_node(self, ti: TypeInfo) -> None:
        d = ti.decl
        fi = ti.file
        parent = ti.outer.id if ti.outer is not None else fi.module_id
        tags = {ti.kind} if ti.kind != "class" else set()
        if ti.is_interface:
            tags.add("interface")
        for mod in ("abstract", "final", "sealed", "non-sealed", "static"):
            if mod in d.modifiers:
                tags.add(mod)
        if ti.kind == "record":
            tags.add("final")
        annos = [a.name.rsplit(".", 1)[-1] for a in d.annotations]
        if "Deprecated" in annos:
            tags.add("deprecated")
        if set(annos) & jdk.FRAMEWORK_CLASS_ANNOTATIONS:
            tags.add("framework")
        supers = self.idx.supers(ti)
        bases = [s.name for s in supers if s.kind in ("proj", "ext")]
        permits = [self._resolved_name(r, ti) for r in d.permits]
        sig = "|".join([ti.kind, ti.visibility, " ".join(sorted(d.modifiers - {"public",
                                                                                  "protected",
                                                                                  "private"})),
                        d.type_params_text, ",".join(bases), ",".join(permits),
                        ",".join(p.type.text() + " " + p.name for p in d.components)])
        shell_parts = [f"{c.text}" for c in d.constants]
        for m in d.members:
            if isinstance(m, P.Initializer):
                shell_parts.append(("static " if m.static else "") + m.body_text)
            elif isinstance(m, P.MethodDecl):
                shell_parts.append("m:" + m.name + "/" + str(len(m.params)))
            elif isinstance(m, P.FieldDecl):
                shell_parts.append("f:" + ",".join(n[0] for n in m.names))
            elif isinstance(m, P.TypeDecl):
                shell_parts.append("t:" + m.name)
        public = ti.visibility in ("public", "protected") and (
            ti.outer is None or self.graph.nodes.get(ti.outer.id or "") is None
            or self.graph.nodes[ti.outer.id].public)
        node = self.node(Node(
            id=ti.id, kind=NodeKind.CLASS, name=d.name, qualname=f"{LANG}@{ti.fqn}",
            module=fi.module_qual, path=fi.path, lineno=d.line, end_lineno=d.end_line or d.line,
            col=d.col, parent=parent, signature=_class_signature(ti, bases, permits),
            bases=bases, decorators=_decorators(d.annotations), public=public,
            tags=sorted(tags), sig_hash=_h(sig), body_hash=_h("\n".join(shell_parts)),
            doc_hash=_h(_norm_doc(d.doc)) if d.doc else "",
            meta={"lang": LANG, "kind": ti.kind, "visibility": ti.visibility,
                  "modifiers": sorted(d.modifiers), "annotations": [a.text() for a in d.annotations],
                  "permits": permits, "type_params": d.type_params,
                  "components": [p.type.text() + " " + p.name for p in d.components],
                  "constants": [c.name for c in d.constants],
                  "exported": public}))
        self.contains(parent, node)
        for ms in list(ti.methods.values()) + [ti.ctors]:
            for mi in ms:
                if mi.id:
                    self.method_node(mi)
        for f in ti.fields.values():
            if f.id:
                self.field_node(f)
        for nested in ti.nested.values():
            if nested.id:
                self.class_node(nested)

    def _resolved_name(self, ref: P.TypeRef, ti: TypeInfo) -> str:
        t = self.idx.resolve_ref(ref, ti.file, [ti] + self.idx.outer_chain(ti))
        return t.name if t is not None else ref.name

    def method_node(self, mi: MethodInfo) -> None:
        ti = mi.owner
        d = mi.decl
        cls = ti.decl
        throws = [self._resolved_name(t, ti) for t in (d.throws if d else [])]
        checked = [t for t in throws if not self._unchecked(t)]
        params = list(d.params) if d else list(cls.components if mi.ctor else [])
        names = [p.name for p in params] if (d or mi.ctor) else []
        type_text = [p.type.text() for p in params] if (d or mi.ctor) else []
        mods = set(d.modifiers) if d else set()
        tags: set[str] = set()
        if mi.static:
            tags.add("static")
        if mi.abstract:
            tags.add("abstract")
        for t in ("final", "native", "synchronized", "default"):
            if t in mods:
                tags.add(t)
        if mi.ctor:
            tags.add("constructor")
        if mi.synthetic:
            tags.add("synthetic")
        if mi.varargs:
            tags.add("varargs")
        annos = [a.name.rsplit(".", 1)[-1] for a in (d.annotations if d else [])]
        if "Override" in annos:
            tags.add("override")
        if "Deprecated" in annos:
            tags.add("deprecated")
        if self._is_test_method(mi, annos):
            tags.add("test")
        if set(annos) & jdk.TEST_LIFECYCLE:
            tags.add("test-fixture")
        if set(annos) & set(jdk.FRAMEWORK_ANNOTATIONS):
            tags.add("framework")
        ret = mi.ret.text() if mi.ret is not None else ("" if mi.ctor else "void")
        arity = {
            "lang": LANG,
            "positional": names,
            "required_positional": len(names) - (1 if mi.varargs else 0),
            "keyword_only": [], "required_keyword_only": [],
            "star_args": mi.varargs, "star_kwargs": False, "defaults": [],
            "types": list(mi.erased), "type_text": type_text, "returns": ret,
            "throws": throws, "checked_throws": checked,
            "throws_chain": {t: self._exception_chain(t) for t in checked},
            "static": mi.static, "visibility": mi.visibility, "final": mi.final,
            "abstract": mi.abstract, "default": mi.default, "constructor": mi.ctor,
            "type_params": d.type_params_text if d else "",
        }
        sig_text = "|".join([mi.visibility, "static" if mi.static else "",
                             "abstract" if mi.abstract else "", "final" if mi.final else "",
                             "default" if mi.default else "", arity["type_params"], ret,
                             mi.name, ",".join(type_text) + ("..." if mi.varargs else ""),
                             ",".join(throws)])
        # parameter types are part of the body hash: an overload whose parameters changed
        # is a removal plus an addition, not a "move" of an identical body
        body = (",".join(mi.erased) + "|" + d.body_text) if d is not None else \
            ("synthetic:" + ",".join(type_text))
        line = d.line if d is not None else cls.line
        end = (d.end_line or d.line) if d is not None else cls.line
        node = self.node(Node(
            id=mi.id, kind=NodeKind.METHOD, name=mi.name, qualname=mi.id[3:],
            module=ti.file.module_qual, path=ti.file.path, lineno=line, end_lineno=end,
            col=d.col if d is not None else 0, parent=ti.id,
            signature=_method_signature(mi, d, ret, throws),
            decorators=_decorators(d.annotations if d else []),
            public=mi.visibility in ("public", "protected"),
            tags=sorted(tags), sig_hash=_h(sig_text),
            body_hash="" if mi.synthetic else _h(body),
            doc_hash=_h(_norm_doc(d.doc)) if d is not None and d.doc else "",
            meta={"lang": LANG, "arity": arity, "annotations":
                  [a.text() for a in (d.annotations if d else [])],
                  **({"skipped_statements": d.unparsed} if d is not None and d.unparsed else {}),
                  "exported": self._exported(mi.visibility, ti)}))
        self.contains(ti.id, node)

    def field_node(self, f: FieldInfo) -> None:
        ti = f.owner
        tags: set[str] = set()
        value = ""
        body = ""
        annos: list[P.Annotation] = []
        doc = ""
        if f.enum_const:
            c: P.EnumConst = f.decl
            tags.add("enum-constant")
            value = c.text
            body = c.text
            annos = c.annotations
            doc = c.doc
            kind = NodeKind.CLASS_ATTR
        else:
            d = f.decl
            if isinstance(d, P.FieldDecl):
                value = d.init_text.get(f.name, "")
                body = d.init_toks.get(f.name, "")
                annos = d.annotations
                doc = d.doc
                for t in ("final", "volatile", "transient"):
                    if t in d.modifiers:
                        tags.add(t)
            else:                                # record component
                tags.add("final")
            kind = NodeKind.CLASS_ATTR if f.static else NodeKind.INSTANCE_ATTR
            if f.static:
                tags.add("static")
        ann = f.type.text() if f.type is not None else ""
        node = self.node(Node(
            id=f.id, kind=kind, name=f.name, qualname=f.id.split(":", 1)[1],
            module=ti.file.module_qual, path=ti.file.path, lineno=f.line or ti.decl.line,
            end_lineno=getattr(f.decl, "end_line", 0) or f.line or ti.decl.line,
            parent=ti.id, decorators=_decorators(annos),
            public=f.visibility in ("public", "protected"), tags=sorted(tags),
            sig_hash=_h("|".join([f.visibility, "static" if f.static else "",
                                  "final" if f.final else "", ann])),
            body_hash=_h(body), doc_hash=_h(_norm_doc(doc)) if doc else "",
            meta={"lang": LANG, "annotation": ann, "value": value[:200],
                  "visibility": f.visibility, "static": f.static, "final": f.final,
                  "exported": self._exported(f.visibility, ti)}))
        self.contains(ti.id, node)

    def _exported(self, vis: str, ti: TypeInfo) -> bool:
        """Is a member with visibility ``vis`` in ``ti`` library API, usable outside the
        project? Rules treat it like a name in Python's ``__all__``."""
        cls = self.graph.nodes.get(ti.id or "")
        return cls is not None and cls.public and (
            vis == "public" or (vis == "protected" and "final" not in ti.decl.modifiers))

    # ---- structural edges -----------------------------------------------
    def imports(self, fi: FileInfo) -> None:
        targets: dict[str, int] = {}
        words: set[str] | None = None
        for imp in fi.cu.imports:
            dotted = imp.name
            if imp.static and not imp.wildcard:
                dotted = dotted.rsplit(".", 1)[0]
            t = self.idx._dotted_type(dotted)
            ti = self.idx.type_info(t)
            if ti is not None and ti.file is not fi:
                targets.setdefault(ti.file.module_id, imp.line)
            elif imp.wildcard and dotted in self.idx.packages:
                # `import org.json.*` names no class: link only the ones this file
                # mentions, or deleting any class in the package reads as still imported
                if words is None:
                    words = set(_WORD.findall(fi.src))
                for simple, fqn in self.idx.packages[dotted].items():
                    tt = self.idx.types.get(fqn)
                    if tt is not None and tt.file is not fi and simple in words:
                        targets.setdefault(tt.file.module_id, imp.line)
        for tgt in sorted(targets):
            self.edge(fi.module_id, tgt, EdgeKind.IMPORTS, targets[tgt], fi.path,
                      meta={"deferred": True})

    def structure(self, ti: TypeInfo) -> None:
        path = ti.file.path
        for s in self.idx.supers(ti):
            if s.kind == "proj":
                sti = self.idx.types.get(s.name)
                if sti is not None and sti.id:
                    self.edge(ti.id, sti.id, EdgeKind.INHERITS, ti.decl.line, path)
            elif s.kind == "ext":
                self.edge(ti.id, self.ext(f"ext:{LANG}@{s.name}"), EdgeKind.INHERITS,
                          ti.decl.line, path)
        self.decorates(ti.decl.annotations, ti.id, ti, ti.decl.line)
        chain = [ti] + self.idx.outer_chain(ti)
        for ms in list(ti.methods.values()) + [ti.ctors]:
            for mi in ms:
                if not mi.id:
                    continue
                for o in mi.overrides:
                    if o.id:
                        self.edge(mi.id, o.id, EdgeKind.OVERRIDES,
                                  mi.decl.line if mi.decl else ti.decl.line, path)
                d = mi.decl
                if d is None:
                    continue
                self.decorates(d.annotations, mi.id, ti, d.line)
                uses = [(p.type, "param") for p in d.params] + [(d.ret, "return")] + \
                       [(t, "throws") for t in d.throws]
                for ref, how in uses:
                    for jt in self._type_refs(ref, ti.file, chain, mi.tparams):
                        self.edge(mi.id, jt, EdgeKind.ANNOTATES, d.line, path, meta={"use": how})
        for f in ti.fields.values():
            if not f.id or f.enum_const:
                continue
            for jt in self._type_refs(f.type, ti.file, chain, {}):
                self.edge(f.id, jt, EdgeKind.ANNOTATES, f.line, path, meta={"use": "field"})
            if isinstance(f.decl, P.FieldDecl):
                self.decorates(f.decl.annotations, f.id, ti, f.line)
        for nested in ti.nested.values():
            if nested.id:
                self.structure(nested)

    def _type_refs(self, ref: P.TypeRef | None, fi: FileInfo, chain: list[TypeInfo],
                   tvars: dict) -> list[str]:
        out: list[str] = []
        if ref is None:
            return out
        t = self.idx.resolve_ref(ref, fi, chain, tvars)
        ti = self.idx.type_info(JType(t.kind, t.name, 0, (), t.ti)) if t is not None else None
        if ti is not None and ti.id:
            out.append(ti.id)
        for a in ref.args:
            out += self._type_refs(a if not a.wildcard else (a.args[0] if a.args else None),
                                   fi, chain, tvars)
        return out

    def decorates(self, annos: list[P.Annotation], target: str, ti: TypeInfo, line: int) -> None:
        for a in annos:
            t = self.idx.resolve_name(a.name, ti.file, [ti] + self.idx.outer_chain(ti))
            ati = self.idx.type_info(t)
            if ati is not None and ati.id:
                # the annotated code names the annotation type, not the other way round:
                # deleting the annotated class must not look like "Since still uses it"
                self.edge(target, ati.id, EdgeKind.ANNOTATES, line, ti.file.path,
                          meta={"use": "annotation"})

    # ---- bodies ----------------------------------------------------------
    def bodies(self, ti: TypeInfo) -> None:
        fi = ti.file
        path = fi.path

        def sink(rec: EdgeRec) -> None:
            dst = rec.dst
            if dst.startswith("ext:"):
                dst = self.ext(dst)
            kind = _EDGE_KINDS[rec.kind]
            kw = {"confidence": rec.confidence, "conditional": rec.conditional,
                  "dynamic": rec.dynamic, "meta": rec.meta}
            if kind in (EdgeKind.CALLS, EdgeKind.INSTANTIATES):
                kw["col"] = rec.col
            elif kind is EdgeKind.RAISES:
                # lets shared rules tell compiler-enforced exceptions from the rest
                kw["meta"] = {**rec.meta, "checked": not self._unchecked(dst.split("@", 1)[-1])}
            self.edge(rec.src, dst, kind, rec.line, path, **kw)

        for ms in list(ti.methods.values()) + [ti.ctors]:
            for mi in ms:
                if not mi.id or mi.decl is None:
                    continue
                d = mi.decl
                w = BodyWalker(self.idx, fi, ti, mi.id, sink, static=mi.static,
                               tvars=mi.tparams, duck=self.duck)
                throws = [t.name.rsplit(".", 1)[-1] for t in d.throws]
                facts = w.walk_method(d, throws)
                self.facts[mi.id] = facts.__dict__
                if facts.switches:
                    self.graph.nodes[mi.id].meta["switches"] = facts.switches
        # initializers, field initializers, enum constant arguments and bodies: the class
        w = BodyWalker(self.idx, fi, ti, ti.id, sink, tvars=dict(ti.tparams), duck=self.duck)
        for c in ti.decl.constants:
            for a in c.args:
                w.walk_expr(a)
            if ti.ctors and ti.kind == "enum":
                arg_types = [w.arg(a) for a in c.args]
                for m, conf in w.choose(ti.ctors, arg_types, len(c.args)):
                    if m.id:
                        sink(EdgeRec(ti.id, "calls", m.id, c.line, 0, conf,
                                     meta={"args": len(c.args), "callee": "<init>"}))
            if c.body is not None:
                w.walk_type_body(c.body, [self.idx.jtype_of(ti)])
        for m in ti.decl.members:
            if isinstance(m, P.FieldDecl):
                for _name, _dims, init, _line in m.names:
                    if init is not None:
                        w.static = "static" in m.modifiers
                        w.walk_expr(init)
            elif isinstance(m, P.Initializer) and m.body is not None:
                w.static = m.static
                w.walk_block(m.body)
        if w.facts.switches:
            self.graph.nodes[ti.id].meta["switches"] = w.facts.switches
        for nested in ti.nested.values():
            if nested.id:
                self.bodies(nested)

    # ---- dispatch --------------------------------------------------------
    def dispatch(self) -> None:
        """A call through a supertype may run any override below it."""
        overriders: dict[str, list[str]] = {}
        by_id: dict[str, MethodInfo] = {}
        for ti in self.idx.types.values():
            for ms in ti.methods.values():
                for m in ms:
                    if m.id:
                        by_id[m.id] = m

        def below(m: MethodInfo, seen: set[str]) -> list[str]:
            out = []
            for o in m.overriders:
                if o.id and o.id not in seen:
                    seen.add(o.id)
                    out.append(o.id)
                    out += below(o, seen)
            return out

        for mid, m in by_id.items():
            if m.overriders and not m.static and not m.final:
                overriders[mid] = below(m, {mid})
        if not overriders:
            return
        for e in list(self.graph.edges):
            if e.kind is not EdgeKind.CALLS or e.dst not in overriders or e.src not in self.added:
                continue
            if e.meta.get("super") or e.meta.get("dispatch"):
                continue
            for dst in overriders[e.dst]:
                if dst == e.src:
                    continue
                self.graph.add_edge(Edge(
                    src=e.src, dst=dst, kind=EdgeKind.CALLS, lineno=e.lineno, path=e.path,
                    confidence=min(e.confidence, DISPATCH_CONFIDENCE),
                    conditional=e.conditional, dynamic=True, context=e.context,
                    meta={**e.meta, "dispatch": True}, col=e.col))

    # ---- JNI ---------------------------------------------------------------
    def jni(self) -> None:
        for ti in self.idx.types.values():
            natives = [m for ms in ti.methods.values() for m in ms if m.native and m.id]
            if not natives:
                continue
            binary = _binary_name(ti)
            for m in natives:
                overloaded = sum(1 for x in ti.methods.get(m.name, ()) if x.native) > 1
                sym = jni_symbol(binary, m.name)
                if overloaded:
                    desc = self._descriptor(m)
                    if desc is not None:
                        sym = jni_symbol(binary, m.name, desc)
                eid = f"ext:abi:{sym}"
                if eid not in self.graph.nodes:
                    self.node(Node(id=eid, kind=NodeKind.EXTERNAL, name=sym, qualname=sym,
                                   module="", meta={"lang": LANG, "abi_import": sym}))
                self.graph.nodes[m.id].meta["jni_symbol"] = sym
                self.edge(m.id, eid, EdgeKind.CALLS, m.decl.line if m.decl else 0,
                          ti.file.path, meta={"args": len(m.params), "jni": True})

    def _descriptor(self, m: MethodInfo) -> str | None:
        out = []
        chain = [m.owner] + self.idx.outer_chain(m.owner)
        for ref in m.params:
            t = self.idx.resolve_ref(ref, m.owner.file, chain, m.tparams)
            if t is None:
                return None
            out.append(_jvm_descriptor(t, self.idx))
        return "".join(out)

    # ---- tests, labels ---------------------------------------------------
    def _is_test_method(self, mi: MethodInfo, annos: list[str]) -> bool:
        if set(annos) & jdk.TEST_ANNOTATIONS:
            return True
        if mi.name.startswith("test") and not mi.static and mi.visibility == "public" \
                and not mi.params and mi.decl is not None:
            return self.idx.is_subtype(mi.owner, "TestCase")
        return False

    def _unchecked(self, name: str) -> bool:
        """Is exception type ``name`` (fqn) unchecked: RuntimeException, Error, or below?

        A project exception is unchecked when a project ancestor is one of those, or when
        the library class it extends is (``extends IllegalArgumentException``)."""
        if name not in self._unchecked_memo:
            ti = self.idx.types.get(name)
            if ti is None:
                out = bool(jdk.is_unchecked_name(name.rsplit(".", 1)[-1]))
            else:
                out = self.idx.is_subtype(ti, "RuntimeException") or \
                    self.idx.is_subtype(ti, "Error") or \
                    any(jdk.is_unchecked_name(e.rsplit(".", 1)[-1])
                        for e in self.idx.ext_ancestors(ti))
            self._unchecked_memo[name] = out
        return self._unchecked_memo[name]

    def _exception_chain(self, name: str) -> list[str]:
        ti = self.idx.types.get(name)
        if ti is None:
            return jdk.exception_chain(name.rsplit(".", 1)[-1])
        out = [ti.decl.name] + [a.decl.name for a in self.idx.ancestors(ti)]
        for e in self.idx.ext_ancestors(ti):
            out += jdk.exception_chain(e.rsplit(".", 1)[-1])
        return list(dict.fromkeys(out))

    def signals(self) -> None:
        state = self.state
        if state is None or not hasattr(state, "sig"):
            return
        test_files: set[str] = set()
        mains: set[str] = set()
        for nid in self.added:
            n = self.graph.nodes[nid]
            if n.kind is NodeKind.METHOD and ("test" in n.tags or "test-fixture" in n.tags):
                test_files.add(n.path)
            if n.kind is NodeKind.METHOD and n.name == "main" and "static" in n.tags \
                    and n.qualname.endswith("main(String[])"):
                mains.add(n.module)
        for nid in self.added:
            n = self.graph.nodes[nid]
            if n.kind is NodeKind.CLASS and n.name.endswith(("Test", "Tests", "TestCase", "IT")) \
                    and _under_test_dir(n.path):
                test_files.add(n.path)
        out_edges: dict[str, list[Edge]] = {}
        for e in self.graph.edges:
            if e.src in self.added:
                out_edges.setdefault(e.src, []).append(e)
        class_annos: dict[str, list[str]] = {}
        for nid in self.added:
            n = self.graph.nodes[nid]
            if n.kind is NodeKind.CLASS:
                class_annos.setdefault(n.module, []).extend(
                    a.split("(")[0].rsplit(".", 1)[-1] for a in n.meta.get("annotations", ()))
        for nid in self.added:
            n = self.graph.nodes[nid]
            if n.kind not in (NodeKind.MODULE, NodeKind.METHOD, NodeKind.CLASS_ATTR,
                              NodeKind.INSTANCE_ATTR):
                continue
            mod = n.module.split("@", 1)[-1]
            module_parts = tuple(mod.split("."))
            path_parts = tuple(n.path.split("/")) if n.path else ()
            if n.path in test_files:
                path_parts += ("tests",)
            sig = state.sig(nid)
            sig.module_parts, sig.path_parts = module_parts, path_parts
            if n.kind is NodeKind.MODULE:
                sig.has_main_guard = n.module in mains
                sig.own_statement_count = 1
                annos = class_annos.get(n.module, [])
                sig.decorators = tuple(jdk.FRAMEWORK_ANNOTATIONS.get(a, a.lower()) if a in
                                       jdk.FRAMEWORK_ANNOTATIONS else
                                       ("route" if a in ("Controller", "RestController", "Path")
                                        else a) for a in annos)
                sig.imported_roots = frozenset(i.split(".")[0].replace("static ", "")
                                               for i in n.meta.get("imports", ()))
                continue
            if n.kind is NodeKind.METHOD:
                f = self.facts.get(nid, {})
                annos = [a.split("(")[0].rsplit(".", 1)[-1] for a in n.meta.get("annotations", ())]
                sig.decorators = tuple(jdk.FRAMEWORK_ANNOTATIONS[a] for a in annos
                                       if a in jdk.FRAMEWORK_ANNOTATIONS) + tuple(annos)
                ar = n.meta.get("arity", {})
                sig.returns_value = ar.get("returns") not in ("void", "")
                sig.has_params = bool(ar.get("positional"))
                sig.own_statement_count = int(f.get("statements", 0))
                sig.calls_exit = bool(f.get("calls_exit"))
                sig.calls_open = bool(f.get("calls_open"))
                edges = out_edges.get(nid, [])
                sig.call_count = sum(1 for e in edges if e.kind is EdgeKind.CALLS)
                for e in edges:
                    tgt = self.graph.nodes.get(e.dst)
                    if tgt is None:
                        continue
                    if tgt.kind in (NodeKind.CLASS_ATTR, NodeKind.INSTANCE_ATTR):
                        if e.kind in (EdgeKind.WRITES, EdgeKind.MUTATES) and tgt.parent != n.parent:
                            sig.mutates_state = True
                            if tgt.kind is NodeKind.CLASS_ATTR:
                                sig.writes_global = True
                        elif e.kind in (EdgeKind.WRITES, EdgeKind.MUTATES) and \
                                tgt.kind is NodeKind.CLASS_ATTR:
                            sig.mutates_state = True
                            sig.writes_global = True
                        elif e.kind is EdgeKind.READS and tgt.kind is NodeKind.CLASS_ATTR \
                                and "final" not in tgt.tags:
                            sig.reads_global = True
                continue
            value = str(n.meta.get("value", ""))
            container = bool(re.match(r"\s*new\s+(java\.util\.)?(concurrent\.)?\w*(List|Map|Set|Queue|"
                                      r"Deque|Vector|Stack|Hashtable|Collection)\b", value))
            sig.is_mutable_container = container and n.kind is NodeKind.CLASS_ATTR
            sig.is_constant = ("final" in n.tags and n.kind is NodeKind.CLASS_ATTR
                               and not container) or "enum-constant" in n.tags


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
#: parsed files by content hash. A check builds the graph twice (the base revision and
#: the working tree), and most files are the same in both: parse each content once.
#: Syntax trees are never modified after parsing, so sharing them is safe.
_PARSED: OrderedDict[str, P.CompilationUnit | str] = OrderedDict()
_PARSED_MAX = 4096


def _parse(src: str, key: str) -> P.CompilationUnit | str:
    """The syntax tree of ``src``, or the parse error message."""
    hit = _PARSED.get(key)
    if hit is not None:
        _PARSED.move_to_end(key)
        return hit
    try:
        out: P.CompilationUnit | str = P.parse(src)
    except (P.ParseError, RecursionError) as exc:
        out = str(exc)
    _PARSED[key] = out
    if len(_PARSED) > _PARSED_MAX:
        _PARSED.popitem(last=False)
    return out


def _guess_package(src: str) -> str:
    m = re.search(r"^\s*package\s+([\w.]+)\s*;", src, re.M)
    return m.group(1) if m else ""


def _under_test_dir(path: str) -> bool:
    return any(p in ("test", "tests", "testing") for p in path.split("/")[:-1])


def _norm_doc(doc: str) -> str:
    return " ".join(doc.replace("/**", "").replace("*/", "").replace("*", " ").split())


def _decorators(annos: list[P.Annotation]) -> list[str]:
    return [a.text() for a in annos if a.name.rsplit(".", 1)[-1] not in jdk.COSMETIC_ANNOTATIONS]


def _class_signature(ti: TypeInfo, bases: list[str], permits: list[str]) -> str:
    d = ti.decl
    mods = " ".join(m for m in ("public", "protected", "private", "abstract", "static", "final",
                                "sealed", "non-sealed") if m in d.modifiers)
    kind = "@interface" if ti.kind == "annotation" else ti.kind
    s = f"{mods} {kind} {d.name}".strip()
    if d.type_params_text:
        s += d.type_params_text.replace(" ", "")
    if d.components:
        s += "(" + ", ".join(f"{p.type.text()} {p.name}" for p in d.components) + ")"
    if bases:
        s += " : " + ", ".join(b.rsplit(".", 1)[-1] for b in bases)
    if permits:
        s += " permits " + ", ".join(p.rsplit(".", 1)[-1] for p in permits)
    return s


def _method_signature(mi: MethodInfo, d: P.MethodDecl | None, ret: str,
                      throws: list[str]) -> str:
    mods = [mi.visibility] if mi.visibility != "package" else []
    for flag, word in ((mi.static, "static"), (mi.abstract and not mi.owner.is_interface,
                                                "abstract"), (mi.default, "default"),
                       (mi.final, "final"), (mi.native, "native")):
        if flag:
            mods.append(word)
    name = mi.owner.decl.name if mi.ctor else mi.name
    if d is not None:
        params = ", ".join(p.type.text().replace("...", "") + ("..." if p.varargs else "")
                           + " " + p.name for p in d.params)
    elif mi.ctor:
        params = ", ".join(f"{p.type.text()} {p.name}" for p in mi.owner.decl.components)
    else:
        params = ""
    head = " ".join(mods + ([d.type_params_text.replace(" ", "")] if d and d.type_params_text
                            else []) + ([ret] if ret else []))
    s = f"{head} {name}({params})".strip()
    if throws:
        s += " throws " + ", ".join(t.rsplit(".", 1)[-1] for t in throws)
    return s


def _binary_name(ti: TypeInfo) -> str:
    """``com.acme.Outer$Inner``: the name the JVM (and JNI) knows the class by."""
    names = [ti.decl.name]
    cur = ti.outer
    while cur is not None:
        names.append(cur.decl.name)
        cur = cur.outer
    pkg = ti.file.package
    return (pkg + "." if pkg else "") + "$".join(reversed(names))


def _jni_escape(s: str) -> str:
    out = []
    for ch in s:
        if ch == "_":
            out.append("_1")
        elif ch == ";":
            out.append("_2")
        elif ch == "[":
            out.append("_3")
        elif ch in "./":
            out.append("_")
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
        else:
            out.append(f"_0{ord(ch):04x}")
    return "".join(out)


def jni_symbol(binary_class: str, method: str, descriptor: str | None = None) -> str:
    """The C symbol a JNI ``native`` method binds to (JNI spec, "Resolving Native Method
    Names"): ``Java_`` + mangled class + ``_`` + mangled method [+ ``__`` + mangled args]."""
    sym = f"Java_{_jni_escape(binary_class)}_{_jni_escape(method)}"
    if descriptor is not None:
        sym += "__" + _jni_escape(descriptor)
    return sym


_PRIM_DESC = {"boolean": "Z", "byte": "B", "char": "C", "short": "S", "int": "I", "long": "J",
              "float": "F", "double": "D", "void": "V"}


def _jvm_descriptor(t: JType, idx: Index) -> str:
    prefix = "[" * t.dims
    if t.kind == "prim":
        return prefix + _PRIM_DESC.get(t.name, "V")
    if t.kind == "proj":
        ti = idx.types.get(t.name)
        name = _binary_name(ti) if ti is not None else t.name
    elif t.kind == "ext" and "." in t.name:
        name = t.name
    else:
        name = "java.lang.Object"                 # type variable: erased
    return prefix + "L" + name.replace(".", "/") + ";"
