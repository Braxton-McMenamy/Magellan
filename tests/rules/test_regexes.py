"""regex-catastrophic-backtracking: Cloudflare, July 2 2019."""

import unittest

from tests.rules.checks import found

CLOUDFLARE = (r"""(?:(?:"|'|\]|\}|\\|\d|(?:nan|infinity|true|false|null|undefined|symbol|math)"""
              r"""|`|\-|\+)+[)]*;?((?:\s|-|~|!|{}|\|\||\+)*.*(?:.*=.*)))""")


def check(pattern: str, call: str = "compile") -> list:
    return found("regex-catastrophic-backtracking", f"import re\nX = re.{call}({pattern!r})\n")


class RegexBacktracking(unittest.TestCase):
    def test_cloudflare_four_parts_in_a_row(self):
        [f] = check(CLOUDFLARE)
        self.assertIn("four unbounded parts", f.message)
        self.assertEqual(f.line, 2)

    def test_nested_repeats(self):
        for pattern in [r"^(a+)+$", r"(\w+\s?)*;", r"^(\d+)*x"]:
            with self.subTest(pattern=pattern):
                [f] = check(pattern, "match")
                self.assertIn("exponentially", f.message)

    def test_three_overlapping_parts_before_something_that_can_fail(self):
        self.assertEqual(len(check(r"\s*(.+?)\s*=")), 1)

    def test_patterns_that_cannot_blow_up_stay_quiet(self):
        for pattern in [
            r"(?i)\bunion\b\s+(?:all\s+)?\bselect\b",
            r"^\[\s*(\w+)\s*\]\s*",                     # \w+ sits between the \s*
            r"(\w+(\.\w+)*)\.(\w*)",                    # each round needs a '.'
            r"\d+\w*'",                                 # two parts: quadratic, left alone
            r".*.*",                                    # nothing after can fail
            r"(a+)+",                                   # the first try always succeeds
            r"[a-z]+@[a-z]+\.com",
        ]:
            with self.subTest(pattern=pattern):
                self.assertEqual(check(pattern), [])

    def test_a_regex_that_does_not_compile_is_left_to_re(self):
        self.assertEqual(check(r"(unclosed"), [])


RULE = "regex-catastrophic-backtracking"


class PatternInAConstant(unittest.TestCase):
    def test_a_constant_is_looked_up_and_reported_on_the_pattern(self):
        [f] = found(RULE, """
            import re

            WORD = r"^(a+)+$"


            def check(text):
                return re.match(WORD, text)
            """)
        self.assertEqual(f.line, 3)                     # the pattern, where the fix goes
        self.assertIn("kept in WORD", f.message)
        self.assertIn("re.match() on line 7", f.message)
        self.assertIn("exponentially", f.message)

    def test_an_annotated_constant_used_twice_is_one_finding(self):
        [f] = found(RULE, """
            import re

            XSS: str = r"(\\w+\\s?)*;"
            STRICT = re.compile(XSS)
            LOOSE = re.compile(XSS, re.IGNORECASE)
            """)
        self.assertEqual(f.line, 3)

    def test_constants_it_cannot_be_sure_of_stay_quiet(self):
        for code in [
            # a safe pattern
            'import re\nWORD = r"[a-z]+@[a-z]+\\.com"\nX = re.compile(WORD)\n',
            # never handed to re
            'import re\nWORD = r"^(a+)+$"\nX = other.compile(WORD)\n',
            # assigned twice: which one reaches the call?
            'import re\nWORD = r"^(a+)+$"\nWORD = r"^a+$"\nX = re.compile(WORD)\n',
            # rebound at run time by a function
            'import re\nWORD = r"^(a+)+$"\n\ndef reset():\n    global WORD\n    WORD = "a"\n\n'
            'X = re.compile(WORD)\n',
            # a parameter or a local of the same name hides the constant
            'import re\nWORD = r"^(a+)+$"\n\ndef f(WORD, s):\n    return re.match(WORD, s)\n',
            'import re\nWORD = r"^(a+)+$"\n\ndef f(s):\n    WORD = r"^a+$"\n'
            '    return re.match(WORD, s)\n',
            # imported from elsewhere: not this module's to read
            'import re\nfrom patterns import WORD\nX = re.compile(WORD)\n',
            # built at run time, not a literal
            'import re\nWORD = "^(" + part + ")+$"\nX = re.compile(WORD)\n',
        ]:
            with self.subTest(code=code):
                self.assertEqual(found(RULE, code), [])


if __name__ == "__main__":
    unittest.main()
