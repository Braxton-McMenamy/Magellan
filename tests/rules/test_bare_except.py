import unittest

from tests.rules.checks import found

RULE = "bare-except"

class BareExcept(unittest.TestCase):
    def test_a_bare_except_is_flagged_at_its_line(self):
        [f] = found(RULE, """
            try:
                connect()
            except:
                pass
            """)
        self.assertEqual(f.line, 3)

    def test_naming_the_exception_is_fine(self):
        self.assertEqual(found(RULE, """
            try:
                connect()
            except ConnectionError:
                pass
            """), [])

    def test_except_exception_is_not_bare(self):
        # broad, but Ctrl+C still works: a different (and noisier) rule
        self.assertEqual(found(RULE, """
            try:
                connect()
            except Exception:
                pass
            """), [])

    def test_every_bare_except_counts(self):
        self.assertEqual(len(found(RULE, """
            try:
                a()
            except:
                pass
            try:
                b()
            except ValueError:
                pass
            except:
                raise
            """)), 2)


if __name__ == "__main__":
    unittest.main()
