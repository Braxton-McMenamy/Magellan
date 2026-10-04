"""Plan, progress, done: the worklist for a change before it is made, what is left, finished.

The project is legacy code where a search by hand trips: a call split over continuation lines,
a call inside an INCLUDE file, a routine passed as an argument, a local array with the same
name, a look-alike routine, and a COBOL field with the same name in another copybook.
"""

import json
import subprocess
import unittest

from magellan_lite import mcp
from magellan_lite.plan import PlanError, done, plan, progress, text
from tests.helpers import Project


def fixed(*lines):
    return "\n".join(lines) + "\n"


def cobol(pid, copy, field, move):
    return fixed("       IDENTIFICATION DIVISION.", f"       PROGRAM-ID. {pid}.",
                 "       DATA DIVISION.", "       WORKING-STORAGE SECTION.", f"           {copy}",
                 f"       01  {field}.", "       PROCEDURE DIVISION.", "       MAIN-PARA.",
                 f"           {move}", "           STOP RUN.")


FILES = {
    "fort/rate.f": fixed("C     RATE: THE ONE TO CHANGE", "      SUBROUTINE RATE(P, R, X)",
                         "      REAL P, R, X", "      X = P * R", "      END"),
    "fort/use.f": fixed("C     USE", "      SUBROUTINE USE(P, X)", "      REAL P, X",
                        "      CALL RATE(P,", "     &          0.5, X)", "      END"),
    "fort/inc/calls.inc": fixed("      CALL RATE(PB, PR, PX)"),
    "fort/inc1.f": fixed("C     INC1", "      SUBROUTINE INC1(PB, PR, PX)", "      REAL PB, PR, PX",
                         "      INCLUDE 'calls.inc'", "      END"),
    "fort/inc2.f": fixed("C     INC2", "      SUBROUTINE INC2(PB, PR, PX)", "      REAL PB, PR, PX",
                         "      INCLUDE 'calls.inc'", "      END"),
    "fort/apply.f": fixed("C     APPLY", "      SUBROUTINE APPLY(FN, P, R, X)", "      EXTERNAL FN",
                          "      REAL P, R, X", "      CALL FN(P, R, X)", "      END"),
    "fort/pass.f": fixed("C     PASS", "      SUBROUTINE PASS(P, X)", "      REAL P, X",
                         "      EXTERNAL RATE", "      CALL APPLY(RATE, P, 0.1, X)", "      END"),
    "fort/table.f": fixed("C     TABLE: RATE IS A LOCAL ARRAY HERE", "      SUBROUTINE TABLE(S)",
                          "      REAL S, RATE(3)", "      RATE(1) = 1.0", "      S = RATE(1)",
                          "      END"),
    "fort/ratex.f": fixed("C     RATEX", "      SUBROUTINE RATEX(P, X)", "      REAL P, X",
                          "      X = P", "      END"),
    "cobol/copy/ACCTREC.cpy": fixed("       01  ACCT-REC.", "           05  ACCT-BAL      PIC S9(7)V99."),
    "cobol/copy/ACCTOLD.cpy": fixed("       01  OLD-REC.", "           05  ACCT-BAL      PIC S9(5)V99."),
    "cobol/P1.cbl": cobol("P1", "COPY ACCTREC.", "OUT-BAL               PIC S9(7)V99",
                          "MOVE ACCT-BAL TO OUT-BAL."),
    "cobol/P2.cbl": cobol("P2", "COPY ACCTREC.", "WIDE-BAL              PIC S9(13)V99",
                          "MOVE ACCT-BAL TO WIDE-BAL."),
    "cobol/P3.cbl": cobol("P3", "COPY ACCTOLD.", "OLD-OUT               PIC S9(5)V99",
                          "MOVE ACCT-BAL TO OLD-OUT."),
}
EDITS = [{"path": "fort/rate.f", "old": "RATE(P, R, X)\n      REAL P, R, X\n",
          "new": "RATE(P, R, X, B)\n      REAL P, R, X, B\n"},
         {"path": "cobol/copy/ACCTREC.cpy", "old": "PIC S9(7)V99", "new": "PIC S9(11)V99"}]


class Staged(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.put(FILES)
        self.p.git("add", "-A")
        self.p.git("commit", "-q", "-m", "legacy")

    def tearDown(self):
        self.p.__exit__()

    def put(self, files):
        for rel, body in files.items():         # written as is: the columns matter
            path = self.p.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8", newline="\n")

    def edit(self, rel, old, new):
        text_ = (self.p.root / rel).read_text(encoding="utf-8")
        self.assertIn(old, text_)
        self.put({rel: text_.replace(old, new, 1)})

    def test_the_plan_lists_every_place_with_its_code_and_what_it_ruled_out(self):
        r = plan(self.p.root, EDITS)
        where = {i["where"]: i for i in r["edit"]}
        self.assertEqual(set(where), {"fort/use.f:4-5", "fort/inc/calls.inc:1", "fort/apply.f:5",
                                      "cobol/P1.cbl:6"})
        # the call over two lines comes whole; the INCLUDE file once, for both includers
        self.assertEqual(where["fort/use.f:4-5"]["code"],
                         ["      CALL RATE(P,", "     &          0.5, X)"])
        self.assertEqual(where["fort/inc/calls.inc:1"]["included_by"], ["fort/inc1.f:4", "fort/inc2.f:4"])
        self.assertIn("PASS passes RATE to APPLY".lower(), where["fort/apply.f:5"]["note"].lower())
        # the field to widen points at its PIC, with how wide it must be
        self.assertEqual(where["cobol/P1.cbl:6"]["code"], ["       01  OUT-BAL               PIC S9(7)V99."])
        self.assertEqual(where["cobol/P1.cbl:6"]["needs"], "at least 11 integer digits")
        self.assertEqual([x["programs"] for x in r["recompile"]], [["P1", "P2"]])
        self.assertIn("cobol/P2.cbl:9", [u["where"] for u in r["unchanged_uses"]])
        why = {x["why"]: x["where"] for x in r["ruled_out"]}
        self.assertEqual(why["a comment"], ["fort/rate.f:1", "fort/table.f:1"])
        self.assertEqual(why["a local variable or array named RATE, not the routine"],
                         ["fort/table.f:3", "fort/table.f:4", "fort/table.f:5"])
        self.assertIn("cobol/P3.cbl:9", next(w for k, w in why.items() if k.startswith("a different ACCT-BAL")))
        self.assertIn("fort/pass.f:4", next(w for k, w in why.items() if k.startswith("EXTERNAL RATE")))
        self.assertIn("fort/ratex.f:2 RATEX", why["a different definition with a similar name (not changed)"])
        self.assertEqual(r["coverage"]["not_read"], [])
        self.assertIn("|      CALL RATE(P,", text(r))
        # kept in the git directory: the working tree is untouched
        self.assertTrue((self.p.root / ".git" / "magellan-lite" / "plan.json").is_file())
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.p.root,
                                capture_output=True, text=True).stdout
        self.assertEqual(status, "")

    def test_progress_then_done(self):
        plan(self.p.root, EDITS)
        self.assertEqual(progress(self.p.root)["summary"],
                         "planned edits: 0 of 2 made; places to change: 4 left (the plan listed 4).")
        self.edit("fort/rate.f", EDITS[0]["old"], EDITS[0]["new"])
        self.edit("fort/use.f", "0.5, X)", "0.5, X, 1.0)")
        r = progress(self.p.root)
        self.assertEqual(sorted(i["where"] for i in r["left"]),
                         ["cobol/P1.cbl:6", "fort/apply.f:5", "fort/inc/calls.inc:1"])
        self.assertEqual([p["where"] for p in r["planned_not_made"]], ["cobol/copy/ACCTREC.cpy:2"])
        self.assertFalse(done(self.p.root)["done"])
        self.edit("fort/inc/calls.inc", "PX)", "PX, 1.0)")
        self.edit("fort/apply.f", "CALL FN(P, R, X)", "CALL FN(P, R, X, 1.0)")
        self.edit("cobol/copy/ACCTREC.cpy", "S9(7)V99", "S9(11)V99")
        self.edit("cobol/P1.cbl", "PIC S9(7)V99", "PIC S9(11)V99")
        r = done(self.p.root)
        self.assertTrue(r["done"], r)
        self.assertEqual(r["changed"]["fort/use.f"], "5")
        self.assertEqual(r["recompile"][0]["programs"], ["P1", "P2"])
        self.assertEqual(len(r["planned_items"]), 4)

    def test_an_edit_that_names_no_single_place_is_refused_plainly(self):
        with self.assertRaisesRegex(PlanError, "not in fort/rate.f"):
            plan(self.p.root, [{"path": "fort/rate.f", "old": "SUBROUTINE RATE(P, Q)", "new": "x"}])
        with self.assertRaisesRegex(PlanError, "occurs 2 times"):
            plan(self.p.root, [{"path": "fort/inc1.f", "old": "PB", "new": "QB"}])
        with self.assertRaisesRegex(PlanError, "no plan for this project yet"):
            progress(self.p.root)

    def test_the_agent_tools(self):
        s = mcp.Server(self.p.root)
        out, err = s.call("magellan_lite_plan", {"edits": EDITS})
        self.assertFalse(err, out)
        self.assertEqual(json.loads(out)["summary"],
                         "4 places to change in 4 files; 2 programs to recompile; 2 uses that need "
                         "nothing; 9 mentions ruled out.")
        out, err = s.call("magellan_lite_progress", {})
        self.assertEqual((json.loads(out)["stage"], err), ("progress", False))
        out, err = s.call("magellan_lite_done", {})
        self.assertFalse(json.loads(out)["done"])
        out, err = s.call("magellan_lite_plan", {"edits": "rate.f"})
        self.assertTrue(err)
        self.assertIn("edits must be a list", json.loads(out)["error"])


if __name__ == "__main__":
    unittest.main()
