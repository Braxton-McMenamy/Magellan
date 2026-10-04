"""unsynchronized-shared-state: Therac-25, 1985-1987."""

import unittest

from tests.rules.checks import found

CONSOLE = """
import threading


class TreatmentConsole:
    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "xray"

    def operator_edit(self, mode):
        self.mode = mode

    def set_up_beam(self):
        return self.mode

    def start(self, mode):
        threading.Thread(target=self.operator_edit, args=(mode,)).start()
        threading.Thread(target=self.set_up_beam).start()
"""


class SharedState(unittest.TestCase):
    def test_therac_two_threads_one_field_no_lock(self):
        [f] = found("unsynchronized-shared-state", CONSOLE)
        self.assertEqual(f.line, 10)
        self.assertIn("operator_edit() writes self.mode", f.message)

    def test_holding_the_lock_on_both_sides_is_quiet(self):
        locked = CONSOLE.replace("        self.mode = mode",
                                 "        with self.lock:\n            self.mode = mode")
        locked = locked.replace("        return self.mode",
                                "        with self.lock:\n            return self.mode")
        self.assertEqual(found("unsynchronized-shared-state", locked), [])

    def test_one_thread_or_no_shared_field_is_quiet(self):
        one = CONSOLE.replace("        threading.Thread(target=self.set_up_beam).start()\n", "")
        self.assertEqual(found("unsynchronized-shared-state", one), [])
        apart = CONSOLE.replace("        return self.mode", "        return 25")
        self.assertEqual(found("unsynchronized-shared-state", apart), [])


if __name__ == "__main__":
    unittest.main()
