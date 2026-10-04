"""co-change: a file git history says moves with the edited one was left alone (from the full
Magellan's cochange.py; the cases come from its tests/rules/test_cochange.py)."""

import unittest

from magellan_lite.engine import check, check_files
from magellan_lite.rules import cochange
from tests.helpers import Project


def found(report) -> list:
    return [f for f in report.findings if f.rule == "co-change"]


class CoChange(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.n = 0

    def tearDown(self):
        self.p.__exit__(None, None, None)

    def commit(self, *names):
        self.n += 1
        self.p.commit({f"{name}.py": f"X = {self.n}\n" for name in names})

    def test_the_partner_left_alone_is_named(self):
        for _ in range(5):
            self.commit("schema", "parser")
        self.p.write({"schema.py": "X = 99\n"})
        [f] = found(self.p.check())
        self.assertEqual((f.path, f.line, f.severity), ("schema.py", 1, "medium"))
        self.assertIn("schema.py usually changes together with parser.py", f.message)
        self.assertIn("5 of the last 5 commits", f.detail)

    def test_quiet_when_the_partner_is_edited_too_or_history_is_thin(self):
        for _ in range(5):
            self.commit("schema", "parser")
        self.p.write({"schema.py": "X = 99\n", "parser.py": "X = 99\n"})
        self.assertEqual(found(self.p.check()), [])
        self.p.write({"schema.py": "X = 5\n", "parser.py": "X = 5\n"})     # back to HEAD
        self.commit("other", "third")
        self.commit("other", "third")
        self.commit("other", "third")                                    # three: not enough
        self.p.write({"other.py": "X = 99\n"})
        self.assertEqual(found(self.p.check()), [])

    def test_a_file_that_also_changes_alone_is_weakly_coupled(self):
        for _ in range(4):
            self.commit("schema", "parser")
        for _ in range(4):
            self.commit("parser")                                        # parser moves alone
        self.p.write({"parser.py": "X = 99\n"})                         # 4 of 8: below 60%
        self.assertEqual(found(self.p.check()), [])
        self.p.write({"parser.py": "X = 8\n", "schema.py": "X = 99\n"})  # 4 of 4 the other way
        self.assertEqual([f.path for f in found(self.p.check())], ["schema.py"])

    def test_no_git_behind_the_check_means_no_opinion(self):
        self.assertEqual(found(check_files({"a.py": "X = 1\n"}, {"a.py": "X = 2\n"})), [])
        for _ in range(5):
            self.commit("schema", "parser")
        old = Project()
        try:
            old.write({"schema.py": "X = 5\n", "parser.py": "X = 5\n"})
            self.p.write({"schema.py": "X = 99\n"})
            self.assertEqual(found(check(self.p.root, against=str(old.root))), [])
        finally:
            old.__exit__(None, None, None)


class History(unittest.TestCase):
    def test_sweeping_commits_say_nothing(self):
        log = "\x1eabc\n" + "\n".join(f"m{i}.py" for i in range(30)) + "\n"
        log += "\x1edef\na.py\nb.py\nREADME.md\n"
        h = cochange.parse_log(log)
        self.assertEqual(dict(h.files), {"a.py": 1, "b.py": 1})
        self.assertEqual(dict(h.pairs), {("a.py", "b.py"): 1})


if __name__ == "__main__":
    unittest.main()
