"""The engine: the call graph, the blast radius, and the calls a change breaks."""

import textwrap
import unittest

from magellan_lite.defs import definitions
from magellan_lite.diff import diff
from magellan_lite.graph import build_graph
from magellan_lite.radius import blast_radius
from magellan_lite.source import Snapshot
from tests.helpers import Project


def snap(files: dict[str, str]) -> Snapshot:
    return Snapshot({k: textwrap.dedent(v).lstrip("\n") for k, v in files.items()})


def edges(files: dict[str, str]) -> set[tuple]:
    s = snap(files)
    return {(e.src, e.kind, e.dst, e.guess) for e in build_graph(s, definitions(s)).edges}


class Graph(unittest.TestCase):
    def test_calls_are_resolved_through_modules_and_imports(self):
        got = edges({
            "app/__init__.py": "",
            "app/util.py": """
                LIMIT = 3


                def helper():
                    return 1
                """,
            "app/main.py": """
                import app.util as u
                from app.util import LIMIT, helper
                from . import util


                def local():
                    return 2


                def run():
                    helper()
                    u.helper()
                    util.helper()
                    local()
                    return LIMIT
                """})
        self.assertLessEqual({
            ("app.main.run", "calls", "app.util.helper", False),
            ("app.main.run", "calls", "app.main.local", False),
            ("app.main.run", "reads", "app.util.LIMIT", False)}, got)

    def test_methods_through_self_and_classes_and_constructors(self):
        got = edges({"shop.py": """
            class Cart:
                def add(self, item):
                    return self.total()

                def total(self):
                    return 0


            def checkout():
                cart = Cart()
                return Cart.total(cart)
            """})
        self.assertLessEqual({
            ("shop.Cart.add", "calls", "shop.Cart.total", False),
            ("shop.checkout", "calls", "shop.Cart", False),
            ("shop.checkout", "calls", "shop.Cart.total", False)}, got)

    def test_a_parameter_named_like_a_function_is_not_that_function(self):
        got = edges({"m.py": """
            def process(x):
                return x


            def run(process):
                return process(1)
            """})
        self.assertNotIn(("m.run", "calls", "m.process", False), got)

    def test_an_unknown_receiver_is_a_guess_only_when_one_method_has_the_name(self):
        got = edges({"m.py": """
            class Ledger:
                def settle(self):
                    return 1

                def get(self):
                    return 2


            def close(book):
                book.settle()
                book.get()
            """})
        self.assertIn(("m.close", "calls", "m.Ledger.settle", True), got)
        self.assertFalse(any(dst == "m.Ledger.get" for _s, _k, dst, _g in got))   # too common


class Radius(unittest.TestCase):
    CHAIN = {"m.py": textwrap.dedent("""
        def leaf(x):
            return x


        def middle(x):
            return leaf(x)


        def top(x):
            return middle(x)


        def unrelated():
            return 0
        """).lstrip("\n")}

    def radius(self, before, after):
        b, a = snap(before), snap(after)
        bd, ad = definitions(b), definitions(a)
        return blast_radius(diff(bd, ad), build_graph(a, ad, bd), ad)

    def test_impact_flows_to_callers_and_fades_with_each_hop(self):
        after = {"m.py": self.CHAIN["m.py"].replace("return x\n\n\ndef middle",
                                                    "return x + 1\n\n\ndef middle")}
        got = {a["name"]: (a["hops"], a["score"]) for a in self.radius(self.CHAIN, after)}
        self.assertEqual(got, {"m.middle": (1, 0.54), "m.top": (2, 0.49)})

    def test_a_signature_change_starts_hotter_than_a_body_change(self):
        after = {"m.py": self.CHAIN["m.py"].replace("def leaf(x):", "def leaf(x, y):")}
        got = {a["name"]: a["score"] for a in self.radius(self.CHAIN, after)}
        self.assertEqual(got["m.middle"], 0.85)          # 0.95 x 0.9, against 0.6 x 0.9 for a body

    def test_what_the_change_edited_is_not_listed_as_reached(self):
        after = {"m.py": self.CHAIN["m.py"]
                 .replace("return x\n\n\ndef middle", "return 1\n\n\ndef middle")
                 .replace("return leaf(x)", "return leaf(x) + 1")}
        names = [a["name"] for a in self.radius(self.CHAIN, after)]
        self.assertEqual(names, ["m.top"])


class Breaks(unittest.TestCase):
    CHANNEL = """
        def parse(fields):
            return fields
        """
    COLLECTOR = """
        from sensor.channel import parse


        def collect(rows):
            return [parse(r) for r in rows]
        """

    def check(self, after_channel, collector=COLLECTOR):
        with Project() as p:
            p.commit({"sensor/__init__.py": "", "sensor/channel.py": self.CHANNEL,
                      "sensor/collector.py": self.COLLECTOR})
            p.write({"sensor/channel.py": after_channel, "sensor/collector.py": collector})
            return p.check()

    def test_a_new_required_parameter_breaks_the_untouched_caller(self):
        r = self.check("def parse(fields, layout):\n    return fields\n")
        [f] = [f for f in r.findings if f.rule == "signature-break"]
        self.assertEqual((f.path, f.line, f.severity), ("sensor/collector.py", 5, "critical"))
        self.assertIn("requires layout", f.message)
        self.assertIn("did not touch", f.detail)
        self.assertEqual(r.verdict, "block")
        self.assertEqual([a["name"] for a in r.affected], ["sensor.collector.collect"])

    def test_compatible_changes_break_nothing(self):
        for new in ("def parse(fields, layout='v3'):\n    return fields\n",
                    "def parse(fields, *rest):\n    return fields\n",
                    "def parse(fields, *, strict=False):\n    return fields\n"):
            with self.subTest(new=new):
                r = self.check(new)
                self.assertEqual([f.rule for f in r.findings if f.rule == "signature-break"], [])

    def test_an_updated_caller_is_fine(self):
        r = self.check("def parse(fields, layout):\n    return fields\n",
                       self.COLLECTOR.replace("parse(r)", "parse(r, 'v4')"))
        self.assertEqual([f.rule for f in r.findings if f.rule == "signature-break"], [])

    def test_a_removed_keyword_breaks_a_caller_that_passed_it(self):
        with Project() as p:
            p.commit({"m.py": "def send(msg, retry=3):\n    return msg\n\n\n"
                              "def go():\n    return send('x', retry=1)\n"})
            p.write({"m.py": "def send(msg):\n    return msg\n\n\n"
                             "def go():\n    return send('x', retry=1)\n"})
            [f] = [f for f in p.check().findings if f.rule == "signature-break"]
            self.assertIn("no parameter named retry", f.message)

    def test_a_call_through_self_does_not_count_self(self):
        with Project() as p:
            src = ("class A:\n    def f(self, x):\n        return x\n\n"
                   "    def g(self):\n        return self.f(1)\n")
            p.commit({"m.py": src})
            p.write({"m.py": src.replace("def f(self, x)", "def f(self, x, y)")})
            [f] = [f for f in p.check().findings if f.rule == "signature-break"]
            self.assertIn("requires y", f.message)

    def test_unpacked_arguments_cannot_be_counted_so_are_not_judged(self):
        r = self.check("def parse(fields, layout):\n    return fields\n",
                       self.COLLECTOR.replace("parse(r)", "parse(*r)"))
        self.assertEqual([f.rule for f in r.findings if f.rule == "signature-break"], [])

    def test_a_deleted_function_still_called_and_imported(self):
        r = self.check("def parse_rows(fields):\n    return [fields]\n")
        found = sorted((f.rule, f.line) for f in r.findings)
        self.assertEqual(found, [("removed-still-referenced", 1), ("removed-still-referenced", 5)])
        self.assertEqual(r.verdict, "block")

    def test_a_rename_left_a_caller_behind(self):
        r = self.check("def parse_row(fields):\n    return fields\n")
        self.assertEqual([c.kind for c in r.changes], ["renamed"])
        msgs = [f.message for f in r.findings if f.rule == "removed-still-referenced"]
        self.assertTrue(msgs, r.findings)
        self.assertTrue(all("renamed to sensor.channel.parse_row" in m for m in msgs), msgs)


class Renames(unittest.TestCase):
    def test_a_renamed_function_is_one_change(self):
        before = snap({"pay.py": "def charge(amount):\n    return amount * 2\n"})
        after = snap({"pay.py": "def bill(amount):\n    return amount * 2\n"})
        [c] = diff(definitions(before), definitions(after))
        self.assertEqual((c.kind, c.name, c.before.name), ("renamed", "pay.bill", "pay.charge"))

    def test_constants_with_the_same_value_are_not_paired(self):
        # Knight Capital: POWER_PEG = 0x08 deleted, RLP = 0x08 added is a new meaning
        before = snap({"flags.py": "POWER_PEG = 0x08\n"})
        after = snap({"flags.py": "RLP = 0x08\n"})
        kinds = sorted(c.kind for c in diff(definitions(before), definitions(after)))
        self.assertEqual(kinds, ["added", "removed"])

    def test_two_equal_candidates_are_not_guessed_between(self):
        before = snap({"m.py": "def a():\n    pass\n\n\ndef b():\n    pass\n"})
        after = snap({"m.py": "def c():\n    pass\n\n\ndef d():\n    pass\n"})
        kinds = sorted(c.kind for c in diff(definitions(before), definitions(after)))
        self.assertEqual(kinds, ["added", "added", "removed", "removed"])


if __name__ == "__main__":
    unittest.main()
