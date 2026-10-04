"""leap-day-date: Windows Azure, February 29 2012."""

import unittest

from tests.rules.checks import found


class LeapDayDate(unittest.TestCase):
    def test_the_azure_shapes_are_caught(self):
        for code in ["def f(now):\n    return now.replace(year=now.year + 1)\n",
                     "def f(d):\n    return date(d.year + 1, d.month, d.day)\n",
                     "def f(d):\n    return datetime.date(d.year - 1, d.month, d.day)\n",
                     "def f(d, n):\n    return d.replace(year=d.year + n)\n"]:
            with self.subTest(code=code):
                self.assertEqual(len(found("leap-day-date", code)), 1)

    def test_the_message_names_the_call(self):
        [f] = found("leap-day-date", "def f(now):\n    return now.replace(year=now.year + 1)\n")
        self.assertIn("now.replace(year=now.year + 1)", f.message)
        self.assertIn("February 29", f.message)

    def test_safe_versions_stay_quiet(self):
        for code in [
            "def f(now):\n    return now + timedelta(days=365)\n",                 # no rebuild
            "def f(d):\n    return d.replace(year=d.year + 4)\n",                  # leap again
            "def f(d):\n    return date(d.year + 1, 1, 1)\n",                      # not the day
            "def f(s):\n    return s.replace('a', 'b')\n",                         # a string
            "def f(d):\n    try:\n        return d.replace(year=d.year + 1)\n"
            "    except ValueError:\n        return d.replace(year=d.year + 1, day=28)\n",
            "def f(d):\n    if d.month == 2 and d.day == 29:\n        d = d.replace(day=28)\n"
            "    return d.replace(year=d.year + 1)\n",
            "def f(d):\n    if calendar.isleap(d.year):\n        pass\n"
            "    return d.replace(year=d.year + 1)\n",
        ]:
            with self.subTest(code=code):
                self.assertEqual(found("leap-day-date", code), [])

    def test_the_keyword_form_is_caught(self):
        for code in [
            "def f(d):\n    return date(year=d.year + 1, month=d.month, day=d.day)\n",
            "def f(d):\n    return date(day=d.day, month=d.month, year=d.year + 1)\n",
            "def f(d):\n    return date(d.year + 1, month=d.month, day=d.day)\n",
            "def f(d):\n    return datetime.datetime(year=d.year + 1, month=d.month, "
            "day=d.day, hour=9)\n",
            "def f(d):\n    return date(year=d.year + 1, month=2, day=d.day)\n",  # February
        ]:
            with self.subTest(code=code):
                [f] = found("leap-day-date", code)
                self.assertIn("February 29", f.message)

    def test_date_libraries_and_fixed_months_stay_quiet(self):
        for code in [
            "def f(d, t):\n    return datetime.combine(d, t)\n",
            "def f(d):\n    return d + relativedelta(years=1)\n",
            "def f(d):\n    return d.shift(years=1)\n",                          # arrow
            "def f(d):\n    return arrow.get(d).shift(years=+1)\n",
            "def f(d):\n    return pendulum.instance(d).add(years=1)\n",          # pendulum
            "def f(d):\n    return date(year=d.year + 1, month=1, day=d.day)\n",  # January
            "def f(d):\n    return date(d.year + 1, 12, d.day)\n",
        ]:
            with self.subTest(code=code):
                self.assertEqual(found("leap-day-date", code), [])


if __name__ == "__main__":
    unittest.main()
