"""The local server: the website, the JSON API, and what it refuses."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from magellan_lite.server import MAX_BODY, MAX_FILES, make_server

BEFORE = {"sensor/__init__.py": "",
          "sensor/channel.py": "def parse(fields):\n    return fields\n",
          "sensor/collector.py": ("from sensor.channel import parse\n\n\n"
                                  "def collect(rows):\n    return [parse(r) for r in rows]\n")}
AFTER = {**BEFORE, "sensor/channel.py": "def parse(fields, layout):\n    return fields\n"}


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = make_server("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def request(self, path, body=None, method=None):
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_health_and_rules(self):
        status, headers, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
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
        # and the map the website draws (magellan_lite/web.py)
        nodes = {n["id"]: n for n in report["map"]["nodes"]}
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
                          ({"after": {"a.py": 3}}, "relative .py path")]:
            with self.subTest(body=body):
                status, _h, out = self.request("/api/check", body)
                self.assertEqual(status, 400)
                self.assertIn(why, json.loads(out)["error"])

    def test_limits(self):
        many = {f"m{i}.py": "" for i in range(MAX_FILES + 1)}
        status, _h, out = self.request("/api/check", {"after": many})
        self.assertEqual(status, 400)
        status, _h, out = self.request("/api/check", b"x" * (MAX_BODY + 1))
        self.assertEqual(status, 413)

    def test_code_that_does_not_parse_is_reported_not_fatal(self):
        status, _h, body = self.request("/api/check",
                                        {"before": {}, "after": {"m.py": "def f(:\n"}})
        self.assertEqual(status, 200)
        self.assertTrue(any("m.py" in e for e in json.loads(body)["errors"]))

    def test_the_website_is_served_and_nothing_outside_it(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())
        status, _h, _b = self.request("/../pyproject.toml")
        self.assertEqual(status, 404)
        status, _h, _b = self.request("/api/nope")
        self.assertEqual(status, 404)

    def test_incidents_are_replayed_live(self):
        status, _h, body = self.request("/api/incidents")
        self.assertEqual(status, 200)
        steps = {f"{i['name']}/{s['dir']}": s["status"]
                 for i in json.loads(body) for s in i["steps"]}
        self.assertEqual(steps["sensor-signature-break/2-layout-argument"], "caught")

    def test_the_browser_preflight_is_answered(self):
        status, headers, _b = self.request("/api/check", method="OPTIONS")
        self.assertEqual(status, 204)
        self.assertIn("POST", headers["Access-Control-Allow-Methods"])


if __name__ == "__main__":
    unittest.main()
