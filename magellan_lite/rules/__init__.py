"""The checklist. Importing this package registers every rule.

Add a rule: write it in its own module here (one rule, or a few related ones, per file),
then import that module below. Give it a test in ``tests/rules/``.
"""

from magellan_lite.rules import defaults  # noqa: F401

# Starter rules: each file has numbered steps and a test waiting for it. A file whose
# `@rule(...)` line is still commented out registers nothing, so these imports are safe.
from magellan_lite.rules import assert_tuple, bare_except, compare_none, debug_leftover  # noqa: F401,E501

# TODO(starter): new to this? Start with the four files imported above, in this order:
#   bare_except.py -> assert_tuple.py -> compare_none.py -> debug_leftover.py
#   Each one says exactly what to do, and its test tells you when you're done.

# TODO(checklist): the rules that catch famous failures. Each one turns an incident in
#   demo/incidents/ green: `python demo/run.py` shows which are still waiting.
#
#   1. leap-day-date (high, blocking): `date(d.year + 1, d.month, d.day)` and
#      `d.replace(year=d.year + 1)` raise ValueError on February 29. Quiet when the offset is
#      a multiple of 4, inside `try/except ValueError`, or when the function checks `== 29`.
#      Turns azure-leap-day-2012 green. The easiest of these four: start here.
#   2. reused-value (high): a @change_rule. A constant deleted (or given a new value) while a
#      new constant in the same module takes its old value, and a function that read the old
#      one now reads the new one AND calls different things. A plain rename calls the same
#      things: stay quiet. Turns knight-capital-2012 step 3 green.
#   3. loop-without-progress (high, blocking): a `while` loop whose body never changes what
#      its condition reads, never passes it to a call, and never breaks/returns/raises.
#      Skip `while True` and conditions that call things (polling). Turns knight-capital-2012
#      step 2 green; the path-by-path version (Zune, `days == 366`) is the stretch goal.
#   4. regex-catastrophic-backtracking (high): a changed regex literal with nested
#      quantifiers like `(a+)+` or three overlapping unbounded parts like `.*(?:.*=.*)`.
#      Parse with `re._parser` (3.11+) / `sre_parse` (3.10), never run the regex.
#      Turns cloudflare-waf-2019 green.
#   Stretch: unsynchronized-shared-state (therac-25-1986) needs thread analysis: last.
#
#   The full Magellan has tested versions of 1-4 (magellan/python/rules/, magellan/rules/):
#   read them for the edge cases, then write your own small version here.
