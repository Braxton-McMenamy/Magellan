"""The fallback C backend: a lexical parser in pure Python, no compiler needed.

It reads what a C file declares at file scope (functions, prototypes, structs,
unions, enums, typedefs, globals, macros) and, inside function bodies, which names
are called, read, assigned and switched on. Names are resolved by spelling, the way
the linker would: a ``static`` in the same file (or in a header it includes) first,
then an extern definition anywhere in the project, then a macro.

What it does not do, and libclang does: expand macros, know types (so no struct
sizes, no canonical types, and a call through a function pointer is matched by the
declared pointer type only), or see through ``typedef`` chains. Preprocessor
conditionals are resolved by taking the first branch of every ``#if`` (the
``#else`` of ``#if 0``, and never ``#ifdef __cplusplus``), plus every other branch of a
group whose branches are each self-contained (whole alternative declarations); a group
that splits a declaration across branches stays one consistent configuration.
Declarations under a condition are marked ``conditional``.
"""

from __future__ import annotations

import bisect
import os
import re

from magellan_lite.polyglot.c.extract import (Decl, Extraction, Ref, Switch, conditional_lines, guard_macro,
                                normalize_type, read_text, identity_macros,
                                blank_identity_calls)

KEYWORDS = frozenset("""auto break case char const continue default do double else enum extern
float for goto if inline int long register restrict return short signed sizeof static struct
switch typedef union unsigned void volatile while _Bool _Complex _Imaginary _Alignas _Alignof
_Atomic _Generic _Noreturn _Static_assert _Thread_local __attribute__ __inline __inline__
__restrict __restrict__ __extension__ __asm__ asm __volatile__ __typeof__ typeof bool
alignof alignas static_assert thread_local __declspec""".split())
_TYPE_WORDS = frozenset("""char short int long float double signed unsigned void _Bool bool
const volatile restrict struct union enum static extern inline register auto _Complex
_Atomic __restrict __restrict__ __inline __inline__ _Noreturn""".split())
_CONTROL = frozenset({"if", "for", "while", "switch", "else", "do"})
_ASSIGN = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="})
_TOK = re.compile(r'(?P<s>"(?:\\.|[^"\\\n])*")|(?P<c>\'(?:\\.|[^\'\\\n])*\')|'
                  r'(?P<i>[A-Za-z_]\w*)|(?P<n>\.?\d[\w.]*(?:[eEpP][-+]?\d+)?)|'
                  r'(?P<p>\.\.\.|->|\+\+|--|<<=|>>=|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^]=|\S)')


def _blank(text: str) -> str:
    """Comments to spaces, keeping every offset and newline."""
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(c if c == "\n" else " " for c in text[i:j]))
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class _File:
    """One file: the preprocessor pass, then tokens of the code that remains."""

    def __init__(self, rel: str, text: str) -> None:
        self.rel = rel
        self.text = text
        self.lines_at = [0] + [m.end() for m in re.finditer("\n", text)]
        self.cond = conditional_lines(text)
        self.guard = guard_macro(text)
        self.macros: list[Decl] = []
        self.includes: list[tuple[str, int]] = []
        code = self._preprocess(_blank(text))
        self.toks: list[tuple[str, str, int]] = [
            (m.lastgroup, m.group(), m.start()) for m in _TOK.finditer(code)]

    def line(self, off: int) -> int:
        return bisect.bisect_right(self.lines_at, off)

    def is_cond(self, line: int) -> bool:
        return 0 < line <= len(self.cond) and self.cond[line - 1]

    @staticmethod
    def _alternatives(lines: list[str]) -> set[int]:
        """First lines of ``#elif``/``#else`` branches that are safe to read as well.

        A group whose every branch is self-contained (balanced braces and parentheses) holds
        alternative whole declarations -- zlib's ``crc_word`` exists only in the ``#else``
        of an ARM-specific ``#if`` -- so reading only the first branch drops them. A group
        whose branches split a declaration (two alternative function heads sharing one
        body) stays first-branch-only.
        """
        def balanced(chunk: list[str]) -> bool:
            code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "",
                          "\n".join(x for x in chunk if not x.lstrip().startswith("#")))
            code = re.sub(r"/\*.*?\*/|//[^\n]*", "", code, flags=re.S)
            return code.count("{") == code.count("}") and code.count("(") == code.count(")")

        keep: set[int] = set()
        stack: list[dict] = []
        for i, ln in enumerate(lines):
            m = re.match(r"\s*#\s*(\w+)\s*(.*)", ln)
            if not m:
                continue
            d, rest = m.group(1), m.group(2).strip()
            if d in ("if", "ifdef", "ifndef"):
                skip = (d == "if" and re.fullmatch(r"0+", rest)) or "__cplusplus" in rest
                stack.append({"branches": [], "cur": i + 1, "skip": bool(skip)})
            elif d in ("elif", "else") and stack:
                top = stack[-1]
                top["branches"].append((top["cur"], i))
                top["cur"] = i + 1
                if "__cplusplus" in rest:
                    top["skip"] = True
            elif d == "endif" and stack:
                top = stack.pop()
                top["branches"].append((top["cur"], i))
                if top["skip"] or len(top["branches"]) < 2:
                    continue
                if all(balanced(lines[a:b]) for a, b in top["branches"]):
                    keep.update(a for a, _b in top["branches"][1:])
        return keep

    def _preprocess(self, code: str) -> str:
        lines = code.split("\n")
        alternatives = self._alternatives(lines)
        out: list[str] = []
        stack: list[list[bool]] = []          # [taking, taken-already]
        i = 0
        while i < len(lines):
            ln = lines[i]
            start = i
            full = ln
            while full.endswith("\\") and i + 1 < len(lines):
                i += 1
                full = full[:-1] + " " + lines[i]
            live = all(s[0] for s in stack)
            m = re.match(r"\s*#\s*(\w+)\s*(.*)", full)
            if m:
                d, rest = m.group(1), m.group(2).strip()
                if d in ("if", "ifdef", "ifndef"):
                    take = True
                    if d == "if" and re.fullmatch(r"0+", rest):
                        take = False
                    if "__cplusplus" in rest and d != "ifndef":
                        take = False
                    stack.append([take, take])
                elif d in ("elif", "else") and stack:
                    top = stack[-1]
                    top[0] = not top[1] or i + 1 in alternatives
                    if d == "elif" and "__cplusplus" in rest:
                        top[0] = False
                    top[1] = top[1] or top[0]
                elif d == "endif" and stack:
                    stack.pop()
                elif live and d == "define":
                    self._define(rest, self.lines_at[start] if start < len(self.lines_at) else 0,
                                 start + 1, i + 1)
                elif live and d == "include":
                    im = re.match(r'"([^"]+)"', rest)
                    if im:
                        self.includes.append((im.group(1), start + 1))
                out.extend("" for _ in range(start, i + 1))
            else:
                keep = live
                out.extend((lines[k] if keep else "") for k in range(start, i + 1))
            i += 1
        # keep offsets: pad each line back to its original length
        orig = code.split("\n")
        return "\n".join(o if o else " " * len(orig[k]) for k, o in enumerate(out))

    def _define(self, rest: str, off: int, line: int, end_line: int) -> None:
        m = re.match(r"([A-Za-z_]\w*)(\()?", rest)
        if not m:
            return
        name = m.group(1)
        if name == self.guard:
            return
        params: list[tuple[str, str]] = []
        variadic = False
        body = rest[m.end():]
        fn_like = bool(m.group(2))
        if fn_like:
            close = body.find(")")
            plist = [p.strip() for p in body[:close].split(",") if p.strip()]
            variadic = bool(plist) and plist[-1].endswith("...")
            params = [(p, "") for p in plist if not p.endswith("...")]
            body = body[close + 1:]
        start = self.text.find(name, off)
        self.macros.append(Decl(
            key=f"macro:{self.rel}@{name}", kind="macro", name=name, path=self.rel, line=line,
            end_line=end_line, start=max(start, 0), end=max(start, 0), is_def=True,
            params=params, variadic=variadic, function_like=fn_like, value=body.strip(),
            conditional=self.is_cond(line)))


def _match(toks: list, i: int, open_: str, close: str) -> int:
    """Index of the token closing the bracket at ``i``."""
    depth = 0
    for j in range(i, len(toks)):
        t = toks[j][1]
        if t == open_:
            depth += 1
        elif t == close:
            depth -= 1
            if depth == 0:
                return j
    return len(toks) - 1


def _split_top(toks: list, sep: str = ",") -> list[list]:
    parts, cur, depth = [], [], 0
    for t in toks:
        if t[1] in "([{":
            depth += 1
        elif t[1] in ")]}":
            depth -= 1
        if t[1] == sep and depth == 0:
            parts.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        parts.append(cur)
    return parts


def _param(toks: list) -> tuple[str, str]:
    """``(name, type)`` of one parameter declaration."""
    words = [t[1] for t in toks]
    if not words:
        return "", ""
    if "(" in words and "*" in words:                      # function pointer parameter
        names = [w for k, w in enumerate(words) if k > 0 and words[k - 1] == "*"
                 and re.match(r"[A-Za-z_]\w*$", w)]
        name = names[0] if names else ""
        return name, " ".join(w for w in words if w != name)
    if "[" in words:
        k = words.index("[")
        return (words[k - 1] if k and re.match(r"[A-Za-z_]", words[k - 1]) else ""), \
            " ".join(words[:k - 1]) + " *"
    last = words[-1]
    if len(words) > 1 and re.match(r"[A-Za-z_]\w*$", last) and last not in _TYPE_WORDS:
        return last, " ".join(words[:-1])
    return "", " ".join(words)


_EXPORT = re.compile(r"[A-Z][A-Z0-9_]*$")


def _strip_attributes(head: list) -> list:
    """Drop ``__attribute__((...))`` and ``__declspec(...)``."""
    out, i = [], 0
    while i < len(head):
        if head[i][1] in ("__attribute__", "__declspec", "__asm__", "asm") and i + 1 < len(head) \
                and head[i + 1][1] == "(":
            i = _match(head, i + 1, "(", ")") + 1
            continue
        out.append(head[i])
        i += 1
    return out


def _head(head: list) -> tuple[list, str, list] | None:
    """``(tokens before the name, name, parameter tokens)`` of a function declarator.

    The name is the identifier before the *last* top-level parenthesis group, so an
    export macro wrapping the return type (``CJSON_PUBLIC(cJSON *) cJSON_Parse(...)``)
    is not mistaken for the function. Only qualifiers may follow the parameter list.
    """
    head = _strip_attributes(head)
    words = [t[1] for t in head]
    if not words or words[0] == "typedef" or "=" in words:
        return None
    groups, i = [], 0
    while i < len(head):
        if head[i][1] == "(":
            j = _match(head, i, "(", ")")
            groups.append((i, j))
            i = j + 1
        elif head[i][1] in ("[", "{"):
            return None
        else:
            i += 1
    if not groups:
        return None
    a, b = groups[-1]
    if any(w not in ("const", "volatile") for w in words[b + 1:]):
        return None
    if a < 1 or not _is_name(words[a - 1]) or words[a - 1] in _TYPE_WORDS:
        return None
    pre = head[:a - 1]
    if not pre:
        return None                                   # `NAME(...)`: a macro invocation
    return pre, words[a - 1], head[a + 1:b]


def _is_name(w: str) -> bool:
    return bool(re.match(r"[A-Za-z_]\w*$", w)) and w not in KEYWORDS


def _ret_type(pre: list) -> str:
    """The return type from the tokens before a function name, export macros unwrapped."""
    out, i = [], 0
    while i < len(pre):
        w = pre[i][1]
        if _EXPORT.match(w) and len(w) > 1 and i + 1 < len(pre) and pre[i + 1][1] == "(":
            j = _match(pre, i + 1, "(", ")")
            out += [t[1] for t in pre[i + 2:j]]            # CJSON_PUBLIC(type) -> type
            i = j + 1
            continue
        if w in ("static", "extern", "inline", "__inline", "__inline__", "_Noreturn", "register") \
                or (_EXPORT.match(w) and "_" in w and w != "FILE"):
            i += 1
            continue
        out.append(w)
        i += 1
    return " ".join(out)


class _Parser:
    def __init__(self, f: _File, ex: Extraction, types: set[str]) -> None:
        self.f = f
        self.ex = ex
        self.types = types                   # typedef names seen so far (project-wide)
        self.bodies: list[tuple[Decl, int, int, set[str]]] = []   # fn, first, last token, params
        self.inits: list[tuple[Decl, int, int]] = []
        self.anon_seen = 0

    def decl(self, **kw) -> Decl:
        d = Decl(path=self.f.rel, **kw)
        d.conditional = self.f.is_cond(d.line)
        self.ex.decls.append(d)
        return d

    def run(self) -> None:
        toks = self.f.toks
        i, n = 0, len(toks)
        extern_c = 0
        while i < n:
            t = toks[i][1]
            if t == "extern" and i + 2 < n and toks[i + 1][0] == "s" and toks[i + 2][1] == "{":
                extern_c += 1
                i += 3
                continue
            if t == "}" and extern_c:
                extern_c -= 1
                i += 1
                continue
            if t == ";":
                i += 1
                continue
            j = self.statement(i)
            i = max(j, i + 1)

    def statement(self, i: int) -> int:
        """Parse one file-scope declaration starting at token ``i``; return the next index."""
        toks = self.f.toks
        n = len(toks)
        kr = self.kr_definition(i)
        if kr is not None:
            head, lbrace = kr
            close = _match(toks, lbrace, "{", "}")
            self.function(head, lbrace, close, i)
            return close + 1
        j = i
        depth = 0
        while j < n:
            t = toks[j][1]
            if t in "([":
                depth += 1
            elif t in ")]":
                depth -= 1
            elif t == "{" and depth == 0:
                close = _match(toks, j, "{", "}")
                head = toks[i:j]
                if any(x[1] == "=" for x in head):             # `T x = { ... };`
                    end = close + 1
                    while end < n and toks[end][1] != ";":
                        end += 1
                    self.simple(toks[i:end], i, end)
                    return end + 1
                if self.is_function_head(head):
                    self.function(head, j, close, i)
                    return close + 1
                # struct/union/enum body (maybe inside a typedef or a variable declaration)
                end = close + 1
                while end < n and toks[end][1] != ";":
                    if toks[end][1] == "{":
                        end = _match(toks, end, "{", "}")
                    end += 1
                self.aggregate(i, j, close, end)
                return end + 1
            elif t == ";" and depth <= 0:
                self.simple(toks[i:j], i, j)
                return j + 1
            elif t == "}" and depth <= 0:
                return j                                # stray brace (unbalanced #if)
            j += 1
        return n

    def kr_definition(self, i: int) -> tuple[list, int] | None:
        """A K&R definition at ``i``: ``int add(a, b) int a; int b; { ... }``.

        Returns the equivalent ANSI head (``int add(int a, int b)``, rebuilt from the same
        tokens, so offsets and lines are unchanged) and the index of the body's ``{``.
        Read naively the first ``;`` ends the declaration, the parameters become globals
        and the body is lost (zlib 1.2.11: every function in the library).
        """
        toks = self.f.toks
        n = len(toks)
        j = i
        while j < n and toks[j][1] not in ("(", ";", "{", "=", "}", "["):
            j += 1
        if j >= n or toks[j][1] != "(" or j == i or toks[j - 1][0] != "i":
            return None
        close = _match(toks, j, "(", ")")
        inner = toks[j + 1:close]
        names = [t for t in inner if t[1] != ","]
        if not names or any(t[0] != "i" for t in names) or \
                [t[1] for t in inner[1::2]] != [","] * (len(inner) // 2):
            return None                                 # not a bare identifier list
        k = close + 1
        if k >= n or toks[k][1] in (";", "{", ",", "=", ")") or toks[k][0] != "i":
            return None
        decls: dict[str, list] = {}
        while k < n and toks[k][1] != "{":
            end = k
            depth = 0
            while end < n and not (toks[end][1] == ";" and depth == 0):
                if toks[end][1] in "([":
                    depth += 1
                elif toks[end][1] in ")]":
                    depth -= 1
                elif toks[end][1] in ("{", "=") and depth == 0:
                    return None
                end += 1
            if end >= n:
                return None
            seg = toks[k:end]
            parts = _split_top(seg)
            if not parts or not parts[0]:
                return None
            # the declared names are known (the identifier list), which also finds them in
            # `void (*init)(void)` and `char *p, *q` (whose type is the first part's base)
            wanted = {t[1] for t in names}
            first = parts[0]
            hit = next((x for x in first if x[0] == "i" and x[1] in wanted), None)
            if hit is None:
                return None
            base = first[:first.index(hit)]
            while base and base[-1][1] in ("*", "("):
                base = base[:-1]
            for part in parts:
                pid = next((x for x in part if x[0] == "i" and x[1] in wanted), None)
                if pid is None:
                    continue
                decls[pid[1]] = part if part is first else base + part
            k = end + 1
        if k >= n or toks[k][1] != "{":
            return None
        head = list(toks[i:j + 1])
        for idx, nm in enumerate(names):
            head.extend(decls.get(nm[1]) or [("i", "int", nm[2]), nm])
            if idx < len(names) - 1:
                head.append(inner[2 * idx + 1])
        head.append(toks[close])
        return head, k

    # -- heads ---------------------------------------------------------
    def is_function_head(self, head: list) -> bool:
        return _head(head) is not None

    def _signature(self, head: list) -> tuple[str, list, list, bool, bool, str, bool]:
        pre, name, inner = _head(head)
        words = [t[1] for t in pre]
        static = "static" in words
        ret = _ret_type(pre) or "int"
        parts = _split_top(inner)
        params, types = [], []
        variadic = False
        noproto = not parts
        for p in parts:
            ws = [t[1] for t in p]
            if ws == ["..."]:
                variadic = True
                continue
            if ws == ["void"]:
                continue
            pn, pt = _param(p)
            params.append((pn, pt))
            types.append(normalize_type(pt))
        return name, params, types, variadic, noproto, ret, static

    def function(self, head: list, lbrace: int, rbrace: int, first: int) -> None:
        f, toks = self.f, self.f.toks
        name, params, types, variadic, noproto, ret, static = self._signature(head)
        start = toks[first][2]
        end = toks[rbrace][2] + 1
        # every program has its own main(): not one symbol shared across executables
        key = f"c:{f.rel}@F@{name}" if static or name == "main" else f"c:@F@{name}"
        d = self.decl(key=key, kind="function", name=name, line=f.line(start),
                      end_line=f.line(end), start=start, end=end, static=static, is_def=True,
                      params=params, ptypes=types, ret=ret, variadic=variadic, noproto=noproto)
        d.type = f"{normalize_type(ret)}({','.join(types)}{',...' if variadic else ''})"
        self.types_in(key, [t[1] for t in head], d.line)
        self.bodies.append((d, lbrace + 1, rbrace, {p: t for p, t in params if p}))

    def simple(self, head: list, i: int, j: int) -> None:
        """A declaration ending in ``;``: prototype, global, typedef, forward decl."""
        f = self.f
        words = [t[1] for t in head]
        if not words:
            return
        start = head[0][2]
        end = f.toks[j][2] + 1
        line = f.line(start)
        if words[0] == "typedef":
            self.typedef(head, None, start, end)
            return
        if words[0] in ("struct", "union", "enum") and len(words) <= 2:
            return                                          # forward declaration
        if "(" in words and "=" not in words and _head(head) is not None:
            name, params, types, variadic, noproto, ret, static = self._signature(head)
            key = f"c:{f.rel}@F@{name}" if static else f"c:@F@{name}"
            d = self.decl(key=key, kind="function", name=name, line=line,
                          end_line=f.line(end), start=start, end=end, static=static,
                          is_def=False, params=params, ptypes=types, ret=ret,
                          variadic=variadic, noproto=noproto)
            d.type = f"{normalize_type(ret)}({','.join(types)}{',...' if variadic else ''})"
            self.types_in(key, words, line)
            return
        if "(" in words and "=" not in words:
            k = words.index("(")
            if words[k + 1:k + 2] != ["*"]:
                return                                      # a macro invocation at file scope
        self.globals_(head, start, end, i, j)

    def globals_(self, head: list, start: int, end: int, i: int, j: int) -> None:
        f = self.f
        words = [t[1] for t in head]
        if len(words) < 2:
            return
        static = "static" in words
        extern = "extern" in words
        eq = words.index("=") if "=" in words else len(words)
        decl_part = head[:eq]
        dw = [t[1] for t in decl_part]
        fnptr = ""
        if "(" in dw and "*" in dw:
            k = dw.index("(")
            if k + 2 < len(dw) and dw[k + 1] == "*":
                name = dw[k + 2]
                close = _match(decl_part, k, "(", ")")
                rest = decl_part[close + 1:]
                if rest and rest[0][1] == "(":
                    pc = _match(rest, 0, "(", ")")
                    pts = [normalize_type(_param(p)[1]) for p in _split_top(rest[1:pc])]
                    fnptr = f"{normalize_type(' '.join(dw[:k]))}({','.join(pts)})"
            else:
                return
        else:
            if "[" in dw:
                name = dw[dw.index("[") - 1]
            else:
                name = dw[-1]
        if not re.match(r"[A-Za-z_]\w*$", name) or name in KEYWORDS:
            return
        typ = " ".join(w for w in dw if w not in ("static", "extern", name))
        value = " ".join(t[1] for t in head[eq + 1:]) if eq < len(words) else ""
        key = f"c:{f.rel}@{name}" if static else f"c:@{name}"
        d = self.decl(key=key, kind="global", name=name, line=f.line(start), end_line=f.line(end),
                      start=start, end=end, static=static, is_def=not extern or bool(value),
                      type=typ, value=value, const=typ.startswith("const") and "*" not in typ
                      or typ.rstrip().endswith("const"), fnptr=fnptr)
        if eq < len(words):
            self.inits.append((d, i + eq + 1, j))
        self.types_in(d.key, dw, f.line(start))

    def types_in(self, src: str, words: list[str], line: int) -> None:
        """ANNOTATES for every struct tag and typedef name among ``words``."""
        for k, w in enumerate(words):
            prev = words[k - 1] if k else ""
            if prev in ("struct", "union", "enum"):
                letter = {"struct": "S", "union": "U", "enum": "E"}[prev]
                self.ex.refs.append(Ref(src=src, kind="annotates", path=self.f.rel, line=line,
                                        dst=f"c:@{letter}@{w}"))
            elif w in self.types:
                self.ex.refs.append(Ref(src=src, kind="annotates", path=self.f.rel, line=line,
                                        dst=f"type:{w}"))

    # -- aggregates ----------------------------------------------------
    def aggregate(self, i: int, lbrace: int, rbrace: int, end: int) -> None:
        f, toks = self.f, self.f.toks
        words = [t[1] for t in toks[i:lbrace]]
        is_typedef = bool(words) and words[0] == "typedef"
        kw = next((w for w in words if w in ("struct", "union", "enum")), None)
        if kw is None:
            return                                           # `int x[] = {..}` handled below
        k = words.index(kw)
        tag = words[k + 1] if k + 1 < len(words) and re.match(r"[A-Za-z_]\w*$", words[k + 1]) \
            else None
        after = [t for t in toks[rbrace + 1:end]]
        aw = [t[1] for t in after]
        alias = None
        if is_typedef:
            names = [w for w in aw if re.match(r"[A-Za-z_]\w*$", w) and w not in KEYWORDS]
            alias = names[0] if names else None
        if not (tag or alias) and kw != "enum":
            self.anon_seen += 1                # an ordinal, not a line: stable under edits above
        name = tag or alias or f"{kw}_anon{self.anon_seen}"
        start = toks[i][2]
        stop = toks[min(end, len(toks) - 1)][2] + 1
        body = toks[lbrace + 1:rbrace]
        if kw == "enum":
            if tag:
                key = f"c:@E@{tag}"
            else:
                first = body[0][1] if body else "L"
                name = alias or f"enum_{first}"
                key = f"c:{f.rel}@EA@{name}"
            self.enum(key, name, body, start, stop)
        else:
            key = f"c:@{'S' if kw == 'struct' else 'U'}@{tag}" if tag else \
                f"c:{f.rel}@{'SA' if kw == 'struct' else 'UA'}@{name}"
            self.record(key, kw, name, body, start, stop)
        if is_typedef and alias:
            self.types.add(alias)
            if tag and alias != tag:
                self.decl(key=f"c:{f.rel}@T@{alias}", kind="typedef", name=alias,
                          line=f.line(start), end_line=f.line(stop), start=start, end=stop,
                          is_def=True, type=f"{kw} {tag}", value=f"{kw} {tag}")
                self.ex.refs.append(Ref(src=f"c:{f.rel}@T@{alias}", kind="annotates",
                                        path=f.rel, line=f.line(start), dst=key))
            else:
                self.ex.refs.append(Ref(src="", kind="alias", path=f.rel, line=0,
                                        dst=key, meta={"name": alias}))
        elif not is_typedef and aw and any(re.match(r"[A-Za-z_]", w) for w in aw):
            # `struct pt { ... } origin = {...};`
            vname = next(w for w in aw if re.match(r"[A-Za-z_]\w*$", w))
            static = "static" in words
            vkey = f"c:{f.rel}@{vname}" if static else f"c:@{vname}"
            value = " ".join(aw[aw.index("=") + 1:]) if "=" in aw else ""
            g = self.decl(key=vkey, kind="global", name=vname, line=f.line(start),
                          end_line=f.line(stop), start=start, end=stop, static=static,
                          is_def=True, type=f"{kw} {name}", value=value)
            self.ex.refs.append(Ref(src=g.key, kind="annotates", path=f.rel, line=g.line,
                                    dst=key))

    def record(self, key: str, kw: str, name: str, body: list, start: int, stop: int) -> None:
        f = self.f
        d = self.decl(key=key, kind=kw, name=name, line=f.line(start), end_line=f.line(stop),
                      start=start, end=stop, is_def=True)
        layout = []
        i = 0
        while i < len(body):
            j = i
            while j < len(body) and body[j][1] != ";":
                if body[j][1] == "{":
                    j = _match(body, j, "{", "}")
                j += 1
            field = body[i:j]
            i = j + 1
            fw = [t[1] for t in field]
            if not fw:
                continue
            if "{" in fw:                                    # nested aggregate member
                lb = fw.index("{")
                rb = _match(field, lb, "{", "}")
                inner_kw = next((w for w in fw[:lb] if w in ("struct", "union")), "struct")
                mname = next((w for w in fw[rb + 1:] if re.match(r"[A-Za-z_]\w*$", w)), "anon")
                ikey = f"{key}@SA@{mname}"
                self.record(ikey, inner_kw, f"{name}_{mname}", field[lb + 1:rb],
                            field[0][2], field[rb][2] + 1)
                self.ex.refs.append(Ref(src=key, kind="annotates", path=f.rel,
                                        line=f.line(field[0][2]), dst=ikey))
                fw = fw[:lb] + fw[rb + 1:]
                field = field[:lb] + field[rb + 1:]
                if not [w for w in fw if re.match(r"[A-Za-z_]", w) and w not in _TYPE_WORDS]:
                    layout.append(f"<anon {ikey}>")
                    continue
            decls = _split_top(field)
            base_words = []
            for n_, part in enumerate(decls):
                pw = [t[1] for t in part]
                if n_ == 0:
                    pname, ptype = _param(part)
                    if ":" in pw:                               # bit-field
                        c = pw.index(":")
                        pname, ptype = _param(part[:c])
                        ptype += " :" + "".join(pw[c + 1:])
                    base_words = ptype.replace("*", "").split()
                    if "[" in pw and "(" not in pw and ":" not in pw:
                        # a member array keeps its bound: in a struct `T x[N]` is not `T *x`
                        k = pw.index("[")
                        ptype = " ".join(pw[:k - 1] + pw[k:])
                else:
                    pname, extra = _param(part)
                    pname = pname or (pw[-1] if pw else "")
                    ptype = " ".join(base_words + [w for w in pw if w == "*"]
                                     + (pw[pw.index("["):] if "[" in pw else []))
                fnptr = ""
                if "(" in pw and "*" in pw:
                    k = pw.index("(")
                    close = _match(part, k, "(", ")")
                    rest = part[close + 1:]
                    if rest and rest[0][1] == "(":
                        pc = _match(rest, 0, "(", ")")
                        pts = [normalize_type(_param(p)[1]) for p in _split_top(rest[1:pc])]
                        fnptr = f"{normalize_type(' '.join(pw[:k]))}({','.join(pts)})"
                if not pname:
                    continue
                fstart = part[0][2]
                canon = normalize_type(ptype)
                self.decl(key=f"{key}@FI@{pname}", kind="field", name=pname,
                          line=f.line(fstart), end_line=f.line(fstart), start=fstart,
                          end=part[-1][2] + 1, type=ptype, parent=key, value=canon,
                          fnptr=fnptr)
                layout.append(f"{pname}:{canon}")
                for w in pw:
                    if w in self.types:
                        self.ex.refs.append(Ref(src=key, kind="annotates", path=f.rel,
                                                line=f.line(fstart), dst=f"type:{w}"))
                    if pw and pw[0] in ("struct", "union") and len(pw) > 1 and w == pw[1]:
                        self.ex.refs.append(Ref(src=key, kind="annotates", path=f.rel,
                                                line=f.line(fstart),
                                                dst=f"c:@{'S' if pw[0] == 'struct' else 'U'}@{w}"))
        d.value = "; ".join(layout)

    def enum(self, key: str, name: str, body: list, start: int, stop: int) -> None:
        f = self.f
        d = self.decl(key=key, kind="enum", name=name, line=f.line(start), end_line=f.line(stop),
                      start=start, end=stop, is_def=True)
        vals = []
        nxt = 0
        known: dict[str, int] = {}
        for part in _split_top(body):
            pw = [t[1] for t in part]
            if not pw or not re.match(r"[A-Za-z_]\w*$", pw[0]):
                continue
            mname = pw[0]
            value: str = str(nxt)
            if "=" in pw:
                expr = " ".join(pw[pw.index("=") + 1:])
                v = _const_eval(expr, known)
                value = str(v) if v is not None else expr
                nxt = (v + 1) if v is not None else nxt + 1
            else:
                nxt += 1
            if value.lstrip("-").isdigit():
                known[mname] = int(value)
            mstart = part[0][2]
            self.decl(key=f"{key}@{mname}", kind="enumerator", name=mname, line=f.line(mstart),
                      end_line=f.line(mstart), start=mstart, end=part[-1][2] + 1, parent=key,
                      value=value)
            vals.append(f"{mname}={value}")
        d.value = ", ".join(vals)

    def typedef(self, head: list, _unused, start: int, end: int) -> None:
        f = self.f
        words = [t[1] for t in head]
        fnptr = ""
        if "(" in words and "*" in words:
            k = words.index("(")
            if k + 2 < len(words) and words[k + 1] == "*":
                name = words[k + 2]
                close = _match(head, k, "(", ")")
                rest = head[close + 1:]
                pts = []
                if rest and rest[0][1] == "(":
                    pc = _match(rest, 0, "(", ")")
                    pts = [normalize_type(_param(p)[1]) for p in _split_top(rest[1:pc])]
                fnptr = f"{normalize_type(' '.join(words[1:k]))}({','.join(pts)})"
                target = " ".join(w for w in words[1:] if w != name)
            else:
                return
        else:
            names = [w for w in words[1:] if re.match(r"[A-Za-z_]\w*$", w)]
            if not names:
                return
            name = words[words.index("[") - 1] if "[" in words else names[-1]
            target = " ".join(w for w in words[1:] if w != name)
        self.types.add(name)
        key = f"c:{f.rel}@T@{name}"
        self.decl(key=key, kind="typedef", name=name, line=f.line(start), end_line=f.line(end),
                  start=start, end=end, is_def=True, type=target, value=normalize_type(target),
                  fnptr=fnptr)
        tw = target.split()
        if len(tw) >= 2 and tw[0] in ("struct", "union", "enum"):
            letter = {"struct": "S", "union": "U", "enum": "E"}[tw[0]]
            self.ex.refs.append(Ref(src=key, kind="annotates", path=f.rel, line=f.line(start),
                                    dst=f"c:@{letter}@{tw[1]}"))
        for w in tw:
            if w in self.types and w != name:
                self.ex.refs.append(Ref(src=key, kind="annotates", path=f.rel,
                                        line=f.line(start), dst=f"type:{w}"))


_CONST_TOKEN = re.compile(r"(0[xX][0-9a-fA-F]+|\d+)[uUlL]*\b|[A-Za-z_]\w*")


def _const_eval(expr: str, known: dict[str, int]) -> int | None:
    """An enumerator's integer constant expression, or ``None`` when it is not plain.

    Literals are read as C reads them (``0x1F``, octal ``010``, ``10u``/``1UL``
    suffixes) and earlier enumerators by value. Only integers and operators reach
    ``eval``; ``**``, ``//`` and huge shifts are refused.
    """
    def sub(m: re.Match) -> str:
        lit = m.group(1)
        if lit is None:
            return str(known[m.group()]) if m.group() in known else "?"
        if lit[:2] in ("0x", "0X"):
            return str(int(lit, 16))
        return str(int(lit, 8) if len(lit) > 1 and lit[0] == "0" else int(lit))
    try:
        e = _CONST_TOKEN.sub(sub, expr)
    except ValueError:                                   # 09: not an octal literal
        return None
    if "?" in e or "**" in e or "//" in e or not re.fullmatch(r"[\d\s()+\-*/%<>|&~^]*", e) \
            or any(int(n) > 64 for n in re.findall(r"<<\s*(\d+)", e)):
        return None
    try:
        v = eval(e, {"__builtins__": {}}, {})             # integers and operators only
        return int(v)
    except Exception:
        return None


# --------------------------------------------------------------------------
# bodies: calls, accesses, switches
# --------------------------------------------------------------------------
class _Symbols:
    def __init__(self, ex: Extraction, includes: dict[str, set[str]]) -> None:
        self.includes = includes
        self.fn_static: dict[tuple[str, str], str] = {}
        self.fn_extern: set[str] = set()
        self.var_static: dict[tuple[str, str], str] = {}
        self.var_extern: set[str] = set()
        self.macros: dict[str, list[Decl]] = {}
        self.enumerators: dict[str, str] = {}
        self.fields: dict[str, list[str]] = {}
        self.types: dict[str, str] = {}
        self.fn_types: dict[str, str] = {}
        self.fnptr_fields: dict[str, str] = {}
        self.fnptr_types: dict[str, str] = {}       # typedef name -> function type
        for d in ex.decls:
            if d.kind == "function":
                if d.static:
                    self.fn_static[(d.path, d.name)] = d.key
                else:
                    self.fn_extern.add(d.name)
                self.fn_types[d.key] = d.type
            elif d.kind == "global":
                if d.static:
                    self.var_static[(d.path, d.name)] = d.key
                else:
                    self.var_extern.add(d.name)
                if d.fnptr:
                    self.fnptr_fields[d.key] = d.fnptr
            elif d.kind == "macro":
                self.macros.setdefault(d.name, []).append(d)
            elif d.kind == "enumerator":
                self.enumerators.setdefault(d.name, d.key)
            elif d.kind == "field":
                self.fields.setdefault(d.name, []).append(d.key)
                if d.fnptr:
                    self.fnptr_fields[d.key] = d.fnptr
            elif d.kind == "typedef":
                self.types.setdefault(d.name, d.key)
                if d.fnptr:
                    self.fnptr_types[d.name] = d.fnptr
        for r in ex.refs:
            if r.kind == "alias":                   # `typedef struct { ... } name;`
                self.types.setdefault(r.meta["name"], r.dst)

    def scope(self, path: str) -> list[str]:
        return [path] + sorted(self.includes.get(path, ()))

    def function(self, path: str, name: str) -> str | None:
        for p in self.scope(path):
            if (p, name) in self.fn_static:
                return self.fn_static[(p, name)]
        if name in self.fn_extern:
            return f"c:@F@{name}"
        return None

    def var(self, path: str, name: str) -> str | None:
        for p in self.scope(path):
            if (p, name) in self.var_static:
                return self.var_static[(p, name)]
        if name in self.var_extern:
            return f"c:@{name}"
        return None

    def visible(self, path: str, d: Decl) -> bool:
        return d.path == path or d.path in self.includes.get(path, ())

    def macro(self, path: str, name: str) -> Decl | None:
        ds = self.macros.get(name)
        if not ds:
            return None
        scope = set(self.scope(path))
        for d in ds:
            if d.path in scope:
                return d
        return ds[0]


def _body(f: _File, ex: Extraction, sym: _Symbols, fn: Decl, lo: int, hi: int,
          params: dict[str, str]) -> None:
    toks = f.toks
    locals_: set[str] = ex.local_names.setdefault((fn.key, fn.path), set())
    locals_ |= set(params)
    local_types = {p: t.replace("const", "").strip() for p, t in params.items()}
    cond_depth: list[bool] = []                 # per open brace: is it a conditional block
    pending_ctrl = False
    stmt_cond = False
    i = lo
    while i < hi:
        kind, t, off = toks[i]
        prev = toks[i - 1][1] if i > lo else ""
        nxt = toks[i + 1][1] if i + 1 < hi else ""
        line = f.line(off)
        in_cond = any(cond_depth) or stmt_cond or f.is_cond(line)
        if t == "{":
            cond_depth.append(pending_ctrl or stmt_cond)
            pending_ctrl = stmt_cond = False
        elif t == "}":
            if cond_depth:
                cond_depth.pop()
        elif t == ";":
            stmt_cond = False
            pending_ctrl = False
        elif t in _CONTROL or t == "?" or t in ("&&", "||"):
            if t in ("if", "for", "while", "switch", "else", "do"):
                pending_ctrl = True
                stmt_cond = True
            if t == "switch":
                _switch(f, ex, sym, fn, i, hi)
        if kind != "i" or t in KEYWORDS:
            i += 1
            continue
        col = off - f.lines_at[line - 1] + 1
        # a local declaration: `type name` / `type *name` (roughly)
        if prev in _TYPE_WORDS or prev in sym.types or (prev == "*" and i >= 2 and (
                toks[i - 2][1] in _TYPE_WORDS or toks[i - 2][1] in sym.types)):
            if nxt in ("=", ";", ",", "[", ")"):
                locals_.add(t)
                local_types[t] = prev
        elif prev == "*" and i >= 2 and toks[i - 2][1] == "(" and nxt == ")" and i >= 3 and (
                toks[i - 3][1] in _TYPE_WORDS or toks[i - 3][1] in sym.types or
                toks[i - 3][1] == "*"):
            locals_.add(t)                               # `int (*fp)(int)`
        if t in sym.types:
            ex.refs.append(Ref(src=fn.key, kind="annotates", path=f.rel, line=line,
                               dst=f"type:{t}"))
            i += 1
            continue
        if prev in ("struct", "union", "enum"):
            letter = {"struct": "S", "union": "U", "enum": "E"}[prev]
            ex.refs.append(Ref(src=fn.key, kind="annotates", path=f.rel, line=line,
                               dst=f"c:@{letter}@{t}"))
            i += 1
            continue
        if prev in (".", "->"):
            keys = sym.fields.get(t, [])
            if nxt == "(":
                via = keys[0] if len(keys) == 1 else ""
                close = _match(toks, i + 1, "(", ")")
                nargs = len(_split_top(toks[i + 2:close]))
                ex.refs.append(Ref(src=fn.key, kind="calls", path=f.rel, line=line, col=col,
                                   args=nargs, dynamic=True, confidence=0.5, conditional=in_cond,
                                   meta={"fntype": sym.fnptr_fields.get(via, ""), "via": via}))
            elif len(keys) == 1:
                kind_ = "writes" if nxt in _ASSIGN or nxt in ("++", "--") else "reads"
                ex.refs.append(Ref(src=fn.key, kind=kind_, path=f.rel, line=line, col=col,
                                   dst=keys[0], conditional=in_cond,
                                   confidence=1.0))
            i += 1
            continue
        if t in locals_:
            if nxt == "(":
                close = _match(toks, i + 1, "(", ")")
                nargs = len(_split_top(toks[i + 2:close]))
                ex.refs.append(Ref(src=fn.key, kind="calls", path=f.rel, line=line, col=col,
                                   args=nargs, dynamic=True, confidence=0.5, conditional=in_cond,
                                   meta={"fntype": sym.fnptr_types.get(local_types.get(t, ""), ""),
                                         "via": ""}))
            i += 1
            continue
        if nxt == "(":
            close = _match(toks, i + 1, "(", ")")
            nargs = len(_split_top(toks[i + 2:close]))
            key = sym.function(f.rel, t)
            m = sym.macro(f.rel, t)
            # the preprocessor runs first: a macro the file can see wins over a function
            if m is not None and m.function_like and (key is None or sym.visible(f.rel, m)):
                ex.refs.append(Ref(src=fn.key, kind="macro", path=f.rel, line=line, col=col,
                                   dst=m.key, args=nargs, conditional=in_cond,
                                   meta={"name": t}))
            elif key is not None:
                ex.refs.append(Ref(src=fn.key, kind="calls", path=f.rel, line=line, col=col,
                                   dst=key, args=nargs, conditional=in_cond))
            else:
                vkey = sym.var(f.rel, t)
                if vkey is not None:                       # global function pointer
                    ex.refs.append(Ref(src=fn.key, kind="calls", path=f.rel, line=line, col=col,
                                       args=nargs, dynamic=True, confidence=0.5,
                                       conditional=in_cond,
                                       meta={"fntype": sym.fnptr_fields.get(vkey, ""),
                                             "via": vkey}))
                else:
                    ex.refs.append(Ref(src=fn.key, kind="calls", path=f.rel, line=line, col=col,
                                       ext=t, args=nargs, conditional=in_cond))
            i += 1
            continue
        key = sym.var(f.rel, t)
        if key is not None:
            kind_ = "reads"
            conf, dyn = 1.0, False
            if nxt in _ASSIGN or nxt in ("++", "--") or prev in ("++", "--"):
                kind_ = "writes"
            elif nxt in (".", "->", "["):
                j = i + 1
                while j < hi and toks[j][1] in (".", "->", "["):
                    j = (_match(toks, j, "[", "]") + 1) if toks[j][1] == "[" else j + 2
                if j < hi and (toks[j][1] in _ASSIGN or toks[j][1] in ("++", "--")):
                    kind_ = "mutates"
            elif prev == "&":
                kind_, conf, dyn = "mutates", 0.5, True
            ex.refs.append(Ref(src=fn.key, kind=kind_, path=f.rel, line=line, col=col, dst=key,
                               conditional=in_cond, confidence=conf, dynamic=dyn))
            i += 1
            continue
        fkey = sym.function(f.rel, t)
        if fkey is not None:
            ex.address_taken[fkey] = sym.fn_types.get(fkey, "")
            ex.refs.append(Ref(src=fn.key, kind="reads", path=f.rel, line=line, col=col,
                               dst=fkey, conditional=in_cond, meta={"address_taken": True}))
            i += 1
            continue
        ekey = sym.enumerators.get(t)
        if ekey is not None:
            ex.refs.append(Ref(src=fn.key, kind="reads", path=f.rel, line=line, col=col,
                               dst=ekey, conditional=in_cond))
            i += 1
            continue
        m = sym.macro(f.rel, t)
        if m is not None and not m.function_like:
            ex.refs.append(Ref(src=fn.key, kind="macro", path=f.rel, line=line, col=col,
                               dst=m.key, conditional=in_cond, meta={"name": t}))
        i += 1


def _switch(f: _File, ex: Extraction, sym: _Symbols, fn: Decl, i: int, hi: int) -> None:
    toks = f.toks
    if i + 1 >= hi or toks[i + 1][1] != "(":
        return
    close = _match(toks, i + 1, "(", ")")
    if close + 1 >= hi or toks[close + 1][1] != "{":
        return
    end = _match(toks, close + 1, "{", "}")
    covered: list[str] = []
    default = False
    j = close + 2
    while j < end:
        t = toks[j][1]
        if t == "switch" and j + 1 < end and toks[j + 1][1] == "(":
            c2 = _match(toks, j + 1, "(", ")")
            if c2 + 1 < end and toks[c2 + 1][1] == "{":
                j = _match(toks, c2 + 1, "{", "}") + 1
                continue
        if t == "case":
            k = j + 1
            while k < end and toks[k][1] != ":":
                if toks[k][0] == "i" and toks[k][1] in sym.enumerators:
                    covered.append(toks[k][1])
                    break
                k += 1
        elif t == "default" and j + 1 < end and toks[j + 1][1] == ":":
            default = True
        j += 1
    if not covered:
        return
    enums: dict[str, int] = {}
    for c in covered:
        key = sym.enumerators[c]
        parent = key.rsplit("@", 1)[0]
        enums[parent] = enums.get(parent, 0) + 1
    enum = max(enums, key=lambda e: enums[e])
    subject = " ".join(t[1] for t in toks[i + 2:close])
    ex.switches.append(Switch(fn=fn.key, path=f.rel, line=f.line(toks[i][2]), enum=enum,
                              covered=sorted(set(covered)), default=default, subject=subject))


def _resolve_include(raw: str, src: str, paths: set[str], by_name: dict[str, list[str]]) -> str:
    for base in (os.path.dirname(src), "", "include", "src"):
        cand = os.path.normpath(os.path.join(base, raw)).replace(os.sep, "/")
        if cand in paths:
            return cand
    tails = [p for p in by_name.get(os.path.basename(raw), ()) if p.endswith("/" + raw) or p == raw]
    return tails[0] if len(tails) == 1 else ""


def extract(root: str, files: list[str]) -> Extraction:
    ex = Extraction(backend="lexical")
    parsed: list[_File] = []
    types: set[str] = set()
    # headers first so typedef names are known when the sources are read
    # CRLF (git's default checkout on Windows) would stop the `[ \t]*$` patterns below matching
    raw = {rel: read_text(root, rel).replace("\r\n", "\n") for rel in files}
    identity = identity_macros(raw.values())
    # `#define local static` (zlib) and friends: the parser must see the keyword
    keywords = {m.group(1): m.group(2) for t in raw.values() for m in re.finditer(
        r"^[ \t]*#[ \t]*define[ \t]+(\w+)[ \t]+(static|extern|inline|const|volatile|register)"
        r"[ \t]*$", t, re.M)}
    for rel in sorted(files, key=lambda p: (not p.endswith(".h"), p)):
        text = blank_identity_calls(raw[rel], identity)
        ex.files[rel] = text
        f = _File(rel, text)
        if keywords:
            f.toks = [(k, keywords.get(w, w) if k == "i" else w, off) for k, w, off in f.toks]
        parsed.append(f)
    parsers = []
    for f in parsed:
        p = _Parser(f, ex, types)
        try:
            p.run()
        except (IndexError, ValueError, StopIteration) as exc:
            ex.diagnostics.append(f"{f.rel}: lexical parse stopped early ({exc!r})")
        ex.decls.extend(f.macros)
        parsers.append(p)
    paths = set(ex.files)
    by_name: dict[str, list[str]] = {}
    for p in paths:
        by_name.setdefault(os.path.basename(p), []).append(p)
    direct: dict[str, set[str]] = {}
    for f in parsed:
        for raw, line in f.includes:
            target = _resolve_include(raw, f.rel, paths, by_name)
            if target:
                ex.includes.append((f.rel, target, line))
                direct.setdefault(f.rel, set()).add(target)
    closure: dict[str, set[str]] = {}
    for f in parsed:
        seen, todo = set(), list(direct.get(f.rel, ()))
        while todo:
            x = todo.pop()
            if x not in seen:
                seen.add(x)
                todo.extend(direct.get(x, ()))
        closure[f.rel] = seen
    sym = _Symbols(ex, closure)
    for p in parsers:
        for fn, lo, hi, params in p.bodies:
            try:
                _body(p.f, ex, sym, fn, lo, hi, params)
            except (IndexError, ValueError) as exc:
                ex.diagnostics.append(f"{p.f.rel}: {fn.name}: body skipped ({exc!r})")
        for g, lo, hi in p.inits:
            for kind, t, off in p.f.toks[lo:hi]:
                if kind != "i":
                    continue
                key = sym.function(p.f.rel, t)
                if key is not None:
                    ex.address_taken[key] = sym.fn_types.get(key, "")
                    ex.refs.append(Ref(src=g.key, kind="reads", path=p.f.rel,
                                       line=p.f.line(off), dst=key,
                                       meta={"address_taken": True}))
    _finish(ex, sym)
    return ex


def _finish(ex: Extraction, sym: _Symbols) -> None:
    """Turn ``type:<name>`` placeholders into keys, and typedef'd anonymous structs."""
    alias: dict[str, str] = {}
    for r in ex.refs:
        if r.kind == "alias":
            alias[r.meta["name"]] = r.dst
    refs = []
    for r in ex.refs:
        if r.kind == "alias":
            continue
        if r.dst.startswith("type:"):
            name = r.dst[5:]
            dst = alias.get(name) or sym.types.get(name)
            if dst is None:
                continue
            r.dst = dst
        if r.kind == "annotates" and r.src == r.dst:
            continue
        refs.append(r)
    ex.refs = refs
