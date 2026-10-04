"""COBOL source text: reference-format handling and the tokenizer.

Fixed (reference) format, the mainframe norm::

    cols 1-6    sequence area         ignored (renumbering is not a change)
    col  7      indicator             '*' '/' comment, '-' continuation, 'D' debug
    cols 8-11   area A                division/section/paragraph headers, 01/77/FD
    cols 12-72  area B                statements
    cols 73-80  identification area   ignored

Free format (COBOL 2002+, ``>>SOURCE FORMAT FREE``) has none of that: ``*>`` starts a
comment anywhere. Old librarian directives (``++INCLUDE``, ``-INC``) are turned into COPY.

Everything downstream works on :class:`Token` lists. Words are compared uppercased;
literals keep their text, since ``'Y'`` and ``'y'`` are different values.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

INDICATORS = frozenset(" *-/DdCc$")
_WORD = re.compile(r"[A-Za-z0-9_\-]+")
_WORD_CH = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_LIT_PREFIX = frozenset({"X", "N", "Z", "G", "B", "U", "NX", "BX", "UX", "H"})


@dataclass
class CodeLine:
    lineno: int
    col: int            # 1-based source column of text[0]
    text: str
    path: str = ""


@dataclass
class Source:
    """One file split into code lines (sequence areas gone) and comment lines."""
    path: str
    fmt: str                                   # "fixed" | "free"
    lines: list[CodeLine] = field(default_factory=list)
    comments: dict[int, str] = field(default_factory=dict)   # lineno -> comment text
    normalized: str = ""                       # what the file hash covers


@dataclass(slots=True)
class Token:
    kind: str          # WORD LIT PERIOD PUNCT PIC
    text: str
    line: int
    col: int
    path: str = ""
    #: innermost copybook the token was copied from ("" = the file itself)
    copy: str = ""
    #: False when a COPY ... REPLACING rewrote it (or it came through one)
    shared: bool = True
    #: the text as compared: words uppercased, literals as written. Read millions of
    #: times by the parser, so computed once (``text`` never changes after tokenizing).
    up: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.up = self.text.upper() if self.kind != "LIT" else self.text

    def clone(self, copy: str, shared: bool) -> "Token":
        """This token as spliced in by a COPY of ``copy``."""
        return Token(self.kind, self.text, self.line, self.col, self.path, copy, shared)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.kind} {self.text!r} {self.line}:{self.col}>"


# --------------------------------------------------------------------------
def detect_format(raw_lines: list[str]) -> str:
    fixed = free = 0
    for raw in raw_lines[:400]:
        up = raw.upper()
        if ">>SOURCE" in up and "FREE" in up:
            return "free"
        if ">>SOURCE" in up and "FIX" in up:
            return "fixed"
        if not raw.strip():
            continue
        stripped = raw.lstrip()
        if stripped.startswith("*>") and len(raw) - len(stripped) < 6:
            free += 1
            continue
        if len(raw) < 7 or raw[6] in INDICATORS:
            fixed += 1
        else:
            free += 1
    return "free" if free > fixed else "fixed"


def _strip_inline_comment(text: str) -> tuple[str, str]:
    """Remove a ``*>`` comment that is not inside a literal."""
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "*" and text.startswith("*>", i):
            return text[:i], text[i + 2:]
    return text, ""


def _open_quote(text: str) -> str:
    """The quote character left open at the end of ``text``, if any."""
    quote = ""
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
    return quote


_LIBRARIAN = re.compile(r"^\s*(\+\+INCLUDE|-INC)\s+([A-Za-z0-9#@$_-]+)", re.I)


def read(text: str, path: str = "", fmt: str | None = None) -> Source:
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    fmt = fmt or detect_format(raw_lines)
    src = Source(path=path, fmt=fmt)
    norm: list[str] = []
    for i, raw in enumerate(raw_lines, 1):
        raw = raw.expandtabs(8)
        if fmt == "fixed":
            m = _LIBRARIAN.match(raw[6:72] if len(raw) > 6 else "") or _LIBRARIAN.match(raw)
            if m:
                src.lines.append(CodeLine(i, 8, f"COPY {m.group(2)}.", path))
                norm.append(f"COPY {m.group(2)}.")
                continue
            if len(raw) < 7:
                norm.append("")
                continue
            ind, area, col = raw[6], raw[7:72], 8
            if ind in "*/":
                src.comments[i] = area.strip()
                norm.append("*" + area.rstrip())
                continue
            if ind in "Dd":            # debugging line: compiled only WITH DEBUGGING MODE
                src.comments[i] = area.strip()
                norm.append("D" + area.rstrip())
                continue
            if area.lstrip().startswith(">>") or ind == "$":
                norm.append(area.rstrip())
                continue
            code, comment = _strip_inline_comment(area)
            if comment:
                src.comments[i] = comment.strip()
            if ind == "-" and src.lines:
                prev = src.lines[-1]
                body = code.lstrip()
                q = _open_quote(prev.text)
                if q and body[:1] in "'\"":
                    # a continued literal runs to column 72 of the previous line
                    prev.text = prev.text.ljust(65) + body[1:]
                else:
                    prev.text = prev.text.rstrip() + body
                norm.append("-" + code.rstrip())
                continue
            norm.append(" " + code.rstrip())
            if code.strip():
                src.lines.append(CodeLine(i, col, code, path))
        else:
            m = _LIBRARIAN.match(raw)
            if m:
                src.lines.append(CodeLine(i, 1, f"COPY {m.group(2)}.", path))
                norm.append(f"COPY {m.group(2)}.")
                continue
            s = raw.lstrip()
            if s.startswith(">>") or s.startswith("$"):
                norm.append(raw.rstrip())
                continue
            if s.startswith("*>"):
                src.comments[i] = s[2:].strip()
                norm.append(raw.rstrip())
                continue
            code, comment = _strip_inline_comment(raw)
            if comment:
                src.comments[i] = comment.strip()
            norm.append(raw.rstrip())
            if code.strip():
                src.lines.append(CodeLine(i, 1, code, path))
    src.normalized = "\n".join(norm).rstrip() + "\n"
    return src


# --------------------------------------------------------------------------
def tokenize(src: Source) -> list[Token]:
    out: list[Token] = []
    expect_pic = False
    for cl in src.lines:
        text, n, i = cl.text, len(cl.text), 0
        while i < n:
            ch = text[i]
            if ch in " \t,;" and not (ch in ",;" and expect_pic):
                i += 1
                continue
            col = cl.col + i
            if expect_pic:
                j = i
                while j < n and text[j] not in " \t":
                    j += 1
                pic = text[i:j]
                tail = ""
                while pic and pic[-1] in ".,;" and (j >= n or text[j] in " \t"):
                    tail = pic[-1] + tail
                    pic = pic[:-1]
                if pic.upper() == "IS":
                    out.append(Token("WORD", pic, cl.lineno, col, cl.path))
                    i = j
                    continue
                out.append(Token("PIC", pic, cl.lineno, col, cl.path))
                if "." in tail:
                    out.append(Token("PERIOD", ".", cl.lineno, col + len(pic), cl.path))
                expect_pic = False
                i = j
                continue
            if ch in "'\"":
                j, buf = i + 1, []
                while j < n:
                    if text[j] == ch:
                        if j + 1 < n and text[j + 1] == ch:
                            buf.append(ch)
                            j += 2
                            continue
                        break
                    buf.append(text[j])
                    j += 1
                out.append(Token("LIT", ch + "".join(buf) + ch, cl.lineno, col, cl.path))
                i = j + 1
                continue
            if ch == "." :
                if i + 1 >= n or text[i + 1] in " \t":
                    out.append(Token("PERIOD", ".", cl.lineno, col, cl.path))
                    i += 1
                    continue
                if text[i + 1].isdigit():
                    m = re.match(r"\.\d+", text[i:])
                    out.append(Token("WORD", m.group(0), cl.lineno, col, cl.path))
                    i += len(m.group(0))
                    continue
                out.append(Token("PUNCT", ".", cl.lineno, col, cl.path))
                i += 1
                continue
            if ch == ":" and i + 1 < n and text[i + 1].isalpha():
                # pseudo-text tag (:TAG:) or an SQL host variable (:WS-X)
                j = i + 1
                while j < n and (text[j] in _WORD_CH or text[j] == ":"):
                    j += 1
                out.append(Token("WORD", text[i:j], cl.lineno, col, cl.path))
                i = j
                continue
            if ch in _WORD_CH or (ch in "+-" and i + 1 < n and text[i + 1].isdigit()
                                  and _sign_context(out)):
                j = i + 1
                while j < n and (text[j] in _WORD_CH or (text[j] == ":" and j + 1 < n
                                                         and text[j - 1] != "("
                                                         and _is_tag_colon(text, j))):
                    j += 1
                word = text[i:j]
                if (j < n and text[j] == "." and j + 1 < n and text[j + 1].isdigit()
                        and re.fullmatch(r"[+-]?\d+", word)):
                    m = re.match(r"\.\d+", text[j:])
                    word += m.group(0)
                    j += len(m.group(0))
                if j < n and text[j] in "'\"" and word.upper() in _LIT_PREFIX:
                    q = text[j]
                    k = text.find(q, j + 1)
                    k = n if k < 0 else k
                    out.append(Token("LIT", word.upper() + text[j:k + 1], cl.lineno, col,
                                     cl.path))
                    i = k + 1
                    continue
                out.append(Token("WORD", word, cl.lineno, col, cl.path))
                if word.upper() in ("PIC", "PICTURE"):
                    expect_pic = True
                i = j
                continue
            if text.startswith(("==", "**", ">=", "<=", "<>"), i):
                out.append(Token("PUNCT", text[i:i + 2], cl.lineno, col, cl.path))
                i += 2
                continue
            out.append(Token("PUNCT", ch, cl.lineno, col, cl.path))
            i += 1
    return out


_SIGNED_AFTER = frozenset({"VALUE", "VALUES", "THRU", "THROUGH", "IS", "ARE", "TO", "BY",
                           "FROM", "WHEN", "ALSO", "GIVING", "EQUAL", "THAN"})


def _sign_context(out: list[Token]) -> bool:
    """Is a ``+``/``-`` followed by a digit a signed number here, not an operator?"""
    if not out:
        return True
    last = out[-1]
    if last.kind == "PUNCT":
        return last.text != ")"
    return last.kind == "WORD" and last.text.upper() in _SIGNED_AFTER


def _is_tag_colon(text: str, j: int) -> bool:
    """Is the ':' at ``j`` the end of a ``:TAG:`` inside a word such as ``WS-:TAG:-X``?"""
    k = j - 1
    while k >= 0 and text[k] in _WORD_CH:
        k -= 1
    return k >= 0 and text[k] == ":"


# --------------------------------------------------------------------------
_PIC_SYMBOL = re.compile(r"(.)\((\d+)\)|(.)")


def pic_runs(pic: str) -> list[tuple[str, int]]:
    """``[(symbol, count)]`` of the expanded picture, adjacent repeats merged, so
    ``9(3)V99`` and ``999V99`` are the same picture.

    Counted, not spelled out: ``X(32000)`` is common in CICS COMMAREAs, and expanding
    it character by character was most of the parse time on such programs.
    """
    out: list[tuple[str, int]] = []
    for m in _PIC_SYMBOL.finditer(pic.upper()):
        if m.group(1) is not None:
            ch, n = m.group(1), min(int(m.group(2)), 100000)
        else:
            ch, n = m.group(3), 1
        if not n:
            continue
        if out and out[-1][0] == ch:
            out[-1] = (ch, out[-1][1] + n)
        else:
            out.append((ch, n))
    return out


def canonical_pic(pic: str) -> str:
    """Run-length form of an expanded picture: ``S9(5)V9(2)``."""
    return "".join(ch + (f"({n})" if n > 1 else "") for ch, n in pic_runs(pic))
