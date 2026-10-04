"""reused-value: Knight Capital, 2012. A change rule, so it is checked on two versions."""

import unittest

from magellan_lite.engine import check_files

BEFORE = {
    "smars/__init__.py": "",
    "smars/flags.py": "POWER_PEG = 0x08\n",
    "smars/power_peg.py": "def power_peg(order):\n    return order\n",
    "smars/rlp.py": "def rlp(order):\n    return order\n",
    "smars/router.py": ("from smars.flags import POWER_PEG\nfrom smars.power_peg import power_peg\n"
                        "\n\ndef route(order):\n    if order.flags & POWER_PEG:\n"
                        "        power_peg(order)\n"),
}
REUSED = {**BEFORE,
          "smars/flags.py": "RLP = 0x08\n",
          "smars/router.py": ("from smars.flags import RLP\nfrom smars.rlp import rlp\n\n\n"
                              "def route(order):\n    if order.flags & RLP:\n        rlp(order)\n")}


def reused(before: dict, after: dict) -> list:
    return [f for f in check_files(before, after).findings if f.rule == "reused-value"]


class ReusedValue(unittest.TestCase):
    def test_knight_the_flag_handed_to_new_code(self):
        [f] = reused(BEFORE, REUSED)
        self.assertEqual((f.path, f.line), ("smars/router.py", 6))
        self.assertIn("RLP takes over the value 0x08 that POWER_PEG had", f.message)
        self.assertIn("power_peg()", f.message)

    def test_a_plain_rename_is_quiet(self):
        renamed = {**BEFORE,
                   "smars/flags.py": "PEG = 0x08\n",
                   "smars/router.py": BEFORE["smars/router.py"].replace("POWER_PEG", "PEG")}
        self.assertEqual(reused(BEFORE, renamed), [])

    def test_a_new_value_for_the_new_code_is_quiet(self):
        fresh = {**REUSED, "smars/flags.py": "RLP = 0x10\n"}
        self.assertEqual(reused(BEFORE, fresh), [])


if __name__ == "__main__":
    unittest.main()
