"""The brief: the check cut down for an AI agent, as JSON and as text."""

import contextlib
import io
import json
import unittest

from magellan_lite.brief import brief, is_test_file, text
from magellan_lite.cli import main
from tests.helpers import Project

BASE = {
    "app/__init__.py": "",
    "app/pay.py": """
        def charge(amount):
            return amount
        """,
    "app/shop.py": """
        from app.pay import charge


        def checkout(cart):
            return charge(sum(cart))


        def page(cart):
            return checkout(cart)
        """,
    "tests/test_pay.py": """
        import unittest

        from app.pay import charge


        class Charge(unittest.TestCase):
            def test_amount(self):
                self.assertEqual(charge(3, "EUR"), 3)
        """,
    "tests/test_other.py": """
        def test_nothing_to_do_with_it():
            assert True
        """,
    "tools/report.py": """
        def summary():
            return "uses charge in a string only"
        """,
}
# a required parameter, and the caller in app/shop.py not updated
SIGNATURE = {"app/pay.py": """
    def charge(amount, currency):
        return amount
    """}

KEYS = ["verdict", "summary", "against", "counts", "changes", "do_first", "reaches",
        "check_these_files", "tests_to_run", "errors", "omitted"]


class Brief(unittest.TestCase):
    def test_a_signature_change_blocks_and_names_the_caller_its_file_and_its_test(self):
        with Project() as p:
            p.commit(BASE)
            p.write(SIGNATURE)
            b = brief(p.root)
        self.assertEqual(list(b), KEYS)
        self.assertEqual(b["verdict"], "block")
        self.assertTrue(b["summary"].startswith("Do not commit yet"), b["summary"])
        self.assertEqual(b["changes"], [{"name": "app.pay.charge", "kind": "signature",
                                         "where": "app/pay.py:1",
                                         "detail": "(amount) -> (amount, currency)"}])
        first = b["do_first"][0]
        self.assertEqual((first["severity"], first["rule"], first["where"]),
                         ("critical", "signature-break", "app/shop.py:5"))
        self.assertIn("app.shop.checkout", first["what"])
        self.assertTrue(first["fix"])
        self.assertEqual(b["counts"]["findings"]["critical"], 1)    # the test passes currency
        self.assertEqual([a["name"] for a in b["reaches"]][:1], ["app.shop.checkout"])
        self.assertEqual(b["reaches"][0]["hops"], 1)
        files = [f["path"] for f in b["check_these_files"]]
        self.assertEqual(files[0], "app/shop.py")
        self.assertNotIn("app/pay.py", files)                       # the agent edited that one
        self.assertIn("signature-break at line 5", b["check_these_files"][0]["why"])
        tests = [t["path"] for t in b["tests_to_run"]]
        self.assertEqual(tests, ["tests/test_pay.py"])              # not test_other, not tools/
        self.assertIn("charge", b["tests_to_run"][0]["why"])
        self.assertEqual(b["errors"], [])
        json.dumps(b)                                               # plain JSON all the way

    def test_the_limit_cuts_every_list_and_counts_what_it_cut(self):
        callers = {f"app/use{i}.py": f"""
            from app.pay import charge


            def use{i}():
                return charge(1)
            """ for i in range(3)}
        with Project() as p:
            p.commit({**BASE, **callers})
            p.write(SIGNATURE)
            full, short = brief(p.root), brief(p.root, limit=1)
        self.assertEqual(len(full["do_first"]), 4)                  # shop + three use<i>
        self.assertEqual(len(short["do_first"]), 1)
        self.assertEqual(short["omitted"]["do_first"], 3)
        self.assertEqual(len(short["reaches"]), 1)
        self.assertEqual(short["omitted"]["reaches"], len(full["reaches"]) - 1)
        self.assertEqual(len(short["check_these_files"]), 1)
        self.assertEqual(short["counts"], full["counts"])           # counts are never cut
        self.assertEqual(short["do_first"][0], full["do_first"][0])

    def test_text_is_terse_and_has_every_part(self):
        with Project() as p:
            p.commit(BASE)
            p.write(SIGNATURE)
            out = text(brief(p.root))
        self.assertTrue(out.startswith("magellan-lite brief: BLOCK"), out)
        for part in ("do first", "1. CRITICAL signature-break  app/shop.py:5", "fix:",
                     "check these files", "tests to run", "tests/test_pay.py"):
            self.assertIn(part, out)

    def test_a_safe_change_is_ok_and_lists_nothing_to_do(self):
        with Project() as p:
            p.commit(BASE)
            p.write({"app/pay.py": """
                def charge(amount):
                    return round(amount, 2)
                """})
            b = brief(p.root)
        self.assertEqual(b["verdict"], "ok")
        self.assertTrue(b["summary"].startswith("OK to commit"), b["summary"])
        self.assertEqual(b["do_first"], [])
        self.assertEqual(b["changes"][0]["kind"], "body")
        self.assertIn("tests/test_pay.py", [t["path"] for t in b["tests_to_run"]])

    def test_no_change_says_so(self):
        with Project() as p:
            p.commit(BASE)
            b = brief(p.root)
        self.assertEqual(b["verdict"], "ok")
        self.assertTrue(b["summary"].startswith("Nothing to check"), b["summary"])
        self.assertEqual((b["changes"], b["reaches"], b["tests_to_run"]), ([], [], []))

    def test_a_common_name_needs_its_module_named_too(self):
        base = {"app/__init__.py": "", "app/store.py": "def get(key):\n    return key\n",
                "tests/test_cache.py": "def test_get():\n    assert {}.get(1) is None\n",
                "tests/test_store.py": ("from app import store\n\n\n"
                                        "def test_get():\n    assert store.get(1) == 1\n")}
        with Project() as p:
            p.commit(base)
            p.write({"app/store.py": "def get(key):\n    return str(key)\n"})
            b = brief(p.root)
        self.assertEqual([t["path"] for t in b["tests_to_run"]], ["tests/test_store.py"])

    def test_the_command_line_prints_json_or_text_and_exits_like_check(self):
        with Project() as p:
            p.commit(BASE)
            p.write(SIGNATURE)
            runs = {}
            for argv in (["brief", str(p.root)],
                         ["brief", str(p.root), "--format", "text", "--limit", "2"],
                         ["brief", str(p.root), "--fail-on", "never"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = main(argv)
                runs[len(runs)] = (code, out.getvalue())
            with contextlib.redirect_stderr(io.StringIO()):
                not_a_project = main(["brief", str(p.root / "nowhere")])
        self.assertEqual(runs[0][0], 1)
        self.assertEqual(json.loads(runs[0][1])["verdict"], "block")
        self.assertEqual(runs[1][0], 1)
        self.assertTrue(runs[1][1].startswith("magellan-lite brief: BLOCK"))
        self.assertEqual(runs[2][0], 0)
        self.assertEqual(not_a_project, 2)


class TestFiles(unittest.TestCase):
    def test_test_files_are_found_by_the_names_runners_use(self):
        for path in ("tests/test_pay.py", "test_pay.py", "pkg/pay_test.py", "tests.py",
                     "src/test/java/geo/ShapeTest.java", "web/cart.spec.ts",
                     "web/__tests__/cart.ts", "c/test_parse.c", "cobol/TESTCALC.cbl"):
            self.assertTrue(is_test_file(path), path)
        for path in ("app/pay.py", "tests/helpers.py", "tests/__init__.py", "conftest.py",
                     "src/main/java/geo/Shape.java", "web/cart.ts", "c/parse.c",
                     "app/contest.py"):
            self.assertFalse(is_test_file(path), path)


if __name__ == "__main__":
    unittest.main()
