"""Java break semantics.

Two entry points, both called by core:

:func:`arity_breaks` -- what a signature change does to *call sites*, for a method
whose id (name and erased parameter types) did not change. Java has no keyword
arguments and no defaults, and overloads are separate ids, so what is left is:

* the return type changed (hard: binary-incompatible, and callers using the value);
* a checked exception was added to ``throws`` (hard, but only for call sites that
  neither catch it nor declare it: test ``unhandled-exception``);
* a checked exception was removed (soft: a ``catch`` for it may become unreachable);
* ``static`` <-> instance (hard);
* varargs became a plain array (hard for calls that pass a different count);
* the generic parameter types changed under the same erasure (soft).

:func:`findings` -- breaks that are not about call sites, which the core diff cannot
see because they live in the type hierarchy or in ``switch`` statements:

* ``signature-break``: an abstract method added to an interface or abstract class
  that concrete implementers do not implement (a ``default`` method is fine); a class
  or method made ``final`` with subclasses/overriders; a class made ``abstract``
  that is instantiated; ``static`` <-> instance on an overridden method; visibility
  narrowed below what existing referrers need (checked per referrer: private is the
  top-level class, package-private the package, protected adds subclasses); a sealed
  type's ``permits`` shrunk under a subclass;
* ``unhandled-new-member``: a new enum constant or permitted subtype of a sealed type,
  and a ``switch`` that handled every previous one and has no ``default``. A switch
  expression or pattern switch no longer compiles (critical); an old-style switch
  statement silently does nothing for the new value (high);
* ``overload-rebinds-call``: a new overload that existing calls now bind to instead of
  the one they used to call (compiles, changes behaviour: medium).
"""

from __future__ import annotations

from magellan_lite.polyglot.core.findings import Finding
from magellan_lite.polyglot.core.model import EdgeKind, Graph, Node, NodeKind
from magellan_lite.polyglot.java import parser as P

LANG = "java"
_VIS_RANK = {"private": 0, "package": 1, "protected": 2, "public": 3}
_ALWAYS = "always"


# --------------------------------------------------------------------------
# call-site breaks
# --------------------------------------------------------------------------
def arity_breaks(old: dict, new: dict) -> list[dict]:
    out: list[dict] = []
    if not old or not new:
        return out
    o_types, n_types = list(old.get("types", [])), list(new.get("types", []))
    if o_types != n_types:
        # only reached for a move or rename between two overloads
        out.append({"hard": True, "test": "positional-count", "counts": [len(o_types)],
                    "text": f"parameter types changed ({', '.join(o_types)}) -> "
                            f"({', '.join(n_types)}) -- calls compiled against the old "
                            f"signature no longer bind to it"})
        return out
    o_ret, n_ret = old.get("returns", ""), new.get("returns", "")
    generified = "<" not in o_ret and _erase(o_ret) == _erase(n_ret)
    if o_ret != n_ret and not new.get("constructor") and not generified:
        # raw List -> List<String> is generification: same erasure and a raw caller still
        # compiles, so it breaks nothing (List<String> -> List<Object> below is soft)
        same_erasure = _erase(o_ret) == _erase(n_ret)
        hard = o_ret not in ("void", "") and not same_erasure
        out.append({"hard": hard, "test": _ALWAYS,
                    "text": f"return type changed from {o_ret or 'void'} to {n_ret or 'void'}"
                            + (" -- callers using the result no longer compile, and compiled "
                               "callers fail to link (NoSuchMethodError)" if hard else
                               " (same erasure) -- callers relying on the old type arguments "
                               "may need casts" if same_erasure else
                               " -- compiled callers fail to link until recompiled")})
    o_chk = set(old.get("checked_throws", ()))
    n_chk = list(new.get("checked_throws", ()))
    chains = new.get("throws_chain", {}) or {}
    for exc in n_chk:
        if exc in o_chk:
            continue
        chain = [c.rsplit(".", 1)[-1] for c in chains.get(exc, [exc])]
        if any(c in {x.rsplit(".", 1)[-1] for x in o_chk} for c in chain[1:]):
            continue                              # a subclass of something already thrown
        simple = exc.rsplit(".", 1)[-1]
        out.append({"hard": True, "test": "unhandled-exception", "accepts": chain,
                    "name": simple,
                    "text": f"now throws checked {simple} -- callers that neither catch nor "
                            f"declare it no longer compile"})
    for exc in sorted(o_chk - set(n_chk)):
        simple = exc.rsplit(".", 1)[-1]
        out.append({"hard": False, "test": _ALWAYS,
                    "text": f"no longer throws {simple} -- a catch clause for it around the "
                            f"call can become unreachable (a compile error if nothing else "
                            f"in the try throws it)"})
    if bool(old.get("static")) != bool(new.get("static")):
        now = "static" if new.get("static") else "an instance method"
        out.append({"hard": True, "test": _ALWAYS,
                    "text": f"became {now} -- compiled callers fail with "
                            f"IncompatibleClassChangeError"
                            + ("" if new.get("static") else
                               ", and Type.method() calls no longer compile")})
    n = len(n_types)
    if old.get("star_args") and not new.get("star_args"):
        out.append({"hard": True, "test": "positional-count",
                    "counts": [c for c in range(0, n + 16) if c != n],
                    "text": "varargs became a plain array -- calls passing the elements "
                            "individually no longer compile"})
    o_txt, n_txt = old.get("type_text", []), new.get("type_text", [])
    if len(o_txt) == len(n_txt):
        for i, (a, b) in enumerate(zip(o_txt, n_txt)):
            if a != b and a.replace("...", "[]") != b.replace("...", "[]"):
                out.append({"hard": False, "test": _ALWAYS,
                            "text": f"parameter {i + 1} type changed {a} -> {b} (same erasure) "
                                    f"-- callers passing the old generic type may no longer "
                                    f"compile"})
    if old.get("type_params", "") != new.get("type_params", "") and old.get("type_params"):
        out.append({"hard": False, "test": _ALWAYS,
                    "text": f"type parameters changed {old.get('type_params')} -> "
                            f"{new.get('type_params') or 'none'} -- explicit type arguments "
                            f"and inference at call sites may change"})
    return out


def still_names(text: str, word: str, target: Node, referrer: Node) -> bool:
    """Does ``text`` (an edited referrer's current source) still use ``target``?

    Core's word match counts any occurrence. Java names a method only by calling it or
    through a method reference, and a constructor only through ``new``, ``::new``,
    ``this(...)`` or ``super(...)``, so a parameter called ``type`` is not a use of a
    deleted ``type(Type)``, nor ``Var.create(...)`` of a deleted ``Var(String, Iterable)``.
    Matched on tokens, so comments and string literals do not count either.
    """
    if target.kind is NodeKind.METHOD and target.name == "<init>" \
            and referrer.kind is NodeKind.CLASS:
        return True                           # enum constants call it as NAME(args)
    toks = P.tokenize(text)
    for i, t in enumerate(toks):
        prev = toks[i - 1].text if i else ""
        nxt = toks[i + 1].text if i + 1 < len(toks) else ""
        if target.kind is not NodeKind.METHOD:
            if t.kind == "ident" and t.text == word:
                return True
        elif target.name != "<init>":
            if t.kind == "ident" and t.text == word and (nxt == "(" or prev == "::"):
                return True
        elif t.kind == "kw" and t.text in ("this", "super") and nxt == "(" and prev != ".":
            return True
        elif t.kind == "kw" and t.text == "new":
            j = i + 1                         # new a.b.Word<...>(
            while j + 1 < len(toks) and toks[j].kind == "ident" and toks[j + 1].text == ".":
                j += 2
            if toks[j].kind == "ident" and toks[j].text == word:
                return True
        elif t.kind == "ident" and t.text == word and nxt in ("::", "<"):
            j = i + 1                         # Word::new, Word<T>::new
            depth = 0
            while j < len(toks) and (depth or toks[j].text == "<"):
                depth += {"<": 1, ">": -1}.get(toks[j].text, 0)
                j += 1
            if j + 1 < len(toks) and toks[j].text == "::" and toks[j + 1].text == "new":
                return True
    return False


def _erase(t: str) -> str:
    """``Map<String, V>[]`` -> ``Map[]``; type text as recorded in the arity."""
    depth, out = 0, []
    for ch in t:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out).replace("...", "[]").rsplit(".", 1)[-1] if "<" in t else \
        t.replace("...", "[]").rsplit(".", 1)[-1]


# --------------------------------------------------------------------------
# hierarchy and dispatch-site breaks
# --------------------------------------------------------------------------
def findings(before: Graph | None, after: Graph, cs, root: str) -> list[Finding]:
    if before is None or cs is None:
        return []
    ctx = _Ctx(before, after, cs)
    if not any(n.meta.get("lang") == LANG for n in after.nodes.values()):
        return []
    out: list[Finding] = []
    out += ctx.new_abstract_methods()
    out += ctx.final_and_abstract()
    out += ctx.visibility()
    out += ctx.permits_shrunk()
    out += ctx.new_members()
    out += ctx.rebinding()
    return out


def _is_java(n: Node | None) -> bool:
    return n is not None and n.meta.get("lang") == LANG


def _top_class(g: Graph, n: Node) -> Node | None:
    cur: Node | None = n
    last = None
    while cur is not None and cur.kind is not NodeKind.MODULE:
        if cur.kind is NodeKind.CLASS:
            last = cur
        cur = g.nodes.get(cur.parent) if cur.parent else None
    return last


def _owner_class(g: Graph, n: Node) -> Node | None:
    cur = g.nodes.get(n.parent) if n.parent else None
    while cur is not None and cur.kind is not NodeKind.CLASS:
        cur = g.nodes.get(cur.parent) if cur.parent else None
    return cur


def _package(g: Graph, n: Node) -> str:
    mod = g.nodes.get(f"mod:{n.module}")
    return str(mod.meta.get("package", "")) if mod is not None else ""


def _vis(n: Node) -> str:
    if n.kind.is_callable:
        return str((n.meta.get("arity") or {}).get("visibility", "public"))
    return str(n.meta.get("visibility", "public"))


def _msig(n: Node) -> str:
    ar = n.meta.get("arity") or {}
    return f"{n.name}({','.join(ar.get('types', []))})"


class _Ctx:
    def __init__(self, before: Graph, after: Graph, cs) -> None:
        self.before = before
        self.after = after
        self.cs = cs
        self.edited = set(cs.files_modified + cs.files_added)

    def where(self, n: Node) -> str:
        return f"{n.qualname.split('@', 1)[-1]} at {n.path}:{n.lineno}" + (
            "  (file was edited)" if n.path in self.edited else "")

    def finding(self, rule: str, severity: str, n: Node, title: str, detail: str,
                evidence: list[str], suggestion: str) -> Finding:
        if len(evidence) > 12:
            evidence = evidence[:12] + [f"... and {len(evidence) - 12} more"]
        return Finding(rule=rule, severity=severity, title=title, detail=detail,
                       node_id=n.id, label=n.qualname, path=n.path, lineno=n.lineno,
                       evidence=evidence, suggestion=suggestion)

    # ---- hierarchy helpers -------------------------------------------------
    def subclasses(self, g: Graph, cls_id: str) -> list[Node]:
        out: list[Node] = []
        stack, seen = [cls_id], {cls_id}
        while stack:
            cid = stack.pop()
            for e in g.in_edges(cid):
                if e.kind is EdgeKind.INHERITS and e.src not in seen:
                    seen.add(e.src)
                    sub = g.nodes.get(e.src)
                    if sub is not None and sub.kind is NodeKind.CLASS:
                        out.append(sub)
                        stack.append(e.src)
        return out

    def supers(self, g: Graph, cls_id: str) -> list[Node]:
        out: list[Node] = []
        stack, seen = [cls_id], {cls_id}
        while stack:
            cid = stack.pop()
            for e in g.out_edges(cid):
                if e.kind is EdgeKind.INHERITS and e.dst not in seen:
                    seen.add(e.dst)
                    sup = g.nodes.get(e.dst)
                    if sup is not None and sup.kind is NodeKind.CLASS:
                        out.append(sup)
                        stack.append(e.dst)
        return out

    def methods(self, g: Graph, cls_id: str) -> list[Node]:
        return [c for c in g.children(cls_id) if c.kind is NodeKind.METHOD]

    def implements(self, g: Graph, cls: Node, m: Node) -> bool:
        """Does ``cls`` (or anything it inherits) provide a concrete ``m``?"""
        want = _msig(m)
        count = len((m.meta.get("arity") or {}).get("types", []))
        for c in [cls] + self.supers(g, cls.id):
            for x in self.methods(g, c.id):
                if x.id == m.id or x.name != m.name or "abstract" in x.tags:
                    continue
                xa = x.meta.get("arity") or {}
                if _msig(x) == want or len(xa.get("types", [])) == count:
                    return True
        return False

    def concrete(self, n: Node) -> bool:
        return "abstract" not in n.tags and "interface" not in n.tags

    # ---- rules ---------------------------------------------------------------
    def new_abstract_methods(self) -> list[Finding]:
        out: list[Finding] = []
        a = self.after
        for ch in self.cs.changes:
            n = a.nodes.get(ch.node_id)
            if not _is_java(n) or n.kind is not NodeKind.METHOD or "abstract" not in n.tags:
                continue
            kind = ch.kind.value
            if kind == "added":
                pass
            elif kind == "signature_changed":
                o = self.before.nodes.get(n.id)
                if o is None or "abstract" in o.tags:
                    continue
            else:
                continue
            owner = a.nodes.get(n.parent or "")
            if owner is None or owner.id not in self.before.nodes:
                continue                     # a new type: nothing could implement it before
            broken = [s for s in self.subclasses(a, owner.id)
                      if self.concrete(s) and not self.implements(a, s, n)]
            anon = [e for e in a.in_edges(owner.id)
                    if e.kind is EdgeKind.INSTANTIATES and e.meta.get("anonymous")
                    and _msig(n) not in (e.meta.get("defines") or ())]
            functional = False
            ob = self.before.nodes.get(owner.id)
            if ob is not None and "interface" in ob.tags:
                functional = sum(1 for x in self.methods(self.before, ob.id)
                                 if "abstract" in x.tags) == 1
            if not broken and not anon and not functional:
                continue
            what = "interface" if "interface" in owner.tags else "abstract class"
            ev = [f"{self.where(s)} does not implement {_msig(n)}" for s in broken]
            ev += [f"anonymous {owner.name} at {e.path}:{e.lineno} must implement it too"
                   for e in anon]
            if functional:
                ev.append(f"{owner.name} was a functional interface: every lambda and method "
                          f"reference targeting it stops compiling")
            out.append(self.finding(
                "signature-break", "critical" if broken or functional else "high", n,
                f"New abstract method {_msig(n)} in {what} {owner.name} breaks "
                f"{len(broken) + len(anon)} implementer(s)",
                f"Every concrete class implementing {owner.name} must now implement "
                f"{_msig(n)}; the ones below do not, so they no longer compile.",
                ev, "Give the method a default implementation (a `default` method in an "
                    "interface), or implement it in each class listed."))
        return out

    def final_and_abstract(self) -> list[Finding]:
        out: list[Finding] = []
        a, b = self.after, self.before
        for nid, n in a.nodes.items():
            if not _is_java(n) or nid not in b.nodes:
                continue
            o = b.nodes[nid]
            if n.kind is NodeKind.CLASS:
                if "final" in n.tags and "final" not in o.tags and "record" not in n.tags \
                        and "enum" not in n.tags:
                    subs = [s for s in self.subclasses(a, nid)
                            if any(e.dst == nid for e in a.out_edges(s.id)
                                   if e.kind is EdgeKind.INHERITS)]
                    if subs:
                        out.append(self.finding(
                            "signature-break", "critical", n,
                            f"{n.name} became final but {len(subs)} class(es) extend it",
                            "A final class cannot be subclassed.",
                            [self.where(s) for s in subs],
                            "Keep the class non-final, or remove the subclasses."))
                if "abstract" in n.tags and "abstract" not in o.tags:
                    sites = [e for e in a.in_edges(nid) if e.kind is EdgeKind.INSTANTIATES
                             and not e.meta.get("anonymous") and not e.meta.get("ref")
                             and e.confidence >= 1.0]
                    if sites:
                        out.append(self.finding(
                            "signature-break", "critical", n,
                            f"{n.name} became abstract but is instantiated at {len(sites)} "
                            f"site(s)", "An abstract class cannot be instantiated with `new`.",
                            [f"{a.nodes[e.src].qualname.split('@', 1)[-1]} at "
                             f"{e.path}:{e.lineno}" for e in sites if e.src in a.nodes],
                            "Instantiate a concrete subclass at those sites."))
            elif n.kind is NodeKind.METHOD:
                overriders = [a.nodes[e.src] for e in a.in_edges(nid)
                              if e.kind is EdgeKind.OVERRIDES and e.src in a.nodes]
                if not overriders:
                    continue
                oa, na = o.meta.get("arity") or {}, n.meta.get("arity") or {}
                if na.get("final") and not oa.get("final"):
                    out.append(self.finding(
                        "signature-break", "critical", n,
                        f"{_msig(n)} became final but {len(overriders)} method(s) override it",
                        "A final method cannot be overridden.",
                        [self.where(x) for x in overriders],
                        "Keep it overridable, or remove the overrides."))
                if bool(na.get("static")) != bool(oa.get("static")):
                    out.append(self.finding(
                        "signature-break", "critical", n,
                        f"{_msig(n)} changed between static and instance with "
                        f"{len(overriders)} override(s)",
                        "An instance method cannot override a static one, nor the reverse.",
                        [self.where(x) for x in overriders],
                        "Change the overrides with it."))
        return out

    def accessible(self, target: Node, ref: Node, vis: str) -> bool:
        a = self.after
        if vis == "public":
            return True
        t_top, r_top = _top_class(a, target), _top_class(a, ref)
        if vis == "private":
            return t_top is not None and r_top is not None and t_top.id == r_top.id
        same_pkg = _package(a, target) == _package(a, ref)
        if vis == "package" or same_pkg:
            return same_pkg
        # protected: subclasses of the declaring class, wherever they are
        owner = target if target.kind is NodeKind.CLASS else _owner_class(a, target)
        rcls = ref if ref.kind is NodeKind.CLASS else _owner_class(a, ref)
        while rcls is not None:
            if owner is not None and (rcls.id == owner.id or
                                      owner.id in {s.id for s in self.supers(a, rcls.id)}):
                return True
            rcls = _owner_class(a, rcls)
        return False

    def visibility(self) -> list[Finding]:
        out: list[Finding] = []
        a, b = self.after, self.before
        kinds = (NodeKind.METHOD, NodeKind.CLASS, NodeKind.CLASS_ATTR, NodeKind.INSTANCE_ATTR)
        for ch in self.cs.changes:
            if ch.kind.value not in ("signature_changed", "annotation_changed", "bases_changed"):
                continue
            n, o = a.nodes.get(ch.node_id), b.nodes.get(ch.node_id)
            if not _is_java(n) or o is None or n.kind not in kinds:
                continue
            ov, nv = _vis(o), _vis(n)
            if _VIS_RANK.get(nv, 3) >= _VIS_RANK.get(ov, 3):
                continue
            bad = []
            for e in a.in_edges(n.id):
                if e.kind in (EdgeKind.CONTAINS,) or e.confidence < 1.0 or e.dynamic:
                    continue
                r = a.nodes.get(e.src)
                if r is None or not _is_java(r):
                    continue
                if not self.accessible(n, r, nv):
                    bad.append(f"{r.qualname.split('@', 1)[-1]} {e.kind.value} it at "
                               f"{e.path}:{e.lineno}" + ("  (file was edited)"
                                                         if e.path in self.edited else ""))
            if bad:
                out.append(self.finding(
                    "signature-break", "critical", n,
                    f"{n.qualname.split('@', 1)[-1]} narrowed from {ov} to {nv} with "
                    f"{len(bad)} reference(s) that can no longer see it",
                    f"{nv.capitalize()} access does not reach the code below.", bad,
                    "Keep the wider visibility, or move the referrers."))
        return out

    def permits_shrunk(self) -> list[Finding]:
        out: list[Finding] = []
        a, b = self.after, self.before
        for nid, n in a.nodes.items():
            if not _is_java(n) or n.kind is not NodeKind.CLASS or nid not in b.nodes:
                continue
            old = set(b.nodes[nid].meta.get("permits") or [])
            new = set(n.meta.get("permits") or [])
            if not old or not new:
                continue
            gone = old - new
            subs = [s for s in self.subclasses(a, nid)
                    if s.qualname.split("@", 1)[-1] in gone
                    and any(e.dst == nid for e in a.out_edges(s.id) if e.kind is EdgeKind.INHERITS)]
            if subs:
                out.append(self.finding(
                    "signature-break", "critical", n,
                    f"sealed {n.name} no longer permits {len(subs)} subclass(es) that extend it",
                    "A class may extend a sealed type only if the type permits it.",
                    [self.where(s) for s in subs], "Permit them again, or change them."))
        return out

    # ---- enum constants / permitted subtypes and switch sites -------------------
    def new_members(self) -> list[Finding]:
        out: list[Finding] = []
        a, b = self.after, self.before
        grown: dict[str, tuple[list[str], list[str], str]] = {}
        for nid, n in a.nodes.items():
            if not _is_java(n) or n.kind is not NodeKind.CLASS or nid not in b.nodes:
                continue
            o = b.nodes[nid]
            if "enum" in n.tags:
                old = list(o.meta.get("constants") or [])
                new = [c for c in n.meta.get("constants") or [] if c not in old]
                if new and old:
                    grown[nid] = (old, new, "constant")
            elif n.meta.get("permits") and o.meta.get("permits"):
                old = list(o.meta.get("permits") or [])
                new = [p for p in n.meta.get("permits") or [] if p not in old]
                if new:
                    grown[nid] = (old, new, "permitted subtype")
        if not grown:
            return out
        sites: dict[str, list[tuple[Node, dict]]] = {}
        for n in a.nodes.values():
            for sw in n.meta.get("switches") or ():
                if sw.get("subject") in grown:
                    sites.setdefault(sw["subject"], []).append((n, sw))
        for subject, (old, new, what) in grown.items():
            owner = a.nodes[subject]
            for holder, sw in sites.get(subject, ()):
                labels = set(sw.get("labels") or [])
                if what == "constant":
                    covered = set(old) <= labels
                    missing = [c for c in new if c not in labels]
                else:
                    def handled(p: str) -> bool:     # a label the resolver could not qualify
                        return p in labels or p.rsplit(".", 1)[-1] in labels
                    covered = all(handled(p) for p in old)
                    missing = [p for p in new if not handled(p)]
                if not covered or not missing:
                    continue
                if sw.get("default"):
                    severity = "low"
                    effect = "falls through to the default branch"
                elif sw.get("expr") or sw.get("patterns"):
                    severity = "critical"
                    effect = "no longer compiles: the switch is not exhaustive"
                else:
                    severity = "high"
                    effect = "silently does nothing for it"
                names = ", ".join(m.rsplit(".", 1)[-1] for m in missing)
                out.append(Finding(
                    rule="unhandled-new-member", severity=severity,
                    title=f"switch over {owner.name} at {holder.path}:{sw.get('line')} does not "
                          f"handle new {what} {names}",
                    detail=f"The switch handled every previous {what} of {owner.name}; with "
                           f"{names} it {effect}.",
                    node_id=holder.id, label=holder.qualname, path=holder.path,
                    lineno=int(sw.get("line") or holder.lineno),
                    evidence=[f"handles {', '.join(sorted(labels))}",
                              f"in {holder.qualname.split('@', 1)[-1]}"
                              + ("  (file was edited)" if holder.path in self.edited else "")],
                    suggestion=f"Add a case for {names}, or a default that says what should "
                               f"happen."))
        return out

    # ---- new overloads that capture existing calls ------------------------------
    def rebinding(self) -> list[Finding]:
        out: list[Finding] = []
        a, b = self.after, self.before
        for ch in self.cs.changes:
            if ch.kind.value != "added":
                continue
            m = a.nodes.get(ch.node_id)
            if not _is_java(m) or m.kind is not NodeKind.METHOD or "synthetic" in m.tags:
                continue
            hits = []
            for e in a.in_edges(m.id):
                if e.kind is not EdgeKind.CALLS or e.confidence < 1.0 or e.meta.get("dispatch"):
                    continue
                src_new, src_old = a.nodes.get(e.src), b.nodes.get(e.src)
                if src_new is None or src_old is None or src_new.body_hash != src_old.body_hash:
                    continue                     # an edited caller chose its call itself
                delta = src_new.lineno - src_old.lineno
                for oe in b.out_edges(e.src):
                    if oe.kind is not EdgeKind.CALLS or oe.col != e.col or \
                            oe.lineno != e.lineno - delta or oe.dst == m.id:
                        continue
                    was = b.nodes.get(oe.dst)
                    if was is None or was.kind is not NodeKind.METHOD or was.name != m.name \
                            or oe.confidence < 1.0 or was.id not in a.nodes:
                        continue                 # a library method, or one that was removed
                    hits.append(f"{src_new.qualname.split('@', 1)[-1]} at {e.path}:{e.lineno} "
                                f"called {_msig(was)}, now {_msig(m)}"
                                + ("  (file was edited)" if e.path in self.edited else ""))
                    break
            if hits:
                out.append(self.finding(
                    "overload-rebinds-call", "medium", m,
                    f"New overload {_msig(m)} captures {len(hits)} existing call(s)",
                    "Overload resolution happens at compile time: these calls compile "
                    "unchanged but now run a different method.",
                    hits, "Check that the new overload is what those calls should run, or "
                          "give it a different name."))
        return out
