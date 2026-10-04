"""The server: the website, plus a JSON API that runs Magellan Lite for real.

    magellan-lite serve                 # http://localhost:8000, this computer only
    magellan-lite serve --open          # and open it in the browser
    magellan-lite serve --public-host NAME --behind-proxy
                                        # behind an HTTPS proxy (Tailscale Funnel) on NAME

    GET  /                  the website (site/)
    GET  /api/health        {"ok": true}
    GET  /api/rules         the checklist rules
    GET  /api/incidents     the famous failures, replayed (refreshed only from this computer)
    POST /api/check         {"before": {path: source}, "after": {path: source}} -> a report
                            and its map (magellan_lite/web.py)

Standard library only. It may face the internet, so it defends itself:

- uploaded code is only ever parsed, never run, and each check runs in its own isolated Python
  process (``-I``) with a hard time limit, only a few at a time: a pathological upload costs a
  few seconds of one process, never the server;
- requests are capped (1 MB, 200 files a side, short relative ``.py`` paths), a client that is
  slow to send its request is cut off, the server holds a bounded number of connections and
  turns the rest away, and every client is rate-limited;
- browsers may call the API only from the origins on an allowlist (CORS), and only the expected
  host names are answered (against DNS rebinding);
- every response carries security headers: a content security policy, no sniffing, no framing,
  no referrer, HSTS;
- errors explain what was wrong with the request, never what went wrong inside (details go to
  the log); there are no directory listings and no version banners.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from collections import defaultdict, deque
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
PACKAGE_PARENT = Path(__file__).resolve().parent.parent      # what makes magellan_lite importable
MAX_BODY = 1_000_000            # bytes in one /api/check request
MAX_FILES = 200                 # files per side
MAX_PATH = 240                  # characters in one file path
DEFAULT_ORIGINS = ("https://magellan-code.pages.dev",)
LOOPBACK = ("127.0.0.1", "::1")


@dataclass
class Policy:
    """How careful the server is. The defaults suit the internet; tests loosen them."""
    hosts: frozenset | None = None          # Host names answered (None: any)
    origins: frozenset = frozenset(DEFAULT_ORIGINS)   # browser origins allowed to call the API
    behind_proxy: bool = False              # trust X-Forwarded-For from a proxy on this machine
    socket_timeout: float = 15.0            # seconds a client gets to send its request
    connections: int = 64                   # requests handled at once; more get 503
    checks_at_once: int = 2                 # checks running at once, each its own process
    check_seconds: float = 20.0             # a check that runs longer is stopped
    queue_seconds: float = 10.0             # how long a check may wait for a free slot
    rates: dict = field(default_factory=lambda: {"check": (20, 60), "any": (300, 60)})


class BadRequest(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


_PATH_OK = re.compile(r"[A-Za-z0-9_./ \-]+")


def _files(value, side: str) -> dict[str, str]:
    """``{relative/path.py: source}``, checked: short, plain, relative .py paths; text sources."""
    if not isinstance(value, dict):
        raise BadRequest(f'"{side}" must be an object of {{"path.py": "source"}}')
    if len(value) > MAX_FILES:
        raise BadRequest(f'"{side}" has {len(value)} files; the limit is {MAX_FILES}')
    out: dict[str, str] = {}
    for path, source in value.items():
        raw = str(path).replace("\\", "/")
        p = PurePosixPath(raw)
        if (not isinstance(source, str) or len(raw) > MAX_PATH or not _PATH_OK.fullmatch(raw)
                or p.is_absolute() or ".." in p.parts or not p.name.endswith(".py")):
            raise BadRequest(f'"{side}": each name must be a short relative .py path '
                             f'(letters, digits, _ - . /) with text source')
        out[str(p)] = source
    return out


def parse_check(body: bytes) -> tuple[dict, dict]:
    """The two versions in one /api/check request body, validated."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise BadRequest("the body is not JSON") from None
    if not isinstance(data, dict):
        raise BadRequest('send {"before": {...}, "after": {...}}')
    return _files(data.get("before", {}), "before"), _files(data.get("after", {}), "after")


# -- checks run in a separate process ----------------------------------------------------------
_WORKER = "\n".join([
    "import json, sys",
    "sys.path.insert(0, sys.argv[1])",
    "from magellan_lite.web import check_with_map",
    "data = json.load(sys.stdin)",
    "json.dump(check_with_map(data['before'], data['after']), sys.stdout)",
])


class CheckFailed(RuntimeError):
    """The check could not finish: too slow, too busy, or it crashed (details logged)."""
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class Checker:
    """Runs checks in isolated child processes (``python -I``), a few at a time, each with a
    hard time limit after which it is killed."""

    def __init__(self, policy: Policy, log=print) -> None:
        self.policy = policy
        self.slots = threading.BoundedSemaphore(policy.checks_at_once)
        self.log = log

    def check(self, before: dict, after: dict) -> dict:
        if not self.slots.acquire(timeout=self.policy.queue_seconds):
            raise CheckFailed("the server is busy checking other changes: try again shortly", 503)
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)      # no console window on Windows
            r = subprocess.run([sys.executable, "-I", "-c", _WORKER, str(PACKAGE_PARENT)],
                               input=json.dumps({"before": before, "after": after}).encode("utf-8"),
                               capture_output=True, timeout=self.policy.check_seconds,
                               creationflags=flags)
        except subprocess.TimeoutExpired:
            raise CheckFailed(f"the check took longer than {self.policy.check_seconds:g} s "
                              "and was stopped", 503) from None
        finally:
            self.slots.release()
        if r.returncode != 0:
            ref = secrets.token_hex(4)
            self.log(f"check {ref} failed: {r.stderr.decode('utf-8', 'replace')[-2000:]}")
            raise CheckFailed(f"the check failed (reference {ref})", 500)
        return json.loads(r.stdout.decode("utf-8"))


# -- per-client rate limits ------------------------------------------------------------------------
class RateLimiter:
    """At most N requests per window per client and bucket (a sliding window)."""

    def __init__(self, rates: dict) -> None:
        self.rates = rates
        self.hits: dict = defaultdict(deque)
        self.lock = threading.Lock()

    def wait(self, client: str, bucket: str) -> float:
        """0 if this request may go ahead (and it is counted), else seconds to wait."""
        limit, window = self.rates[bucket]
        now = time.monotonic()
        with self.lock:
            q = self.hits[(client, bucket)]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return max(1.0, q[0] + window - now)
            q.append(now)
            if len(self.hits) > 50_000:                  # forget idle clients
                for k in [k for k, v in self.hits.items() if not v]:
                    del self.hits[k]
            return 0.0


# -- security headers ----------------------------------------------------------------------------
def _inline_script_hashes(site: Path) -> list[str]:
    """CSP hashes for the inline scripts of the site's pages, so nothing else inline runs."""
    out = []
    for page in site.glob("*.html"):
        for body in re.findall(r"<script>(.*?)</script>", page.read_text("utf-8", "replace"), re.S):
            out.append("'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'")
    return sorted(set(out))


def _csp(site: Path) -> str:
    return "; ".join([
        "default-src 'self'",
        "script-src 'self' " + " ".join(_inline_script_hashes(site)),
        "style-src 'self' 'unsafe-inline'",           # style="--heat: .8" on map nodes
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'none'",
    ])


class _Incidents:
    """The replayed incidents, computed once and kept (they only change with the code)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: list | None = None

    def get(self, refresh: bool = False) -> list:
        from magellan_lite.incidents import replay_all
        with self._lock:
            if self._data is None or refresh:
                self._data = replay_all()
            return self._data


class Handler(SimpleHTTPRequestHandler):
    incidents = _Incidents()
    server_version = "magellan-lite"       # no version banner
    sys_version = ""                       # and no Python version

    @property
    def policy(self) -> Policy:
        return self.server.policy

    def setup(self) -> None:
        self.timeout = self.policy.socket_timeout          # a slow sender is cut off
        super().setup()

    # -- who is asking, and from where --------------------------------------------------------
    def client(self) -> str:
        """The client's address. Behind a proxy on this machine, the address the proxy
        appended last (an attacker can forge earlier entries, not that one)."""
        peer = self.client_address[0]
        if self.policy.behind_proxy and peer in LOOPBACK:
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded.strip():
                return forwarded.split(",")[-1].strip()[:64]
        return peer

    def _host_ok(self) -> bool:
        hosts = self.policy.hosts
        if hosts is None:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        name = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.split(":")[0]
        return name in hosts

    def _origin(self) -> str | None:
        """The request's Origin, if it is one the API answers."""
        origin = self.headers.get("Origin")
        return origin if origin and origin in self.policy.origins else None

    # -- responses ------------------------------------------------------------------------------
    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("Content-Security-Policy", self.server.csp)
        super().end_headers()

    def _json(self, status: int, payload, extra: dict | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = self._origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _refuse(self, status: int, message: str, extra: dict | None = None) -> None:
        if self.path.startswith("/api/"):
            self._json(status, {"error": message}, extra)
        else:
            self.send_error(status, message)

    def _gate(self, bucket: str = "any") -> bool:
        """Host and rate checks shared by every method. False: already answered."""
        if not self._host_ok():
            self._refuse(421, "this server does not answer for that host name")
            return False
        wait = self.server.limiter.wait(self.client(), bucket)
        if wait:
            self._refuse(429, "too many requests: slow down", {"Retry-After": str(int(wait + 0.999))})
            return False
        return True

    # -- methods --------------------------------------------------------------------------------
    def do_OPTIONS(self) -> None:                       # the browser's CORS preflight
        if not self._gate():
            return
        self.send_response(204)
        origin = self._origin()
        if origin:                                      # anyone else gets no permission
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _static(self, send) -> None:
        """Serve a file of the website, refusing names no real file has: a NUL or another
        control character makes the file system raise instead of answering."""
        name = unquote(urlparse(self.path).path)
        if any(ord(ch) < 32 or ch == "\x7f" for ch in name):
            return self.send_error(400, "Bad request")
        try:
            send()
        except (ValueError, OSError):                   # never a crashed handler, never a 502
            self.send_error(404, "Not found")

    def do_HEAD(self) -> None:
        if not self._gate():
            return
        if self.path.startswith("/api/"):
            return self._refuse(405, "use GET")
        self._static(super().do_HEAD)

    def do_GET(self) -> None:
        if not self._gate():
            return
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self._static(super().do_GET)         # the website
        try:
            if url.path == "/api/health":
                return self._json(200, {"ok": True})
            if url.path == "/api/rules":
                import magellan_lite.rules  # noqa: F401
                from magellan_lite.findings import RULES
                return self._json(200, [
                    {"id": r.id, "severity": r.severity, "blocking": r.blocking,
                     "kind": "file" if r.per_file else "change", "fix": r.fix}
                    for r in sorted(RULES.values(), key=lambda r: r.id)])
            if url.path == "/api/incidents":
                # re-running every replay is expensive: only from this computer
                refresh = (parse_qs(url.query).get("refresh", ["0"])[0] not in ("0", "")
                           and self.client() in LOOPBACK and self.client_address[0] in LOOPBACK
                           and not self.headers.get("X-Forwarded-For"))
                return self._json(200, self.incidents.get(refresh))
            return self._json(404, {"error": "no such endpoint"})
        except Exception as exc:                        # noqa: BLE001 - logged, not echoed
            return self._internal(exc)

    def do_POST(self) -> None:
        if not self._gate("check"):
            return
        if urlparse(self.path).path != "/api/check":
            return self._json(404, {"error": "no such endpoint"})
        try:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise BadRequest("send the request as application/json", 415)
            raw = self.headers.get("Content-Length")
            if raw is None or not raw.isdigit():
                raise BadRequest("the request needs a Content-Length", 411 if raw is None else 400)
            length = int(raw)
            if length > MAX_BODY:
                if length <= 16 * MAX_BODY:             # worth reading, so the 413 arrives
                    self._discard(length)
                self.close_connection = True
                raise BadRequest(f"the request is {length} bytes; the limit is {MAX_BODY}", 413)
            before, after = parse_check(self.rfile.read(length))
            return self._json(200, self.server.checker.check(before, after))
        except BadRequest as exc:
            return self._json(exc.status, {"error": str(exc)})
        except CheckFailed as exc:
            return self._json(exc.status, {"error": str(exc)})
        except Exception as exc:                        # noqa: BLE001
            return self._internal(exc)

    def _internal(self, exc: Exception) -> None:
        ref = secrets.token_hex(4)
        self.log_error("internal error %s: %s: %s", ref, type(exc).__name__, exc)
        return self._json(500, {"error": f"something went wrong (reference {ref})"})

    def _discard(self, length: int) -> None:
        """Read and drop an oversized body, so the client gets the 413 rather than a
        connection reset mid-upload (Windows resets when unread data is left behind).
        Bodies past 16x the limit are not worth reading: those clients just see a reset."""
        remaining = min(length, 16 * MAX_BODY)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def list_directory(self, path):                     # no directory listings
        self.send_error(404, "Not found")
        return None

    def log_message(self, fmt: str, *args) -> None:     # who, not what: never bodies
        if not self.path.startswith("/api/health"):
            sys.stderr.write(f"{self.client()} [{self.log_date_time_string()}] {fmt % args}\n")


class Server(ThreadingHTTPServer):
    """A threading server that holds at most ``policy.connections`` requests at once and turns
    the rest away at once with 503, instead of starting a thread per connection forever."""
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, handler, policy: Policy, site: Path) -> None:
        self.policy = policy
        self.limiter = RateLimiter(policy.rates)
        self.checker = Checker(policy, log=lambda m: sys.stderr.write(m + "\n"))
        self.csp = _csp(site)
        self._slots = threading.BoundedSemaphore(policy.connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                # take what the client already sent (briefly: this runs on the accept loop),
                # so closing doesn't turn the 503 into a connection reset on Windows
                request.settimeout(0.2)
                request.recv(65536)
            except OSError:
                pass
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nRetry-After: 5\r\n"
                                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(host: str = "127.0.0.1", port: int = 8000, site: Path = SITE_DIR,
                policy: Policy | None = None, public_hosts=(), origins=None,
                behind_proxy: bool = False) -> Server:
    """The server, with its policy. Host names answered: this computer's, plus ``public_hosts``
    (the proxy's name); any, when listening on every interface with no public name given."""
    if policy is None:
        hosts = None if host in ("0.0.0.0", "::") and not public_hosts else frozenset(
            {"localhost", "127.0.0.1", "::1", host.lower()} | {h.lower() for h in public_hosts})
        policy = Policy(hosts=hosts, behind_proxy=behind_proxy,
                        origins=frozenset(origins if origins is not None else DEFAULT_ORIGINS)
                        | {f"https://{h}" for h in public_hosts})
    return Server((host, port), partial(Handler, directory=str(site)), policy, site)


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False,
          public_hosts=(), origins=None, behind_proxy: bool = False) -> int:
    httpd = make_server(host, port, public_hosts=public_hosts, origins=origins,
                        behind_proxy=behind_proxy)
    url = f"http://{'localhost' if host in ('127.0.0.1', 'localhost') else host}:" \
          f"{httpd.server_address[1]}/"
    print(f"magellan-lite: serving {SITE_DIR.name}/ and the API on {url}  (Ctrl+C to stop)",
          flush=True)
    for h in public_hosts:
        print(f"  and as https://{h}/ through the proxy", flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print("  listening beyond this computer: anyone on the network can send code to check",
              flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nmagellan-lite: stopped")
    finally:
        httpd.server_close()
    return 0
