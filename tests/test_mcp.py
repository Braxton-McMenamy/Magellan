"""The MCP server: the protocol, every tool, and a clean stdout."""

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from magellan_lite import mcp
from tests.helpers import Project

REPO = Path(__file__).resolve().parents[1]
BASE = {
    "app/__init__.py": "",
    "app/pay.py": """
        RATE = 3


        def charge(amount):
            return amount * RATE


        def refund(amount):
            return -amount


        def forgotten():
            return 0
        """,
    "app/shop.py": """
        from app.pay import charge


        def checkout(cart):
            return charge(sum(cart))


        def page(cart):
            return checkout(cart)


        class Basket:
            def charge(self):
                return 0
        """,
    "tests/test_pay.py": """
        from app.pay import charge, refund


        def test_charge():
            assert charge(1) == 3
            assert refund(1) == -1
        """,
}
CHANGE = {"app/pay.py": BASE["app/pay.py"].replace("def charge(amount):",
                                                    "def charge(amount, currency):")}
TOOL_NAMES = {"magellan_lite_brief", "magellan_lite_check", "magellan_lite_reach",
              "magellan_lite_node", "magellan_lite_unused", "magellan_lite_team",
              "magellan_lite_rules", "magellan_lite_plan", "magellan_lite_progress",
              "magellan_lite_done"}


def request(id_, method, params=None) -> dict:
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = Project()
        cls.project.commit(BASE)
        cls.project.write(CHANGE)
        cls.server = mcp.Server(cls.project.root)

    @classmethod
    def tearDownClass(cls):
        cls.project.__exit__()

    def call(self, tool, /, **arguments):
        """``(parsed JSON, isError)`` of one tools/call."""
        r = self.server.handle(request(1, "tools/call", {"name": tool, "arguments": arguments}))
        self.assertEqual(r["jsonrpc"], "2.0")
        content = r["result"]["content"]
        self.assertEqual([c["type"] for c in content], ["text"])
        return json.loads(content[0]["text"]), r["result"]["isError"]

    # -- the protocol ----------------------------------------------------------------------

    def test_initialize_names_the_server_and_its_tools_capability(self):
        r = self.server.handle(request(0, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}))
        self.assertEqual(r["id"], 0)
        res = r["result"]
        self.assertEqual(res["protocolVersion"], "2025-06-18")       # a version we speak: kept
        self.assertEqual(res["serverInfo"]["name"], "magellan-lite")
        self.assertIn("tools", res["capabilities"])
        self.assertIn("magellan_lite_brief", res["instructions"])
        newer = self.server.handle(request(1, "initialize", {"protocolVersion": "2099-01-01"}))
        self.assertEqual(newer["result"]["protocolVersion"], mcp.VERSIONS[0])
        none = self.server.handle(request(2, "initialize", {}))
        self.assertEqual(none["result"]["protocolVersion"], mcp.PROTOCOL)

    def test_tools_list_has_every_tool_with_a_description_and_an_input_schema(self):
        tools = self.server.handle(request(1, "tools/list"))["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, TOOL_NAMES)
        for t in tools:
            schema = t["inputSchema"]
            self.assertEqual(schema["type"], "object", t["name"])
            self.assertIn("path", schema["properties"], t["name"])
            self.assertGreater(len(t["description"]), 80, t["name"])
            for key in schema["required"]:
                self.assertIn(key, schema["properties"])
        required = {t["name"]: t["inputSchema"]["required"] for t in tools}
        self.assertEqual(required["magellan_lite_reach"], ["name"])
        self.assertEqual(required["magellan_lite_node"], ["name"])

    def test_ping_notifications_and_unknown_methods(self):
        self.assertEqual(self.server.handle(request(5, "ping")), {"jsonrpc": "2.0", "id": 5,
                                                                    "result": {}})
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0",
                                              "method": "notifications/initialized"}))
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "id": 9, "result": {}}))
        self.assertEqual(self.server.handle(request(6, "resources/list"))["error"]["code"],
                         -32601)

    def test_an_unknown_tool_is_a_json_rpc_error(self):
        r = self.server.handle(request(7, "tools/call", {"name": "magellan_lite_nope"}))
        self.assertEqual(r["id"], 7)
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("magellan_lite_brief", r["error"]["message"])     # says what exists

    def test_bad_json_is_a_parse_error_and_the_server_goes_on(self):
        out = io.StringIO()
        mcp.serve(self.project.root, io.StringIO('{"jsonrpc": "2.0", "id": 1, "method"\n'
                                                 + json.dumps(request(2, "ping")) + "\n"), out)
        first, second = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(first["error"]["code"], -32700)
        self.assertIsNone(first["id"])
        self.assertEqual(second, {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_a_stray_print_never_reaches_stdout(self):
        def noisy(root, args):
            print("debugging output from a tool")
            return {"ok": True}
        spec = {**mcp.TOOLS["magellan_lite_rules"], "run": noisy}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(mcp.TOOLS, {"magellan_lite_rules": spec}), \
                mock.patch.object(sys, "stderr", err):
            mcp.serve(self.project.root, io.StringIO(json.dumps(request(
                1, "tools/call", {"name": "magellan_lite_rules"})) + "\n"), out)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(json.loads(lines[0])["result"]["content"][0]["text"]),
                         {"ok": True})
        self.assertIn("debugging output", err.getvalue())

    # -- the tools -------------------------------------------------------------------------

    def test_brief_blocks_the_signature_change_and_names_the_caller(self):
        b, is_error = self.call("magellan_lite_brief")
        self.assertFalse(is_error)                  # a block is an answer, not an error
        self.assertEqual(b["verdict"], "block")
        self.assertEqual(b["do_first"][0]["where"], "app/shop.py:5")
        self.assertEqual(b["check_these_files"][0]["path"], "app/shop.py")
        self.assertEqual([t["path"] for t in b["tests_to_run"]], ["tests/test_pay.py"])
        short, _ = self.call("magellan_lite_brief", path=str(self.project.root), limit=1)
        self.assertEqual(len(short["reaches"]), 1)

    def test_check_is_the_full_report_and_can_add_the_map(self):
        r, is_error = self.call("magellan_lite_check")
        self.assertFalse(is_error)
        self.assertEqual(r["verdict"], "block")
        self.assertIn("affected", r)
        self.assertNotIn("map", r)
        with_map, _ = self.call("magellan_lite_check", map=True)
        self.assertIn("app.pay.charge", {n["id"] for n in with_map["map"]["nodes"]})

    def test_reach_lists_the_callers_hop_by_hop(self):
        r, is_error = self.call("magellan_lite_reach", name="app.pay.charge")
        self.assertFalse(is_error)
        self.assertEqual(r["target"]["where"], "app/pay.py:4")
        hops = {h["hop"]: [d["name"] for d in h["definitions"]] for h in r["hops"]}
        self.assertIn("app.shop.checkout", hops[1])
        self.assertIn("app.shop.page", hops[2])
        self.assertIn("(app/shop.py:5)", " ".join(d["via"] for d in r["hops"][0]["definitions"]))
        self.assertEqual(r["files"][0]["path"], "app/shop.py")
        self.assertIn("app/shop.py:1", r["imported_at"])
        self.assertIn("tests/test_pay.py", [t["path"] for t in r["tests"]])
        direct, _ = self.call("magellan_lite_reach", name="app.pay.charge", depth=1)
        self.assertEqual([h["hop"] for h in direct["hops"]], [1])

    def test_a_short_name_resolves_and_an_ambiguous_one_lists_the_candidates(self):
        r, is_error = self.call("magellan_lite_reach", name="pay.charge")
        self.assertFalse(is_error)
        self.assertEqual(r["target"]["name"], "app.pay.charge")
        amb, is_error = self.call("magellan_lite_reach", name="charge")    # function and method
        self.assertTrue(is_error)
        self.assertEqual({c["name"] for c in amb["candidates"]},
                         {"app.pay.charge", "app.shop.Basket.charge"})
        missing, is_error = self.call("magellan_lite_node", name="chrage")
        self.assertTrue(is_error)
        self.assertIn("charge", missing["did_you_mean"])
        at, _ = self.call("magellan_lite_node", name="app/shop.py:9")
        self.assertEqual(at["name"], "app.shop.page")

    def test_node_shows_both_sides_of_its_edges(self):
        n, is_error = self.call("magellan_lite_node", name="app.shop.checkout")
        self.assertFalse(is_error)
        self.assertEqual((n["kind"], n["lang"], n["where"], n["signature"]),
                         ("function", "Python", "app/shop.py:4", "(cart)"))
        self.assertEqual([(c["name"], c["at"]) for c in n["called_by"]],
                         [("app.shop.page", ["app/shop.py:9"])])
        self.assertEqual([c["name"] for c in n["calls"]], ["app.pay.charge"])
        charge, _ = self.call("magellan_lite_node", name="app.pay.charge")
        self.assertIn("app.pay.RATE", [c["name"] for c in charge["calls"] if c["how"] == "reads"])
        basket, _ = self.call("magellan_lite_node", name="app.shop.Basket")
        self.assertEqual(basket["members"], ["app.shop.Basket.charge"])

    def test_unused_lists_what_nothing_mentions_with_the_caveat(self):
        r, is_error = self.call("magellan_lite_unused")
        self.assertFalse(is_error)
        names = [d["name"] for d in r["definitions"]]
        self.assertIn("app.pay.forgotten", names)
        self.assertIn("app.shop.page", names)
        self.assertNotIn("app.pay.charge", names)
        self.assertNotIn("app.pay.refund", names)          # a test calls it: not dead
        self.assertEqual(r["count"], len(names))
        self.assertIn("computed name", r["caveat"])
        self.assertIn("app/pay.py:", r["definitions"][0]["where"])
        only, _ = self.call("magellan_lite_unused", under="app/shop.py")
        self.assertEqual([d["name"] for d in only["definitions"]],
                         ["app.shop.page", "app.shop.Basket"])           # by line

    def test_team_without_a_remote_says_so_plainly(self):
        r, is_error = self.call("magellan_lite_team")
        self.assertTrue(is_error)
        self.assertIn("no remote named 'origin'", r["error"])

    def test_rules_and_bad_arguments(self):
        r, is_error = self.call("magellan_lite_rules")
        self.assertFalse(is_error)
        self.assertIn("signature-break", {x["id"] for x in r["rules"]})
        for name, args, says in (
                ("magellan_lite_reach", {}, "needs 'name'"),
                ("magellan_lite_reach", {"name": "charge", "colour": 1}, "unknown argument"),
                ("magellan_lite_brief", {"limit": "five"}, "whole number"),
                ("magellan_lite_brief", {"against": "nowhere"}, "against must be"),
                ("magellan_lite_unused", {"path": "no/such/folder"}, "not a folder")):
            err, is_error = self.call(name, **args)
            self.assertTrue(is_error, (name, args))
            self.assertIn(says, err["error"])


class Fresh(unittest.TestCase):
    def test_the_next_call_sees_an_edit(self):
        def callers(server):
            r = server.handle(request(1, "tools/call", {"name": "magellan_lite_node",
                                                        "arguments": {"name": "forgotten"}}))
            return [c["name"] for c in json.loads(r["result"]["content"][0]["text"])["called_by"]]
        with Project() as p:
            p.commit(BASE)
            server = mcp.Server(p.root)
            self.assertEqual(callers(server), [])
            p.write({"app/later.py": "from app.pay import forgotten\n\n\n"
                                     "def use():\n    return forgotten()\n"})
            self.assertEqual(callers(server), ["app.later.use"])


class OverStdio(unittest.TestCase):
    def test_a_real_session_writes_only_json_rpc_to_stdout(self):
        with Project() as p:
            p.commit(BASE)
            p.write(CHANGE)
            messages = [
                request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                          "clientInfo": {"name": "test", "version": "0"}}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                request(2, "tools/list"),
                request(3, "tools/call", {"name": "magellan_lite_brief", "arguments": {}}),
                request(4, "tools/call", {"name": "magellan_lite_reach",
                                          "arguments": {"name": "checkout"}}),
            ]
            r = subprocess.run(
                [sys.executable, "-m", "magellan_lite", "mcp", str(p.root)], cwd=REPO,
                input="".join(json.dumps(m) + "\n" for m in messages).encode("utf-8"),
                capture_output=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"\r\n", r.stdout)
        replies = [json.loads(line) for line in r.stdout.decode("utf-8").splitlines()]
        self.assertEqual([m["id"] for m in replies], [1, 2, 3, 4])     # none for the notification
        self.assertTrue(all(m["jsonrpc"] == "2.0" for m in replies))
        self.assertEqual(len(replies[1]["result"]["tools"]), len(TOOL_NAMES))
        brief = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual(brief["verdict"], "block")
        reach = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertEqual(reach["target"]["name"], "app.shop.checkout")
        self.assertEqual([d["name"] for d in reach["hops"][0]["definitions"]], ["app.shop.page"])


if __name__ == "__main__":
    unittest.main()
