"""A small ctypes binding to libclang's C API: just what the C frontend reads.

No Python ``clang`` package is needed, only the shared library, which ships with
every LLVM install (``libclang.so`` / ``libclang-NN.so`` / ``libclang.dylib``). It
is found through ``MAGELLAN_LIBCLANG`` (a file path), then the usual install
locations. :func:`load` returns ``None`` when there is none, and never raises.

The binding is deliberately narrow and read-only: parse a translation unit, walk
its cursors, and ask each cursor for its kind, name, USR, location, type, and what
it refers to. Everything returned is a plain Python value; libclang objects never
leave this module except as opaque :class:`Cursor` handles valid while their
translation unit is alive.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import os
import re
import sys
from ctypes import (CFUNCTYPE, POINTER, Structure, byref, c_char_p, c_int, c_longlong,
                    c_uint, c_void_p)
from typing import Callable


# --------------------------------------------------------------------------
# C structs (by value)
# --------------------------------------------------------------------------
class CXString(Structure):
    _fields_ = [("data", c_void_p), ("flags", c_uint)]


class CXCursor(Structure):
    _fields_ = [("kind", c_int), ("xdata", c_int), ("data", c_void_p * 3)]


class CXType(Structure):
    _fields_ = [("kind", c_int), ("data", c_void_p * 2)]


class CXSourceLocation(Structure):
    _fields_ = [("ptr_data", c_void_p * 2), ("int_data", c_uint)]


class CXSourceRange(Structure):
    _fields_ = [("ptr_data", c_void_p * 2), ("begin_int_data", c_uint),
                ("end_int_data", c_uint)]


VISITOR = CFUNCTYPE(c_int, CXCursor, CXCursor, c_void_p)

# cursor kinds (clang-c/Index.h; stable across releases)
STRUCT_DECL, UNION_DECL, ENUM_DECL, FIELD_DECL, ENUM_CONSTANT_DECL = 2, 3, 5, 6, 7
FUNCTION_DECL, VAR_DECL, PARM_DECL, TYPEDEF_DECL = 8, 9, 10, 20
TYPE_REF = 43
DECL_REF_EXPR, MEMBER_REF_EXPR, CALL_EXPR = 101, 102, 103
UNARY_OPERATOR = 112
CASE_STMT, DEFAULT_STMT, SWITCH_STMT = 203, 204, 206
TRANSLATION_UNIT = 350
MACRO_DEFINITION, MACRO_EXPANSION, INCLUSION_DIRECTIVE = 501, 502, 503

# type kinds
TYPE_POINTER, TYPE_RECORD, TYPE_ENUM, TYPE_TYPEDEF, TYPE_ELABORATED = 101, 105, 106, 107, 119
TYPE_FUNCTION_NOPROTO, TYPE_FUNCTION_PROTO = 110, 111
TYPE_DEPENDENT = 26

# linkage / storage
LINKAGE_INTERNAL, LINKAGE_EXTERNAL = 2, 4
SC_EXTERN, SC_STATIC = 2, 3

VISIT_BREAK, VISIT_CONTINUE, VISIT_RECURSE = 0, 1, 2

#: ``(unnamed struct at /path/file.h:12:5)`` / ``(anonymous union at ...)`` in a type spelling
_UNNAMED_AT = re.compile(r"\(((?:unnamed|anonymous)(?: struct| union| enum)?) at [^()]*\)")

# parse options
OPT_DETAILED_PREPROCESSING = 0x01
OPT_KEEP_GOING = 0x200

_SIGNATURES: list[tuple[str, list, object]] = [
    ("clang_createIndex", [c_int, c_int], c_void_p),
    ("clang_disposeIndex", [c_void_p], None),
    ("clang_parseTranslationUnit2",
     [c_void_p, c_char_p, POINTER(c_char_p), c_int, c_void_p, c_uint, c_uint,
      POINTER(c_void_p)], c_int),
    ("clang_disposeTranslationUnit", [c_void_p], None),
    ("clang_getTranslationUnitCursor", [c_void_p], CXCursor),
    ("clang_visitChildren", [CXCursor, VISITOR, c_void_p], c_uint),
    ("clang_getCString", [CXString], c_char_p),
    ("clang_disposeString", [CXString], None),
    ("clang_getCursorSpelling", [CXCursor], CXString),
    ("clang_getCursorUSR", [CXCursor], CXString),
    ("clang_getCursorLocation", [CXCursor], CXSourceLocation),
    ("clang_getCursorExtent", [CXCursor], CXSourceRange),
    ("clang_getRangeStart", [CXSourceRange], CXSourceLocation),
    ("clang_getRangeEnd", [CXSourceRange], CXSourceLocation),
    ("clang_getExpansionLocation",
     [CXSourceLocation, POINTER(c_void_p), POINTER(c_uint), POINTER(c_uint), POINTER(c_uint)],
     None),
    ("clang_getSpellingLocation",
     [CXSourceLocation, POINTER(c_void_p), POINTER(c_uint), POINTER(c_uint), POINTER(c_uint)],
     None),
    ("clang_getFileName", [c_void_p], CXString),
    ("clang_Location_isInSystemHeader", [CXSourceLocation], c_int),
    ("clang_getCursorType", [CXCursor], CXType),
    ("clang_getTypeSpelling", [CXType], CXString),
    ("clang_getCanonicalType", [CXType], CXType),
    ("clang_getPointeeType", [CXType], CXType),
    ("clang_getResultType", [CXType], CXType),
    ("clang_getTypedefDeclUnderlyingType", [CXCursor], CXType),
    ("clang_getEnumConstantDeclValue", [CXCursor], c_longlong),
    ("clang_Cursor_getNumArguments", [CXCursor], c_int),
    ("clang_Cursor_getArgument", [CXCursor, c_uint], CXCursor),
    ("clang_isFunctionTypeVariadic", [CXType], c_uint),
    ("clang_Type_getSizeOf", [CXType], c_longlong),
    ("clang_Cursor_getOffsetOfField", [CXCursor], c_longlong),
    ("clang_getCursorReferenced", [CXCursor], CXCursor),
    ("clang_isCursorDefinition", [CXCursor], c_uint),
    ("clang_getCursorLinkage", [CXCursor], c_int),
    ("clang_Cursor_getStorageClass", [CXCursor], c_int),
    ("clang_Cursor_isMacroFunctionLike", [CXCursor], c_uint),
    ("clang_Cursor_isAnonymous", [CXCursor], c_uint),
    ("clang_Cursor_isNull", [CXCursor], c_int),
    ("clang_getCursorSemanticParent", [CXCursor], CXCursor),
    ("clang_getNumDiagnostics", [c_void_p], c_uint),
    ("clang_getDiagnostic", [c_void_p, c_uint], c_void_p),
    ("clang_getDiagnosticSeverity", [c_void_p], c_int),
    ("clang_disposeDiagnostic", [c_void_p], None),
    ("clang_getDiagnosticSpelling", [c_void_p], CXString),
    ("clang_getClangVersion", [], CXString),
    ("clang_getIncludedFile", [CXCursor], c_void_p),
]


#: where Windows installs keep libclang.dll: LLVM, Visual Studio's bundled clang, MSYS2
_WINDOWS_LIBCLANG = [
    "C:/Program Files/LLVM/bin/libclang.dll",
    "C:/Program Files/Microsoft Visual Studio/*/*/VC/Tools/Llvm/x64/bin/libclang.dll",
    "C:/msys64/*/bin/libclang.dll",
]


def _candidates() -> list[str]:
    env = os.environ.get("MAGELLAN_LIBCLANG")
    out = [env] if env else []
    # find_library("clang") looks for clang.dll on Windows; LLVM ships libclang.dll
    out += [f for f in (ctypes.util.find_library("clang"),
                        ctypes.util.find_library("libclang")) if f]
    if sys.platform == "win32":
        out += [f for pat in _WINDOWS_LIBCLANG for f in sorted(glob.glob(pat), reverse=True)]
    elif sys.platform == "darwin":
        out += ["/Library/Developer/CommandLineTools/usr/lib/libclang.dylib",
                "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain"
                "/usr/lib/libclang.dylib", "/opt/homebrew/opt/llvm/lib/libclang.dylib",
                "/usr/local/opt/llvm/lib/libclang.dylib"]
    else:
        def ver(p: str) -> int:
            digits = "".join(ch if ch.isdigit() else " " for ch in p).split()
            return max((int(d) for d in digits if len(d) <= 2), default=0)
        pats = ["/usr/lib/llvm-*/lib/libclang.so*", "/usr/lib/llvm-*/lib/libclang-*.so*",
                "/usr/lib/x86_64-linux-gnu/libclang-*.so*", "/usr/lib/aarch64-linux-gnu/libclang-*.so*",
                "/usr/lib64/libclang.so*", "/usr/lib/libclang.so*", "/usr/local/lib/libclang.so*"]
        hits: list[str] = []
        for pat in pats:
            hits += glob.glob(pat)
        out += sorted(set(hits), key=lambda p: (-ver(p), len(p)))
    return out


class LibClang:
    """The loaded library with typed entry points."""

    def __init__(self, lib: ctypes.CDLL, path: str) -> None:
        self.lib = lib
        self.path = path
        for name, args, res in _SIGNATURES:
            fn = getattr(lib, name)
            fn.argtypes = args
            fn.restype = res
        self.version = self.string(lib.clang_getClangVersion())

    # -- strings, locations ---------------------------------------------
    def string(self, s: CXString) -> str:
        raw = self.lib.clang_getCString(s)
        out = raw.decode("utf-8", "replace") if raw else ""
        self.lib.clang_disposeString(s)
        return out

    def spelling(self, c: CXCursor) -> str:
        return self.string(self.lib.clang_getCursorSpelling(c))

    def usr(self, c: CXCursor) -> str:
        return self.string(self.lib.clang_getCursorUSR(c))

    def _loc(self, loc: CXSourceLocation, spelling: bool = False) -> tuple[str, int, int, int]:
        f, line, col, off = c_void_p(), c_uint(), c_uint(), c_uint()
        get = self.lib.clang_getSpellingLocation if spelling else self.lib.clang_getExpansionLocation
        get(loc, byref(f), byref(line), byref(col), byref(off))
        name = self.string(self.lib.clang_getFileName(f)) if f.value else ""
        return name, line.value, col.value, off.value

    def location(self, c: CXCursor) -> tuple[str, int, int, int]:
        """``(file, line, column, offset)`` where the cursor is expanded."""
        return self._loc(self.lib.clang_getCursorLocation(c))

    def in_system_header(self, c: CXCursor) -> bool:
        """Is the cursor in a header found through a system include path? (cheap)"""
        return bool(self.lib.clang_Location_isInSystemHeader(self.lib.clang_getCursorLocation(c)))

    def spelled_at(self, c: CXCursor) -> tuple[str, int, int, int]:
        """Where the cursor's text is written: inside a macro body for expanded code."""
        return self._loc(self.lib.clang_getCursorLocation(c), spelling=True)

    def extent(self, c: CXCursor) -> tuple[tuple[str, int, int, int], tuple[str, int, int, int]]:
        r = self.lib.clang_getCursorExtent(c)
        return self._loc(self.lib.clang_getRangeStart(r)), self._loc(self.lib.clang_getRangeEnd(r))

    def included_file(self, c: CXCursor) -> str:
        f = self.lib.clang_getIncludedFile(c)
        return self.string(self.lib.clang_getFileName(f)) if f else ""

    # -- types -----------------------------------------------------------
    def type_of(self, c: CXCursor) -> CXType:
        return self.lib.clang_getCursorType(c)

    def type_spelling(self, t: CXType) -> str:
        """The type as written; an unnamed record's source location is dropped.

        libclang spells ``union { ... } u`` as ``union (unnamed union at /abs/f.h:83:2)``.
        The path and line change with the checkout directory and with any edit above the
        declaration, which would make every field of that type look retyped.
        """
        return _UNNAMED_AT.sub(r"(\1)", self.string(self.lib.clang_getTypeSpelling(t)))

    def canonical(self, t: CXType) -> CXType:
        return self.lib.clang_getCanonicalType(t)

    def size_of(self, t: CXType) -> int:
        return int(self.lib.clang_Type_getSizeOf(t))

    # -- cursors ---------------------------------------------------------
    def is_null(self, c: CXCursor) -> bool:
        return bool(self.lib.clang_Cursor_isNull(c))

    def referenced(self, c: CXCursor) -> CXCursor:
        return self.lib.clang_getCursorReferenced(c)

    def children(self, c: CXCursor) -> list[CXCursor]:
        out: list[CXCursor] = []

        def visit(child, _parent, _data):
            out.append(CXCursor.from_buffer_copy(child))
            return VISIT_CONTINUE
        self.lib.clang_visitChildren(c, VISITOR(visit), None)
        return out

    def walk(self, c: CXCursor, fn: Callable[[CXCursor, CXCursor], int]) -> None:
        """Visit descendants; ``fn(cursor, parent)`` returns a VISIT_* code."""
        def visit(child, parent, _data):
            try:
                return fn(CXCursor.from_buffer_copy(child), parent)
            except Exception:                    # never let an exception cross the C boundary
                return VISIT_CONTINUE
        self.lib.clang_visitChildren(c, VISITOR(visit), None)

    # -- translation units ----------------------------------------------
    def parse(self, index: int, path: str, args: list[str], options: int) -> "TU | None":
        argv = (c_char_p * len(args))(*[a.encode() for a in args])
        tu = c_void_p()
        err = self.lib.clang_parseTranslationUnit2(
            index, path.encode(), argv, len(args), None, 0, options, byref(tu))
        if err != 0 or not tu.value:
            return None
        return TU(self, tu)

    def index(self) -> int:
        return self.lib.clang_createIndex(0, 0)


class TU:
    def __init__(self, lc: LibClang, handle: c_void_p) -> None:
        global TRANSLATION_UNIT
        self.lc = lc
        self.handle = handle
        # 300 in older libclang (13), 350 in newer ones: take it from the library in use
        TRANSLATION_UNIT = self.cursor.kind

    @property
    def cursor(self) -> CXCursor:
        return self.lc.lib.clang_getTranslationUnitCursor(self.handle)

    def errors(self) -> int:
        lib = self.lc.lib
        n = 0
        for i in range(lib.clang_getNumDiagnostics(self.handle)):
            d = lib.clang_getDiagnostic(self.handle, i)
            if lib.clang_getDiagnosticSeverity(d) >= 3:
                n += 1
            lib.clang_disposeDiagnostic(d)
        return n

    def error_messages(self, limit: int = 3) -> list[str]:
        """The first ``limit`` error messages, for a diagnostic that says why."""
        lib = self.lc.lib
        out: list[str] = []
        for i in range(lib.clang_getNumDiagnostics(self.handle)):
            d = lib.clang_getDiagnostic(self.handle, i)
            try:
                if lib.clang_getDiagnosticSeverity(d) >= 3 and len(out) < limit:
                    out.append(self.lc.string(lib.clang_getDiagnosticSpelling(d)))
            finally:
                lib.clang_disposeDiagnostic(d)
        return out

    def dispose(self) -> None:
        if self.handle is not None:
            self.lc.lib.clang_disposeTranslationUnit(self.handle)
            self.handle = None


_loaded: list[LibClang | None] = []


def load() -> LibClang | None:
    """The first libclang that loads and answers; ``None`` if there is none. Never raises."""
    if _loaded:
        return _loaded[0]
    found: LibClang | None = None
    if os.environ.get("MAGELLAN_LIBCLANG") != "none":
        for cand in _candidates():
            try:
                found = LibClang(ctypes.CDLL(cand), cand)
                break
            except (OSError, AttributeError, TypeError):
                continue
    _loaded.append(found)
    return found


def reset() -> None:
    """Forget the cached library lookup (tests switch ``MAGELLAN_LIBCLANG``)."""
    _loaded.clear()
