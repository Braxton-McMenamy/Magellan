"""Read Python 2 source, so code awaiting a 2-to-3 upgrade is still on the map.

``ast`` only parses Python 3. A Python 2 file used to be a syntax error and dropped out of
the graph entirely, so the "before" side of a 2-to-3 upgrade had none of its modules and
everything they held looked newly introduced by the upgrade (requests 0.6: ``models.py``).

:func:`parse` tries Python 3 first. On a syntax error it rewrites the Python 2 forms that
3 rejects into their Python 3 spelling and tries again, keeping every statement on its own
line so line numbers still match the file. The rewrite is what 2to3 would produce, so a
file before and after a 2-to-3 port parses to the same tree, and the port hashes as
unchanged except where the code really changed. Anything it cannot rewrite raises the
original error.
"""
from __future__ import annotations

import ast
import re

_STRING = re.compile(r"""(?:[rRbBuU]{0,2})(?:'''.*?'''|\"\"\".*?\"\"\"|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*")""",
                     re.S)
_EXCEPT = re.compile(r"^(\s*except\s+)(\([^()]*\)|[\w.]+)\s*,\s*(\w+)\s*:")
_PRINT = re.compile(r"^(\s*)print\b(?!\s*[(=.\[,)])(.*)$")
_EXEC = re.compile(r"^(\s*)exec\b(?!\s*\()\s+(.+?)(?:\s+in\s+(.+))?\s*$")
_RAISE = re.compile(r"^(\s*raise\s+)([\w.]+)\s*,\s*(.+?)\s*$")
_BACKTICK = re.compile(r"`([^`\n]+)`")
_OCTAL = re.compile(r"(?<![\w.])0([0-7]+)(?![\w.])")
_LONG = re.compile(r"(?<![\w.])(\d+)[lL](?!\w)")
_UR = re.compile(r"(?<![\w])[uU][rR](['\"])")


def parse(source: str, filename: str = "<unknown>") -> ast.Module:
    """``ast.parse``, falling back to a Python 2 reading of ``source``."""
    try:
        return ast.parse(source, filename=filename)
    except SyntaxError as first:
        try:
            return ast.parse(to_python3(source), filename=filename)
        except SyntaxError:
            raise first from None


def is_python2(source: str) -> bool:
    """Parses only once rewritten from Python 2."""
    try:
        ast.parse(source)
        return False
    except SyntaxError:
        pass
    try:
        ast.parse(to_python3(source))
        return True
    except SyntaxError:
        return False


def _mask(line: str) -> str:
    """The line with string literals blanked to the same length (so offsets still match)."""
    return _STRING.sub(lambda m: "_" * len(m.group()), line)


def _depth(text: str) -> int:
    """Open brackets minus closed ones, outside strings and comments."""
    code = _mask(text).split("#", 1)[0]
    return sum(code.count(c) for c in "([{") - sum(code.count(c) for c in ")]}")


def to_python3(source: str) -> str:
    """Rewrite Python 2-only syntax line by line; the line count never changes."""
    lines = source.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        masked = _mask(line)
        code_part = masked.split("#", 1)[0]
        m = _EXCEPT.match(line)
        if m:
            line = f"{m.group(1)}{m.group(2)} as {m.group(3)}:" + line[m.end():]
        m = _PRINT.match(line) if _PRINT.match(code_part) else None
        if m:
            indent, rest = m.group(1), m.group(2)
            # a print statement that continues over several lines closes where its
            # brackets balance (or where its backslash continuations end)
            j, depth = i, _depth(rest)
            tail = rest
            while (depth > 0 or tail.rstrip().endswith("\\")) and j + 1 < len(lines):
                j += 1
                tail = lines[j]
                depth += _depth(tail)
            if j == i:
                out.append(_tokens(indent + "print(" + _print_args(rest) + ")"))
            else:
                # the arguments span lines: `>>f` and a trailing comma become keywords
                # on the last line, where the call closes
                body, extra = rest.strip(), []
                if body.startswith(">>"):
                    target, _, body = body[2:].partition(",")
                    extra.append(f"file={target.strip()}")
                out.append(_tokens(indent + "print(" + body.strip()))
                for k in range(i + 1, j):
                    out.append(_tokens(lines[k]))
                last = lines[j].rstrip()
                if last.endswith(",") and _depth(last) <= 0:
                    last = last[:-1]
                    extra.append("end=' '")
                out.append(_tokens(last + "".join(", " + x for x in extra) + ")"))
            i = j + 1
            continue
        m = _EXEC.match(line) if _EXEC.match(code_part) else None
        if m:
            line = f"{m.group(1)}exec({m.group(2)}" + (f", {m.group(3)})" if m.group(3) else ")")
        m = _RAISE.match(line)
        if m and _depth(m.group(3)) == 0 and "#" not in _mask(m.group(3)):
            parts = _split_top(m.group(3))
            line = f"{m.group(1)}{m.group(2)}({parts[0]})"
        out.append(_tokens(line))
        i += 1
    return "\n".join(out)


def _tokens(line: str) -> str:
    """Backticks, ``<>``, ``0777``, ``10L`` and ``ur''`` outside string literals."""
    line = _outside_strings(line, lambda s: _OCTAL.sub(r"0o\1", _LONG.sub(r"\1", _BACKTICK.sub(
        r"repr(\1)", s.replace("<>", "!=")))))
    return _UR.sub(r"r\1", line)


def _print_args(rest: str) -> str:
    """``>>f, a, b,`` -> ``a, b, file=f, end=' '`` (the arguments only; no parentheses)."""
    body = rest.strip()
    if not body:
        return ""
    extra = []
    if body.startswith(">>"):
        target, _, body = body[2:].partition(",")
        extra.append(f"file={target.strip()}")
        body = body.strip()
    if body.endswith(",") and _depth(body) == 0:
        body = body[:-1].rstrip()
        extra.append("end=' '")
    return ", ".join(x for x in [body] + extra if x)


def _split_top(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch, mk in zip(text, _mask(text)):
        if mk in "([{":
            depth += 1
        elif mk in ")]}":
            depth -= 1
        if mk == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return parts


def _outside_strings(line: str, fix) -> str:
    """Apply ``fix`` to the code between string literals (and before any comment)."""
    out, last = [], 0
    for m in _STRING.finditer(line):
        out.append(fix(line[last:m.start()]))
        out.append(m.group())
        last = m.end()
    rest = line[last:]
    code, hash_, comment = rest.partition("#")
    out.append(fix(code) + hash_ + comment)
    return "".join(out)
