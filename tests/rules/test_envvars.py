"""env-var-renamed and env-var-default-changed: the environment contract changed under its
readers (from the full Magellan's envvars.py; its test cases, adapted)."""

import unittest

from magellan_lite.engine import check_files


def env(before: str, after: str, path: str = "cfg.py") -> list:
    return [(f.rule, f.severity, f.line, f.message)
            for f in check_files({path: before}, {path: after}).findings
            if f.rule.startswith("env-var-")]


DB = 'import os\n\n\ndef db():\n    return os.environ["{}"]\n'
PORT = 'import os\n\n\ndef port():\n    return int(os.getenv("PORT", {}))\n'


class Renamed(unittest.TestCase):
    def test_a_reader_switches_variables(self):
        [(rule, sev, line, message)] = env(DB.format("DB_URL"), DB.format("DATABASE_URL"))
        self.assertEqual((rule, sev, line), ("env-var-renamed", "high", 5))
        self.assertIn("db() now reads the environment variable DATABASE_URL instead of DB_URL",
                      message)

    def test_a_module_constant_and_every_import_style(self):
        before = 'from os import getenv\n\nURL = getenv("API_URL", "http://localhost")\n'
        [(rule, *_)] = env(before, before.replace("API_URL", "SERVICE_URL"))
        self.assertEqual(rule, "env-var-renamed")
        before = 'from os import environ\n\nKEY = "TOKEN"\n\n\ndef t():\n    return environ.get(KEY)\n'
        [(rule, *_)] = env(before, before.replace('"TOKEN"', '"API_TOKEN"'))
        self.assertEqual(rule, "env-var-renamed")

    def test_keeping_the_old_name_or_adding_one_stays_quiet(self):
        both = 'import os\n\n\ndef db():\n    return os.getenv("DATABASE_URL") or os.getenv("DB_URL")\n'
        self.assertEqual(env(DB.format("DB_URL"), both), [])
        extra = DB.format("DB_URL").replace("return", 'os.getenv("POOL", "5")\n    return')
        self.assertEqual(env(DB.format("DB_URL"), extra), [])
        self.assertEqual(env(DB.format("DB_URL"), "def db():\n    return None\n"), [])


class DefaultChanged(unittest.TestCase):
    def test_a_new_default(self):
        [(rule, sev, _line, message)] = env(PORT.format('"8080"'), PORT.format('"9090"'))
        self.assertEqual((rule, sev), ("env-var-default-changed", "medium"))
        self.assertIn("'8080' -> '9090'", message)

    def test_optional_becoming_required(self):
        before = 'import os\n\n\ndef p():\n    return os.environ.get("TOKEN", "")\n'
        [(rule, _sev, _line, message)] = env(before, 'import os\n\n\ndef p():\n    return os.environ["TOKEN"]\n')
        self.assertEqual(rule, "env-var-default-changed")
        self.assertIn("now requires the environment variable TOKEN", message)

    def test_quiet_when_nothing_a_deployment_sees_changed(self):
        self.assertEqual(env(PORT.format('"8080"'), PORT.format("'8080'")), [])   # quoting
        required = 'import os\n\n\ndef p():\n    return os.environ["TOKEN"]\n'
        self.assertEqual(env(required, required.replace('os.environ["TOKEN"]',
                                                        'os.getenv("TOKEN", "x")')), [])
        dynamic = "import os\n\n\ndef f(k):\n    return os.environ[k], os.getenv(k, 1)\n"
        self.assertEqual(env(dynamic, dynamic.replace(", 1)", ", 2)")), [])
        self.assertEqual(env(PORT.format('"8080"'),
                             PORT.format('"8080"').replace("int(", "float(")), [])


if __name__ == "__main__":
    unittest.main()
