"""The VS Code extension (editors/vscode): its copy of the website's map renderer is current,
and its own Node tests pass (when Node is installed)."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "editors" / "vscode"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


class Extension(unittest.TestCase):
    def test_the_map_renderer_matches_the_website(self):
        files = json.loads((EXT / "shared.json").read_text(encoding="utf-8"))["files"]
        stale = [to for src, to in files.items() if not (EXT / to).is_file()
                 or _text(EXT / to) != _text(ROOT / src)]
        self.assertEqual(stale, [], "run: node editors/vscode/sync.js")

    def test_saving_any_file_the_engine_reads_checks_again(self):
        # lite.js keeps its own copy of the suffixes (no Python at activation): they must agree
        import re
        from magellan_lite.languages import SUFFIXES
        block = re.search(r"const SUFFIXES = \[(.*?)\];", _text(EXT / "lite.js"), re.S).group(1)
        self.assertEqual(set(re.findall(r'"([^"]+)"', block)), {".py", *SUFFIXES})

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_the_extensions_own_tests_pass(self):
        r = subprocess.run(["node", "--test", "test/*.test.js"], cwd=EXT,
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
