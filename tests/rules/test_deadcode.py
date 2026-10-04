"""unreachable-code and constant-condition: code that can never run (from the full Magellan)."""

import unittest

from tests.rules.checks import found


class UnreachableCode(unittest.TestCase):
    def test_statements_after_a_return_raise_or_continue(self):
        [f] = found("unreachable-code", """
            def save(order):
                return store(order)
                audit(order)
        """)
        self.assertEqual(f.line, 3)
        self.assertIn("the return on line 2", f.message)

        [f] = found("unreachable-code", """
            def check(x):
                if x < 0:
                    raise ValueError(x)
                    log("negative")
                return x
        """)
        self.assertEqual(f.line, 4)
        self.assertIn("raise", f.message)

        [f] = found("unreachable-code", """
            def total(rows):
                n = 0
                for r in rows:
                    continue
                    n += r
                return n
        """)
        self.assertEqual(f.line, 5)

    def test_reachable_and_idiomatic_code_stays_quiet(self):
        for code in [
            "def f(x):\n    if x:\n        return 1\n    return 2\n",              # other block
            "def gen():\n    return\n    yield\n",                                 # empty generator
            "def f():\n    raise NotImplementedError\n    pass\n",                  # inert leftover
            "def f():\n    return 1\n    ...\n",
            "def f(xs):\n    for x in xs:\n        if x:\n            break\n        use(x)\n",
            "def f():\n    try:\n        return g()\n    finally:\n        close()\n",
        ]:
            with self.subTest(code=code):
                self.assertEqual(found("unreachable-code", code), [])


class ConstantCondition(unittest.TestCase):
    def test_literal_conditions(self):
        [f] = found("constant-condition", "def f():\n    if False:\n        send()\n")
        self.assertIn("`if False:` never runs", f.message)
        [f] = found("constant-condition", "def f():\n    while 0:\n        poll()\n")
        self.assertIn("`while 0:`", f.message)
        [f] = found("constant-condition",
                    "def f():\n    if True:\n        a()\n    else:\n        b()\n")
        self.assertEqual(f.line, 5)
        self.assertIn("else", f.message)

    def test_real_conditions_and_deliberate_loops_stay_quiet(self):
        for code in [
            "def f():\n    while True:\n        work()\n",
            "def f():\n    while 1:\n        work()\n",
            "def f():\n    if True:\n        a()\n",                 # nothing is dead
            "def f():\n    if DEBUG:\n        a()\n",
            "def f(x):\n    if x == 0:\n        a()\n",
            "def f():\n    if 'x':\n        a()\n",
        ]:
            with self.subTest(code=code):
                self.assertEqual(found("constant-condition", code), [])


if __name__ == "__main__":
    unittest.main()
