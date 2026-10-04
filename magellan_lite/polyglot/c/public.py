"""Which C/C++ headers are a library's public interface.

A header the build installs is compiled into code outside this repository, so changing a
struct in it breaks binaries that were built against the old layout -- the change is an ABI
break, not a refactor (zlib: ``zlib.h`` and ``zconf.h`` are installed, ``deflate.h`` is not).

The build files say what is installed:

- CMake: ``install(FILES ...)``, ``install(... PUBLIC_HEADER ...)`` and the ``PUBLIC_HEADER``
  target property, with ``${VAR}`` expanded from ``set(VAR ...)`` in the same file;
- Make: a rule line that copies headers to ``$(includedir)`` / ``$(INCLUDEDIR)`` or ``install``s them;
- Automake: ``include_HEADERS``, ``pkginclude_HEADERS``, ``nobase_include_HEADERS``;
- Meson: ``install_headers(...)``.

A header under an ``include/`` directory is public by convention even without build files.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

HEADER = re.compile(r"[\w./+-]+\.(?:h|hh|hpp|hxx|h\+\+|inl)\b")
_SKIP = {".git", "node_modules", "build", "dist", "out", ".magellan", "__pycache__"}
_CMAKE_SET = re.compile(r"\bset\s*\(\s*(\w+)\s+(.*?)\)", re.S | re.I)
_CMAKE_INSTALL = re.compile(r"\binstall\s*\((.*?)\)", re.S | re.I)
_CMAKE_PROP = re.compile(r"PUBLIC_HEADER\s+\"?([^)\"]*)\"?", re.I)
_AM = re.compile(r"^\s*\w*include_HEADERS\s*\+?=\s*((?:.*\\\n)*.*)$", re.M)
_MESON = re.compile(r"\binstall_headers\s*\((.*?)\)", re.S)


def _names(text: str) -> set[str]:
    return {os.path.basename(m.group()) for m in HEADER.finditer(text)}


def _cmake(text: str) -> set[str]:
    text = re.sub(r"#[^\n]*", "", text)
    sets = {m.group(1): m.group(2) for m in _CMAKE_SET.finditer(text)}

    def expand(s: str, depth: int = 0) -> str:
        if depth > 4:
            return s
        return re.sub(r"\$\{(\w+)\}", lambda m: expand(sets.get(m.group(1), ""), depth + 1), s)

    out: set[str] = set()
    for m in _CMAKE_INSTALL.finditer(text):
        body = m.group(1)
        if re.match(r"\s*(FILES|DIRECTORY)\b", body, re.I) or "PUBLIC_HEADER" in body.upper():
            out |= _names(expand(body))
    for m in _CMAKE_PROP.finditer(text):
        out |= _names(expand(m.group(1)))
    # a variable named like PUBLIC_HDRS / PUBLIC_HEADERS is public whatever installs it
    for var, value in sets.items():
        if "PUBLIC" in var.upper() and "H" in var.upper():
            out |= _names(expand(value))
    return out


def _make(text: str) -> set[str]:
    out: set[str] = set()
    for line in text.splitlines():
        low = line.lower()
        if "includedir" in low or ("install" in low and ".h" in low):
            out |= _names(line)
    return out


@lru_cache(maxsize=32)
def public_headers(root: str) -> frozenset[str]:
    """Basenames of the headers ``root``'s build installs."""
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP and not d.startswith(".")]
        for fn in filenames:
            low = fn.lower()
            path = os.path.join(dirpath, fn)
            if not (low in ("cmakelists.txt", "meson.build") or low.endswith((".cmake", ".mk", ".am"))
                    or low.startswith(("makefile", "gnumakefile"))):
                continue
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if low == "cmakelists.txt" or low.endswith(".cmake"):
                out |= _cmake(text)
            elif low == "meson.build":
                for m in _MESON.finditer(text):
                    out |= _names(m.group(1))
            elif low.endswith(".am"):
                for m in _AM.finditer(text):
                    out |= _names(m.group(1))
                out |= _make(text)
            else:
                out |= _make(text)
    return frozenset(out)


def is_public(root: str | None, relpath: str) -> bool:
    """Is the header at ``relpath`` part of the installed interface?"""
    if "/include/" in "/" + relpath.replace(os.sep, "/"):
        return True
    if not root:
        return False
    return os.path.basename(relpath) in public_headers(str(root))
