"""import-cycle: the change makes modules import each other while they load (from the full
Magellan's risk.py, narrowed to cycles the change creates)."""

import unittest

from magellan_lite.engine import check_files

BASE = {
    "shop/__init__.py": "",
    "shop/models.py": "from shop.prices import tax\n\n\nclass Order:\n    def total(self):\n"
                      "        return tax(1)\n",
    "shop/prices.py": "RATE = 0.08\n\n\ndef tax(x):\n    return x * RATE\n",
}


def cycles(before: dict, after: dict) -> list:
    return [f for f in check_files(before, after).findings if f.rule == "import-cycle"]


class ImportCycle(unittest.TestCase):
    def test_a_new_import_closes_a_circle(self):
        after = {**BASE, "shop/prices.py": "from shop.models import Order\n\nRATE = 0.08\n\n\n"
                                           "def tax(x):\n    return x * RATE\n"}
        [f] = cycles(BASE, after)
        self.assertEqual((f.path, f.line, f.severity), ("shop/prices.py", 1, "medium"))
        self.assertIn("shop.prices -> shop.models -> shop.prices", f.message)

    def test_relative_imports_count_too(self):
        after = {**BASE, "shop/prices.py": "from .models import Order\n\nRATE = 0.08\n"}
        self.assertEqual(len(cycles(BASE, after)), 1)

    def test_imports_that_do_not_run_at_load_time_stay_quiet(self):
        for prices in [
            "RATE = 0.08\n\n\ndef tax(x):\n    from shop.models import Order\n    return x\n",
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
            "    from shop.models import Order\nRATE = 0.08\n",
            "RATE = 0.08\nif __name__ == '__main__':\n    from shop.models import Order\n",
        ]:
            with self.subTest(prices=prices):
                self.assertEqual(cycles(BASE, {**BASE, "shop/prices.py": prices}), [])

    def test_plain_module_imports_both_ways_load_fine(self):
        base = {"a.py": "import b\n\n\ndef f():\n    return b.g()\n",
                "b.py": "def g():\n    return 1\n"}
        after = {**base, "b.py": "import a\n\n\ndef g():\n    return a.f\n"}
        self.assertEqual(cycles(base, after), [])

    def test_an_old_cycle_is_not_news(self):
        looped = {**BASE, "shop/prices.py": "from shop.models import Order\n\nRATE = 0.08\n\n\n"
                                            "def tax(x):\n    return x * RATE\n"}
        edited = {**looped, "shop/prices.py": looped["shop/prices.py"].replace("0.08", "0.09")}
        self.assertEqual(cycles(looped, edited), [])


if __name__ == "__main__":
    unittest.main()
