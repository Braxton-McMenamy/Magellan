"""The map and the diff: what counts as a change, and what never does."""

import ast
import textwrap
import unittest

from magellan_lite.defs import definitions, definitions_in
from magellan_lite.diff import changed_spans, diff, in_spans
from magellan_lite.source import Snapshot, module_name


def snap(files: dict[str, str]) -> Snapshot:
    return Snapshot({k: textwrap.dedent(v).lstrip("\n") for k, v in files.items()})


SOURCE = """
    LIMIT = 3


    class Ledger(Base):
        rate = 0.5

        def post(self, amount):
            \"\"\"Book it.\"\"\"
            return amount * self.rate


    def charge(amount: int, currency: str = "USD") -> int:
        return amount
    """


class Map(unittest.TestCase):
    def test_names_kinds_and_lines(self):
        defs = definitions(snap({"app/pay.py": SOURCE}))
        self.assertEqual({n: d.kind for n, d in defs.items()}, {
            "app.pay.LIMIT": "constant", "app.pay.Ledger": "class",
            "app.pay.Ledger.rate": "constant", "app.pay.Ledger.post": "method",
            "app.pay.charge": "function"})
        post = defs["app.pay.Ledger.post"]
        self.assertEqual((post.line, post.end_line), (7, 9))
        self.assertEqual(defs["app.pay.charge"].signature,
                         "(amount: int, currency: str='USD') -> int")
        self.assertEqual(defs["app.pay.Ledger"].signature, "Base")

    def test_module_names(self):
        self.assertEqual(module_name("pkg/mod.py"), "pkg.mod")
        self.assertEqual(module_name("pkg/__init__.py"), "pkg")
        self.assertEqual(module_name("src/pkg/mod.py"), "pkg.mod")
        self.assertEqual(module_name("main.py"), "main")

    def test_formatting_docstrings_moves_and_line_endings_are_not_changes(self):
        before = snap({"app/pay.py": SOURCE})
        reshaped = (SOURCE.replace('"""Book it."""', '"""Book the amount."""')
                    .replace("return amount * self.rate", "return (amount *\n                    self.rate)"))
        after = Snapshot({"app/pay.py": "\n\n# moved down\n" + textwrap.dedent(reshaped)
                          .lstrip("\n").replace("\n", "\r\n")})
        self.assertEqual(diff(definitions(before), definitions(after)), [])

    def test_each_kind_of_change(self):
        base = textwrap.dedent(SOURCE).lstrip("\n")
        before = definitions(snap({"app/pay.py": base}))
        after = definitions(snap({"app/pay.py": base
                                  .replace("LIMIT = 3", "LIMIT = 5")
                                  .replace("return amount * self.rate", "return amount")
                                  .replace("currency: str = \"USD\"", "currency: str")
                                  .replace("    rate = 0.5\n", "") + "\n\ndef refund():\n    pass\n"}))
        got = {c.name: c.kind for c in diff(before, after)}
        self.assertEqual(got, {"app.pay.LIMIT": "value", "app.pay.Ledger.post": "body",
                               "app.pay.charge": "signature", "app.pay.Ledger.rate": "removed",
                               "app.pay.refund": "added", "app.pay.Ledger": "body"})

    def test_spans_cover_what_changed(self):
        before = definitions(snap({"app/pay.py": SOURCE}))
        after = definitions(snap({"app/pay.py": SOURCE.replace("return amount\n", "return 0\n")}))
        spans = changed_spans(diff(before, after))
        self.assertEqual(spans, {"app/pay.py": [(12, 13)]})
        self.assertTrue(in_spans(spans, "app/pay.py", 13))
        self.assertFalse(in_spans(spans, "app/pay.py", 9))

    def test_multi_line_constants_span_their_whole_value(self):
        [d] = definitions_in(ast.parse("RULES = {\n  'a': 1,\n  'b': 2,\n}\n"), "waf.py")
        self.assertEqual((d.line, d.end_line), (1, 4))

    def test_a_file_that_does_not_parse_is_skipped_and_reported(self):
        s = snap({"ok.py": "def f():\n    pass\n", "broken.py": "def f(:\n"})
        self.assertEqual(list(definitions(s)), ["ok.f"])
        self.assertIn("broken.py:1", s.errors["broken.py"])


if __name__ == "__main__":
    unittest.main()
