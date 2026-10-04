"""The server: the website, the JSON API, what it refuses, and how it defends itself."""

import http.client
import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request

from magellan_lite.server import MAX_BODY, MAX_FILES, Policy, make_server

BEFORE = {"sensor/__init__.py": "",
          "sensor/channel.py": "def parse(fields):\n    return fields\n",
          "sensor/collector.py": ("from sensor.channel import parse\n\n\n"
                                  "def collect(rows):\n    return [parse(r) for r in rows]\n")}
AFTER = {**BEFORE, "sensor/channel.py": "def parse(fields, layout):\n    return fields\n"}
SITE = "https://magellan-code.pages.dev"


class Running:
    """A server on a free port, in a thread, for one test class."""

    def __init__(self, **kw) -> None:
        self.httpd = make_server("127.0.0.1", 0, **kw)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, path, body=None, method=None, headers=None):
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def raw(self, method, path, headers, body=b""):
        """A request exactly as written: for headers urllib would fix up."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders(body or None)
        r = c.getresponse()
        out = (r.status, r.headers, r.read())
        c.close()
        return out


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Running()

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def request(self, *a, **kw):
        return self.s.request(*a, **kw)

    def test_health_and_rules(self):
        status, _h, body = self.request("/api/health")
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))     # no version
        rules = {r["id"]: r for r in json.loads(self.request("/api/rules")[2])}
        self.assertTrue(rules["signature-break"]["blocking"])

    def test_check_finds_the_broken_caller_and_the_blast_radius(self):
        status, _h, body = self.request("/api/check", {"before": BEFORE, "after": AFTER})
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertEqual(report["verdict"], "block")
        self.assertEqual([(f["rule"], f["path"]) for f in report["findings"]],
                         [("signature-break", "sensor/collector.py")])
        self.assertEqual([a["name"] for a in report["affected"]], ["sensor.collector.collect"])
        nodes = {n["id"]: n for n in report["map"]["nodes"]}       # the map the website draws
        self.assertEqual(nodes["sensor.channel.parse"]["change"], "signature")
        self.assertTrue(nodes["sensor.collector.collect"]["finding"])

    def test_windows_style_paths_are_accepted(self):
        before = {k.replace("/", "\\"): v for k, v in BEFORE.items()}
        after = {k.replace("/", "\\"): v for k, v in AFTER.items()}
        status, _h, body = self.request("/api/check", {"before": before, "after": after})
        self.assertEqual((status, json.loads(body)["verdict"]), (200, "block"))

    def test_bad_requests_get_a_clear_error(self):
        for body, why in [(b"not json", "not JSON"), ({"before": [1]}, "must be an object"),
                          ({"after": {"../evil.py": "x"}}, "relative .py path"),
                          ({"after": {"C:/x.py": "x"}}, "relative .py path"),
                          ({"after": {"notes.txt": "x"}}, "relative .py path"),
                          ({"after": {"a;rm -rf.py": "x"}}, "relative .py path"),
                          ({"after": {"a\x00.py": "x"}}, "relative .py path"),
                          ({"after": {"d/" * 200 + "a.py": "x"}}, "relative .py path"),
                          ({"after": {"a.py": 3}}, "relative .py path")]:
            with self.subTest(body=str(body)[:40]):
                status, _h, out = self.request("/api/check", body)
                self.assertEqual(status, 400)
                self.assertIn(why, json.loads(out)["error"])

    def test_limits(self):
        many = {f"m{i}.py": "" for i in range(MAX_FILES + 1)}
        status, _h, _out = self.request("/api/check", {"after": many})
        self.assertEqual(status, 400)
        status, _h, _out = self.request("/api/check", b"x" * (MAX_BODY + 1))
        self.assertEqual(status, 413)

    def test_code_that_does_not_parse_is_reported_not_fatal(self):
        status, _h, body = self.request("/api/check",
                                        {"before": {}, "after": {"m.py": "def f(:\n"}})
        self.assertEqual(status, 200)
        self.assertTrue(any("m.py" in e for e in json.loads(body)["errors"]))

    def test_the_website_is_served_and_nothing_outside_it(self):
        status, _h, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())
        for path in ("/../pyproject.toml", "/%2e%2e/pyproject.toml", "/api/nope", "/data/"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)       # no listings either

    def test_incidents_are_replayed(self):
        status, _h, body = self.request("/api/incidents")
        self.assertEqual(status, 200)
        steps = {f"{i['name']}/{s['dir']}": s["status"]
                 for i in json.loads(body) for s in i["steps"]}
        self.assertEqual(steps["sensor-signature-break/2-layout-argument"], "caught")

    def test_only_allowed_origins_may_call_the_api_from_a_browser(self):
        _s, h, _b = self.request("/api/health", headers={"Origin": SITE})
        self.assertEqual(h["Access-Control-Allow-Origin"], SITE)
        self.assertIn("Origin", h["Vary"])
        for origin in ("https://evil.example", "null", SITE + ".evil.example"):
            with self.subTest(origin=origin):
                _s, h, _b = self.request("/api/health", headers={"Origin": origin})
                self.assertIsNone(h["Access-Control-Allow-Origin"])
        status, h, _b = self.request("/api/check", method="OPTIONS", headers={"Origin": SITE})
        self.assertEqual((status, h["Access-Control-Allow-Origin"]), (204, SITE))
        self.assertIn("POST", h["Access-Control-Allow-Methods"])
        _s, h, _b = self.request("/api/check", method="OPTIONS",
                                 headers={"Origin": "https://evil.example"})
        self.assertIsNone(h["Access-Control-Allow-Methods"])

    def test_every_response_carries_security_headers_and_no_version(self):
        for path in ("/", "/api/health", "/nope.html"):
            with self.subTest(path=path):
                _s, h, _b = self.request(path)
                self.assertEqual(h["X-Content-Type-Options"], "nosniff")
                self.assertEqual(h["X-Frame-Options"], "DENY")
                self.assertEqual(h["Referrer-Policy"], "no-referrer")
                csp = h["Content-Security-Policy"]
                self.assertIn("frame-ancestors 'none'", csp)
                script = next(p for p in csp.split("; ") if p.startswith("script-src"))
                self.assertNotIn("unsafe", script)
                self.assertIn("'sha256-", script)          # the one inline script, by hash
                self.assertEqual(h["Server"].strip(), "magellan-lite")

    def test_unknown_host_names_are_refused(self):
        status, _h, _b = self.s.raw("GET", "/api/health", {"Host": "evil.example"})
        self.assertEqual(status, 421)
        status, _h, _b = self.s.raw("GET", "/api/health", {"Host": f"localhost:{self.s.port}"})
        self.assertEqual(status, 200)

    def test_a_check_must_be_json_with_an_honest_length(self):
        host = {"Host": "127.0.0.1"}
        status, _h, _b = self.s.raw("POST", "/api/check",
                                    {**host, "Content-Type": "text/plain", "Content-Length": "2"}, b"{}")
        self.assertEqual(status, 415)
        status, _h, _b = self.s.raw("POST", "/api/check", {**host, "Content-Type": "application/json"})
        self.assertEqual(status, 411)
        status, _h, _b = self.s.raw("POST", "/api/check",
                                    {**host, "Content-Type": "application/json", "Content-Length": "-5"})
        self.assertEqual(status, 400)


class Defences(unittest.TestCase):
    """Servers with tight limits, to see each one hold."""

    def test_each_client_is_rate_limited_and_cannot_forge_who_it_is(self):
        s = Running(policy=Policy(hosts=None, behind_proxy=True,
                                  rates={"check": (2, 60), "any": (2, 60)}))
        try:
            ask = lambda ip: s.request("/api/health", headers={"X-Forwarded-For": ip})  # noqa: E731
            self.assertEqual([ask("1.1.1.1")[0] for _ in range(3)], [200, 200, 429])
            status, h, _b = ask("1.1.1.1")
            self.assertTrue(int(h["Retry-After"]) >= 1)
            self.assertEqual(ask("2.2.2.2")[0], 200)                # another client
            self.assertEqual(ask("2.2.2.2, 1.1.1.1")[0], 429)      # the proxy's entry counts
        finally:
            s.stop()

    def test_a_slow_client_is_cut_off(self):
        s = Running(policy=Policy(hosts=None, socket_timeout=0.5))
        try:
            c = socket.create_connection(("127.0.0.1", s.port), timeout=5)
            c.sendall(b"GET / HTTP/1.1\r\n")                        # ...and never finishes
            time.sleep(1.2)
            self.assertEqual(c.recv(100), b"")                      # the server hung up
            c.close()
            self.assertEqual(s.request("/api/health")[0], 200)      # and still serves others
        finally:
            s.stop()

    def test_a_check_that_runs_too_long_is_stopped(self):
        s = Running(policy=Policy(hosts=None, check_seconds=0.01))
        try:
            status, _h, body = s.request("/api/check", {"before": BEFORE, "after": AFTER})
            self.assertEqual(status, 503)
            self.assertIn("stopped", json.loads(body)["error"])
        finally:
            s.stop()

    def test_past_its_connection_limit_the_server_says_busy(self):
        s = Running(policy=Policy(hosts=None, connections=0))
        try:
            self.assertEqual(s.request("/api/health")[0], 503)
        finally:
            s.stop()


if __name__ == "__main__":
    unittest.main()
