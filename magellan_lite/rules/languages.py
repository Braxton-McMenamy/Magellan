"""The checklist's rules for C, C++, Java, Fortran and COBOL.

Each language's frontend (vendored from the full Magellan, run by ``magellan_lite/languages.py``)
finds its own hazards in a change; this registers each of them as a rule, so they are on the
checklist (``magellan-lite rules``), count toward the verdict, and can be told apart. The work
is done once per check, whichever of these rules asks first.
"""

from magellan_lite import languages
from magellan_lite.findings import change_rule


def _rule(rule_id: str):
    def check(ctx):
        if all(p.endswith(".py") for p in set(ctx.before.files) | set(ctx.after.files)):
            return []
        return [f for f in languages.findings(ctx) if f.rule == rule_id]
    check.__name__ = rule_id.replace("-", "_")
    return check


for _id, (_severity, _blocking, _fix) in languages.RULES.items():
    change_rule(_id, _severity, _fix, blocking=_blocking)(_rule(_id))
