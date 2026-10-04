"""regex-catastrophic-backtracking: a regular expression whose matching time can explode.

Cloudflare, July 2 2019: a new WAF rule's regular expression ended in ``.*(?:.*=.*)``. On a
request with no match, a backtracking engine tries every way of sharing the text between the
``.*`` parts before giving up; the work grows with a power of the input's length. The rule
ran on every HTTP request worldwide, the CPUs serving them went to nearly 100%, and Cloudflare
was down for 27 minutes.

Python's ``re`` backtracks the same way. The rule reads regex literals passed to
``re.compile``, ``re.search`` and friends with Python's own regex parser -- it never runs
them -- and looks for the two classic shapes:

* exponential: an unbounded repeat inside an unbounded repeat with nothing else that must
  match, ``(a+)+`` or ``(\\w+\\s?)*``: a run of the same character can be split between them in
  exponentially many ways;
* polynomial: unbounded parts one after another that can match the same characters, ``.*.*``
  (Cloudflare's had four), with something after them that can fail.

Only reported when something after the ambiguous part can make the match fail: if the first
attempt always succeeds, there is nothing to backtrack over.
"""

from __future__ import annotations

import ast
import re

try:                                                    # Python 3.11+
    from re import _constants as C, _parser as P
except ImportError:                                     # Python 3.10
    import sre_constants as C                           # type: ignore[no-redef]
    import sre_parse as P                               # type: ignore[no-redef]

from magellan_lite.findings import rule

_FUNCTIONS = {"compile", "match", "search", "fullmatch", "findall", "finditer", "sub", "subn",
              "split"}
_ALPHA = frozenset(range(128))                          # ASCII is enough to see overlaps
_NL = 10
_DIGIT = frozenset(range(48, 58))
_WORD = _DIGIT | frozenset(range(65, 91)) | frozenset(range(97, 123)) | {95}
_SPACE = frozenset({9, 10, 11, 12, 13, 32})
_CATEGORIES = {
    C.CATEGORY_DIGIT: _DIGIT, C.CATEGORY_NOT_DIGIT: _ALPHA - _DIGIT,
    C.CATEGORY_WORD: _WORD, C.CATEGORY_NOT_WORD: _ALPHA - _WORD,
    C.CATEGORY_SPACE: _SPACE, C.CATEGORY_NOT_SPACE: _ALPHA - _SPACE,
}
_REPEATS = (C.MAX_REPEAT, C.MIN_REPEAT)                 # possessive repeats never backtrack
_ATOMIC = getattr(C, "ATOMIC_GROUP", None)


class _Pattern:
    def __init__(self, flags: int) -> None:
        self.dotall = bool(flags & re.DOTALL)
        self.nocase = bool(flags & re.IGNORECASE)

    # -- what an item can match ----------------------------------------------------------
    def chars(self, items) -> frozenset:
        out = set()
        for op, av in items:
            out |= self.item_chars(op, av)
        if self.nocase:
            out |= {ord(chr(c).swapcase()) for c in out if chr(c).isalpha() and ord(chr(c).swapcase()) < 128}
        return frozenset(out)

    def item_chars(self, op, av) -> frozenset:
        if op == C.LITERAL:
            return frozenset({av})
        if op == C.NOT_LITERAL:
            return _ALPHA - {av}
        if op == C.ANY:
            return _ALPHA if self.dotall else _ALPHA - {_NL}
        if op == C.IN:
            out, negate = set(), False
            for o, a in av:
                if o == C.NEGATE:
                    negate = True
                elif o == C.LITERAL:
                    out.add(a)
                elif o == C.RANGE:
                    out |= set(range(a[0], min(a[1], 127) + 1))
                elif o == C.CATEGORY:
                    out |= _CATEGORIES.get(a, _ALPHA)
            return _ALPHA - out if negate else frozenset(out)
        if op in _REPEATS or op == getattr(C, "POSSESSIVE_REPEAT", None):
            return self.chars(av[2])
        if op == C.SUBPATTERN:
            return self.chars(av[-1])
        if op == C.BRANCH:
            return frozenset().union(*(self.chars(b) for b in av[1]))
        if op == _ATOMIC:
            return self.chars(av)
        if op in (C.GROUPREF, C.GROUPREF_EXISTS):
            return _ALPHA
        return frozenset()                              # anchors and lookarounds: no text

    def nullable(self, op, av) -> bool:
        """Can this item match the empty string?"""
        if op in (C.LITERAL, C.NOT_LITERAL, C.ANY, C.IN):
            return False
        if op in _REPEATS or op == getattr(C, "POSSESSIVE_REPEAT", None):
            return av[0] == 0 or self.all_nullable(av[2])
        if op == C.SUBPATTERN:
            return self.all_nullable(av[-1])
        if op == C.BRANCH:
            return any(self.all_nullable(b) for b in av[1])
        if op == _ATOMIC:
            return self.all_nullable(av)
        return True

    def all_nullable(self, items) -> bool:
        return all(self.nullable(op, av) for op, av in items)

    def pure(self, op, av, c: int) -> bool:
        """Can this item match a run of the character ``c`` and nothing else? ``\\w+`` can
        for ``a``; ``(\\.\\w+)*`` cannot (each round needs a ``.``)."""
        if op in (C.LITERAL, C.NOT_LITERAL, C.ANY, C.IN):
            return c in self.chars([(op, av)])
        if op in _REPEATS or op == getattr(C, "POSSESSIVE_REPEAT", None):
            return av[1] > 0 and self.all_pure(av[2], c)
        if op == C.SUBPATTERN:
            return self.all_pure(av[-1], c)
        if op == C.BRANCH:
            return any(self.all_pure(b, c) for b in av[1])
        if op == _ATOMIC:
            return self.all_pure(av, c)
        return False

    def all_pure(self, items, c: int) -> bool:
        some = False
        for op, av in items:
            if self.pure(op, av, c):
                some = True
            elif not self.nullable(op, av):
                return False
        return some

    # -- the two shapes ------------------------------------------------------------------
    @staticmethod
    def flat(items) -> list:
        """Groups opened up into the sequence around them: what matches one after another."""
        out = []
        for op, av in items:
            if op == C.SUBPATTERN:
                out += _Pattern.flat(av[-1])
            else:
                out.append((op, av))
        return out

    @staticmethod
    def unbounded(op, av) -> bool:
        return op in _REPEATS and av[1] == C.MAXREPEAT

    def can_fail(self, rest, end_counts: bool) -> bool:
        """Is there something after the ambiguous part that can make the match fail?"""
        for op, av in rest:
            if op == C.AT:
                if end_counts and av in (C.AT_END, C.AT_END_STRING):
                    return True
                continue
            if not self.nullable(op, av):
                return True
        return False

    def problems(self, items, after=()):
        """``(kind, k, char)``: the worst shape in ``items`` (followed by ``after``)."""
        seq = self.flat(items)
        for i, (op, av) in enumerate(seq):
            rest = [*seq[i + 1:], *after]
            if self.unbounded(op, av):
                body = self.flat(av[2])
                inner = [(o, a) for o, a in body if self.unbounded(o, a)]
                others = [(o, a) for o, a in body if not self.unbounded(o, a)]
                runs = [c for c in sorted(_ALPHA) if inner and self.pure(*inner[0], c)]
                if runs and self.all_nullable(others) and self.can_fail(rest, end_counts=True):
                    yield "exponential", 2, 32 if 32 in runs else runs[0]
            if op in _REPEATS or op == C.BRANCH or op == C.ASSERT or op == C.ASSERT_NOT:
                subs = [av[2]] if op in _REPEATS else av[1] if op == C.BRANCH else [av[1]]
                for sub in subs:
                    yield from self.problems(sub, rest)
        # unbounded parts in a row (only parts that can match nothing between them) that can
        # all take a run of the same character. Two is quadratic, everywhere and mostly
        # harmless; three or more (Cloudflare's had four) is worth stopping for.
        candidates = set().union(*(self.item_chars(*s) for s in seq if self.unbounded(*s)))
        best = None
        for c in sorted(candidates):
            count = 0
            for k, (op, av) in enumerate([*seq, (None, None)]):     # (None, None): the end
                if op is not None and self.unbounded(op, av) and self.pure(op, av, c):
                    count += 1
                elif op is not None and self.nullable(op, av):
                    continue
                else:                                   # the run ends here
                    rest = seq[k:] if op is not None else []
                    if count >= 3 and self.can_fail([*rest, *after], end_counts=False):
                        if best is None or count > best[0] or (count == best[0] and c == 32):
                            best = (count, c)
                    count = 0
        if best:
            yield "polynomial", best[0], best[1]


def _flags(call: ast.Call, index: int) -> int:
    expr = call.args[index] if len(call.args) > index else \
        next((k.value for k in call.keywords if k.arg == "flags"), None)
    out = 0
    for node in ast.walk(expr) if expr is not None else ():
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "re":
            out |= getattr(re, node.attr, 0) if isinstance(getattr(re, node.attr, 0), int) else 0
    return out


def _describe(kind: str, k: int, char: int) -> str:
    c = repr(chr(char))
    if kind == "exponential":
        return (f"an unbounded repeat sits inside another, so a run of {c} can be split "
                f"between them in exponentially many ways before the match fails")
    words = {2: "two", 3: "three", 4: "four", 5: "five"}
    return (f"{words.get(k, str(k))} unbounded parts in a row can all match the same text "
            f"(a run of {c}), and what follows can fail: the work grows like n^{k} with the "
            f"input's length")


@rule("regex-catastrophic-backtracking", "high",
      fix="Remove the overlap: drop redundant `.*`s, make the parts match different "
          "characters, or bound them ({0,100}); then time the pattern on a long input that "
          "does not match.")
def regex_catastrophic_backtracking(tree: ast.Module, path: str):
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name) and call.func.value.id == "re"
                and call.func.attr in _FUNCTIONS and call.args):
            continue
        literal = call.args[0]
        if not (isinstance(literal, ast.Constant) and isinstance(literal.value, str)):
            continue
        flag_index = {"compile": 1, "match": 2, "search": 2, "fullmatch": 2, "findall": 2,
                      "finditer": 2, "split": 3, "sub": 4, "subn": 4}[call.func.attr]
        try:
            parsed = P.parse(literal.value, _flags(call, flag_index))
        except Exception:                               # noqa: BLE001 - re.compile would say
            continue
        pattern = _Pattern(parsed.state.flags)
        found = sorted(pattern.problems(list(parsed)), key=lambda p: (p[0] != "exponential", -p[1]))
        if not found:
            continue
        shown = literal.value if len(literal.value) <= 48 else literal.value[:45] + "..."
        yield (literal, f"the regular expression `{shown}` can backtrack catastrophically: "
                        f"{_describe(*found[0])}",
               "A request that almost matches keeps the CPU busy for seconds or more. "
               "Cloudflare, 2019: one such pattern took every server's HTTP CPUs to nearly "
               "100% and the network down for 27 minutes.")
