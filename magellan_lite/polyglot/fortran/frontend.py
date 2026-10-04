"""Fortran frontend: F77 fixed form through modern free form, in pure Python.

The parser (:mod:`magellan_lite.polyglot.fortran.parser`) finds program units, declarations and
references; this module resolves names the way the language does and emits the
shared graph. No compiler is needed. ``available()`` is always true; gfortran,
when present, is not required for anything (see ``docs/languages.md``).

Name resolution follows the standard's order for a reference inside a unit:
statement functions, local and dummy declarations (``EXTERNAL`` makes a name a
procedure), interface bodies, generic interfaces, contained procedures, derived
types, use association (``ONLY`` lists and ``local => remote`` renames, followed
through modules that re-export), host association, then global external
procedures and finally intrinsics. ``f(x)`` is a function reference when ``f`` is
not a declared array (a declared non-character scalar followed by ``(`` can only
be a typed external function). When the unit's declarations are not all visible
(an unresolved ``INCLUDE``, a ``USE`` of a module outside the project), an
undeclared ``f(x)`` might be an array we cannot see, so the edge gets confidence
0.6 and ``dynamic=True``; if nothing in the project defines ``f`` the edge goes
to an external node at 0.4 (0.9 when the scope is fully visible).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from magellan_lite.polyglot.core.hashing import file_hash
from magellan_lite.polyglot.core.model import Edge, EdgeKind, FileRecord, Node, NodeKind
from magellan_lite.polyglot.fortran import source as fsrc
from magellan_lite.polyglot.fortran.intrinsics import INTRINSIC_MODULES, INTRINSIC_SUBROUTINES, INTRINSICS
from magellan_lite.polyglot.fortran.parser import (Unit, Use, _norm_type, close_paren, parse, positionals,
                                     shape_of, split_top, type_size)
from magellan_lite.polyglot.python.project import read_source

LANG = "fortran"
_UPPER = tuple(s.upper() for s in fsrc.FIXED_SUFFIXES + fsrc.FREE_SUFFIXES)
SOURCE_SUFFIXES = (fsrc.FIXED_SUFFIXES + fsrc.FREE_SUFFIXES + _UPPER
                   + (".inc", ".INC", ".fi", ".fh", ".h"))
CONFIG_FILES = ("fpm.toml",)
SKIP_DIRS = {".git", ".magellan", "node_modules", "build", "_build", "__pycache__", ".venv",
             "venv", "CMakeFiles"}
_UNIT_SUFFIXES = tuple(s for s in SOURCE_SUFFIXES if s.lower() in
                       fsrc.FIXED_SUFFIXES + fsrc.FREE_SUFFIXES)

#: Other named constants of the intrinsic modules
_INTRINSIC_MODULE_NAMES = {
    "output_unit", "input_unit", "error_unit", "iostat_end", "iostat_eor",
    "iostat_inquire_internal_unit", "numeric_storage_size", "character_storage_size",
    "file_storage_size", "character_kinds", "integer_kinds", "logical_kinds", "real_kinds",
    "atomic_int_kind", "atomic_logical_kind", "stat_failed_image", "stat_locked",
    "stat_stopped_image", "stat_unlocked", "team_type", "event_type", "lock_type",
    "c_null_ptr", "c_null_funptr", "c_null_char", "c_new_line", "c_ptr", "c_funptr",
    "c_alert", "c_backspace", "c_form_feed", "c_carriage_return", "c_horizontal_tab",
    "c_vertical_tab", "c_sizeof",
}

#: Every public name of these intrinsic modules has the prefix (ieee_quiet_nan, c_null_ptr, ...)
_INTRINSIC_PREFIX = {"ieee_arithmetic": "ieee_", "ieee_exceptions": "ieee_",
                     "ieee_features": "ieee_", "iso_c_binding": "c_", "omp_lib": "omp_",
                     "omp_lib_kinds": "omp_", "openacc": "acc_"}

#: What the standard says about each legacy construct Magellan tags (for upgrade planning).
LEGACY_STATUS = {
    "arithmetic-if": "obsolescent in F90, deleted in F2018",
    "computed-goto": "obsolescent in F95",
    "assigned-goto": "deleted in F95",
    "assign": "deleted in F95",
    "pause": "deleted in F95",
    "hollerith": "not standard since F77 (H edit descriptor deleted in F95)",
    "alternate-return": "obsolescent in F90",
    "statement-function": "obsolescent in F95",
    "entry": "obsolescent in F2008",
    "shared-do-termination": "obsolescent in F90, deleted in F2018",
    "labelled-do": "obsolescent in F2018",
    "common": "obsolescent in F2018",
    "equivalence": "obsolescent in F2018",
    "block-data": "obsolescent in F2018",
    "forall": "obsolescent in F2018",
    "fixed-form": "obsolescent in F95",
    "implicit-typing": "legal, but IMPLICIT NONE is standard practice since F90",
}

#: kind values of the named constants of iso_fortran_env and iso_c_binding (gfortran, LP64)
_KIND_CONSTANTS = {
    "real32": 4, "real64": 8, "real128": 16, "int8": 1, "int16": 2, "int32": 4, "int64": 8,
    "c_float": 4, "c_double": 8, "c_long_double": 10, "c_float_complex": 4,
    "c_double_complex": 8, "c_int": 4, "c_short": 2, "c_long": 8, "c_long_long": 8,
    "c_signed_char": 1, "c_size_t": 8, "c_int8_t": 1, "c_int16_t": 2, "c_int32_t": 4,
    "c_int64_t": 8, "c_intptr_t": 8, "c_bool": 1, "c_char": 1,
}


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
def has_sources(root: str | Path) -> bool:
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if any(fn.endswith(_UNIT_SUFFIXES) for fn in filenames):
            return True
    return False


def available() -> bool:
    """The pure-Python parser needs nothing; this never raises."""
    return True


def gfortran_version() -> str | None:
    """Version of gfortran if installed (informational only)."""
    import shutil
    import subprocess
    try:
        exe = shutil.which("gfortran")
        if not exe:
            return None
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=10)
        return out.stdout.splitlines()[0] if out.stdout else None
    except Exception:
        return None


def _discover(root: Path) -> tuple[list[str], dict[str, list[str]]]:
    units: list[str] = []
    by_base: dict[str, list[str]] = defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
            by_base[fn.lower()].append(rel)
            if fn.endswith(_UNIT_SUFFIXES):
                units.append(rel)
    return units, by_base


def _dotted(rel: str) -> str:
    return "fortran@" + rel.replace("/", ".")


# --------------------------------------------------------------------------
class Program:
    """Every parsed file of the project and the global tables resolution needs."""

    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, Unit] = {}
        self.forms: dict[str, str] = {}
        self.sources: dict[str, str] = {}
        self.includes: dict[str, fsrc.Source] = {}
        self.modules: dict[str, Unit] = {}
        #: every module of a name: example and test trees reuse names (fpm has several
        #: greet_m); a USE takes the one nearest to it
        self.same_name: dict[str, list[Unit]] = defaultdict(list)
        #: external procedures of library files, by global linker name
        self.externals: dict[str, Unit] = {}
        #: external procedures defined next to a main PROGRAM: part of that executable
        #: (every test driver may define its own FCN), so file-scoped
        self.driver_defs: dict[str, list[Unit]] = defaultdict(list)
        #: library procedures defined again in another library file (LAPACK's SRC/VARIANTS,
        #: one INSTALL/second_*.f per timer): alternatives, only one of which is linked.
        #: The one ``externals`` holds keeps the linker name; these are file-scoped.
        self.variants: dict[int, Unit] = {}
        self.library: dict[str, list[Unit]] = {}
        #: path of the unit whose references are being resolved (picks the local one)
        self.here = ""
        self.entries: dict[str, tuple[Unit, tuple[str, list[str], int]]] = {}
        self.qual: dict[int, str] = {}
        self.by_base: dict[str, list[str]] = {}
        self.parse_errors: list[str] = []
        self._export_cache: dict[tuple[int, str], object] = {}
        self.types_by_name: dict[str, list[Unit]] = defaultdict(list)

    # -- loading -------------------------------------------------------------
    def load(self) -> None:
        units, self.by_base = _discover(self.root)
        for rel in units:
            try:
                text = read_source(self.root / rel)
            except OSError as exc:
                self.parse_errors.append(f"{rel}: {exc}")
                continue
            self.sources[rel] = text
            src = fsrc.read(text, rel, include_text=self._include_text(rel))
            self.forms[rel] = src.form
            try:
                self.files[rel] = parse(src, self._loader(src.form))
            except Exception as exc:  # a parser bug must not take the build down
                self.parse_errors.append(f"{rel}: {type(exc).__name__}: {exc}")

    def _loader(self, form: str):
        def load(target: str, origin: str):
            rel = self._find_include(target, origin)
            if rel is None:
                return None
            key = rel + "|" + form
            if key not in self.includes:
                try:
                    text = read_source(self.root / rel)
                except OSError:
                    return None
                self.sources.setdefault(rel, text)
                f = fsrc.form_of(rel)
                self.includes[key] = fsrc.read(text, rel, form if f == "unknown" else f,
                                               include_text=self._include_text(rel))
            return self.includes[key]
        return load

    def _include_text(self, origin: str):
        """Reads what a ``#include`` inside a statement of ``origin`` names."""
        def text(target: str) -> str | None:
            rel = self._find_include(target, origin)
            if rel is None:
                return None
            if rel not in self.sources:
                try:
                    self.sources[rel] = read_source(self.root / rel)
                except OSError:
                    return None
            return self.sources[rel]
        return text

    def _find_include(self, target: str, origin: str) -> str | None:
        t = target.replace("\\", "/")
        here = os.path.dirname(origin)
        for cand in (os.path.normpath(os.path.join(here, t)), os.path.normpath(t)):
            cand = cand.replace(os.sep, "/")
            if not cand.startswith("..") and (self.root / cand).is_file():
                return cand
        hits = self.by_base.get(os.path.basename(t).lower(), [])
        if hits:
            # nearest to the including file
            return sorted(hits, key=lambda h: (not h.startswith(here), len(h)))[0]
        return None

    # -- tables ----------------------------------------------------------------
    def index(self) -> None:
        library: dict[str, list[Unit]] = defaultdict(list)
        for rel, f in self.files.items():
            driver = any(u.kind == "program" for u in f.children)
            for u in f.children:
                if u.kind == "module":
                    self.modules.setdefault(u.name, u)
                    self.same_name[u.name].append(u)
                elif u.kind in ("subroutine", "function"):
                    if driver:
                        self.driver_defs[u.name].append(u)
                    else:
                        library[u.name].append(u)
        self.library = dict(library)
        for name, defs in library.items():
            # the linked one: the shallowest file (SRC/dgetrf.f, not SRC/VARIANTS/lu/CR/)
            canon = min(defs, key=lambda d: (d.path.count("/"), d.path, d.line))
            self.externals[name] = canon
            for d in defs:
                if d is not canon:
                    self.variants[id(d)] = canon
        for rel, f in self.files.items():
            for u in f.children:
                if u.kind in ("subroutine", "function") and id(u) not in self.variants:
                    for e in u.entries:
                        self.entries.setdefault(e[0], (u, e))
            for u in f.walk():
                if u.kind == "type":
                    self.types_by_name[u.name].append(u)
                for e in u.entries:
                    if u.parent is not None and u.parent.kind != "file":
                        self.entries.setdefault(e[0], (u, e))
        for rel, f in self.files.items():
            self._assign(f, None)

    def _assign(self, u: Unit, scope_q: str | None) -> None:
        if u.kind == "file":
            q = _dotted(u.path)
        elif u.kind == "module":
            q = f"fortran@{u.name}"
        elif u.kind == "submodule":
            q = f"fortran@{u.ancestor}.{u.name}"
        elif u.kind == "program":
            q = f"{scope_q}.{u.name}"
        elif u.kind == "blockdata":
            q = f"fortran@blockdata.{u.name}" if u.name else f"{scope_q}._blockdata"
        elif u.kind in ("subroutine", "function") and u.parent is not None \
                and u.parent.kind == "file":
            # the global linker name, unless it lives in a driver file (see ``driver_defs``)
            q = f"{scope_q}.{u.name}" if self.scoped(u) else f"fortran@{u.name}"
        elif u.kind == "separate" or (u.kind in ("subroutine", "function")
                                      and "module" in u.prefixes and u.parent is not None
                                      and u.parent.kind == "submodule"):
            q = f"fortran@{u.parent.ancestor}.{u.name}"
        else:
            q = f"{scope_q}.{u.name}"
        self.qual[id(u)] = q
        for c in u.children:
            self._assign(c, q)

    def q(self, u: Unit) -> str:
        return self.qual[id(u)]

    # -- scopes ----------------------------------------------------------------
    @staticmethod
    def host(u: Unit) -> Unit | None:
        p = u.parent
        return p if p is not None and p.kind != "file" else None

    def ext(self, name: str) -> Unit:
        """The external procedure ``name`` as linked into the executable of ``self.here``.

        A driver's own definition first; then, among library definitions, the one nearest
        to the caller: LAPACK's test programs link TESTING/LIN/xerbla.f, not SRC/xerbla.f
        (build systems put the local objects before the library). A tie goes to the
        shallowest, which keeps the linker name.
        """
        for u in self.driver_defs.get(name, ()):
            if u.path == self.here:
                return u
        defs = self.library.get(name, ())
        if len(defs) > 1 and self.here:
            here = self.here.split("/")[:-1]
            canon = self.externals[name]

            def near(d: Unit) -> tuple[int, bool]:
                a = d.path.split("/")[:-1]
                k = 0
                while k < min(len(a), len(here)) and a[k] == here[k]:
                    k += 1
                return (k, d is canon)
            return max(defs, key=near)
        if name in self.externals:
            return self.externals[name]
        return self.driver_defs[name][0]

    def has_external(self, name: str) -> bool:
        return name in self.externals or name in self.driver_defs

    def scoped(self, u: Unit) -> bool:
        """An external procedure whose id is file-scoped: a driver's own, or a variant."""
        return self.file_local(u) or id(u) in self.variants

    def file_local(self, u: Unit) -> bool:
        return u.kind in ("subroutine", "function") and u.parent is not None \
            and u.parent.kind == "file" and any(u is d for d in self.driver_defs.get(u.name, ()))

    @staticmethod
    def file_of(u: Unit) -> Unit:
        while u.parent is not None:
            u = u.parent
        return u

    def module_of(self, u: Unit) -> Unit:
        """The MODULE-kind container of a unit: its Fortran module or its file."""
        x = u
        while x is not None:
            if x.kind in ("module", "submodule", "file"):
                return x
            x = x.parent
        return u

    def implicit_type(self, u: Unit, name: str) -> str:
        x: Unit | None = u
        while x is not None and x.kind != "file":
            if name[:1] in x.implicit:
                return x.implicit[name[:1]]
            if x.implicit_none:
                return "?"
            x = x.parent
        return "integer" if name[:1] in "ijklmn" else "real"

    def type_of(self, u: Unit, name: str) -> str:
        x: Unit | None = u
        while x is not None and x.kind != "file":
            v = x.vars.get(name)
            if v is not None and v.type:
                return v.type
            x = x.parent
        ent = self.lookup(u, name, want_proc=False)
        if ent and ent[0] == "var" and ent[2].type:
            return ent[2].type
        return self.implicit_type(u, name)

    def norm_type(self, u: Unit, t: str | None) -> str | None:
        """``real(dp)`` -> ``real*8`` when ``dp`` resolves; ``procedure(f)`` -> ``procedure``.

        Upgrading ``double precision`` to ``real(wp)`` (or renaming ``dp`` to ``wp``) is
        not a type change, and must not be reported as one.
        """
        if not t:
            return t
        if t.startswith("procedure"):
            return "procedure"
        m = re.fullmatch(r"(?:typeof|classof)\(([a-z]\w*)\)", t)      # F2023
        if m:
            return self.norm_type(u, self.type_of(u, m.group(1))) if m.group(1) != "" else t
        m = re.fullmatch(r"(integer|real|complex|logical)\((.+)\)", t)
        if not m:
            return t
        k = self.kind_value(u, m.group(2))
        return _norm_type(m.group(1), str(k)) if k is not None else t

    def kind_value(self, u: Unit, expr: str, depth: int = 0) -> int | None:
        e = expr.replace(" ", "")
        if e.startswith("kind="):
            e = e[5:]
        if re.fullmatch(r"\d+", e):
            return int(e)
        if depth > 8:
            return None
        if e in _KIND_CONSTANTS:
            return _KIND_CONSTANTS[e]
        m = re.fullmatch(r"kind\((.+)\)", e)
        if m:
            lit = m.group(1)
            km = re.search(r"_(\w+)$", lit)
            if km:
                return self.kind_value(u, km.group(1), depth + 1)
            if re.search(r"\d[dD][+-]?\d*$|^[+-]?\d*\.?\d*[dD]", lit):
                return 8
            if re.fullmatch(r"[+-]?\d+", lit):
                return 4
            if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?", lit):
                return 4
            return None
        m = re.fullmatch(r"selected_real_kind\((?:p=)?([^,]+)(?:,.*)?\)", e)
        if m:
            p = self.kind_value(u, m.group(1), depth + 1) if not m.group(1).isdigit() \
                else int(m.group(1))
            if p is None:
                return None
            return 4 if p <= 6 else 8 if p <= 15 else 16
        m = re.fullmatch(r"selected_int_kind\(([^,]+)\)", e)
        if m and m.group(1).isdigit():
            r = int(m.group(1))
            return 1 if r <= 2 else 2 if r <= 4 else 4 if r <= 9 else 8
        if re.fullmatch(r"[a-z]\w*", e):
            # only a named constant can be a kind: a local variable of the same name
            # (ODEPACK had a `dp` array shadowing the kind `dp`) does not hide the host's
            x: Unit | None = u
            while x is not None and x.kind != "file":
                v = x.vars.get(e)
                if v is not None and "parameter" in v.attrs and v.init:
                    return self.kind_value(x, v.init, depth + 1)
                if v is not None and x.parent is not None and x.parent.kind != "file":
                    x = x.parent
                    continue
                break
            ent = self.lookup(x or u, e, want_proc=False)
            if ent and ent[0] == "iconst":
                return _KIND_CONSTANTS.get(ent[1])
            if ent and ent[0] == "var" and ent[2] is not None and ent[2].init \
                    and "parameter" in ent[2].attrs:
                return self.kind_value(ent[1], ent[2].init, depth + 1)
        return None

    def implicit_typing(self, u: Unit) -> bool:
        x: Unit | None = u
        while x is not None and x.kind != "file":
            if x.implicit_none:
                return False
            x = x.parent
        return True

    def incomplete(self, u: Unit) -> bool:
        x: Unit | None = u
        while x is not None:
            if x.unresolved_includes:
                return True
            for use in x.uses:
                if self.module_for(use) is None and use.module not in INTRINSIC_MODULES:
                    return True
            x = x.parent
        return False

    def module_for(self, use) -> Unit | None:
        """The project module a USE names; ``use, intrinsic ::`` never means one (fpm ships
        its own iso_fortran_env for a test, which captured every intrinsic use)."""
        if use.intrinsic:
            return None
        cands = self.same_name.get(use.module, ())
        if len(cands) > 1 and use.path:
            def near(m: Unit) -> tuple[bool, int]:
                a, b = m.path.split("/")[:-1], use.path.split("/")[:-1]
                k = 0
                while k < min(len(a), len(b)) and a[k] == b[k]:
                    k += 1
                return (m.path == use.path, k)
            return max(cands, key=near)
        return self.modules.get(use.module)

    def export(self, mod: Unit, name: str, depth: int = 0):
        key = (id(mod), name)
        if key in self._export_cache:
            return self._export_cache[key]
        self._export_cache[key] = None                   # cycle guard
        ent = None
        private = name in mod.private or (mod.access_default == "private"
                                          and name not in mod.public)
        if not private and depth < 12:
            ent = self._own(mod, name)
            if ent is None:
                for use in mod.uses:
                    ent = self._via_use(use, name, depth + 1)
                    if ent is not None:
                        break
        self._export_cache[key] = ent
        return ent

    def _via_use(self, use, name: str, depth: int = 0):
        mod = self.module_for(use)
        if mod is None and use.module not in INTRINSIC_MODULES:
            return None
        if mod is not None and use.module in INTRINSIC_MODULES:
            # a project module shadowing an intrinsic one (fpm's example iso_fortran_env)
            # is used only for what it defines; output_unit still comes from the compiler
            if self.export(mod, use.renames.get(name, name), depth) is None:
                return self._via_use(Use(use.module, use.only, use.renames, use.line, True),
                                     name, depth)
        if use.only is not None and name not in use.only:
            return None
        if name in use.renames:
            remote = use.renames[name]
        elif name in use.renames.values() and use.only is None:
            return None                                  # renamed away
        else:
            remote = name
        if mod is None:
            # iso_fortran_env / iso_c_binding: a constant (kinds are what types need)
            if remote in _KIND_CONSTANTS or remote in _INTRINSIC_MODULE_NAMES \
                    or use.only is not None or remote.startswith(
                        _INTRINSIC_PREFIX.get(use.module, "\0")):
                return ("iconst", remote)
            return None
        return self.export(mod, remote, depth)

    def _own(self, u: Unit, name: str):
        for c in u.children:
            if c.name == name and c.kind in ("subroutine", "function", "separate"):
                return ("proc", c)
        if name in u.generics:
            return ("generic", u, u.generics[name])
        for c in u.children:
            if c.kind == "type" and c.name == name:
                return ("type", c)
        if name in u.iface_bodies:
            body = u.iface_bodies[name]
            if "module" in body.prefixes and u.kind in ("module", "submodule"):
                return ("separate", u, body)
            return ("iface", u, body)
        v = u.vars.get(name)
        if v is not None:
            return ("var", u, v)
        return None

    def lookup(self, u: Unit, name: str, want_proc: bool = True, call: bool = False):
        """Resolve ``name`` as seen from inside ``u`` (``call``: as the target of CALL)."""
        x: Unit | None = u
        while x is not None and x.kind != "file":
            if name in x.stmt_funcs:
                return ("stmtfunc", x)
            v = x.vars.get(name)
            if x.kind in ("subroutine", "function", "separate") and name in x.args:
                return ("dummy", x, v)                 # incl. dummy procedures (EXTERNAL f)
            if x.kind == "function":
                if x.result and name == x.result:
                    return ("var", x, v)
                if name == x.name:
                    return ("proc", x) if x.result else ("var", x, None)
            if name in x.externals:
                return ("external", x, name)
            if name in x.intrinsics:
                return ("intrinsic",)
            if name in x.iface_bodies:
                return ("iface", x, x.iface_bodies[name])
            if name in x.generics:
                return ("generic", x, x.generics[name])
            if v is not None:
                if "common" in v.attrs:
                    return ("common", x, v)
                return ("var", x, v)
            for c in x.children:
                if c.name == name and c.kind in ("subroutine", "function", "separate"):
                    return ("proc", c)
                if c.name == name and c.kind == "type":
                    return ("type", c)
            for use in x.uses:
                ent = self._via_use(use, name)
                if ent is not None:
                    return ent
            if x.kind in ("module", "submodule"):
                if x.kind == "submodule":
                    anc = self.modules.get(x.ancestor or "")
                    if anc is not None:
                        ent = self._own(anc, name)
                        if ent is not None:
                            return ent
                        for use in anc.uses:
                            ent = self._via_use(use, name)
                            if ent is not None:
                                return ent
            x = x.parent
        if name in INTRINSICS and not (call and name not in INTRINSIC_SUBROUTINES):
            return ("intrinsic",)
        if want_proc:
            if name in self.externals or name in self.driver_defs:
                return ("proc", self.ext(name))
            if name in self.entries:
                return ("entry", self.entries[name])
        return None


# --------------------------------------------------------------------------
def _arity(prog: Program, u: Unit, args: list[str] | None = None, drop: str | None = None,
           explicit: bool | None = None) -> dict:
    args = list(u.args if args is None else args)
    if drop is not None and drop in args:
        args.remove(drop)
    optional = [a for a in args if a in u.vars and "optional" in u.vars[a].attrs]
    required = 0
    for i, a in enumerate(args):
        if a not in optional:
            required = i + 1
    types, shapes, intents, value = {}, {}, {}, []
    # an F77 dummy procedure is often declared nowhere: `CALL FCN(...)` is what says so
    invoked = {r.name for r in u.refs if r.kind == "call"} | {
        r.name for r in u.refs if r.kind == "fref"
        and not (u.vars.get(r.name) is not None and u.vars[r.name].is_array)}
    for a in args:
        if a == "*":
            continue
        v = u.vars.get(a)
        types[a] = prog.norm_type(u, prog.type_of(u, a) if v is None or not v.type else v.type)
        if a in u.iface_bodies:
            types[a] = "procedure"                    # a dummy with an interface block
        elif a in invoked and not (v is not None and (v.type or "").lower()
                                   .startswith("character")):
            # a dummy that is called is a procedure, typed or not (F77 `DOUBLE PRECISION F`
            # then `F(X)`); on a CHARACTER dummy `str(1:n)` is a substring, not a call
            types[a] = "procedure"
        if v is not None and ("external" in v.attrs or a in u.externals):
            types[a] = "procedure"
        elif a in u.externals:
            types[a] = "procedure"
        shapes[a] = shape_of(v.dims if v else None, v.attrs if v else set())
        intents[a] = v.intent if v else ""
        if v is not None and "value" in v.attrs:
            value.append(a)
    if explicit is None:
        explicit = _explicit(u)
    # derived types are left out: their components may be default-initialized, and a
    # component passed on (call get_value(t, "x", self%x)) reads as a use of the whole
    rbw = _read_before_write(prog, u, [a for a in args if a != "*"
                                       and shapes.get(a) == "scalar"
                                       and types.get(a) != "procedure"
                                       and not (types.get(a) or "").startswith(("type(",
                                                                              "class("))])
    out = {
        "lang": LANG,
        "positional": args,
        "required_positional": required,
        "keyword_only": [], "required_keyword_only": [],
        "star_args": False, "star_kwargs": False, "defaults": [],
        "optional": optional, "types": types, "shapes": shapes, "intents": intents,
        "value": value,
        "interface": "explicit" if explicit else "implicit",
        "kind": "function" if u.kind == "function" or (u.kind == "separate" and u.result)
        else u.kind if u.kind in ("subroutine", "function") else "subroutine",
        "bind": u.bind,
        "reads_before_write": rbw,
        "writes": _written(prog, u, [a for a in args if a != "*"]),
    }
    if u.kind == "function":
        rname = u.result or u.name
        rv = u.vars.get(rname)
        out["result"] = prog.norm_type(u, rv.type if rv is not None and rv.type else u.rtype
                                       or prog.implicit_type(u, u.name))
    return out


_READ_KINDS = ("name", "fref", "bound", "call")
_INTRINSIC_OUT_SUBS = {"random_number", "cpu_time", "date_and_time", "system_clock",
                       "get_command_argument", "get_command", "get_environment_variable",
                       "move_alloc", "mvbits", "random_seed", "getarg", "execute_command_line"}


def _unset_locals(prog: "Program", u: Unit, carried: set[str] | None = None) -> list[str]:
    """Scalar locals whose first use reads them: set by nothing before, in statement order.

    In F77 this often "worked" because compilers gave locals static storage, so the value
    from the previous call was still there; with SAVE-less modern compilation (or
    -frecursive, OpenMP) it is garbage. Units with backward GOTOs are skipped: statement
    order says nothing about flow there.
    """
    if any(lab == "?" or (lab in u.label_lines and u.label_lines[lab] <= line)
           for line, lab in u.goto_targets):
        return []
    if u.save_all:
        return []
    names = {r.name for r in u.refs if r.kind in ("name", "write", "mutate", "passed")}
    names -= prog.file_of(u).macros         # FILE_ENCODING: a cpp macro, not a variable
    x: Unit | None = u
    while x is not None:                     # a NAMELIST group name (here or in the host)
        names -= set(x.namelists)
        x = x.parent
    equiv = {n for g in u.equivalences for n in g}
    cands = []
    maybe_imported = prog.incomplete(u)       # USE of a module the project does not have
    for n in names:
        if n in u.args or n in u.stmt_funcs or n in (u.name, u.result) or n in u.saved \
                or n in u.externals \
                or n in u.data_names or n in equiv or (n in INTRINSICS and n not in u.vars):
            continue
        v = u.vars.get(n)
        if v is not None:
            if v.dims is not None or v.init is not None or v.attrs & {
                    "parameter", "save", "common", "external", "pointer", "allocatable",
                    "target", "coarray", "value"} or (v.type or "").startswith(
                    ("type(", "class(", "procedure")):
                continue
        elif prog.lookup(u, n, want_proc=False) is not None or maybe_imported:
            continue                               # host, module or COMMON: not a local
        cands.append(n)
    return _read_before_write(prog, u, cands, lenient=True, carried=carried)


def _callee_intent(prog: "Program", u: Unit, r) -> str | None:
    """Intent of the dummy an actual argument lands on; None when it cannot be known."""
    if not r.obj:
        return None
    ent = prog.lookup(u, r.obj, call=r.in_call)
    if ent is None:
        return None
    body = None
    kind = ent[0]
    if kind == "intrinsic":
        return "out" if r.obj in _INTRINSIC_OUT_SUBS else "in"
    if kind == "proc":
        body = ent[1]
    elif kind in ("iface", "separate"):
        body = ent[2]
    elif kind == "external" and prog.has_external(r.obj):
        body = prog.ext(r.obj)
    elif kind == "dummy":
        body = u.iface_bodies.get(r.obj)
        v = ent[2]
        if body is None and v is not None and v.type and v.type.startswith("procedure("):
            e2 = prog.lookup(u, v.type[len("procedure("):-1], want_proc=False)
            if e2 and e2[0] in ("iface", "separate"):
                body = e2[2]
    if body is None:
        return None
    if r.keywords:
        name = r.keywords[0]
    elif r.args < len(body.args):
        name = body.args[r.args]
    else:
        return None
    v = body.vars.get(name)
    return v.intent if v is not None and v.intent else ""


def _callee_reads(prog: "Program", u: Unit, r, lenient: bool, depth: int) -> str | None:
    """What handing ``r`` to its callee does to it: "read", "write" or None (unknown).

    ``in`` reads and ``out`` writes. For ``inout`` or no intent, a callee whose body we
    have decides by what that body does first with the dummy (ODEPACK declares outputs
    such as IER inout); an interface without a body keeps its declared contract.
    """
    intent = _callee_intent(prog, u, r)
    if intent is None:
        return None
    if intent == "in":
        return "read"
    if intent == "out":
        return "write"
    body = _callee_body(prog, u, r)
    if body is not None and body.kind in ("subroutine", "function", "separate") \
            and depth < 2:
        dummy = r.keywords[0] if r.keywords else (
            body.args[r.args] if r.args < len(body.args) else None)
        if dummy is None:
            return None
        return "read" if dummy in _read_before_write(prog, body, [dummy], lenient,
                                                    depth + 1) else "write"
    if intent == "inout":
        return "read"
    return None if lenient else "read"


def _callee_body(prog: "Program", u: Unit, r) -> Unit | None:
    ent = prog.lookup(u, r.obj, call=r.in_call) if r.obj else None
    if ent is None:
        return None
    if ent[0] == "proc":
        return ent[1]
    if ent[0] == "external" and prog.has_external(r.obj):
        return prog.ext(r.obj)
    return None


def _read_before_write(prog: "Program", u: Unit, names: list[str],
                       lenient: bool = False, depth: int = 0,
                       carried: set[str] | None = None) -> list[str]:
    """Scalar dummies whose first use, in statement order, reads the incoming value.

    Flow-insensitive (a loop or a GOTO can reorder things), which is enough to catch an
    ``intent(out)`` put on an argument the procedure consults first -- minpack's IFLAG.
    Passing it on reads it unless the receiving dummy is ``intent(out)``; a callee we
    cannot see decides nothing. Arrays are left out: element-wise loops (fill x(j) from
    x(j+1..n)) read "before" writing in statement order and are fine.
    """
    want = set(names)
    first: dict[str, str] = {}
    by_stmt: dict[int, list] = defaultdict(list)
    for r in u.refs:
        n = r.obj if r.kind == "bound" else r.name
        if n in want:
            by_stmt[r.seq].append(r)
    for seq in sorted(by_stmt):
        refs = by_stmt[seq]
        for n in {r.obj if r.kind == "bound" else r.name for r in refs}:
            if n in first:
                continue
            mine = [r for r in refs if (r.obj if r.kind == "bound" else r.name) == n
                    and r.kind != "inquiry"]
            passed = [r for r in mine if r.kind == "passed"]
            if passed and all(r.kind in ("passed", "name") for r in mine):
                effects = {_callee_reads(prog, u, r, lenient, depth) for r in passed}
                if effects <= {None}:
                    first[n] = "unknown"          # handed to something we cannot see
                    continue
                first[n] = "read" if "read" in effects else "write"
                if carried is not None and first[n] == "read" and passed[0].loops \
                        and _set_later_in_loop(u, n, passed[0]):
                    carried.add(n)
                continue
            for r in mine:
                if r.kind == "passed":
                    continue
                if r.kind == "bound" and not r.has_paren:
                    continue
                first[n] = "read" if r.kind in _READ_KINDS else "write"
                if carried is not None and first[n] == "read" and r.loops \
                        and _set_later_in_loop(u, n, r):
                    carried.add(n)
                break
    return sorted(n for n, k in first.items() if k == "read")


def _set_later_in_loop(u: Unit, name: str, read) -> bool:
    """Is ``name`` assigned after ``read`` inside a DO loop around both?

    Then the read may see the value an earlier iteration left (minpack's test_chkder sets
    LNP in one branch and reads it, on the last iteration, in the other; ODEPACK's MDP
    FREE is the same shape and was a real bug). Telling the two apart needs path
    feasibility, so these are reported, but as ``low``.
    """
    around = set(read.loops)
    return any(r.name == name and r.seq > read.seq and around & set(r.loops)
               and r.kind in ("write", "mutate", "passed") for r in u.refs)


def _written(prog: "Program", u: Unit, names: list[str]) -> list[str]:
    """Dummies the body assigns, or hands to a dummy that may write (out/inout)."""
    want = set(names)
    out: set[str] = set()
    for r in u.refs:
        n = r.obj if r.kind == "bound" else r.name
        if n not in want or n in out:
            continue
        if r.kind in ("write", "mutate"):
            out.add(n)
        elif r.kind == "passed" and _callee_intent(prog, u, r) in ("out", "inout"):
            out.add(n)
    return sorted(out)


_rx_name = re.compile(r"[a-z]\w*")
_TYPE_WORD = re.compile(r"(integer|real|complex|logical|character|type|class|procedure)")


def _compatible(actual: str | None, dummy: str | None) -> bool:
    """Can an actual of type ``actual`` match a dummy of type ``dummy``? (unknown: yes)"""
    if not actual or not dummy:
        return True
    a, d = _TYPE_WORD.match(actual), _TYPE_WORD.match(dummy)
    if a is None or d is None:
        return True
    derived = ("type", "class")
    return a.group(1) == d.group(1) or (a.group(1) in derived and d.group(1) in derived)


def _explicit(u: Unit) -> bool:
    p = u.parent
    return p is not None and p.kind != "file"


def _signature(ar: dict, name: str) -> str:
    parts = []
    for a in ar["positional"]:
        if a == "*":
            parts.append("*")
            continue
        t = ar["types"].get(a, "?")
        sh = ar["shapes"].get(a, "scalar")
        extra = []
        if ar["intents"].get(a):
            extra.append(f"intent({ar['intents'][a]})")
        if a in ar["optional"]:
            extra.append("optional")
        if a in ar["value"]:
            extra.append("value")
        dims = "" if sh == "scalar" else f"[{sh}]"
        parts.append(f"{a}: {t}{dims}" + (f" {' '.join(extra)}" if extra else ""))
    s = f"{ar['kind']} {name}({', '.join(parts)})"
    if ar.get("result"):
        s += f" -> {ar['result']}"
    if ar.get("bind") is not None:
        s += f" bind(c{', name=' + repr(ar['bind']) if ar['bind'] else ''})"
    return s


# --------------------------------------------------------------------------
# COMMON layouts
# --------------------------------------------------------------------------
def _int_value(prog: Program, u: Unit, expr: str, depth: int = 0) -> int | None:
    expr = expr.strip().replace(" ", "")
    if re.fullmatch(r"[+-]?\d+", expr):
        return int(expr)
    if depth > 6:
        return None
    if re.fullmatch(r"[a-z]\w*", expr):
        x: Unit | None = u
        while x is not None and x.kind != "file":
            v = x.vars.get(expr)
            if v is not None and "parameter" in v.attrs and v.init:
                return _int_value(prog, x, v.init, depth + 1)
            x = x.parent
        ent = prog.lookup(u, expr, want_proc=False)
        if ent and ent[0] == "var" and ent[2] is not None and "parameter" in ent[2].attrs \
                and ent[2].init:
            return _int_value(prog, ent[1], ent[2].init, depth + 1)
        return None
    if expr.startswith("(") and close_paren(expr, 0) == len(expr):
        return _int_value(prog, u, expr[1:-1], depth + 1)
    # split at the last top-level operator of the lowest precedence (left-associative):
    # `2*n+1` is (2*n)+1, `n-2-1` is (n-2)-1; unary signs and `**` are not split points
    for ops in ("+-", "*/"):
        level = 0
        for i in range(len(expr) - 1, 0, -1):
            c = expr[i]
            if c == ")":
                level += 1
            elif c == "(":
                level -= 1
            elif level == 0 and c in ops and expr[i - 1] not in "*/+-(" \
                    and not (c == "*" and (expr[i + 1:i + 2] == "*" or expr[i - 1] == "*")):
                a = _int_value(prog, u, expr[:i], depth + 1)
                b = _int_value(prog, u, expr[i + 1:], depth + 1)
                if a is None or b is None:
                    return None
                if c == "/":
                    return a // b if b and a % b == 0 else None
                return a + b if c == "+" else a - b if c == "-" else a * b
    if expr[:1] in "+-" and len(expr) > 1:
        v = _int_value(prog, u, expr[1:], depth + 1)
        return None if v is None else -v if expr[0] == "-" else v
    return None


def _count(prog: Program, u: Unit, dims: str | None) -> int | str:
    if dims is None:
        return 1
    total: int | str = 1
    for d in split_top(dims):
        lo, _, hi = d.rpartition(":") if ":" in d else ("1", "", d)
        a, b = _int_value(prog, u, lo or "1"), _int_value(prog, u, hi)
        if a is None or b is None or isinstance(total, str):
            total = f"{total}*({d})" if total != 1 else f"({d})"
        else:
            total = total * (b - a + 1)
    return total


def _layout(prog: Program, u: Unit, block: str) -> list[tuple[str, str, int | str]]:
    out = []
    for m in u.commons.get(block, []):
        v = u.vars.get(m)
        t = prog.norm_type(u, prog.type_of(u, m))
        out.append((m, t, _count(prog, u, v.dims if v else None)))
    return out


def _flatten(layout: list[tuple[str, str, int | str]]) -> list[tuple[str, int | str]]:
    runs: list[list] = []
    for _, t, n in layout:
        base, size = type_size(t)
        key = f"{base}*{size}" if not base.startswith(("type", "class")) else base
        if base == "character":
            key, n = "character", (n * size if isinstance(n, int) else f"{n}*{size}")
        if runs and runs[-1][0] == key and isinstance(runs[-1][1], int) and isinstance(n, int):
            runs[-1][1] += n
        else:
            runs.append([key, n])
    return [(k, n) for k, n in runs]


def _bytes(flat) -> int | None:
    total = 0
    for k, n in flat:
        if not isinstance(n, int):
            return None
        m = re.search(r"\*(\d+)$", k)
        total += n * (int(m.group(1)) if m else 1)
    return total


def _layout_text(layout) -> str:
    return ", ".join(f"{n}: {t}" + (f"({c})" if c != 1 else "") for n, t, c in layout)


def _flat_text(flat) -> str:
    return ", ".join(f"{k} x{n}" for k, n in flat)


def compare_layouts(block: str, flats: dict[str, list]) -> list[str]:
    """Human-readable differences between the declarations of one COMMON block."""
    distinct = Counter(json.dumps(f) for f in flats.values())
    if len(distinct) <= 1:
        return []
    canon = json.loads(distinct.most_common(1)[0][0])
    canon_bytes = _bytes(canon)
    out = []
    for who, flat in sorted(flats.items()):
        if json.dumps(flat) == json.dumps(canon):
            continue
        nb = _bytes(flat)
        if block == "":                                   # blank COMMON may differ in length
            k = min(len(flat), len(canon))
            if [tuple(x) for x in flat[:k - 1]] == [tuple(x) for x in canon[:k - 1]] \
                    and (k == 0 or flat[k - 1][0] == canon[k - 1][0]):
                continue
        if nb is not None and canon_bytes is not None and nb != canon_bytes:
            out.append(f"{who} declares {nb} bytes ({_flat_text(flat)}), most declarations "
                       f"have {canon_bytes} ({_flat_text(canon)})")
        else:
            out.append(f"{who} lays it out as {_flat_text(flat)}, most declarations as "
                       f"{_flat_text(canon)}")
    return out


def _permuted_members(names: dict[str, list[str]]) -> list[str]:
    """Declarations that list the usual members in another order.

    Storage is by position, so ``COMMON /INFOC/ NOUT, INFOT`` where every other routine
    has ``INFOT, NOUT`` swaps the two values even though both are INTEGER and the layouts
    agree. Renaming members is legal and common, so only a permutation of the very same
    names counts.
    """
    seqs = Counter(tuple(v) for v in names.values())
    if len(seqs) < 2:
        return []
    canon = seqs.most_common(1)[0][0]
    return [f"{who} lists the same members in another order ({', '.join(seq)}), most "
            f"declarations as {', '.join(canon)}"
            for who, seq in sorted(names.items())
            if tuple(seq) != canon and sorted(seq) == sorted(canon)]


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------
class _Emitter:
    def __init__(self, state, prog: Program):
        self.state = state
        self.graph = state.graph
        self.prog = prog
        self.program_files = frozenset(rel for rel, f in prog.files.items()
                                       if any(c.kind == "program" for c in f.children))
        self.added: set[str] = set()
        self.proc_id: dict[int, str] = {}
        self._pending_binding: list = []
        self._pending_generic_binding: list = []
        self._pending_annot: list[tuple] = []
        self.common_report: dict[str, list[str]] = {}
        #: member references to COMMON blocks, emitted once blocks are grouped
        self._common_refs: list[tuple] = []
        self.first_edge: set[tuple] = set()

    def node(self, **kw) -> Node:
        meta = kw.pop("meta", {})
        meta["lang"] = LANG
        if kw["kind"] in (NodeKind.FUNCTION, NodeKind.METHOD):
            # A caller binds a procedure by name only. When one is moved (into a module,
            # into CONTAINS) or removed while another of that name stays in reach, the
            # edited caller now resolves to that one: core.diff._rebound reads this key
            # and does not count the call as a dangling reference to the removed id.
            meta["overload_key"] = f"{LANG}@{kw['name']}"
        n = Node(meta=meta, **kw)
        got = self.graph.add_node(n)
        self.added.add(got.id)
        return got

    def edge(self, src: str, dst: str, kind: EdgeKind, line: int, path: str, **kw) -> None:
        if src not in self.graph.nodes or dst not in self.graph.nodes:
            return
        self.graph.add_edge(Edge(src=src, dst=dst, kind=kind, lineno=line, path=path, **kw))

    def contains(self, parent: str, child: Node) -> None:
        self.edge(parent, child.id, EdgeKind.CONTAINS, child.lineno, child.path)

    def external(self, name: str, abi: str | None = None, cabi: bool = False) -> str:
        if cabi:
            eid = f"ext:abi:{abi}"
            if eid not in self.graph.nodes:
                self.node(id=eid, kind=NodeKind.EXTERNAL, name=abi, qualname=f"abi:{abi}",
                          module="", meta={"abi_import": abi})
            return eid
        eid = f"ext:fortran@{name}"
        if eid not in self.graph.nodes:
            meta = {"abi_import": abi} if abi else {}
            self.node(id=eid, kind=NodeKind.EXTERNAL, name=name, qualname=f"fortran@{name}",
                      module="", meta=meta)
        return eid

    # -- units -------------------------------------------------------------
    def run(self) -> None:
        prog = self.prog
        for rel, f in sorted(prog.files.items()):
            src_text = prog.sources.get(rel, "")
            self.graph.files[rel] = FileRecord(path=rel, module=prog.q(f),
                                               sha256=file_hash(src_text),
                                               lines=f.end, source_root="")
            has_program = any(c.kind == "program" for c in f.children)
            fnode = self.node(
                id=f"mod:{prog.q(f)}", kind=NodeKind.MODULE, name=rel.rsplit("/", 1)[-1],
                qualname=prog.q(f), module=prog.q(f), path=rel, lineno=1, end_lineno=f.end,
                body_hash=_h("\n".join(f.own)), doc_hash=_h("\n".join(f.comments)),
                tags=sorted({f"{prog.forms.get(rel, 'free')}-form"}
                            | ({"program"} if has_program else set())),
                meta={"form": prog.forms.get(rel), "file_module": True})
            # every external procedure (and ENTRY) is a linker symbol: callable from outside
            fnode.meta["__all__"] = sorted(
                {c.name for c in f.children if c.kind in ("subroutine", "function")}
                | {e[0] for c in f.children for e in c.entries})
            sig = self.state.sig(fnode.id)
            sig.module_parts = tuple(prog.q(f).split("."))
            sig.path_parts = tuple(rel.split("/")) + (("tests",) if _is_test(
                rel, self.program_files) else ())
            sig.has_main_guard = has_program
            sig.own_statement_count = f.stmt_count
            for c in f.children:
                self.unit(c, fnode.id)
        for src, dst, line, path in self._pending_annot:
            self.edge(src, dst, EdgeKind.ANNOTATES, line, path)
        self.includes()
        self.separate_interfaces()
        self.enumerations()
        self.pointer_targets()
        for f in prog.files.values():
            for u in f.walk():
                if u.kind != "file":
                    self.references(u)
        self.commons()
        self.overrides()

    def unit(self, u: Unit, parent_id: str) -> None:
        prog = self.prog
        q = prog.q(u)
        mod = prog.module_of(u.parent) if u.kind not in ("module", "submodule") else u
        modq = prog.q(mod)
        legacy = {k: v for k, v in u.legacy.items() if v}
        meta: dict = {"legacy": legacy} if legacy else {}
        if u.kind in ("module", "submodule"):
            node = self.node(
                id=f"mod:{q}", kind=NodeKind.MODULE, name=u.name, qualname=q, module=q,
                path=u.path, lineno=u.line, end_lineno=u.end, parent=parent_id,
                sig_hash=_h(u.header), body_hash=_h("\n".join(u.own)),
                doc_hash=_h("\n".join(u.comments)),
                tags=sorted({u.kind} | self._legacy_tags(u)), meta=meta)
            self.contains(parent_id, node)
            node.meta["__all__"] = self._public_names(u)
            sig = self.state.sig(node.id)
            sig.module_parts = tuple(q.split("."))
            sig.path_parts = tuple(u.path.split("/"))
            sig.own_statement_count = u.stmt_count
            sig.imported_roots = frozenset(x.module for x in u.uses)
            if u.kind == "submodule":
                anc = prog.modules.get(u.ancestor or "")
                if anc is not None:
                    self.edge(node.id, f"mod:{prog.q(anc)}", EdgeKind.IMPORTS, u.line, u.path)
            self.module_vars(u, node.id)
        elif u.kind == "type":
            self.derived_type(u, parent_id, modq)
            return
        else:
            self.procedure(u, parent_id, modq)
        for c in u.children:
            self.unit(c, f"mod:{q}" if u.kind in ("module", "submodule") else
                      self.proc_id.get(id(u), f"mod:{q}"))
        # generic interfaces declared in this unit
        for g in u.generics.values():
            self.generic(u, g, modq)

    @staticmethod
    def _public_names(m: Unit) -> list[str]:
        names = {c.name for c in m.children if c.kind in ("subroutine", "function", "type",
                                                          "separate")}
        names |= set(m.generics) | {v for v in m.vars if "common" not in m.vars[v].attrs}
        names |= {n for n in m.iface_bodies if "module" in m.iface_bodies[n].prefixes}
        if m.access_default == "private":
            names &= m.public
        names |= m.public                       # re-exported use-associated names
        names -= m.private
        # bind(C) procedures are called from C whatever their Fortran accessibility
        names |= {c.name for c in m.children if c.bind is not None}
        return sorted(n for n in names if re.fullmatch(r"[a-z]\w*", n))

    def _legacy_tags(self, u: Unit) -> set[str]:
        tags = {k for k, v in u.legacy.items() if v and k in (
            "arithmetic-if", "computed-goto", "assigned-goto", "entry", "alternate-return",
            "hollerith", "statement-function", "equivalence", "common", "pause",
            "shared-do-termination", "assign", "forall")}
        if u.kind in ("program", "subroutine", "function", "module", "blockdata") \
                and self.prog.implicit_typing(u):
            tags.add("implicit-typing")
        if u.kind in ("subroutine", "function", "separate") and any(
                v.init is not None and not v.attrs & {"parameter", "save", "common"}
                and n not in u.saved and n not in u.args for n, v in u.vars.items()):
            tags.add("implicit-save")         # `integer :: n = 0` in a procedure is SAVEd
        return tags

    def procedure(self, u: Unit, parent_id: str, modq: str) -> None:
        prog = self.prog
        prog.here = u.path                  # the arity reads callees' bodies via prog.ext
        q = prog.q(u)
        tags = self._legacy_tags(u)
        meta: dict = {}
        if u.legacy:
            meta["legacy"] = {k: v for k, v in u.legacy.items() if v}
        status = {t: LEGACY_STATUS[t] for t in tags | set(meta.get("legacy", {}))
                  | ({"block-data"} if u.kind == "blockdata" else set()) if t in LEGACY_STATUS}
        if status:
            meta["legacy_status"] = status
        if u.kind == "program":
            kind_tags = {"program"}
            ar = None
            sig_text = f"program {u.name}"
        elif u.kind == "blockdata":
            kind_tags = {"block-data"}
            ar = None
            sig_text = f"block data {u.name}"
        else:
            if u.kind == "separate":
                self._adopt_separate(u)
            kind_tags = {u.kind if u.kind != "separate" else "module-procedure"}
            contract = self._separate_interface(u)
            ar = _arity(prog, contract or u, explicit=True if contract else None)
            if contract is not None:
                kind_tags.add("separate")
                meta["interface_in"] = contract.path
            meta["arity"] = ar
            sig_text = _signature(ar, u.name)
            for p in ("pure", "elemental", "recursive"):
                if p in u.prefixes:
                    kind_tags.add(p)
        is_external = u.parent is not None and u.parent.kind == "file" \
            and u.kind in ("subroutine", "function")
        exports = []
        if u.bind is not None:
            exports.append(u.bind or u.name)
            kind_tags.add("bind-c")
        elif is_external:
            exports.append(u.name + "_")                  # gfortran/f77 mangling
        elif u.parent is not None and u.parent.kind == "module" and u.kind != "program":
            exports.append(f"__{u.parent.name}_MOD_{u.name}")
        if exports:
            meta["abi_exports"] = exports
        if u.save_all:
            tags.add("save")
        if prog.file_local(u):
            meta["driver_local"] = True
        if id(u) in prog.variants:
            meta["variant_of"] = f"fn:{prog.q(prog.variants[id(u)])}"
            tags.add("variant")
        public = True
        if u.parent is not None and u.parent.kind == "module":
            m = u.parent
            public = not (u.name in m.private or (m.access_default == "private"
                                                  and u.name not in m.public))
        if u.parent is not None and u.parent.kind not in ("file", "module", "submodule"):
            kind_tags.add("internal")
        sig_payload = json.dumps(ar, sort_keys=True) if ar else sig_text
        node = self.node(
            id=f"fn:{q}", kind=NodeKind.FUNCTION, name=u.name, qualname=q, module=modq,
            path=u.path, lineno=u.line, end_lineno=u.end, parent=parent_id,
            signature=sig_text, public=public,
            tags=sorted(tags | kind_tags),
            sig_hash=_h(sig_payload + "|" + ",".join(sorted(u.prefixes))),
            body_hash=_h("\n".join(u.own)), doc_hash=_h("\n".join(u.comments)), meta=meta)
        self.proc_id[id(u)] = node.id
        self.contains(parent_id, node)
        # ENTRY statements: more ways into the same body
        for name, args, line in u.entries:
            eq = f"fortran@{name}" if is_external and not prog.scoped(u) \
                else f"{prog.q(u.parent)}.{name}"
            ear = _arity(prog, u, args=args)
            ear["kind"] = ar["kind"] if ar else "subroutine"
            emeta = {"arity": ear, "entry_of": node.id}
            emeta["abi_exports"] = [name + "_"] if is_external else []
            en = self.node(id=f"fn:{eq}", kind=NodeKind.FUNCTION, name=name, qualname=eq,
                           module=modq, path=u.path, lineno=line, end_lineno=u.end,
                           parent=node.id, signature=_signature(ear, name),
                           tags=["entry"] + sorted(tags), sig_hash=_h(json.dumps(ear,
                                                                             sort_keys=True)),
                           body_hash=node.body_hash, meta=emeta)
            self.contains(node.id, en)
            # an ENTRY runs its host's body; the "call" passes whatever the host takes
            self.edge(en.id, node.id, EdgeKind.CALLS, line, u.path,
                      meta={"entry": True, "args": len(u.args)})
            self._signals(u, en)
        if u.kind in ("subroutine", "function", "separate", "program"):
            carried: set[str] = set()
            unset = _unset_locals(prog, u, carried)
            if unset:
                node.meta["reads_unset"] = unset
                if carried:
                    node.meta["reads_unset_in_loop"] = sorted(carried)
                node.tags = sorted(set(node.tags) | {"reads-unset-local"})
        self._signals(u, node)
        self.saved_vars(u, node.id, modq)
        # derived types of dummies and locals: the procedure depends on their layout
        for v in u.vars.values():
            self._annotates(u, node.id, v)

    def _separate_interface(self, u: Unit) -> Unit | None:
        """For a submodule's separate module procedure, the interface body in its ancestor."""
        p = u.parent
        if p is None or p.kind != "submodule" or not (u.kind == "separate"
                                                       or "module" in u.prefixes):
            return None
        anc = self.prog.modules.get(p.ancestor or "")
        body = anc.iface_bodies.get(u.name) if anc is not None else None
        return body if body is not None and "module" in body.prefixes else None

    def separate_interfaces(self) -> None:
        """A parent module's ``module subroutine f`` with no submodule body in the project."""
        for m in self.prog.modules.values():
            for name, body in m.iface_bodies.items():
                if "module" not in body.prefixes:
                    continue
                nid = f"fn:{self.prog.q(m)}.{name}"
                if nid in self.graph.nodes:
                    continue
                ar = _arity(self.prog, body, explicit=True)
                node = self.node(id=nid, kind=NodeKind.FUNCTION, name=name,
                                 qualname=nid[3:], module=self.prog.q(m), path=body.path,
                                 lineno=body.line, end_lineno=body.end or body.line,
                                 parent=f"mod:{self.prog.q(m)}", signature=_signature(ar, name),
                                 tags=["separate", "interface-only"],
                                 sig_hash=_h(json.dumps(ar, sort_keys=True)),
                                 body_hash="", meta={"arity": ar})
                self.contains(f"mod:{self.prog.q(m)}", node)

    def _annotates(self, scope: Unit, src: str, v) -> None:
        if not (v.type and v.type.startswith(("type(", "class("))):
            return
        ent = self.prog.lookup(scope, v.type[v.type.index("(") + 1:-1], want_proc=False)
        if ent and ent[0] == "type":           # emitted once every unit exists
            self._pending_annot.append((src, f"cls:{self.prog.q(ent[1])}", v.line, scope.path))

    def _adopt_separate(self, u: Unit) -> None:
        """``module procedure f`` in a submodule takes its interface from the ancestor."""
        anc = self.prog.modules.get(u.parent.ancestor or "") if u.parent else None
        body = anc.iface_bodies.get(u.name) if anc is not None else None
        if body is None:
            return
        u.args = list(body.args)
        u.result = body.result
        for k, v in body.vars.items():
            u.vars.setdefault(k, v)
        u.implicit_none = u.implicit_none or body.implicit_none
        if re.search(r"\bfunction\b", body.header.split("(")[0]):
            u.kind = "function"
            u.rtype = body.rtype

    def _signals(self, u: Unit, node: Node) -> None:
        sig = self.state.sig(node.id)
        sig.module_parts = tuple(node.module.split("."))
        sig.path_parts = tuple(u.path.split("/")) + (("tests",) if _is_test(
            u.path, self.program_files) else ())
        sig.returns_value = u.kind == "function"
        sig.has_params = bool(u.args)
        sig.own_statement_count = u.stmt_count
        sig.calls_exit = u.kind == "program" or u.stops
        sig.calls_open = u.io
        sig.imported_roots = frozenset(x.module for x in u.uses)

    def module_vars(self, u: Unit, mod_id: str) -> None:
        q = self.prog.q(u)
        for name, v in u.vars.items():
            if "common" in v.attrs:
                continue
            vq = f"{q}.{name}"
            const = "parameter" in v.attrs
            private = name in u.private or (u.access_default == "private"
                                            and name not in u.public)
            ann = (v.type or self.prog.implicit_type(u, name)) + (f"({v.dims})" if v.dims else "")
            tags = {"constant"} if const else {"module-variable"}
            if "coarray" in v.attrs:
                tags.add("coarray")
            if "protected" in v.attrs:
                tags.add("protected")
            if v.init is not None and not const:
                tags.add("initialized")
            n = self.node(id=f"var:{vq}", kind=NodeKind.GLOBAL, name=name, qualname=vq,
                          module=q, path=u.path, lineno=v.line, end_lineno=v.line,
                          parent=mod_id, public=not private, tags=sorted(tags),
                          sig_hash=_h(ann + "|" + ",".join(sorted(v.attrs - {'save'}))),
                          body_hash=_h(v.init or ""),
                          meta={"annotation": ann, "value": v.init or ""})
            self.contains(mod_id, n)
            self._annotates(u, n.id, v)
            sig = self.state.sig(n.id)
            sig.module_parts = tuple(q.split("."))
            sig.is_constant = const
            sig.is_mutable_container = (bool(v.dims) or "coarray" in v.attrs) and not const

    def saved_vars(self, u: Unit, proc_id: str, modq: str) -> None:
        """SAVEd locals (explicit, or implied by an initializer) are hidden global state."""
        if u.kind not in ("subroutine", "function", "separate", "program"):
            return
        for name, v in u.vars.items():
            if "parameter" in v.attrs or "common" in v.attrs or name in u.args:
                continue
            explicit = name in u.saved or "save" in v.attrs
            implied = v.init is not None and u.kind != "program"
            coarray = "coarray" in v.attrs          # every image sees it: shared state
            if not (explicit or implied or coarray):
                continue
            q = f"{self.prog.q(u)}.{name}"
            tags = {"save"} | ({"implicit-save"} if implied and not explicit else set()) \
                | ({"coarray"} if coarray else set())
            ann = (v.type or self.prog.implicit_type(u, name)) + (f"({v.dims})" if v.dims else "")
            n = self.node(id=f"var:{q}", kind=NodeKind.GLOBAL, name=name, qualname=q,
                          module=modq, path=u.path, lineno=v.line, end_lineno=v.line,
                          parent=proc_id, public=False, tags=sorted(tags),
                          sig_hash=_h(ann), body_hash=_h(v.init or ""),
                          meta={"annotation": ann, "value": v.init or "", "saved_in": proc_id})
            self.contains(proc_id, n)
            if coarray:
                self.state.sig(n.id).is_mutable_container = True

    def derived_type(self, t: Unit, parent_id: str, modq: str) -> None:
        prog = self.prog
        q = prog.q(t)
        bases = []
        base_unit = self._base(t)
        if base_unit is not None:
            bases.append(prog.q(base_unit))
        elif t.extends:
            bases.append(t.extends)
        tags = {"derived-type"} | {a for a in t.type_attrs if a in ("abstract", "sequence")}
        if any(a.startswith("bind") for a in t.type_attrs):
            tags.add("bind-c")
        public = True
        if t.parent is not None and t.parent.kind == "module":
            m = t.parent
            public = not (t.name in m.private or (m.access_default == "private"
                                                  and t.name not in m.public))
        node = self.node(
            id=f"cls:{q}", kind=NodeKind.CLASS, name=t.name, qualname=q, module=modq,
            path=t.path, lineno=t.line, end_lineno=t.end, parent=parent_id, bases=bases,
            public=public, tags=sorted(tags),
            sig_hash=_h(t.header + "|" + ",".join(bases)), body_hash=_h("\n".join(t.own)),
            doc_hash=_h("\n".join(t.comments)))
        self.contains(parent_id, node)
        if base_unit is not None:
            self.edge(node.id, f"cls:{prog.q(base_unit)}", EdgeKind.INHERITS, t.line, t.path)
        for name, v in t.vars.items():
            cq = f"{q}.{name}"
            ann = (v.type or "?") + (f"({v.dims})" if v.dims else "")
            c = self.node(id=f"attr:{cq}", kind=NodeKind.CLASS_ATTR, name=name, qualname=cq,
                          module=modq, path=t.path, lineno=v.line, end_lineno=v.line,
                          parent=node.id, sig_hash=_h(ann), body_hash=_h(v.init or ""),
                          public="private-components" not in t.type_attrs
                          and "private" not in v.attrs,
                          meta={"annotation": ann, "value": v.init or ""})
            self.contains(node.id, c)
            if v.type and v.type.startswith(("type(", "class(")):
                ent = prog.lookup(t.parent, v.type[v.type.index("(") + 1:-1], want_proc=False) \
                    if t.parent is not None else None
                if ent and ent[0] == "type":
                    self.edge(c.id, f"cls:{prog.q(ent[1])}", EdgeKind.ANNOTATES, v.line, t.path)
        scope = t.parent
        for b in t.bindings:
            bq = f"{q}.{b.name}"
            impl = None
            if b.kind == "procedure" and scope is not None:
                ent = prog.lookup(scope, b.impl)
                if ent and ent[0] == "proc":
                    impl = ent[1]
                elif ent and ent[0] in ("iface", "separate"):
                    impl = ent[2]
            meta: dict = {"binding": b.impl, "binding_kind": b.kind}
            sig_text = f"procedure {b.name} => {b.impl}"
            if impl is not None:
                passed = None
                if "nopass" not in b.attrs and impl.args:
                    pm = next((re.match(r"pass\((\w+)\)", a) for a in b.attrs
                               if a.startswith("pass(")), None)
                    passed = pm.group(1) if pm else impl.args[0]
                ar = _arity(prog, impl, drop=passed, explicit=True)
                meta["arity"] = ar
                meta["passed_object"] = passed
                sig_text = _signature(ar, b.name)
            btags = {"type-bound"} | {a for a in b.attrs if a in ("deferred", "nopass",
                                                                   "non_overridable")}
            if b.kind != "procedure":
                btags.add(b.kind)
            m = self.node(id=f"fn:{bq}", kind=NodeKind.METHOD, name=b.name, qualname=bq,
                          module=modq, path=t.path, lineno=b.line, end_lineno=b.line,
                          parent=node.id, signature=sig_text, tags=sorted(btags),
                          public="private" not in b.attrs,
                          sig_hash=_h(sig_text + "|" + ",".join(sorted(b.attrs))
                                      + "|" + ",".join(b.specifics)),
                          body_hash=_h(b.impl), meta=meta)
            self.contains(node.id, m)
            sig = self.state.sig(m.id)
            sig.module_parts = tuple(modq.split("."))
            sig.path_parts = tuple(t.path.split("/"))
            sig.has_params = True
            if impl is not None and impl.kind != "iface-body":
                self._pending_binding.append((m.id, impl, b.line, t.path))
            for s in b.specifics:
                self._pending_generic_binding.append((m.id, f"fn:{q}.{s}", t, s, b.line))

    def _base(self, t: Unit) -> Unit | None:
        if not t.extends or t.parent is None:
            return None
        ent = self.prog.lookup(t.parent, t.extends, want_proc=False)
        if ent and ent[0] == "type":
            return ent[1]
        cands = self.prog.types_by_name.get(t.extends, [])
        return cands[0] if len(cands) == 1 else None

    def generic(self, u: Unit, g, modq: str) -> None:
        if not re.fullmatch(r"[a-z]\w*", g.name):
            return                                          # operator(...)/assignment(=)
        if any(c.name == g.name for c in u.children if c.kind in ("subroutine", "function")):
            return                                          # the generic shares a specific's name
        q = f"{self.prog.q(u)}.{g.name}"
        public = True
        if u.kind == "module":
            public = not (g.name in u.private or (u.access_default == "private"
                                                  and g.name not in u.public))
        n = self.node(id=f"fn:{q}", kind=NodeKind.FUNCTION, name=g.name, qualname=q, module=modq,
                      path=u.path, lineno=g.line, end_lineno=g.line,
                      parent=f"mod:{modq}" if u.kind in ("module", "submodule")
                      else self.proc_id.get(id(u)),
                      signature=f"generic {g.name} => {', '.join(g.specifics)}",
                      public=public, tags=["generic"],
                      sig_hash=_h(",".join(sorted(g.specifics))),
                      body_hash=_h(",".join(g.specifics)), meta={"specifics": list(g.specifics)})
        if n.parent:
            self.contains(n.parent, n)
        for s in g.specifics:
            tgt = self._proc_target(u, s)
            if tgt:
                self.edge(n.id, tgt, EdgeKind.CALLS, g.line, u.path, dynamic=True,
                          confidence=0.9, meta={"generic": g.name, "binding": True})

    # -- enumerations ----------------------------------------------------------
    def enumerations(self) -> None:
        """``enumeration type`` (F2023) and ``enum, bind(c)``: a CLASS of enumerators."""
        prog = self.prog
        for f in prog.files.values():
            for host in f.walk():
                for e in host.enums:
                    names = list(e.vars)
                    if not names:
                        continue
                    ename = e.name or f"enum_{names[0]}"
                    scope = prog.q(host)
                    q = f"{scope}.{ename}"
                    mod = prog.module_of(host)
                    parent = f"mod:{scope}" if host.kind in ("module", "submodule", "file") \
                        else self.proc_id.get(id(host))
                    cls = self.node(id=f"cls:{q}", kind=NodeKind.CLASS, name=ename, qualname=q,
                                    module=prog.q(mod), path=e.path, lineno=e.line,
                                    end_lineno=e.end or e.line, parent=parent,
                                    tags=["enumeration" if e.name else "enum-bind-c"],
                                    sig_hash=_h(e.header), body_hash=_h(",".join(names)),
                                    meta={"enumerators": names})
                    if parent:
                        self.contains(parent, cls)
                    for n in names:
                        v = e.vars[n]
                        a = self.node(id=f"attr:{q}.{n}", kind=NodeKind.CLASS_ATTR, name=n,
                                      qualname=f"{q}.{n}", module=prog.q(mod), path=e.path,
                                      lineno=v.line, end_lineno=v.line, parent=cls.id,
                                      sig_hash=_h("enumerator"), body_hash=_h(v.init or ""),
                                      meta={"value": v.init or "", "annotation": "enumerator"})
                        self.contains(cls.id, a)

    # -- procedure pointers -----------------------------------------------------
    def pointer_targets(self) -> None:
        """``p => impl`` anywhere: what a call through the pointer ``p`` may run."""
        prog = self.prog
        self.ptr_targets: dict[str, set[str]] = defaultdict(set)
        for f in prog.files.values():
            for u in f.walk():
                prog.here = u.path
                for r in u.refs:
                    if r.kind != "ptrassign":
                        continue
                    ent = prog.lookup(u, r.obj)
                    if ent is None or ent[0] not in ("proc", "iface", "external", "separate",
                                                     "entry"):
                        continue
                    tgt, _ = self._target_of(ent, r.obj)
                    if tgt and tgt.startswith("fn:"):
                        self.ptr_targets[r.name].add(tgt)

    def _through_pointer(self, src: str, name: str, r, path: str) -> None:
        for tgt in sorted(self.ptr_targets.get(name, ())):
            self._call(src, tgt, r, path, 0.5, True, extra={"procedure_pointer": name})

    # -- includes ------------------------------------------------------------
    def includes(self) -> None:
        prog = self.prog
        done: set[str] = set()
        for f in prog.files.values():
            for u in f.walk():
                for target, line, resolved in u.includes:
                    if resolved is None:
                        continue
                    iq = _dotted(resolved)
                    iid = f"mod:{iq}"
                    if resolved not in done and iid not in self.graph.nodes:
                        done.add(resolved)
                        src = next((s for k, s in prog.includes.items()
                                    if k.startswith(resolved + "|")), None)
                        body = "\n".join(re.sub(r"\s+", "", s.text) for s in src.stmts) \
                            if src else ""
                        self.node(id=iid, kind=NodeKind.MODULE, name=resolved.rsplit("/", 1)[-1],
                                  qualname=iq, module=iq, path=resolved, lineno=1,
                                  end_lineno=src.lines if src else 0, tags=["include"],
                                  body_hash=_h(body),
                                  doc_hash=_h("\n".join(c for _, c in src.comments))
                                  if src else "")
                        text = prog.sources.get(resolved, "")
                        if resolved not in self.graph.files:
                            self.graph.files[resolved] = FileRecord(
                                path=resolved, module=iq, sha256=file_hash(text),
                                lines=src.lines if src else 0)
                    holder = prog.module_of(u)
                    self.edge(f"mod:{prog.q(holder)}", iid, EdgeKind.IMPORTS, line, u.path,
                              meta={"include": target})

    # -- references ----------------------------------------------------------
    def _proc_target(self, scope: Unit, name: str) -> str | None:
        ent = self.prog.lookup(scope, name)
        return self._target_of(ent, name)[0] if ent else None

    def _target_of(self, ent, name: str) -> tuple[str | None, str]:
        """(node id, how) for a resolved entity used as a procedure."""
        prog = self.prog
        kind = ent[0]
        if kind == "proc":
            u = ent[1]
            nid = self.proc_id.get(id(u)) or f"fn:{prog.q(u)}"
            return nid, "proc"
        if kind == "entry":
            host, (ename, _, _) = ent[1]
            is_ext = host.parent is not None and host.parent.kind == "file"
            q = f"fortran@{ename}" if is_ext and not prog.scoped(host) \
                else f"{prog.q(host.parent)}.{ename}"
            return f"fn:{q}", "entry"
        if kind == "separate":
            owner, body = ent[1], ent[2]
            anc = owner.ancestor if owner.kind == "submodule" else owner.name
            return f"fn:fortran@{anc}.{body.name}", "proc"
        if kind == "iface":
            body = ent[2]
            if body.bind is not None:
                cname = body.bind or body.name
                # implemented in Fortran here? then it is a plain call
                if prog.has_external(body.name):
                    return f"fn:{prog.q(prog.ext(body.name))}", "proc"
                return self.external(body.name, cname, cabi=True), "abi"
            if prog.has_external(body.name):
                return f"fn:{prog.q(prog.ext(body.name))}", "proc"
            return self.external(body.name, body.name + "_"), "ext"
        if kind == "external":
            nm = ent[2]
            if prog.has_external(nm):
                return f"fn:{prog.q(prog.ext(nm))}", "proc"
            if nm in prog.entries:
                return self._target_of(("entry", prog.entries[nm]), nm)
            return self.external(nm, nm + "_"), "ext"
        if kind == "generic":
            owner = ent[1]
            g = ent[2]
            if any(c.name == g.name for c in owner.children if c.kind in ("subroutine",
                                                                         "function")):
                return None, "generic-same"
            return f"fn:{prog.q(owner)}.{g.name}", "generic"
        if kind == "type":
            return f"cls:{prog.q(ent[1])}", "type"
        return None, kind

    def references(self, u: Unit) -> None:
        prog = self.prog
        if u.kind in ("type", "interface", "iface-body", "enum"):
            return
        src = self.proc_id.get(id(u))
        if src is None:
            if u.kind in ("module", "submodule"):
                src = f"mod:{prog.q(u)}"
            else:
                return
        incomplete = prog.incomplete(u)
        prog.here = u.path
        seen_state: set[tuple] = set()
        mod_holder = prog.module_of(u)
        mod_id = f"mod:{prog.q(mod_holder)}"
        # USE -> IMPORTS (+ aliases for renames)
        for use in u.uses:
            m = prog.module_for(use)
            if m is None:
                if not use.intrinsic and use.module not in INTRINSIC_MODULES:
                    eid = f"ext:fortran@module.{use.module}"
                    if eid not in self.graph.nodes:
                        self.node(id=eid, kind=NodeKind.EXTERNAL, name=use.module,
                                  qualname=f"fortran@module.{use.module}", module="",
                                  meta={"module": True})
                    self.edge(mod_id, eid, EdgeKind.IMPORTS, use.line, u.path)
                continue
            self.edge(mod_id, f"mod:{prog.q(m)}", EdgeKind.IMPORTS, use.line, u.path,
                      meta={"only": use.only} if use.only is not None else {})
            for local, remote in use.renames.items():
                ent = prog.export(m, remote)
                tgt = self._state_or_proc(ent)
                if tgt is None:
                    continue
                aq = f"{prog.q(u)}.{local}"
                a = self.node(id=f"ali:{aq}", kind=NodeKind.IMPORT_ALIAS, name=local,
                              qualname=aq, module=prog.q(mod_holder), path=u.path,
                              lineno=use.line, end_lineno=use.line, parent=src,
                              meta={"target": remote, "module": use.module})
                self.edge(a.id, tgt, EdgeKind.BINDS, use.line, u.path)
        for r in u.refs:
            self.reference(u, src, r, incomplete, seen_state)

    def _state_or_proc(self, ent) -> str | None:
        if ent is None:
            return None
        if ent[0] == "var":
            owner = ent[1]
            return f"var:{self.prog.q(owner)}.{ent[2].name}" if ent[2] is not None else None
        tgt, _ = self._target_of(ent, "")
        return tgt

    def reference(self, u: Unit, src: str, r, incomplete: bool, seen: set) -> None:
        prog = self.prog
        path = u.path
        if r.kind in ("call", "fref"):
            if r.kind == "fref" and r.name in u.args and not (
                    u.vars.get(r.name) is not None and u.vars[r.name].is_array):
                return                                       # dummy procedure or array dummy
            ent = prog.lookup(u, r.name, call=r.kind == "call")
            if ent is None:
                if r.kind == "call":
                    tgt = self.external(r.name, r.name + "_")
                    self._call(src, tgt, r, path, 1.0, False)
                elif not self._declared_scalar_or_array(u, r.name) \
                        and r.name not in prog.file_of(u).macros:
                    tgt = self.external(r.name, r.name + "_")
                    self._call(src, tgt, r, path, 0.4 if incomplete else 0.9, True,
                               extra={"unresolved": True})
                return
            kind = ent[0]
            if kind in ("intrinsic", "stmtfunc", "dummy"):
                return
            if kind in ("var", "common") and ent[2] is not None and ent[2].type \
                    and ent[2].type.startswith("procedure") and "pointer" in ent[2].attrs:
                self._through_pointer(src, r.name, r, path)
                return
            if kind in ("var", "common"):
                v = ent[2]
                if r.kind == "call":
                    tgt, _how = self._target_of(("external", u, r.name), r.name)
                    if tgt:
                        self._call(src, tgt, r, path, 1.0, False)
                    return
                if r.kind == "fref":
                    if v is None or v.is_array:
                        self._state(u, src, ent, "name", r, seen)
                        return
                    if v.is_char and not u.externals.__contains__(r.name):
                        return                               # substring
                    # a typed scalar followed by '(' is a typed external function
                    tgt, how = self._target_of(("external", u, r.name), r.name)
                    if tgt:
                        self._call(src, tgt, r, path, 1.0 if how == "proc" else 0.9, False)
                    return
                return
            tgt, how = self._target_of(ent, r.name)
            if how == "generic":
                self._generic_call(u, src, ent, r, path)
                return
            if how == "generic-same":
                self._generic_call(u, src, ent, r, path)
                return
            if tgt is None:
                return
            if how == "type":
                self.edge(src, tgt, EdgeKind.INSTANTIATES, r.line, path, col=r.col,
                          conditional=r.conditional, meta={"args": r.args})
                return
            declared = r.name in u.vars or r.name in u.externals or kind != "proc" \
                or ent[1].parent is not None and ent[1].parent.kind != "file"
            conf, dyn = 1.0, False
            if r.kind == "fref" and not declared and incomplete:
                conf, dyn = 0.6, True
            self._call(src, tgt, r, path, conf, dyn)
            return
        if r.kind == "bound":
            self._bound(u, src, r, path)
            return
        if r.kind == "passed":
            ent = prog.lookup(u, r.name, want_proc=False)
            if ent is None:
                return
            if ent[0] in ("external", "proc", "iface"):
                tgt, _ = self._target_of(ent, r.name)
                if tgt and (src, tgt, "passed") not in seen:
                    seen.add((src, tgt, "passed"))
                    self.edge(src, tgt, EdgeKind.READS, r.line, path, conditional=r.conditional,
                              meta={"procedure_argument": True})
            return
        if r.kind in ("name", "write", "mutate"):
            ent = prog.lookup(u, r.name, want_proc=False)
            if ent is None:
                return
            if ent[0] in ("var", "common"):
                self._state(u, src, ent, r.kind, r, seen)

    def _declared_scalar_or_array(self, u: Unit, name: str) -> bool:
        x: Unit | None = u
        while x is not None and x.kind != "file":
            if name in x.vars:
                return True
            x = x.parent
        return False

    def _call(self, src: str, dst: str, r, path: str, conf: float, dyn: bool,
              extra: dict | None = None) -> None:
        d = self.graph.nodes.get(dst)
        if d is not None and d.meta.get("driver_local") and d.path != path:
            conf, dyn = min(conf, 0.5), True      # a driver's own routine, from elsewhere
        meta = {"args": r.args, "callee": r.name}
        if r.keywords:
            meta["opaque"] = list(r.keywords)
        if r.alt_returns:
            meta["alt_returns"] = r.alt_returns
        if r.kind == "fref":
            meta["function_ref"] = True
        if extra:
            meta.update(extra)
        self.edge(src, dst, EdgeKind.CALLS, r.line, path, col=r.col, confidence=conf,
                  dynamic=dyn, conditional=r.conditional, context=src.split(":", 1)[-1],
                  meta=meta)

    def _generic_call(self, u: Unit, src: str, ent, r, path: str) -> None:
        owner, g = ent[1], ent[2]
        gid = f"fn:{self.prog.q(owner)}.{g.name}"
        cands = self._specifics(u, r, [self._proc_target(owner, s) for s in g.specifics])
        if gid in self.graph.nodes:
            if len(cands) == 1:
                self._nominal(src, gid, r, path)
            else:
                self._call(src, gid, r, path, 1.0, False)
        for tgt in cands:
            one = len(cands) == 1
            self._call(src, tgt, r, path, 1.0 if one else 0.6, not one,
                       extra={"generic": g.name})

    def _nominal(self, src: str, gid: str, r, path: str) -> None:
        """The call names generic ``gid`` but resolves to one specific (edged separately).

        Kept, so removing the generic still leaves a reference, but below the confidence
        call-graph cycles use (0.6): with it and the generic's edges to every specific, a
        specific that calls a sibling through the generic (json-fortran's print_file
        overloads) looked like it called itself.
        """
        self._call(src, gid, r, path, 0.5, True, extra={"nominal": True})

    def _specifics(self, u: Unit, r, targets: list) -> list[str]:
        """The specifics of a generic a call can resolve to: argument count, then the
        types of the actuals whose type is plain (a declared variable or a literal)."""
        cands = []
        for tgt in targets:
            if tgt is None or tgt not in self.graph.nodes:
                continue
            ar = self.graph.nodes[tgt].meta.get("arity") or {}
            npos = len(ar.get("positional", []))
            if ar and not (ar.get("required_positional", 0) <= r.args + len(r.keywords) <= npos):
                continue
            cands.append((tgt, ar))
        if len(cands) > 1 and r.argtext:
            typed = [(tgt, ar) for tgt, ar in cands
                     if all(_compatible(self._actual_type(u, a),
                                        (ar.get("types") or {}).get(d))
                            for a, d in zip(positionals(r.argtext), ar.get("positional", [])))]
            if typed:
                cands = typed
        return [tgt for tgt, _ in cands]

    def _actual_type(self, u: Unit, text: str) -> str | None:
        t = text.strip()
        if _rx_name.fullmatch(t):
            if self.prog.lookup(u, t, want_proc=False) is None and t not in u.vars:
                return None
            return self.prog.norm_type(u, self.prog.type_of(u, t))
        if re.fullmatch(r"[+-]?\d+(_\w+)?", t):
            return "integer"
        if re.fullmatch(r"(\w+_)?@\d+", t):
            return "character"
        if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)([ed][+-]?\d+)?(_\w+)?", t):
            return "real"
        return None

    def _bound(self, u: Unit, src: str, r, path: str) -> None:
        prog = self.prog
        tunit = None
        poly = False
        if len(r.chain) == 1:
            v = None
            x: Unit | None = u
            while x is not None and x.kind != "file" and v is None:
                v = x.vars.get(r.obj)
                x = x.parent
            if v is None:
                ent = prog.lookup(u, r.obj, want_proc=False)
                if ent and ent[0] == "var":
                    v = ent[2]
            if v is not None and v.type and v.type.startswith(("type(", "class(")):
                tname = v.type[v.type.index("(") + 1:-1]
                ent = prog.lookup(u, tname, want_proc=False)
                if ent and ent[0] == "type":
                    tunit = ent[1]
                    poly = v.type.startswith("class(")
        if tunit is not None:
            t = tunit
            while t is not None:
                mid = f"fn:{prog.q(t)}.{r.name}"
                if mid in self.graph.nodes:
                    b = next((b for b in t.bindings if b.name == r.name), None)
                    if b is not None and b.kind == "generic":
                        cands = self._specifics(u, r, [self._binding_id(t, x)
                                                       for x in b.specifics])
                        if len(cands) == 1:           # the specific binding it resolves to
                            self._nominal(src, mid, r, path)
                            self._call(src, cands[0], r, path, 1.0, False,
                                       extra={"generic": r.name})
                            return
                    self._call(src, mid, r, path, 1.0, False)
                    if poly:
                        for sub in self._descendants(t):
                            sid = f"fn:{prog.q(sub)}.{r.name}"
                            if sid in self.graph.nodes:
                                self._call(src, sid, r, path, 0.6, True)
                    return
                t = self._base(t)
            comp = tunit.vars.get(r.name)
            if comp is not None and comp.type and comp.type.startswith("procedure"):
                self._through_pointer(src, r.name, r, path)   # a procedure pointer component
            return                                            # a component, not a binding
        if not r.has_paren:
            return
        cands = [f"fn:{prog.q(t)}.{r.name}" for ts in prog.types_by_name.values() for t in ts
                 if any(b.name == r.name for b in t.bindings)]
        if 0 < len(cands) <= 4:
            for c in cands:
                self._call(src, c, r, path, 0.3, True)

    def _binding_id(self, t: Unit, name: str) -> str | None:
        """The binding ``name`` of type ``t`` or its nearest ancestor that has it."""
        x: Unit | None = t
        while x is not None:
            bid = f"fn:{self.prog.q(x)}.{name}"
            if bid in self.graph.nodes:
                return bid
            x = self._base(x)
        return None

    def _descendants(self, t: Unit) -> list[Unit]:
        out = []
        for ts in self.prog.types_by_name.values():
            for x in ts:
                b = self._base(x)
                seen = 0
                while b is not None and seen < 20:
                    if b is t:
                        out.append(x)
                        break
                    b = self._base(b)
                    seen += 1
        return out

    def _state(self, u: Unit, src: str, ent, how: str, r, seen: set) -> None:
        prog = self.prog
        owner, v = ent[1], ent[2]
        if ent[0] == "common":
            block = next((b for b, ms in owner.commons.items() if v.name in ms), None)
            if block is not None:
                self._common_refs.append((src, block, owner, how, r, u.path, v.name))
            return
        else:
            if v is None:
                return
            if owner.kind in ("module", "submodule"):
                tgt = f"var:{prog.q(owner)}.{v.name}"
            elif owner is u or owner.kind in ("subroutine", "function", "separate", "program"):
                tgt = f"var:{prog.q(owner)}.{v.name}"
            else:
                return
        if tgt not in self.graph.nodes:
            return
        kind = {"name": EdgeKind.READS, "write": EdgeKind.WRITES,
                "mutate": EdgeKind.MUTATES}[how]
        key = (src, tgt, kind)
        if key in seen:
            return
        seen.add(key)
        self.edge(src, tgt, kind, r.line, u.path, conditional=r.conditional,
                  meta={"member": v.name} if ent[0] == "common" else {})

    # -- COMMON blocks -------------------------------------------------------
    def _uid(self, u: Unit) -> str | None:
        if u.kind in ("module", "submodule"):
            return f"mod:{self.prog.q(u)}"
        return self.proc_id.get(id(u))

    def _program_reach(self) -> dict[str, set[str]]:
        """node id -> the programs (executables) whose call graph reaches it."""
        progs = [nid for nid in self.added if "program" in self.graph.nodes[nid].tags
                 and self.graph.nodes[nid].kind is NodeKind.FUNCTION]
        reach: dict[str, set[str]] = defaultdict(set)
        for p in progs:
            stack, seen = [p], {p}
            while stack:
                x = stack.pop()
                reach[x].add(p)
                for e in self.graph.out_edges(x):
                    if e.dst in seen or e.confidence < 0.6:
                        continue
                    if e.kind is EdgeKind.CALLS or (e.kind is EdgeKind.READS
                                                    and e.meta.get("procedure_argument")):
                        seen.add(e.dst)
                        stack.append(e.dst)
        # a routine no program reaches (called back through a library, say) still belongs
        # with the one main program of its own file: an F77 driver deck is one executable
        by_file: dict[str, list[str]] = defaultdict(list)
        for p in progs:
            by_file[self.graph.nodes[p].path].append(p)
        for nid in self.added:
            n = self.graph.nodes[nid]
            if n.kind is NodeKind.FUNCTION and not reach.get(nid) \
                    and len(by_file.get(n.path, ())) == 1:
                reach[nid].add(by_file[n.path][0])
        return reach

    def _link_groups(self, items: list, reach: dict[str, set[str]]) -> list[tuple[str, list]]:
        """Split a COMMON's declarers into executables that can never be linked together.

        Declarers reached from intersecting sets of programs are one executable. A block
        declared by five test drivers, each its own program, is five blocks, not one
        shared (and mismatched) one. Declarers no program reaches (library routines,
        BLOCK DATA) may be linked into any of them, so they join every group.
        """
        groups: list[tuple[set[str], list]] = []
        loose = []
        for it in items:
            uid = self._uid(it[0])
            progs = reach.get(uid or "", set())
            if not progs:
                loose.append(it)
                continue
            hit = [g for g in groups if g[0] & progs]
            merged = (set(progs), [it])
            for g in hit:
                merged[0].update(g[0])
                merged[1][:0] = g[1]
                groups.remove(g)
            groups.append(merged)
        if len(groups) <= 1:
            return [("", items)]
        # an unreached declarer (dead, or an alternative such as LAPACK's TESTING/LIN/*x.f)
        # is linked, if at all, with the programs built next to it: it joins the groups
        # whose programs share the most of its directory, or every group if none does
        dirs = [{tuple(self.graph.nodes[p].path.split("/")[:-1]) for p in progs}
                for progs, _ in groups]
        extra: list[list] = [[] for _ in groups]
        for it in loose:
            here = it[0].path.split("/")[:-1]

            def shared(d: tuple) -> int:
                k = 0
                while k < min(len(d), len(here)) and d[k] == here[k]:
                    k += 1
                return k
            score = [max(shared(d) for d in ds) for ds in dirs]
            best = max(score)
            for i, sc in enumerate(score):
                if sc == best:
                    extra[i].append(it)
        out = []
        for (progs, members), more in zip(groups, extra):
            first = min(self.graph.nodes[p].qualname for p in progs)
            out.append((first.split("@", 1)[-1], members + more))
        return sorted(out, key=lambda t: t[0])

    def commons(self) -> None:
        prog = self.prog
        decls: dict[str, list[tuple[Unit, list, list]]] = defaultdict(list)
        for f in prog.files.values():
            for u in f.walk():
                for block in u.commons:
                    lay = _layout(prog, u, block)
                    decls[block].append((u, lay, _flatten(lay)))
        reach = self._program_reach()
        gids: dict[tuple[str, int], list[str]] = defaultdict(list)
        for block, all_items in sorted(decls.items()):
            all_items.sort(key=lambda t: (t[0].common_origin.get(block, t[0].path),
                                          t[0].common_lines.get(block, 0)))
            for suffix, items in self._link_groups(all_items, reach):
                gid = _common_id(block, suffix)
                for u, _, _ in items:
                    gids[(block, id(u))].append(gid)
                self._common_node(block, gid, items, suffix)
        for src, block, owner, how, r, path, member in self._common_refs:
            kind = {"name": EdgeKind.READS, "write": EdgeKind.WRITES,
                    "mutate": EdgeKind.MUTATES}[how]
            for gid in gids.get((block, id(owner)), ()):
                if not any(e.dst == gid and e.kind is kind and not e.meta.get("declaration")
                           for e in self.graph.out_edges(src)):
                    self.edge(src, gid, kind, r.line, path, conditional=r.conditional,
                              meta={"member": member})

    def _common_node(self, block: str, gid: str, items: list, suffix: str) -> None:
        prog = self.prog
        first = items[0][0]
        flats = {prog.q(u) if u.kind != "file" else u.path: fl for u, _, fl in items}
        mism = compare_layouts(block, flats) + _permuted_members(
            {prog.q(u) if u.kind != "file" else u.path: [m for m, _, _ in lay]
             for u, lay, _ in items})
        distinct = sorted({json.dumps(fl) for _, _, fl in items})
        named = Counter(_layout_text(lay) for _, lay, _ in items)
        canon_lay = named.most_common(1)[0][0]
        aliased = any(any(m in grp for grp in u.equivalences for m in u.commons[block])
                      for u, _, _ in items)
        saved = any(f"/{block}/" in u.saved for u, _, _ in items)
        tags = {"common-block"}
        if mism:
            tags.add("layout-mismatch")
        if aliased:
            tags.add("aliased")
        if saved:
            tags.add("save")
        layouts = {prog.q(u) if u.kind != "file" else u.path: _layout_text(lay)
                   for u, lay, _ in items}
        origin = first.common_origin.get(block, first.path)
        n = self.node(
            id=gid, kind=NodeKind.GLOBAL, name=f"/{block}/", qualname=gid[4:],
            module="", path=origin, lineno=first.common_lines.get(block, first.line),
            end_lineno=first.common_lines.get(block, first.line), tags=sorted(tags),
            sig_hash=_h("\n".join(distinct)), body_hash=_h("\n".join(sorted(named))),
            meta={"annotation": " | ".join(_flat_text(json.loads(d)) for d in distinct),
                  "value": " | ".join(sorted(named)), "canonical": canon_lay,
                  "layouts": layouts, "layout_mismatch": mism,
                  "declared_in": len(items), "common": block})
        sig = self.state.sig(n.id)
        sig.is_mutable_container = True
        if suffix:
            n.meta["executable"] = suffix
        if mism:
            self.common_report[gid[4:]] = mism
        for u, _, _ in items:
            src = self.proc_id.get(id(u))
            if src is None and u.kind in ("module", "submodule"):
                src = f"mod:{prog.q(u)}"
            if src is None:
                continue
            line = u.common_lines.get(block, u.line)
            self.edge(src, gid, EdgeKind.READS, line, u.path, meta={"common": block,
                                                                    "declaration": True})
            if u.kind == "blockdata" or u.data_names & set(u.commons[block]):
                self.edge(src, gid, EdgeKind.WRITES, line, u.path,
                          meta={"common": block, "data": True})

    def overrides(self) -> None:
        prog = self.prog
        for mid, impl, line, path in self._pending_binding:
            tgt = self.proc_id.get(id(impl)) or f"fn:{prog.q(impl)}"
            ar = self.graph.nodes[tgt].meta.get("arity", {}) if tgt in self.graph.nodes else {}
            self.edge(mid, tgt, EdgeKind.CALLS, line, path,
                      meta={"args": len(ar.get("positional", [])), "binding": True})
        for mid, spec_id, t, s, line in self._pending_generic_binding:
            tt: Unit | None = t
            while tt is not None:
                sid = f"fn:{prog.q(tt)}.{s}"
                if sid in self.graph.nodes:
                    self.edge(mid, sid, EdgeKind.CALLS, line, t.path, dynamic=True,
                              confidence=0.9, meta={"generic": True, "binding": True})
                    break
                tt = self._base(tt)
        for ts in prog.types_by_name.values():
            for t in ts:
                base = self._base(t)
                for b in t.bindings:
                    x = base
                    while x is not None:
                        if any(bb.name == b.name for bb in x.bindings):
                            self.edge(f"fn:{prog.q(t)}.{b.name}", f"fn:{prog.q(x)}.{b.name}",
                                      EdgeKind.OVERRIDES, b.line, t.path)
                            break
                        x = self._base(x)


def _common_id(block: str, executable: str = "") -> str:
    return f"var:fortran@/{block}/" + (f"@{executable}" if executable else "")


#: files with a main program named like this are test drivers (MINPACK's ex/hybdrv.f,
#: ex/chkdrv.f): numerical libraries test through driver programs, not a test framework
_DRIVER = ("drv", "driver", "tst")


def _is_test(rel: str, programs: frozenset[str] = frozenset()) -> bool:
    low = rel.lower()
    parts = low.split("/")
    if any(p in ("test", "tests", "testing") for p in parts[:-1]) or \
            parts[-1].startswith(("test_", "test")) or "_test." in parts[-1]:
        return True
    stem = parts[-1].rsplit(".", 1)[0]
    return rel in programs and (stem.endswith(_DRIVER) or stem.startswith(("tst", "chk")))


# --------------------------------------------------------------------------
def merge(state, root: str | Path) -> set[str]:
    """Add every Fortran file under ``root`` to the graph; returns the ids added."""
    root = Path(root)
    if not has_sources(root):
        return set()
    prog = Program(root)
    prog.load()
    prog.index()
    em = _Emitter(state, prog)
    em.run()
    # refresh signals that depend on edges
    for nid in em.added:
        node = state.graph.nodes.get(nid)
        if node is None or not node.kind.is_callable:
            continue
        sig = state.sig(nid)
        outs = state.graph.out_edges(nid)
        sig.call_count = sum(1 for e in outs if e.kind is EdgeKind.CALLS)
        for e in outs:
            tgt = state.graph.nodes.get(e.dst)
            if tgt is None or tgt.kind is not NodeKind.GLOBAL:
                continue
            if e.kind in (EdgeKind.WRITES, EdgeKind.MUTATES):
                sig.writes_global = sig.mutates_state = True
            elif e.kind is EdgeKind.READS and not e.meta.get("declaration"):
                sig.reads_global = True
    for msg in prog.parse_errors:
        state.graph.diagnostics.append(f"fortran: parse error in {msg}")
    fixed = sum(1 for f in prog.forms.values() if f == "fixed")
    n_units = sum(1 for f in prog.files.values() for u in f.walk() if u.kind != "file")
    mism = em.common_report
    state.graph.diagnostics.append(
        f"fortran: {len(prog.files)} files ({fixed} fixed-form), {n_units} units"
        + (f"; COMMON layout mismatch in {', '.join(q.split('@', 1)[-1] for q in sorted(mism))}"
           if mism else ""))
    return em.added
