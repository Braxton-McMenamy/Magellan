"""dependency-undeclared and dependency-removed-still-used (from the full Magellan's deps.py;
the cases come from its tests/rules/test_deps.py). Manifests live on disk, so these run in a
throwaway git repository."""

import unittest

from magellan_lite.engine import check_files
from magellan_lite.rules import deps
from tests.helpers import Project

PYPROJECT = ('[project]\nname = "x"\nversion = "1.0"\n'
             'dependencies = ["requests>=2.0", "PyYAML", "python-dateutil~=2.8"]\n')
MAIN = ("import requests\nimport yaml\nfrom dateutil import parser\n\n\ndef go(u):\n"
        "    return requests.get(u), yaml.safe_load(u), parser.parse(u)\n")
BASE = {"pyproject.toml": PYPROJECT, "app/__init__.py": "", "app/main.py": MAIN}


def rules(report) -> list:
    return sorted((f.rule, f.severity, f.path, f.line) for f in report.findings
                  if f.rule.startswith("dependency-"))


class Dependencies(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.p.commit(BASE)

    def tearDown(self):
        self.p.__exit__(None, None, None)

    def test_an_untouched_manifest_and_known_imports_are_quiet(self):
        self.p.write({"app/main.py": MAIN.replace("parser.parse(u)", "parser.parse(u.strip())"),
                      "app/util.py": "import json, os\nfrom app import main\nfrom . import main as m\n"
                                     "\n\ndef u():\n    return json.dumps(os.sep)\n"})
        self.assertEqual(rules(self.p.check()), [])

    def test_a_new_import_nothing_declares(self):
        self.p.write({"app/extra.py": "import numpy\n\n\ndef z():\n    return numpy.zeros(1)\n"})
        report = self.p.check()
        self.assertEqual(rules(report), [("dependency-undeclared", "high", "app/extra.py", 1)])
        [f] = [f for f in report.findings if f.rule == "dependency-undeclared"]
        self.assertIn("numpy is imported in app/extra.py but no manifest declares it", f.message)

    def test_optional_imports_and_test_only_imports(self):
        self.p.write({"app/fast.py": "try:\n    import orjson as json\nexcept ImportError:\n"
                                     "    import json\n",
                      "app/types.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
                                      "    import pandas\n"})
        self.assertEqual(rules(self.p.check()), [])
        self.p.write({"tests/test_x.py": "import pytest\n\n\ndef test_x():\n    assert pytest\n"})
        self.assertEqual(rules(self.p.check()), [("dependency-undeclared", "medium",
                                                  "tests/test_x.py", 1)])

    def test_a_requirements_file_declares_too(self):
        self.p.write({"requirements.txt": "numpy==1.26  # arrays\n-r dev.txt\n",
                      "app/extra.py": "import numpy\n"})
        self.assertEqual(rules(self.p.check()), [])

    def test_removed_but_still_imported_blocks(self):
        self.p.write({"pyproject.toml": PYPROJECT.replace('"PyYAML", ', "")})
        report = self.p.check()
        self.assertEqual(rules(report), [("dependency-removed-still-used", "critical",
                                          "app/main.py", 2)])
        self.assertEqual(report.verdict, "block")

    def test_removed_and_no_longer_imported_is_fine(self):
        self.p.write({"pyproject.toml": PYPROJECT.replace('"PyYAML", ', ""),
                      "app/main.py": MAIN.replace("import yaml\n", "").replace(
                          "yaml.safe_load(u), ", "")})
        self.assertEqual(rules(self.p.check()), [])

    def test_no_manifest_means_no_opinion(self):
        self.p.write({"pyproject.toml": None, "app/extra.py": "import numpy\n"})
        self.assertEqual(rules(self.p.check()), [])

    def test_computed_dependencies_mean_no_opinion(self):
        self.p.write({"setup.py": "from setuptools import setup\nsetup(install_requires=open("
                                  "'reqs').read().split())\n",
                      "app/extra.py": "import numpy\n"})
        self.assertEqual(rules(self.p.check()), [])

    def test_code_handed_over_as_text_is_never_judged(self):
        report = check_files({"app/a.py": "def f():\n    return 1\n"},
                             {"app/a.py": "import numpy\n\n\ndef f():\n    return 1\n"})
        self.assertEqual(rules(report), [])


class Manifests(unittest.TestCase):
    def test_names_from_every_manifest(self):
        m = deps.parse_manifests({
            "pyproject.toml": PYPROJECT + '[project.optional-dependencies]\ndev = ["pytest>=7"]\n'
                              '[build-system]\nrequires = ["setuptools>=61"]\n',
            "requirements-dev.txt": "# c\nblack==24.1.0\n-r other.txt\ngit+https://x/y\n"
                                    "ruff>=0.1 ; python_version>'3'\n",
            "setup.cfg": "[options]\ninstall_requires =\n    attrs>=22\n    click\n",
        })
        self.assertEqual(set(m.declared), {"x", "requests", "pyyaml", "python-dateutil",
                                           "pytest", "setuptools", "black", "ruff", "attrs",
                                           "click"})
        self.assertTrue(m.certain)

    def test_import_names_map_to_distributions(self):
        declared = {deps.norm(n): "" for n in ("pyyaml", "python-dateutil",
                                               "google-cloud-storage", "Pillow")}
        for name in ("yaml", "dateutil", "google", "PIL"):
            self.assertIsNotNone(deps.declared_as(name, declared), name)
        self.assertIsNone(deps.declared_as("numpy", declared))

    def test_the_python_3_10_reader_finds_the_same_names(self):
        text = ('[project]\nname = "x"\ndependencies = [\n    "requests>=2.0",  # http\n'
                '    "pkg @ git+https://example.invalid/pkg#egg=pkg",\n]\n'
                '[project.optional-dependencies]\ndocs = ["sphinx"]\n'
                '[tool.poetry.dependencies]\npython = "^3.10"\nrich = { version = "^13" }\n'
                '[tool.poetry.group.dev.dependencies]\nmypy = "*"\n'
                '[[tool.mypy.overrides]]\nmodule = ["a.*"]\n')
        data = deps._mini_toml(text)
        self.assertEqual(data["project"]["dependencies"],
                         ["requests>=2.0", "pkg @ git+https://example.invalid/pkg#egg=pkg"])
        self.assertEqual(data["project"]["optional-dependencies"], {"docs": ["sphinx"]})
        self.assertEqual(set(data["tool"]["poetry"]["dependencies"]), {"python", "rich"})
        self.assertIn("mypy", data["tool"]["poetry"]["group"]["dev"]["dependencies"])


if __name__ == "__main__":
    unittest.main()
