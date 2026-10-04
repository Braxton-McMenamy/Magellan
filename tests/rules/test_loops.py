"""loop-without-progress: Knight Capital (2005 change, 2012 loss) and the Zune (2008)."""

import unittest

from tests.rules.checks import found

KNIGHT = """
def power_peg(order):
    while order.filled < order.qty:
        send_child(order.symbol, order.qty - order.filled)
"""

ZUNE = """
def year_from_days(days: int) -> int:
    year = ORIGIN_YEAR
    while days > 365:
        if is_leap_year(year):
            if days > 366:
                days -= 366
                year += 1
        else:
            days -= 365
            year += 1
    return year
"""


class LoopWithoutProgress(unittest.TestCase):
    def test_knight_capital_the_whole_loop(self):
        [f] = found("loop-without-progress", KNIGHT)
        self.assertEqual(f.line, 2)
        self.assertIn("order.filled", f.message)

    def test_zune_one_path_with_a_witness(self):
        [f] = found("loop-without-progress", ZUNE)
        self.assertEqual(f.line, 3)
        self.assertIn("days == 366", f.message)

    def test_the_fixes_stay_quiet(self):
        self.assertEqual(found("loop-without-progress", KNIGHT.replace(
            "        send_child(order.symbol, order.qty - order.filled)",
            "        order.filled += send_child(order.symbol, order.qty - order.filled)")), [])
        self.assertEqual(found("loop-without-progress", KNIGHT.replace(
            "        send_child(order.symbol, order.qty - order.filled)",
            "        filled = send_child(order.symbol, order.qty - order.filled)\n"
            "        track_cumulative(order, filled)")), [])          # 2003: hands order over
        self.assertEqual(found("loop-without-progress", ZUNE.replace(
            "                year += 1\n        else:",
            "                year += 1\n            else:\n                break\n        else:")), [])

    def test_loops_that_end_some_other_way_stay_quiet(self):
        for code in [
            "def f():\n    while True:\n        work()\n",                          # constant
            "def f(q):\n    while not q.empty():\n        handle(q)\n",                # polls
            "def f(self):\n    while not self.done:\n        time.sleep(1)\n",          # waits
            "def f(n):\n    while n > 0:\n        if n == 3:\n            return\n        n -= 1\n",
            "def f(items):\n    while items:\n        items.pop()\n",                  # method
            "def f(items):\n    pop = items.pop\n    while items:\n        pop()\n",   # alias
            "def f(node):\n    while node.children:\n        node.children[0].remove()\n",
            "def f():\n    global running\n    running = True\n    while running:\n        tick()\n",
            "def f(it):\n    while it:\n        yield it\n",                           # generator
            "def f(x: int):\n    while x > 0:\n        x = step(x)\n",
        ]:
            with self.subTest(code=code):
                self.assertEqual(found("loop-without-progress", code), [])

    def test_len_of_something_the_body_does_not_change_is_a_plain_value(self):
        for code, names in [
            ("def f(items):\n    i = 0\n    while i < len(items):\n        handle(items[i])\n",
             "i or items"),                             # forgot i += 1
            ("def f(items):\n    while len(items) > 0:\n        log(items[0])\n", "items"),
            ("def f(self):\n    i = 0\n    while i < len(self.queue):\n"
             "        total = self.queue[i]\n", "i or self.queue"),
        ]:
            with self.subTest(code=code):
                [f] = found("loop-without-progress", code)
                self.assertIn(f"nothing in it changes {names}", f.message)

    def test_len_that_can_move_stays_quiet(self):
        for code in [
            "def f(items):\n    i = 0\n    while i < len(items):\n        handle(items[i])\n"
            "        i += 1\n",
            "def f(items):\n    while len(items) > 0:\n        items.pop()\n",          # shrinks
            "def f(items):\n    while len(items) > 1:\n        items = items[1:]\n",     # rebound
            "def f(items):\n    i = 0\n    while i < len(items):\n        trim(items)\n",  # handed
            "def f(self):\n    while len(self.queue) > 0:\n        self.handle()\n",
            "def f(self):\n    while len(self.buf) < 10:\n        time.sleep(0.1)\n",      # waits
            "def f():\n    i = 0\n    while i < len(ITEMS):\n        work()\n",           # global
            "def f():\n    i = 0\n    while i < len(load()):\n        pass\n",           # polls
            "def f(items):\n    i = 0\n    while i < len(items) and ready(i):\n        pass\n",
        ]:
            with self.subTest(code=code):
                self.assertEqual(found("loop-without-progress", code), [])

    def test_an_impossible_path_is_not_reported(self):
        code = ("def f(n: int):\n    while n > 10:\n        if n < 5:\n            pass\n"
                "        else:\n            n -= 1\n")
        self.assertEqual(found("loop-without-progress", code), [])


if __name__ == "__main__":
    unittest.main()
