"""How a report reads: markdown for a pull request, colour in a terminal."""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from magellan_lite import output
from magellan_lite.cli import main
from magellan_lite.findings import Finding
from magellan_lite.output import markdown, text, wants_colour
from magellan_lite.report import Report
from tests.helpers import Project

FINDINGS = [
    Finding("signature-break", "critical", "pay() calls charge() the old way", "app/pay.py", 6,
            detail="charge went from (amount) to (amount, currency).", fix="Update the call."),
    Finding("mutable-default-argument", "medium", "refund() has a mutable default argument `[]`",
            "app/pay.py", 9),
    Finding("compare-to-none", "low", "__init__ compares <x> with None", "lib/a_b.py", 1),
]


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Markdown(unittest.TestCase):
    def test_every_finding_becomes_one_task_line(self):
        md = markdown(Report(".", "git:origin/main", findings=list(FINDINGS)))
        tasks = [line for line in md.splitlines() if line.startswith("- [ ]")]
        self.assertEqual(len(tasks), len(FINDINGS))
        for f, line in zip(FINDINGS, tasks):
            self.assertIn(f"**{f.severity.upper()}**", line)
            self.assertIn(f"`{f.path}:{f.line}`", line)
            self.assertIn(f"(`{f.rule}`)", line)
        # why and what to do sit under their finding, not as tasks of their own
        self.assertIn("  - charge went from (amount) to (amount, currency).", md)
        self.assertIn("  - Fix: Update the call.", md)

    def test_text_is_escaped_but_code_spans_are_kept(self):
        md = markdown(Report(".", "git:HEAD", findings=list(FINDINGS)))
        self.assertIn("`[]`", md)                         # the rule's own code span
        self.assertIn(r"\_\_init\_\_ compares \<x\>", md)  # not bold, not an HTML tag

    def test_nothing_to_check(self):
        md = markdown(Report(".", "git:HEAD"))
        self.assertIn("**OK**", md)
        self.assertNotIn("- [ ]", md)
        self.assertIn("Nothing to check", md)

    def test_from_the_command_line(self):
        with Project() as p:
            p.commit({"m.py": "def f(x):\n    return x\n"})
            p.write({"m.py": "def f(x=[]):\n    return x\n"})
            code, out, _ = run_cli("check", str(p.root), "--format", "markdown")
            self.assertEqual(code, 0)                     # review does not fail by default
            self.assertTrue(out.startswith("### Magellan Lite: **REVIEW**"), out)
            tasks = [line for line in out.splitlines() if line.startswith("- [ ]")]
            self.assertEqual(len(tasks), 1)
            self.assertIn("`mutable-default-argument`", tasks[0])
            self.assertIn("<summary>What changed: 1 change in 1 file</summary>", out)


class FakeTerminal(io.StringIO):
    def isatty(self):
        return True


class Colour(unittest.TestCase):
    def test_the_verdict_is_red_yellow_or_green(self):
        block = Report(".", "x", findings=[Finding("t", "high", "m", "a.py", 1)])
        with mock.patch("magellan_lite.report.blocking_rules", return_value={"t"}):
            self.assertIn("\033[1;31mBLOCK\033[0m", text(block, colour=True))
        review = Report(".", "x", findings=[Finding("t", "medium", "m", "a.py", 1)])
        self.assertIn("\033[1;33mREVIEW\033[0m", text(review, colour=True))
        self.assertIn("\033[1;32mOK\033[0m", text(Report(".", "x"), colour=True))
        self.assertNotIn("\033", text(review))            # off unless asked for

    def test_only_for_a_terminal_and_never_with_no_color(self):
        with mock.patch.object(output, "_windows_console_colour", return_value=True):
            self.assertTrue(wants_colour(FakeTerminal(), {}))
            self.assertFalse(wants_colour(FakeTerminal(), {"NO_COLOR": "1"}))
            self.assertFalse(wants_colour(FakeTerminal(), {"TERM": "dumb"}))
            self.assertFalse(wants_colour(io.StringIO(), {}))       # a pipe or a file
        with mock.patch.object(output, "_windows_console_colour", return_value=False), \
                mock.patch.object(output.os, "name", "nt"):
            self.assertFalse(wants_colour(FakeTerminal(), {}))      # a console without colour

    def test_the_command_line_prints_no_colour_into_a_pipe(self):
        with Project() as p:
            p.commit({"m.py": "def f(x):\n    return x\n"})
            p.write({"m.py": "def f(x=[]):\n    return x\n"})
            _code, out, _ = run_cli("check", str(p.root))
            self.assertIn("REVIEW", out)
            self.assertNotIn("\033", out)


if __name__ == "__main__":
    unittest.main()
