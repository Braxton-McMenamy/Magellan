"""The local server: the website, plus a JSON API that runs Magellan Lite for real.

    magellan-lite serve                 # http://localhost:8000, this computer only
    magellan-lite serve --open          # and open it in the browser

    GET  /                  the website (site/)
    GET  /api/health        {"ok": true, "version": ...}
    GET  /api/rules         the checklist rules
    GET  /api/incidents     the famous failures, replayed now (?refresh=1 to re-run)
    POST /api/check         {"before": {path: source}, "after": {path: source}} -> a report
                            and its map (magellan_lite/web.py)

Standard library only. By default it listens on this computer alone (127.0.0.1). Code sent to
/api/check is only parsed, never run; requests are capped in size and file count. API
responses allow any origin, so the website also works when index.html is opened from disk.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse

from magellan_lite import __version__

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
MAX_BODY = 1_000_000            # bytes in one /api/check request
MAX_FILES = 200                 # files per side


class BadRequest(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _files(value, side: str) -> dict[str, str]:
    """``{relative/path.py: source}``, checked: plain relative .py paths, text sources."""
    if not isinstance(value, dict):
        raise BadRequest(f'"{side}" must be an object of {{"path.py": "source"}}')
    if len(value) > MAX_FILES:
        raise BadRequest(f'"{side}" has {len(value)} files; the limit is {MAX_FILES}')
    out: dict[str, str] = {}
    for path, source in value.items():
        p = PurePosixPath(str(path).replace("\\", "/"))
        if (not isinstance(source, str) or p.is_absolute() or ".." in p.parts
                or not p.name.endswith(".py") or ":" in str(p)):
            raise BadRequest(f'"{side}": {path!r} must be a relative .py path with text source')
        out[str(p)] = source
    return out


def check_request(body: bytes) -> dict:
    """The JSON report for one /api/check request body, with the map the website draws."""
    from magellan_lite.web import check_with_map
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadRequest(f"the body is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise BadRequest('send {"before": {...}, "after": {...}}')
    return check_with_map(_files(data.get("before", {}), "before"),
                          _files(data.get("after", {}), "after"))


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
    server_version = f"magellan-lite/{__version__}"

    # -- API ------------------------------------------------------------------------------
    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:                       # the browser's CORS preflight
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return super().do_GET()                     # the website
        try:
            if url.path == "/api/health":
                return self._json(200, {"ok": True, "version": __version__})
            if url.path == "/api/rules":
                import magellan_lite.rules  # noqa: F401
                from magellan_lite.findings import RULES
                return self._json(200, [
                    {"id": r.id, "severity": r.severity, "blocking": r.blocking,
                     "kind": "file" if r.per_file else "change", "fix": r.fix}
                    for r in sorted(RULES.values(), key=lambda r: r.id)])
            if url.path == "/api/incidents":
                refresh = parse_qs(url.query).get("refresh", ["0"])[0] not in ("0", "")
                return self._json(200, self.incidents.get(refresh))
            return self._json(404, {"error": f"no such endpoint: {url.path}"})
        except Exception as exc:                        # noqa: BLE001 - a JSON error, not a hang
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/check":
            return self._json(404, {"error": f"no such endpoint: {self.path}"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._discard(length)
                raise BadRequest(f"the request is {length} bytes; the limit is {MAX_BODY}", 413)
            return self._json(200, check_request(self.rfile.read(length)))
        except BadRequest as exc:
            return self._json(exc.status, {"error": str(exc)})
        except Exception as exc:                        # noqa: BLE001
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

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

    def log_message(self, fmt: str, *args) -> None:     # quieter than the default
        if not self.path.startswith("/api/health"):
            super().log_message(fmt, *args)


def make_server(host: str = "127.0.0.1", port: int = 8000,
                site: Path = SITE_DIR) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), partial(Handler, directory=str(site)))
    httpd.daemon_threads = True
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False) -> int:
    httpd = make_server(host, port)
    url = f"http://{'localhost' if host in ('127.0.0.1', 'localhost') else host}:" \
          f"{httpd.server_address[1]}/"
    print(f"magellan-lite: serving {SITE_DIR.name}/ and the API on {url}  (Ctrl+C to stop)",
          flush=True)
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
