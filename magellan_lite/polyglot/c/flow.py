"""Statements that can never run, in the C functions a change touched ("goto fail").

Apple's SecureTransport shipped this in ``SSLVerifySignedServerKeyExchange``
(CVE-2014-1266, February 2014)::

    if ((err = SSLHashSHA1.update(&hashCtx, &signedParams)) != 0)
        goto fail;
        goto fail;
    if ((err = SSLHashSHA1.final(&hashCtx, &hashOut)) != 0)
        goto fail;
    err = sslRawVerify(ctx, ctx->peerPubKey, ...);
    ...
fail:
    SSLFreeBuffer(&signedHashes);
    return err;

The second ``goto fail;`` is outside the ``if`` whatever its indentation. It always runs, with
``err == 0``, so the signature check is skipped and every TLS server signature "verified".
The function reads fine at a glance; what gives it away is that the statements between an
unconditional jump and the next label can never run.

``unreachable-statement`` (medium: the id and severity of the Python rule in
:mod:`magellan_lite.polyglot.python.rules.reachability`, so suppressions, the brief and SARIF treat both
languages alike) reports, in each C function the change added or edited
(:func:`magellan_lite.polyglot.rules.scope.changed_spans`):

* code after ``goto L;``, ``return ...;``, ``break;``, ``continue;`` or a call that never
  returns (``abort()``, ``exit(n)``, ``__builtin_unreachable()``, ...) at the same block level,
  up to the next label (``name:``, ``case ...:``, ``default:``) or the block's closing brace;
* code after ``if (c) <leaves> else <leaves>``, and after a braced block that always leaves;
* with its own explanation, a jump repeated right after the same jump as the body of a
  brace-less ``if``/``else`` (the "goto fail" shape), and a jump indented like the body of the
  ``if`` above it but outside it.

It reads source text, so it works the same with either C backend: comments and string and
character literals (escapes, line continuations) are blanked first, preprocessor lines are
set aside, and the body is parsed into blocks, ``if``/``else`` chains, loops, ``do ... while``,
``switch`` and labels. A false positive costs more than a miss, so it says nothing when:

* a preprocessor line sits anywhere from the statement before the jump to the end of the
  first unreachable statement: another configuration may compile different code there, so
  the code after it is read as reachable (``#if A return 0; #else <body> #endif`` reads as a
  return before the body). A whole function inside one ``#ifdef`` is still checked: no
  configuration differs inside it;
* a macro may be in play: ``FOREACH(x) return x;`` reads the macro as a loop header, and a
  macro call with no ``;`` (``CASE(x) y = 1;``, ``vmcase(OP) {``, ``_Py_COMP_DIAG_POP``) may
  expand to a ``case`` label or a pragma, so it is treated like a label; jumps that macros
  expand to are invisible (missed, not guessed);
* the only unreachable statements are ``break;`` (``return x; break;`` in a ``case`` is a
  style), markers (``Py_UNREACHABLE();``, ``assert(0);``, ``abort();``), or jumps after a call
  that never returns (``abort(); return -1;`` quiets compilers); ``_Pragma`` and
  ``_Static_assert`` compile to no code at all;
* a ``NOTREACHED`` / ``not reached`` / ``unreachable`` comment marks the spot;
* the next statement holds a label (a ``goto`` can enter it), or the function's braces do
  not balance where the frontend says the function ends.

Loops, ``switch`` and ``do ... while`` are never taken to leave (``for (;;) {}`` without a
``break`` does, but proving it is not worth a false positive).
"""

from __future__ import annotations

import bisect
import os
import re
from dataclasses import dataclass, field

from magellan_lite.polyglot.core.findings import Finding
from magellan_lite.polyglot.core.model import Graph, Node, NodeKind

RULE = "unreachable-statement"
SEVERITY = "medium"

#: calls that never return, and how many arguments the standard one takes; a project that
#: defines a function or macro of the same name gets no special treatment
NORETURN = {"abort": 0, "exit": 1, "_exit": 1, "_Exit": 1, "quick_exit": 1, "thrd_exit": 1,
            "pthread_exit": 1, "longjmp": 2, "siglongjmp": 2, "__builtin_unreachable": 0,
            "__builtin_trap": 0, "unreachable": 0}

_STMT_KW = frozenset({"if", "else", "for", "while", "do", "switch", "case", "default", "goto",
                      "return", "break", "continue"})
#: a statement keyword that may follow a macro written without its ``;``: the macro ends there
_ENDS_STMT = frozenset({"else", "case", "default", "while"})
_ASM = frozenset({"asm", "__asm__", "__asm"})
#: statements that compile to no code: never "unreachable"
_NO_CODE = frozenset({"_Pragma", "__pragma", "_Static_assert", "static_assert"})
#: a statement that only says "this point is never reached" (``Py_UNREACHABLE();``)
_MARKER = re.compile(r"(?i)unreachable|not_?reached|^assert$")
_ASSIGN = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="})
_NOT_CALLS = _STMT_KW | _ASM | frozenset(
    """sizeof _Alignof alignof __alignof__ typeof __typeof__ __typeof _Generic __attribute__
    __builtin_expect likely unlikely defined __extension__ _Static_assert static_assert void
    int char short long float double signed unsigned const volatile struct union enum _Bool
    bool""".split())
_OPEN, _CLOSE = frozenset("([{"), frozenset(")]}")

_is_name = re.compile(r"[A-Za-z_$][\w$]*\Z").match
#: numbers first, so a C23 digit separator (``1'000``) does not open a character literal
_LEX = re.compile(r"(?P<num>\b\d(?:[eEpP][-+]|[\w.]|'(?=\w))*)"
                  r"|(?P<com>/\*.*?(?:\*/|\Z)|//(?:[^\\\n]|\\.)*)"
                  r"|(?P<lit>\"(?:[^\"\\\n]|\\.)*\"?|'(?:[^'\\\n]|\\.)*'?)", re.S)
_TOKEN = re.compile(r"[A-Za-z_$][\w$]*|\.?\d(?:[eEpP][-+]|[\w.'])*|->|\+\+|--|<<=|>>=|<<|>>|"
                    r"[<>=!]=|&&|\|\||[-+*/%&|^]=|\.\.\.|\S")
_PP = re.compile(r"[ \t]*#[ \t]*([A-Za-z_]\w*)?(.*)", re.S)
_ZERO = re.compile(r"\(?\s*0+[uUlL]*\s*\)?\s*\Z")
_ONE = re.compile(r"\(?\s*1[uUlL]*\s*\)?\s*\Z")
_CXX = re.compile(r"(?:defined\s*\(?\s*)?__cplusplus\s*\)?\s*\Z")
_BRACKETS = re.compile(r"[(){}\[\]]")
_BRACES = re.compile(r"[{}]")
_NOTREACHED = re.compile(r"not\s*reached|unreachable", re.I)


def _blank(s: str) -> str:
    return re.sub(r"[^\n]", " ", s)


def _strip(text: str) -> tuple[str, str]:
    """``(code, plain)``: comments blanked in both; literals become ``0`` + blanks in ``code``.

    Every offset and newline is kept, so lines and columns stay those of the file.
    """
    code: list[str] = []
    plain: list[str] = []
    last = 0
    for m in _LEX.finditer(text):
        if m.lastgroup == "num":
            continue
        s, e = m.span()
        code.append(text[last:s])
        plain.append(text[last:s])
        frag = m.group()
        if m.lastgroup == "com":
            b = _blank(frag)
            code.append(b)
            plain.append(b)
        else:
            code.append("0" + _blank(frag[1:]))
            plain.append(frag)
        last = e
    code.append(text[last:])
    plain.append(text[last:])
    return "".join(code), "".join(plain)


class Source:
    """One file read for flow: literals and comments blanked, preprocessor lines set aside.

    ``variants`` holds the code twice: with every branch of each ``#if`` (except the dead
    side of ``#if 0`` / ``#if 1`` and ``#ifdef __cplusplus``), and with only the first
    branch -- the fallback when the branches do not balance their braces on their own.
    """

    def __init__(self, text: str) -> None:
        self.text = text.replace("\r\n", "\n").replace("\r", " ")
        code, self.plain = _strip(self.text)
        lines = code.split("\n")
        self.starts = [0]
        for ln in lines[:-1]:
            self.starts.append(self.starts[-1] + len(ln) + 1)
        self.directive = [False] * len(lines)
        every, first = list(lines), list(lines)
        stack: list[list[bool]] = []          # [live (every), live (first), taken, sure]
        k = 0
        while k < len(lines):
            if _PP.match(lines[k]) is None:
                ln = lines[k]
                body = ln.rstrip(" \t")
                if body.endswith("\\"):        # a line continuation in code
                    ln = body[:-1] + " " + ln[len(body):]
                every[k] = ln if all(s[0] for s in stack) else _blank(ln)
                first[k] = ln if all(s[1] for s in stack) else _blank(ln)
                k += 1
                continue
            j = k
            while lines[j].rstrip(" \t").endswith("\\") and j + 1 < len(lines):
                j += 1
            full = " ".join(lines[x].rstrip(" \t").rstrip("\\") for x in range(k, j + 1))
            m = _PP.match(full)
            for x in range(k, j + 1):
                self.directive[x] = True
                every[x] = first[x] = " " * len(lines[x])
            _branch(stack, (m.group(1) or "") if m else "", (m.group(2) if m else "").strip())
            k = j + 1
        self.variants = ("\n".join(every), "\n".join(first))
        self._dirs = [0]
        for d in self.directive:
            self._dirs.append(self._dirs[-1] + d)

    def line(self, off: int) -> int:
        return bisect.bisect_right(self.starts, off)

    def has_directive(self, lo: int, hi: int) -> bool:
        """Is any line from ``lo`` to ``hi`` (1-based, inclusive) a preprocessor line?"""
        lo, hi = max(lo, 1), min(hi, len(self.directive))
        return lo <= hi and self._dirs[hi] - self._dirs[lo - 1] > 0

    def next_directive(self, line: int) -> int:
        """The first preprocessor line after ``line`` (0 when there is none)."""
        for k in range(max(line, 0), len(self.directive)):
            if self.directive[k]:
                return k + 1
        return 0

    def indent(self, off: int) -> str | None:
        """The whitespace before ``off`` when nothing else precedes it on its line."""
        start = self.starts[self.line(off) - 1]
        lead = self.text[start:off]
        return lead if not lead.strip() else None

    def body(self, name: str, line: int, end_line: int | None) -> tuple[int, int, str] | None:
        """``(lbrace, rbrace, code)`` of the definition of ``name`` starting on ``line``."""
        for code in self.variants:
            span = _find_body(code, self.starts, name, line, end_line)
            if span is not None:
                return span[0], span[1], code
        return None


def _branch(stack: list[list[bool]], name: str, rest: str) -> None:
    if name in ("if", "ifdef", "ifndef"):
        const = None
        if (name == "if" and _ZERO.match(rest)) or (name != "ifndef" and _CXX.match(rest)):
            const = False                     # never compiled as C
        elif name == "if" and _ONE.match(rest):
            const = True
        live = const is not False
        stack.append([live, live, live, const is True])
    elif name in ("elif", "else") and stack:
        top = stack[-1]
        const = None
        if name == "elif":
            if _ZERO.match(rest) or _CXX.match(rest):
                const = False
            elif _ONE.match(rest):
                const = True
        top[0] = not top[3] and const is not False
        top[1] = not top[2] and const is not False
        top[2] = top[2] or top[1]
        top[3] = top[3] or (const is True and top[0])
    elif name == "endif" and stack:
        stack.pop()


def _find_body(code: str, starts: list[int], name: str, line: int,
               end_line: int | None) -> tuple[int, int] | None:
    if not 1 <= line <= len(starts):
        return None
    lo = starts[line - 1]
    hi = starts[end_line] if end_line and end_line < len(starts) else len(code)
    pos = lo
    if name:
        m = re.compile(r"(?<![\w$])" + re.escape(name) + r"\s*\(").search(code, lo, hi)
        if m is not None:
            pos = m.end() - 1
    depth, lbrace = 0, -1
    for m in _BRACKETS.finditer(code, pos, hi):
        c = m.group()
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif depth <= 0:
            if c == "}":
                return None
            lbrace = m.start()
            break
    if lbrace < 0:
        return None
    depth = 0
    for m in _BRACES.finditer(code, lbrace):
        if m.group() == "{":
            depth += 1
            continue
        depth -= 1
        if depth == 0:
            if end_line and bisect.bisect_right(starts, m.start()) != end_line:
                return None                   # not where the frontend saw it end
            return lbrace, m.start()
    return None


# --------------------------------------------------------------------------
# statements
# --------------------------------------------------------------------------
@dataclass(eq=False)
class _St:
    kind: str                  # block|if|loop|switch|do|jump|label|empty|simple|macro
    a: int                     # first token
    b: int                     # last token (inclusive)
    body: list = field(default_factory=list)      # block: its statements
    arms: list = field(default_factory=list)      # if: [(`if` token, `)` token, then)]
    other: "_St | None" = None                    # else; loop/switch/do/macro body
    jump: str = ""             # goto|return|break|continue|call
    named: bool = False        # label: a name (or a macro), not case/default
    head: int = -1             # if/loop/switch: the `)` closing the condition
    opaque: bool = False       # a macro with no `;` (`CASE(x) ...`): may expand to a label
    _leaves: bool | None = None


class _Parser:
    def __init__(self, toks: list[str], defined: frozenset[str]) -> None:
        self.t = toks
        self.n = len(toks)
        self.defined = defined

    def close(self, i: int) -> int | None:
        depth = 0
        for j in range(i, self.n):
            c = self.t[j]
            if c in _OPEN:
                depth += 1
            elif c in _CLOSE:
                depth -= 1
                if depth == 0:
                    return j
        return None

    def items(self, i: int) -> tuple[list[_St], int]:
        out: list[_St] = []
        while i < self.n and self.t[i] != "}":
            st, j = self.stmt(i)
            out.append(st)
            i = max(j, i + 1)
        return out, i

    def sub(self, i: int) -> tuple[_St | None, int]:
        """The statement a control keyword governs, or None when there is none."""
        if i >= self.n or self.t[i] == "}":
            return None, i
        return self.stmt(i)

    def stmt(self, i: int) -> tuple[_St, int]:
        t, n = self.t, self.n
        w = t[i]
        nxt = t[i + 1] if i + 1 < n else ""
        if w == "{":
            body, j = self.items(i + 1)
            return _St("block", i, min(j, n - 1), body=body), j + 1
        if w == ";":
            return _St("empty", i, i), i + 1
        if w == "if" and nxt == "(":
            return self.if_(i)
        if w in ("for", "while", "switch") and nxt == "(":
            c = self.close(i + 1)
            if c is not None:
                body, j = self.sub(c + 1)
                return _St("switch" if w == "switch" else "loop", i, max(j - 1, c),
                           other=body, head=c), j
        if w == "do":
            body, j = self.sub(i + 1)
            if j + 1 < n and t[j] == "while" and t[j + 1] == "(":
                c = self.close(j + 1)
                if c is not None:
                    j = c + 1
                    if j < n and t[j] == ";":
                        j += 1
            return _St("do", i, max(j - 1, i), other=body), j
        if w == "else":                       # an `else` with no `if`: both #if branches read
            body, j = self.sub(i + 1)
            return _St("macro", i, max(j - 1, i), other=body, opaque=True), j
        if w == "goto":
            if _is_name(nxt) and i + 2 < n and t[i + 2] == ";":
                return _St("jump", i, i + 2, jump="goto"), i + 3
            if nxt == "*":                    # GNU computed goto
                end = self.end_of(i + 1)
                if end is not None:
                    return _St("jump", i, end, jump="goto"), end + 1
            return self.simple(i)
        if w == "return":
            end = self.end_of(i + 1)
            if end is not None:
                return _St("jump", i, end, jump="return"), end + 1
            return self.simple(i)
        if w in ("break", "continue") and nxt == ";":
            return _St("jump", i, i + 1, jump=w), i + 2
        if w == "case":
            depth = q = 0
            for j in range(i + 1, n):
                c = t[j]
                if c in _OPEN:
                    depth += 1
                elif c in _CLOSE:
                    depth -= 1
                elif depth == 0 and c == "?":
                    q += 1
                elif depth == 0 and c == ":":
                    if not q:
                        return _St("label", i, j), j + 1
                    q -= 1
                elif depth == 0 and c in (";", "}"):
                    break
            return self.simple(i)
        if w == "default" and nxt == ":":
            return _St("label", i, i + 1), i + 2
        if _is_name(w) and w not in _STMT_KW and nxt == ":":
            return _St("label", i, i + 1, named=True), i + 2
        if w in _ASM:
            end = self.end_of(i + 1, keywords=False)
            if end is not None:
                return _St("simple", i, end), end + 1
        if w in _NO_CODE and nxt == "(":
            c = self.close(i + 1)
            if c is not None:
                j = c + 2 if c + 1 < n and t[c + 1] == ";" else c + 1
                return _St("empty", i, j - 1), j
        st, j = self.simple(i)
        if st.kind == "simple" and self.noreturn(st):
            st.kind, st.jump = "jump", "call"
        return st, j

    def if_(self, i: int) -> tuple[_St, int]:
        t, n = self.t, self.n
        arms: list = []
        other = None
        k, j = i, i
        while True:
            c = self.close(k + 1)
            if c is None:
                if not arms:
                    return self.simple(i)
                other, j = self.simple(k)
                break
            then, j = self.sub(c + 1)
            arms.append((k, c, then))
            if j < n and t[j] == "else":
                if j + 2 < n and t[j + 1] == "if" and t[j + 2] == "(":
                    k = j + 1
                    continue
                other, j = self.sub(j + 1)
                j = max(j, arms[-1][1] + 1)
            break
        return _St("if", i, max(j - 1, arms[0][1]), arms=arms, other=other,
                   head=arms[0][1]), j

    def end_of(self, i: int, keywords: bool = True) -> int | None:
        """The ``;`` ending the statement whose tail starts at ``i``; None if it does not end
        cleanly (a ``}`` or another statement first: a macro, or text the parser cannot read)."""
        depth = 0
        for j in range(i, self.n):
            c = self.t[j]
            if c in _OPEN:
                depth += 1
            elif c in _CLOSE:
                if depth == 0:
                    return None
                depth -= 1
            elif depth == 0:
                if c == ";":
                    return j
                if keywords and c in _STMT_KW:
                    return None
        return None

    def simple(self, i: int) -> tuple[_St, int]:
        """An expression or declaration, up to its ``;``, read the way macros are written.

        ``FOREACH(x) return x;``, ``FOREACH(x) {`` and ``CASE(x) y = 1;`` (a macro call with
        no ``;`` before another statement) read as a header and the statement it governs,
        and may hide a label (``vmcase(OP_MOVE) {`` is ``case OP_MOVE:``); ``CASE(x):`` reads
        as a label; a statement with no ``;`` before ``}``, ``else`` or ``case`` ends there.
        """
        t, n = self.t, self.n
        if _is_name(t[i]) and t[i] not in _STMT_KW and i + 1 < n and t[i + 1] == "(":
            c = self.close(i + 1)
            if c is not None and c + 1 < n and (t[c + 1] == "{" or (
                    _is_name(t[c + 1]) and t[c + 1] not in _ENDS_STMT)):
                body, k = self.stmt(c + 1)
                return _St("macro", i, max(k - 1, c), other=body, opaque=True), k
        depth = q = 0
        assigned = False
        j = i
        while j < n:
            c = t[j]
            if depth == 0:
                if c == ";":
                    return _St("simple", i, j), j + 1
                # no `;`: a macro (`Py_END_ALLOW_THREADS`, `_Py_COMP_DIAG_POP`) that may
                # expand to anything -- a pragma, a closing brace, a label
                if c == "}":
                    if j == i:
                        return _St("empty", i, i), j
                    return _St("simple", i, j - 1, opaque=True), j
                if j > i and c in _STMT_KW:
                    if c in _ENDS_STMT:
                        return _St("simple", i, j - 1, opaque=True), j
                    body, k = self.stmt(j)
                    return _St("macro", i, max(k - 1, j), other=body, opaque=True), k
                if c == "?":
                    q += 1
                elif c == ":" and j > i:
                    if not q:
                        return _St("label", i, j, named=True), j + 1
                    q -= 1
                elif c in _ASSIGN:
                    assigned = True
                elif c == "{" and j > i and t[j - 1] == ")" and _is_name(t[i]) \
                        and not assigned:
                    body, k = self.stmt(j)
                    return _St("macro", i, max(k - 1, j), other=body, opaque=True), k
            if c in _OPEN:
                depth += 1
            elif c in _CLOSE and depth > 0:
                depth -= 1
            j += 1
        return _St("simple", i, n - 1, opaque=True), n     # the body ended with no `;`

    def noreturn(self, st: _St) -> bool:
        t, a, b = self.t, st.a, st.b
        name = t[a]
        if name not in NORETURN or name in self.defined:
            return False
        if b - a < 3 or t[a + 1] != "(" or t[b] != ";" or self.close(a + 1) != b - 1:
            return False
        inner = t[a + 2:b - 1]
        args, depth = (1 if inner else 0), 0
        for c in inner:
            if c in _OPEN:
                depth += 1
            elif c in _CLOSE:
                depth -= 1
            elif c == "," and depth == 0:
                args += 1
        return args == NORETURN[name]


def _children(st: _St):
    if st.kind == "block":
        yield from st.body
    for _k, _c, then in st.arms:
        if then is not None:
            yield then
    if st.other is not None:
        yield st.other


def _leaves(st: _St | None) -> bool:
    """Control never falls off the end of ``st`` to the statement after it."""
    if st is None:
        return False
    if st._leaves is None:
        if st.kind == "jump":
            st._leaves = True
        elif st.kind == "block":
            st._leaves = not _falls_through(st.body)
        elif st.kind == "if":
            st._leaves = st.other is not None and _leaves(st.other) and \
                all(_leaves(then) for _k, _c, then in st.arms)
        else:                                 # loops, switch, macros: not claimed
            st._leaves = False
    return st._leaves


def _falls_through(items: list[_St]) -> bool:
    live = True
    for st in items:
        if st.kind == "label":
            live = True
        elif live:
            live = not _leaves(st)
        elif _has_label(st):
            live = True                       # entered by a jump to the label inside
    return live


def _has_label(st: _St, cases: bool = True) -> bool:
    """Does ``st`` hold a label another statement can jump to?

    ``case``/``default`` inside a nested ``switch`` belong to that switch; a named label
    anywhere counts.
    """
    if st.kind == "label":
        return st.named or cases
    if st.opaque:
        return True
    inner = cases and st.kind != "switch"
    return any(_has_label(c, inner) for c in _children(st))


def _lists(items: list[_St]):
    yield items
    for st in items:
        yield from _lists_in(st)


def _lists_in(st: _St):
    if st.kind == "block":
        yield from _lists(st.body)
    else:
        for c in _children(st):
            yield from _lists_in(c)


def _tail(st: _St) -> tuple[_St, int] | None:
    """``(statement, guard token)``: the brace-less statement ``st`` ends with, and the
    ``if``/``else``/``for``/``while`` that governs it."""
    while True:
        if st.kind == "if":
            if st.other is None:
                k, _c, cand = st.arms[-1]
            else:
                cand, k = st.other, st.other.a - 1
        elif st.kind == "loop":
            cand, k = st.other, st.a
        else:
            return None
        if cand is None or cand.kind in ("block", "empty"):
            return None
        if cand.kind in ("if", "loop"):
            st = cand
            continue
        return cand, k


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------
@dataclass
class Hit:
    """One run of statements that can never run."""

    kind: str          # duplicate|misleading|jump|noreturn|branches|block
    line: int          # the first statement that never runs
    first: str         # its text (the condition, for a control statement)
    count: int         # statements that never run, up to ``until``
    until: str         # "`fail:`", "the end of the block", ...
    until_line: int
    jump: str          # the statement that leaves (or the `if` whose branches all leave)
    jump_line: int
    calls: list[str] = field(default_factory=list)
    guard: str = ""    # duplicate/misleading: the `if`/`else`/`for`/`while` above
    guard_line: int = 0
    guarded_line: int = 0   # the statement that guard really governs
    block_start: int = 0
    keyword: str = ""  # goto/return/break/continue, or the call that never returns


def scan(source: Source | str, name: str, line: int, end_line: int | None = None,
         defined: frozenset[str] = frozenset()) -> list[Hit]:
    """Runs of unreachable statements in the definition of ``name`` that starts on ``line``.

    ``end_line`` is where the frontend says it ends; when the braces do not end there in
    either reading of the preprocessor branches the function is skipped. ``defined``: names
    of functions and macros the project defines (they shadow ``exit``, ``abort``...).
    """
    src = source if isinstance(source, Source) else Source(source)
    span = src.body(name, line, end_line)
    if span is None:
        return []
    lbrace, rbrace, code = span
    found = [(m.group(), m.start()) for m in _TOKEN.finditer(code, lbrace + 1, rbrace)]
    toks = [x for x, _ in found]
    offs = [o for _, o in found]
    if not toks:
        return []
    p = _Parser(toks, defined)
    items, stop = p.items(0)
    if stop < len(toks):
        return []                             # a stray `}`: not read the way it compiles
    lines = [src.line(o) for o in offs]

    def text(a: int, b: int) -> str:
        s = " ".join(src.plain[offs[a]:offs[b] + len(toks[b])].split())
        return s if len(s) <= 80 else s[:79] + "…"

    def head(st: _St) -> str:
        if st.kind in ("if", "loop", "switch") and st.head >= 0:
            return text(st.a, st.head)
        if st.kind == "do":
            return "do …"
        if st.kind == "block":
            return "{ … }"
        return text(st.a, st.b)

    out: list[Hit] = []
    for lst in _lists(items):
        i = 0
        while i < len(lst):
            run = _run_at(lst, i)
            if run is None:
                i += 1
                continue
            j, k = run
            at, st = i, lst[i]
            idx = [x for x in range(j, k) if lst[x].kind != "empty"]
            ctx = lst[at - 1] if at > 0 else st
            if src.has_directive(lines[ctx.a], lines[lst[idx[0]].b]):
                # another configuration may reach it (`#if A return 0; #else <body> #endif`
                # reads as a return before the body): read on from there
                i = j
                continue
            keep = [idx[0]]
            for x in idx[1:]:
                if src.has_directive(lines[lst[keep[-1]].b], lines[lst[x].b]):
                    break
                keep.append(x)
            trimmed = len(keep) < len(idx)
            i = keep[-1] + 1 if trimmed else k
            dead = [lst[x] for x in keep]
            if all(d.kind == "jump" and d.jump == "break" for d in dead):
                continue                      # `return x; break;`: a style, not a bug
            if st.kind == "jump" and st.jump == "call" and all(d.kind == "jump" for d in dead):
                continue                      # `abort(); return -1;` keeps compilers quiet
            if all(_marker(toks, d) for d in dead):
                continue                      # `return x; Py_UNREACHABLE();` says so itself
            if _NOTREACHED.search(src.text[offs[st.b]:offs[dead[0].a]]):
                continue
            hit = Hit(kind="jump", line=lines[dead[0].a], first=head(dead[0]), count=len(dead),
                      until="", until_line=0, jump=head(st), jump_line=lines[st.a],
                      calls=_calls(toks, dead))
            if trimmed:
                hit.until_line = src.next_directive(lines[dead[-1].b])
                hit.until = f"the preprocessor line {hit.until_line}"
            elif k < len(lst):
                hit.until, hit.until_line = f"`{text(lst[k].a, lst[k].b)}`", lines[lst[k].a]
            else:
                hit.until = "the end of the function" if lst is items else "the end of the block"
                hit.until_line = src.line(rbrace) if lst is items else lines[_block_end(items, lst)]
            if st.kind == "jump":
                hit.keyword = toks[st.a] if st.jump != "call" else f"{toks[st.a]}()"
                hit.kind = "noreturn" if st.jump == "call" else "jump"
                prev = lst[at - 1] if at > 0 else None
                tail = _tail(prev) if prev is not None else None
                if tail is not None and st.jump != "call":
                    cand, g = tail
                    hit.guard, hit.guard_line = toks[g], lines[g]
                    hit.guarded_line = lines[cand.a]
                    if cand.kind == "jump" and toks[g] in ("if", "else") and \
                            text(cand.a, cand.b) == text(st.a, st.b):
                        hit.kind = "duplicate"
                    elif _misleading(src, offs, lines, cand, g, st):
                        hit.kind = "misleading"
            elif st.kind == "if":
                hit.kind, hit.jump = "branches", head(st)
            else:
                hit.kind, hit.block_start = "block", lines[st.a]
                hit.jump_line = lines[st.b]
            out.append(hit)
    return out


def _run_at(items: list[_St], i: int) -> tuple[int, int] | None:
    """``(j, k)`` when ``items[i]`` always leaves and ``items[j:k]`` can never run: ``j`` the
    first statement after it (empty ones skipped), ``k`` the next label (or the end)."""
    st = items[i]
    if st.kind == "label" or not _leaves(st):
        return None
    j = i + 1
    while j < len(items) and items[j].kind == "empty":
        j += 1
    if j >= len(items) or items[j].kind == "label" or _has_label(items[j]):
        return None
    k = j
    while k < len(items) and items[k].kind != "label" and not _has_label(items[k]):
        k += 1
    return j, k


def _marker(toks: list[str], st: _St) -> bool:
    """A statement that only marks the spot as unreachable or stops the program."""
    if st.kind not in ("simple", "jump") or (st.kind == "jump" and st.jump != "call"):
        return False
    return bool(_MARKER.search(toks[st.a])) or toks[st.a] in NORETURN


def _block_end(items: list[_St], lst: list[_St]) -> int:
    """Token index of the ``}`` closing the block whose statements are ``lst``."""
    for outer in _lists(items):
        for st in outer:
            for b in _blocks(st):
                if b.body is lst:
                    return b.b
    return lst[-1].b


def _blocks(st: _St):
    if st.kind == "block":
        yield st
        return
    for c in _children(st):
        yield from _blocks(c)


def _misleading(src: Source, offs: list[int], lines: list[int], cand: _St, g: int,
                st: _St) -> bool:
    """``st`` is indented like ``cand`` (the brace-less body of the guard at token ``g``),
    deeper than the guard, with the body on a line of its own."""
    if lines[cand.a] == lines[g] or lines[st.a] == lines[cand.b]:
        return False
    body, here, guard = src.indent(offs[cand.a]), src.indent(offs[st.a]), src.indent(offs[g])
    if body is None or here is None or guard is None:
        return False
    return here == body and len(here.expandtabs(8)) > len(guard.expandtabs(8))


def _calls(toks: list[str], sts: list[_St]) -> list[str]:
    """Functions called by ``sts``, in order: ``SSLHashSHA1.final``, ``sslRawVerify``."""
    out: list[str] = []
    for st in sts:
        for x in range(st.a, st.b):
            if toks[x + 1] != "(" or not _is_name(toks[x]) or toks[x] in _NOT_CALLS:
                continue
            name, y = toks[x], x
            while y - 2 >= st.a and toks[y - 1] in (".", "->") and _is_name(toks[y - 2]):
                name, y = toks[y - 2] + toks[y - 1] + name, y - 2
            if name not in out:
                out.append(name)
    return out


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------
def _and(names: list[str], limit: int = 4) -> str:
    shown = names[:limit]
    if len(names) > limit:
        return ", ".join(shown) + f" and {len(names) - limit} more"
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def _finding(fn: Node, h: Hit) -> Finding:
    until = h.until + (f" on line {h.until_line}" if h.until.startswith("`") else "")
    skipped = f"everything from line {h.line} to {h.until} is skipped"
    if h.calls:
        skipped += (", including the call to " if len(h.calls) == 1
                    else ", including the calls to ") + _and(h.calls)
    first = f"The first statement that never runs is line {h.line}: `{h.first}`."
    run = f"{h.count} statement(s) skipped, up to {until}"
    common = dict(rule=RULE, severity=SEVERITY, node_id=fn.id, label=fn.qualname, path=fn.path,
                  lineno=h.line, identity=f"{fn.qualname}|{h.kind}|{h.jump}|{h.first}")
    if h.kind == "duplicate":
        return Finding(
            title=f"The second `{h.jump}` in {fn.name} runs unconditionally: the `{h.guard}` "
                  f"above guards only the first",
            detail=(f"{skipped[0].upper()}{skipped[1:]}. {first} Without braces the "
                    f"`{h.guard}` on line {h.guard_line} governs only line {h.guarded_line}; "
                    f"the copy on line {h.jump_line} always runs, whatever its indentation. "
                    f"This is the shape of Apple's \"goto fail\" (CVE-2014-1266), which "
                    f"skipped TLS signature verification."),
            evidence=[f"line {h.line} never runs: {h.first}",
                      f"the `{h.guard}` on line {h.guard_line} guards the `{h.jump}` on line "
                      f"{h.guarded_line} only, not the copy on line {h.jump_line}",
                      run],
            suggestion=(f"Delete the duplicated `{h.jump}` on line {h.jump_line} (or, if both "
                        f"statements belong to the `{h.guard}`, put them in braces)."),
            **common)
    if h.kind == "misleading":
        return Finding(
            title=f"{h.count} statement(s) in {fn.name} can never execute: `{h.jump}` on line "
                  f"{h.jump_line} is not inside the `{h.guard}` it is indented under",
            detail=(f"{skipped[0].upper()}{skipped[1:]}. `{h.jump}` on line {h.jump_line} is "
                    f"indented like the body of the `{h.guard}` on line {h.guard_line}, but "
                    f"without braces only line {h.guarded_line} belongs to it, so the "
                    f"`{h.keyword}` always runs. {first}"),
            evidence=[f"line {h.line} never runs: {h.first}",
                      f"the `{h.guard}` on line {h.guard_line} governs only line "
                      f"{h.guarded_line}", run],
            suggestion=(f"If `{h.jump}` belongs to the `{h.guard}` on line {h.guard_line}, put "
                        f"braces around both statements, or else remove the code after it "
                        f"or move it above."),
            **common)
    if h.kind == "branches":
        cause = f"both branches of the `if` on line {h.jump_line} leave the block"
        fix = (f"Remove it, or let a branch of the `if` on line {h.jump_line} fall through "
               f"to it.")
    elif h.kind == "block":
        cause = f"the block on lines {h.block_start}-{h.jump_line} always leaves"
        fix = f"Remove it, or move it above the jump that ends the block on line {h.jump_line}."
    elif h.kind == "noreturn":
        cause = f"`{h.jump}` on line {h.jump_line} never returns"
        fix = f"Remove it, or move it above the `{h.keyword}` that shadows it."
    else:
        cause = f"`{h.jump}` on line {h.jump_line} always runs"
        fix = f"Remove it, or move it above the `{h.keyword}` that shadows it."
    return Finding(
        title=f"{h.count} statement(s) in {fn.name} can never execute: {cause}",
        detail=f"{cause[0].upper()}{cause[1:]}, so {skipped}. {first}",
        evidence=[f"line {h.line} never runs: {h.first}", run],
        suggestion=fix, **common)


def _read(root: str, rel: str, overlay: dict[str, str] | None) -> str | None:
    if overlay and rel in overlay:
        return overlay[rel]
    try:
        with open(os.path.join(root, rel), "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return None


def _checked(n: Node) -> bool:
    return n.kind is NodeKind.FUNCTION and n.meta.get("lang") == "c" \
        and "macro" not in n.tags and "declared-only" not in n.tags


def findings(before: Graph | None, after: Graph, cs, root: str,
             overlay: dict[str, str] | None = None) -> list[Finding]:
    """``unreachable-statement`` for the C functions ``cs`` added or edited.

    Reads each file from ``root``, or from ``overlay`` (``{relpath: text}``, an editor's
    unsaved buffers) when it has the path. ``before`` is not needed; it is accepted so the
    signature matches the other C rules. Never raises: a function that cannot be read is
    skipped with a diagnostic on ``after``.
    """
    if cs is None or after is None:
        return []
    from magellan_lite.polyglot.rules.scope import changed_spans
    spans = changed_spans(after, cs)
    out: list[Finding] = []
    defined: frozenset[str] | None = None
    for path in sorted(spans):
        fns = {s.node.id: s.node for s in spans[path] if _checked(s.node)}
        if not fns:
            continue
        text = _read(str(root), path, overlay)
        if text is None:
            continue
        if defined is None:
            defined = frozenset(n.name for n in after.nodes.values()
                                if n.kind is NodeKind.FUNCTION and n.meta.get("lang") == "c"
                                and "declared-only" not in n.tags)
        try:
            src = Source(text)
        except Exception as exc:              # a heuristic pass must not take the check down
            after.diagnostics.append(f"c: {RULE} skipped {path}: {exc!r}")
            continue
        for fn in sorted(fns.values(), key=lambda n: (n.lineno, n.id)):
            try:
                hits = scan(src, fn.name, fn.lineno, fn.end_lineno, defined)
            except (RecursionError, Exception) as exc:
                after.diagnostics.append(f"c: {RULE} skipped {fn.qualname}: {exc!r}")
                continue
            out += [_finding(fn, h) for h in hits]
    return out
