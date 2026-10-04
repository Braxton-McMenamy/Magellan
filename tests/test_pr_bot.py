"""The pull request bot (.github/workflows/magellan-lite.yml): what it runs and what it posts.

GitHub cannot be called from a test, so the workflow's own check step is run here, under bash,
on a repository whose ``origin/main`` is the pull request's base: the comment it would post
must carry the hidden marker the posting step looks for, and the checklist.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import magellan_lite
from tests.helpers import Project

REPO = Path(magellan_lite.__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "magellan-lite.yml"
MARKER = "<!-- magellan-lite -->"


def step_script(workflow: str, step_id: str) -> str:
    """The ``run: |`` block of the step with ``id: step_id``."""
    lines = workflow.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"id: {step_id}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip())
    body = []
    for line in lines[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def find_bash() -> str | None:
    """bash as GitHub's runner has it: on Windows, Git's own (not WSL's in System32)."""
    if os.name != "nt":
        return shutil.which("bash")
    try:
        exec_path = subprocess.run(["git", "--exec-path"], capture_output=True,
                                   encoding="utf-8").stdout.strip()
    except OSError:
        return None
    git = Path(exec_path).parents[2]                  # .../Git/mingw64/libexec/git-core
    for bash in (git / "usr" / "bin" / "bash.exe", git / "bin" / "bash.exe"):
        if bash.is_file():
            return str(bash)
    return None


class Workflow(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_it_runs_on_pull_requests_with_only_the_rights_it_needs(self):
        self.assertIn("on:\n  pull_request:", self.text)
        self.assertIn("permissions:\n  contents: read\n  pull-requests: write\n", self.text)
        self.assertIn("fetch-depth: 0", self.text)       # the base branch's history
        self.assertIn("uses: actions/setup-python@", self.text)
        self.assertIn("run: pip install .", self.text)
        self.assertNotIn("\t", self.text)                 # YAML refuses tabs

    def test_it_checks_against_the_base_branch_as_markdown(self):
        script = step_script(self.text, "check")
        self.assertIn("BASE: ${{ github.base_ref }}", self.text)
        self.assertIn('magellan-lite check --against "git:origin/$BASE" --format markdown',
                      script)

    def test_it_updates_its_own_comment_instead_of_adding_one(self):
        post = self.text[self.text.index("- name: Post the checklist"):]
        self.assertIn(f'startswith("{MARKER}")', post)    # finds the earlier comment ...
        self.assertIn("--method PATCH", post)             # ... and edits it
        self.assertIn(f'echo "{MARKER}"', step_script(self.text, "check"))   # the marker


@unittest.skipIf(find_bash() is None, "needs bash (Git's, on Windows)")
class CheckStep(unittest.TestCase):
    """The workflow's check step, run for real on a pull request's checkout."""

    def run_step(self, pr: Project) -> tuple[str, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shim = tmp / "bin" / "magellan-lite"          # what `pip install .` puts on PATH
            shim.parent.mkdir()
            shim.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" '
                            f'-m magellan_lite "$@"\n', encoding="utf-8", newline="\n")
            shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
            bash = find_bash()
            env = {**os.environ,
                   "PATH": os.pathsep.join([str(shim.parent), str(Path(bash).parent),
                                            os.environ.get("PATH", "")]),
                   "PYTHONPATH": str(REPO),
                   "BASE": "main",
                   "GITHUB_OUTPUT": (tmp / "output").as_posix(),
                   "GITHUB_STEP_SUMMARY": (tmp / "summary").as_posix()}
            script = step_script(WORKFLOW.read_text(encoding="utf-8"), "check")
            r = subprocess.run([bash, "-e", "-c", script], cwd=pr.root, env=env,
                               capture_output=True, encoding="utf-8", errors="replace")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return ((pr.root / "comment.md").read_text(encoding="utf-8"),
                    (tmp / "output").read_text(encoding="utf-8"),
                    (tmp / "summary").read_text(encoding="utf-8"))

    def pull_request(self, base: dict, change: dict) -> Project:
        p = Project()
        p.commit(base)
        p.git("update-ref", "refs/remotes/origin/main", "HEAD")    # the base branch
        p.commit(change, message="the pull request")
        return p

    def test_a_pull_request_with_a_mutable_default_gets_its_checklist(self):
        with self.pull_request({"app/pay.py": "def charge(amount):\n    return amount\n"},
                               {"app/pay.py": "def charge(amount, log=[]):\n"
                                              "    log.append(amount)\n"
                                              "    return amount\n"}) as p:
            comment, output, summary = self.run_step(p)
            self.assertEqual(comment.splitlines()[0], MARKER)
            self.assertIn("### Magellan Lite: **REVIEW**", comment)
            tasks = [line for line in comment.splitlines() if line.startswith("- [ ]")]
            self.assertEqual(len(tasks), 1)
            self.assertIn("`mutable-default-argument`", tasks[0])
            self.assertIn("`git:origin/main`", comment)
            self.assertIn("code=0", output)                  # review: the run stays green
            self.assertIn(tasks[0], summary)

    def test_a_blocking_pull_request_fails_the_run_after_commenting(self):
        before = "def charge(amount):\n    return amount\n\n\ndef pay():\n    return charge(1)\n"
        with self.pull_request({"app/pay.py": before},
                               {"app/pay.py": before.replace("(amount)", "(amount, currency)")}
                               ) as p:
            comment, output, _summary = self.run_step(p)
            self.assertIn("**BLOCK**", comment)
            self.assertIn("`signature-break`", comment)
            self.assertIn("code=1", output)                  # the last step turns the run red

    def test_a_check_that_cannot_run_says_why(self):
        with Project() as p:
            p.commit({"m.py": "X = 1\n"})                    # no origin/main to compare with
            comment, output, _summary = self.run_step(p)
            self.assertIn("### Magellan Lite could not run", comment)
            self.assertIn("cannot read revision 'origin/main'", comment)
            self.assertIn("code=2", output)


if __name__ == "__main__":
    unittest.main()
