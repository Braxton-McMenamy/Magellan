import unittest

from tests.rules.checks import found

RULE = "assert-on-tuple"

class AssertOnTuple(unittest.TestCase):
    def test_an_assert_on_a_tuple_is_flagged(self):
        [f] = found(RULE, """
            def withdraw(balance, amount):
                assert (amount <= balance, "overdrawn")
                return balance - amount
            """)
        self.assertEqual(f.line, 2)

    def test_the_intended_assert_is_fine(self):
        self.assertEqual(found(RULE, """
            def withdraw(balance, amount):
                assert amount <= balance, "overdrawn"
                return balance - amount
            """), [])

    def test_parentheses_around_just_the_condition_are_fine(self):
        self.assertEqual(found(RULE, "assert (x > 0)\n"), [])

    def test_an_empty_tuple_is_a_different_mistake(self):
        # `assert ()` always FAILS (an empty tuple is false): not what this rule is about
        self.assertEqual(found(RULE, "assert ()\n"), [])


if __name__ == "__main__":
    unittest.main()
