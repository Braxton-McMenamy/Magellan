"""complexity-regression: an edited function that now grows faster with its input (from the full
Magellan's complexity.py; the shapes come from its tests/fixtures/complexity_algos.py)."""

import unittest

from magellan_lite.engine import check_files
from magellan_lite.rules.cost import _Project
from magellan_lite.source import Snapshot


def slower(before: str, after: str) -> list:
    return [f for f in check_files({"m.py": before}, {"m.py": after}).findings
            if f.rule == "complexity-regression"]


LINEAR = "def total(orders, items):\n    t = 0\n    for o in orders:\n        t += o.qty\n    return t\n"


class Regression(unittest.TestCase):
    def test_a_loop_added_inside_a_loop(self):
        after = LINEAR.replace("        t += o.qty\n",
                               "        for i in items:\n            t += o.qty * i.price\n")
        [f] = slower(LINEAR, after)
        self.assertEqual((f.severity, f.line), ("medium", 1))
        self.assertIn("total() went from O(n) to O(n^2)", f.message)
        self.assertIn("loops over items inside a loop over orders", f.detail)

    def test_a_list_scanned_for_every_item(self):
        before = ("def dedupe(xs):\n    seen = set()\n    out = []\n    for x in xs:\n"
                  "        if x not in seen:\n            seen.add(x)\n            out.append(x)\n"
                  "    return out\n")
        [f] = slower(before, before.replace("seen = set()", "seen = []")
                     .replace("seen.add(x)", "seen.append(x)"))
        self.assertIn("`in seen` (a list", f.detail)

    def test_a_sort_inside_a_loop(self):
        before = "def top(xs):\n    best = []\n    for x in xs:\n        best.append(x)\n    return best\n"
        [f] = slower(before, before.replace("best.append(x)\n",
                                            "best.append(x)\n        best = sorted(best)\n"))
        self.assertIn("O(n^2 log n)", f.message)

    def test_a_helper_that_loops_called_once_per_item(self):
        helper = ("def find(items, key):\n    for i in items:\n        if i.key == key:\n"
                  "            return i\n\n\n")
        before = helper + ("def match(orders, items):\n    index = {i.key: i for i in items}\n"
                           "    return [index.get(o.key) for o in orders]\n")
        after = helper + "def match(orders, items):\n    return [find(items, o.key) for o in orders]\n"
        [f] = slower(before, after)
        self.assertIn("calls find()", f.detail)


class Quiet(unittest.TestCase):
    def test_shapes_that_do_not_multiply(self):
        for inner in [
            "        for line in o.lines:\n            t += line.qty\n",       # a piece of o
            "        for k in range(10):\n            t += k\n",
            "        for k in KNOWN_KEYS:\n            t += k\n",
            "        t += normalize(o.name)\n",
            "        t += normalize(o.name or o.title)\n",                  # built from o
            "        if o.kind in ('a', 'b'):\n            t += 1\n",
            "        t += sum(o.parts)\n",
            "        lines = o.lines if o.ok else []\n        for line in lines:\n"
            "            t += line.qty\n",                                   # still a piece of o
        ]:
            with self.subTest(inner=inner):
                helper = ("\n\ndef normalize(name):\n    out = 0\n    for ch in name:\n"
                          "        out += 1\n    return out\n")
                self.assertEqual(slower(LINEAR + helper,
                                        LINEAR.replace("        t += o.qty\n", inner) + helper), [])

    def test_calling_something_expensive_once_is_that_functions_business(self):
        helper = ("def pairs(xs):\n    out = []\n    for a in xs:\n        for b in xs:\n"
                  "            out.append((a, b))\n    return out\n\n\n")
        before = helper + "def report(xs):\n    return len(xs)\n"
        self.assertEqual(slower(before, helper + "def report(xs):\n    return len(pairs(xs))\n"),
                         [])
        state = ("class Store:\n    def lookup(self, key):\n        for i in self.items:\n"
                 "            if i.key == key:\n                return i\n\n"
                 "    def match(self, orders):\n        return [o.key for o in orders]\n")
        self.assertEqual(slower(state, state.replace("[o.key for o in orders]",
                                                     "[self.lookup(o.key) for o in orders]")), [])

    def test_already_quadratic_or_new_code_is_not_this_change(self):
        quad = ("def pairs(xs):\n    out = []\n    for a in xs:\n        for b in xs:\n"
                "            out.append((a, b))\n    return out\n")
        self.assertEqual(slower(quad, quad.replace("(a, b)", "(b, a)")), [])
        self.assertEqual(slower("", quad), [])

    def test_capped_and_halving_loops(self):
        before = "def f(xs, rounds=8):\n    for x in xs:\n        pass\n"
        self.assertEqual(slower(before, "def f(xs, rounds=8):\n    for _ in range(rounds):\n"
                                        "        for x in xs:\n            pass\n"), [])
        before = "def f(xs, n):\n    for x in xs:\n        pass\n"
        self.assertEqual(slower(before, "def f(xs, n):\n    for x in xs:\n        while n > 1:\n"
                                        "            n //= 2\n"), [])


class Estimates(unittest.TestCase):
    """A few shapes from the full Magellan's fixture, estimated directly."""

    SOURCE = '''
KNOWN_KEYS = ("a", "b", "c")

def linear(xs):
    t = 0
    for x in xs:
        t += x
    return t

def quadratic(xs):
    c = 0
    for a in xs:
        for b in xs:
            c += a * b
    return c

def cubic(xs):
    for a in xs:
        for b in xs:
            for d in xs:
                pass

def fixed_loops(xs):
    for i in range(10):
        for j in range(10):
            pass

def sort_in_loop(xs):
    for x in xs:
        sorted(xs)

def dedupe_set(xs):
    seen = set()
    for x in xs:
        if x in seen:
            continue
        seen.add(x)
    return seen

def bubble(xs):
    n = len(xs)
    for i in range(n):
        for j in range(n - i - 1):
            if xs[j] > xs[j + 1]:
                xs[j], xs[j + 1] = xs[j + 1], xs[j]

def nested_comp(xs):
    return [[a * b for b in xs] for a in xs]

def list_insert_loop(xs):
    out = []
    for x in xs:
        out.insert(0, x)
    return out

def capped_rounds(xs, max_rounds=8):
    for _ in range(max_rounds):
        for x in xs:
            pass

def constant_table(xs):
    for x in xs:
        for k in KNOWN_KEYS:
            pass

def child_loops(nodes):
    for n in nodes:
        for c in n.children:
            pass

def two_sequential(xs):
    for x in xs:
        pass
    for x in xs:
        pass
'''
    EXPECTED = {"linear": (1, 0), "quadratic": (2, 0), "cubic": (3, 0), "fixed_loops": (0, 0),
                "sort_in_loop": (2, 1), "dedupe_set": (1, 0), "nested_comp": (2, 0),
                "list_insert_loop": (2, 0), "capped_rounds": (1, 0), "constant_table": (1, 0),
                "child_loops": (1, 0), "two_sequential": (1, 0)}

    def test_estimates(self):
        from magellan_lite.defs import definitions
        from magellan_lite.graph import build_graph
        snap = Snapshot({"algos.py": self.SOURCE})
        project = _Project(snap, build_graph(snap, definitions(snap)))
        for name, rank in self.EXPECTED.items():
            with self.subTest(name):
                self.assertEqual(project.cost(f"algos.{name}").rank, rank)

    def test_bubble_sort_is_quadratic(self):
        from magellan_lite.defs import definitions
        from magellan_lite.graph import build_graph
        snap = Snapshot({"algos.py": self.SOURCE})
        project = _Project(snap, build_graph(snap, definitions(snap)))
        self.assertEqual(project.cost("algos.bubble").rank, (2, 0))


if __name__ == "__main__":
    unittest.main()
