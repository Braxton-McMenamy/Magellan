"""The other languages: C, C++, Java, Fortran, COBOL, TypeScript/JavaScript, and Python 2.

Lite reads Python 3 itself (defs.py, graph.py). For everything else it uses the full
Magellan's frontends, vendored in ``magellan_lite/polyglot``. Each frontend reads files from a
directory, so a snapshot's files in those languages are written to a temporary one, mapped
there, and turned into what the rest of Lite already works with:

- ``definitions(snapshot)``: a ``Definition`` per function, method, class and global, named
  ``<lang>@<qualified name>`` (``c@parse.load``, ``java@geo.Shape.area(double,double)``) so it
  can never collide with a Python name, and one per COBOL copybook (a class: its fields are
  its members);
- ``edges(snapshot)``: who calls and reads whom, inside a language and across languages (a
  Java ``native`` method to its C function, a COBOL ``CALL`` to the program it names), and
  who copies each copybook;
- ``findings(ctx)``: what each language's own checks find in a change -- call sites a new
  signature breaks, deleted code still referenced, and the language's hazards (a COBOL
  copybook whose layout moved, a C ``goto fail``, a Fortran ``COMMON`` block that no longer
  lines up). ``rules/languages.py`` puts them on the checklist.

Python 2 files are read by ``source.Snapshot.tree`` through the same package (``py2.parse``),
so code waiting for its 2-to-3 upgrade is on the map too.

Nothing here runs the code it reads. A frontend that fails on a file adds an error to the
report; the check goes on without it.
"""

from __future__ import annotations

import shutil
import tempfile
import weakref
from dataclasses import dataclass, field
from pathlib import Path

from magellan_lite.defs import Definition
from magellan_lite.findings import Finding
from magellan_lite.graph import Edge
from magellan_lite.source import Snapshot

#: file suffix -> language, as the frontends declare them (tests/test_languages.py checks).
#: A ``.h`` header counts as C here; the C++ and Fortran frontends read it too when the
#: project has their sources.
LANGUAGE_OF: dict[str, str] = {
    **dict.fromkeys((".java",), "java"),
    **dict.fromkeys((".f", ".for", ".f77", ".ftn", ".fpp", ".f90", ".f95", ".f03", ".f08",
                     ".f18", ".f23", ".F", ".FOR", ".F77", ".FTN", ".FPP", ".F90", ".F95",
                     ".F03", ".F08", ".F18", ".F23", ".inc", ".INC", ".fi", ".fh"), "fortran"),
    **dict.fromkeys((".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h++", ".ipp",
                     ".tpp"), "cpp"),
    **dict.fromkeys((".c", ".h"), "c"),
    **dict.fromkeys((".cbl", ".cob", ".cpy", ".cobol", ".dcl", ".CBL", ".COB", ".CPY",
                     ".COBOL", ".DCL", ".Cbl", ".Cpy"), "cobol"),
    **dict.fromkeys((".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
                    "typescript"),
}
SUFFIXES = tuple(LANGUAGE_OF)
#: the language names a person reads
NAMES = {"python": "Python", "c": "C", "cpp": "C++", "java": "Java", "fortran": "Fortran",
         "cobol": "COBOL", "typescript": "TypeScript/JavaScript"}

_KINDS = {"function": "function", "method": "method", "class": "class",
          "global_var": "constant", "class_attr": "constant"}
_CALLS = {"calls": "calls", "instantiates": "calls", "reads": "reads", "writes": "reads",
          "mutates": "reads"}


def language(path: str) -> str:
    """``python`` for a ``.py`` file, else the language its suffix says ("" if none)."""
    if path.endswith(".py"):
        return "python"
    dot = path.rfind(".")
    return LANGUAGE_OF.get(path[dot:], "") if dot >= 0 else ""


def is_source(path: str) -> bool:
    """A file some frontend maps (Python included)."""
    return bool(language(path))


@dataclass
class Mapped:
    """One snapshot's files in the other languages, mapped."""
    graph: object                     # polyglot.core.model.Graph
    root: str                         # where its files were written (removed with the snapshot)
    defs: dict[str, Definition] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    ids: dict[str, str] = field(default_factory=dict)       # polyglot node id -> name
    errors: list[str] = field(default_factory=list)
    languages: set[str] = field(default_factory=set)


class _State:
    """What a frontend's ``merge(state, root)`` uses: the graph and labelling signals."""

    def __init__(self, graph) -> None:
        self.graph = graph
        self.signals: dict = {}

    def sig(self, node_id: str):
        from magellan_lite.polyglot.core.taxonomy import Signals
        return self.signals.setdefault(node_id, Signals())


def mapped(snapshot: Snapshot) -> Mapped | None:
    """The snapshot's non-Python files, mapped once and kept on the snapshot."""
    if hasattr(snapshot, "_polyglot"):
        return snapshot._polyglot
    files = {p: t for p, t in snapshot.files.items() if language(p) not in ("", "python")}
    snapshot._polyglot = _map(files) if files else None
    if snapshot._polyglot and snapshot._polyglot.root:
        # the files stay while the snapshot lives: the diff reads them again
        weakref.finalize(snapshot, shutil.rmtree, snapshot._polyglot.root, True)
    return snapshot._polyglot


def _map(files: dict[str, str]) -> Mapped:
    try:
        from magellan_lite.polyglot.analyze.frontends import all_frontends
        from magellan_lite.polyglot.analyze.interop import link
        from magellan_lite.polyglot.core.model import Graph
    except ImportError:                       # the website's engine has Python only, so far
        langs = sorted({NAMES[language(p)] for p in files})
        return Mapped(None, "", errors=[f"{', '.join(langs)} files skipped: this copy of "
                                        "Magellan Lite reads Python only"])

    root = Path(tempfile.mkdtemp(prefix="magellan-lite-"))
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")
    graph = Graph(str(root))
    out = Mapped(graph, str(root))
    state = _State(graph)
    for lang, mod in all_frontends():
        if not any(p.endswith(tuple(mod.SOURCE_SUFFIXES)) for p in files):
            continue
        if lang == "cpp" and not any(language(p) == "cpp" for p in files):
            continue                          # headers alone: C reads them
        try:
            if not mod.available():
                out.errors.append(f"{NAMES[lang]} files skipped: "
                                  + ("libclang is not installed (pip install libclang)"
                                     if lang == "cpp" else "Node.js with TypeScript is not installed"
                                     if lang == "typescript" else "its reader is unavailable"))
                continue
            mod.merge(state, str(root))
            out.languages.add(lang)
        except Exception as exc:               # noqa: BLE001 - one language never stops a check
            out.errors.append(f"{NAMES.get(lang, lang)}: could not map: {exc}")
    try:
        link(graph)
    except Exception as exc:                   # noqa: BLE001
        out.errors.append(f"cross-language links skipped: {exc}")
    out.errors += [d for d in graph.diagnostics if "error" in d.lower()]

    for node in graph.nodes.values():
        kind = _KINDS.get(node.kind.value)
        if node.kind.value == "module" and "copybook" in node.tags:
            kind = "class"                    # so "who copies ACCTREC?" has an answer
        if kind is None or node.path not in files:
            continue
        name = node.qualname
        lang = node.meta.get("lang") or language(node.path)
        out.ids[node.id] = name
        out.defs[name] = Definition(
            name=name, kind=kind, path=node.path, line=node.lineno or 1,
            end_line=max(node.end_lineno or node.lineno or 1, node.lineno or 1),
            signature=(node.signature or "") + (f" #{node.sig_hash}" if node.sig_hash else ""),
            body=node.body_hash or node.sig_hash or "",
            value=node.body_hash if kind == "constant" else "",
            lang=lang, label=node.name or name.rsplit(".", 1)[-1])
    seen = set()
    for e in graph.edges:
        kind = _CALLS.get(e.kind.value)
        src, dst = out.ids.get(e.src), out.ids.get(e.dst)
        if e.kind.value == "imports" and dst and out.defs[dst].kind == "class" and \
                "copybook" in graph.nodes[e.dst].tags:
            # COPY X: the program (whose module and entry share a name) or copybook reads X
            kind, src = "reads", src or (graph.nodes[e.src].qualname if e.src in graph.nodes
                                         and graph.nodes[e.src].qualname in out.defs else None)
        if not kind or not src or not dst or src == dst or (src, dst, kind, e.lineno) in seen:
            continue
        seen.add((src, dst, kind, e.lineno))
        out.edges.append(Edge(src, dst, kind, e.path or out.defs[src].path, e.lineno,
                              guess=e.confidence < 1.0, positional=int(e.meta.get("args", 0))))
    # a Fortran routine passed as an argument is called by the routine it is passed to: no
    # call there names it, so without this APPLY "calls nothing" and is missing from INTRST's
    # callers (a guess: what runs depends on what each caller passes)
    passed = {e.dst for e in graph.edges if (e.meta or {}).get("procedure_argument")}
    for t in sorted(passed & out.ids.keys()):
        for _, _, callee, _, no, inner in _procedure_calls(graph, t, lambda p: files.get(p, "")):
            src = out.ids.get(callee.id)
            if src and src != out.ids[t] and (src, out.ids[t], "calls", no) not in seen:
                seen.add((src, out.ids[t], "calls", no))
                out.edges.append(Edge(src, out.ids[t], "calls", callee.path, no, guess=True,
                                      positional=len(inner)))
    return out


def definitions(snapshot: Snapshot) -> dict[str, Definition]:
    m = mapped(snapshot)
    return dict(m.defs) if m else {}


def edges(snapshot: Snapshot) -> list[Edge]:
    m = mapped(snapshot)
    return list(m.edges) if m else []


def errors(snapshot: Snapshot) -> list[str]:
    m = mapped(snapshot)
    return list(m.errors) if m else []


# -- findings --------------------------------------------------------------------------------
#: every rule the other languages report: severity when the language gives none, blocking,
#: and what to do. The full Magellan's own words for each are in its findings.
RULES: dict[str, tuple[str, bool, str]] = {
    "struct-layout-change": ("high", True, "Rebuild everything that includes the header, and "
                             "check code that reads the struct as raw bytes."),
    "enum-values-shifted": ("high", True, "Add new members at the end, or give each one an "
                            "explicit value, so stored and exchanged numbers keep their meaning."),
    "unreachable-statement": ("high", True, "Put the guarded statements in braces: what is "
                              "indented under the if is not inside it."),
    "non-exhaustive-match": ("medium", False, "Handle the new member, or add a default that "
                             "fails loudly."),
    "unhandled-new-member": ("medium", False, "Handle the new member everywhere the type is "
                             "switched on."),
    "overload-rebinds-call": ("high", True, "Make the call pick the overload it means (a cast, "
                              "or a distinct name)."),
    "common-layout-mismatch": ("critical", True, "Declare the COMMON block the same way in "
                               "every unit: one INCLUDE file or a module."),
    "implicit-interface-arg-mismatch": ("critical", True, "Give the routine an explicit "
                                        "interface (a module), then fix the calls."),
    "intent-out-read-before-write": ("high", True, "Set the INTENT(OUT) argument before "
                                     "reading it, or make it INTENT(INOUT)."),
    "reads-unset-local": ("medium", False, "Give the variable a value on every path before "
                          "it is read."),
    "perform-thru-range-changed": ("high", True, "Check every PERFORM ... THRU that spans the "
                                   "paragraphs you moved or added."),
    "copybook-layout-changed": ("critical", True, "Recompile every program that copies the "
                                "copybook, and convert data written with the old layout."),
    "call-using-mismatch": ("critical", True, "Make every CALL ... USING pass what the "
                            "called program's LINKAGE SECTION now expects."),
    "move-truncates": ("high", True, "Widen the receiving field, or check the value fits "
                       "before the MOVE."),
    "fall-through-changed": ("medium", False, "Check the paragraphs control now falls "
                             "into."),
    "legacy-construct-introduced": ("low", False, "Prefer the structured form (EVALUATE, "
                                    "PERFORM, inline code) to GO TO and ALTER."),
}
_SEVERITIES = ("critical", "high", "medium", "low")


def findings(ctx) -> list[Finding]:
    """Everything the other languages find in this change, worked out once per check."""
    if hasattr(ctx, "_polyglot_findings"):
        return ctx._polyglot_findings
    b, a = mapped(ctx.before), mapped(ctx.after)
    b, a = (b if b and b.graph is not None else None), (a if a and a.graph is not None else None)
    ctx._polyglot_findings = _findings(b, a) if (b or a) else []
    return ctx._polyglot_findings


# -- Fortran: a routine passed as an argument ------------------------------------------------
_FIXED_FORM = (".f", ".for", ".f77", ".ftn", ".fpp", ".inc", ".fi", ".fh")


def _fortran_statements(text: str, fixed: bool) -> list[tuple[int, str]]:
    """``(first line, statement)`` with comments dropped and continuation lines joined."""
    out: list[tuple[int, str]] = []
    pending = False
    for no, line in enumerate(text.splitlines(), 1):
        if fixed:
            if not line.strip() or line[:1] in "cC*!dD":
                continue
            body = line[6:72]
            if len(line) > 5 and line[5] not in " 0" and out:
                out[-1] = (out[-1][0], out[-1][1] + " " + body)
                continue
            out.append((no, body))
        else:
            body = line.split("!", 1)[0].rstrip()
            if not body.strip():
                continue
            more = body.endswith("&")
            body = body.rstrip("&").lstrip().lstrip("&")
            if pending and out:
                out[-1] = (out[-1][0], out[-1][1] + " " + body)
            else:
                out.append((no, body))
            pending = more
    return out


def _call_args(stmt: str, name: str) -> list[str] | None:
    """The actual arguments of ``CALL name(...)`` in ``stmt`` (any case), or None."""
    import re
    m = re.search(r"\bCALL\s+" + re.escape(name) + r"\s*\((.*)\)\s*$", stmt, re.I)
    if not m:
        return None
    args, depth, cur = [], 0, ""
    for ch in m.group(1):
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
            continue
        depth += (ch == "(") - (ch == ")")
        cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def _procedure_calls(g, target_id: str, source):
    """Every call made to ``target`` through a procedure argument.

    ``CALL APPLY(INTRST, P, R, D, X)`` hands INTRST to APPLY, which runs it as ``CALL FN(P,
    R, D, X)``. No call names INTRST there, so the call graph and a Fortran 77 compiler both
    pass it by. This finds the parameter it lands in and every call made through it, as
    ``(passing edge, call edge, callee, dummy, line, arguments)``; ``source(path)`` is a
    file's text."""
    from magellan_lite.polyglot.core.model import EdgeKind
    name = (g.nodes[target_id].name or "").lower()
    for e in g.in_edges(target_id):
        if not (e.meta or {}).get("procedure_argument"):
            continue
        for c in g.out_edges(e.src):
            if c.kind is not EdgeKind.CALLS or c.lineno != e.lineno or c.dst not in g.nodes:
                continue
            callee = g.nodes[c.dst]
            params = ((callee.meta or {}).get("arity") or {}).get("positional") or []
            fixed = c.path.lower().endswith(_FIXED_FORM)
            stmt = next((st for no, st in _fortran_statements(source(c.path), fixed)
                         if no <= c.lineno and _call_args(st, callee.name) is not None
                         and no >= c.lineno - 20), None)
            args = _call_args(stmt, callee.name) if stmt else None
            if not args:
                continue
            slots = [i for i, a in enumerate(args) if a.strip().lower() == name]
            for k in slots:
                if k >= len(params):
                    continue
                cfixed = callee.path.lower().endswith(_FIXED_FORM)
                for no, st in _fortran_statements(source(callee.path), cfixed):
                    inner = _call_args(st, params[k])
                    if inner is not None and callee.lineno <= no <= (callee.end_lineno or no):
                        yield e, c, callee, params[k], no, inner


def _through_procedure_arguments(g, target_id: str, hard: list, root: str, label, affects,
                                 EdgeKind) -> list[Finding]:
    """Calls made to ``target`` through a procedure argument, judged against its new signature
    (a wrapper with the old argument list, passed instead, is the usual fix)."""
    target = g.nodes[target_id]
    out: list[Finding] = []
    seen: set[tuple[str, int]] = set()

    def source(path: str) -> str:
        try:
            return (Path(root) / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    for e, c, callee, dummy, no, inner in _procedure_calls(g, target_id, source):
        if (callee.path, no) in seen:
            continue
        site = {"positional": len(inner), "keywords": set(), "star": False,
                "dstar": False, "known": True, "handled": []}
        hits = [h for h in hard if affects(h, site, 0)]
        if hits:
            seen.add((callee.path, no))
            out.append(Finding(
                "signature-break", "critical",
                f"{label(g, callee.id)} calls {label(g, target_id)} through its "
                f"procedure argument {dummy.upper()} the old way: {hits[0]['text']}",
                callee.path, no,
                detail=(f"{label(g, e.src)} passes {target.name.upper()} to "
                        f"{callee.name.upper()} ({c.path}:{c.lineno}), which calls it "
                        f"as {dummy.upper()}. Fortran checks neither."),
                fix=(f"Pass a wrapper with the old argument list, or have "
                     f"{callee.name.upper()} pass the new argument.")))
    return out


def _copiers(g, copybook_id: str) -> list[str]:
    """Every COBOL program that copies the copybook, directly or through other copybooks."""
    names, seen, todo = set(), {copybook_id}, [copybook_id]
    while todo:
        for e in g.in_edges(todo.pop()):
            src = g.nodes.get(e.src)
            if e.kind.value != "imports" or src is None or e.src in seen:
                continue
            seen.add(e.src)
            if "copybook" in src.tags:
                todo.append(e.src)
            elif "cobol-program" in src.tags:
                names.add(src.name)
    return sorted(names)


def _findings(b: Mapped | None, a: Mapped | None) -> list[Finding]:
    from magellan_lite.polyglot.analyze.frontends import language_findings
    from magellan_lite.polyglot.core.diff import (ChangeKind, affects, call_shape, diff_graphs,
                                                  surviving_references)
    from magellan_lite.polyglot.core.model import EdgeKind, Graph

    old = b.graph if b else Graph()
    new = a.graph if a else Graph()
    label = lambda g, nid: (g.nodes[nid].qualname.split("@", 1)[-1]   # noqa: E731
                            if nid in g.nodes else nid)
    cs = diff_graphs(old, new)
    out: list[Finding] = []

    for ch in cs.changes:
        # a signature some call sites can no longer satisfy: each such call site
        if ch.kind in (ChangeKind.SIGNATURE_CHANGED, ChangeKind.RENAMED) and ch.specs:
            hard = [s for s in ch.specs if s.get("hard")]
            for e in new.in_edges(ch.node_id):
                if e.kind not in (EdgeKind.CALLS, EdgeKind.INSTANTIATES):
                    continue
                hits = [s for s in hard if affects(s, call_shape(e), 0)]
                if hits:
                    out.append(Finding(
                        "signature-break", "critical",
                        f"{label(new, e.src)} calls {label(new, ch.node_id)} the old way: "
                        f"{hits[0]['text']}", e.path, e.lineno,
                        detail="The call compiled against the old signature; it no longer "
                               "matches.",
                        fix="Update the call, or keep the old signature next to the new one."))
            # passed as an argument (EXTERNAL INTRST; CALL APPLY(INTRST, ...)): the calls that
            # matter are the ones made through the parameter, inside the routine it is passed to
            if a and new.nodes.get(ch.node_id) is not None and \
                    (new.nodes[ch.node_id].meta or {}).get("lang") == "fortran":
                out += _through_procedure_arguments(new, ch.node_id, hard, a.root, label, affects,
                                                    EdgeKind)
        # deleted, and code nobody edited still uses it
        elif ch.kind is ChangeKind.REMOVED and ch.breaks:
            root = a.root if a else None
            for e in surviving_references(old, new, ch.node_id, source_root=root):
                where = new.nodes.get(e.src)
                out.append(Finding(
                    "removed-still-referenced", "critical",
                    f"{label(old, e.src)} still uses {label(old, ch.node_id)}, which this "
                    f"change deletes",
                    where.path if where else e.path,
                    e.lineno if where is None or where.lineno <= e.lineno <= where.end_lineno
                    else where.lineno,
                    fix="Keep it, or move every user to its replacement first."))

    try:
        theirs = language_findings(old, new, cs, a.root if a else (b.root if b else "."))
    except Exception as exc:                   # noqa: BLE001
        theirs = []
        new.diagnostics.append(f"language findings skipped: {exc}")
    for f in theirs:
        # Lite's checklist says how serious each rule is (RULES): the verdict blocks only on
        # high or critical findings of blocking rules
        ours = RULES.get(f.rule)
        severity = ours[0] if ours else f.severity if f.severity in _SEVERITIES else "low"
        detail = f.detail
        if f.rule == "copybook-layout-changed" and f.node_id in new.nodes:
            # its fix says "recompile every program listed": list them, all of them
            progs = _copiers(new, f.node_id)
            if progs:
                detail += f" Copied by {len(progs)} program(s): {', '.join(progs)}."
        out.append(Finding(f.rule, severity, f.title, f.path, f.lineno or 1,
                           detail=detail, fix=f.suggestion or (ours[2] if ours else "")))

    unique, seen = [], set()
    for f in out:
        if (f.rule, f.path, f.line) not in seen:
            seen.add((f.rule, f.path, f.line))
            unique.append(f)
    return unique
