"""Systems-design labelling: the vocabulary the UI colours and filters by.

Two orthogonal axes are attached to every node.

``layer`` -- where the node sits in the system's architecture.
``data_role`` -- what the node does to data as it flows through.

Both are inferred heuristically and every label carries ``evidence`` strings, so
the UI can always answer "why is this tagged data_access?" and a human can
override it. Nothing in the propagation engine depends on these labels being
right; they are for reading the map.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- layers, ordered outside-in -----------------------------------------
LAYERS: dict[str, str] = {
    "entrypoint": "Process starts here: __main__, CLI, worker loop, scheduled job.",
    "interface": "Public surface others call: HTTP routes, RPC handlers, SDK, CLI verbs.",
    "orchestration": "Coordinates other units; sequencing and transactions, little logic of its own.",
    "domain": "Business rules and computation. The part that is worth testing.",
    "state": "Holds mutable state that outlives a single call: caches, registries, pools.",
    "data_access": "Talks to a datastore or another service: SQL, ORM, HTTP client, queue.",
    "io_boundary": "Touches the outside world directly: files, sockets, subprocesses, env.",
    "config": "Constants, settings, feature flags.",
    "util": "Leaf helpers with no dependencies of their own.",
    "test": "Test code. Impact here is a signal, not a hazard.",
    "unknown": "Not enough evidence to label.",
}

#: Blast radius through these layers is a bigger deal -- used to rank findings.
LAYER_CRITICALITY: dict[str, float] = {
    "entrypoint": 1.0,
    "interface": 1.0,
    "state": 0.95,
    "data_access": 0.9,
    "io_boundary": 0.9,
    "orchestration": 0.75,
    "domain": 0.7,
    "config": 0.6,
    "util": 0.4,
    "unknown": 0.5,
    "test": 0.2,
}

# --- data roles ----------------------------------------------------------
DATA_ROLES: dict[str, str] = {
    "source": "Brings data in from outside the process.",
    "sink": "Sends data out of the process.",
    "transformer": "Pure-ish: arguments in, value out, no state touched.",
    "consumer": "Reads state or arguments and causes effects, returns nothing useful.",
    "store": "Is the state: a container, cache, registry, or connection pool.",
    "accessor": "Reads or writes a store on someone else's behalf.",
    "control": "Dispatch and sequencing; moves no data itself.",
    "unknown": "Not enough evidence to label.",
}

_DATA_ACCESS_MODULES = {
    "sqlite3", "sqlalchemy", "psycopg", "psycopg2", "pymysql", "asyncpg", "redis",
    "pymongo", "boto3", "requests", "httpx", "urllib", "urllib3", "aiohttp",
    "kafka", "pika", "elasticsearch", "grpc",
}
#: Modules whose import is itself evidence of I/O. `os`, `io`, `pathlib`, `threading`
#: and `asyncio` used to be here; nearly every module imports one of them (for
#: `os.path`, `os.environ`, a Lock), which labelled 77% of click `io_boundary`. Actual
#: I/O is detected from calls instead (see collect._scan_body_signals).
_IO_MODULES = {
    "subprocess", "socket", "shutil", "mmap", "ctypes", "select", "fcntl", "signal",
}
_CONFIG_MODULES = {"configparser", "dotenv", "tomllib", "argparse"}

_NAME_HINTS: list[tuple[frozenset[str], str]] = [
    (frozenset({"test", "tests", "conftest"}), "test"),
    (frozenset({"main", "__main__", "cli", "app", "entry", "manage", "run", "worker", "daemon"}), "entrypoint"),
    (frozenset({"api", "routes", "router", "views", "handlers", "endpoints", "controllers", "rpc", "serializers"}), "interface"),
    (frozenset({"service", "services", "usecase", "usecases", "workflow", "orchestrator", "pipeline", "coordinator", "manager"}), "orchestration"),
    (frozenset({"repository", "repositories", "dao", "db", "database", "store", "storage", "client", "gateway", "adapter", "queries", "models"}), "data_access"),
    (frozenset({"cache", "registry", "pool", "session", "state"}), "state"),
    (frozenset({"config", "settings", "constants", "conf", "env", "flags"}), "config"),
    (frozenset({"util", "utils", "helpers", "common", "shared", "lib", "tools", "misc"}), "util"),
    (frozenset({"domain", "core", "logic", "rules", "model", "entities"}), "domain"),
]

_INTERFACE_DECORATORS = {
    "route", "get", "post", "put", "patch", "delete", "websocket", "app", "api",
    "command", "group", "task", "endpoint", "rpc", "handler", "listener", "subscribe",
}

_MUTABLE_LITERALS = ("list", "dict", "set", "deque", "defaultdict", "OrderedDict", "Counter", "WeakValueDictionary")


@dataclass
class Signals:
    """Everything the collector noticed about one scope, fed to the labeller."""

    module_parts: tuple[str, ...] = ()
    path_parts: tuple[str, ...] = ()
    imported_roots: frozenset[str] = frozenset()
    decorators: tuple[str, ...] = ()
    has_main_guard: bool = False
    calls_open: bool = False
    calls_exit: bool = False
    returns_value: bool = False
    has_params: bool = False
    writes_global: bool = False
    reads_global: bool = False
    mutates_state: bool = False
    is_mutable_container: bool = False
    is_constant: bool = False
    call_count: int = 0
    own_statement_count: int = 0
    dunder_all: bool = False


def _name_layer(parts: tuple[str, ...]) -> tuple[str | None, str]:
    for part in reversed(parts):
        low = part.lower().removesuffix(".py").strip("_")
        for names, layer in _NAME_HINTS:
            if low in names:
                return layer, f"name '{part}' reads as {layer}"
    for part in parts:
        low = part.lower().removesuffix(".py")
        for names, layer in _NAME_HINTS:
            if any(low.startswith(n + "_") or low.endswith("_" + n) for n in names):
                return layer, f"name '{part}' reads as {layer}"
    return None, ""


def _hint_parts(sig: Signals) -> tuple[str, ...]:
    """The name parts that say something about a module's role.

    Not the root package: `app/`, `my_app/` or `core/` is the project's name, not a
    statement that every module under it is an entrypoint (or domain logic). A
    top-level single-file module keeps its own name.
    """
    mods = sig.module_parts
    return tuple(mods[1:]) if len(mods) > 1 else tuple(mods)


def label_module(sig: Signals) -> tuple[str, list[str]]:
    ev: list[str] = []
    layer, why = _name_layer(_hint_parts(sig))
    if layer:
        ev.append(why)
    if any(p.lower().removesuffix(".py") in ("test", "tests", "conftest")
           or p.lower().startswith("test_") for p in sig.path_parts):
        return "test", ["lives under a test path"]
    if sig.has_main_guard:
        ev.append("has a __name__ == '__main__' guard")
        layer = "entrypoint"
    hit = sig.imported_roots & _DATA_ACCESS_MODULES
    if hit and layer in (None, "domain", "util", "unknown", "orchestration"):
        layer = "data_access"
        ev.append(f"imports {', '.join(sorted(hit))}")
    hit = sig.imported_roots & _IO_MODULES
    if hit and layer in (None, "domain", "util", "unknown"):
        layer = "io_boundary"
        ev.append(f"imports {', '.join(sorted(hit))}")
    hit = sig.imported_roots & _CONFIG_MODULES
    if hit and layer is None:
        layer = "config"
        ev.append(f"imports {', '.join(sorted(hit))}")
    if any(d.split(".")[-1] in _INTERFACE_DECORATORS for d in sig.decorators):
        layer = "interface"
        ev.append("declares routed/registered handlers")
    if layer is None:
        layer = "domain" if sig.own_statement_count else "unknown"
        ev.append("no stronger signal; defaulted")
    return layer, ev


def label_callable(sig: Signals, module_layer: str) -> tuple[str, str, list[str]]:
    """Return ``(layer, data_role, evidence)`` for a function or method."""
    ev: list[str] = []
    layer = module_layer
    named, why = _name_layer((sig.module_parts[-1:] if sig.module_parts else ()) + ())
    dec_tail = {d.split(".")[-1].lower() for d in sig.decorators}
    if dec_tail & _INTERFACE_DECORATORS:
        layer = "interface"
        ev.append(f"decorated with {', '.join(sorted(dec_tail & _INTERFACE_DECORATORS))}")
    if sig.calls_open or (sig.imported_roots & _IO_MODULES):
        if layer in ("domain", "util", "unknown", "orchestration"):
            layer = "io_boundary"
            ev.append("performs direct I/O")
    if sig.calls_exit:
        layer = "entrypoint"
        ev.append("exits the process")

    # data role
    if sig.mutates_state or sig.writes_global:
        role = "accessor" if sig.returns_value else "consumer"
        ev.append("writes or mutates state outside its own scope")
    elif sig.returns_value and sig.has_params and not sig.reads_global:
        role = "transformer"
        ev.append("arguments in, value out, no outer state read")
    elif sig.returns_value and (layer in ("data_access", "io_boundary")):
        role = "source"
        ev.append("returns data obtained from outside")
    elif sig.returns_value:
        role = "transformer"
        ev.append("returns a value")
    elif layer in ("data_access", "io_boundary"):
        role = "sink"
        ev.append("sends data outward, returns nothing")
    elif sig.call_count and not sig.own_statement_count:
        role = "control"
        ev.append("only delegates to other callables")
    else:
        role = "consumer"
        ev.append("effects only, no return value")
    if named:
        ev.append(why)
    return layer, role, ev


def label_variable(sig: Signals, module_layer: str) -> tuple[str, str, list[str]]:
    ev: list[str] = []
    if sig.is_constant:
        ev.append("bound once to a literal constant")
        return ("config" if module_layer != "test" else "test"), "store", ev
    if sig.is_mutable_container:
        ev.append("module-level mutable container: state that outlives every call")
        return "state", "store", ev
    return module_layer, "store", ev


def is_mutable_container_expr(text: str) -> bool:
    t = text.strip()
    if t.startswith(("[", "{")):
        return True
    head = t.split("(")[0].split(".")[-1]
    return head in _MUTABLE_LITERALS
