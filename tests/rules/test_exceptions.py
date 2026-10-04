"""swallowed-exception: a broad handler that throws the error away (from the full Magellan)."""

import unittest

from tests.rules.checks import found


class SwallowedException(unittest.TestCase):
    def test_broad_handlers_that_do_nothing(self):
        for code in [
            "def f():\n    try:\n        g()\n    except Exception:\n        pass\n",
            "def f():\n    try:\n        g()\n    except BaseException:\n        ...\n",
            "def f(xs):\n    for x in xs:\n        try:\n            g(x)\n"
            "        except (ValueError, Exception):\n            continue\n",
            "def f():\n    try:\n        g()\n    except builtins.Exception:\n"
            "        '''ignore'''\n",
        ]:
            with self.subTest(code=code):
                [f] = found("swallowed-exception", code)
                self.assertIn("throws the error away", f.message)

    def test_suppress_exception(self):
        [f] = found("swallowed-exception",
                    "from contextlib import suppress\n\ndef f():\n"
                    "    with suppress(Exception):\n        g()\n")
        self.assertEqual(f.line, 4)

    def test_narrow_handlers_and_handlers_that_act_stay_quiet(self):
        for code in [
            "def f():\n    try:\n        g()\n    except ValueError:\n        pass\n",
            "def f():\n    try:\n        g()\n    except Exception as e:\n        log(e)\n",
            "def f():\n    try:\n        g()\n    except Exception:\n        raise\n",
            "def f():\n    try:\n        g()\n    except Exception:\n        return None\n",
            "def f():\n    try:\n        g()\n    except:\n        pass\n",        # bare-except's
            "def f():\n    with suppress(KeyError):\n        g()\n",
        ]:
            with self.subTest(code=code):
                self.assertEqual(found("swallowed-exception", code), [])


if __name__ == "__main__":
    unittest.main()
