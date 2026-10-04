"""Dependencies: imports the project's manifests do not back up.

From the full Magellan (``magellan/rules/deps.py``). Every ``import requests`` leans on
something you do not own, and the manifests (``pyproject.toml``, ``requirements*.txt``,
``setup.py``, ``setup.cfg``, ``Pipfile``) say what a fresh install will get. Two ways they
disagree break a deploy while working fine on the author's machine:

* ``dependency-undeclared``: the change adds an import of a package no manifest declares. It
  imports on a machine that happens to have it, and fails with ModuleNotFoundError in CI, on
  a colleague's machine, on the server.
* ``dependency-removed-still-used``: the change takes a package out of the manifest while the
  code still imports it. Fresh installs stop getting it; the import fails at startup.

Manifests are files, not source, so these rules read them from disk: only when the check is
on a real project (``magellan-lite check``, the editor), never for code handed over as text.
The old manifests come from git (``--against git:REV``) or the baseline directory.
``pyproject.toml`` is read with ``tomllib`` on Python 3.11+, and with a small reader of the
dependency tables on 3.10. Imports that may fail on purpose (inside ``try: ... except
ImportError``, under ``if TYPE_CHECKING:``) are left alone, and when a manifest cannot be read
for sure (dependencies computed in ``setup.py``, a conda ``environment.yml``) the rules stay
quiet rather than guess.
"""

from __future__ import annotations

import ast
import configparser
import os
import re
import sys
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from magellan_lite.findings import Finding, change_rule
from magellan_lite.rules.common import (baseline, changed_files, git, is_test_path, on_disk,
                                        under_import_guard)
from magellan_lite.source import is_skipped_dir, module_name

_MANIFEST = re.compile(r"(^|/)(pyproject\.toml|requirements[\w.-]*\.(txt|in)|requirements/[\w.-]+"
                       r"\.(txt|in)|setup\.py|setup\.cfg|Pipfile|environment\.ya?ml)$")
_SKIP = {"node_modules", "venv", "env", "build", "dist", "site-packages", "__pycache__"}

#: import name -> distribution name, for the common ones that differ
PY_ALIASES = {
    "pil": "pillow", "yaml": "pyyaml", "sklearn": "scikit-learn", "cv2": "opencv-python",
    "bs4": "beautifulsoup4", "dateutil": "python-dateutil", "dotenv": "python-dotenv",
    "jwt": "pyjwt", "openssl": "pyopenssl", "attr": "attrs", "git": "gitpython",
    "serial": "pyserial", "crypto": "pycryptodome", "magic": "python-magic",
    "mysqldb": "mysqlclient", "psycopg2": "psycopg2-binary", "skimage": "scikit-image",
    "docx": "python-docx", "zmq": "pyzmq", "usb": "pyusb", "gi": "pygobject",
    "ruamel": "ruamel-yaml", "socks": "pysocks", "faiss": "faiss-cpu",
    "google": "google-api-python-client", "multipart": "python-multipart",
    "jose": "python-jose", "slugify": "python-slugify", "telegram": "python-telegram-bot",
    "pkg_resources": "setuptools",
}
#: where imports are tooling, not the product: a missing declaration breaks CI, not a deploy
_TOOLING_DIRS = {"docs", "doc", "scripts", "tools", "examples", "benchmarks", "bench"}
_TOOLING_FILES = {"setup.py", "noxfile.py", "tasks.py", "fabfile.py", "conftest.py"}
#: standard-library modules of earlier Pythons that this interpreter no longer lists
_PAST_STDLIB = frozenset("""
    imp distutils asyncore asynchat smtpd cgi cgitb crypt imghdr pipes telnetlib nntplib
    audioop uu xdrlib msilib nis ossaudiodev spwd sndhdr sunau chunk mailcap lib2to3 binhex
    formatter parser symbol macpath dummy_threading urllib2 urlparse httplib cookielib Cookie
    HTMLParser htmlentitydefs xmlrpclib SimpleXMLRPCServer BaseHTTPServer SimpleHTTPServer
    CGIHTTPServer SocketServer ConfigParser Queue StringIO cStringIO cPickle copy_reg repr
    anydbm commands sets md5 sha new mimetools rfc822 thread exceptions __builtin__ Tkinter
    tkMessageBox ttk UserDict UserList UserString robotparser
""".split())
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) | _PAST_STDLIB | {"__future__"}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


# -- reading manifests ----------------------------------------------------------------------
@dataclass
class Manifests:
    declared: dict[str, str] = field(default_factory=dict)    # normalized name -> manifest
    found: list[str] = field(default_factory=list)            # manifest paths that declare
    certain: bool = True        # False: some manifest computes its list; do not judge by it


_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirement(text: str) -> str | None:
    t = text.strip()
    if not t or t.startswith(("#", "-", "git+", "http", ".", "/")):
        return None
    if " @ " in t:                                  # name @ git+https://...
        t = t.split(" @ ", 1)[0]
    m = _REQ.match(t)
    return norm(m.group(1)) if m else None


def _read_toml(text: str) -> dict | None:
    try:
        import tomllib                              # Python 3.11+
    except ImportError:
        return _mini_toml(text)
    try:
        return tomllib.loads(text)
    except (ValueError, TypeError):
        return None


def _mini_toml(text: str) -> dict | None:
    """Just enough TOML for dependency tables, for Python 3.10: ``[a.b]`` headers, and keys
    whose value is a string, an array of strings (over several lines) or an inline table of
    strings. Anything else is skipped."""
    data: dict = {}
    table: dict | None = data
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith("[["):
            table = None                            # an array of tables: not needed here
            continue
        if line.startswith("["):
            table = data
            for part in _keys(line.strip("[] \t")):
                table = table.setdefault(part, {})
                if not isinstance(table, dict):
                    table = None
                    break
            continue
        if table is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value.startswith("["):
            while _depth(value) > 0 and i < len(lines):
                value += "\n" + lines[i]
                i += 1
            parsed = _strings(value)
        elif value.startswith("{"):
            parsed = dict(re.findall(r"""([\w-]+)\s*=\s*["']([^"']*)["']""", value))
        elif value[:1] in "\"'":
            found = _strings(value)
            parsed = found[0] if found else ""
        else:
            continue
        keys = _keys(key.strip())
        target = table
        for part in keys[:-1]:
            target = target.setdefault(part, {})
            if not isinstance(target, dict):
                break
        else:
            target[keys[-1]] = parsed
    return data


def _keys(dotted: str) -> list[str]:
    return [k.strip().strip("\"'") for k in dotted.split(".")]


def _strings(value: str) -> list[str]:
    return [a or b for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', _no_comments(value))]


def _no_comments(value: str) -> str:
    """Drop ``# ...`` comments that are not inside a string."""
    out, quote = [], ""
    for line in value.splitlines():
        keep = []
        for ch in line:
            if quote:
                quote = "" if ch == quote else quote
            elif ch in "\"'":
                quote = ch
            elif ch == "#":
                break
            keep.append(ch)
        out.append("".join(keep))
        quote = ""
    return "\n".join(out)


def _depth(value: str) -> int:
    text = _no_comments(value)
    text = re.sub(r'"[^"]*"|\'[^\']*\'', "", text)
    return text.count("[") - text.count("]")


def _pyproject(text: str, path: str, m: Manifests) -> None:
    data = _read_toml(text)
    if data is None:
        m.certain = False
        return
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    poetry = (data.get("tool") or {}).get("poetry") if isinstance(data.get("tool"), dict) else None
    groups = data.get("dependency-groups") if isinstance(data.get("dependency-groups"), dict) else {}
    lists: list = []
    if project:
        lists.append(project.get("dependencies") or [])
        lists += list((project.get("optional-dependencies") or {}).values())
        if "dependencies" in (project.get("dynamic") or []):
            m.certain = False                       # setuptools fills it in from elsewhere
        if isinstance(project.get("name"), str):
            m.declared.setdefault(norm(project["name"]), path)
    lists += list(groups.values())
    build = data.get("build-system") if isinstance(data.get("build-system"), dict) else {}
    lists.append(build.get("requires") or [])
    for entries in lists:
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, str) and (name := _requirement(entry)):
                m.declared.setdefault(name, path)
    if isinstance(poetry, dict):
        tables = [poetry.get("dependencies") or {}, poetry.get("dev-dependencies") or {}]
        tables += [g.get("dependencies") or {} for g in (poetry.get("group") or {}).values()
                   if isinstance(g, dict)]
        for table in tables:
            for name in table if isinstance(table, dict) else []:
                if name.lower() != "python":
                    m.declared.setdefault(norm(name), path)
    if project or poetry or groups:
        m.found.append(path)


def _setup_py(text: str, path: str, m: Manifests) -> None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        m.certain = False
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in ("install_requires", "extras_require", "tests_require"):
                    try:
                        value = ast.literal_eval(kw.value)
                    except (ValueError, SyntaxError, TypeError):
                        m.certain = False           # computed: cannot be read for sure
                        continue
                    lists = value.values() if isinstance(value, dict) else [value]
                    for entries in lists:
                        for entry in [entries] if isinstance(entries, str) else entries:
                            if isinstance(entry, str) and (name := _requirement(entry)):
                                m.declared.setdefault(name, path)
                    m.found.append(path)


def _setup_cfg(text: str, path: str, m: Manifests) -> None:
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read_string(text)
    except configparser.Error:
        m.certain = False
        return
    values = []
    if cfg.has_option("options", "install_requires"):
        values.append(cfg.get("options", "install_requires"))
    if cfg.has_section("options.extras_require"):
        values += [v for _k, v in cfg.items("options.extras_require")]
    for value in values:
        for line in value.splitlines():
            if name := _requirement(line):
                m.declared.setdefault(name, path)
    if values:
        m.found.append(path)


def parse_manifests(files: dict[str, str]) -> Manifests:
    """What ``{path: text}`` of manifest files declares, all of them together."""
    m = Manifests()
    for path, text in sorted(files.items()):
        base = path.rsplit("/", 1)[-1]
        if base == "pyproject.toml":
            _pyproject(text, path, m)
        elif base.startswith("requirements") or f"/{path}".count("/requirements/"):
            for line in text.splitlines():
                if name := _requirement(line.split(";", 1)[0]):
                    m.declared.setdefault(name, path)
            m.found.append(path)
        elif base == "setup.py":
            _setup_py(text, path, m)
        elif base == "setup.cfg":
            _setup_cfg(text, path, m)
        elif base == "Pipfile":
            data = _read_toml(text) or {}
            for table in ("packages", "dev-packages"):
                for name in data.get(table) or {}:
                    m.declared.setdefault(norm(name), path)
            m.found.append(path)
        elif base.startswith("environment."):
            m.certain = False                       # conda: other names, other channels
    return m


def manifests_on_disk(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP and not is_skipped_dir(d))
        for name in filenames:
            rel = Path(dirpath, name).relative_to(root).as_posix()
            if _MANIFEST.search(rel):
                try:
                    out[rel] = (Path(dirpath) / name).read_text(encoding="utf-8",
                                                                errors="replace")
                except OSError:
                    pass
    return out


def manifests_at(root: Path, rev: str) -> dict[str, str] | None:
    """The manifests under ``root`` as they were at git revision ``rev``; None if unknown."""
    top = git(root, "rev-parse", "--show-toplevel")
    if top is None:
        return None
    try:
        prefix = root.resolve().relative_to(Path(top.strip()).resolve()).as_posix()
    except ValueError:
        return None
    prefix = "" if prefix == "." else prefix + "/"
    listing = git(Path(top.strip()), "ls-tree", "-r", "--full-tree", "--name-only", rev)
    if listing is None:
        return None
    out: dict[str, str] = {}
    for line in listing.splitlines():
        if not line.startswith(prefix):
            continue
        rel = line[len(prefix):]
        if any(part in _SKIP or is_skipped_dir(part) for part in rel.split("/")[:-1]):
            continue
        if _MANIFEST.search(rel):
            text = git(Path(top.strip()), "show", f"{rev}:{line}")
            if text is None:
                return None
            out[rel] = text
    return out


# -- imports ----------------------------------------------------------------------------------
_dists: dict[str, list[str]] | None = None


def _installed(import_name: str) -> list[str]:
    """The installed distributions that provide an import name (this environment's view)."""
    global _dists
    if _dists is None:
        try:
            from importlib.metadata import packages_distributions
            _dists = {k: [norm(x) for x in v] for k, v in packages_distributions().items()}
        except Exception:                               # noqa: BLE001 - only a hint
            _dists = {}
    return _dists.get(import_name, [])


def declared_as(import_root: str, declared: dict[str, str]) -> str | None:
    """The declared distribution that provides ``import_root``, if any."""
    low = norm(import_root)
    candidates = {low, norm(PY_ALIASES.get(import_root.lower(), low)), *_installed(import_root)}
    for c in candidates:
        if c in declared:
            return c
    for name in declared:                              # namespaces: google-cloud-*, azure-*
        if name.startswith(low + "-") or low.startswith(name + "-"):
            return name
    return None


def imports(snapshot, paths=None) -> dict[str, list[tuple[str, int]]]:
    """``{top-level module: [(path, line)]}`` for every absolute import in ``paths`` (default:
    every file) that is not guarded (``try/except ImportError``, ``if TYPE_CHECKING``)."""
    out: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(snapshot.files if paths is None else paths):
        tree = snapshot.tree(path) if path.endswith(".py") and path in snapshot.files else None
        if tree is None:
            continue
        guarded = under_import_guard(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)) or node.lineno in guarded:
                continue
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                roots = [node.module.split(".")[0]]
            else:
                continue
            for r in roots:
                out.setdefault(r, []).append((path, node.lineno))
    return out


def _internal(*snapshots) -> set[str]:
    """Names the project's own code can be imported by: every module and directory name."""
    out: set[str] = set()
    for snap in snapshots:
        for path in snap.files:
            parts = path.split("/")
            out |= set(parts[:-1])
            out.add(parts[-1].rsplit(".", 1)[0])
            if path.endswith(".py"):
                out.add(module_name(path).split(".")[0])
    return out


def _third_party(name: str, internal: set[str]) -> bool:
    return name not in _STDLIB and name not in internal and not name.startswith("_")


class _State:
    """What both rules read, each part worked out once per check and only when asked."""

    def __init__(self, ctx, root: Path) -> None:
        self.ctx, self.root = ctx, root

    @cached_property
    def now(self) -> Manifests:
        return parse_manifests(manifests_on_disk(self.root))

    @cached_property
    def manifests_changed(self) -> bool:
        """Did any manifest change against the baseline? One cheap question before the
        expensive ones."""
        base = baseline(self.ctx)
        if base is None:
            return False
        if base[0] == "git":
            changed = git(self.root, "diff", "--name-only", "--relative", base[1], "--", ".")
            return changed is not None and any(_MANIFEST.search(line.strip())
                                               for line in changed.splitlines())
        return manifests_on_disk(base[1]) != manifests_on_disk(self.root)

    @cached_property
    def old(self) -> Manifests | None:
        base = baseline(self.ctx)
        if base is None:
            return None
        files = manifests_at(self.root, base[1]) if base[0] == "git" \
            else manifests_on_disk(base[1])
        return parse_manifests(files) if files is not None else None

    @cached_property
    def internal(self) -> set[str]:
        return _internal(self.ctx.after, self.ctx.before)


def _state(ctx) -> _State | None:
    if not hasattr(ctx, "_lite_deps"):
        root = on_disk(ctx)
        ctx._lite_deps = _State(ctx, root) if root is not None else None
    return ctx._lite_deps


def _tooling(path: str) -> bool:
    parts = path.split("/")
    return is_test_path(path) or parts[0] in _TOOLING_DIRS or parts[-1] in _TOOLING_FILES


def _where(sites: list[tuple[str, int]]) -> str:
    files = sorted({p for p, _ in sites})
    return files[0] if len(files) == 1 else f"{files[0]} and {len(files) - 1} other file(s)"


def _new_imports(ctx, state: _State) -> dict[str, list[tuple[str, int]]]:
    """Third-party imports this change adds: imported in an edited file now, and nowhere in
    the old version."""
    changed = changed_files(ctx)
    now = imports(ctx.after, [p for p in changed if p in ctx.after.files])
    was = imports(ctx.before, [p for p in changed if p in ctx.before.files])
    fresh = {n: s for n, s in now.items() if n not in was}
    if not fresh:
        return {}
    fresh = {n: s for n, s in fresh.items() if _third_party(n, state.internal)}
    untouched = [p for p in ctx.before.files if p.endswith(".py") and p not in changed]
    for name in list(fresh):
        mention = [p for p in untouched if name in ctx.before.files[p]]   # text first: cheap
        if mention and name in imports(ctx.before, mention):
            del fresh[name]
    return fresh


@change_rule("dependency-undeclared", "high",
             fix="Add it to the project's dependencies (pyproject.toml or requirements.txt), "
                 "or import something the project already depends on.")
def dependency_undeclared(ctx):
    state = _state(ctx)
    if state is None:
        return
    fresh = _new_imports(ctx, state)
    if not fresh or not state.now.found or not state.now.certain:
        return
    now = state.now
    for name, sites in sorted(fresh.items()):
        if declared_as(name, now.declared) is not None:
            continue
        product = [s for s in sites if not _tooling(s[0])]
        path, line = (product or sites)[0]
        yield Finding(
            "dependency-undeclared", "high" if product else "medium",
            f"{name} is imported in {_where(sites)} but no manifest declares it",
            path, line,
            detail=(f"It imports here because it happens to be installed on this machine. A "
                    f"fresh install (CI, a colleague, the server) reads "
                    f"{', '.join(now.found[:3])} and never installs {name}, so the import "
                    f"fails with ModuleNotFoundError there."
                    + ("" if product else " Only tests or tooling import it, so it is CI "
                                          "that breaks, not the product.")))


@change_rule("dependency-removed-still-used", "critical", blocking=True,
             fix="Put the dependency back, or remove the imports that still need it in the "
                 "same change.")
def dependency_removed_still_used(ctx):
    state = _state(ctx)
    if state is None or not state.manifests_changed:
        return
    now, old = state.now, state.old
    if not now.found or not now.certain or old is None or not old.found or not old.certain:
        return
    gone = sorted(set(old.declared) - set(now.declared))
    if not gone:
        return
    used = imports(ctx.after)
    for dep in gone:
        for name, sites in sorted(used.items()):
            if not _third_party(name, state.internal) or declared_as(name, {dep: ""}) is None:
                continue
            path, line = sites[0]
            yield Finding(
                "dependency-removed-still-used", "critical",
                f"{dep} was removed from {old.declared[dep]}, but {_where(sites)} still "
                f"imports {name}",
                path, line,
                detail=(f"Installs from the new manifest no longer get {dep}. On any fresh "
                        f"machine `import {name}` fails with ModuleNotFoundError as soon as "
                        f"this module is imported ({len(sites)} import(s) left)."))
