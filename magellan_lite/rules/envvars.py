"""Environment variables: a contract with every deployment, and no import statement says so.

From the full Magellan (``magellan/rules/envvars.py``). A function that reads
``DATABASE_URL`` depends on whoever deploys it. Rename the variable, change its default or
start requiring it, and every server, container, CI job and colleague's ``.env`` that was
fine yesterday fails -- or worse, quietly falls back to a default. The edit is one line; the
fault shows up in a deployment nobody looked at.

* ``env-var-renamed``: code that read one variable now reads another in its place. Everything
  that sets the old name is silently ignored.
* ``env-var-default-changed``: the fallback for a variable changed, or an optional variable
  became required (``os.getenv("X", "d")`` -> ``os.environ["X"]``).

Reads are ``os.environ[...]``, ``os.environ.get(...)`` and ``os.getenv(...)`` (also imported
as ``environ`` / ``getenv``), with the name written as a string or a module constant. Each read
belongs to the function that does it, or to the module constant it initialises. A read whose
name is computed at run time is left alone.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from magellan_lite.findings import Finding, change_rule
from magellan_lite.rules.common import callables, changed_files, dotted
from magellan_lite.source import module_name


@dataclass(frozen=True)
class Read:
    var: str
    reader: str            # the function, or module constant, that reads it
    required: bool         # os.environ[...]: KeyError when unset
    default: str           # the fallback as written ("" when none)
    shape: str             # the fallback's syntax tree, so quoting never counts as a change
    path: str
    line: int


def _aliases(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _canonical(node: ast.AST, aliases: dict[str, str]) -> str:
    name = dotted(node)
    head, _, rest = name.partition(".")
    if head in aliases:
        return aliases[head] + ("." + rest if rest else "")
    return name


def _strings(tree: ast.Module) -> dict[str, str]:
    """Module constants holding a string: ``KEY = "API_URL"`` lets ``getenv(KEY)`` be read."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            out.update({t.id: node.value.value for t in node.targets if isinstance(t, ast.Name)})
    return out


def _name(node: ast.AST | None, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    return None


def _reads_in(nodes, reader: str, path: str, aliases, consts) -> list[Read]:
    out: list[Read] = []
    for node in nodes:
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) \
                and _canonical(node.value, aliases) == "os.environ":
            var = _name(node.slice, consts)
            if var:
                out.append(Read(var, reader, True, "", "", path, node.lineno))
        elif isinstance(node, ast.Call) \
                and _canonical(node.func, aliases) in ("os.environ.get", "os.getenv"):
            var = _name(node.args[0] if node.args else
                        next((k.value for k in node.keywords if k.arg == "key"), None), consts)
            if not var:
                continue
            default = node.args[1] if len(node.args) > 1 else next(
                (k.value for k in node.keywords if k.arg == "default"), None)
            out.append(Read(var, reader, False,
                            ast.unparse(default) if default is not None else "",
                            ast.dump(default) if default is not None else "",
                            path, node.lineno))
    return out


def env_reads(snapshot, paths=None) -> list[Read]:
    """Every environment variable read in the snapshot's Python files (or in ``paths``)."""
    out: list[Read] = []
    for path, text in sorted(snapshot.files.items()):
        if not path.endswith(".py") or ("environ" not in text and "getenv" not in text)                 or (paths is not None and path not in paths):
            continue
        tree = snapshot.tree(path)
        if tree is None:
            continue
        aliases, consts = _aliases(tree), _strings(tree)
        if not any(v.startswith("os") for v in aliases.values()):
            continue
        mod = module_name(path)
        for name, fn, _cls in callables(tree, path):
            out += _reads_in(ast.walk(fn), name, path, aliases, consts)

        def top(body, prefix):
            for stmt in body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if isinstance(stmt, ast.ClassDef):
                    top(stmt.body, f"{prefix}.{stmt.name}")
                    continue
                targets = stmt.targets if isinstance(stmt, ast.Assign) else \
                    [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
                named = [t.id for t in targets if isinstance(t, ast.Name)]
                reader = f"{prefix}.{named[0]}" if len(named) == 1 else prefix
                out.extend(_reads_in(ast.walk(stmt), reader, path, aliases, consts))
        top(tree.body, mod)
    return out


def _by_reader(reads: list[Read]) -> dict[str, dict[str, list[Read]]]:
    out: dict[str, dict[str, list[Read]]] = {}
    for r in reads:
        out.setdefault(r.reader, {}).setdefault(r.var, []).append(r)
    return out


def _state(ctx):
    if not hasattr(ctx, "_lite_env"):
        files = changed_files(ctx)
        ctx._lite_env = None
        if files:                                   # a reader in an untouched file is unchanged
            ctx._lite_env = (_by_reader(env_reads(ctx.before, files)),
                             _by_reader(env_reads(ctx.after, files)), files)
    return ctx._lite_env


def _who(reader: str) -> str:
    """How a person names a reader: ``db()`` for a function, ``DB_URL`` for a constant."""
    short = reader.rsplit(".", 1)[-1]
    return short if short.isupper() or "." not in reader else f"{short}()"


def _fallback(r: Read) -> str:
    return "raises KeyError where it is not set" if r.required else (
        f"falls back to {r.default}" if r.default else "gets None where it is not set")


@change_rule("env-var-renamed", "high",
             fix="Read the old name as a fallback (`os.getenv(NEW) or os.getenv(OLD)`), or "
                 "update every deployment that sets it in the same release.")
def env_var_renamed(ctx):
    state = _state(ctx)
    if state is None:
        return
    before, after, files = state
    read_before = None
    for reader in sorted(set(before) & set(after)):
        old, new = before[reader], after[reader]
        lost = sorted(set(old) - set(new))
        gained = sorted(set(new) - set(old))
        if len(lost) != 1 or len(gained) != 1:
            continue
        if read_before is None:                     # was the new name in use already?
            read_before = {r.var for r in env_reads(ctx.before)}
        if gained[0] in read_before:
            continue
        r = new[gained[0]][0]
        if r.path not in files:
            continue
        still = sorted({x.path for vars_ in after.values() for x in vars_.get(lost[0], [])})
        yield Finding(
            "env-var-renamed", "high",
            f"{_who(reader)} now reads the environment variable {gained[0]} instead of "
            f"{lost[0]}",
            r.path, r.line,
            detail=(f"Every server, container, CI job and .env file that sets {lost[0]} is "
                    f"now ignored here, and where {gained[0]} is not set yet the code "
                    f"{_fallback(r)}. Nothing fails at the commit; it fails, or quietly "
                    f"misbehaves, in the deployment."
                    + (f" {lost[0]} is still read in {', '.join(still[:3])}, so the two "
                       f"places now disagree." if still else "")))


@change_rule("env-var-default-changed", "medium",
             fix="Check that every deployment sets the variable explicitly, or keep the old "
                 "default.")
def env_var_default_changed(ctx):
    state = _state(ctx)
    if state is None:
        return
    before, after, files = state
    for reader in sorted(set(before) & set(after)):
        for var in sorted(set(before[reader]) & set(after[reader])):
            old_variants = {(r.required, r.shape) for r in before[reader][var]}
            new_variants = {(r.required, r.shape) for r in after[reader][var]}
            if len(old_variants) != 1 or len(new_variants) != 1 or old_variants == new_variants:
                continue
            was, now = before[reader][var][0], after[reader][var][0]
            if now.path not in files:
                continue
            if now.required and not was.required:
                message = (f"{_who(reader)} now requires the environment variable {var}: it "
                           f"used to {'fall back to ' + was.default if was.default else 'get None'}"
                           f" when it was not set")
                detail = (f"Every environment that never set {var} -- because it did not "
                          f"have to -- now raises KeyError here.")
            elif was.required or now.required:
                continue                                # required -> optional: looser
            else:
                message = (f"the default for the environment variable {var} changed in "
                           f"{_who(reader)}: {was.default or 'None'} -> {now.default or 'None'}")
                detail = (f"Every environment that does not set {var} now behaves "
                          f"differently, without anyone editing its configuration.")
            yield Finding("env-var-default-changed", "medium", message, now.path, now.line,
                          detail=detail)
