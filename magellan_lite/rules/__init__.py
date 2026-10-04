"""The checklist. Importing this package registers every rule.

Add a rule: write it in its own module here (one rule, or a few related ones, per file),
then import that module below. Give it a test in ``tests/rules/``, with cases that must stay
quiet. After changing a rule, ``python demo/run.py`` must still show no MISSED, and
``python demo/build_site.py`` refreshes what the website shows.
"""

from magellan_lite.rules import defaults  # noqa: F401

# Calls the change breaks (signature-break, removed-still-referenced): need the call graph.
from magellan_lite.rules import breaks  # noqa: F401

# The small rules, short and commented step by step. New to the checklist? Read them first,
# in this order: bare_except.py -> assert_tuple.py -> compare_none.py -> debug_leftover.py,
# each beside its test in tests/rules/. They show the whole shape of a rule in a page.
from magellan_lite.rules import assert_tuple, bare_except, compare_none, debug_leftover  # noqa: F401,E501

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
