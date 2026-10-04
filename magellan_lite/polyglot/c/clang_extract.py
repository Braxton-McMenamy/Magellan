"""Extract C declarations and references with libclang (the precise backend).

Every ``.c`` file is parsed as a translation unit with its flags from
``compile_commands.json`` when there is one, else with ``-I`` for the project root
and every directory holding a header. Headers no ``.c`` file includes are parsed
on their own (header-only libraries). Each project file is walked once, in the
first translation unit that reaches it.

libclang resolves every name, so calls, variable accesses, field accesses and
type uses are linked to their declarations by USR, not by spelling. What it cannot
see is code in inactive ``#if`` branches: only the configuration being parsed
exists. :func:`magellan_lite.polyglot.c.extract.conditional_lines` still marks what sits inside
an ``#if`` so edges from it are ``conditional``.

What a translation unit contributes is remembered for the rest of the process
(:class:`_TUCache`). A check builds the tree twice, before and after a change, and
nearly every translation unit is the same in both: it is reused when its own text,
every project file it reached, its flags and the project's file list are unchanged.
``MAGELLAN_C_TU_CACHE=0`` turns that off.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shlex

from magellan_lite.polyglot.c import clang as C
from magellan_lite.polyglot.c.extract import (Decl, Extraction, HEADER_SUFFIXES, Ref, SOURCE_SUFFIXES, Switch,
                                conditional_lines, guard_macro, macro_call_args, normalize_type,
                                read_text, source_files)

_PASS_THROUGH = {100, 111}          # unexposed (implicit casts), parens
_COND_ALL = {208, 209, 116}         # do, for, ?: -- every child may not run
_COND_AFTER_FIRST = {205, 206, 207}  # if, switch, while -- the condition always runs


# --------------------------------------------------------------------------
# compile flags
# --------------------------------------------------------------------------
def compile_db(root: str) -> dict[str, list[str]]:
    """``{absolute source path: [flags]}`` from compile_commands.json, if present."""
    for cand in ("compile_commands.json", "build/compile_commands.json"):
        p = os.path.join(root, cand)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                entries = json.load(fh)
        except (OSError, ValueError):
            return {}
        out: dict[str, list[str]] = {}
        for e in entries if isinstance(entries, list) else []:
            try:
                d = e.get("directory", root)
                argv = msvc_flags(e.get("arguments") or split_command(e.get("command", "")))
                src = os.path.normcase(os.path.normpath(os.path.join(d, e["file"])))
            except (KeyError, TypeError, ValueError):
                continue
            out[src] = _filter_flags(argv[1:], d)
        return out
    return {}


def split_command(command: str) -> list[str]:
    """A compile command as its shell would split it (``-DNAME="a b"`` is one argument).

    On Windows that is the Windows rule (as LLVM reads compile_commands.json there), under
    which ``-IC:\\proj\\include`` keeps its backslashes.
    """
    if os.name == "nt":
        return split_windows_command(command)
    try:
        return shlex.split(command)
    except ValueError:                      # unbalanced quotes: take the words as they are
        return command.split()


def split_windows_command(command: str) -> list[str]:
    """Split as CommandLineToArgvW does: a backslash is literal unless a quote follows it."""
    args: list[str] = []
    cur: list[str] = []
    quoted = started = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\":
            j = i
            while j < n and command[j] == "\\":
                j += 1
            run = j - i
            if j < n and command[j] == '"':
                cur.append("\\" * (run // 2))      # 2n backslashes + quote: n, and the quote
                if run % 2:                        # toggles; 2n+1: n and a literal quote
                    cur.append('"')
                    j += 1
            else:
                cur.append("\\" * run)
            i, started = j, True
            continue
        if ch == '"':
            quoted, started = not quoted, True
        elif ch in " \t" and not quoted:
            if started:
                args.append("".join(cur))
                cur, started = [], False
        else:
            cur.append(ch)
            started = True
        i += 1
    if started:
        args.append("".join(cur))
    return args


def msvc_flags(argv: list[str]) -> list[str]:
    """A cl.exe / clang-cl command's /I /D /U /std: flags, spelled the way clang reads them."""
    tool = os.path.basename(argv[0]).lower() if argv else ""
    if tool.removesuffix(".exe") not in ("cl", "clang-cl"):
        return argv                         # elsewhere /U... is a path (/Users/me), not a flag
    out = argv[:1]
    for a in argv[1:]:
        if a[:2] in ("/I", "/D", "/U"):
            a = "-" + a[1:]
        elif a.startswith("/std:"):
            a = "-std=" + ("c++2b" if a[5:] == "c++latest" else a[5:])
        out.append(a)
    return out


def _abs(p: str, cwd: str) -> str:
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(cwd, p))


def _filter_flags(argv: list[str], cwd: str) -> list[str]:
    """Only what changes how the code parses: include paths, macros, the standard."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if a.startswith("-std="):
            out.append(a)
        elif a in ("-I", "-isystem", "-iquote", "-idirafter", "-include") and nxt is not None:
            out += [a, _abs(nxt, cwd)]
            i += 1
        elif a in ("-D", "-U") and nxt is not None:
            out += [a, nxt]
            i += 1
        elif a.startswith(("-D", "-U")):
            out.append(a)
        elif a.startswith("-I"):
            out.append("-I" + _abs(a[2:], cwd))
        elif a.startswith("-isystem"):
            out.append("-isystem" + _abs(a[8:], cwd))
        elif a.startswith("-iquote"):
            out.append("-iquote" + _abs(a[7:], cwd))
        i += 1
    return out


def _system_dirs(args: list[str]) -> list[str]:
    """Directories given to ``-isystem`` / ``-idirafter`` (headers there are "system")."""
    out = []
    for i, a in enumerate(args):
        for opt in ("-isystem", "-idirafter"):
            if a == opt and i + 1 < len(args):
                out.append(args[i + 1])
            elif a.startswith(opt) and len(a) > len(opt):
                out.append(a[len(opt):])
    return out


def compile_flags(root: str) -> list[str]:
    """Flags from ``compile_flags.txt`` (one per line, clangd's format), for every file."""
    p = os.path.join(root, "compile_flags.txt")
    try:
        with open(p, encoding="utf-8") as fh:
            argv = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    except OSError:
        return []
    return _filter_flags(argv, root)


#: clang 16 made these C89-era constructs errors; they are warnings for the code they
#: occur in (and -w hides warnings)
LEGACY_FLAGS = ("-Wno-error=implicit-function-declaration", "-Wno-error=implicit-int",
                "-Wno-error=int-conversion", "-Wno-error=incompatible-function-pointer-types")


def default_flags(root: str, files: list[str]) -> list[str]:
    dirs = sorted({os.path.dirname(f) for f in files if f.endswith(HEADER_SUFFIXES)})
    flags = ["-I" + root]
    for d in dirs[:200]:
        flags.append("-I" + os.path.join(root, d))
    return flags


# --------------------------------------------------------------------------
class _LogDict(dict):
    """A dict that also lists its writes while ``log`` is a list (see :class:`_TUCache`)."""

    log: list | None = None

    def __setitem__(self, k, v) -> None:
        if self.log is not None:
            self.log.append((k, v))
        super().__setitem__(k, v)


@dataclasses.dataclass
class _TUResult:
    """What one translation unit added to the extraction (references pre-``finish``)."""
    static: tuple                     # flags (root-relative), project file list, libclang
    reached: dict[str, str]           # project file -> sha1 of its bytes
    done_before: frozenset[str]       # reached files an earlier unit had already walked
    walked: frozenset[str]
    decls: list
    refs: list
    switches: list
    includes: list
    diagnostics: list
    expansions: list
    alias: list
    address_taken: list
    anon: dict
    local_names: dict


def _copy_ref(r: Ref) -> Ref:
    return dataclasses.replace(r, meta=dict(r.meta))


class _TUCache:
    """Per-process memory of translation units, keyed by their project-relative path.

    Reuse is exact: the unit's result depends only on the text of the files it
    reached (hashed), its flags, which files exist (include resolution), libclang, and
    which reached files earlier units had already walked (each file is walked once).
    All of those are compared; anything different parses again.
    """
    MAX = 4096

    def __init__(self) -> None:
        self.units: dict[str, _TUResult] = {}

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("MAGELLAN_C_TU_CACHE", "1") != "0"

    def put(self, rel: str, res: _TUResult) -> None:
        if len(self.units) >= self.MAX:
            self.units.clear()
        self.units[rel] = res

    def clear(self) -> None:
        self.units.clear()


TU_CACHE = _TUCache()


class _Extractor:
    def __init__(self, lc: C.LibClang, root: str, ex: Extraction,
                 allowed: set[str] | None = None) -> None:
        self.lc = lc
        self.root = os.path.realpath(root)
        self.ex = ex
        self.allowed = allowed                # project files this frontend owns
        self.texts: dict[str, bytes] = {}
        self.cond: dict[str, list[bool]] = {}
        self.guards: dict[str, str] = {}
        self.done: set[str] = set()
        self.alias: _LogDict = _LogDict()    # libclang USR -> our key
        ex.address_taken = _LogDict(ex.address_taken)
        self.rel_cache: dict[str, str | None] = {}
        self.annot_seen: set[tuple[str, str]] = set()
        self.expansions: list[tuple[str, int, int, int, str, str]] = []
        self.anon_seen: dict[str, int] = {}
        self.tu = ""
        self.reached: set[str] | None = None   # project files the current unit reached
        self.hashes: dict[str, str] = {}
        self.files_key = hashlib.sha1("\n".join(sorted(allowed or ())).encode()).hexdigest()

    # -- files -----------------------------------------------------------
    def rel(self, path: str) -> str | None:
        if path in self.rel_cache:
            out = self.rel_cache[path]
            if out is not None and self.reached is not None:
                self.reached.add(out)
            return out
        out = None
        if path:
            full = os.path.realpath(path)
            if full.startswith(self.root + os.sep) and full.endswith(SOURCE_SUFFIXES):
                out = os.path.relpath(full, self.root).replace(os.sep, "/")
                if self.allowed is not None and out not in self.allowed:
                    out = None                # a C++ header, or a skipped directory
            if out is not None:
                if out not in self.texts:
                    try:
                        with open(full, "rb") as fh:
                            self.texts[out] = fh.read()
                    except OSError:
                        self.texts[out] = b""
                    text = self.texts[out].decode("utf-8", "replace")
                    self.ex.files[out] = text
                    self.cond[out] = conditional_lines(text)
                    self.guards[out] = guard_macro(text)
                if self.reached is not None:
                    self.reached.add(out)
        self.rel_cache[path] = out
        return out

    def digest(self, rel: str) -> str | None:
        """sha1 of a project file's bytes (loading it like :meth:`rel`), or None."""
        if rel not in self.hashes:
            if self.rel(os.path.join(self.root, rel)) != rel:
                return None
            self.hashes[rel] = hashlib.sha1(self.texts.get(rel, b"")).hexdigest()
        return self.hashes[rel]

    def slice(self, rel: str, a: int, b: int) -> str:
        return self.texts.get(rel, b"")[a:b].decode("utf-8", "replace")

    def literal(self, c, name: str) -> bool:
        """Is ``name`` written at this cursor in the function's own text?

        False when the code came out of a macro body: that reference belongs to the
        macro (the frontend links macro -> callee from the #define), not to every
        function that expands it. Macro *arguments* are the caller's own text.
        """
        f, _line, _col, off = self.lc.spelled_at(c)
        rel = self.rel(f)
        if rel is None:
            return False
        raw = self.texts.get(rel, b"")
        if raw[off:off + len(name)] != name.encode():
            return False
        start = raw.rfind(b"\n", 0, off) + 1
        while True:
            end = raw.find(b"\n", start)
            if raw[start:end if end >= 0 else len(raw)].lstrip().startswith(b"#"):
                return False                                  # inside a #define
            if start <= 1:
                return True
            prev = raw.rfind(b"\n", 0, start - 1) + 1
            if not raw[prev:start - 1].rstrip().endswith(b"\\"):
                return True
            start = prev

    def anon(self, rel: str) -> int:
        """Ordinal of the next unnamed file-scope record in ``rel``.

        Stable when lines move above it; a line number in the id was not, so every edit
        above ``static struct { ... } table[]`` removed one node and added another.
        """
        self.anon_seen[rel] = self.anon_seen.get(rel, 0) + 1
        return self.anon_seen[rel]

    def is_cond(self, rel: str, line: int) -> bool:
        c = self.cond.get(rel) or []
        return 0 < line <= len(c) and c[line - 1]

    def where(self, c) -> tuple[str | None, int, int, int]:
        f, line, col, off = self.lc.location(c)
        return self.rel(f), line, col, off

    def span(self, c) -> tuple[int, int, int, int]:
        (_f, l1, _c1, o1), (_f2, l2, _c2, o2) = self.lc.extent(c)
        return l1, l2, o1, o2

    # -- keys ------------------------------------------------------------
    def key_of(self, c) -> str:
        """Our key for a declaration cursor (alias of its USR where we renamed it)."""
        usr = self.lc.usr(c)
        return self.alias.get(usr, usr)

    def fn_key(self, c, rel: str, name: str) -> str:
        usr = self.lc.usr(c)
        if self.lc.lib.clang_getCursorLinkage(c) == C.LINKAGE_INTERNAL:
            key = f"c:{rel}@F@{name}"
            self.alias[usr] = key
            return key
        return usr

    # -- translation units ----------------------------------------------
    def static_key(self, flags: list[str]) -> tuple:
        root = self.root
        return (tuple(f.replace(root, "<root>") for f in flags),
                self.files_key, self.lc.path, self.lc.version)

    def reuse(self, rel: str, static: tuple) -> bool:
        """Replay a remembered unit if nothing it depends on changed."""
        res = TU_CACHE.units.get(rel)
        if res is None or res.static != static:
            return False
        if any(self.digest(f) != h for f, h in res.reached.items()):
            return False
        if frozenset(f for f in res.reached if f in self.done) != res.done_before:
            return False
        ex = self.ex
        self.tu = rel
        ex.decls.extend(res.decls)                          # never mutated after creation
        ex.refs.extend(_copy_ref(r) for r in res.refs)      # finish() rewrites src/dst
        ex.switches.extend(dataclasses.replace(s) for s in res.switches)
        ex.includes.extend(res.includes)
        ex.diagnostics.extend(res.diagnostics)
        self.expansions.extend(res.expansions)
        for k, v in res.alias:
            self.alias[k] = v
        for k, v in res.address_taken:
            ex.address_taken[k] = v
        self.anon_seen.update(res.anon)
        for k, v in res.local_names.items():
            ex.local_names[k] = set(v)
        self.done |= res.walked
        return True

    def run_tu(self, index: int, path: str, flags: list[str]) -> bool:
        rel_tu = self.rel(path) or path
        if not TU_CACHE.enabled():
            return self._run_tu(index, path, flags)
        static = self.static_key(flags)
        if self.reuse(rel_tu, static):
            return True
        ex = self.ex
        marks = (len(ex.decls), len(ex.refs), len(ex.switches), len(ex.includes),
                 len(ex.diagnostics), len(self.expansions))
        done_before = set(self.done)
        self.reached, self.alias.log, ex.address_taken.log = set(), [], []
        try:
            ok = self._run_tu(index, path, flags)
            reached = set(self.reached)
            self.reached = None
            if ok and all(self.digest(f) is not None for f in reached):
                walked = frozenset(self.done - done_before)
                TU_CACHE.put(rel_tu, _TUResult(
                    static=static, reached={f: self.digest(f) for f in reached},
                    done_before=frozenset(reached & done_before), walked=walked,
                    decls=ex.decls[marks[0]:],
                    refs=[_copy_ref(r) for r in ex.refs[marks[1]:]],
                    switches=[dataclasses.replace(s) for s in ex.switches[marks[2]:]],
                    includes=ex.includes[marks[3]:], diagnostics=ex.diagnostics[marks[4]:],
                    expansions=self.expansions[marks[5]:], alias=list(self.alias.log),
                    address_taken=list(ex.address_taken.log),
                    anon={f: n for f, n in self.anon_seen.items() if f in walked},
                    local_names={k: set(v) for k, v in ex.local_names.items()
                                 if k[1] in walked}))
            return ok
        finally:
            self.reached = None
            self.alias.log = ex.address_taken.log = None

    def _run_tu(self, index: int, path: str, flags: list[str]) -> bool:
        # Pre-C99 code is legal C89 that clang 16+ rejects by default (zlib 1.2.11 calls
        # lseek/read/write with no declaration in scope); read old code the way the
        # compilers it was written for did
        args = ["-xc", "-std=gnu11", "-w", "-ferror-limit=0", *LEGACY_FLAGS] + flags
        if any(f.startswith("-std=") for f in flags):
            args.remove("-std=gnu11")
        tu = self.lc.parse(index, path, args, C.OPT_DETAILED_PREPROCESSING | C.OPT_KEEP_GOING)
        if tu is None:
            self.ex.diagnostics.append(f"libclang could not parse {self.rel(path) or path}")
            return False
        try:
            self.tu = self.rel(path) or path
            # one ANNOTATES per pair and unit: two programs' `main`s share a key, and the
            # second one's edges were dropped as duplicates of the first's
            self.annot_seen = set()
            if tu.errors():
                why = "; ".join(dict.fromkeys(tu.error_messages(3)))
                self.ex.diagnostics.append(f"{self.tu}: parsed with errors (missing includes or "
                                           f"flags?); results for it may be partial"
                                           + (f": {why}" if why else ""))
            seen: set[str] = set()
            # Most top-level cursors are libc's declarations and macros; skipping them
            # without asking for their file name halves the walk. Not when a system
            # include path points into the project (then its headers are ours).
            skip_system = not any(os.path.realpath(d) == self.root
                                  or os.path.realpath(d).startswith(self.root + os.sep)
                                  for d in _system_dirs(args))
            for c in self.lc.children(tu.cursor):
                if skip_system and self.lc.in_system_header(c):
                    continue
                rel, line, col, off = self.where(c)
                if rel is None or rel in self.done:
                    continue
                seen.add(rel)
                self.top(c, rel, line, col, off)
            self.done |= seen
        finally:
            tu.dispose()
        return True

    def top(self, c, rel: str, line: int, col: int, off: int) -> None:
        k = c.kind
        if k == C.FUNCTION_DECL:
            self.function(c, rel)
        elif k == C.VAR_DECL:
            self.global_var(c, rel)
        elif k in (C.STRUCT_DECL, C.UNION_DECL):
            self.record(c, rel)
        elif k == C.ENUM_DECL:
            self.enum(c, rel)
        elif k == C.TYPEDEF_DECL:
            self.typedef(c, rel)
        elif k == C.MACRO_DEFINITION:
            self.macro(c, rel)
        elif k == C.MACRO_EXPANSION:
            ref = self.lc.referenced(c)
            if not self.lc.is_null(ref):
                mrel, _l, _c, _o = self.where(ref)
                if mrel is not None:
                    name = self.lc.spelling(c)
                    self.expansions.append((rel, line, col, off, f"macro:{mrel}@{name}", name))
        elif k == C.INCLUSION_DIRECTIVE:
            target = self.rel(self.lc.included_file(c))
            if target:
                self.ex.includes.append((rel, target, line))

    # -- declarations ----------------------------------------------------
    def function(self, c, rel: str) -> None:
        lc = self.lc
        name = lc.spelling(c)
        key = self.fn_key(c, rel, name)
        t = lc.type_of(c)
        l1, l2, o1, o2 = self.span(c)
        params, ptypes = [], []
        n = lc.lib.clang_Cursor_getNumArguments(c)
        for i in range(max(n, 0)):
            a = lc.lib.clang_Cursor_getArgument(c, i)
            at = lc.type_of(a)
            params.append((lc.spelling(a), lc.type_spelling(at)))
            ptypes.append(normalize_type(lc.type_spelling(lc.canonical(at))))
        is_def = bool(lc.lib.clang_isCursorDefinition(c))
        d = Decl(key=key, kind="function", name=name, path=rel, line=l1, end_line=l2,
                 start=o1, end=o2, static=lc.lib.clang_getCursorLinkage(c) == C.LINKAGE_INTERNAL,
                 is_def=is_def, params=params, ptypes=ptypes,
                 ret=lc.type_spelling(lc.lib.clang_getResultType(t)),
                 # libclang calls a K&R `int f()` variadic; it is unprototyped, not `...`
                 variadic=t.kind == C.TYPE_FUNCTION_PROTO
                 and bool(lc.lib.clang_isFunctionTypeVariadic(t)),
                 noproto=t.kind == C.TYPE_FUNCTION_NOPROTO,
                 conditional=self.is_cond(rel, l1), tu=self.tu)
        d.type = normalize_type(lc.type_spelling(lc.canonical(t)))
        self.ex.decls.append(d)
        for child in lc.children(c):
            self.visit(child, key, rel, False, "")

    def global_var(self, c, rel: str) -> None:
        lc = self.lc
        name = lc.spelling(c)
        usr = lc.usr(c)
        static = lc.lib.clang_getCursorLinkage(c) == C.LINKAGE_INTERNAL
        key = usr
        if static:
            key = f"c:{rel}@{name}"
            self.alias[usr] = key
        t = lc.type_of(c)
        canon = lc.type_spelling(lc.canonical(t))
        l1, l2, o1, o2 = self.span(c)
        text = self.slice(rel, o1, o2)
        value = text.split("=", 1)[1].strip() if "=" in text else ""
        d = Decl(key=key, kind="global", name=name, path=rel, line=l1, end_line=l2, start=o1,
                 end=o2, static=static,
                 is_def=lc.lib.clang_Cursor_getStorageClass(c) != C.SC_EXTERN or bool(value),
                 type=lc.type_spelling(t), value=value, const=_is_const(canon),
                 conditional=self.is_cond(rel, l1), fnptr=self.fnptr(t), tu=self.tu)
        self.ex.decls.append(d)
        for child in lc.children(c):
            self.visit(child, key, rel, False, "")

    def fnptr(self, t) -> str:
        lc = self.lc
        can = lc.canonical(t)
        if can.kind == C.TYPE_POINTER:
            p = lc.canonical(lc.lib.clang_getPointeeType(can))
            if p.kind in (C.TYPE_FUNCTION_PROTO, C.TYPE_FUNCTION_NOPROTO):
                return normalize_type(lc.type_spelling(p))
        return ""

    def record(self, c, rel: str, name: str | None = None, key: str | None = None,
               parent: str = "") -> str | None:
        lc = self.lc
        if not lc.lib.clang_isCursorDefinition(c):
            return None
        kind = "struct" if c.kind == C.STRUCT_DECL else "union"
        spelled = lc.spelling(c)
        anon = bool(lc.lib.clang_Cursor_isAnonymous(c)) or not spelled or "(" in spelled
        l1, l2, o1, o2 = self.span(c)
        usr = lc.usr(c)
        if key is None:
            if anon:
                if name is None:
                    name = f"{parent.rsplit('@', 1)[-1] if parent else kind}_anon{self.anon(rel)}"
                key = f"c:{rel}@{'SA' if kind == 'struct' else 'UA'}@{name}"
            else:
                name, key = spelled.split(" ")[-1], usr
        self.alias[usr] = key
        t = lc.type_of(c)
        d = Decl(key=key, kind=kind, name=name or spelled, path=rel, line=l1, end_line=l2,
                 start=o1, end=o2, is_def=True, size=lc.size_of(t), anonymous=anon,
                 conditional=self.is_cond(rel, l1), tu=self.tu)
        self.ex.decls.append(d)
        layout = []
        pending_anon = None
        for child in lc.children(c):
            if child.kind in (C.STRUCT_DECL, C.UNION_DECL):
                pending_anon = child
                continue
            if child.kind == C.ENUM_DECL:
                self.enum(child, rel)
                continue
            if child.kind != C.FIELD_DECL:
                continue
            fname = lc.spelling(child)
            ft = lc.type_of(child)
            if pending_anon is not None:
                inner = self.record(pending_anon, rel, name=f"{d.name}_{fname or 'anon'}",
                                    parent=key)
                pending_anon = None
                if inner:
                    self.annotate(key, inner, rel, l1)
            fl1, fl2, fo1, fo2 = self.span(child)
            canon = normalize_type(lc.type_spelling(lc.canonical(ft)))
            fkey = f"{key}@FI@{fname}"
            self.alias[lc.usr(child)] = fkey
            off = lc.lib.clang_Cursor_getOffsetOfField(child)
            self.ex.decls.append(Decl(
                key=fkey, kind="field", name=fname, path=rel, line=fl1, end_line=fl2,
                start=fo1, end=fo2, type=lc.type_spelling(ft), parent=key, offset=int(off),
                value=canon, conditional=self.is_cond(rel, fl1), fnptr=self.fnptr(ft),
                tu=self.tu))
            layout.append(f"{fname}:{canon}")
            for sub in lc.children(child):
                if sub.kind == C.TYPE_REF:
                    ref = lc.referenced(sub)
                    if not lc.is_null(ref):
                        self.annotate(key, self.key_of(ref), rel, fl1, raw=True)
        if pending_anon is not None:            # anonymous member struct/union (C11)
            inner = self.record(pending_anon, rel, name=f"{d.name}_anon", parent=key)
            if inner:
                self.annotate(key, inner, rel, l1)
                layout.append(f"<anon {inner}>")
        d.value = "; ".join(layout)
        return key

    def enum(self, c, rel: str, name: str | None = None) -> str | None:
        lc = self.lc
        if not lc.lib.clang_isCursorDefinition(c):
            return None
        spelled = lc.spelling(c)
        anon = bool(lc.lib.clang_Cursor_isAnonymous(c)) or not spelled or "(" in spelled
        l1, l2, o1, o2 = self.span(c)
        usr = lc.usr(c)
        members = [ch for ch in lc.children(c) if ch.kind == C.ENUM_CONSTANT_DECL]
        if anon:
            if name is None:
                first = lc.spelling(members[0]) if members else f"L{l1}"
                name = f"enum_{first}"
            key = f"c:{rel}@EA@{name}"
        else:
            name, key = spelled.split(" ")[-1], usr
        self.alias[usr] = key
        d = Decl(key=key, kind="enum", name=name, path=rel, line=l1, end_line=l2, start=o1,
                 end=o2, is_def=True, anonymous=anon, conditional=self.is_cond(rel, l1),
                 tu=self.tu)
        self.ex.decls.append(d)
        vals = []
        for m in members:
            mname = lc.spelling(m)
            ml1, ml2, mo1, mo2 = self.span(m)
            mkey = f"{key}@{mname}"
            self.alias[lc.usr(m)] = mkey
            v = int(lc.lib.clang_getEnumConstantDeclValue(m))
            self.ex.decls.append(Decl(
                key=mkey, kind="enumerator", name=mname, path=rel, line=ml1, end_line=ml2,
                start=mo1, end=mo2, parent=key, value=str(v),
                conditional=self.is_cond(rel, ml1), tu=self.tu))
            vals.append(f"{mname}={v}")
            for sub in lc.children(m):
                self.visit(sub, mkey, rel, False, "")
        d.value = ", ".join(vals)
        return key

    def typedef(self, c, rel: str) -> None:
        lc = self.lc
        name = lc.spelling(c)
        kids = lc.children(c)
        for k in kids:
            if k.kind in (C.STRUCT_DECL, C.UNION_DECL, C.ENUM_DECL):
                spelled = lc.spelling(k)
                anon = bool(lc.lib.clang_Cursor_isAnonymous(k)) or not spelled or "(" in spelled
                if anon:
                    # `typedef struct { ... } name;` -- the struct *is* the typedef
                    self.alias[lc.usr(c)] = f"c:{rel}@T@{name}"
                    got = (self.enum(k, rel, name=name) if k.kind == C.ENUM_DECL
                           else self.record(k, rel, name=name))
                    if got:
                        self.alias[lc.usr(c)] = got
                    return
                if k.kind == C.ENUM_DECL:
                    self.enum(k, rel)
                else:
                    self.record(k, rel)
        key = f"c:{rel}@T@{name}"
        self.alias[lc.usr(c)] = key
        l1, l2, o1, o2 = self.span(c)
        under = lc.lib.clang_getTypedefDeclUnderlyingType(c)
        d = Decl(key=key, kind="typedef", name=name, path=rel, line=l1, end_line=l2, start=o1,
                 end=o2, is_def=True, type=lc.type_spelling(under),
                 value=normalize_type(lc.type_spelling(lc.canonical(under))),
                 conditional=self.is_cond(rel, l1), fnptr=self.fnptr(under), tu=self.tu)
        self.ex.decls.append(d)
        for k in kids:
            if k.kind == C.TYPE_REF or k.kind in (C.STRUCT_DECL, C.UNION_DECL, C.ENUM_DECL):
                ref = lc.referenced(k) if k.kind == C.TYPE_REF else k
                if not lc.is_null(ref):
                    self.annotate(key, self.key_of(ref), rel, l1, raw=True)
            elif k.kind == C.PARM_DECL:
                for sub in lc.children(k):
                    self.visit(sub, key, rel, False, "")

    def macro(self, c, rel: str) -> None:
        lc = self.lc
        name = lc.spelling(c)
        if name == self.guards.get(rel):
            return
        l1, l2, o1, o2 = self.span(c)
        text = self.slice(rel, o1, o2)
        fn_like = bool(lc.lib.clang_Cursor_isMacroFunctionLike(c))
        params: list[tuple[str, str]] = []
        body = text[len(name):] if text.startswith(name) else text
        variadic = False
        if fn_like and body.startswith("("):
            close = body.find(")")
            plist = [p.strip() for p in body[1:close].split(",") if p.strip()]
            variadic = bool(plist) and plist[-1].endswith("...")
            params = [(p, "") for p in plist if not p.endswith("...")]
            body = body[close + 1:]
        self.ex.decls.append(Decl(
            key=f"macro:{rel}@{name}", kind="macro", name=name, path=rel, line=l1, end_line=l2,
            start=o1, end=o2, is_def=True, params=params, variadic=variadic,
            function_like=fn_like, value=body.strip(), conditional=self.is_cond(rel, l1),
            tu=self.tu))

    # -- bodies ----------------------------------------------------------
    def annotate(self, src: str, dst: str, rel: str, line: int, raw: bool = False) -> None:
        if (src, dst) in self.annot_seen or src == dst:
            return
        self.annot_seen.add((src, dst))
        self.ex.refs.append(Ref(src=src, kind="annotates", path=rel, line=line, dst=dst,
                                tu=self.tu))

    def add(self, src: str, kind: str, rel: str, c, dst: str = "", ext: str = "",
            cond: bool = False, **kw) -> None:
        _r, line, col, _o = self.where(c)
        self.ex.refs.append(Ref(src=src, kind=kind, path=rel, line=line, col=col, dst=dst,
                                ext=ext, conditional=cond or self.is_cond(rel, line),
                                tu=self.tu, **kw))

    def visit(self, c, src: str, rel: str, cond: bool, mode: str) -> None:
        """Walk an expression or statement; ``mode`` is "", "write" or "mutate"."""
        lc = self.lc
        k = c.kind
        if k == C.CALL_EXPR:
            self.call(c, src, rel, cond)
            return
        if k == C.DECL_REF_EXPR:
            self.decl_ref(c, src, rel, cond, mode)
            return
        if k == C.MEMBER_REF_EXPR:
            ref = lc.referenced(c)
            if not lc.is_null(ref) and ref.kind == C.FIELD_DECL:
                kind = {"write": "writes", "mutate": "mutates"}.get(mode, "reads")
                self.add(src, kind, rel, c, dst=self.key_of(ref), cond=cond)
            for ch in lc.children(c):
                self.visit(ch, src, rel, cond, "mutate" if mode else "")
            return
        if k == C.TYPE_REF:
            ref = lc.referenced(c)
            if not lc.is_null(ref):
                _r, line, _c, _o = self.where(c)
                self.annotate(src, self.key_of(ref), rel, line, raw=True)
            return
        kids = lc.children(c)
        if k == 100 and kids and kids[0].kind == C.DECL_REF_EXPR \
                and lc.type_of(c).kind == C.TYPE_DEPENDENT:
            # RecoveryExpr: a call the compiler rejected (wrong argument count, bad
            # types). These are exactly the callers a signature change broke.
            ref = lc.referenced(kids[0])
            if not lc.is_null(ref) and ref.kind == C.FUNCTION_DECL \
                    and self.literal(kids[0], lc.spelling(ref)):
                frel, *_ = self.where(ref)
                self.add(src, "calls", rel, kids[0], dst=self.key_of(ref),
                         ext="" if frel else lc.spelling(ref), args=len(kids) - 1, cond=cond,
                         meta={"invalid": True})
                for ch in kids[1:]:
                    self.visit(ch, src, rel, cond, "")
                return
        if k in (114, 115) and len(kids) == 2:          # binary / compound assignment
            lhs, rhs = kids
            op = "="
            if k == 114:
                (_f, _l, _c, lo) = lc.extent(lhs)[1]
                (_f2, _l2, _c2, ro) = lc.extent(rhs)[0]
                op = self.slice(rel, lo, ro).strip() if 0 <= lo <= ro else ""
            if op in ("&&", "||"):
                self.visit(lhs, src, rel, cond, "")
                self.visit(rhs, src, rel, True, "")
                return
            if k == 115 or op == "=":
                self.visit(lhs, src, rel, cond, "write")
                self.visit(rhs, src, rel, cond, "")
                return
        if k == C.UNARY_OPERATOR and kids:
            (_f, _l, _c, a), (_f2, _l2, _c2, b) = lc.extent(c)
            text = self.slice(rel, a, b).strip()
            if text.startswith(("++", "--")) or text.endswith(("++", "--")):
                self.visit(kids[0], src, rel, cond, "write")
                return
            if text.startswith("&"):
                self.visit(kids[0], src, rel, cond, "address")
                return
            if text.startswith("*") and mode:
                self.visit(kids[0], src, rel, cond, "mutate")
                return
        if k == 113 and len(kids) == 2:                  # array subscript
            self.visit(kids[0], src, rel, cond, "mutate" if mode in ("write", "mutate") else mode)
            self.visit(kids[1], src, rel, cond, "")
            return
        if k == C.SWITCH_STMT:
            self.switch(c, kids, src, rel)
        if k in (C.VAR_DECL, C.PARM_DECL):              # a local (static ones too) or parameter
            self.ex.local_names.setdefault((src, rel), set()).add(lc.spelling(c))
        inner = mode if k in _PASS_THROUGH else ""
        for i, ch in enumerate(kids):
            ccond = cond or k in _COND_ALL or (k in _COND_AFTER_FIRST and i > 0)
            self.visit(ch, src, rel, ccond, inner)

    def decl_ref(self, c, src: str, rel: str, cond: bool, mode: str) -> None:
        lc = self.lc
        ref = lc.referenced(c)
        if lc.is_null(ref):
            return
        rk = ref.kind
        if rk == C.FUNCTION_DECL:
            key = self.key_of(ref)
            frel, *_ = self.where(ref)
            self.ex.address_taken[key] = normalize_type(
                lc.type_spelling(lc.canonical(lc.type_of(ref))))
            if self.literal(c, lc.spelling(ref)):
                self.add(src, "reads", rel, c, dst=key, ext="" if frel else lc.spelling(ref),
                         cond=cond, meta={"address_taken": True})
        elif rk == C.VAR_DECL:
            parent = lc.lib.clang_getCursorSemanticParent(ref)
            if parent.kind != C.TRANSLATION_UNIT:
                return                                   # a local
            frel, *_ = self.where(ref)
            if frel is None:
                return                                   # errno, stdin, environ...
            key = self.key_of(ref)
            if mode == "write":
                self.add(src, "writes", rel, c, dst=key, cond=cond)
            elif mode == "mutate":
                self.add(src, "mutates", rel, c, dst=key, cond=cond)
            elif mode == "address":
                self.add(src, "mutates", rel, c, dst=key, cond=cond, dynamic=True,
                         confidence=0.5, meta={"address_taken": True})
            else:
                self.add(src, "reads", rel, c, dst=key, cond=cond)
        elif rk == C.ENUM_CONSTANT_DECL:
            frel, *_ = self.where(ref)
            if frel is not None:
                self.add(src, "reads", rel, c, dst=self.key_of(ref), cond=cond)

    def call(self, c, src: str, rel: str, cond: bool) -> None:
        lc = self.lc
        kids = lc.children(c)
        nargs = lc.lib.clang_Cursor_getNumArguments(c)
        nargs = nargs if nargs >= 0 else None
        ref = lc.referenced(c)
        direct = not lc.is_null(ref) and ref.kind == C.FUNCTION_DECL
        if direct:
            frel, *_ = self.where(ref)
            name = lc.spelling(ref)
            if self.literal(kids[0] if kids else c, name):
                self.add(src, "calls", rel, c, dst=self.key_of(ref), ext="" if frel else name,
                         args=nargs, cond=cond)
            rest = kids[1:] if kids else []
        else:
            fntype = self.fnptr(lc.type_of(kids[0])) if kids else ""
            via = ""
            if not lc.is_null(ref) and ref.kind in (C.FIELD_DECL, C.VAR_DECL):
                if ref.kind == C.FIELD_DECL or lc.lib.clang_getCursorSemanticParent(
                        ref).kind == C.TRANSLATION_UNIT:
                    via = self.key_of(ref)
            self.add(src, "calls", rel, c, args=nargs, cond=cond, dynamic=True,
                     confidence=0.5, meta={"fntype": fntype, "via": via})
            rest = kids
        for ch in rest:
            self.visit(ch, src, rel, cond, "")

    def switch(self, c, kids, src: str, rel: str) -> None:
        lc = self.lc
        if len(kids) < 2:
            return
        covered: list[str] = []
        enums: dict[str, int] = {}
        default = False

        def labels(node) -> None:
            nonlocal default
            for ch in lc.children(node):
                if ch.kind == C.SWITCH_STMT:
                    continue
                if ch.kind == C.DEFAULT_STMT:
                    default = True
                if ch.kind == C.CASE_STMT:
                    sub = lc.children(ch)
                    if sub:
                        const = _enum_const(lc, sub[0])
                        if const is not None:
                            parent = lc.lib.clang_getCursorSemanticParent(const)
                            ekey = self.key_of(parent)
                            enums[ekey] = enums.get(ekey, 0) + 1
                            covered.append(lc.spelling(const))
                labels(ch)
        labels(kids[-1])
        if not enums:
            return
        enum = max(enums, key=lambda e: enums[e])
        _r, line, _c, _o = self.where(c)
        (_f, _l, _c1, a), (_f2, _l2, _c2, b) = lc.extent(kids[0])
        self.ex.switches.append(Switch(fn=src, path=rel, line=line, enum=enum,
                                       covered=sorted(set(covered)), default=default,
                                       subject=self.slice(rel, a, b).strip()))

    # -- after all translation units ------------------------------------
    def finish(self) -> None:
        by_file: dict[str, list[Decl]] = {}
        for d in self.ex.decls:
            if d.kind in ("function", "global") and d.is_def:
                by_file.setdefault(d.path, []).append(d)
        for rel, line, col, off, mkey, name in self.expansions:
            holders = [d for d in by_file.get(rel, ()) if d.line <= line <= d.end_line]
            if not holders:
                continue
            holder = min(holders, key=lambda d: d.end_line - d.line)
            text = self.ex.files.get(rel, "")
            raw = self.texts.get(rel, b"")
            args = None
            if 0 <= off < len(raw):
                # a byte offset is a character offset unless the file has multi-byte text
                pos = off if len(raw) == len(text) else len(raw[:off].decode("utf-8", "replace"))
                args = macro_call_args(text, pos)
            self.ex.refs.append(Ref(src=holder.key, kind="macro", path=rel, line=line, col=col,
                                    dst=mkey, args=args, conditional=self.is_cond(rel, line),
                                    meta={"name": name}))
        for r in self.ex.refs:
            r.src = self.alias.get(r.src, r.src)
            r.dst = self.alias.get(r.dst, r.dst)
            if r.meta.get("via"):
                r.meta["via"] = self.alias.get(r.meta["via"], r.meta["via"])
        for s in self.ex.switches:
            s.enum = self.alias.get(s.enum, s.enum)
        self.ex.address_taken = {self.alias.get(k, k): v
                                 for k, v in self.ex.address_taken.items()}
        self.ex.local_names = {(self.alias.get(k, k), p): v
                               for (k, p), v in self.ex.local_names.items()}


def _enum_const(lc: C.LibClang, node):
    """The enumerator a case label names, looking through casts and parentheses."""
    if node.kind == C.DECL_REF_EXPR:
        ref = lc.referenced(node)
        if not lc.is_null(ref) and ref.kind == C.ENUM_CONSTANT_DECL:
            return ref
        return None
    if node.kind in _PASS_THROUGH or node.kind == 117:
        for ch in lc.children(node):
            got = _enum_const(lc, ch)
            if got is not None:
                return got
    return None


def _is_const(canon: str) -> bool:
    t = canon.strip()
    if "*" in t:
        return t.rstrip().endswith("const")
    return t.startswith("const ") or " const" in t


# --------------------------------------------------------------------------
def extract(root: str, lc: C.LibClang, files: list[str] | None = None) -> Extraction:
    root = os.path.realpath(str(root))
    files = files if files is not None else source_files(root)
    ex = Extraction(backend=f"libclang ({lc.version})")
    ext = _Extractor(lc, root, ex, set(files))
    db = compile_db(root)
    fallback = default_flags(root, files) + compile_flags(root)
    index = lc.index()
    try:
        sources = [f for f in files if f.endswith(".c")]
        for rel in sources:
            full = os.path.join(root, rel)
            flags = db.get(os.path.normcase(os.path.normpath(full)))
            if flags is None:
                flags = fallback
            ext.run_tu(index, full, flags)
        for rel in files:
            if rel.endswith(HEADER_SUFFIXES) and rel not in ext.done:
                ext.run_tu(index, os.path.join(root, rel), fallback)
    finally:
        lc.lib.clang_disposeIndex(index)
    ext.finish()
    for rel in files:
        if rel not in ex.files:
            ex.files[rel] = read_text(root, rel)
    if db:
        ex.diagnostics.append(f"compile_commands.json: {len(db)} entries")
    return ex
