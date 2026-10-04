"""COBOL frontend: programs, copybooks and record layouts as a Magellan graph.

Pure Python (no compiler needed): :mod:`magellan_lite.polyglot.cobol.source` handles fixed and free
reference format and tokenizes, :mod:`magellan_lite.polyglot.cobol.parser` expands COPY and recovers
the structure. This module maps it onto the shared node and edge kinds.

Mapping (COBOL -> NodeKind / EdgeKind)
--------------------------------------
======================================  ==============================================
PROGRAM-ID                              MODULE ``mod:cobol@PAYROLL`` (nested: CONTAINS)
the program as a callable               FUNCTION ``fn:cobol@PAYROLL`` (tag ``entry``);
                                        arity = PROCEDURE DIVISION USING / LINKAGE
ENTRY 'ALTNAME' USING ...               FUNCTION ``fn:cobol@ALTNAME`` (tag ``entry``)
SECTION, paragraph                      FUNCTION ``fn:cobol@PAYROLL.CALC-TAX``
                                        (``PAYROLL.SECT.PARA`` when a name repeats)
copybook                                MODULE ``mod:cobol@copy:CUSTREC`` (tag ``copybook``;
                                        its own namespace: a program may share the name)
01 / 77 / 66 item                       GLOBAL ``var:cobol@PAYROLL.WS-TOTALS``
02-49 item, 88 condition name           CLASS_ATTR ``var:cobol@PAYROLL.WS-TAX``, CONTAINed
                                        by its group. Ids use the data name alone (COBOL
                                        code names data that way, so moving a field into a
                                        group keeps its id) and the ``var:`` prefix at every
                                        level; a name that repeats in one owner is
                                        qualified by its named groups. Copybook items live
                                        under it (``var:cobol@copy:CUSTREC.CUST-ID``) and are
                                        shared by every program that COPYs it without
                                        REPLACING. All carry ``meta.storage = "program"``
                                        (each program has its own copy) unless EXTERNAL.
FD / SELECT file                        GLOBAL ``var:cobol@PAYROLL.PAYFILE`` (tag ``file``)
COPY X [REPLACING ...]                  IMPORTS program -> copybook
PERFORM X [THRU Y]                      CALLS to X and to every unit in the range
fall-through into the next paragraph    CALLS, ``meta.fallthrough`` + ``transfer``, conf. < 1
GO TO X / GO TO ... DEPENDING ON        CALLS, ``meta.goto`` + ``transfer`` (a jump, not a call)
ALTER X TO PROCEED TO Y                 CALLS X -> Y, dynamic
CALL 'LIT' USING a b                    CALLS -> ``fn:cobol@LIT`` (``meta.args`` = 2), or
                                        ``ext:abi:LIT`` with ``abi_import`` when no COBOL
                                        program has that name
CALL identifier                         CALLS to programs whose names are MOVEd to it,
                                        confidence 0.6, dynamic
EXEC CICS LINK / XCTL PROGRAM(...)      CALLS, like CALL (COMMAREA is the one argument)
MOVE/COMPUTE/ADD/... target             WRITES; sources READS; a write to a REDEFINES
                                        view also MUTATES the other view
SET cond-name TO TRUE                   WRITES the 88's parent field
READ / WRITE / REWRITE / DELETE         READS / WRITES the file node and ``ext:dd.<ddname>``
EXEC SQL                                READS / WRITES ``ext:sql.<TABLE>``; host variables
                                        are data reads/writes
EXEC CICS file / queue / map commands   READS / WRITES ``ext:cics.<file|queue|map>.<name>``
======================================  ==============================================

Hashes: a data item's ``sig_hash`` is its layout (PIC, USAGE, OCCURS, REDEFINES, SIGN,
nesting of its children: names excluded), ``body_hash`` its VALUE. A paragraph's
``body_hash`` covers its statements; a section's also its paragraph list. The program
entry's ``sig_hash`` covers the USING list and each LINKAGE parameter's layout. Sequence
and identification areas never reach any hash, so renumbering is not a change.

Upgrade signals are recorded as tags (``alter``, ``go-to``, ``go-to-depending``,
``perform-thru``, ``falls-through``, ``next-sentence``, ``redefines``, ``examine``...)
and summarised per program in ``meta["modernization"]``.
"""
from __future__ import annotations

import hashlib
import os
from collections import Counter, defaultdict
from pathlib import Path

from magellan_lite.polyglot.cobol import parser as P
from magellan_lite.polyglot.cobol.source import Source, read, tokenize
from magellan_lite.polyglot.core.hashing import file_hash
from magellan_lite.polyglot.core.model import Edge, EdgeKind, FileRecord, Graph, Node, NodeKind

LANG = "cobol"
SOURCE_SUFFIXES = (".cbl", ".cob", ".cpy", ".cobol", ".dcl", ".CBL", ".COB", ".CPY", ".COBOL", ".DCL",
                   ".Cbl", ".Cpy")
_SUFFIXES = frozenset(s.lower() for s in SOURCE_SUFFIXES)
_COPY_SUFFIXES = frozenset({".cpy", ".copy", ".dcl"})
SKIP_DIRS = {"node_modules", ".git", ".magellan", "__pycache__", "venv", ".venv", "build",
             "dist", "target"}
FALLTHROUGH_CONFIDENCE = 0.5
DYNAMIC_CALL_CONFIDENCE = 0.6
ALIAS_CONFIDENCE = 0.8

import re as _re
_PID = _re.compile(r"PROGRAM-ID\s*\.?\s*['\"]?([A-Za-z0-9#@$_-]+)", _re.I)
_ENTRY = _re.compile(r"\bENTRY\s+['\"]([A-Za-z0-9#@$_-]+)['\"]", _re.I)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _toks_text(toks) -> str:
    out = []
    for t in toks:
        if isinstance(t, list):
            out.append(_toks_text(t))
        else:
            out.append(t.up)
    return " ".join(out)


# --------------------------------------------------------------------------
def _walk(root: str | Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() in _SUFFIXES:
                yield os.path.join(dirpath, fn)


def has_sources(root: str | Path) -> bool:
    return next(_walk(root), None) is not None


def available() -> bool:
    """The parser is pure Python, so COBOL is always analyzable."""
    return True


def _read(path: str) -> str:
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# --------------------------------------------------------------------------
class _Builder:
    def __init__(self, graph: Graph, root: str | Path) -> None:
        self.g = graph
        self.root = Path(root)
        self.added: set[str] = set()
        self.notes: list[str] = []
        self.sources: dict[str, Source] = {}
        self.texts: dict[str, str] = {}
        self.lib = P.Library()
        self.lib.loader = self._load
        #: callable name (PROGRAM-ID / ENTRY) -> entry node id
        self.callables: dict[str, str] = {}
        self.program_key: dict[tuple[str, str], str] = {}   # (path, name) -> key
        self.signals: dict[str, dict] = defaultdict(dict)
        self.evaluate_sites: list[tuple[str, dict, dict]] = []
        self.dynamic_unresolved = 0
        self.missing: dict[str, list[str]] = defaultdict(list)
        #: per owner: data names that repeat, and named paths that still repeat
        self.ambiguous: dict[str, set[str]] = {}
        self.clashing: dict[str, set[tuple]] = {}
        self._pending_imports: list = []
        self._deferred_calls: list = []
        self._units: list = []
        self._unit_ids: dict[str, str] = {}
        self._moves: dict[int, list[tuple[str, tuple]]] = {}

    def _load(self, rel: str) -> str | None:
        if rel not in self.texts:
            try:
                self.texts[rel] = _read(str(self.root / rel))
            except OSError:
                return None
        return self.texts[rel]

    # -- nodes ---------------------------------------------------------------
    def node(self, n: Node) -> Node:
        n.meta.setdefault("lang", LANG)
        got = self.g.add_node(n)
        if got is n:
            self.added.add(n.id)
        return got

    def edge(self, src: str, dst: str, kind: EdgeKind, line: int = 0, path: str = "",
             **kw) -> None:
        if src not in self.g.nodes or dst not in self.g.nodes:
            return
        self.g.add_edge(Edge(src=src, dst=dst, kind=kind, lineno=line, path=path, **kw))

    def external(self, qual: str, **meta) -> str:
        eid = f"ext:{qual}"
        if eid not in self.g.nodes:
            meta.setdefault("lang", LANG)
            self.node(Node(id=eid, kind=NodeKind.EXTERNAL, name=qual.rsplit(".", 1)[-1],
                           qualname=qual, module="", meta=meta))
        return eid

    def _missing_copybook(self, name: str) -> str:
        """``ext:cobol@copy:NAME`` for a COPY whose member is not in the tree."""
        eid = f"ext:cobol@{P.COPYBOOK_KEY}{name}"
        if eid not in self.g.nodes:
            self.node(Node(id=eid, kind=NodeKind.EXTERNAL, name=name, qualname=eid[4:],
                           module="", meta={"copybook": True}))
        return eid

    # -- driver ---------------------------------------------------------------
    def run(self) -> None:
        rels = [os.path.relpath(p, self.root).replace(os.sep, "/") for p in _walk(self.root)]
        if not rels:
            return
        programs: list[str] = []
        pid_paths: dict[str, list[str]] = defaultdict(list)
        for rel in rels:
            text = self._load(rel) or ""
            ext = os.path.splitext(rel)[1].lower()
            pids = [] if ext in _COPY_SUFFIXES else _PID.findall(text)
            if pids:
                programs.append(rel)
                for p in dict.fromkeys(x.upper() for x in pids):
                    pid_paths[p].append(rel)
            else:
                self.lib.add(Path(rel).stem, rel)
        self.lib.finalize()
        # A PROGRAM-ID defined in several files: the one named after it keeps the plain
        # key (it is the member a CALL loads), else the shallowest. Generated or sample
        # copies elsewhere then never take the name over when they come and go.
        primary = {name: min(paths, key=lambda p: (Path(p).stem.upper() != name,
                                                   p.count("/"), p))
                   for name, paths in pid_paths.items()}

        # copybooks first: their items are shared nodes programs point at
        for name in sorted(self.lib.by_name):
            for rel in self.lib.by_name[name]:
                try:
                    self._copybook(rel)
                except Exception as exc:    # like programs: one bad member loses only itself
                    self.notes.append(f"{rel}: copybook mapping failed "
                                      f"({type(exc).__name__}: {exc})")
        parsed: list[tuple[str, Source, list[P.Program], list[P.CopyRef], set[str]]] = []
        for rel in programs:
            try:
                src = read(self.texts[rel], rel)
                toks = tokenize(src)
                copies: list[P.CopyRef] = []
                missing: set[str] = set()
                toks = P.expand(toks, self.lib, rel, copies, missing)
                progs = P.parse_units(toks, rel, self.notes)
            except Exception as exc:        # a parser bug must not lose the other files
                self.notes.append(f"{rel}: parse failed ({type(exc).__name__}: {exc})")
                self.g.files[rel] = FileRecord(path=rel, module="", sha256=file_hash(
                    self.texts.get(rel, "")), lines=self.texts.get(rel, "").count("\n") + 1,
                    parse_error=str(exc))
                continue
            self.sources[rel] = src
            for prog in progs:
                key = prog.name if primary.get(prog.name, rel) == rel else \
                    P.alt_key(prog.name, rel)
                if key in self.program_key.values():
                    key = P.alt_key(prog.name, rel) + f"-{prog.line}"
                self.program_key[(rel, prog.name)] = key
            parsed.append((rel, src, progs, copies, missing))
            first = progs[0].name if progs else Path(rel).stem.upper()
            self.g.files[rel] = FileRecord(
                path=rel, module=f"cobol@{self.program_key.get((rel, first), first)}",
                sha256=file_hash(src.normalized), lines=src.normalized.count("\n"))
        # every callable name first, so CALLs resolve regardless of file order
        for rel, src, progs, _c, _m in parsed:
            for prog in progs:
                self._declare_program(rel, prog)
        for rel, src, progs, copies, missing in parsed:
            for prog in progs:
                try:
                    self._program(rel, src, prog, copies)
                except Exception as exc:
                    self.notes.append(f"{rel}: {prog.name}: mapping failed "
                                      f"({type(exc).__name__}: {exc})")
            for name in missing:
                self.missing[name].append(rel)

    # -- copybooks ------------------------------------------------------------
    def _copybook(self, rel: str) -> None:
        key = self.lib.keys[rel]
        src = self.lib.source(rel)
        if src is None:
            return
        self.sources[rel] = src
        mid = f"mod:cobol@{key}"
        toks = self.lib.tokens(rel)
        self.g.files[rel] = FileRecord(path=rel, module=f"cobol@{key}",
                                       sha256=file_hash(src.normalized),
                                       lines=src.normalized.count("\n"))
        self.node(Node(id=mid, kind=NodeKind.MODULE, name=P.member_name(key),
                       qualname=f"cobol@{key}", module=f"cobol@{key}", path=rel, lineno=1,
                       end_lineno=src.normalized.count("\n"), tags=["copybook"],
                       body_hash=_h(_toks_text(toks)),
                       doc_hash=_h("\n".join(src.comments.values())),
                       meta={"copybook": True}))
        try:
            items, copies, is_data = P.parse_copybook(src, key, self.lib, self.notes)
        except Exception as exc:
            self.notes.append(f"{rel}: copybook parse failed ({type(exc).__name__}: {exc})")
            return
        self.g.nodes[mid].meta["kind"] = "data" if is_data else "code"
        for c in copies:
            dst = f"mod:cobol@{c.key}" if c.key else self._missing_copybook(c.name)
            self._pending_imports.append((mid, dst, c.line, c.path, c.replacing))
        P.assign_owners(items, key)
        self._note_ambiguous(key, _all(items))
        for it in _all(items):
            if it.owner == key:
                self._item_node(it, key, f"cobol@{key}", mid)
        self.signals[mid]["statements"] = len(items)

    # -- data items ------------------------------------------------------------
    def _item_id(self, it: P.DataItem, owner_key: str) -> str:
        """``var:cobol@OWNER.NAME``: COBOL code names data by its (unique) name.

        Moving a field into a group, or a 66-level becoming a group item, keeps its id,
        because every reference to it still resolves. Only names that repeat inside one
        owner are qualified, by their named ancestors (``WS-USER-NAME OF WS-GREETING``,
        as the code has to write it): an unnamed FILLER group above them has only a
        position, which shifts whenever another FILLER is added before it.
        """
        if it.is_filler:
            return f"var:cobol@{owner_key}." + ".".join(it.rel)
        if it.name in self.ambiguous.get(owner_key, ()):
            named = _named(it.rel)
            rel = it.rel if named in self.clashing.get(owner_key, ()) else named
            return f"var:cobol@{owner_key}." + ".".join(rel)
        return f"var:cobol@{owner_key}.{it.name}"

    def _note_ambiguous(self, owner_key: str, items: list[P.DataItem]) -> None:
        if owner_key in self.ambiguous:
            return
        mine = [it for it in items if it.owner == owner_key and not it.is_filler]
        seen = Counter(it.name for it in mine)
        self.ambiguous[owner_key] = {n for n, c in seen.items() if c > 1}
        paths = Counter(_named(it.rel) for it in mine if seen[it.name] > 1)
        self.clashing[owner_key] = {p for p, c in paths.items() if c > 1}

    def _item_node(self, it: P.DataItem, owner_key: str, module: str, module_id: str,
                   parent_id: str | None = None) -> str:
        nid = self._item_id(it, owner_key)
        if nid in self.g.nodes:
            return nid
        if it.is_filler and not it.children:
            return ""                     # unnamed storage: in its parent's layout only
        kind = NodeKind.GLOBAL if it.level in (1, 77, 66) else NodeKind.CLASS_ATTR
        if parent_id is None:
            parent_id = module_id
            if it.parent is not None and it.parent.owner == it.owner:
                parent_id = self._item_id(it.parent, owner_key)
        tags = []
        if it.is_condition:
            tags.append("condition-name")
        if it.redefines:
            tags.append("redefines")
        if it.renames:
            tags.append("renames")
        if it.occurs:
            tags.append("occurs-depending" if it.occurs[2] else "occurs")
        if it.section == "LINKAGE":
            tags.append("linkage")
        if it.fd:
            tags.append("file-record")
        if it.usage == "PACKED":
            tags.append("comp-3")
        layout = it.layout() if not it.is_condition else ""
        meta = {"storage": "external" if "EXTERNAL" in it.flags else "program",
                "level": it.level, "section": it.section, "annotation": it.own_layout(),
                "value": " ".join(it.values), "size": it.size, "offset": it.offset,
                "layout": layout}
        if it.pic:
            meta["pic"] = it.pic
        if it.usage:
            meta["usage"] = it.usage
        if it.occurs:
            meta["occurs"] = list(it.occurs)
        if it.redefines:
            meta["redefines"] = it.redefines
        if it.fd:
            meta["fd"] = it.fd
        if it.copy:
            meta["copybook"] = it.copy
        if it.is_condition:
            meta["values"] = [P.norm_value(v) if " THRU " not in v else
                              " THRU ".join(P.norm_value(x) for x in v.split(" THRU "))
                              for v in it.values]
        self.node(Node(
            id=nid, kind=kind, name=it.name if not it.is_filler else "FILLER",
            qualname=nid.split(":", 1)[1], module=module, path=it.path, lineno=it.line,
            end_lineno=max(it.end_line, it.line), col=it.col, parent=parent_id,
            public=it.section == "LINKAGE" or bool(it.copy), tags=tags,
            sig_hash=_h(layout if not it.is_condition else "88"),
            body_hash=_h(" ".join(it.values)), meta=meta))
        self.edge(parent_id, nid, EdgeKind.CONTAINS, it.line, it.path)
        return nid

    def _used_as(self, nid: str | None, what: str) -> None:
        """Mark a copybook item as crossing a program boundary (file, LINKAGE, CALL)."""
        node = self.g.nodes.get(nid or "")
        if node is None or "copybook" not in node.meta:
            return
        used = node.meta.setdefault("used_as", [])
        if what not in used:
            used.append(what)
            used.sort()

    # -- programs ----------------------------------------------------------------
    def _declare_program(self, rel: str, prog: P.Program) -> None:
        key = self.program_key[(rel, prog.name)]
        if key == prog.name:
            self.callables[prog.name] = f"fn:cobol@{key}"
        else:
            self.callables.setdefault(prog.name, f"fn:cobol@{key}")
        for para in ([prog.preamble] if prog.preamble else []) + prog.paragraphs:
            for e in para.entries:
                nm = e["name"].upper()
                if nm not in self.callables:
                    self.callables[nm] = f"fn:cobol@{nm}" if nm not in self.program_key.values() \
                        else f"fn:cobol@{key}.ENTRY-{nm}"

    def _program(self, rel: str, src: Source, prog: P.Program,
                 copies: list[P.CopyRef]) -> None:
        key = self.program_key[(rel, prog.name)]
        module = f"cobol@{key}"
        mid = f"mod:{module}"
        parent_mid = None
        if prog.parent:
            pk = self.program_key.get((rel, prog.parent))
            parent_mid = f"mod:cobol@{pk}" if pk else None
        comments = "\n".join(v for k, v in sorted(src.comments.items())
                             if prog.line <= k <= prog.end_line)
        units = prog.paragraphs
        shell = "|".join([
            _toks_text(prog.env_tokens),
            ",".join(c.name + ("*" if c.replacing else "") for c in copies
                     if prog.line <= c.line <= prog.end_line or c.path != rel),
            ",".join(("S:" if u.is_section else "") + u.name for u in units),
            ",".join(f"{n}:{f.clauses}" for n, f in sorted(prog.files.items())),
        ])
        flags = set()
        for u in units + ([prog.preamble] if prog.preamble else []):
            flags |= u.flags
        tags = ["cobol-program"]
        if "exec-cics" in flags:
            tags.append("cics")
        if "exec-sql" in flags:
            tags.append("sql")
        if prog.parent:
            tags.append("nested-program")
        self.node(Node(id=mid, kind=NodeKind.MODULE, name=prog.name, qualname=module,
                       module=module, path=rel, lineno=prog.line, end_lineno=prog.end_line,
                       parent=parent_mid, tags=tags, body_hash=_h(shell),
                       doc_hash=_h(comments),
                       meta={"program_id": prog.literal_name, "callable": True,
                             "files": sorted(prog.files)}))
        if parent_mid:
            self.edge(parent_mid, mid, EdgeKind.CONTAINS, prog.line, rel)

        # data
        roots = prog.items
        P.assign_owners(roots, key)
        items = _all(roots)
        self._note_ambiguous(key, items)
        item_ids: dict[int, str] = {}
        for it in items:
            if it.owner == key:
                item_ids[id(it)] = self._item_node(it, key, module, mid)
            else:
                cb_mid = f"mod:cobol@{it.owner}"
                if cb_mid not in self.g.nodes:
                    self.node(Node(id=cb_mid, kind=NodeKind.MODULE, name=P.member_name(it.owner),
                                   qualname=f"cobol@{it.owner}", module=f"cobol@{it.owner}",
                                   path=it.path, tags=["copybook"], meta={"copybook": True}))
                item_ids[id(it)] = self._item_node(it, it.owner, f"cobol@{it.owner}", cb_mid)
                if it.parent is None or it.parent.owner != it.owner:
                    if it.section == "LINKAGE":
                        self._used_as(item_ids[id(it)], "LINKAGE")
                    if it.fd or it.section == "FILE":
                        self._used_as(item_ids[id(it)], "file record")
        index = _Index(items, item_ids)
        file_ids: dict[str, str] = {}
        for fname, f in sorted(prog.files.items()):
            fid = f"var:{module}.{fname}"
            recs = [index.lookup((r,)) for r in f.records]
            layout = ";".join(self.g.nodes[r].meta.get("layout", "") for r in recs
                              if r and r in self.g.nodes)
            self.node(Node(id=fid, kind=NodeKind.GLOBAL, name=fname, qualname=fid[4:],
                           module=module, path=f.path, lineno=f.line, parent=mid,
                           tags=["file"], sig_hash=_h(f.clauses + "|" + layout),
                           body_hash=_h(f.assign),
                           meta={"storage": "program",
                                 "assign": f.assign, "organization": f.organization,
                                 "access": f.access, "records": f.records,
                                 "annotation": f.clauses, "value": f.assign}))
            self.edge(mid, fid, EdgeKind.CONTAINS, f.line, f.path)
            file_ids[fname] = fid
            for r in recs:
                if r:
                    self.edge(fid, r, EdgeKind.CONTAINS, f.line, f.path)

        # copybooks: import edges
        seen_cp: set[tuple[str, int]] = set()
        for c in copies:
            if c.context == "nested":
                continue
            if not (prog.line <= c.line <= prog.end_line) and c.path == rel:
                continue
            dst = f"mod:cobol@{c.key}" if c.key else self._missing_copybook(c.name)
            if (dst, c.line) in seen_cp:
                continue
            seen_cp.add((dst, c.line))
            self.edge(mid, dst, EdgeKind.IMPORTS, c.line, c.path,
                      meta={"copy": c.name, "replacing": c.replacing})

        # the program as a callable
        entry_id = f"fn:{module}"
        arity = self._arity(prog.using, prog.returning, index, prog)
        sig_text = "PROCEDURE DIVISION" + (
            " USING " + " ".join(("" if m == "REFERENCE" else f"BY {m} ") + ".".join(c)
                                 for m, c in prog.using) if prog.using else "") + (
            f" RETURNING {prog.returning[0]}" if prog.returning else "")
        pre = prog.preamble
        exports = list(dict.fromkeys([prog.literal_name, prog.name]))
        self.node(Node(id=entry_id, kind=NodeKind.FUNCTION, name=prog.name, qualname=module,
                       module=module, path=rel, lineno=prog.line, end_lineno=prog.end_line,
                       parent=mid, signature=sig_text, public=True,
                       tags=["entry", "program"],
                       sig_hash=_h(repr(sorted((k, v) for k, v in arity.items()
                                               if k != "names"))),
                       body_hash=_h(_toks_text(pre.tokens) if pre else ""),
                       meta={"arity": arity, "abi_exports": exports}))
        self.edge(mid, entry_id, EdgeKind.CONTAINS, prog.line, rel)
        if pre is not None:
            self._statements(pre, entry_id, prog, index, file_ids)

        # sections and paragraphs
        unit_ids: dict[str, str] = {}
        by_name: dict[str, list[P.Paragraph]] = defaultdict(list)
        for u in units:
            by_name[u.name].append(u)
        ordered: list[tuple[P.Paragraph, str]] = []
        cur_section_id = None
        for u in units:
            if u.is_section:
                uid = f"fn:{module}.{u.name}"
            elif len(by_name[u.name]) > 1 and u.section:
                uid = f"fn:{module}.{u.section}.{u.name}"
            else:
                uid = f"fn:{module}.{u.name}"
            if uid in self.g.nodes:
                uid = f"{uid}@{u.line}"
            parent = mid
            if u.is_section:
                cur_section_id = uid
            elif u.section and cur_section_id:
                parent = cur_section_id
            body = _toks_text(u.tokens)
            if u.is_section:
                body += "|" + ",".join(p.name for p in units if p.section == u.name
                                       and not p.is_section)
            ucomments = "\n".join(v for k, v in sorted(self.sources.get(
                u.path, src).comments.items()) if u.line <= k <= u.end_line)
            self.node(Node(
                id=uid, kind=NodeKind.FUNCTION, name=u.name,
                qualname=uid.split(":", 1)[1], module=module, path=u.path, lineno=u.line,
                end_lineno=max(u.end_line, u.line), col=u.col, parent=parent, public=False,
                tags=sorted({"section" if u.is_section else "paragraph"}
                            | {f for f in u.flags if not f.startswith("_")}
                            | ({"from-copybook"} if u.copy else set())),
                sig_hash=_h("section" if u.is_section else "paragraph"),
                body_hash=_h(body), doc_hash=_h(ucomments),
                signature=f"{u.name} {'SECTION' if u.is_section else ''}".strip() + ".",
                meta={"statements": u.statements,
                      "arity": {"lang": LANG, "positional": [], "required_positional": 0,
                                "keyword_only": [], "required_keyword_only": [],
                                "star_args": False, "star_kwargs": False, "defaults": []},
                      **({"copybook": u.copy} if u.copy else {})}))
            self.edge(parent, uid, EdgeKind.CONTAINS, u.line, u.path)
            unit_ids.setdefault(u.name, uid)
            if u.section:
                unit_ids[f"{u.section}.{u.name}"] = uid
            ordered.append((u, uid))
        self._units = ordered
        self._unit_ids = unit_ids
        if ordered:
            self.edge(entry_id, ordered[0][1], EdgeKind.CALLS, ordered[0][0].line, rel,
                      meta={"args": 0, "entry": True})
        for u, uid in ordered:
            self._statements(u, uid, prog, index, file_ids)
        if pre is not None:
            self._control(pre, entry_id, prog, index)
        for u, uid in ordered:
            self._control(u, uid, prog, index)
        self._fallthrough(ordered, prog, entry_id)

        # ENTRY points
        for u in ([pre] if pre else []) + units:
            for e in u.entries:
                nm = e["name"].upper()
                eid = self.callables.get(nm)
                if not eid or eid in self.g.nodes:
                    continue
                ea = self._arity([(a["mode"], a["chain"]) for a in e["args"]], (), index, prog)
                self.node(Node(id=eid, kind=NodeKind.FUNCTION, name=nm,
                               qualname=eid.split(":", 1)[1], module=module, path=e["path"],
                               lineno=e["line"], parent=mid, public=True,
                               tags=["entry"], signature=f"ENTRY '{e['name']}'",
                               sig_hash=_h(repr(sorted((k, v) for k, v in ea.items()
                                                       if k != "names"))),
                               body_hash=_h(""),
                               meta={"arity": ea, "abi_exports": [e["name"]]}))
                self.edge(mid, eid, EdgeKind.CONTAINS, e["line"], e["path"])
                owner = next((uid for uu, uid in ordered if uu is u), entry_id)
                self.edge(eid, owner, EdgeKind.CALLS, e["line"], e["path"],
                          meta={"args": 0, "entry": True})

        # summary for modernization planning
        summary: dict[str, int] = defaultdict(int)
        for u in units + ([pre] if pre else []):
            for f in u.flags:
                if f in _UPGRADE_FLAGS:
                    summary[f] += 1
        for it in items:
            if it.redefines:
                summary["redefines"] += 1
            if it.occurs and it.occurs[2]:
                summary["occurs-depending"] += 1
        falls = sum(1 for _, uid in ordered if "falls-through" in self.g.nodes[uid].tags)
        if falls:
            summary["falls-through"] = falls
        self.g.nodes[mid].meta["modernization"] = dict(sorted(summary.items()))
        self.g.nodes[mid].tags = sorted(set(self.g.nodes[mid].tags)
                                        | {f"uses-{k}" for k in summary})
        self.signals[mid]["statements"] = sum(u.statements for u in units)

    # -- arity -------------------------------------------------------------------
    def _arity(self, using: list, returning: tuple, index: "_Index",
               prog: P.Program) -> dict:
        names, layouts, sizes, modes = [], [], [], []
        cics = False
        if not using and any(it.name == "DFHCOMMAREA" and it.section == "LINKAGE"
                             for it in prog.items):
            using = [("REFERENCE", ("DFHCOMMAREA",))]
            cics = True
        for mode, chain in using:
            nid = index.lookup(chain)
            node = self.g.nodes.get(nid) if nid else None
            item = index.item(tuple(chain))
            names.append(".".join(chain))
            layouts.append(_h(node.meta.get("layout", "")) if node else "?")
            # OCCURS DEPENDING ON: the length is whatever the caller says it is
            variable = item is not None and any(x.occurs and x.occurs[2] for x in _all([item]))
            sizes.append(node.meta.get("size") if node and not variable else None)
            modes.append(mode)
        return {"lang": LANG, "positional": names, "required_positional": len(names),
                "keyword_only": [], "required_keyword_only": [], "star_args": False,
                "star_kwargs": False, "defaults": [], "layouts": layouts, "sizes": sizes,
                "by": modes, "returning": ".".join(returning), "cics": cics}

    # -- statements -> edges ------------------------------------------------------
    def _statements(self, u: P.Paragraph, uid: str, prog: P.Program, index: "_Index",
                    file_ids: dict[str, str]) -> None:
        sig = self.signals[uid]
        sig["statements"] = u.statements
        writes = False
        for r in u.refs:
            nid = index.lookup(r.chain)
            if nid is None:
                if r.chain and r.chain[0] in file_ids:
                    nid = file_ids[r.chain[0]]
                else:
                    continue
            node = self.g.nodes[nid]
            # a MOVE's sending and receiving fields share a statement key, so a rule can
            # pair them (a widened field moved into one that was not widened truncates)
            move = {"move": "%s:%d:%d" % r.stmt} if r.verb == "MOVE" and r.stmt else None
            if "r" in r.access:
                self.edge(uid, nid, EdgeKind.READS, r.line, r.path, conditional=r.cond,
                          context=self.g.nodes[uid].qualname, **({"meta": move} if move else {}))
            if "w" in r.access:
                target = nid
                if "condition-name" in node.tags and node.parent in self.g.nodes:
                    target = node.parent          # SET cond TO TRUE writes the field
                self.edge(uid, target, EdgeKind.WRITES, r.line, r.path, conditional=r.cond,
                          context=self.g.nodes[uid].qualname, **({"meta": move} if move else {}))
                writes = True
                for alias in index.aliases(target):
                    self.edge(uid, alias, EdgeKind.MUTATES, r.line, r.path,
                              conditional=r.cond, confidence=ALIAS_CONFIDENCE,
                              meta={"alias": "redefines"},
                              context=self.g.nodes[uid].qualname)
        sig["writes"] = writes
        io = False
        for op in u.io:
            fid = file_ids.get(op["name"])
            if fid is None:
                rec = index.lookup((op["name"],))
                fd = self.g.nodes[rec].meta.get("fd") if rec else None
                fid = file_ids.get(fd) if fd else None
            io = True
            if fid is None:
                continue
            kind = EdgeKind.READS if op["verb"] in ("READ", "RETURN", "START") else (
                EdgeKind.WRITES if op["verb"] in ("WRITE", "REWRITE", "DELETE", "RELEASE")
                else None)
            assign = self.g.nodes[fid].meta.get("assign")
            if kind is not None:
                self.edge(uid, fid, kind, op["line"], op["path"], conditional=op["cond"])
            if op["verb"] in ("READ", "RETURN"):
                # a READ without INTO lands in the FD record area
                for rec in self.g.nodes[fid].meta.get("records", ()):
                    rid = index.lookup((rec,))
                    if rid:
                        self.edge(uid, rid, EdgeKind.WRITES, op["line"], op["path"],
                                  conditional=op["cond"])
                if assign:
                    ext = self.external(f"dd.{assign}", dataset=assign)
                    self.edge(uid, ext, kind, op["line"], op["path"], conditional=op["cond"])
        for s in u.sql:
            io = True
            tables = s["tables"] or prog.cursors.get(s.get("cursor", ""), [])
            kind = EdgeKind.WRITES if s["op"] in ("INSERT", "UPDATE", "DELETE", "MERGE") \
                else EdgeKind.READS
            for t in tables:
                ext = self.external(f"sql.{t}", sql_table=t)
                self.edge(uid, ext, kind, s["line"], s["path"], conditional=s["cond"],
                          meta={"sql": s["op"]})
        for c in u.cics:
            base = c["cmd"].split()[0]
            if not c["resource"]:
                continue
            kind_word, _, name = c["resource"].partition(":")
            if kind_word == "TRANSID":
                continue
            held = index.item((name,))
            if held is not None:
                lits = [P._lit_value(v).strip() for v in held.values if v[:1] in "'\""]
                if len(lits) == 1 and lits[0]:
                    name = lits[0].upper()
            io = True
            ext = self.external(f"cics.{kind_word.lower()}.{name}", cics=kind_word.lower())
            reads = base in ("READ", "READNEXT", "READPREV", "STARTBR", "RECEIVE", "READQ")
            self.edge(uid, ext, EdgeKind.READS if reads else EdgeKind.WRITES, c["line"],
                      c["path"], conditional=c["cond"], meta={"cics": c["cmd"]})
        sig["io"] = io
        sig["exit"] = "stop-run" in u.flags
        for fact in u.evaluates:
            site = self._evaluate_site(fact, index)
            if site is not None:
                self.g.nodes[uid].meta.setdefault("evaluates", []).append(site)

    def _evaluate_site(self, fact: dict, index: "_Index") -> dict | None:
        subject = fact["subject"]
        whens = fact["whens"]
        field_id = None
        covered: set[str] = set()
        complex_ = any(w.get("complex") for w in whens)
        if subject in (["TRUE"],):
            parents: dict[str, set[str]] = defaultdict(set)
            for w in whens:
                if "name" in w:
                    nid = index.lookup(tuple(w["name"]))
                    node = self.g.nodes.get(nid) if nid else None
                    if node is not None and "condition-name" in node.tags and node.parent:
                        parents[node.parent].add(node.name)
                    else:
                        complex_ = True
            if not parents:
                return None
            field_id = max(sorted(parents), key=lambda p: len(parents[p]))
            covered = parents[field_id]
        elif subject not in (["FALSE"],):
            nid = index.lookup(tuple(subject))
            if nid is None:
                return None
            field_id = nid
            members = [self.g.nodes[e.dst] for e in self.g.out_edges(nid)
                       if e.kind is EdgeKind.CONTAINS and e.dst in self.g.nodes
                       and "condition-name" in self.g.nodes[e.dst].tags]
            if not members:
                return None
            handled = {v for w in whens for v in w.get("lits", ())}
            for m in members:
                vals = m.meta.get("values") or []
                if vals and all(v in handled for v in vals):
                    covered.add(m.name)
        else:
            return None
        return {"field": field_id, "covered": sorted(covered), "other": fact["other"],
                "line": fact["line"], "path": fact["path"], "complex": complex_,
                "subject": " ".join(subject)}

    def _unit(self, name: str) -> str | None:
        return self._unit_ids.get(name)

    def _control(self, u: P.Paragraph, uid: str, prog: P.Program, index: "_Index") -> None:
        order = [x[1] for x in self._units]
        pos = {x: k for k, x in enumerate(order)}
        calls = 0
        for pf in u.performs:
            first = self._unit(pf["first"])
            if first is None:
                continue
            members = [first]
            if pf["last"]:
                last = self._unit(pf["last"])
                if last is not None and pos.get(last, -1) >= pos.get(first, 0):
                    members = order[pos[first]:pos[last] + 1]
            for k, m in enumerate(members):
                meta: dict = {"args": 0, "perform": True}
                if pf["last"]:
                    meta["thru"] = [pf["first"], pf["last"]]
                    meta["range"] = [self.g.nodes[x].name for x in members]
                    if k:
                        meta["via"] = "thru"
                if pf["loop"]:
                    meta["loop"] = True
                self.edge(uid, m, EdgeKind.CALLS, pf["line"], pf["path"],
                          conditional=pf["cond"], col=pf["col"] + k,
                          context=self.g.nodes[uid].qualname, meta=meta)
                calls += 1
        altered = {a["src"] for pp, _ in self._units for a in pp.alters}
        for gt in u.gotos:
            for k, t in enumerate(gt["targets"]):
                dst = self._unit(t)
                if dst is None:
                    continue
                meta = {"args": 0, "goto": True, "transfer": True}
                if gt["depending"]:
                    meta["depending"] = gt["depending"]
                if gt.get("handler"):
                    meta["cics_handle"] = gt["handler"]
                self.edge(uid, dst, EdgeKind.CALLS, gt["line"], gt["path"],
                          conditional=gt["cond"], col=gt["col"] + k,
                          dynamic=bool(gt.get("handler")) or u.name in altered,
                          context=self.g.nodes[uid].qualname, meta=meta)
                calls += 1
        for a in u.alters:
            src, dst = self._unit(a["src"]), self._unit(a["dst"])
            if src and dst:
                self.edge(src, dst, EdgeKind.CALLS, a["line"], u.path, dynamic=True,
                          confidence=DYNAMIC_CALL_CONFIDENCE, conditional=True,
                          meta={"args": 0, "altered_by": self.g.nodes[uid].name,
                                "transfer": True})
        for c in u.calls:
            calls += 1
            self._call(uid, c, prog, index)
        self.signals[uid]["calls"] = calls

    def _call(self, uid: str, c: dict, prog: P.Program, index: "_Index") -> None:
        n_args = len(c["args"])
        sizes = []
        for a in c["args"]:
            nid = index.lookup(a["chain"]) if a.get("chain") and not a.get("special") else None
            sizes.append(self.g.nodes[nid].meta.get("size") if nid else None)
            self._used_as(nid, "COMMAREA" if c["kind"] != "call" else "CALL argument")
        meta = {"args": n_args, "callee": c["target"].upper(),
                "using": [".".join(a.get("chain") or ()) or a.get("literal", "") or
                          ("OMITTED" if a.get("omitted") else "") for a in c["args"]],
                "by": [a["mode"] for a in c["args"]], "arg_sizes": sizes}
        if c["kind"] != "call":
            meta["cics"] = c["kind"][5:]
        common = dict(conditional=c["cond"], col=c["col"],
                      context=self.g.nodes[uid].qualname)
        if c["literal"]:
            name = c["target"].upper()
            dst = self.callables.get(name)
            if dst:
                self._deferred_calls.append((uid, dst, c["line"], c["path"], meta, common,
                                             1.0, False))
            else:
                ext = self.external(f"abi:{c['target']}", abi_import=c["target"])
                self.g.nodes[ext].meta["abi_import"] = c["target"]
                self.edge(uid, ext, EdgeKind.CALLS, c["line"], c["path"], meta=meta, **common)
            return
        # CALL identifier: the program names this program ever puts in it
        chain = tuple(c["target"].split("."))
        item = index.item(chain)
        names: list[str] = []
        if item is not None:
            names += [P._lit_value(v) for v in item.values if v[:1] in "'\""]
            names += [lit for lit, target in self._literal_moves(prog)
                      if index.item(target) is item]
        names = list(dict.fromkeys(n.strip().upper() for n in names if n.strip()))
        if not names:
            self.dynamic_unresolved += 1
            self.g.nodes[uid].meta.setdefault("dynamic_calls", []).append(c["target"])
            return
        for k, name in enumerate(names):
            dst = self.callables.get(name)
            m = dict(meta, callee=name, dynamic_via=c["target"])
            cm = dict(common, col=c["col"] + k)
            if dst:
                self._deferred_calls.append((uid, dst, c["line"], c["path"], m, cm,
                                             DYNAMIC_CALL_CONFIDENCE, True))
            else:
                ext = self.external(f"abi:{name}", abi_import=name)
                self.edge(uid, ext, EdgeKind.CALLS, c["line"], c["path"], meta=m,
                          confidence=DYNAMIC_CALL_CONFIDENCE, dynamic=True, **cm)

    def _literal_moves(self, prog: P.Program) -> list[tuple[str, tuple]]:
        """Every ``MOVE 'LIT' TO X`` in the program, collected once for all its CALLs."""
        key = id(prog)
        if key not in self._moves:
            self._moves[key] = [m for pp in ([prog.preamble] if prog.preamble else [])
                                + prog.paragraphs for m in P.literal_moves(pp)]
        return self._moves[key]

    def _fallthrough(self, ordered: list[tuple[P.Paragraph, str]], prog: P.Program,
                     entry_id: str) -> None:
        """Control that runs off the end of one paragraph into the next.

        A unit is *sequential* when control can arrive there other than by a PERFORM
        that returns at its end: the start of the procedure, a GO TO / ALTER / CICS
        HANDLE label from elsewhere, or falling off a sequential unit above. Only a
        sequential unit's fall-through into the next section (or unsectioned
        paragraph) is a real transfer; within a section it is how the section runs.

        Two things keep this from overreaching:

        * a GO TO inside one section (``GO TO 1000-EXIT``) does not change how that
          section was entered, so it only makes its target sequential if the jump
          itself runs sequentially;
        * a unit that unconditionally PERFORMs something that never returns (a
          ``GET-ME-OUT-OF-HERE`` section ending in EXEC CICS RETURN / GOBACK) never
          reaches its own end;
        * what an EXEC CICS HANDLE ABEND LABEL routine falls into runs only when the
          task has abended and the routine did not end it. Those edges are kept, with
          ``meta["abend_exit"]``, but they are not the program's normal flow.
        """
        if not ordered:
            return
        ids = [uid for _, uid in ordered]
        pos = {uid: k for k, uid in enumerate(ids)}
        scope = {uid: (u.name if u.is_section else u.section) for u, uid in ordered}
        term = self._terminal_units(ordered)
        sequential: set[str] = set()
        abend: set[str] = set()
        structured: set[str] = set()
        inner_jumps: dict[str, set[str]] = defaultdict(set)
        pre = prog.preamble
        if pre is None or not pre.terminal:
            sequential.add(ids[0])
        for e in (x for src in ids + [entry_id] for x in self.g.out_edges(src)):
            if e.kind is not EdgeKind.CALLS or e.dst not in pos:
                continue
            if e.meta.get("goto") or e.meta.get("altered_by"):
                if e.meta.get("cics_handle") == "LABEL":
                    abend.add(e.dst)                # HANDLE ABEND LABEL(...)
                elif scope.get(e.src) and scope.get(e.src) == scope[e.dst] \
                        and not e.meta.get("altered_by"):
                    inner_jumps[e.src].add(e.dst)
                else:
                    sequential.add(e.dst)
            if e.meta.get("thru"):
                rng = e.meta.get("range") or []
                if self.g.nodes[e.dst].name != rng[-1]:
                    structured.add(e.dst)
        # a performed section runs its paragraphs in order
        for k, (u, uid) in enumerate(ordered[:-1]):
            nxt_u = ordered[k + 1][0]
            if u.is_section and nxt_u.section == u.name and not nxt_u.is_section:
                structured.add(uid)
            elif not u.is_section and u.section and nxt_u.section == u.section \
                    and not nxt_u.is_section:
                structured.add(uid)

        def spread(reached: set[str]) -> None:
            changed = True
            while changed:
                changed = False
                for k, uid in enumerate(ids):
                    if uid not in reached:
                        continue
                    reach = set(inner_jumps.get(uid, ()))
                    if k + 1 < len(ids) and uid not in term:
                        reach.add(ids[k + 1])
                    if not reach <= reached:
                        reached |= reach
                        changed = True
        spread(sequential)
        abend -= sequential
        spread(abend)
        abend -= sequential
        for k, (u, uid) in enumerate(ordered[:-1]):
            if uid in term:
                continue
            nxt = ids[k + 1]
            if uid in structured:
                self.edge(uid, nxt, EdgeKind.CALLS, u.end_line, u.path,
                          confidence=0.9, meta={"args": 0, "fallthrough": True,
                                                "implicit": True, "structured": True,
                                                "transfer": True})
            elif uid in sequential:
                self.edge(uid, nxt, EdgeKind.CALLS, u.end_line, u.path,
                          confidence=FALLTHROUGH_CONFIDENCE, conditional=True,
                          meta={"args": 0, "fallthrough": True, "implicit": True,
                                "transfer": True})
                node = self.g.nodes[uid]
                node.tags = sorted(set(node.tags) | {"falls-through"})
            elif uid in abend:
                self.edge(uid, nxt, EdgeKind.CALLS, u.end_line, u.path,
                          confidence=FALLTHROUGH_CONFIDENCE, conditional=True,
                          meta={"args": 0, "fallthrough": True, "implicit": True,
                                "transfer": True, "abend_exit": True})

    def _terminal_units(self, ordered: list[tuple[P.Paragraph, str]]) -> set[str]:
        """Units whose end control never reaches: they end in GOBACK / STOP RUN / an
        unconditional GO TO, or unconditionally PERFORM something that never returns."""
        term = {uid for u, uid in ordered if u.terminal}
        unit = {uid: u for u, uid in ordered}
        leaves = {"go-to", "go-to-depending", "_early-exit"}
        members: dict[str, list[tuple[P.Paragraph, str]]] = defaultdict(list)
        for u, uid in ordered:
            if u.is_section:
                members[u.name].append((u, uid))
            elif u.section:
                members[u.section].append((u, uid))

        def returns(uid: str) -> bool:
            """Can a PERFORM of ``uid`` come back?"""
            u = unit[uid]
            run = members.get(u.name, []) if u.is_section else [(u, uid)]
            for p, pid in run:
                if leaves & p.flags:
                    return True           # may leave by another way: assume it returns
                if pid in term:
                    return False
            return True

        changed = True
        while changed:
            changed = False
            for u, uid in ordered:
                if uid in term:
                    continue
                for pf in u.performs:
                    if pf["cond"] or pf["last"]:
                        continue
                    target = self._unit(pf["first"])
                    if target is not None and target != uid and not returns(target):
                        term.add(uid)
                        changed = True
                        break
        return term

    # -- finishing -----------------------------------------------------------------
    def finish(self, state) -> None:
        for src, dst, kind_line, path, replacing in self._pending_imports:
            self.edge(src, dst, EdgeKind.IMPORTS, kind_line, path,
                      meta={"replacing": replacing})
        for uid, dst, line, path, meta, common, conf, dyn in self._deferred_calls:
            if dst in self.g.nodes:
                self.edge(uid, dst, EdgeKind.CALLS, line, path, meta=meta, confidence=conf,
                          dynamic=dyn, **common)
            else:
                ext = self.external(f"abi:{meta['callee']}", abi_import=meta["callee"])
                self.edge(uid, ext, EdgeKind.CALLS, line, path, meta=meta, confidence=conf,
                          dynamic=dyn, **common)
        self._labels(state)

    def _labels(self, state) -> None:
        for nid in self.added:
            node = self.g.nodes.get(nid)
            if node is None or node.kind is NodeKind.EXTERNAL:
                continue
            s = self.signals.get(nid, {})
            sig = state.sig(nid)
            path_parts = tuple(node.path.split("/")) if node.path else ()
            sig.module_parts = (LANG, node.module.split("@", 1)[-1])
            sig.path_parts = path_parts
            if node.kind is NodeKind.MODULE:
                sig.own_statement_count = int(s.get("statements", 0))
                if "cics" in node.tags:
                    sig.decorators = ("handler",)    # a CICS transaction program
            elif node.kind.is_callable:
                edges = self.g.out_edges(nid)
                sig.own_statement_count = int(s.get("statements", 0))
                sig.call_count = sum(1 for e in edges if e.kind is EdgeKind.CALLS)
                sig.calls_open = bool(s.get("io"))
                sig.calls_exit = bool(s.get("exit"))
                sig.has_params = bool(node.meta.get("arity", {}).get("positional"))
                sig.mutates_state = bool(s.get("writes"))
                sig.writes_global = bool(s.get("writes"))
                sig.reads_global = any(e.kind is EdgeKind.READS for e in edges)
            elif node.kind.is_data:
                sig.is_constant = "condition-name" in node.tags


#: copybooks the compiler or middleware supplies (CICS, MQ, DB2, IMS)
_VENDOR_COPYBOOKS = ("DFH", "CMQ", "SQLCA", "SQLDA", "DLI", "CEE", "IMS")

_UPGRADE_FLAGS = frozenset({
    "alter", "go-to", "go-to-depending", "alterable-go-to", "perform-thru", "next-sentence",
    "examine", "transform", "exhibit", "ready-trace", "reset-trace", "note", "enter",
    "stop-literal", "dynamic-call", "cics-handle",
})


def _named(rel: tuple) -> tuple:
    """An item's path without the positional FILLER groups in it."""
    return tuple(seg for seg in rel if not seg.startswith("FILLER#"))


def _all(roots: list[P.DataItem]) -> list[P.DataItem]:
    out: list[P.DataItem] = []

    def walk(it: P.DataItem) -> None:
        out.append(it)
        for c in it.children:
            walk(c)
    for r in roots:
        walk(r)
    return out


class _Index:
    """Resolve ``A OF B OF C`` against a program's items (its own and its copybooks')."""

    def __init__(self, items: list[P.DataItem], ids: dict[int, str]) -> None:
        self.ids = ids
        self.by_name: dict[str, list[P.DataItem]] = defaultdict(list)
        for it in items:
            if not it.is_filler:
                self.by_name[it.name].append(it)
        self.redefined_by: dict[int, list[P.DataItem]] = defaultdict(list)
        siblings: dict[int, dict[str, P.DataItem]] = defaultdict(dict)
        roots_by_name = {it.name: it for it in items if it.parent is None}
        for it in items:
            scope = siblings[id(it.parent)] if it.parent is not None else roots_by_name
            if it.redefines:
                base = scope.get(it.redefines) if it.parent is not None else \
                    roots_by_name.get(it.redefines)
                if base is not None:
                    self.redefined_by[id(base)].append(it)
                    self.redefined_by[id(it)].append(base)
            if it.parent is not None:
                siblings[id(it.parent)][it.name] = it
        self._by_id = {v: k for k, v in ids.items()}
        self._items = {id(it): it for it in items}

    def item(self, chain: tuple) -> P.DataItem | None:
        if not chain:
            return None
        cands = self.by_name.get(chain[0], [])
        if len(chain) > 1:
            quals = list(chain[1:])

            def ok(it: P.DataItem) -> bool:
                anc = [a.name for a in it.ancestors()]
                k = 0
                for a in anc:
                    if k < len(quals) and a == quals[k]:
                        k += 1
                if k < len(quals) and it.fd == quals[-1]:
                    k += 1
                return k == len(quals)
            cands = [c for c in cands if ok(c)]
        return cands[0] if cands else None

    def lookup(self, chain: tuple) -> str | None:
        it = self.item(tuple(chain))
        return (self.ids.get(id(it)) or None) if it is not None else None

    def aliases(self, nid: str) -> list[str]:
        """Items sharing storage with ``nid`` through REDEFINES (it or an ancestor)."""
        key = self._by_id.get(nid)
        it = self._items.get(key) if key is not None else None
        out: list[str] = []
        while it is not None:
            for other in self.redefined_by.get(id(it), ()):
                oid = self.ids.get(id(other))
                if oid and oid != nid:
                    out.append(oid)
            it = it.parent
        return out


# --------------------------------------------------------------------------
def merge(state, root: str | Path) -> set[str]:
    """Add the COBOL programs and copybooks under ``root`` to the graph."""
    if not has_sources(root):
        return set()
    b = _Builder(state.graph, root)
    b.run()
    b.finish(state)
    if b.missing:
        vendor = sorted(n for n in b.missing if n.startswith(_VENDOR_COPYBOOKS))
        own = sorted(n for n in b.missing if not n.startswith(_VENDOR_COPYBOOKS))
        if own:
            b.notes.insert(0, "copybooks not found (their fields are unresolved): " + ", ".join(
                f"{n} ({len(b.missing[n])} program(s))" for n in own))
        if vendor:
            b.notes.append("system copybooks not in the tree (expected): " + ", ".join(vendor))
    n_prog = sum(1 for n in b.added if n.startswith("mod:")
                 and "cobol-program" in state.graph.nodes[n].tags)
    n_copy = sum(1 for n in b.added if n.startswith("mod:")
                 and "copybook" in state.graph.nodes[n].tags)
    state.graph.diagnostics.append(
        f"cobol: {n_prog} programs, {n_copy} copybooks, {len(b.added)} nodes"
        + (f", {b.dynamic_unresolved} dynamic CALL(s) unresolved" if b.dynamic_unresolved
           else ""))
    for note in b.notes[:50]:
        state.graph.diagnostics.append(f"cobol: {note}")
    if len(b.notes) > 50:
        state.graph.diagnostics.append(f"cobol: ... and {len(b.notes) - 50} more notes")
    return b.added

