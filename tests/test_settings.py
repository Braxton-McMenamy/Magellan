"""A project's settings from pyproject.toml's [tool.magellan-lite]: fail-on, disable, exclude."""

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from magellan_lite import settings
from magellan_lite.cli import main
from tests.helpers import Project

NEEDS_TOMLLIB = unittest.skipIf(sys.version_info < (3, 11), "tomllib is Python 3.11 and later")

MUTABLE = "def f(x=[]):\n    return x\n"


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def project_with(pyproject: str, files: dict) -> Project:
    p = Project()
    p.commit({"README.md": "x\n"})
    p.write({"pyproject.toml": pyproject, **files})
    return p


@NEEDS_TOMLLIB
class FromPyproject(unittest.TestCase):
    def check(self, p, *flags):
        code, out, err = run_cli("check", str(p.root), "--format", "json", *flags)
        return code, json.loads(out) if out else None, err

    def test_fail_on_and_the_command_line_beats_it(self):
        with project_with('[tool.magellan-lite]\nfail-on = "review"\n', {"m.py": MUTABLE}) as p:
            code, report, _ = self.check(p)
            self.assertEqual((code, report["verdict"]), (1, "review"))
            code, _report, _ = self.check(p, "--fail-on", "block")
            self.assertEqual(code, 0)

    def test_a_disabled_rule_says_nothing(self):
        with project_with('[tool.magellan-lite]\ndisable = ["mutable-default-argument"]\n',
                          {"m.py": MUTABLE}) as p:
            _code, report, err = self.check(p)
            self.assertNotIn("mutable-default-argument", [f["rule"] for f in report["findings"]])
            self.assertEqual(err, "")

    def test_excluded_paths_are_not_reported(self):
        with project_with('[tool.magellan-lite]\nexclude = ["gen/", "*_pb2.py"]\n',
                          {"gen/a.py": MUTABLE, "api/b_pb2.py": MUTABLE,
                           "app/c.py": MUTABLE}) as p:
            _code, report, _ = self.check(p)
            paths = {f["path"] for f in report["findings"]}
            self.assertEqual(paths, {"app/c.py"})
            self.assertEqual(report["files_changed"], ["app/c.py"])

    def test_settings_above_a_project_in_a_subdirectory(self):
        # pyproject.toml at the repository's top; the check runs on svc/, so the pattern
        # svc/gen/ still means svc/gen/ and not svc/svc/gen/
        with project_with('[tool.magellan-lite]\nexclude = ["svc/gen/"]\n',
                          {"svc/gen/a.py": MUTABLE, "svc/app.py": MUTABLE}) as p:
            code, out, _ = run_cli("check", str(p.root / "svc"), "--format", "json")
            self.assertEqual({f["path"] for f in json.loads(out)["findings"]}, {"app.py"})

    def test_mistakes_are_explained(self):
        with project_with('[tool.magellan-lite]\nfail-on = "sometimes"\n', {"m.py": MUTABLE}) as p:
            code, _out, err = run_cli("check", str(p.root))
            self.assertEqual(code, 2)
            self.assertIn("fail-on must be one of review, block, never", err)
        with project_with('[tool.magellan-lite]\ndisable = ["no-such-rule"]\ndisabled = []\n',
                          {"m.py": MUTABLE}) as p:
            code, _out, err = run_cli("check", str(p.root))
            self.assertEqual(code, 0)                      # a warning, not a stop
            self.assertIn("no rule called 'no-such-rule'", err)
            self.assertIn("unknown setting 'disabled'", err)
        with project_with("[tool.magellan-lite\n", {"m.py": MUTABLE}) as p:
            code, _out, err = run_cli("check", str(p.root))
            self.assertEqual(code, 2)
            self.assertIn("cannot read", err)

    def test_no_table_means_the_defaults(self):
        with project_with('[project]\nname = "x"\n', {"m.py": MUTABLE}) as p:
            s = settings.load(p.root)
            self.assertEqual((s.fail_on, s.disable, s.exclude, s.warnings), (None, (), (), []))


class WithoutTomllib(unittest.TestCase):
    def test_python_3_10_falls_back_to_the_defaults_quietly(self):
        with project_with('[tool.magellan-lite]\nfail-on = "review"\n', {"m.py": MUTABLE}) as p, \
                mock.patch.dict(sys.modules, {"tomllib": None}):      # import tomllib fails
            self.assertEqual(settings.load(p.root), settings.Settings())
            code, _out, err = run_cli("check", str(p.root))
            self.assertEqual((code, err), (0, ""))       # review does not fail by default


class Excluded(unittest.TestCase):
    def test_patterns(self):
        s = settings.Settings(exclude=("migrations", "docs/*.py", "./gen/", "win\\path"))
        self.assertTrue(s.is_excluded("migrations/0001_initial.py"))
        self.assertTrue(s.is_excluded("docs/a/b.py"))
        self.assertTrue(s.is_excluded("gen/x.py"))
        self.assertTrue(s.is_excluded("win/path/x.py"))
        self.assertFalse(s.is_excluded("app/migrations.py"))
        self.assertFalse(s.is_excluded("generated/x.py"))


if __name__ == "__main__":
    unittest.main()
