import unittest

from tests.rules.checks import found

RULE = "compare-to-none"


@unittest.skip("TODO(starter): delete this line once magellan_lite/rules/compare_none.py is written")
class CompareToNone(unittest.TestCase):
    def test_equals_none_is_flagged(self):
        [f] = found(RULE, """
            def label(user):
                if user.name == None:
                    return "anonymous"
                return user.name
            """)
        self.assertEqual(f.line, 2)

    def test_not_equals_none_is_flagged(self):
        self.assertEqual(len(found(RULE, "ok = result != None\n")), 1)

    def test_is_none_is_fine(self):
        self.assertEqual(found(RULE, "ok = result is None or other is not None\n"), [])

    def test_comparing_with_other_values_is_fine(self):
        self.assertEqual(found(RULE, "ok = count == 0 or name != ''\n"), [])

    @unittest.skip("bonus (step 5): None on the left")
    def test_none_on_the_left_counts_too(self):
        self.assertEqual(len(found(RULE, "ok = None == result\n")), 1)


if __name__ == "__main__":
    unittest.main()
