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


WORKERS = """
import threading


class Dose:
    def __init__(self):
        self.lock = threading.Lock()
        self.delivered = 0

    def work(self, pulses):
        for pulse in pulses:
            fire(pulse)
            self.delivered += 1

    def start(self, batches):
{start}
"""

LOOP = ("        for batch in batches:\n"
        "            threading.Thread(target=self.work, args=(batch,)).start()\n")


def workers(start: str = LOOP, update: str = "            self.delivered += 1") -> list:
    code = WORKERS.replace("            self.delivered += 1", update).format(start=start)
    return found("unsynchronized-shared-state", code)


class OneMethodOnManyThreads(unittest.TestCase):
    def test_started_in_a_loop_it_races_with_itself(self):
        [f] = workers()
        self.assertEqual(f.line, 12)
        self.assertIn("work() updates self.delivered from the old value", f.message)
        self.assertIn("in a loop (line 16)", f.detail)

    def test_other_ways_to_start_it_more_than_once(self):
        for start in [
            "        self.threads = [threading.Thread(target=self.work, args=(b,))\n"
            "                        for b in batches]\n",
            "        for batch in batches:\n            self.pool.submit(self.work, batch)\n",
            "        threading.Thread(target=self.work, args=(batches[0],)).start()\n"
            "        threading.Thread(target=self.work, args=(batches[1],)).start()\n",
        ]:
            with self.subTest(start=start):
                self.assertEqual(len(workers(start)), 1)

    def test_other_lost_updates(self):
        for update in ["            self.delivered = self.delivered + 1",
                       "            self.per_pulse[pulse] += 1"]:
            with self.subTest(update=update):
                self.assertEqual(len(workers(update=update)), 1)

    def test_what_cannot_lose_an_update_stays_quiet(self):
        self.assertEqual(workers(update="            with self.lock:\n"
                                        "                self.delivered += 1"), [])
        self.assertEqual(workers(update="            self.running = True\n"   # only sets it
                                        "            self.results.put(pulse)"), [])  # a Queue
        self.assertEqual(workers(update="            self.local.count += 1"), [])  # per thread
        self.assertEqual(workers(update="            total += 1"), [])         # a local

    def test_threads_that_never_overlap_stay_quiet(self):
        for start in [
            "        threading.Thread(target=self.work, args=(batches,)).start()\n",  # once
            "        for batch in batches:\n"                                          # joined
            "            t = threading.Thread(target=self.work, args=(batch,))\n"
            "            t.start()\n            t.join()\n",
        ]:
            with self.subTest(start=start):
                self.assertEqual(workers(start), [])

    def test_a_timer_that_re_arms_itself_runs_one_at_a_time(self):
        code = WORKERS.format(start="        threading.Timer(1, self.tick).start()\n") + (
            "\n    def tick(self):\n"
            "        self.delivered += 1\n"
            "        threading.Timer(1, self.tick).start()\n")
        self.assertEqual(found("unsynchronized-shared-state", code), [])


if __name__ == "__main__":
    unittest.main()
