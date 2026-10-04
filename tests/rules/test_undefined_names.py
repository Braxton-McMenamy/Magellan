"""undefined-name: a name the change left undefined (from the full Magellan's names.py)."""

import unittest

from magellan_lite.engine import check_files


def undefined(before: str | None, after: str, path: str = "app/report.py") -> list:
    b = {} if before is None else {path: before}
    return [f for f in check_files(b, {path: after}).findings if f.rule == "undefined-name"]


DUMP = "import json\n\n\ndef dump(x):\n    return json.dumps(x)\n"


class UndefinedName(unittest.TestCase):
    def test_a_removed_import_still_used_in_a_function(self):
        [f] = undefined(DUMP, DUMP.replace("import json\n", ""))
        self.assertEqual((f.severity, f.line), ("high", 4))
        self.assertIn("json is used in app.report but nothing defines it any more", f.message)
        self.assertIn("NameError", f.detail)

    def test_at_import_time_it_is_critical_and_blocks(self):
        before = "import math\n\nTAU = math.pi * 2\n"
        report = check_files({"geo.py": before}, {"geo.py": "TAU = math.pi * 2\n"})
        [f] = [f for f in report.findings if f.rule == "undefined-name"]
        self.assertEqual((f.severity, f.line), ("critical", 1))
        self.assertEqual(report.verdict, "block")

    def test_a_module_renamed_by_an_upgrade(self):
        before = "import httplib\n\n\ndef conn(h):\n    return httplib.HTTPSConnection(h)\n"
        after = "import http.client\n\n\ndef conn(h):\n    return httplib.HTTPSConnection(h)\n"
        [f] = undefined(before, after)
        self.assertIn("httplib", f.message)

    def test_a_typo_in_new_code(self):
        [f] = undefined(None, "def total(xs):\n    return summ(xs)\n")
        self.assertEqual(f.line, 2)
        self.assertNotIn("any more", f.message)

    def test_names_python_defines_stay_quiet(self):
        for code in [
            "def f(xs):\n    return sorted(len(x) for x in xs)\n",
            "class A:\n    q = __qualname__\n    m = __module__\n",
            "def f():\n    global G\n    G = 1\n\n\ndef g():\n    return G\n",
            "try:\n    import ujson as json\nexcept ImportError:\n    json = None\n",
            "def f():\n    return later()\n\n\ndef later():\n    return 1\n",
            "def f():\n    return [y for y in range(3)]\n",
            "def f():\n    return _('hello')\n",                                  # gettext
            "from __future__ import annotations\n\n\ndef f(x: Missing) -> None:\n    pass\n",
            "def f(path):\n    return __file__, __name__, path\n",
        ]:
            with self.subTest(code=code):
                self.assertEqual(undefined(None, code), [])

    def test_unknowable_files_stay_quiet(self):
        for code in [
            "from os.path import *\n\n\ndef f(p):\n    return join(p, 'x')\n",    # star import
            "globals()['X'] = 1\n\n\ndef f():\n    return X\n",                  # made at run time
            "print 'python 2'\n\n\ndef f():\n    return nothing\n",              # not Python 3
        ]:
            with self.subTest(code=code):
                self.assertEqual(undefined(None, code), [])

    def test_a_name_that_was_already_undefined_is_not_this_change(self):
        before = "def f():\n    return helper_from_nowhere()\n"
        after = "def f():\n    x = 1\n    return helper_from_nowhere() + x\n"
        self.assertEqual(undefined(before, after), [])

    def test_a_deleted_function_still_called_is_reported_once(self):
        before = "def helper():\n    return 1\n\n\ndef f():\n    return helper()\n"
        after = "def f():\n    return helper()\n"
        report = check_files({"m.py": before}, {"m.py": after})
        rules = [f.rule for f in report.findings]
        self.assertEqual(rules, ["removed-still-referenced"])


if __name__ == "__main__":
    unittest.main()
