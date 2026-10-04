"""The checklist. Importing this package registers every rule.

Add a rule: write it in its own module here (one rule, or a few related ones, per file),
then import that module below. Give it a test in ``tests/rules/``.
"""

from magellan_lite.rules import defaults  # noqa: F401

# Calls the change breaks (signature-break, removed-still-referenced): need the call graph.
from magellan_lite.rules import breaks  # noqa: F401

# Starter rules: each file has numbered steps and a test waiting for it. A file whose
# `@rule(...)` line is still commented out registers nothing, so these imports are safe.
from magellan_lite.rules import assert_tuple, bare_except, compare_none, debug_leftover  # noqa: F401,E501

# TODO(starter): new to this? Start with the four files imported above, in this order:
#   bare_except.py -> assert_tuple.py -> compare_none.py -> debug_leftover.py
#   Each one says exactly what to do, and its test tells you when you're done.

# The other languages' own checks (a COBOL copybook whose layout moved, a C `goto fail`, a
# Fortran COMMON block that no longer lines up): languages.py runs them, this lists them.
from magellan_lite.rules import languages  # noqa: F401

# The famous failures, one rule each (demo/incidents/ replays them: `python demo/run.py`).
from magellan_lite.rules import dates  # noqa: F401      leap-day-date: Azure, 2012
from magellan_lite.rules import reuse  # noqa: F401      reused-value: Knight Capital, 2012
from magellan_lite.rules import loops  # noqa: F401      loop-without-progress: Knight, Zune
from magellan_lite.rules import regexes  # noqa: F401    regex-catastrophic-backtracking: Cloudflare
from magellan_lite.rules import threads  # noqa: F401    unsynchronized-shared-state: Therac-25

# Ported from the full Magellan's checklist: the generic ones worth stopping a commit for.
from magellan_lite.rules import undefined_names  # noqa: F401  undefined-name: a NameError left behind
from magellan_lite.rules import deps  # noqa: F401       dependency-undeclared / -removed-still-used
from magellan_lite.rules import taint  # noqa: F401      unvalidated-input-reaches-sink
from magellan_lite.rules import envvars  # noqa: F401    env-var-renamed / env-var-default-changed
from magellan_lite.rules import recursion  # noqa: F401  recursive-cycle: recursion with no end
from magellan_lite.rules import cost  # noqa: F401       complexity-regression: newly quadratic
from magellan_lite.rules import imports  # noqa: F401    import-cycle: modules importing each other
from magellan_lite.rules import cochange  # noqa: F401   co-change: git history's usual partner
from magellan_lite.rules import deadcode  # noqa: F401   unreachable-code / constant-condition
from magellan_lite.rules import exceptions  # noqa: F401  swallowed-exception: except Exception: pass
from magellan_lite.rules import newcode  # noqa: F401    near-duplicate / new-unreferenced

# TODO(checklist): make the famous-failure rules see more. Each has a test file in
#   tests/rules/ to add your case to; `python demo/run.py` must stay all caught, and after
#   any rule change run `python demo/build_site.py` (the website shows the rules' results).
#   1. regexes.py: a pattern kept in a constant, `WORD = r"(a+)+"` then `re.compile(WORD)`.
#      Look the name up among the module's assignments. (The easiest one: start here.)
#   2. dates.py: `date(year=d.year + 1, month=d.month, day=d.day)` written with keywords is
#      caught; `datetime.combine(...)` and `d + relativedelta(years=1)` are fine. Add a test
#      that `arrow`/`pendulum`-style `.shift(years=1)` stays quiet.
#   3. threads.py: a method started twice as two threads (`for _ in range(4): Thread(
#      target=self.work)`) races with itself. Report it when it writes a field unlocked.
#   4. loops.py: `while i < len(items):` calls len(), so it is skipped as polling today.
#      Treat len() of something the body does not change as a plain value.
