"""The fleet's tests: run with `python -m unittest` from the repository folder."""

import unittest
from datetime import date

from fleet.fees import harbour_fee
from fleet.voyage import charter_renewal, depart, plan_route

CARGO = [{"kind": "cloves", "kg": 2600, "count": 2}, {"kind": "fuel", "kg": 800}]


class Fleet(unittest.TestCase):
    def test_route(self):
        self.assertEqual(plan_route("Seville", "Cebu", ["Rio", "Guam"]),
                         ["Seville", "Rio", "Guam", "Cebu"])

    def test_harbour_fee(self):
        self.assertEqual(harbour_fee("Rio", CARGO), 136.5)

    def test_manifest(self):
        m = depart("Trinidad", CARGO, "Seville", "Puerto San Julian", ["Rio"])
        self.assertEqual(m["miles"], 6600)
        self.assertEqual(sorted(m["fees"]), ["Puerto San Julian", "Rio"])

    def test_charter_renews_a_year_later(self):
        self.assertEqual(charter_renewal(date(2026, 3, 1)), date(2027, 3, 1))


if __name__ == "__main__":
    unittest.main()
