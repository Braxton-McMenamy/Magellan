"""New code: checks on what a change adds, rather than what it edits.

From the full Magellan (``magellan/python/rules/newcode.py``). An edited function has callers
that might break; a new one has none, so the other rules have little to say about it -- yet
it is where an author (a person, or a model) who does not know the code base well leaves
things behind:

* ``near-duplicate``: the new function has the same shape as one the project already has.
  Shapes are compared on the syntax tree with local names and constants erased, keeping what
  is called, so a renamed copy matches and two functions that merely have the same length do
  not. Two copies drift: the next fix lands in one and not the other.
* ``new-unreferenced``: a new private function (``_name``) that nothing calls or mentions.
  A helper written but never wired in is usually a step the change forgot -- the validation
  that never runs -- or a leftover.

Tests are left out of both (test functions are alike on purpose), and so are small functions
(fewer than 30 syntax nodes: getters and one-liners are alike by nature), overrides of a
method with the same name, and methods of classes whose base comes from outside the project
(a framework may call them by name).
"""

from __future__ import annotations

import ast
import re

from magellan_lite.findings import Finding, change_rule
from magellan_lite.rules.common import functions, is_test_path

MIN_TOKENS = 30
SIMILAR = 0.82            # share of four-step sequences the two shapes have in common
SHINGLE = 4
_SAME_ON_PURPOSE = re.compile(r"^(__\w+__|visit_\w+|test_\w+|setUp|tearDown|run|main|handle|"
                              r"process|setup|teardown)$")


def tokens(fn: ast.AST) -> list[str]:
    """The function's shape: node types in order, with called and attribute names kept and
    local names and constant values erased."""
    out: list[str] = []
    body = list(getattr(fn, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]                             # the docstring is not the shape
    stack = list(reversed(body))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.Name, ast.arg)):
            out.append("N")
        elif isinstance(n, ast.Constant):
            out.append("C:" + type(n.value).__name__)
        elif isinstance(n, ast.Attribute):
            out.append("A:" + n.attr)
        elif isinstance(n, ast.Call):
            f = n.func
            out.append("call:" + (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "?")))
        else:
            out.append(type(n).__name__)
        stack.extend(reversed(list(ast.iter_child_nodes(n))))
    return out


def _shingles(toks: list[str]) -> frozenset:
    if len(toks) < SHINGLE:
        return frozenset({tuple(toks)})
    return frozenset(tuple(toks[i:i + SHINGLE]) for i in range(len(toks) - SHINGLE + 1))


def _similarity(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _added(ctx) -> list:
    return [c.after for c in ctx.changes
            if c.kind == "added" and c.after.kind in ("function", "method") and not c.after.lang
            and c.after.path.endswith(".py") and not is_test_path(c.after.path)]


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _copies(a: str, b: str, files: dict) -> bool:
    """Are the two files copies of one module -- a vendored or bundled copy, where one path
    ends with the other, or the same text twice? Then every function in them repeats, on
    purpose, and saying so a hundred times helps nobody."""
    if a == b:
        return False
    if a.endswith("/" + b) or b.endswith("/" + a):
        return True
    ta, tb = files.get(a), files.get(b)
    return ta is not None and ta.replace("\r\n", "\n") == (tb or "").replace("\r\n", "\n")


MAX_FINDINGS = 10
#: more new functions than this is a code base being imported, not a change to review
MAX_ADDED = 200


def _names(toks) -> set[str]:
    """What a shape calls and reads by name: a near copy calls most of the same things."""
    return {t.split(":", 1)[1] for t in toks if t.startswith(("call:", "A:"))}


def _mentions(text: str, names: set[str]) -> bool:
    return not names or sum(n in text for n in names) >= 0.7 * len(names)


@change_rule("near-duplicate", "low",
             fix="Call or extend the existing function, or say in a comment why the two must "
                 "differ.")
def near_duplicate(ctx):
    added = [d for d in _added(ctx) if not _SAME_ON_PURPOSE.match(d.short)]
    if not added or len(added) > MAX_ADDED:
        return
    fns = functions(ctx.after)
    mine: dict[str, tuple[tuple, frozenset, set]] = {}
    for d in added:
        if d.name in fns:
            toks = tuple(tokens(fns[d.name][1]))
            if len(toks) >= MIN_TOKENS:
                mine[d.name] = (toks, _shingles(list(toks)), _names(toks))
    if not mine:
        return
    lines: dict[str, list[str]] = {}
    best: dict[str, tuple[float, object]] = {}
    for other, (path, fn, _cls) in fns.items():
        if is_test_path(path) or _SAME_ON_PURPOSE.match(fn.name):
            continue
        text = ctx.after.files[path]
        if not any(_mentions(text, m[2]) for m in mine.values()):
            continue                                # text first: most files are no match
        body = "\n".join(lines.setdefault(path, text.splitlines())[fn.lineno - 1:fn.end_lineno])
        hopeful = [n for n, m in mine.items() if n != other and _mentions(body, m[2])]
        if not hopeful:
            continue
        o = ctx.after_defs.get(other)
        otoks = tuple(tokens(fn))
        if o is None or len(otoks) < MIN_TOKENS:
            continue
        theirs = None
        for name in hopeful:
            d = ctx.after_defs[name]
            toks, shingles, _ = mine[name]
            if not 0.6 * len(toks) <= len(otoks) <= len(toks) / 0.6:
                continue
            if o.kind == "method" and d.kind == "method" and o.short == d.short:
                continue                            # an override, or a sibling implementation
            if o.path == d.path and (d.line <= o.line <= d.end_line or o.line <= d.line <= o.end_line):
                continue                            # one inside the other
            if _copies(d.path, o.path, ctx.after.files):
                continue
            if theirs is None:
                theirs = _shingles(list(otoks))
            sim = 1.0 if otoks == toks else _similarity(shingles, theirs)
            if sim >= SIMILAR and sim > best.get(name, (0.0, None))[0]:
                best[name] = (sim, o)
    hits, reported = [], set()
    for name, (score, other) in sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0])):
        pair = frozenset((name, other.name))
        if pair in reported:
            continue
        reported.add(pair)
        hits.append((ctx.after_defs[name], other, score))
    for d, other, score in hits[:MAX_FINDINGS]:
        exact_copy = score >= 0.999
        where = "in the same file" if other.path == d.path else f"in {other.path}"
        yield Finding(
            "near-duplicate", "low",
            f"new {d.short}() {'repeats' if exact_copy else 'closely resembles'} "
            f"{other.short}() {where}",
            d.path, d.line,
            detail=(("The same steps and the same calls; only the names differ. "
                     if exact_copy else f"About {score:.0%} of their shape is the same. ")
                    + f"Two copies drift apart: the next fix lands in one and not the other "
                      f"({other.path}:{other.line})."))


def _external_base(tree: ast.Module, cls_name: str | None, project_classes: set[str]) -> bool:
    """Does the class inherit from something outside the project (a framework class that may
    call its methods by name)?"""
    if cls_name is None:
        return False
    short = cls_name.rsplit(".", 1)[-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == short:
            for b in node.bases:
                base = ast.unparse(b).split("[", 1)[0].rsplit(".", 1)[-1]
                if base not in ("object", "Generic", "Protocol", "ABC") \
                        and base not in project_classes:
                    return True
    return False


@change_rule("new-unreferenced", "low",
             fix="Call it where it was meant to be used, or delete it.")
def new_unreferenced(ctx):
    candidates = []
    for d in _added(ctx):
        if not d.short.startswith("_") or d.short.startswith("__"):
            continue
        if ctx.graph.callers(d.name) or any(i.target == d.name for i in ctx.graph.imports):
            continue
        candidates.append(d)
    if not candidates:
        return
    wanted = {d.short for d in candidates}
    mentions: dict[str, list[tuple[str, int]]] = {}
    strings: dict[str, set[str]] = {}
    for path, text in sorted(ctx.after.files.items()):
        if not path.endswith(".py") or not any(w in text for w in wanted):
            continue                                # text first: most files never mention it
        tree = ctx.after.tree(path)
        if tree is None:
            continue
        for n in ast.walk(tree):
            word = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) \
                else n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None
            if word in wanted:
                mentions.setdefault(word, []).append((path, n.lineno))
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and 3 <= len(n.value) < 40:
                strings.setdefault(path, set()).add(n.value)
    fns = functions(ctx.after, {d.path for d in candidates})
    project_classes = {d.short for d in ctx.after_defs.values() if d.kind == "class"}
    for d in candidates:
        elsewhere = [(p, ln) for p, ln in mentions.get(d.short, [])
                     if not (p == d.path and d.line <= ln <= d.end_line)]
        if elsewhere:
            continue
        if any(d.short.startswith(s) and s != d.short for s in strings.get(d.path, ())):
            continue                                # getattr(self, "_visit_" + kind) dispatch
        entry = fns.get(d.name)
        if entry is None or entry[1].decorator_list:
            continue                                # registered by a decorator
        if d.kind == "method" and _external_base(ctx.after.tree(d.path), entry[2],
                                                 project_classes):
            continue
        yield Finding(
            "new-unreferenced", "low",
            f"new {d.short}() is never called",
            d.path, d.line,
            detail=("Nothing in the project calls it, reads it or names it. A private helper "
                    "written but never wired in is usually a step this change forgot (the "
                    "check that never runs), or a leftover."))
