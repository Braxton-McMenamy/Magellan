"""recursive-cycle: recursion the change creates with nothing visible to stop it (from the full
Magellan's risk.py; the standing-cycle cases come from its tests/regressions)."""

import unittest

from magellan_lite.engine import check_files


def cycles(before: str | None, after: str, path: str = "m.py") -> list:
    b = {} if before is None else {path: before}
    return [f for f in check_files(b, {path: after}).findings if f.rule == "recursive-cycle"]


class AlwaysCallsItself(unittest.TestCase):
    def test_the_shapes_that_can_never_return(self):
        for code, line in [
            ("class User:\n    @property\n    def name(self):\n        return self.name\n", 4),
            ("class User:\n    @property\n    def name(self):\n        return self._name\n\n"
             "    @name.setter\n    def name(self, value):\n        self.name = value\n", 8),
            ("class Cache:\n    def get(self, key):\n        return self.get(key)\n", 3),
            ("def load(path):\n    text = load(path)\n    return text\n", 2),
            ("class Rec:\n    def __setattr__(self, k, v):\n        self.changed = True\n", 3),
        ]:
            with self.subTest(code=code):
                report = check_files({}, {"m.py": code})
                [f] = [f for f in report.findings if f.rule == "recursive-cycle"]
                self.assertEqual((f.severity, f.line), ("high", line))
                self.assertIn("can never return", f.message)
                self.assertEqual(report.verdict, "block")

    def test_recursion_that_can_end_stays_quiet(self):
        for code in [
            "def fact(n):\n    if n <= 1:\n        return 1\n    return n * fact(n - 1)\n",
            "def walk(node):\n    for child in node.children:\n        walk(child)\n",
            "def f(x):\n    return x and f(x[1:])\n",
            "class C(B):\n    def get(self, key):\n        return super().get(key)\n",
            "class C:\n    def get(self, key):\n        return self._data.get(key)\n",
            "def gen(node):\n    yield node\n    yield from gen(node.next)\n",
            "def f(x):\n    try:\n        return f(x)\n    except RecursionError:\n        return None\n",
            "def load(load):\n    return load()\n",
            "class C:\n    def __init__(self):\n        self.run = print\n\n"
            "    def run(self, x):\n        self.run(x)\n",
            "class C:\n    @property\n    def name(self):\n        return self._name\n",
        ]:
            with self.subTest(code=code):
                self.assertEqual(cycles(None, code), [])

    def test_a_function_that_already_did_is_not_this_change(self):
        before = "def load(path):\n    return load(path)\n"
        self.assertEqual(cycles(before, "def load(path):\n    x = 1\n    return load(path)\n"),
                         [])


A_B = "def a(x):\n    return b(x)\n\n\ndef b(x):\n    return x + 1\n"


class CallEachOther(unittest.TestCase):
    def test_existing_functions_closed_into_a_loop(self):
        [f] = cycles(A_B, A_B.replace("return x + 1", "return a(x) + 1"))
        self.assertEqual(f.severity, "medium")
        self.assertIn("call each other in a loop", f.message)
        self.assertIn("a() -> b() -> a()", f.detail)

    def test_a_visible_bound_or_recursion_by_design_stays_quiet(self):
        guarded = ("def a(x, depth=0):\n    return b(x, depth + 1)\n\n\n"
                   "def b(x, depth):\n    return x + 1\n")
        self.assertEqual(cycles(guarded, guarded.replace("return x + 1",
                                                         "return a(x, depth) if depth < 9 else x")),
                         [])
        new = ("def parse(t):\n    return term(t)\n\n\ndef term(t):\n    return parse(t[1:]) "
               "if t else 0\n")
        self.assertEqual(cycles(None, new), [])          # written as recursion, from scratch
        walker = ("def walk(node):\n    for child in node.children:\n        visit(child)\n\n\n"
                  "def visit(node):\n    return node.name\n")
        self.assertEqual(cycles(walker, walker.replace("return node.name",
                                                       "return walk(node.body)")), [])

    def test_an_old_loop_is_not_news_whichever_member_is_touched(self):
        loop = ("def a(x):\n    return b(x)\n\n\ndef b(x):\n    \"\"\"Second hop.\"\"\"\n"
                "    return c(x)\n\n\ndef c(x):\n    return a(x)\n")
        self.assertEqual(cycles(loop, loop.replace("return c(x)", "return c(x + 1)")), [])


if __name__ == "__main__":
    unittest.main()
