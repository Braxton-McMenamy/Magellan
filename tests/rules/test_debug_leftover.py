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


if __name__ == "__main__":
    unittest.main()
