"""Team: two people's changes that are fine apart and break together.

Each test builds a small team: a shared (bare) repository and two people with their own
clones, each with uncommitted work in progress.
"""

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from magellan_lite.engine import check
from magellan_lite.team import share, team, text
from tests.helpers import _rmtree

BASE = {
    "sensor/__init__.py": "",
    "sensor/channel.py": "def parse_record(fields):\n    return fields\n",
    "sensor/collector.py": ("from sensor.channel import parse_record\n\n\n"
                            "def collect(rows):\n    return [parse_record(r) for r in rows]\n"),
}
# Braxton: parse_record needs a layout now, and he updates the one call he knows about
LAYOUT = {
    "sensor/channel.py": "def parse_record(fields, layout):\n    return fields\n",
    "sensor/collector.py": BASE["sensor/collector.py"].replace("parse_record(r)", 'parse_record(r, "v4")'),
}
# Alice, meanwhile: a new upload path that calls parse_record the way it is on main
UPLOAD = {"sensor/api.py": ("from sensor.channel import parse_record\n\n\n"
                            "def upload(row):\n    return parse_record(row)\n")}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class Person:
    def __init__(self, remote: Path, root: Path, name: str) -> None:
        self.root = root
        git(root.parent, "clone", "-q", str(remote), root.name)
        for key, value in (("user.name", name), ("user.email", f"{name}@example.invalid"),
                           ("core.autocrlf", "false")):
            git(root, "config", key, value)

    def write(self, files: dict) -> None:
        for rel, body in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(body), encoding="utf-8", newline="\n")


class Team(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="magellan-lite-team-"))
        self.remote = self.tmp / "team.git"
        git(self.tmp, "init", "-q", "--bare", "--initial-branch=main", str(self.remote))
        seed = self.tmp / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "--initial-branch=main")
        for key, value in (("user.name", "seed"), ("user.email", "seed@example.invalid"),
                           ("core.autocrlf", "false")):
            git(seed, "config", key, value)
        for rel, body in BASE.items():
            (seed / rel).parent.mkdir(parents=True, exist_ok=True)
            (seed / rel).write_text(body, encoding="utf-8", newline="\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "base")
        git(seed, "push", "-q", str(self.remote), "main")
        self.alice = Person(self.remote, self.tmp / "alice", "Alice")
        self.braxton = Person(self.remote, self.tmp / "braxton", "Braxton")

    def tearDown(self) -> None:
        _rmtree(self.tmp)

    def test_a_new_call_the_old_way_breaks_only_together(self):
        self.braxton.write(LAYOUT)
        self.alice.write(UPLOAD)
        # each is fine alone: that is why nobody notices
        self.assertEqual(check(self.braxton.root).verdict, "ok")
        self.assertEqual(check(self.alice.root).verdict, "ok")

        ref, _ = share(self.braxton.root)
        self.assertEqual(ref, "refs/wip/braxton")
        [c] = team(self.alice.root)
        self.assertEqual((c.name, c.verdict), ("braxton", "block"))
        self.assertEqual([(f.rule, f.path, f.line) for f in c.report.findings],
                         [("signature-break", "sensor/api.py", 5)])
        self.assertEqual({ch.name for ch in c.their_changes},
                         {"sensor.channel.parse_record", "sensor.collector.collect"})
        out = text([c])
        self.assertIn("BLOCK · 1 problem that only the combination has", out)
        self.assertIn("sensor/api.py:5", out)

        # and the other way round: Braxton sees Alice's new call too
        share(self.alice.root)
        [c] = team(self.braxton.root)
        self.assertEqual((c.name, c.verdict), ("alice", "block"))

    def test_sharing_leaves_your_branch_index_and_stash_alone(self):
        self.braxton.write({**LAYOUT, "sensor/new_helper.py": "def helper():\n    return 1\n"})
        git(self.braxton.root, "add", "sensor/channel.py")          # something staged
        before = [git(self.braxton.root, *a) for a in
                  (["rev-parse", "HEAD"], ["status", "--porcelain"], ["stash", "list"])]
        share(self.braxton.root)
        after = [git(self.braxton.root, *a) for a in
                 (["rev-parse", "HEAD"], ["status", "--porcelain"], ["stash", "list"])]
        self.assertEqual(before, after)
        shared = git(self.remote, "ls-tree", "-r", "--name-only", "refs/wip/braxton")
        self.assertIn("sensor/new_helper.py", shared.splitlines())   # new files go too

    def test_compatible_work_is_fine_together(self):
        self.braxton.write({"sensor/channel.py":
                            "def parse_record(fields, layout='v3'):\n    return fields\n"})
        self.alice.write(UPLOAD)
        share(self.braxton.root)
        [c] = team(self.alice.root)
        self.assertEqual((c.verdict, c.report.findings), ("ok", []))
        self.assertIn("fine together", text([c]))

    def test_your_own_share_is_skipped(self):
        self.alice.write(UPLOAD)
        share(self.alice.root)
        self.assertEqual(team(self.alice.root), [])
        self.assertIn("nobody else has shared", text([]))

    def test_a_file_you_both_edit_is_named(self):
        self.braxton.write(LAYOUT)
        self.alice.write({"sensor/collector.py": BASE["sensor/collector.py"] + "\n\nLIMIT = 5\n"})
        share(self.braxton.root)
        [c] = team(self.alice.root)
        self.assertEqual(c.overlap, ["sensor/collector.py"])
        self.assertIn("you both edit sensor/collector.py", text([c]))


if __name__ == "__main__":
    unittest.main()
