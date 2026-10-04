"""`magellan-lite hook install`: git's pre-commit hook runs the check, and a blocking change
cannot be committed."""

import io
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import magellan_lite
from magellan_lite.cli import main
from magellan_lite.hook import MARKER
from tests.helpers import Project

#: so the hook's ``python -m magellan_lite`` finds this copy of the code, installed or not
REPO = str(Path(magellan_lite.__file__).resolve().parents[1])

BEFORE = """
    def charge(amount):
        return amount


    def pay():
        return charge(1)
    """


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Hook(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        # a global core.hooksPath (shared hooks) must not catch this throwaway repository's hook
        self.p.git("config", "core.hooksPath", (self.p.root / ".git" / "hooks").as_posix())
        self.hook = self.p.root / ".git" / "hooks" / "pre-commit"

    def tearDown(self):
        self.p.__exit__(None, None, None)

    def commit(self, message: str) -> subprocess.CompletedProcess:
        self.p.git("add", "-A")
        env = {**os.environ, "PYTHONPATH": REPO + os.pathsep + os.environ.get("PYTHONPATH", "")}
        return subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.p.root, env=env,
                              capture_output=True, encoding="utf-8", errors="replace")

    def test_committing_a_blocking_change_is_refused(self):
        self.p.commit({"app/pay.py": BEFORE})
        code, out, _ = run_cli("hook", "install", str(self.p.root))
        self.assertEqual(code, 0)
        self.assertIn("installed the pre-commit hook", out)
        self.assertNotIn(b"\r", self.hook.read_bytes())           # sh wants LF, on Windows too

        # pay() still calls charge() with one argument: a TypeError waiting to happen
        self.p.write({"app/pay.py": BEFORE.replace("(amount)", "(amount, currency)")})
        r = self.commit("break pay")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("BLOCK", r.stdout + r.stderr)
        self.assertIn("signature-break", r.stdout + r.stderr)

        # a change that breaks nothing goes through
        self.p.write({"app/pay.py": BEFORE.replace("return amount", "return amount * 2")})
        r = self.commit("double it")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_someone_elses_hook_is_left_alone_unless_forced(self):
        self.p.commit({"m.py": "X = 1\n"})
        theirs = "#!/bin/sh\necho their hook\n"
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        self.hook.write_text(theirs, encoding="utf-8", newline="\n")

        code, _out, err = run_cli("hook", "install", str(self.p.root))
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(self.hook.read_text(encoding="utf-8"), theirs)

        code, out, _ = run_cli("hook", "install", str(self.p.root), "--force")
        self.assertEqual(code, 0)
        self.assertIn("pre-commit.bak", out)
        self.assertIn(MARKER, self.hook.read_text(encoding="utf-8"))
        self.assertEqual((self.hook.parent / "pre-commit.bak").read_text(encoding="utf-8"), theirs)

        # installing again over our own hook needs no --force
        code, _out, _err = run_cli("hook", "install", str(self.p.root))
        self.assertEqual(code, 0)

    def test_a_project_in_a_subdirectory_is_the_one_checked(self):
        self.p.commit({"svc/m.py": "X = 1\n"})
        code, _out, _err = run_cli("hook", "install", str(self.p.root / "svc"))
        self.assertEqual(code, 0)
        self.assertIn("-m magellan_lite check svc\n", self.hook.read_text(encoding="utf-8"))


class NotARepository(unittest.TestCase):
    def test_says_so(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(Path(d).parent)}):
            # the ceiling stops git looking for a repository above the folder
            code, _out, err = run_cli("hook", "install", d)
            self.assertEqual(code, 2)
            self.assertIn("cannot install the hook", err)


if __name__ == "__main__":
    unittest.main()
