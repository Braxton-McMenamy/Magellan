import unittest

from tests.rules.checks import found

RULE = "debug-leftover"

class DebugLeftover(unittest.TestCase):
    def test_breakpoint_is_flagged(self):
        [f] = found(RULE, """
            def total(items):
                breakpoint()
                return sum(items)
            """)
        self.assertEqual(f.line, 2)
        self.assertIn("breakpoint", f.message)

    def test_pdb_set_trace_is_flagged(self):
        [f] = found(RULE, """
            import pdb


            def total(items):
                pdb.set_trace()
                return sum(items)
            """)
        self.assertIn("set_trace", f.message)

    def test_print_is_flagged(self):
        self.assertEqual(len(found(RULE, """
            def total(items):
                print("debug", items)
                return sum(items)
            """)), 1)

    def test_print_in_the_main_block_is_the_point(self):
        self.assertEqual(found(RULE, """
            def total(items):
                return sum(items)


            if __name__ == "__main__":
                print(total([1, 2, 3]))
            """), [])

    def test_other_calls_and_methods_named_print_are_fine(self):
        self.assertEqual(found(RULE, """
            def report(doc, printer):
                printer.print(doc)
                log.info("printed")
            """), [])


LIBRARY = """
def total(items):
    print("debug", items)
    return sum(items)
"""


class PrintIsAProgramsOutput(unittest.TestCase):
    """A command-line tool prints on purpose: those print() calls are not leftovers."""

    def test_print_in_main_is_the_program_talking(self):
        self.assertEqual(found(RULE, """
            class App:
                def main(self, args):
                    print("done:", len(args), "files")
            """), [])

    def test_a_file_named_cli_or_main_is_a_program(self):
        for path in ["cli.py", "pkg/cli.py", "pkg/__main__.py"]:
            with self.subTest(path=path):
                self.assertEqual(found(RULE, LIBRARY, path=path), [])

    def test_a_module_that_is_a_command_line_program(self):
        for program in [
            LIBRARY + '\n\nif __name__ == "__main__":\n    total([1, 2])\n',
            LIBRARY + "\n\ndef main():\n    total([1, 2])\n",
            "import argparse\n" + LIBRARY,
            "from argparse import ArgumentParser\n" + LIBRARY,
        ]:
            with self.subTest(program=program):
                self.assertEqual(found(RULE, program), [])

    def test_a_function_whose_name_says_it_prints(self):
        for name in ["print_report", "show_usage", "display", "printSummary", "dump_table"]:
            with self.subTest(name=name):
                self.assertEqual(found(RULE, LIBRARY.replace("total", name)), [])

    def test_print_that_says_where_it_writes(self):
        self.assertEqual(found(RULE, """
            import sys


            def total(items):
                print("warning: empty", file=sys.stderr)
                return sum(items)
            """), [])

    def test_a_library_print_is_still_flagged(self):
        for code, path in [
            (LIBRARY, "pkg/prices.py"),
            (LIBRARY, "pkg/client.py"),                      # not cli.py
            (LIBRARY.replace("total", "domain_total"), "m.py"),  # "domain" is not "main"
            (LIBRARY.replace("total", "printer_setup"), "m.py"),  # a printer, not printing
            ("import argparse_helpers\n" + LIBRARY, "m.py"),
            ("print_all = True\n" + LIBRARY, "m.py"),        # a name, not a function
        ]:
            with self.subTest(code=code, path=path):
                [f] = found(RULE, code, path=path)
                self.assertIn("print() left in", f.message)

    def test_breakpoint_is_flagged_even_in_a_program(self):
        [f] = found(RULE, """
            def main():
                breakpoint()
                print("hello")
            """, path="cli.py")
        self.assertIn("breakpoint", f.message)


if __name__ == "__main__":
    unittest.main()
