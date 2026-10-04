"""The other languages: each one's change caught the way the full Magellan catches it, and
Python 2 still on the map. ``check_files`` takes ``{path: source}``, so no git is needed."""

import unittest

from magellan_lite import languages
from magellan_lite.engine import check_files

C_PARSE = "int parse(const char *s, int len) {\n    return len;\n}\n"
C_LOAD = '#include "parse.h"\nint load(void) {\n    return parse("x", 1);\n}\n'


def rules(report) -> list[tuple[str, str]]:
    return [(f.rule, f.path) for f in report.findings]


class Languages(unittest.TestCase):
    def test_c_a_parameter_added_breaks_the_untouched_caller(self):
        r = check_files({"parse.c": C_PARSE, "load.c": C_LOAD},
                        {"parse.c": C_PARSE.replace("int len)", "int len, int flags)"),
                         "load.c": C_LOAD})
        self.assertEqual((r.verdict, rules(r)), ("block", [("signature-break", "load.c")]))
        self.assertIn("calls parse.parse the old way", r.findings[0].message)
        self.assertEqual([a["name"] for a in r.affected], ["c@load.load"])   # the blast radius

    def test_c_a_function_deleted_while_still_called(self):
        use = "int run(void) { return helper(); }\n"
        r = check_files({"parse.c": C_PARSE + "int helper(void) { return 1; }\n", "run.c": use},
                        {"parse.c": C_PARSE, "run.c": use})
        self.assertEqual(rules(r), [("removed-still-referenced", "run.c")])

    def test_c_goto_fail(self):
        code = ("static int check(int a, int b) {\n    int err;\n    if ((err = a) != 0)\n"
                "        goto fail;\n    if ((err = b) != 0)\n        goto fail;\n    err = 0;\n"
                "fail:\n    return err;\n}\n")
        broken = code.replace("        goto fail;\n    err = 0;",
                              "        goto fail;\n        goto fail;\n    err = 0;")
        r = check_files({"ssl.c": code}, {"ssl.c": broken})
        self.assertEqual((r.verdict, rules(r)), ("block", [("unreachable-statement", "ssl.c")]))

    def test_java_an_overload_that_gained_a_parameter(self):
        shape = ("package geo;\npublic class Shape {\n    public double area(double w, double h) "
                 "{ return w * h; }\n}\n")
        use = "package geo;\npublic class Use {\n    double sq(Shape s, double x) { return s.area(x, x); }\n}\n"
        r = check_files({"geo/Shape.java": shape, "geo/Use.java": use},
                        {"geo/Shape.java": shape.replace("double h)", "double h, double d)"),
                         "geo/Use.java": use})
        self.assertEqual(r.verdict, "block")
        self.assertEqual([(c.kind, c.name) for c in r.changes if "area" in c.name],
                         [("signature", "java@geo.Shape.area(double,double,double)")])
        self.assertEqual([p for _, p in rules(r)], ["geo/Use.java"])

    def test_fortran_77_an_argument_added(self):
        sub = "      SUBROUTINE ADDUP(A, B, C)\n      REAL A, B, C\n      C = A + B\n      END\n"
        main = "      PROGRAM MAIN\n      REAL X\n      CALL ADDUP(1.0, 2.0, X)\n      END\n"
        r = check_files({"addup.f": sub, "main.f": main},
                        {"addup.f": sub.replace("A, B, C)", "A, B, C, D)").replace("A, B, C\n", "A, B, C, D\n"),
                         "main.f": main})
        self.assertEqual((r.verdict, rules(r)), ("block", [("signature-break", "main.f")]))

    def test_cobol_a_copybook_field_widened(self):
        cpy = "       01 CUSTOMER-REC.\n           05 CUST-ID    PIC 9(6).\n           05 CUST-NAME  PIC X(30).\n"
        prog = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. BILLING.\n       DATA DIVISION.\n"
                "       WORKING-STORAGE SECTION.\n       COPY CUSTREC.\n       PROCEDURE DIVISION.\n"
                "       MAIN-PARA.\n           DISPLAY CUST-ID.\n           STOP RUN.\n")
        r = check_files({"CUSTREC.cpy": cpy, "BILLING.cbl": prog},
                        {"CUSTREC.cpy": cpy.replace("9(6)", "9(8)"), "BILLING.cbl": prog})
        self.assertEqual((r.verdict, rules(r)), ("block", [("copybook-layout-changed", "CUSTREC.cpy")]))
        self.assertIn("cobol@BILLING", [a["name"] for a in r.affected])

    def test_the_blast_radius_crosses_languages(self):
        java = ("package app;\npublic class Native {\n    public static native int crc(byte[] data);\n"
                "    public int sum(byte[] d) { return crc(d); }\n}\n")
        c = ("#include <jni.h>\nJNIEXPORT jint JNICALL Java_app_Native_crc(JNIEnv *env, jclass k, "
             "jbyteArray data) {\n    return 0;\n}\n")
        r = check_files({"app/Native.java": java, "crc.c": c},
                        {"app/Native.java": java, "crc.c": c.replace("return 0;", "return 1;")})
        self.assertEqual([a["name"] for a in r.affected],
                         ["java@app.Native.crc(byte[])", "java@app.Native.sum(byte[])"])

    def test_python_2_is_read_and_checked(self):
        old = "def greet(name):\n    print 'hello', name\n\ndef main():\n    greet('x')\n"
        r = check_files({"old.py": old}, {"old.py": old.replace("(name):", "(name, punct):")})
        self.assertEqual(r.errors, [])
        self.assertIn(("signature-break", "old.py"), rules(r))

    def test_a_python_only_project_never_loads_the_other_languages(self):
        import subprocess
        import sys
        code = ("import sys; from magellan_lite.engine import check_files\n"
                "check_files({'a.py': 'def f(x):\\n    return x\\n'}, {'a.py': 'def f(x, y):\\n    return x\\n'})\n"
                "print(any(m.startswith('magellan_lite.polyglot') for m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "False")

    def test_every_suffix_the_frontends_read_is_collected(self):
        from magellan_lite.polyglot.analyze.frontends import all_frontends
        declared = {s for _, mod in all_frontends() for s in mod.SOURCE_SUFFIXES}
        self.assertEqual(declared, set(languages.SUFFIXES))


class FoundByTheModernizationExperiment(unittest.TestCase):
    """Three places a legacy upgrade went unchecked, found by two agents racing on a ticket."""

    def test_fortran_a_routine_passed_as_an_argument_is_followed_into_its_calls(self):
        sub = "      SUBROUTINE INTRST(P, R, D, X)\n      REAL P, R, D, X\n      X = P * R * D / 360.0\n      END\n"
        apply = ("      SUBROUTINE APPLY(FN, P, R, D, X)\n      EXTERNAL FN\n      REAL P, R, D, X\n"
                 "      CALL FN(P, R, D, X)\n      END\n")
        batch = ("      SUBROUTINE BATCH(P, R, D, X)\n      REAL P, R, D, X\n      EXTERNAL INTRST\n"
                 "      CALL APPLY(INTRST, P, R, D, X)\n      END\n")
        new = sub.replace("(P, R, D, X)\n      REAL P, R, D, X\n", "(P, R, D, X, B)\n      REAL P, R, D, X, B\n")
        r = check_files({"interest.f": sub, "apply.f": apply, "batch.f": batch},
                        {"interest.f": new, "apply.f": apply, "batch.f": batch})
        self.assertEqual(rules(r), [("signature-break", "apply.f")])
        self.assertEqual(r.findings[0].line, 4)
        self.assertIn("through its procedure argument FN", r.findings[0].message)
        # and on the map APPLY calls INTRST, so it is among what the change reaches
        self.assertIn("fortran@apply", [a["name"] for a in r.affected])
        # a wrapper with the old argument list is the fix, and then nothing is left to report
        fixed = batch.replace("EXTERNAL INTRST", "EXTERNAL INT360").replace("APPLY(INTRST", "APPLY(INT360") + \
            "      SUBROUTINE INT360(P, R, D, X)\n      REAL P, R, D, X\n      CALL INTRST(P, R, D, X, 360.0)\n      END\n"
        r = check_files({"interest.f": sub, "apply.f": apply, "batch.f": batch},
                        {"interest.f": new, "apply.f": apply, "batch.f": fixed})
        self.assertNotIn("signature-break", [f.rule for f in r.findings])

    COPY = "       01 ACCT-REC.\n           05 ACCT-BAL      PIC S9(7)V99.\n"

    def program(self, copy, field, move):
        return ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. P1.\n       DATA DIVISION.\n"
                f"       WORKING-STORAGE SECTION.\n           {copy}\n       01 {field}.\n"
                f"       PROCEDURE DIVISION.\n       MAIN-PARA.\n           {move}\n           STOP RUN.\n")

    def widened(self, program):
        before = {"ACCTREC.cpy": self.COPY, "P1.cbl": program}
        after = {**before, "ACCTREC.cpy": self.COPY.replace("S9(7)V99", "S9(11)V99")}
        return [(f.rule, f.message) for f in check_files(before, after).findings if f.rule == "move-truncates"]

    def test_cobol_a_prefix_replaced_by_copy_replacing_still_counts(self):
        # ==ACCT-== can't be a whole COBOL word (none ends in a hyphen): it is a prefix
        p = self.program("COPY ACCTREC REPLACING ==ACCT-== BY ==ARC-==.", "ARC-OUT PIC S9(7)V99",
                         "MOVE ARC-BAL TO ARC-OUT.")
        self.assertEqual(self.widened(p), [("move-truncates", "MOVE ARC-BAL TO ARC-OUT now drops 4 high-order digits")])

    def test_cobol_an_edited_picture_counts_its_digits_to_the_decimal_point(self):
        p = self.program("COPY ACCTREC.", "PRT-BAL PIC -Z,ZZZ,ZZ9.99", "MOVE ACCT-BAL TO PRT-BAL.")
        self.assertEqual(self.widened(p), [("move-truncates", "MOVE ACCT-BAL TO PRT-BAL now drops 4 high-order digits")])
        wide = self.program("COPY ACCTREC.", "PRT-BAL PIC -ZZ,ZZZ,ZZZ,ZZ9.99", "MOVE ACCT-BAL TO PRT-BAL.")
        self.assertEqual(self.widened(wide), [])

    def test_cobol_a_move_that_already_truncated_and_now_loses_more_digits(self):
        # ZZZ,ZZ9.99 took 1 digit off the 7-digit balance; after the widening it takes 5
        short = self.program("COPY ACCTREC.", "PRT-BAL PIC ZZZ,ZZ9.99", "MOVE ACCT-BAL TO PRT-BAL.")
        self.assertEqual(self.widened(short),
                         [("move-truncates", "MOVE ACCT-BAL TO PRT-BAL now drops 5 high-order digits "
                                             "(it dropped 1 before)")])
        # text cut short on purpose (a prefix) stays quiet, however much longer the source grows
        copy = self.COPY.replace("05 ACCT-BAL      PIC S9(7)V99.", "05 ACCT-NAME     PIC X(30).")
        p = self.program("COPY ACCTREC.", "SHORT-NAME PIC X(10)", "MOVE ACCT-NAME TO SHORT-NAME.")
        before = {"ACCTREC.cpy": copy, "P1.cbl": p}
        r = check_files(before, {**before, "ACCTREC.cpy": copy.replace("X(30)", "X(40)")})
        self.assertNotIn("move-truncates", [f.rule for f in r.findings])

    def test_cobol_every_program_that_copies_the_copybook_is_named(self):
        # at 40 programs, "recompile every program listed" listed none, and the copybook
        # itself could not be looked up: a program that copies it through another copybook
        # and never names the changed field was nowhere in the report
        view = "           COPY ACCTREC.\n       01 ACCT-VIEW.\n           05 VIEW-FLAG PIC X.\n"
        p1 = self.program("COPY ACCTREC.", "OUT-BAL PIC S9(13)V99", "MOVE ACCT-BAL TO OUT-BAL.")
        p2 = p1.replace("P1.", "P2.").replace("COPY ACCTREC.", "COPY ACCTVIEW.") \
            .replace("MOVE ACCT-BAL TO OUT-BAL.", "DISPLAY VIEW-FLAG.")
        before = {"ACCTREC.cpy": self.COPY, "ACCTVIEW.cpy": view, "P1.cbl": p1, "P2.cbl": p2}
        r = check_files(before, {**before, "ACCTREC.cpy": self.COPY.replace("S9(7)V99", "S9(11)V99")})
        layout = [f for f in r.findings if f.rule == "copybook-layout-changed"]
        self.assertEqual(len(layout), 1)
        self.assertIn("Copied by 2 program(s): P1, P2.", layout[0].detail)
        self.assertIn("cobol@P2", [a["name"] for a in r.affected])
        self.assertIn("cobol@copy:ACCTREC", [c.name for c in r.changes])


if __name__ == "__main__":
    unittest.main()
