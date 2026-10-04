"""near-duplicate and new-unreferenced: checks on added code (from the full Magellan's
newcode.py; the cases come from its tests/python/test_newcode.py)."""

import textwrap
import unittest

from magellan_lite.engine import check_files

BASE = {
    "pkg/__init__.py": "",
    "pkg/text.py": textwrap.dedent('''
        def clean_rows(rows):
            """Strip and drop empty rows."""
            out = []
            for row in rows:
                row = row.strip()
                if not row:
                    continue
                out.append(row.lower())
            return out

        def join_all(parts):
            return ",".join(parts)
    '''),
    "pkg/app.py": "from pkg.text import clean_rows\n\n\ndef run(x):\n    return clean_rows(x)\n",
}


def found(rule: str, added: dict) -> list:
    return [f for f in check_files(BASE, {**BASE, **added}).findings if f.rule == rule]


class NearDuplicate(unittest.TestCase):
    def test_a_renamed_copy(self):
        [f] = found("near-duplicate", {"pkg/more.py": textwrap.dedent('''
            def tidy(lines):
                res = []
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    res.append(line.lower())
                return res
        ''')})
        self.assertEqual((f.path, f.line, f.severity), ("pkg/more.py", 2, "low"))
        self.assertIn("new tidy() repeats clean_rows() in pkg/text.py", f.message)

    def test_different_logic_tiny_functions_and_tests_stay_quiet(self):
        for added in [
            {"pkg/more.py": textwrap.dedent('''
                def summarise(rows):
                    total = 0
                    seen = set()
                    for row in rows:
                        if row in seen:
                            total += 2
                        else:
                            seen.add(row)
                            total += len(row) * 3
                    return {"total": total, "unique": len(seen)}
            ''')},
            {"pkg/more.py": 'def glue(parts):\n    return ",".join(parts)\n'},
            {"tests/test_more.py": textwrap.dedent('''
                def tidy(lines):
                    res = []
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        res.append(line.lower())
                    return res
            ''')},
        ]:
            with self.subTest(added=list(added)):
                self.assertEqual(found("near-duplicate", added), [])

    def test_a_vendored_copy_of_a_module_is_a_copy_on_purpose(self):
        self.assertEqual(found("near-duplicate", {"vendor/pkg/text.py": BASE["pkg/text.py"]}),
                         [])
        self.assertEqual(found("near-duplicate", {"pkg/text_copy.py": BASE["pkg/text.py"]}),
                         [])

    def test_an_override_is_not_a_duplicate(self):
        base = textwrap.dedent('''
            class Base:
                def render(self, rows):
                    out = []
                    for row in rows:
                        row = row.strip()
                        if not row:
                            continue
                        out.append(row.lower())
                    return out
        ''')
        child = base + textwrap.dedent('''
            class Child(Base):
                def render(self, rows):
                    out = []
                    for row in rows:
                        row = row.strip()
                        if not row:
                            continue
                        out.append(row.upper())
                    return out
        ''')
        self.assertEqual([f for f in check_files({"r.py": base}, {"r.py": child}).findings
                          if f.rule == "near-duplicate"], [])


class NewUnreferenced(unittest.TestCase):
    def test_an_unused_private_helper(self):
        [f] = found("new-unreferenced", {"pkg/more.py": textwrap.dedent('''
            def _orphan():
                return 1

            def _used():
                return 2

            def public():
                return _used()
        ''')})
        self.assertEqual((f.line, f.severity), (2, "low"))
        self.assertIn("new _orphan() is never called", f.message)

    def test_helpers_used_in_ways_a_call_graph_misses_stay_quiet(self):
        for added in [
            "def _recurse(n):\n    return 0\n\n\nHANDLERS = {'x': _recurse}\n",       # a table
            "import threading\n\n\nclass W:\n    def _run(self):\n        pass\n\n"
            "    def start(self):\n        threading.Thread(target=self._run).start()\n",
            "class V:\n    def _visit_Name(self, node):\n        pass\n\n"
            "    def visit(self, node):\n        return getattr(self, '_visit_' + "
            "type(node).__name__)(node)\n",                                          # dispatch
            "import functools\n\n\n@functools.cache\ndef _cached():\n    return 1\n",
            "import logging\n\n\nclass H(logging.Handler):\n    def _hook(self):\n"
            "        pass\n",                                                        # framework
            "def public():\n    return 1\n",                                         # not private
            "def __getattr__(name):\n    return name\n",                             # dunder
        ]:
            with self.subTest(added=added):
                self.assertEqual(found("new-unreferenced", {"pkg/more.py": added}), [])

    def test_calling_itself_is_not_being_used(self):
        [f] = found("new-unreferenced", {"pkg/more.py": "def _walk(n):\n    return _walk(n - 1) "
                                                        "if n else 0\n"})
        self.assertIn("_walk", f.message)


if __name__ == "__main__":
    unittest.main()
