"""A small ctypes binding to libclang's stable C API.

No Python bindings or pip packages are needed: only a ``libclang.so`` (or ``.dylib`` /
``.dll``). It is found through ``MAGELLAN_LIBCLANG`` (a file path), then the loader's
search path, then the usual LLVM install locations, newest version first.

Nothing here is C++-specific: the C frontend (or anything else wanting a clang AST) can
use it as is. It covers what an indexer needs -- cursors, types, tokens, overrides,
inclusions, diagnostics -- and nothing that writes.

``load()`` never raises; it returns ``None`` when no usable library is found. Functions
that a given libclang version lacks (``clang_CXXMethod_isExplicit`` is 17+) are bound
lazily and report ``False`` / ``None`` when missing.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import os
import re
from ctypes import (CFUNCTYPE, POINTER, Structure, byref, c_char_p, c_int, c_longlong,
                    c_uint, c_ulonglong, c_void_p, py_object)
from typing import Callable, Iterator


# --------------------------------------------------------------------------
# C structs
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


class CXToken(Structure):
    _fields_ = [("int_data", c_uint * 4), ("ptr_data", c_void_p)]


class CXUnsavedFile(Structure):
    _fields_ = [("Filename", c_char_p), ("Contents", c_char_p), ("Length", ctypes.c_ulong)]


VISITOR = CFUNCTYPE(c_int, CXCursor, CXCursor, py_object)
INCLUSION_VISITOR = CFUNCTYPE(None, c_void_p, POINTER(CXSourceLocation), c_uint, py_object)


# --------------------------------------------------------------------------
# enums (values from clang-c/Index.h; stable across versions)
# --------------------------------------------------------------------------
class K:
    """Cursor kinds."""
    UNEXPOSED_DECL = 1
    STRUCT_DECL = 2
    UNION_DECL = 3
    CLASS_DECL = 4
    ENUM_DECL = 5
    FIELD_DECL = 6
    ENUM_CONSTANT_DECL = 7
    FUNCTION_DECL = 8
    VAR_DECL = 9
    PARM_DECL = 10
    TYPEDEF_DECL = 20
    CXX_METHOD = 21
    NAMESPACE = 22
    LINKAGE_SPEC = 23
    CONSTRUCTOR = 24
    DESTRUCTOR = 25
    CONVERSION_FUNCTION = 26
    TEMPLATE_TYPE_PARAMETER = 27
    NON_TYPE_TEMPLATE_PARAMETER = 28
    TEMPLATE_TEMPLATE_PARAMETER = 29
    FUNCTION_TEMPLATE = 30
    CLASS_TEMPLATE = 31
    CLASS_TEMPLATE_PARTIAL_SPECIALIZATION = 32
    NAMESPACE_ALIAS = 33
    USING_DIRECTIVE = 34
    USING_DECLARATION = 35
    TYPE_ALIAS_DECL = 36
    CXX_ACCESS_SPECIFIER = 39
    TYPE_REF = 43
    CXX_BASE_SPECIFIER = 44
    TEMPLATE_REF = 45
    NAMESPACE_REF = 46
    MEMBER_REF = 47
    OVERLOADED_DECL_REF = 49
    VARIABLE_REF = 50
    UNEXPOSED_EXPR = 100
    DECL_REF_EXPR = 101
    MEMBER_REF_EXPR = 102
    CALL_EXPR = 103
    UNARY_OPERATOR = 112
    BINARY_OPERATOR = 114
    COMPOUND_ASSIGN_OPERATOR = 115
    CONDITIONAL_OPERATOR = 116
    CXX_THROW_EXPR = 133
    CXX_NEW_EXPR = 134
    CXX_DELETE_EXPR = 135
    LAMBDA_EXPR = 144
    UNEXPOSED_STMT = 200
    COMPOUND_STMT = 202
    CASE_STMT = 203
    DEFAULT_STMT = 204
    IF_STMT = 205
    SWITCH_STMT = 206
    WHILE_STMT = 207
    DO_STMT = 208
    FOR_STMT = 209
    RETURN_STMT = 214
    CXX_CATCH_STMT = 223
    CXX_TRY_STMT = 224
    CXX_FOR_RANGE_STMT = 225
    TRANSLATION_UNIT = 350
    CXX_FINAL_ATTR = 404
    CXX_OVERRIDE_ATTR = 405
    INCLUSION_DIRECTIVE = 503
    TYPE_ALIAS_TEMPLATE_DECL = 601
    STATIC_ASSERT = 602
    FRIEND_DECL = 603
    CONCEPT_DECL = 604

    RECORDS = frozenset({STRUCT_DECL, UNION_DECL, CLASS_DECL, CLASS_TEMPLATE,
                         CLASS_TEMPLATE_PARTIAL_SPECIALIZATION})
    FUNCTIONS = frozenset({FUNCTION_DECL, CXX_METHOD, CONSTRUCTOR, DESTRUCTOR,
                           CONVERSION_FUNCTION, FUNCTION_TEMPLATE})
    SCOPES = RECORDS | frozenset({NAMESPACE, ENUM_DECL})


class Linkage:
    INVALID, NONE, INTERNAL, UNIQUE_EXTERNAL, EXTERNAL = range(5)


class Access:
    INVALID, PUBLIC, PROTECTED, PRIVATE = range(4)


class TokenKind:
    PUNCTUATION, KEYWORD, IDENTIFIER, LITERAL, COMMENT = range(5)


class ExceptionSpec:
    NONE = 0              # no specification: may throw
    DYNAMIC_NONE = 1      # throw()
    DYNAMIC = 2
    MS_ANY = 3
    BASIC_NOEXCEPT = 4    # noexcept
    COMPUTED_NOEXCEPT = 5  # noexcept(expr)
    UNEVALUATED = 6
    UNINSTANTIATED = 7
    UNPARSED = 8
    NOTHROW = 9


#: ``(unnamed struct at /abs/f.h:12:5)``, ``(lambda at /abs/f.cpp:3:9)`` in a spelling
_UNNAMED_AT = re.compile(
    r"\(((?:unnamed|anonymous|lambda)(?: struct| union| enum| class)?) at [^()]*\)")


def _no_location(spelling: str) -> str:
    """Drop the source location clang writes into an unnamed type's spelling.

    The path changes with the checkout directory (a ``--rev`` snapshot is built in a temp
    directory) and the line with any edit above, which made every field or parameter of
    such a type look retyped.
    """
    return _UNNAMED_AT.sub(r"(\1)", spelling) if " at " in spelling else spelling


# CXTranslationUnit flags
TU_DETAILED_PREPROCESSING = 0x01
TU_INCOMPLETE = 0x02
TU_SKIP_FUNCTION_BODIES = 0x40
TU_KEEP_GOING = 0x200
TU_IGNORE_NON_ERRORS_FROM_INCLUDED = 0x4000

# CXChildVisitResult
BREAK, CONTINUE, RECURSE = 0, 1, 2


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
_SIGNATURES: list[tuple[str, list, object]] = [
    ("clang_createIndex", [c_int, c_int], c_void_p),
    ("clang_disposeIndex", [c_void_p], None),
    ("clang_parseTranslationUnit2",
     [c_void_p, c_char_p, POINTER(c_char_p), c_int, POINTER(CXUnsavedFile), c_uint, c_uint,
      POINTER(c_void_p)], c_int),
    ("clang_disposeTranslationUnit", [c_void_p], None),
    ("clang_getTranslationUnitCursor", [c_void_p], CXCursor),
    ("clang_getCString", [CXString], c_char_p),
    ("clang_disposeString", [CXString], None),
    ("clang_visitChildren", [CXCursor, VISITOR, py_object], c_uint),
    ("clang_getCursorKind", [CXCursor], c_int),
    ("clang_getCursorSpelling", [CXCursor], CXString),
    ("clang_getCursorDisplayName", [CXCursor], CXString),
    ("clang_getCursorUSR", [CXCursor], CXString),
    ("clang_getCursorLocation", [CXCursor], CXSourceLocation),
    ("clang_getCursorExtent", [CXCursor], CXSourceRange),
    ("clang_getRangeStart", [CXSourceRange], CXSourceLocation),
    ("clang_getRange", [CXSourceLocation, CXSourceLocation], CXSourceRange),
    ("clang_getLocationForOffset", [c_void_p, c_void_p, c_uint], CXSourceLocation),
    ("clang_getRangeEnd", [CXSourceRange], CXSourceLocation),
    ("clang_getExpansionLocation",
     [CXSourceLocation, POINTER(c_void_p), POINTER(c_uint), POINTER(c_uint), POINTER(c_uint)],
     None),
    ("clang_getFileName", [c_void_p], CXString),
    ("clang_Location_isInSystemHeader", [CXSourceLocation], c_int),
    ("clang_getCursorSemanticParent", [CXCursor], CXCursor),
    ("clang_getCursorLexicalParent", [CXCursor], CXCursor),
    ("clang_getCursorReferenced", [CXCursor], CXCursor),
    ("clang_getCursorDefinition", [CXCursor], CXCursor),
    ("clang_getCanonicalCursor", [CXCursor], CXCursor),
    ("clang_isCursorDefinition", [CXCursor], c_uint),
    ("clang_Cursor_isNull", [CXCursor], c_int),
    ("clang_equalCursors", [CXCursor, CXCursor], c_uint),
    ("clang_getCursorType", [CXCursor], CXType),
    ("clang_getCursorResultType", [CXCursor], CXType),
    ("clang_getTypeSpelling", [CXType], CXString),
    ("clang_getCanonicalType", [CXType], CXType),
    ("clang_getTypeDeclaration", [CXType], CXCursor),
    ("clang_getPointeeType", [CXType], CXType),
    ("clang_Type_getNamedType", [CXType], CXType),
    ("clang_isConstQualifiedType", [CXType], c_uint),
    ("clang_Type_getCXXRefQualifier", [CXType], c_int),
    ("clang_Cursor_getNumArguments", [CXCursor], c_int),
    ("clang_Cursor_getArgument", [CXCursor, c_uint], CXCursor),
    ("clang_Cursor_isVariadic", [CXCursor], c_uint),
    ("clang_CXXMethod_isVirtual", [CXCursor], c_uint),
    ("clang_CXXMethod_isPureVirtual", [CXCursor], c_uint),
    ("clang_CXXMethod_isStatic", [CXCursor], c_uint),
    ("clang_CXXMethod_isConst", [CXCursor], c_uint),
    ("clang_CXXMethod_isDefaulted", [CXCursor], c_uint),
    ("clang_CXXRecord_isAbstract", [CXCursor], c_uint),
    ("clang_EnumDecl_isScoped", [CXCursor], c_uint),
    ("clang_getEnumConstantDeclValue", [CXCursor], c_longlong),
    ("clang_getCXXAccessSpecifier", [CXCursor], c_int),
    ("clang_getCursorLinkage", [CXCursor], c_int),
    ("clang_getOverriddenCursors", [CXCursor, POINTER(POINTER(CXCursor)), POINTER(c_uint)],
     None),
    ("clang_disposeOverriddenCursors", [POINTER(CXCursor)], None),
    ("clang_Cursor_isDynamicCall", [CXCursor], c_int),
    ("clang_isVirtualBase", [CXCursor], c_uint),
    ("clang_getSpecializedCursorTemplate", [CXCursor], CXCursor),
    ("clang_getTemplateCursorKind", [CXCursor], c_int),
    ("clang_Cursor_getRawCommentText", [CXCursor], CXString),
    ("clang_Cursor_getMangling", [CXCursor], CXString),
    ("clang_Cursor_getStorageClass", [CXCursor], c_int),
    ("clang_Cursor_hasVarDeclGlobalStorage", [CXCursor], c_int),
    ("clang_Cursor_isInlineNamespace", [CXCursor], c_uint),
    ("clang_Cursor_isAnonymous", [CXCursor], c_uint),
    ("clang_getNumOverloadedDecls", [CXCursor], c_uint),
    ("clang_getOverloadedDecl", [CXCursor, c_uint], CXCursor),
    ("clang_tokenize", [c_void_p, CXSourceRange, POINTER(POINTER(CXToken)), POINTER(c_uint)],
     None),
    ("clang_disposeTokens", [c_void_p, POINTER(CXToken), c_uint], None),
    ("clang_getTokenSpelling", [c_void_p, CXToken], CXString),
    ("clang_getTokenKind", [CXToken], c_int),
    ("clang_getTokenLocation", [c_void_p, CXToken], CXSourceLocation),
    ("clang_getInclusions", [c_void_p, INCLUSION_VISITOR, py_object], None),
    ("clang_getNumDiagnostics", [c_void_p], c_uint),
    ("clang_getDiagnostic", [c_void_p, c_uint], c_void_p),
    ("clang_getDiagnosticSeverity", [c_void_p], c_int),
    ("clang_formatDiagnostic", [c_void_p, c_uint], CXString),
    ("clang_disposeDiagnostic", [c_void_p], None),
    ("clang_getClangVersion", [], CXString),
]
_OPTIONAL: list[tuple[str, list, object]] = [
    ("clang_CXXMethod_isDeleted", [CXCursor], c_uint),
    ("clang_CXXMethod_isExplicit", [CXCursor], c_uint),
    ("clang_getCursorExceptionSpecificationType", [CXCursor], c_int),
    ("clang_getCursorBinaryOperatorKind", [CXCursor], c_int),
    ("clang_getCursorUnaryOperatorKind", [CXCursor], c_int),
    ("clang_getBinaryOperatorKindSpelling", [c_int], CXString),
]

_lib = None
_tried = False


def candidates() -> list[str]:
    """Paths worth trying, most specific first."""
    out: list[str] = []
    env = os.environ.get("MAGELLAN_LIBCLANG")
    if env:
        out.append(env)
    # find_library("clang") looks for clang.dll on Windows; LLVM ships libclang.dll
    out += [f for f in (ctypes.util.find_library("clang"),
                        ctypes.util.find_library("libclang")) if f]
    pats = ["/usr/lib/llvm-*/lib/libclang*.so*", "/usr/lib/x86_64-linux-gnu/libclang*.so*",
            "/usr/lib/aarch64-linux-gnu/libclang*.so*", "/usr/lib64/libclang*.so*",
            "/usr/lib/libclang*.so*", "/usr/local/lib/libclang*.so*",
            "/opt/homebrew/opt/llvm/lib/libclang.dylib", "/usr/local/opt/llvm/lib/libclang.dylib",
            "/Library/Developer/CommandLineTools/usr/lib/libclang.dylib",
            "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain"
            "/usr/lib/libclang.dylib"]
    found_files: list[str] = []
    for p in pats:
        found_files += [f for f in glob.glob(p) if "libclang-cpp" not in f
                        and not f.endswith(".a")]

    out += sorted(set(found_files), key=lambda f: (-_llvm_major(f), f))
    if os.name == "nt":                               # in order of preference
        out += [f for pat in _WINDOWS_LIBCLANG for f in sorted(glob.glob(pat), reverse=True)]
    return list(dict.fromkeys(out))


#: where Windows installs keep libclang.dll: LLVM, Visual Studio's bundled clang, MSYS2
_WINDOWS_LIBCLANG = [
    "C:/Program Files/LLVM/bin/libclang.dll",
    "C:/Program Files/Microsoft Visual Studio/*/*/VC/Tools/Llvm/x64/bin/libclang.dll",
    "C:/msys64/*/bin/libclang.dll",
]


def _llvm_major(path: str) -> int:
    """The LLVM major version a library path names (``llvm-18/``, ``libclang-18.so``), or 0.

    Only those two spellings count: the digits in ``x86_64-linux-gnu`` or in the ABI suffix
    of ``libclang.so.1`` are not a version, and sorting on them preferred an old library.
    """
    m = re.search(r"(?:llvm-|libclang-)(\d+)", path)
    return int(m.group(1)) if m else 0


def load():
    """The loaded library with argtypes set, or ``None``. Never raises."""
    global _lib, _tried
    if _tried:
        return _lib
    _tried = True
    for path in candidates():
        try:
            lib = ctypes.CDLL(path)
            for name, args, res in _SIGNATURES:
                fn = getattr(lib, name)
                fn.argtypes = args
                fn.restype = res
            for name, args, res in _OPTIONAL:
                fn = getattr(lib, name, None)
                if fn is not None:
                    fn.argtypes = args
                    fn.restype = res
            lib._magellan_path = path
            _lib = lib
            return _lib
        except (OSError, AttributeError):
            continue
    return None


def _s(cx: CXString) -> str:
    lib = _lib
    raw = lib.clang_getCString(cx)
    out = raw.decode("utf-8", "replace") if raw else ""
    lib.clang_disposeString(cx)
    return out


def version() -> str:
    lib = load()
    return _s(lib.clang_getClangVersion()) if lib else ""


def library_path() -> str:
    lib = load()
    return getattr(lib, "_magellan_path", "") if lib else ""


def _opt(name: str) -> Callable | None:
    return getattr(_lib, name, None) if _lib is not None else None


# --------------------------------------------------------------------------
# object wrappers
# --------------------------------------------------------------------------
class Type:
    __slots__ = ("t",)

    def __init__(self, t: CXType) -> None:
        self.t = t

    @property
    def kind(self) -> int:
        return self.t.kind

    @property
    def spelling(self) -> str:
        return _no_location(_s(_lib.clang_getTypeSpelling(self.t)))

    @property
    def canonical(self) -> "Type":
        return Type(_lib.clang_getCanonicalType(self.t))

    @property
    def pointee(self) -> "Type":
        return Type(_lib.clang_getPointeeType(self.t))

    @property
    def declaration(self) -> "Cursor":
        return Cursor(_lib.clang_getTypeDeclaration(self.t), None)

    @property
    def is_const(self) -> bool:
        return bool(_lib.clang_isConstQualifiedType(self.t))

    @property
    def ref_qualifier(self) -> str:
        """``""``, ``"&"`` or ``"&&"`` for a member function type."""
        return {1: "&", 2: "&&"}.get(_lib.clang_Type_getCXXRefQualifier(self.t), "")


class Cursor:
    """A cursor plus the translation unit it came from (tokens need it)."""
    __slots__ = ("c", "tu", "_kind")

    def __init__(self, c: CXCursor, tu) -> None:
        self.c = c
        self.tu = tu
        self._kind = None

    # -- identity ---------------------------------------------------------
    @property
    def kind(self) -> int:
        if self._kind is None:
            self._kind = _lib.clang_getCursorKind(self.c)
        return self._kind

    @property
    def is_null(self) -> bool:
        return bool(_lib.clang_Cursor_isNull(self.c))

    def __eq__(self, other) -> bool:
        return isinstance(other, Cursor) and bool(_lib.clang_equalCursors(self.c, other.c))

    def __hash__(self) -> int:
        return hash(self.usr) if self.usr else id(self)

    @property
    def spelling(self) -> str:
        return _no_location(_s(_lib.clang_getCursorSpelling(self.c)))

    @property
    def displayname(self) -> str:
        return _no_location(_s(_lib.clang_getCursorDisplayName(self.c)))

    @property
    def usr(self) -> str:
        return _s(_lib.clang_getCursorUSR(self.c))

    @property
    def mangled(self) -> str:
        return _s(_lib.clang_Cursor_getMangling(self.c))

    @property
    def raw_comment(self) -> str:
        return _s(_lib.clang_Cursor_getRawCommentText(self.c))

    # -- location ---------------------------------------------------------
    @staticmethod
    def _loc(loc: CXSourceLocation) -> tuple[str, int, int, int]:
        f = c_void_p()
        line, col, off = c_uint(), c_uint(), c_uint()
        _lib.clang_getExpansionLocation(loc, byref(f), byref(line), byref(col), byref(off))
        name = _s(_lib.clang_getFileName(f)) if f.value else ""
        return name, line.value, col.value, off.value

    @property
    def location(self) -> tuple[str, int, int, int]:
        """``(file, line, column, offset)`` of the cursor's name."""
        return self._loc(_lib.clang_getCursorLocation(self.c))

    @property
    def extent(self) -> tuple[tuple[str, int, int, int], tuple[str, int, int, int]]:
        r = _lib.clang_getCursorExtent(self.c)
        return self._loc(_lib.clang_getRangeStart(r)), self._loc(_lib.clang_getRangeEnd(r))

    @property
    def in_system_header(self) -> bool:
        return bool(_lib.clang_Location_isInSystemHeader(_lib.clang_getCursorLocation(self.c)))

    # -- navigation -------------------------------------------------------
    def children(self) -> list["Cursor"]:
        out: list[Cursor] = []
        tu = self.tu

        def visit(child, _parent, _data):
            out.append(Cursor(child, tu))
            return CONTINUE
        _lib.clang_visitChildren(self.c, VISITOR(visit), None)
        return out

    def walk(self) -> Iterator["Cursor"]:
        """Pre-order, self included."""
        stack = [self]
        while stack:
            c = stack.pop()
            yield c
            stack.extend(reversed(c.children()))

    def _wrap(self, c: CXCursor) -> "Cursor":
        return Cursor(c, self.tu)

    @property
    def semantic_parent(self) -> "Cursor":
        return self._wrap(_lib.clang_getCursorSemanticParent(self.c))

    @property
    def lexical_parent(self) -> "Cursor":
        return self._wrap(_lib.clang_getCursorLexicalParent(self.c))

    @property
    def referenced(self) -> "Cursor":
        return self._wrap(_lib.clang_getCursorReferenced(self.c))

    @property
    def definition(self) -> "Cursor":
        return self._wrap(_lib.clang_getCursorDefinition(self.c))

    @property
    def canonical(self) -> "Cursor":
        return self._wrap(_lib.clang_getCanonicalCursor(self.c))

    @property
    def is_definition(self) -> bool:
        return bool(_lib.clang_isCursorDefinition(self.c))

    @property
    def specialized_template(self) -> "Cursor":
        return self._wrap(_lib.clang_getSpecializedCursorTemplate(self.c))

    @property
    def template_kind(self) -> int:
        return _lib.clang_getTemplateCursorKind(self.c)

    def overloaded_decls(self) -> list["Cursor"]:
        n = _lib.clang_getNumOverloadedDecls(self.c)
        return [self._wrap(_lib.clang_getOverloadedDecl(self.c, i)) for i in range(n)]

    def overridden(self) -> list["Cursor"]:
        arr = POINTER(CXCursor)()
        n = c_uint()
        _lib.clang_getOverriddenCursors(self.c, byref(arr), byref(n))
        out = [self._wrap(CXCursor.from_buffer_copy(arr[i])) for i in range(n.value)]
        if n.value:
            _lib.clang_disposeOverriddenCursors(arr)
        return out

    # -- types and properties --------------------------------------------
    @property
    def type(self) -> Type:
        return Type(_lib.clang_getCursorType(self.c))

    @property
    def result_type(self) -> Type:
        return Type(_lib.clang_getCursorResultType(self.c))

    def arguments(self) -> list["Cursor"]:
        n = _lib.clang_Cursor_getNumArguments(self.c)
        return [self._wrap(_lib.clang_Cursor_getArgument(self.c, i)) for i in range(max(n, 0))]

    @property
    def num_arguments(self) -> int:
        return _lib.clang_Cursor_getNumArguments(self.c)

    @property
    def is_variadic(self) -> bool:
        return bool(_lib.clang_Cursor_isVariadic(self.c))

    @property
    def is_virtual(self) -> bool:
        return bool(_lib.clang_CXXMethod_isVirtual(self.c))

    @property
    def is_pure_virtual(self) -> bool:
        return bool(_lib.clang_CXXMethod_isPureVirtual(self.c))

    @property
    def is_static_method(self) -> bool:
        return bool(_lib.clang_CXXMethod_isStatic(self.c))

    @property
    def is_const_method(self) -> bool:
        return bool(_lib.clang_CXXMethod_isConst(self.c))

    @property
    def is_defaulted(self) -> bool:
        return bool(_lib.clang_CXXMethod_isDefaulted(self.c))

    @property
    def is_deleted(self) -> bool:
        fn = _opt("clang_CXXMethod_isDeleted")
        return bool(fn(self.c)) if fn else False

    @property
    def is_explicit(self) -> bool:
        fn = _opt("clang_CXXMethod_isExplicit")
        return bool(fn(self.c)) if fn else False

    @property
    def is_abstract(self) -> bool:
        return bool(_lib.clang_CXXRecord_isAbstract(self.c))

    @property
    def is_inline_namespace(self) -> bool:
        return bool(_lib.clang_Cursor_isInlineNamespace(self.c))

    @property
    def is_anonymous(self) -> bool:
        return bool(_lib.clang_Cursor_isAnonymous(self.c))

    @property
    def is_scoped_enum(self) -> bool:
        return bool(_lib.clang_EnumDecl_isScoped(self.c))

    @property
    def enum_value(self) -> int:
        return _lib.clang_getEnumConstantDeclValue(self.c)

    @property
    def access(self) -> int:
        return _lib.clang_getCXXAccessSpecifier(self.c)

    @property
    def linkage(self) -> int:
        return _lib.clang_getCursorLinkage(self.c)

    @property
    def storage_class(self) -> int:
        return _lib.clang_Cursor_getStorageClass(self.c)

    @property
    def has_global_storage(self) -> bool:
        return bool(_lib.clang_Cursor_hasVarDeclGlobalStorage(self.c) == 1)

    @property
    def is_dynamic_call(self) -> bool:
        return bool(_lib.clang_Cursor_isDynamicCall(self.c))

    @property
    def is_virtual_base(self) -> bool:
        return bool(_lib.clang_isVirtualBase(self.c))

    @property
    def exception_spec(self) -> int:
        fn = _opt("clang_getCursorExceptionSpecificationType")
        return fn(self.c) if fn else -1

    @property
    def binary_operator(self) -> str:
        """Spelling of a binary/compound-assignment operator (``=``, ``+=``), or ``""``."""
        kind_fn = _opt("clang_getCursorBinaryOperatorKind")
        spell_fn = _opt("clang_getBinaryOperatorKindSpelling")
        if kind_fn is None or spell_fn is None:
            return ""
        k = kind_fn(self.c)
        return _s(spell_fn(k)) if k else ""

    @property
    def unary_operator_kind(self) -> int:
        fn = _opt("clang_getCursorUnaryOperatorKind")
        return fn(self.c) if fn else 0

    # -- tokens -----------------------------------------------------------
    def tokens(self) -> list[tuple[int, str, int, int]]:
        """``(kind, spelling, line, column)`` for each token in the cursor's extent.

        The range is rebuilt from expansion locations: a declaration that starts with a macro
        (``CLI11_INLINE void f() {...}``) has an extent beginning inside the macro's
        definition, and clang tokenizes such a mixed range as nothing at all.
        """
        tu = self.tu
        rng = _lib.clang_getCursorExtent(self.c)
        fa, fb = c_void_p(), c_void_p()
        oa, ob, dummy = c_uint(), c_uint(), c_uint()
        _lib.clang_getExpansionLocation(_lib.clang_getRangeStart(rng), byref(fa), byref(dummy),
                                        byref(dummy), byref(oa))
        _lib.clang_getExpansionLocation(_lib.clang_getRangeEnd(rng), byref(fb), byref(dummy),
                                        byref(dummy), byref(ob))
        if fa.value and fa.value == fb.value and ob.value >= oa.value:
            rng = _lib.clang_getRange(_lib.clang_getLocationForOffset(tu, fa, oa.value),
                                      _lib.clang_getLocationForOffset(tu, fb, ob.value))
        toks = POINTER(CXToken)()
        n = c_uint()
        _lib.clang_tokenize(tu, rng, byref(toks), byref(n))
        out = []
        line, col = c_uint(), c_uint()
        where = _lib.clang_getExpansionLocation
        for i in range(n.value):
            t = toks[i]
            where(_lib.clang_getTokenLocation(tu, t), None, byref(line), byref(col), None)
            out.append((_lib.clang_getTokenKind(t), _s(_lib.clang_getTokenSpelling(tu, t)),
                        line.value, col.value))
        if n.value:
            _lib.clang_disposeTokens(tu, toks, n)
        return out


class TranslationUnit:
    def __init__(self, index, tu) -> None:
        self.index = index
        self.tu = tu
        # 300 in older libclang (13), 350 in newer ones: take it from the library in use
        K.TRANSLATION_UNIT = self.cursor.kind

    @property
    def cursor(self) -> Cursor:
        return Cursor(_lib.clang_getTranslationUnitCursor(self.tu), self.tu)

    def diagnostics(self, min_severity: int = 3) -> list[str]:
        """Formatted diagnostics at or above ``min_severity`` (3 = error, 4 = fatal)."""
        out = []
        for i in range(_lib.clang_getNumDiagnostics(self.tu)):
            d = _lib.clang_getDiagnostic(self.tu, i)
            if _lib.clang_getDiagnosticSeverity(d) >= min_severity:
                out.append(_s(_lib.clang_formatDiagnostic(d, 0x1 | 0x2)))
            _lib.clang_disposeDiagnostic(d)
        return out

    def inclusions(self) -> list[tuple[str, str, int]]:
        """``(includer, included, line)`` for every #include the preprocessor followed."""
        out: list[tuple[str, str, int]] = []

        def visit(included, stack, depth, _data):
            if depth == 0:
                return
            name = _s(_lib.clang_getFileName(included))
            src, line, _c, _o = Cursor._loc(stack[0])
            out.append((src, name, line))
        _lib.clang_getInclusions(self.tu, INCLUSION_VISITOR(visit), None)
        return out

    def close(self) -> None:
        if self.tu:
            _lib.clang_disposeTranslationUnit(self.tu)
            self.tu = None


class Index:
    def __init__(self) -> None:
        if load() is None:
            raise OSError("libclang not found")
        self.idx = _lib.clang_createIndex(0, 0)

    def parse(self, path: str, args: list[str], flags: int = TU_KEEP_GOING,
              unsaved: dict[str, str] | None = None) -> TranslationUnit | None:
        argv = (c_char_p * len(args))(*[a.encode() for a in args])
        files = list((unsaved or {}).items())
        arr = (CXUnsavedFile * len(files))(*[
            CXUnsavedFile(n.encode(), t.encode(), len(t.encode())) for n, t in files])
        tu = c_void_p()
        err = _lib.clang_parseTranslationUnit2(self.idx, path.encode(), argv, len(args),
                                               arr if files else None, len(files), flags,
                                               byref(tu))
        if err or not tu.value:
            return None
        return TranslationUnit(self, tu)

    def close(self) -> None:
        if self.idx:
            _lib.clang_disposeIndex(self.idx)
            self.idx = None
