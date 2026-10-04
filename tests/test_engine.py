"""End to end on real git repositories: what a pre-commit check would say."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from magellan_lite.cli import main
from magellan_lite.findings import Finding, RULES, change_rule, rule, run_file_rules
from magellan_lite.git import GitError
from tests.helpers import Project

PAY = """
    def charge(amount):
        return amount


    def refund(amount, log=[]):
        log.append(amount)
        return -amount
    """


class Check(unittest.TestCase):
    def test_only_what_the_change_touched_is_checked(self):
        with Project() as p:
            p.commit({"app/pay.py": PAY})
            # refund() already had a mutable default: an old problem, not this change's
            p.write({"app/pay.py": PAY.replace("return amount\n", "return amount * 2\n")})
            r = p.check()
            self.assertEqual([c.name for c in r.changes], ["app.pay.charge"])
            self.assertEqual(r.findings, [])
            self.assertEqual(r.verdict, "ok")

            p.write({"app/pay.py": PAY.replace("log.append(amount)", "log.append(-amount)")})
            r = p.check()
            self.assertEqual([f.rule for f in r.findings], ["mutable-default-argument"])
            self.assertEqual((r.findings[0].path, r.findings[0].line), ("app/pay.py", 5))
            self.assertEqual(r.verdict, "review")

    def test_a_new_file_is_checked_in_full(self):
        with Project() as p:
            p.commit({"README.md": "x\n"})
            p.write({"app/new.py": "def f(x={}):\n    return x\n"})
            r = p.check()
            self.assertEqual([c.kind for c in r.changes], ["added"])
            self.assertEqual([f.rule for f in r.findings], ["mutable-default-argument"])

    def test_against_a_directory(self):
        with Project() as old, Project() as new:
            old.write({"m.py": "def f(x):\n    return x\n"})
            new.write({"m.py": "def f(x, y):\n    return x\n"})
            r = new.check(against=str(old.root))
            self.assertEqual([(c.name, c.kind) for c in r.changes], [("m.f", "signature")])

    def test_every_kind_of_change_reads_as_json(self):
        # a deleted definition has no "after" and a new one no "before": to_dict copes with both
        with Project() as old, Project() as new:
            old.write({"m.py": "X = 1\n\n\ndef gone():\n    return 1\n\n\ndef f(x):\n    return x\n"})
            new.write({"m.py": "X = 2\n\n\ndef fresh():\n    return 2\n\n\n"
                               "def f(x, y):\n    return x\n"})
            report = json.loads(json.dumps(new.check(against=str(old.root)).to_dict()))
            detail = {c["kind"]: c["detail"] for c in report["changes"]}
            self.assertEqual(detail["removed"], "function deleted")
            self.assertEqual(detail["added"], "new function")
            self.assertIn("->", detail["signature"])
            self.assertIn("->", detail["value"])

    def test_a_project_in_a_subdirectory_of_the_repository(self):
        with Project() as p:
            p.commit({"svc/app/pay.py": "def f():\n    return 1\n", "other/x.py": "Y = 1\n"})
            p.write({"svc/app/pay.py": "def f():\n    return 2\n"})
            from magellan_lite.engine import check
            r = check(p.root / "svc")
            self.assertEqual([c.name for c in r.changes], ["app.pay.f"])

    def test_non_ascii_names_and_content(self):
        with Project() as p:
            p.commit({"café/menü.py": "NAME = 'Łódź'\n"})
            p.write({"café/menü.py": "NAME = 'Kraków'\n"})
            r = p.check()
            self.assertEqual([(c.name, c.kind) for c in r.changes], [("café.menü.NAME", "value")])

    def test_crlf_working_tree_against_lf_history_is_no_change(self):
        with Project() as p:
            p.commit({"m.py": "def f():\n    return 1\n"})
            p.write({"m.py": "def f():\n    return 1\n"}, newline="\r\n")
            self.assertEqual(p.check().changes, [])

    def test_a_file_that_does_not_parse_is_reported_not_fatal(self):
        with Project() as p:
            p.commit({"m.py": "def f():\n    return 1\n"})
            p.write({"m.py": "def f(:\n", "ok.py": "def g(x=[]):\n    return x\n"})
            r = p.check()
            self.assertTrue(any("m.py:1" in e for e in r.errors), r.errors)
            self.assertEqual([f.rule for f in r.findings], ["mutable-default-argument"])

    def test_no_commits_yet_is_a_clear_error(self):
        with Project() as p:
            p.write({"m.py": "X = 1\n"})
            with self.assertRaises(GitError) as ctx:
                p.check()
            self.assertIn("cannot read revision 'HEAD'", str(ctx.exception))


class Registry(unittest.TestCase):
    def tearDown(self):
        for name in [n for n in RULES if n.startswith("test-")]:
            del RULES[name]

    def test_a_rule_can_yield_a_line_number_and_a_detail(self):
        @rule("test-line", "low", fix="do x")
        def by_line(tree, path):
            yield 7, "seven", "because"
        import ast
        found, errors = run_file_rules(ast.parse(""), "m.py")
        mine = [f for f in found if f.rule == "test-line"]
        self.assertEqual([(f.line, f.message, f.detail, f.fix) for f in mine],
                         [(7, "seven", "because", "do x")])
        self.assertEqual(errors, [])

    def test_a_crashing_rule_is_reported_not_fatal(self):
        @rule("test-crash", "low")
        def crash(tree, path):
            raise RuntimeError("boom")
            yield
        import ast
        _found, errors = run_file_rules(ast.parse(""), "m.py")
        self.assertTrue(any("test-crash" in e and "boom" in e for e in errors), errors)

    def test_duplicate_ids_and_unknown_severities_are_refused(self):
        with self.assertRaises(ValueError):
            rule("test-x", "urgent")
        rule("test-dup", "low")(lambda t, p: [])
        with self.assertRaises(ValueError):
            rule("test-dup", "low")(lambda t, p: [])

    def test_a_blocking_change_rule_blocks(self):
        @change_rule("test-block", "high", blocking=True)
        def always(ctx):
            return [Finding("test-block", "high", "stop", "m.py", 1)] if ctx.changes else []
        with Project() as p:
            p.commit({"m.py": "X = 1\n"})
            p.write({"m.py": "X = 2\n"})
            self.assertEqual(p.check().verdict, "block")


class Cli(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_json_and_exit_codes(self):
        with Project() as p:
            p.commit({"m.py": "def f(x):\n    return x\n"})
            p.write({"m.py": "def f(x=[]):\n    return x\n"})
            code, out, _ = self.run_cli("check", str(p.root), "--format", "json")
            data = json.loads(out)
            self.assertEqual((code, data["verdict"]), (0, "review"))      # fails only on block
            self.assertEqual(data["findings"][0]["rule"], "mutable-default-argument")
            code, _out, _ = self.run_cli("check", str(p.root), "--fail-on", "review")
            self.assertEqual(code, 1)
            self.assertNotIn("map", data)                           # only when asked
            _code, out, _ = self.run_cli("check", str(p.root), "--format", "json", "--map")
            nodes = {n["id"]: n for n in json.loads(out)["map"]["nodes"]}
            self.assertEqual(nodes["m.f"]["change"], "signature")

    def test_text_output_reads_as_a_checklist(self):
        with Project() as p:
            p.commit({"m.py": "def f(x):\n    return x\n"})
            p.write({"m.py": "def f(x={}):\n    return x\n"})
            _code, out, _ = self.run_cli("check", str(p.root))
            self.assertIn("REVIEW", out)
            self.assertIn("[ ] MEDIUM", out)

    def test_cannot_run_exits_2_with_a_message(self):
        with Project() as p:
            code, _out, err = self.run_cli("check", str(p.root))
            self.assertEqual(code, 2)
            self.assertIn("cannot read revision", err)

    def test_rules_lists_the_checklist(self):
        code, out, _ = self.run_cli("rules")
        self.assertEqual(code, 0)
        self.assertIn("mutable-default-argument", out)


if __name__ == "__main__":
    unittest.main()
