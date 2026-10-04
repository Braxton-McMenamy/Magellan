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


if __name__ == "__main__":
    unittest.main()
