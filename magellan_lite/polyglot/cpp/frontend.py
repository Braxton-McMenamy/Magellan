"""C++ frontend, backed by libclang through ctypes.

libclang parses each translation unit exactly as a compiler would (templates, overload
resolution, virtual dispatch, implicit constructor calls), so names resolve through the
type checker rather than by spelling. The binding is :mod:`magellan_lite.polyglot.cpp.libclang` (no pip
packages); the walk runs in a child process (:mod:`magellan_lite.polyglot.cpp.extract`) so a crash in
native code costs only the C++ part of the graph. When no libclang is found the frontend
adds a diagnostic and contributes nothing.

Flags come from ``compile_commands.json`` when there is one (absolute paths from another
checkout are re-rooted); otherwise every ``.cpp/.cc/.cxx`` file is a translation unit
compiled with ``-std=`` from CMake (default C++17) and the project's include directories.
Headers no translation unit reached are parsed on their own.

Mapping onto the shared model
-----------------------------
=============================  ==============================================================
C++                            Magellan
=============================  ==============================================================
source or header file          MODULE ``mod:cpp@src.util.cpp`` (the path, dotted)
namespace                      qualname segments (``cpp@ns.inner.f``); inline namespaces are
                               left out (``fmt::v11`` is ``fmt``); anonymous ones read
                               ``(anonymous)`` and the id gains the file
class / struct / union         CLASS (tags ``struct``/``union``/``abstract``/``final``/
                               ``polymorphic``); class templates tag ``template``;
                               specializations are their own CLASS (``Box<int*>``)
enum / enum class              CLASS tagged ``enum``; enumerators are CLASS_ATTR with ``value``
free function / fn template    FUNCTION; methods, constructors, destructors, operators and
                               conversion functions are METHOD
non-static data member         INSTANCE_ATTR; static data members are CLASS_ATTR
namespace-scope variable       GLOBAL
base class                     INHERITS (``meta`` records ``virtual`` and access)
``override`` of a virtual      OVERRIDES (from clang, not by name)
call / constructor call        CALLS (+ INSTANTIATES to the class for a construction)
call through a base            CALLS to the declared method at 1.0, and to every override at
pointer or reference           0.6, ``dynamic=True``
dependent call in a template   CALLS to each candidate at 0.6, ``meta.dependent``
call clang cannot resolve      CALLS to each candidate at 0.5, ``meta.unresolved`` -- this is
                               how a signature change still finds the calls it broke
``#include`` of a project file IMPORTS between modules
``throw X`` / ``catch (X&)``   RAISES / HANDLES
=============================  ==============================================================

Ids
---
Callables carry their parameter types, because overloads are distinct functions:
``fn:cpp@ns.Cls.m(int,const std::string&) const``. Internal linkage (``static``, anonymous
namespace) prefixes the file: ``fn:cpp@src/a.cpp:helper(int)``. ``meta["overload_key"]`` is
the id without the parameter list, shared by every overload.

Hashes: ``sig_hash`` covers what a caller binds to -- return and parameter types, defaults
(substituted at the call site), cv/ref qualifiers, ``virtual``/``override``/``final``/
``static``/``explicit``/``= delete``, ``noexcept`` and access. ``body_hash`` covers the
tokens after the parameter list, so comments and whitespace do not count. A class's
``sig_hash`` covers its bases and whether it has a vtable; its ``body_hash`` is the layout
(fields in order, virtual methods in order).

``extern "C"`` functions defined here export their unmangled name (``meta["abi_exports"]``);
calls to C-linkage functions not defined in C++ sources go to ``ext:abi:<name>`` for
:mod:`magellan_lite.polyglot.analyze.interop` to link. Mangled names are never linked.

``.h`` files: C++ claims them when the project has no ``.c`` files; otherwise only headers
that read as C++ (classes, namespaces, templates) and the C frontend keeps the rest.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from magellan_lite.polyglot.c.clang_extract import msvc_flags, split_command
from magellan_lite.polyglot.core.hashing import file_hash
from magellan_lite.polyglot.core.model import Edge, EdgeKind, FileRecord, Graph, Node, NodeKind
from magellan_lite.polyglot.cpp.extract import short_hash as _h

LANG = "cpp"
SOURCE_SUFFIXES = (".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h++", ".ipp",
                   ".tpp", ".h")
TU_SUFFIXES = (".cpp", ".cc", ".cxx", ".c++")
HEADER_SUFFIXES = (".hpp", ".hh", ".hxx", ".h++", ".ipp", ".tpp")
CONFIG_FILES = ("compile_commands.json", "CMakeLists.txt")
SKIP_DIRS = {".git", ".magellan", "node_modules", "third_party", "thirdparty", "3rdparty",
             "third-party", "external", "vendor", "_deps", "__pycache__", ".venv", "venv",
             "out", "dist"}
_SKIP_FILES = re.compile(r"^(catch(_amalgamated)?|doctest|gtest|gmock)\.(h|hpp)$", re.I)
_CXX_HINT = re.compile(r"^\s*(class|namespace|template\s*<|using\s+namespace)\b|::|\bpublic:",
                       re.M)

_KINDS = {"class": NodeKind.CLASS, "function": NodeKind.FUNCTION, "method": NodeKind.METHOD,
          "global_var": NodeKind.GLOBAL, "class_attr": NodeKind.CLASS_ATTR,
          "instance_attr": NodeKind.INSTANCE_ATTR}
_EDGES = {"calls": EdgeKind.CALLS, "instantiates": EdgeKind.INSTANTIATES,
          "reads": EdgeKind.READS, "writes": EdgeKind.WRITES, "mutates": EdgeKind.MUTATES,
          "raises": EdgeKind.RAISES, "handles": EdgeKind.HANDLES}
#: calls through a base pointer may run any override; only the runtime knows which
VIRTUAL_CONFIDENCE = 0.6
_IO = ("std.ifstream", "std.ofstream", "std.fstream", "fopen", "std.filesystem", "open",
       "std.basic_ifstream", "std.basic_ofstream", "std.basic_fstream")
_EXIT = {"exit", "abort", "std.exit", "std.abort", "_Exit", "std._Exit", "quick_exit",
         "std.terminate"}


# --------------------------------------------------------------------------
# which files are ours
# --------------------------------------------------------------------------
def _walk(root: Path, skip: frozenset[str] = frozenset()):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and d not in skip
                             and not d.startswith(".") and not d.startswith("cmake-build")
                             and not (d.startswith("build") and _is_build_dir(Path(dirpath, d))))
        for fn in sorted(filenames):
            yield Path(dirpath, fn)


def _is_build_dir(p: Path) -> bool:
    return (p / "CMakeCache.txt").exists() or (p / "Makefile").exists() \
        or (p / "build.ninja").exists() or p.name in ("build", "builds")


def has_sources(root: str | Path) -> bool:
    for p in _walk(Path(root)):
        if p.name.endswith(TU_SUFFIXES + HEADER_SUFFIXES):
            return True
    return False


def owned_files(root: str | Path, skip: Iterable[str] = ()) -> list[str]:
    """Project-relative paths this frontend maps (see the module docstring for ``.h``)."""
    root = Path(root)
    out: list[str] = []
    hs: list[str] = []
    has_c = has_cxx = False
    for p in _walk(root, frozenset(skip)):
        name = p.name
        if _SKIP_FILES.match(name):
            continue
        rel = p.relative_to(root).as_posix()
        if name.endswith(".c"):
            has_c = True
        elif name.endswith(".h"):
            hs.append(rel)
        elif name.endswith(TU_SUFFIXES + HEADER_SUFFIXES):
            has_cxx = True
            out.append(rel)
    if hs and (has_cxx or not has_c):
        for rel in hs:
            if not has_c or _reads_as_cxx(root / rel):
                out.append(rel)
    return sorted(out)


def _reads_as_cxx(p: Path) -> bool:
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
    return bool(_CXX_HINT.search(text))


def available() -> bool:
    """A libclang shared library loads here. Never raises."""
    try:
        from magellan_lite.polyglot.cpp import libclang
        return libclang.load() is not None
    except Exception:
        return False


# --------------------------------------------------------------------------
# compile flags
# --------------------------------------------------------------------------
_STD = re.compile(r"CMAKE_CXX_STANDARD\s+(\d+)|cxx_std_(\d+)")


def _std(root: Path) -> str:
    try:
        text = (root / "CMakeLists.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "c++17"
    stds = [int(a or b) for a, b in _STD.findall(text)]
    # newer standards rarely reject older code; older ones reject newer library calls
    return f"c++{max(stds + [17])}" if stds else "c++17"


def _include_dirs(root: Path, owned: list[str]) -> list[str]:
    dirs = {root}
    for rel in owned:
        parts = rel.split("/")
        for i, part in enumerate(parts[:-1]):
            if part in ("include", "inc", "src", "source", "lib"):
                dirs.add(root.joinpath(*parts[:i + 1]))
    return [str(d) for d in sorted(dirs)]


_KEEP_FLAG = re.compile(r"^-(I|D|U|std=|isystem|include|iquote|f(no-)?(exceptions|rtti|"
                        r"char8_t|modules)|x)")


def _compile_db(root: Path) -> list[dict] | None:
    for cand in [root / "compile_commands.json"] + sorted(root.glob("*/compile_commands.json")):
        if cand.is_file():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
    return None


def _reroot(path: str, directory: str, root: Path, owned: set[str]) -> str | None:
    """Map a path from another checkout onto ``root`` by its longest owned suffix."""
    p = os.path.normpath(os.path.join(directory, path))
    try:
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        if rel in owned:
            return rel
    except ValueError:
        pass
    parts = p.replace(os.sep, "/").split("/")
    for i in range(1, len(parts)):
        cand = "/".join(parts[i:])
        if cand in owned:
            return cand
    return None


def _db_args(entry: dict, root: Path) -> list[str]:
    raw = msvc_flags(entry.get("arguments") or split_command(entry.get("command", "")))
    directory = entry.get("directory", "")
    out: list[str] = []
    it = iter(raw[1:])
    for a in it:
        if a in ("-I", "-isystem", "-iquote", "-include", "-D", "-U", "-x"):
            nxt = next(it, "")
            a = a + nxt if a in ("-D", "-U") else a
            if a in ("-I", "-isystem", "-iquote", "-include"):
                out += [a, _reroot_dir(nxt, directory, root)]
            else:
                out.append(a if a != "-x" else f"-x{nxt}")
            continue
        if _KEEP_FLAG.match(a):
            if a.startswith(("-I", "-isystem")) and len(a) > 2 and not a.startswith("-isystem") \
                    or a.startswith("-isystem") and len(a) > 8:
                flag = "-I" if a.startswith("-I") else "-isystem"
                out += [flag, _reroot_dir(a[len(flag):], directory, root)]
            else:
                out.append(a)
    return out


def _reroot_dir(d: str, directory: str, root: Path) -> str:
    p = os.path.normpath(os.path.join(directory, d))
    if os.path.isdir(p):
        return p
    parts = p.replace(os.sep, "/").split("/")
    for i in range(1, len(parts)):
        cand = root.joinpath(*parts[i:])
        if cand.is_dir():
            return str(cand)
    return p


LEGACY_INCLUDE = Path(__file__).with_name("legacy_include")


def plan(root: str | Path, owned: list[str]) -> tuple[list[dict], list[dict], str]:
    """``(translation units, standalone headers, where the flags came from)``."""
    root = Path(root).resolve()
    owned_set = set(owned)
    # pre-standard headers (<iostream.h>, <fstream.h>) for code older than C++98, searched
    # after everything else so a project's own headers always win
    common = ["-xc++", f"-std={_std(root)}", "-Wno-everything",
              "-ferror-limit=0", "-idirafter", str(LEGACY_INCLUDE)]
    if os.name == "nt":
        # an older libclang meets a newer Visual Studio STL, which refuses it (STL1000);
        # reading declarations does not need the two to agree
        common.append("-D_ALLOW_COMPILER_AND_STL_VERSION_MISMATCH")
    common += [x for d in _include_dirs(root, owned) for x in ("-I", d)]
    tus: list[dict] = []
    source = "defaults"
    db = _compile_db(root)
    if db:
        seen: set[str] = set()
        for entry in db:
            rel = _reroot(entry.get("file", ""), entry.get("directory", ""), root, owned_set)
            if rel is None or rel in seen or not rel.endswith(TU_SUFFIXES):
                continue
            seen.add(rel)
            tus.append({"file": str(root / rel), "args": common + _db_args(entry, root)})
        if tus:
            source = "compile_commands.json"
    listed = {Path(t["file"]).relative_to(root).as_posix() for t in tus}
    for rel in owned:
        if rel.endswith(TU_SUFFIXES) and rel not in listed:
            tus.append({"file": str(root / rel), "args": common})
    headers = [{"file": str(root / rel), "args": common} for rel in owned
               if not rel.endswith(TU_SUFFIXES)]
    return tus, headers, source


# --------------------------------------------------------------------------
# running the extractor
# --------------------------------------------------------------------------
def extract(root: str | Path, owned: list[str], timeout: int | None = None) -> dict | None:
    tus, headers, source = plan(root, owned)
    jobs = int(os.environ.get("MAGELLAN_CPP_JOBS") or min(os.cpu_count() or 1, 16))
    job = {"root": str(Path(root).resolve()), "owned": owned, "tus": tus, "headers": headers,
           "jobs": jobs}
    timeout = timeout or int(os.environ.get("MAGELLAN_CPP_TIMEOUT") or 1200)
    env = dict(os.environ)
    pkg_root = str(Path(__file__).resolve().parent.parent.parent)
    env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH")
                                    else "")
    try:
        r = subprocess.run([sys.executable, "-m", "magellan_lite.polyglot.cpp.extract"],
                           input=json.dumps(job), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": f"extractor exited {r.returncode}: {r.stderr.strip()[-300:]}"}
    data["flags_from"] = source
    return data


# --------------------------------------------------------------------------
# ids
# --------------------------------------------------------------------------
def module_name(rel: str) -> str:
    return f"{LANG}@" + rel.replace("/", ".")


def _qual(d: dict) -> str:
    base = ".".join(d["scope"] + [d["name"]])
    if d.get("internal"):
        return f"{LANG}@{d['file']}:{base}"
    return f"{LANG}@{base}"


def _params_sig(d: dict) -> str:
    types = [p["type"] for p in d.get("params", [])]
    if d.get("variadic"):
        types.append("...")
    f = d.get("flags", {})
    tail = (" const" if f.get("const") else "") + (f.get("ref") or "")
    return "(" + ",".join(types) + ")" + tail


def _qualname(d: dict) -> str:
    q = _qual(d)
    if d["nk"] in ("function", "method"):
        q += _params_sig(d)
    return q


def _prefix(nk: str) -> str:
    return _KINDS[nk].prefix


# --------------------------------------------------------------------------
# merging records from many translation units
# --------------------------------------------------------------------------
def _merge_records(decls: list[dict]) -> dict[str, dict]:
    """One record per USR: the definition wins; defaults come from the declaration."""
    by_usr: dict[str, list[dict]] = {}
    for d in decls:
        if d.get("usr"):
            by_usr.setdefault(d["usr"], []).append(d)
    out: dict[str, dict] = {}
    for usr, recs in by_usr.items():
        recs.sort(key=lambda r: (not r.get("is_def"), r["file"], r["line"]))
        primary = dict(recs[0])
        primary["declared_in"] = sorted({r["file"] for r in recs})
        if "params" in primary:
            for r in recs:
                ps = r.get("params") or []
                if len(ps) == len(primary["params"]) and any(p.get("default") for p in ps):
                    primary["params"] = [dict(p, name=p["name"] or q["name"])
                                         for p, q in zip(ps, primary["params"])]
                    break
            names = [next((r["params"][i]["name"] for r in recs
                           if len(r.get("params") or []) > i and r["params"][i]["name"]), "")
                     for i in range(len(primary["params"]))]
            primary["params"] = [dict(p, name=n or p["name"])
                                 for p, n in zip(primary["params"], names)]
            flags = dict(primary.get("flags", {}))
            for r in recs:
                for k in ("override", "final", "virtual", "explicit", "static", "deleted"):
                    if r.get("flags", {}).get(k):
                        flags[k] = True
                if r.get("access") and not primary.get("access"):
                    primary["access"] = r["access"]
            primary["flags"] = flags
            primary["tags"] = sorted({t for r in recs for t in r.get("tags", [])})
            primary["overridden"] = sorted({u for r in recs for u in r.get("overridden", [])})
        if not primary.get("doc"):
            primary["doc"] = next((r["doc"] for r in recs if r.get("doc")), "")
        primary["has_def"] = any(r.get("is_def") for r in recs)
        out[usr] = primary
    return out


def _arity(d: dict) -> dict:
    params = d.get("params", [])
    names = [p["name"] or f"arg{i}" for i, p in enumerate(params)]
    required = len(params)
    for i, p in enumerate(params):
        if p.get("default") is not None or "..." in p["type"]:
            required = i                    # a default, or a parameter pack (may be empty)
            break
    pack = any("..." in p["type"] for p in params)
    f = d.get("flags", {})
    return {
        "lang": LANG,
        "positional": names,
        "types": [p["type"] for p in params],
        "required_positional": required,
        "defaults": [p.get("default") or "?" for p in params[required:]
                     if "..." not in p["type"]],
        "keyword_only": [], "required_keyword_only": [],
        "star_args": bool(d.get("variadic") or pack), "star_kwargs": False,
        "returns": d.get("ret", ""),
        # what the type is, not how it is spelled: `size_t` -> `std::size_t` changes nothing
        "returns_canonical": d.get("ret_canonical", d.get("ret", "")),
        "virtual": bool(f.get("virtual")), "pure": bool(f.get("pure")),
        "static": bool(f.get("static")), "const": bool(f.get("const")),
        "ref": f.get("ref") or "", "explicit": bool(f.get("explicit")),
        "deleted": bool(f.get("deleted")), "noexcept": f.get("noexcept", ""),
        "override": bool(f.get("override")), "final": bool(f.get("final")),
        "access": d.get("access", ""), "template": "template" in d.get("tags", []),
        "ctor": "ctor" in d.get("tags", []),
    }


def _signature(d: dict) -> str:
    ps = ", ".join(p["type"] + (f" {p['name']}" if p["name"] else "")
                   + (f" = {p['default']}" if p.get("default") else "")
                   for p in d.get("params", []))
    if d.get("variadic"):
        ps += ", ..." if ps else "..."
    f = d.get("flags", {})
    pre = ("virtual " if f.get("virtual") and not f.get("override") else "") + \
        ("static " if f.get("static") else "") + ("explicit " if f.get("explicit") else "")
    ret = (d.get("ret") + " ") if d.get("ret") else ""
    tail = (" const" if f.get("const") else "") + (f.get("ref") or "") + \
        (f" {f['noexcept']}" if f.get("noexcept") else "") + \
        (" override" if f.get("override") else "") + (" final" if f.get("final") else "") + \
        (" = 0" if f.get("pure") else "") + (" = delete" if f.get("deleted") else "")
    tp = d.get("template_params") or []
    head = f"template <{', '.join(tp)}> " if tp and d["nk"] in ("function", "method") else ""
    return f"{head}{pre}{ret}{d['name']}({ps}){tail}"


# --------------------------------------------------------------------------
# merge into the graph
# --------------------------------------------------------------------------
def merge(state, root: str | Path) -> set[str]:
    graph: Graph = state.graph
    root = Path(root)
    if not has_sources(root):
        return set()
    owned = owned_files(root, getattr(getattr(state, "config", None), "excludes", ()) or ())
    if not owned:
        return set()
    if not available():
        graph.diagnostics.append(
            "C++ sources found but libclang is unavailable; skipped (install libclang, or "
            "set MAGELLAN_LIBCLANG to the path of libclang.so, libclang.dylib or libclang.dll)")
        return set()
    data = extract(root, owned)
    if data is None or "error" in data:
        graph.diagnostics.append(f"C++ extraction failed: {(data or {}).get('error', '?')}")
        return set()
    return build(graph, root, owned, data, state)


def amalgamated_copies(decls: list[dict], min_defs: int = 10) -> set[str]:
    """Files that are generated copies of the rest of the project (``single_include/x.hpp``).

    A single-header distribution repeats every definition of the library, with the same
    USRs, and is regenerated only now and then. Mapped, it is a second body for every
    function, and whatever it still defines after the real headers moved on (an overload
    since replaced) reads as a live function that lost its callers. It is recognised by
    what it contains, not by its name: at least ``min_defs`` definitions, 80% of them also
    defined elsewhere, spread over at least three other files. An ordinary header whose
    definitions are all copied into the amalgamation has its twins in one file only.
    """
    homes: dict[str, set[str]] = {}
    for d in decls:
        if d.get("is_def") and d.get("usr") and d.get("nk") in ("function", "method", "class"):
            homes.setdefault(d["usr"], set()).add(d["file"])
    defs: dict[str, set[str]] = {}
    for usr, files in homes.items():
        for f in files:
            defs.setdefault(f, set()).add(usr)
    out: set[str] = set()
    for f, usrs in defs.items():
        if len(usrs) < min_defs:
            continue
        shared = [u for u in usrs if len(homes[u]) > 1]
        twins = {g for u in shared for g in homes[u]} - {f}
        if len(shared) >= 0.8 * len(usrs) and len(twins) >= 3:
            out.add(f)
    return out


def build(graph: Graph, root: Path, owned: list[str], data: dict, state=None) -> set[str]:
    added: set[str] = set()
    copies = amalgamated_copies(data["decls"])
    if copies:
        data = dict(data, decls=[d for d in data["decls"] if d["file"] not in copies],
                    refs=[r for r in data["refs"] if r["file"] not in copies])
        graph.diagnostics.append(
            f"cpp: {', '.join(sorted(copies))} repeat(s) the project's own definitions "
            f"(a generated single-header copy); mapped as files only")
    records = _merge_records(data["decls"])

    # -- modules ---------------------------------------------------------------
    includes: dict[str, set[str]] = {}
    for a, b, _line in data.get("includes", []):
        includes.setdefault(a, set()).add(b)
    for rel in owned:
        try:
            raw = (root / rel).read_bytes()
        except OSError:
            continue
        mod = module_name(rel)
        graph.files[rel] = FileRecord(path=rel, module=mod,
                                      sha256=file_hash(raw.decode("utf-8", "replace")),
                                      lines=raw.count(b"\n") + 1, source_root="")
        node = Node(id=f"mod:{mod}", kind=NodeKind.MODULE, name=rel.rsplit("/", 1)[-1],
                    qualname=mod, module=mod, path=rel, lineno=1,
                    end_lineno=raw.count(b"\n") + 1,
                    body_hash=_h(sorted(includes.get(rel, ()))), meta={"lang": LANG})
        if graph.add_node(node) is node:
            added.add(node.id)

    # -- ids ----------------------------------------------------------------------
    id_of: dict[str, str] = {}
    by_id: dict[str, list[dict]] = {}
    for usr, d in sorted(records.items(), key=lambda kv: (kv[1]["file"], kv[1]["line"], kv[0])):
        if d["nk"] not in _KINDS:
            continue
        if d["nk"] == "function" and d.get("c_symbol") and not d["has_def"]:
            continue                        # declared here, defined in C: reached by ABI name
        nid = f"{_prefix(d['nk'])}:{_qualname(d)}"
        id_of[usr] = nid
        by_id.setdefault(nid, []).append(d)

    overload_sets: dict[str, list[str]] = {}
    for nid, recs in by_id.items():
        d = recs[0]
        if d["nk"] in ("function", "method"):
            overload_sets.setdefault(f"{_prefix(d['nk'])}:{_qual(d)}", []).append(nid)

    # -- nodes ----------------------------------------------------------------------
    template_usrs: dict[str, str] = {}
    for nid, recs in sorted(by_id.items()):
        d = recs[0]
        kind = _KINDS[d["nk"]]
        parent = id_of.get(d.get("parent") or "") or f"mod:{module_name(d['file'])}"
        meta: dict = {"lang": LANG, "usr": d["usr"]}
        tags = list(d.get("tags", []))
        if d.get("internal"):
            tags.append("internal")
        if kind.is_callable:
            arity = _arity(d)
            meta["arity"] = arity
            meta["overload_key"] = f"{_prefix(d['nk'])}:{_qual(d)}"
            siblings = overload_sets.get(meta["overload_key"], [])
            if len(siblings) > 1:
                meta["overloads"] = len(siblings)
            if d.get("c_symbol"):
                meta["abi_exports"] = [d["c_symbol"]]
                tags.append("extern_c")
            names = {p["name"] for p in d.get("params", []) if p["name"]}
            names |= set(d.get("locals", ()))
            if names:
                # names this function declares itself: a removed global it now shadows
                # with a local is not "still referenced" (the contract comes with
                # language/c's surviving_references change; nothing reads it before)
                meta["local_names"] = sorted(names)
            if d.get("switches"):
                sw = [dict(s, enum=id_of[s["enum"]]) for s in d["switches"]
                      if s["enum"] in id_of]
                if sw:
                    meta["switches"] = sw
            sig_basis = {k: v for k, v in arity.items() if k not in ("positional", "returns")}
            sig_hash = _h(json.dumps(sig_basis, sort_keys=True))
            body_hash = _h(_params_sig(d), d.get("body", ""))
            signature = _signature(d)
        elif kind is NodeKind.CLASS:
            meta["bases"] = [b["name"] for b in d.get("bases", [])]
            if d.get("fields") is not None:
                meta["layout"] = [f"{t} {n}" for n, t in d.get("fields", [])]
            if d.get("enum_members") is not None:
                meta["enum_members"] = d["enum_members"]
                meta["scoped"] = bool(d.get("scoped"))
            if d.get("template_params"):
                meta["template_params"] = d["template_params"]
            sig_hash = _h(json.dumps(d.get("sig", []), default=str))
            body_hash = d.get("body", "") or ""
            signature = None
            if "template" in tags:
                template_usrs[d["usr"]] = nid
        else:
            meta["annotation"] = d.get("type", "")
            meta["value"] = d.get("value", "")
            sig_hash = _h(json.dumps(d.get("sig", []), default=str))
            body_hash = d.get("body", "") or ""
            signature = None
        public = d.get("access") not in ("private", "protected") and not d.get("internal")
        node = Node(
            id=nid, kind=kind, name=d["name"], qualname=nid.split(":", 1)[1],
            module=module_name(d["file"]), path=d["file"], lineno=d["line"],
            end_lineno=max(d.get("end", d["line"]), d["line"]), col=d.get("col", 0),
            parent=parent, signature=signature,
            bases=[b["name"] for b in d.get("bases", [])], public=public,
            tags=sorted(set(tags)), sig_hash=sig_hash, body_hash=body_hash,
            doc_hash=_h(" ".join((d.get("doc") or "").split())) if d.get("doc") else "",
            meta=meta)
        for extra in recs[1:]:              # same id, different entity (SFINAE twins)
            node.body_hash = _h(node.body_hash, extra.get("body", ""))
            node.meta["redefined"] = node.meta.get("redefined", 0) + 1
        if graph.add_node(node) is node:
            added.add(nid)
        if parent in graph.nodes:
            graph.add_edge(Edge(src=parent, dst=nid, kind=EdgeKind.CONTAINS,
                                lineno=node.lineno, path=node.path))

    def resolve(usrs: list[str]) -> str | None:
        for u in usrs:
            nid = id_of.get(u)
            if nid is not None:
                return nid
        return None

    # -- structure: inheritance, overrides, includes ------------------------------------
    for usr, d in records.items():
        nid = id_of.get(usr)
        if nid is None or nid not in added:
            continue
        for b in d.get("bases", []) or []:
            dst = resolve(b.get("usrs", []))
            if dst is not None:
                graph.add_edge(Edge(src=nid, dst=dst, kind=EdgeKind.INHERITS, lineno=d["line"],
                                    path=d["file"], meta={"virtual": b["virtual"],
                                                          "access": b["access"]}))
        for o in d.get("overridden", []) or []:
            dst = id_of.get(o)
            if dst is not None:
                graph.add_edge(Edge(src=nid, dst=dst, kind=EdgeKind.OVERRIDES,
                                    lineno=d["line"], path=d["file"]))
    for a, b, line in data.get("includes", []):
        ma, mb = f"mod:{module_name(a)}", f"mod:{module_name(b)}"
        if ma in graph.nodes and mb in graph.nodes:
            graph.add_edge(Edge(src=ma, dst=mb, kind=EdgeKind.IMPORTS, lineno=line, path=a))

    # -- references ------------------------------------------------------------------------
    def external(ext: dict) -> str:
        if ext.get("abi"):
            eid = f"ext:abi:{ext['abi']}"
            if eid not in graph.nodes:
                graph.add_node(Node(id=eid, kind=NodeKind.EXTERNAL, name=ext["abi"],
                                    qualname=f"abi:{ext['abi']}", module="",
                                    meta={"abi_import": ext["abi"], "lang": LANG}))
                added.add(eid)
            return eid
        name = ext["name"]
        eid = f"ext:{LANG}@{name}"
        if eid not in graph.nodes:
            graph.add_node(Node(id=eid, kind=NodeKind.EXTERNAL, name=name.rsplit(".", 1)[-1],
                                qualname=f"{LANG}@{name}", module="",
                                meta={"lang": LANG, "distribution": name.split(".")[0]}))
            added.add(eid)
        return eid

    c_decls = {usr: d for usr, d in records.items()
               if d.get("c_symbol") and not d.get("has_def")}
    instantiations: dict[str, set[str]] = {}
    for r in sorted(data["refs"], key=lambda r: (r["file"], r["line"], r["col"], r["kind"])):
        kind = _EDGES.get(r["kind"])
        if kind is None:
            continue
        s = r["src"]
        src = f"mod:{module_name(s[5:])}" if s.startswith("file:") else id_of.get(s)
        if src is None or src not in graph.nodes:
            continue
        dst = resolve(r["dst"])
        if dst is None:
            c_decl = next((c_decls[u] for u in r["dst"] if u in c_decls), None)
            if c_decl is not None:
                dst = external({"abi": c_decl["c_symbol"]})
            elif r.get("ext"):
                dst = external(r["ext"])
            else:
                continue
        meta = dict(r.get("meta") or {})
        if meta.get("instantiation") and dst in graph.nodes \
                and "template" in graph.nodes[dst].tags:
            instantiations.setdefault(dst, set()).add(meta["instantiation"])
        graph.add_edge(Edge(src=src, dst=dst, kind=kind, lineno=r["line"], path=r["file"],
                            confidence=float(r.get("conf", 1.0)), conditional=bool(r["cond"]),
                            dynamic=bool(r.get("dyn")), context=graph.nodes[src].qualname,
                            meta=meta, col=r["col"]))
    for nid, insts in instantiations.items():
        graph.nodes[nid].meta["instantiations"] = sorted(insts)[:50]

    _virtual_dispatch(graph, added)
    if state is not None:
        _signals(state, graph, added)
    errs = data.get("tu_errors", 0)
    graph.diagnostics.append(
        f"cpp (libclang {data.get('version', '?').split(' version ')[-1].split(' ')[0]}): "
        f"{len(owned)} files, {data.get('tus', 0)} translation units "
        f"(flags from {data.get('flags_from', 'defaults')}), {len(by_id)} declarations"
        + (f"; {errs} unit(s) had compile errors, e.g. {data['errors'][0]}"
           if errs and data.get("errors") else ""))
    return added


def _virtual_dispatch(graph: Graph, added: set[str]) -> None:
    """A call through a base pointer may run any override: link each at lower confidence."""
    overriders: dict[str, list[str]] = {}
    for e in graph.edges:
        if e.kind is EdgeKind.OVERRIDES and e.src in added:
            overriders.setdefault(e.dst, []).append(e.src)

    def all_overriders(mid: str) -> list[str]:
        out, stack, seen = [], [mid], {mid}
        while stack:
            for o in overriders.get(stack.pop(), ()):
                if o not in seen:
                    seen.add(o)
                    out.append(o)
                    stack.append(o)
        return out

    for e in list(graph.edges):
        if e.kind is not EdgeKind.CALLS or not e.meta.get("virtual") or e.src not in added:
            continue
        for o in all_overriders(e.dst):
            graph.add_edge(Edge(src=e.src, dst=o, kind=EdgeKind.CALLS, lineno=e.lineno,
                                path=e.path, confidence=min(e.confidence, VIRTUAL_CONFIDENCE),
                                conditional=e.conditional, dynamic=True, context=e.context,
                                meta=dict(e.meta, via=e.dst), col=e.col))


def _signals(state, graph: Graph, added: set[str]) -> None:
    out_edges: dict[str, list[Edge]] = {}
    for e in graph.edges:
        if e.src in added:
            out_edges.setdefault(e.src, []).append(e)
    state_kinds = (NodeKind.GLOBAL, NodeKind.CLASS_ATTR, NodeKind.INSTANCE_ATTR)
    for nid in added:
        node = graph.nodes[nid]
        if node.kind is NodeKind.EXTERNAL:
            continue
        module_parts = tuple(node.module.split("@", 1)[-1].split(".")) if node.module else ()
        path_parts = tuple(node.path.split("/")) if node.path else ()
        sig = state.sig(nid)
        sig.module_parts, sig.path_parts = module_parts, path_parts
        edges = out_edges.get(nid, [])
        if node.kind is NodeKind.MODULE:
            continue
        if node.kind.is_callable:
            arity = node.meta.get("arity", {})
            sig.has_params = bool(arity.get("positional"))
            sig.returns_value = arity.get("returns", "void") not in ("", "void")
            sig.call_count = sum(1 for e in edges if e.kind is EdgeKind.CALLS)
            sig.own_statement_count = max(1, (node.end_lineno - node.lineno))
            for e in edges:
                tgt = graph.nodes.get(e.dst)
                if tgt is None:
                    continue
                if tgt.kind is NodeKind.EXTERNAL:
                    q = tgt.qualname.split("@", 1)[-1]
                    if q.startswith(_IO) or q in ("fopen", "open", "std.fopen"):
                        sig.calls_open = True
                    if q in _EXIT:
                        sig.calls_exit = True
                    continue
                if tgt.kind not in state_kinds:
                    continue
                if e.kind in (EdgeKind.WRITES, EdgeKind.MUTATES):
                    sig.mutates_state = True
                    if tgt.kind is NodeKind.GLOBAL:
                        sig.writes_global = True
                elif e.kind is EdgeKind.READS and tgt.kind is NodeKind.GLOBAL:
                    sig.reads_global = True
        elif node.kind in state_kinds:
            ann = str(node.meta.get("annotation", ""))
            container = bool(re.search(r"\b(vector|map|unordered_map|set|unordered_set|deque|"
                                       r"list|queue|stack|multimap|multiset)\s*<", ann))
            sig.is_mutable_container = container and "const" not in node.tags
            sig.is_constant = "const" in node.tags
