"""The Team suite's check: everyone's work in progress alone, two by two, and all together.

``web.team_live`` is what the website runs in the browser on files it read from GitHub, so it
gets the same story as test_team.py, without git.
"""

import time
import unittest

from magellan_lite.combine import derive
from magellan_lite.source import Snapshot
from magellan_lite.web import team_live
from tests.test_team import BASE, LAYOUT, UPLOAD

# Faidh, meanwhile: adds a helper nobody else uses
DOCS = {"sensor/collector.py": BASE["sensor/collector.py"] + "\n\ndef count(rows):\n    return len(rows)\n"}


def work(changes: dict) -> dict:
    return {**BASE, **changes}


class TeamLive(unittest.TestCase):
    def test_fine_apart_broken_together(self):
        out = team_live(BASE, {"braxton": work(LAYOUT), "alice": work(UPLOAD)})
        members = {m["name"]: m for m in out["members"]}
        self.assertEqual(sorted(members), ["alice", "braxton"])
        self.assertEqual({n: m["verdict"] for n, m in members.items()},
                         {"alice": "ok", "braxton": "ok"})      # each is fine alone
        self.assertIn("nodes", members["braxton"]["map"])

        (pair,) = out["pairs"]
        self.assertEqual((pair["a"], pair["b"], pair["verdict"]), ("alice", "braxton", "block"))
        self.assertEqual([(f["rule"], f["path"]) for f in pair["findings"]],
                         [("signature-break", "sensor/api.py")])
        self.assertEqual(pair["overlap"], [])

        team = out["team"]
        self.assertEqual(team["verdict"], "block")
        nodes = {n["id"]: n for n in team["map"]["nodes"]}
        self.assertEqual(nodes["sensor.channel.parse_record"]["by"], ["braxton"])
        self.assertEqual(nodes["sensor.api.upload"]["by"], ["alice"])
        self.assertTrue(nodes["sensor.api.upload"]["finding"])
        self.assertEqual(nodes["sensor.collector.collect"]["by"], ["braxton"])   # his call update
        self.assertEqual(nodes["sensor.collector.collect"]["reached_by"], [])

    def test_one_finding_per_pair_and_none_for_harmless_work(self):
        out = team_live(BASE, {"braxton": work(LAYOUT), "alice": work(UPLOAD),
                               "faidh": work(DOCS)})
        verdicts = {(p["a"], p["b"]): p["verdict"] for p in out["pairs"]}
        self.assertEqual(verdicts, {("alice", "braxton"): "block", ("alice", "faidh"): "ok",
                                    ("braxton", "faidh"): "ok"})

    def test_a_file_two_people_changed_is_listed_not_merged(self):
        out = team_live(BASE, {"braxton": work(LAYOUT), "faidh": work(DOCS)})
        self.assertEqual(out["team"]["overlap"],
                         [{"path": "sensor/collector.py", "names": ["braxton", "faidh"]}])
        self.assertEqual(out["pairs"][0]["overlap"], ["sensor/collector.py"])

    def test_nobody_yet_and_code_that_does_not_parse(self):
        self.assertEqual(team_live(BASE, {}), {"members": [], "pairs": [],
                                               "team": team_live(BASE, {})["team"]})
        out = team_live(BASE, {"alice": work({"sensor/api.py": "def upload(:\n"})})
        self.assertTrue(any("sensor/api.py" in e for e in out["members"][0]["errors"]))

    def test_unchanged_files_are_parsed_once(self):
        base = Snapshot(dict(BASE))
        tree = base.tree("sensor/channel.py")
        mine = derive(base, work(UPLOAD))
        self.assertIs(mine.tree("sensor/channel.py"), tree)
        self.assertIsNot(derive(base, work(LAYOUT)).tree("sensor/channel.py"), tree)

    def test_a_team_of_five_on_a_real_sized_project_is_quick(self):
        # the browser runs this every time someone shares: it must not take long
        base = {f"pkg/m{i}.py": "".join(f"def f{j}(x):\n    return x + {j}\n\n" for j in range(30))
                for i in range(80)}
        works = {f"p{k}": {**base, f"pkg/m{k}.py": base[f"pkg/m{k}.py"] + "def extra(y):\n    return y\n"}
                 for k in range(5)}
        t0 = time.perf_counter()
        out = team_live(base, works)
        self.assertEqual(len(out["pairs"]), 10)
        self.assertLess(time.perf_counter() - t0, 30)


if __name__ == "__main__":
    unittest.main()
