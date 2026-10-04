"""unvalidated-input-reaches-sink: outside input reaching a dangerous call unchecked.

From the full Magellan (``magellan/python/rules/dataflow.py``). The dangerous change is rarely
at the edit site: a route handler gains a parameter and passes it three calls down to
``subprocess.run``, or a helper that used to get a validated number now gets a string. Nothing
at either end looks wrong; the *path* is wrong, and the call graph knows the calls.

Values from outside -- request data in a web handler (Flask, Django, FastAPI and friends),
``sys.argv`` and ``argparse``, ``input()``, a network response -- are followed through
assignments, string building and calls, into the project's own functions and back out of
them. A flow ends in a sink: a shell command, ``eval``/``exec``, a SQL string, an unsafe
deserializer (``pickle``, ``yaml.load``), a file path, an outbound URL, a redirect, raw HTML.

A flow is cleared by a sanitizer (``int()``, ``shlex.quote`` for shells only, ``html.escape``
for HTML only, ``os.path.basename``, ...), by a guard (``if x in ALLOWED``, ``x.isdigit()``,
``re.fullmatch(p, x)``, or ``if not valid(x): raise`` before the use), or by a function named
like a validator (``validate_*``, ``sanitize_*``, ``check_*``). Only flows through code the
change added or edited are reported: a standing flow is not news on every commit.

Quiet on purpose: request data counts only in files that import a web framework, a command
line tool opening the path it was given is what it is for (only shells, ``eval``, SQL and
deserializers count for command-line input, one level lower), and anything the analysis cannot
follow (a value smuggled through another object, a call it cannot resolve) is dropped, so a
missing finding is not proof of safety.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from magellan_lite.findings import Finding, change_rule
from magellan_lite.rules.common import call_name, dotted, edited, functions, is_test_path

ALL = "*"
_WEB = ("flask", "django", "fastapi", "starlette", "aiohttp", "tornado", "bottle", "falcon",
        "sanic", "pyramid", "werkzeug", "quart", "litestar", "rest_framework")
_CLI = ("click", "typer")
_REQUEST_NAMES = {"request", "req", "flask_request"}
_REQUEST_ATTRS = {"args", "form", "values", "json", "data", "files", "headers", "cookies",
                  "GET", "POST", "body", "query_params", "path_params", "query", "params",
                  "url", "host", "query_string", "get_json", "get_data", "stream", "content",
                  "match_info", "path"}
_ROUTE_DECORATORS = {"route", "get", "post", "put", "patch", "delete", "websocket",
                     "api_route", "api_view", "view_config", "endpoint"}
_CLI_DECORATORS = {"command", "group"}
#: handler parameters that are framework objects, not user data
_NOT_DATA = {"request", "req", "response", "session", "context", "db", "background_tasks",
             "self", "cls"}
#: parameter types a framework has already converted (FastAPI, Flask converters)
_TYPED = {"int", "float", "bool", "UUID", "uuid.UUID", "date", "datetime", "Decimal"}
_NETWORK_CALLS = {"requests.get", "requests.post", "requests.put", "requests.request",
                  "urllib.request.urlopen", "httpx.get", "httpx.post", "httpx.request"}
_NETWORK_RESULT = {"text", "json", "content", "read"}


@dataclass(frozen=True)
class SinkSpec:
    kind: str
    names: frozenset          # canonical dotted names (after import aliases)
    tails: frozenset = frozenset()     # matched on the last part: framework functions
    methods: frozenset = frozenset()   # matched on `<anything>.<method>(...)`
    builtins: frozenset = frozenset()  # matched on a bare name nothing in the file rebinds
    args: str = "first"       # "first" | "any"
    does: str = ""


SINKS = (
    SinkSpec("exec", frozenset({
        "os.system", "os.popen", "subprocess.run", "subprocess.call", "subprocess.check_call",
        "subprocess.check_output", "subprocess.Popen", "subprocess.getoutput",
        "subprocess.getstatusoutput", "os.execv", "os.execvp", "os.execl", "os.execlp",
        "os.spawnl", "os.spawnlp", "commands.getoutput", "asyncio.create_subprocess_shell"}),
        does="runs a shell command"),
    SinkSpec("eval", frozenset({"builtins.eval", "builtins.exec"}),
             builtins=frozenset({"eval", "exec", "compile"}), does="runs it as Python code"),
    SinkSpec("sql", frozenset(), methods=frozenset({"execute", "executemany", "executescript"}),
             does="runs it as part of a SQL statement"),
    SinkSpec("deserialize", frozenset({
        "pickle.loads", "pickle.load", "marshal.loads", "marshal.load", "dill.loads",
        "yaml.unsafe_load", "yaml.load", "shelve.open", "jsonpickle.decode",
        "cPickle.loads"}), does="unpickles it, which can run code"),
    SinkSpec("path", frozenset({
        "io.open", "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.rename",
        "os.chmod", "os.listdir", "os.makedirs", "shutil.rmtree", "shutil.copy",
        "shutil.copyfile", "shutil.move", "shutil.copytree"}),
        tails=frozenset({"send_file"}), builtins=frozenset({"open"}), args="any",
        does="uses it as a file path"),
    SinkSpec("ssrf", frozenset({
        "requests.get", "requests.post", "requests.put", "requests.delete", "requests.head",
        "requests.patch", "requests.request", "urllib.request.urlopen", "httpx.get",
        "httpx.post", "httpx.request", "aiohttp.request"}),
        does="fetches it as a URL from the server"),
    SinkSpec("redirect", frozenset(),
             tails=frozenset({"redirect", "HttpResponseRedirect", "RedirectResponse"}),
             does="redirects the visitor to it"),
    SinkSpec("html", frozenset(),
             tails=frozenset({"render_template_string", "Markup", "mark_safe",
                              "HTMLResponse"}), does="puts it into a page as raw HTML"),
)
_SEVERITY = {"exec": "critical", "eval": "critical", "sql": "critical",
             "deserialize": "critical", "path": "high", "ssrf": "high",
             "redirect": "medium", "html": "medium"}
_LOCAL_KINDS = {"cli", "stdin"}             # the person running it typed it
_LOCAL_SINKS = {"exec", "eval", "sql", "deserialize"}
_FIX = {
    "exec": "Pass a list of arguments without shell=True, and check the value against a "
            "list of allowed ones; shlex.quote is a last resort.",
    "eval": "Never run input as code: parse it (json, ast.literal_eval) or look it up in a "
            "fixed table.",
    "sql": "Use a parameterized query: execute(sql, params), never build the SQL string "
           "from the value.",
    "deserialize": "Use a data-only format (json), or yaml.safe_load, or check a signature "
                   "before loading.",
    "path": "Resolve the path and check it stays inside the allowed folder; use "
            "os.path.basename for a file name.",
    "ssrf": "Check the host against an allow-list and refuse internal addresses.",
    "redirect": "Only redirect to known relative paths or an allow-listed host.",
    "html": "Escape it (html.escape, or let the template escape it) instead of marking it "
            "safe.",
}

_CLEAR_ALL = {"int", "float", "bool", "len", "abs", "round", "ord", "hash", "id", "min",
              "max", "isinstance", "type", "uuid.UUID", "UUID", "ipaddress.ip_address",
              "Decimal", "decimal.Decimal", "os.path.basename", "secure_filename",
              "werkzeug.utils.secure_filename", "slugify", "hexdigest", "digest",
              "hashlib.sha256", "hashlib.sha1", "hashlib.md5", "datetime.fromisoformat"}
_CLEAR_KINDS = {
    "shlex.quote": {"exec"}, "shlex.join": {"exec"}, "pipes.quote": {"exec"},
    "html.escape": {"html"}, "markupsafe.escape": {"html"}, "escape": {"html"},
    "bleach.clean": {"html"}, "urllib.parse.quote": {"ssrf", "redirect", "path"},
    "urllib.parse.quote_plus": {"ssrf", "redirect", "path"}, "quote": {"ssrf", "redirect", "path"},
    "quote_plus": {"ssrf", "redirect", "path"}, "re.escape": {"eval"},
    "json.dumps": {"eval", "html"},
}
_VALIDATOR = re.compile(r"^_*(validate|sanitize|sanitise|clean|check|verify|is_valid|ensure|"
                        r"assert_valid|require)", re.I)
_VALID_METHODS = {"isdigit", "isalnum", "isalpha", "isnumeric", "isidentifier", "isdecimal"}


@dataclass(frozen=True)
class Source:
    kind: str
    desc: str
    fn: str
    path: str
    line: int


@dataclass(frozen=True)
class Origin:
    """Where a value's taint comes from: a real source, or the function's Nth parameter."""
    src: Source | None = None
    param: int = -1
    cleared: frozenset = frozenset()

    def clearing(self, kinds) -> "Origin":
        return Origin(self.src, self.param, self.cleared | frozenset(kinds))


@dataclass(frozen=True)
class Hop:
    fn: str               # the function that makes the call
    path: str
    line: int
    callee: str           # the function it hands the value to


@dataclass(frozen=True)
class SinkHit:
    kind: str
    call: str
    fn: str
    path: str
    line: int
    chain: tuple = ()             # calls leading down to the sink


@dataclass
class Summary:
    param_sinks: dict = field(default_factory=dict)      # param index -> {key: SinkHit}
    param_returns: set = field(default_factory=set)
    returns_source: set = field(default_factory=set)


@dataclass(frozen=True)
class Flow:
    source: Source
    sink: SinkHit
    via: str                       # the function the flow was found in


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1]


class _File:
    """What one module says about its names: imports, and whether it is web or CLI code."""

    def __init__(self, tree: ast.Module) -> None:
        self.aliases: dict[str, str] = {}
        self.rebound: set[str] = set()
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    roots.add(a.name.split(".")[0])
                    self.aliases[a.asname or a.name.split(".")[0]] = \
                        a.name if a.asname else a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                roots.add(node.module.split(".")[0])
                for a in node.names:
                    self.aliases[a.asname or a.name] = f"{node.module}.{a.name}"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.rebound.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self.rebound.add(node.id)
        self.web = any(r in _WEB for r in roots)
        self.cli = any(r in _CLI for r in roots)

    def canonical(self, func: ast.AST) -> str:
        """The dotted name a call target stands for, through the module's imports."""
        name = dotted(func)
        if not name:
            if isinstance(func, ast.Attribute):
                return "?." + func.attr
            return ""
        head, _, rest = name.partition(".")
        if head in self.aliases:
            return self.aliases[head] + ("." + rest if rest else "")
        return name


class _Analysis:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.fns = functions(ctx.after)
        self.files: dict[str, _File] = {}
        self.summaries: dict[str, Summary] = {}
        self.busy: set[str] = set()
        self.flows: set[Flow] = set()
        self.walked: set[str] = set()

    def file(self, path: str) -> _File:
        if path not in self.files:
            self.files[path] = _File(self.ctx.after.tree(path))
        return self.files[path]

    def callees(self, owner: str, call: ast.Call, calls_on_line: int) -> list[str]:
        """The project's definitions this exact call resolves to."""
        edges = [e for e in self.ctx.graph.uses(owner)
                 if e.kind == "calls" and not e.guess and e.line == call.lineno]
        named = [e.dst for e in edges if _short(e.dst) == call_name(call)]
        if named:
            return list(dict.fromkeys(named))
        return list(dict.fromkeys(e.dst for e in edges)) if calls_on_line == 1 else []

    def target(self, name: str) -> str | None:
        """The function a call to ``name`` runs: itself, or a class's ``__init__``."""
        if name in self.fns:
            return name
        init = f"{name}.__init__"
        return init if init in self.fns else None

    def summary(self, name: str) -> Summary | None:
        if name in self.summaries:
            return self.summaries[name]
        if name in self.busy or len(self.busy) > 40:
            return None                     # recursion, or too deep: nothing known
        self.walk(name)
        return self.summaries.get(name)

    def walk(self, name: str) -> None:
        if name in self.walked or name not in self.fns:
            return
        path, fn, cls = self.fns[name]
        if is_test_path(path):
            self.walked.add(name)
            return
        self.busy.add(name)
        w = _Walk(self, name, path, fn, cls, self.file(path))
        w.block(fn.body)
        self.busy.discard(name)
        self.walked.add(name)
        self.summaries[name] = w.summary
        self.flows |= w.flows


class _Walk:
    """One forward pass over a function body, tracking which names carry outside input."""

    def __init__(self, an: _Analysis, name: str, path: str, fn, cls, f: _File) -> None:
        self.an, self.name, self.path, self.fn, self.f = an, name, path, fn, f
        self.env: dict[str, frozenset] = {}
        self.summary = Summary()
        self.flows: set[Flow] = set()
        self.depth = 0
        self.lines: dict[int, int] = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                self.lines[n.lineno] = self.lines.get(n.lineno, 0) + 1
        kind = self._handler()
        args = fn.args
        positional = [*args.posonlyargs, *args.args]
        method = cls is not None and positional[:1] and positional[0].arg in ("self", "cls")
        for i, a in enumerate(positional):
            if method and i == 0:
                continue
            if kind and a.arg not in _NOT_DATA and not self._typed(a):
                self.env[a.arg] = frozenset({Origin(src=Source(
                    kind, f"parameter {a.arg} of {_short(name)}()", name, path, fn.lineno))})
            elif not kind:
                self.env[a.arg] = frozenset({Origin(param=i)})
        for a in [*args.kwonlyargs, args.vararg, args.kwarg]:
            if a is not None and kind and a.arg not in _NOT_DATA and not self._typed(a):
                self.env[a.arg] = frozenset({Origin(src=Source(
                    kind, f"parameter {a.arg} of {_short(name)}()", name, path, fn.lineno))})

    def _handler(self) -> str:
        """``http-request`` for a web handler, ``cli`` for a click/typer command, else ""."""
        for d in self.fn.decorator_list:
            target = d.func if isinstance(d, ast.Call) else d
            tail = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if tail in _ROUTE_DECORATORS and self.f.web:
                return "http-request"
            if tail in _CLI_DECORATORS and self.f.cli:
                return "cli"
        params = [a.arg for a in [*self.fn.args.posonlyargs, *self.fn.args.args]]
        params = params[1:] if params[:1] in (["self"], ["cls"]) else params
        if params[:1] == ["request"] and self.f.web:
            return "http-request"                     # a Django view
        return ""

    @staticmethod
    def _typed(a: ast.arg) -> bool:
        return a.annotation is not None and ast.unparse(a.annotation) in _TYPED

    # -- values ---------------------------------------------------------------------------
    def _src(self, kind: str, desc: str, node: ast.AST) -> frozenset:
        return frozenset({Origin(src=Source(kind, desc, self.name, self.path,
                                            getattr(node, "lineno", 0)))})

    def T(self, e: ast.AST | None) -> frozenset:
        if e is None or isinstance(e, ast.Constant):
            return frozenset()
        if isinstance(e, ast.Name):
            return self.env.get(e.id, frozenset())
        if isinstance(e, ast.Call):
            return self.call(e)
        if isinstance(e, ast.Attribute):
            base = dotted(e)
            root = base.split(".")[0]
            if root in _REQUEST_NAMES and e.attr in _REQUEST_ATTRS and self.f.web:
                return self._src("http-request", base, e)
            if self.f.canonical(e) == "sys.argv":
                return self._src("cli", "sys.argv", e)
            if e.attr in _NETWORK_RESULT and isinstance(e.value, ast.Call) \
                    and self.f.canonical(e.value.func) in _NETWORK_CALLS:
                return self._src("network", f"the response of {dotted(e.value.func)}()", e)
            return self.T(e.value)
        if isinstance(e, ast.Subscript):
            if self.f.canonical(e.value) == "sys.argv":
                return self._src("cli", "sys.argv", e)
            return self.T(e.value) | self.T(e.slice)
        if isinstance(e, ast.Compare):
            return frozenset()
        if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            saved = dict(self.env)
            for gen in e.generators:
                taint = self.T(gen.iter)
                for n in ast.walk(gen.target):
                    if isinstance(n, ast.Name):
                        self.env[n.id] = taint
            out = self.T(e.value if isinstance(e, ast.DictComp) else e.elt)
            if isinstance(e, ast.DictComp):
                out |= self.T(e.key)
            self.env = saved
            return out
        if isinstance(e, ast.NamedExpr):
            taint = self.T(e.value)
            self.env[e.target.id] = taint
            return taint
        if isinstance(e, (ast.JoinedStr, ast.FormattedValue, ast.BinOp, ast.BoolOp, ast.IfExp,
                          ast.Tuple, ast.List, ast.Set, ast.Dict, ast.UnaryOp, ast.Await,
                          ast.Starred, ast.Slice)):
            out: frozenset = frozenset()
            for child in ast.iter_child_nodes(e):
                if isinstance(child, ast.expr):
                    out |= self.T(child)
            return out
        return frozenset()

    def _reach(self, taint: frozenset, hit: SinkHit) -> None:
        for o in taint:
            if hit.kind in o.cleared or ALL in o.cleared:
                continue
            if o.src is not None:
                self.flows.add(Flow(o.src, hit, self.name))
            elif o.param >= 0:
                self.summary.param_sinks.setdefault(o.param, {}).setdefault(
                    (hit.kind, hit.fn, hit.line), hit)

    def call(self, node: ast.Call) -> frozenset:
        func = node.func
        name = self.f.canonical(func)
        tail = name.rsplit(".", 1)[-1] if name else ""
        recv = self.T(func.value) if isinstance(func, ast.Attribute) else frozenset()
        args = [self.T(a) for a in node.args]
        kwargs = {k.arg: self.T(k.value) for k in node.keywords}
        every = recv.union(*args, *kwargs.values())

        # sources
        if name in ("input", "raw_input") and isinstance(func, ast.Name) \
                and func.id not in self.f.rebound:
            return self._src("stdin", f"{func.id}()", node)
        if name in ("sys.stdin.read", "sys.stdin.readline", "sys.stdin.readlines"):
            return self._src("stdin", name, node)
        if tail in ("recv", "recvfrom") and isinstance(func, ast.Attribute):
            return self._src("socket", f"{dotted(func) or tail}()", node)
        if tail == "parse_args" and isinstance(func, ast.Attribute):
            return self._src("cli", "the command line arguments", node)
        if tail in _NETWORK_RESULT and isinstance(func, ast.Attribute) \
                and isinstance(func.value, ast.Call) \
                and self.f.canonical(func.value.func) in _NETWORK_CALLS:
            return self._src("network", f"the response of {dotted(func.value.func)}()", node)

        targets = [t for t in (self.an.target(c) for c in
                               self.an.callees(self.name, node, self.lines.get(node.lineno, 0)))
                   if t]
        if not targets:
            self._sinks(node, name, args, kwargs)
        if name in _CLEAR_ALL or tail in _CLEAR_ALL or _VALIDATOR.match(tail or ""):
            return frozenset()
        cleared = _CLEAR_KINDS.get(name) or _CLEAR_KINDS.get(tail)

        result = every
        composed = False
        for t in targets:
            summ = self.an.summary(t)
            if summ is None:
                continue
            bound = self._bind(node, t, args, kwargs)
            hop = Hop(self.name, self.path, node.lineno, t)
            for idx, taint in bound.items():
                for hit in summ.param_sinks.get(idx, {}).values():
                    self._reach(taint, SinkHit(hit.kind, hit.call, hit.fn, hit.path, hit.line,
                                               (hop,) + hit.chain))
            if not composed:
                result, composed = frozenset(), True
            for idx in summ.param_returns:
                result |= bound.get(idx, frozenset())
            result |= frozenset(Origin(src=s) for s in summ.returns_source)
        if cleared:
            result = frozenset(o.clearing(cleared) for o in result)
        return result

    def _bind(self, call: ast.Call, target: str, args, kwargs) -> dict[int, frozenset]:
        _path, fn, cls = self.an.fns[target]
        positional = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args]]
        shift = 1 if cls is not None and positional[:1] in (["self"], ["cls"]) else 0
        out: dict[int, frozenset] = {}
        for i, taint in enumerate(args):
            if isinstance(call.args[i], ast.Starred):
                break
            if i + shift < len(positional):
                out[i + shift] = out.get(i + shift, frozenset()) | taint
        for k, taint in kwargs.items():
            if k in positional:
                idx = positional.index(k)
                out[idx] = out.get(idx, frozenset()) | taint
        return out

    def _sinks(self, node: ast.Call, name: str, args, kwargs) -> None:
        func = node.func
        tail = name.rsplit(".", 1)[-1] if name else ""
        bare = isinstance(func, ast.Name) and func.id not in self.f.rebound \
            and func.id not in self.f.aliases
        for spec in SINKS:
            hit = (name in spec.names or (tail in spec.tails and tail)
                   or (bare and func.id in spec.builtins)
                   or (isinstance(func, ast.Attribute) and func.attr in spec.methods))
            if not hit:
                continue
            if spec.kind == "deserialize" and name == "yaml.load":
                loader = next((ast.unparse(k.value) for k in node.keywords
                               if k.arg == "Loader"), "")
                if len(node.args) > 1:
                    loader = ast.unparse(node.args[1])
                if "Safe" in loader:
                    continue
            if spec.kind == "exec" and name.startswith("subprocess.") \
                    and name not in ("subprocess.getoutput", "subprocess.getstatusoutput"):
                shell = any(k.arg == "shell" and isinstance(k.value, ast.Constant)
                            and k.value.value for k in node.keywords)
                taint = args[0] if args else kwargs.get("args", frozenset())
                first = node.args[0] if node.args else None
                if not shell and isinstance(first, (ast.List, ast.Tuple)):
                    # an argument list is not re-read by a shell: only the program matters
                    taint = self.T(first.elts[0]) if first.elts else frozenset()
            elif spec.args == "first":
                taint = args[0] if args else kwargs.get("url", frozenset())
            else:
                taint = frozenset().union(*args[:2]) if args[:2] else frozenset()
                for k in ("file", "path", "src", "dst", "filename", "name"):
                    taint |= kwargs.get(k, frozenset())
            if taint:
                shown = dotted(func) or tail
                self._reach(taint, SinkHit(spec.kind, shown, self.name, self.path,
                                           node.lineno))

    # -- statements ---------------------------------------------------------------------------
    def validated(self, test: ast.expr) -> tuple[set[str], set[str]]:
        """``(names safe when the test is true, names safe when it is false)``."""
        yes: set[str] = set()
        no: set[str] = set()
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            t, f = self.validated(test.operand)
            return f, t
        if isinstance(test, ast.BoolOp):
            for v in test.values:
                t, f = self.validated(v)
                if isinstance(test.op, ast.And):
                    yes |= t
                else:
                    no |= f
            return yes, no
        if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) \
                and len(test.ops) == 1:
            op, comp = test.ops[0], test.comparators[0]
            fixed = isinstance(comp, (ast.Tuple, ast.List, ast.Set, ast.Constant)) or (
                isinstance(comp, ast.Name) and comp.id.isupper()) or (
                isinstance(comp, ast.Attribute) and comp.attr.isupper())
            if fixed and isinstance(op, (ast.In, ast.Eq, ast.Is)):
                yes.add(test.left.id)
            elif fixed and isinstance(op, (ast.NotIn, ast.NotEq, ast.IsNot)):
                no.add(test.left.id)
        if isinstance(test, ast.Call):
            f = test.func
            if isinstance(f, ast.Attribute) and f.attr in _VALID_METHODS \
                    and isinstance(f.value, ast.Name):
                yes.add(f.value.id)
            name = self.f.canonical(f)
            if name in ("re.fullmatch", "re.match") and len(test.args) >= 2 \
                    and isinstance(test.args[1], ast.Name):
                yes.add(test.args[1].id)
            last = call_name(test)
            if _VALIDATOR.match(last or "") or last.startswith("is_"):
                yes |= {a.id for a in test.args if isinstance(a, ast.Name)}
        return yes, no

    def _clear(self, names: set[str]) -> dict[str, frozenset]:
        saved = {n: self.env[n] for n in names if n in self.env}
        for n in saved:
            self.env[n] = frozenset()
        return saved

    def block(self, body: list[ast.stmt]) -> None:
        for st in body:
            self.stmt(st)

    def assign(self, target: ast.AST, taint: frozenset, strong: bool) -> None:
        if isinstance(target, ast.Name):
            self.env[target.id] = taint if strong else self.env.get(target.id, frozenset()) | taint
        elif isinstance(target, (ast.Tuple, ast.List)):
            for t in target.elts:
                self.assign(t, taint, strong)
        elif isinstance(target, ast.Starred):
            self.assign(target.value, taint, strong)
        elif isinstance(target, (ast.Subscript, ast.Attribute)):
            root = target
            while isinstance(root, (ast.Subscript, ast.Attribute)):
                root = root.value
            if isinstance(root, ast.Name):
                self.env[root.id] = self.env.get(root.id, frozenset()) | taint

    def stmt(self, st: ast.stmt) -> None:
        if isinstance(st, ast.Assign):
            taint = self.T(st.value)
            for t in st.targets:
                self.assign(t, taint, strong=self.depth == 0)
        elif isinstance(st, ast.AnnAssign):
            if st.value is not None:
                self.assign(st.target, self.T(st.value), strong=self.depth == 0)
        elif isinstance(st, ast.AugAssign):
            self.assign(st.target, self.T(st.value) | self.T(st.target), strong=False)
        elif isinstance(st, ast.Expr):
            self.T(st.value)
        elif isinstance(st, ast.Return):
            for o in self.T(st.value):
                if ALL in o.cleared:
                    continue
                if o.param >= 0:
                    self.summary.param_returns.add(o.param)
                elif o.src is not None:
                    self.summary.returns_source.add(o.src)
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            self.assign(st.target, self.T(st.iter), strong=False)
            self.depth += 1
            self.block(st.body)
            self.block(st.orelse)
            self.depth -= 1
        elif isinstance(st, ast.While):
            self.T(st.test)
            self.depth += 1
            self.block(st.body)
            self.block(st.orelse)
            self.depth -= 1
        elif isinstance(st, ast.If):
            self.T(st.test)
            yes, no = self.validated(st.test)
            self.depth += 1
            saved = self._clear(yes)
            self.block(st.body)
            self.env.update({k: v for k, v in saved.items() if not self.env.get(k)})
            saved = self._clear(no)
            self.block(st.orelse)
            self.env.update({k: v for k, v in saved.items() if not self.env.get(k)})
            self.depth -= 1
            if _exits(st.body) and no:              # if not valid(x): raise
                self._clear(no)
            if st.orelse and _exits(st.orelse) and yes:
                self._clear(yes)
        elif isinstance(st, ast.Assert):
            self._clear(self.validated(st.test)[0])
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            for item in st.items:
                taint = self.T(item.context_expr)
                if item.optional_vars is not None:
                    self.assign(item.optional_vars, taint, strong=self.depth == 0)
            self.block(st.body)
        elif isinstance(st, ast.Try):
            self.depth += 1
            self.block(st.body)
            for h in st.handlers:
                self.block(h.body)
            self.block(st.orelse)
            self.block(st.finalbody)
            self.depth -= 1
        elif isinstance(st, getattr(ast, "Match", ())):
            self.T(st.subject)
            self.depth += 1
            for case in st.cases:
                self.block(case.body)
            self.depth -= 1
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        else:
            for child in ast.iter_child_nodes(st):
                if isinstance(child, ast.expr):
                    self.T(child)


def _exits(body: list[ast.stmt]) -> bool:
    return bool(body) and isinstance(body[-1], (ast.Return, ast.Raise, ast.Continue, ast.Break))


def _candidates(ctx, touched: set[str], fns: dict) -> list[str]:
    """The edited functions and everything that calls them, directly or not: a flow through
    edited code is found in one of these."""
    seen = {n for n in touched if n in fns}
    todo = list(seen)
    while todo and len(seen) < 3000:
        for e in ctx.graph.callers(todo.pop()):
            if e.kind == "calls" and not e.guess and e.src not in seen and e.src in fns:
                seen.add(e.src)
                todo.append(e.src)
    return sorted(seen)


def _severity(sink: str, source: str) -> str | None:
    sev = _SEVERITY[sink]
    if source in _LOCAL_KINDS:
        if sink not in _LOCAL_SINKS:
            return None                     # a local tool doing what its user asked
        sev = {"critical": "high", "high": "medium"}.get(sev, "low")
    return sev


_RANK = ("critical", "high", "medium", "low")
_KIND_WORDS = {"http-request": "request data", "network": "data from the network",
               "socket": "data from a socket", "cli": "command-line input",
               "stdin": "typed input"}
_WHO = {"http-request": "anyone who can send a request", "network": "whoever controls that "
        "server", "socket": "whoever is on the other end", "cli": "whoever runs it (or a "
        "script that passes it data)", "stdin": "whoever types or pipes the input"}


def _route(fl: Flow, path: str, line: int) -> str:
    src, sink = fl.source, fl.sink
    steps = [f"{_short(src.fn)}() reads {src.desc} ({src.path}:{src.line})"]
    steps += [f"{_short(h.fn)}() hands it to {_short(h.callee)}() ({h.path}:{h.line})"
              for h in sink.chain]
    steps.append(f"{_short(sink.fn)}() passes it to {sink.call}() ({path}:{line})")
    return ", then ".join(steps)


@change_rule("unvalidated-input-reaches-sink", "critical",
             fix="Validate the value before it reaches the call, or use the safe form of the "
                 "call.")
def unvalidated_input_reaches_sink(ctx):
    if not any(p.endswith(".py") for p in ctx.after.files):
        return
    an = _Analysis(ctx)
    touched = {n for n in edited(ctx) if n in an.fns}
    if not touched:
        return
    for name in _candidates(ctx, touched, an.fns):
        an.walk(name)

    groups: dict[tuple, list[Flow]] = {}
    for fl in an.flows:
        involved = {fl.source.fn, fl.via, fl.sink.fn, *(h.fn for h in fl.sink.chain)}
        if not involved & touched:
            continue
        if _severity(fl.sink.kind, fl.source.kind) is None:
            continue
        groups.setdefault((fl.sink.path, fl.sink.line, fl.sink.kind), []).append(fl)

    for (path, line, kind), flows in sorted(groups.items())[:20]:
        flows.sort(key=lambda f: (_RANK.index(_severity(kind, f.source.kind)),
                                  f.source.path, f.source.line, f.source.desc))
        first = flows[0]
        sev = _severity(kind, first.source.kind)
        spec = next(s for s in SINKS if s.kind == kind)
        others = sorted({f.source.desc for f in flows[1:]} - {first.source.desc})
        words = _KIND_WORDS.get(first.source.kind, "outside input")
        yield Finding(
            "unvalidated-input-reaches-sink", sev,
            f"{words} ({first.source.desc}) reaches {first.sink.call}() in "
            f"{_short(first.sink.fn)}() unchecked: it {spec.does}",
            path, line,
            detail=(f"{_route(first, path, line)}. Nothing on the way validates or escapes "
                    f"it, so {_WHO.get(first.source.kind, 'whoever controls the input')} "
                    f"decides what {first.sink.call}() gets."
                    + (f" Also reached from {', '.join(others[:3])}." if others else "")),
            fix=_FIX[kind])
