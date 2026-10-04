"""The website's JavaScript that has rules to keep (tests/js/*.test.js), run with Node when it
is installed: the Team suite's GitHub reader."""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SiteScripts(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_the_sites_script_tests_pass(self):
        r = subprocess.run(["node", "--test", "tests/js/*.test.js"], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
