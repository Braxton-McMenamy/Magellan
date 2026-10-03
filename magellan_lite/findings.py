"""Findings and the rule registry. This is the shared contract: change it together.

There are two kinds of rule.

``@rule`` looks at one parsed file and yields ``(where, message)`` or
``(where, message, detail)``, where ``where`` is an AST node or a line number. The engine
keeps only what lands inside code the change touched, so a rule never needs to know about
the change::

    @rule("debug-leftover", "low", fix="Remove it before committing.")
    def debug_leftover(tree, path):
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "breakpoint":
                yield node, "breakpoint() left in"

``@change_rule`` sees the whole change -- both versions of every definition -- and returns
``Finding`` objects itself. Use it for rules about what changed *between* versions, such as
a constant's value handed to a new meaning.

``blocking=True`` lets a rule's high or critical findings make the verdict ``block``. Keep it
for rules that mean "this change will break something", not "have a look".
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from typing import Callable, Iterable

SEVERITIES = ("critical", "high", "medium", "low")


@dataclass
class Finding:
    rule: str
    severity: str
    message: str            # one line: what is wrong, where
    path: str               # relative to the project root, forward slashes on every OS
    line: int
    detail: str = ""        # why it matters
    fix: str = ""           # what to do about it

    @property
    def rank(self) -> int:
        """0 for critical, 3 for low: sort by it to put the worst first."""
        return SEVERITIES.index(self.severity)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    fix: str
    blocking: bool
    per_file: bool          # True: @rule (one file at a time); False: @change_rule
    check: Callable


RULES: dict[str, Rule] = {}


def _register(rule_id: str, severity: str, fix: str, blocking: bool, per_file: bool):
    if severity not in SEVERITIES:
        raise ValueError(f"{rule_id}: severity must be one of {SEVERITIES}, not {severity!r}")

    def register(check: Callable) -> Callable:
        if rule_id in RULES and RULES[rule_id].check is not check:
            raise ValueError(f"two rules are called {rule_id!r}")
        RULES[rule_id] = Rule(rule_id, severity, fix, blocking, per_file, check)
        return check
    return register


def rule(rule_id: str, severity: str, fix: str = "", blocking: bool = False):
    """Register a check over one parsed file. See the module docstring."""
    return _register(rule_id, severity, fix, blocking, per_file=True)


def change_rule(rule_id: str, severity: str, fix: str = "", blocking: bool = False):
    """Register a check over the whole change. See the module docstring."""
    return _register(rule_id, severity, fix, blocking, per_file=False)


def run_file_rules(tree: ast.Module, path: str) -> tuple[list[Finding], list[str]]:
    """Every per-file rule's findings for one file, and the errors of rules that crashed.

    A broken rule must not take the whole check down: its error is reported instead.
    """
    out: list[Finding] = []
    errors: list[str] = []
    for r in RULES.values():
        if not r.per_file:
            continue
        try:
            for hit in r.check(tree, path):
                where, message, *rest = hit
                line = where if isinstance(where, int) else getattr(where, "lineno", 0)
                out.append(Finding(r.id, r.severity, message, path, line,
                                   detail=rest[0] if rest else "", fix=r.fix))
        except Exception as exc:                    # noqa: BLE001 - reported, not hidden
            errors.append(f"rule {r.id} failed on {path}: {type(exc).__name__}: {exc}")
    return out, errors


def blocking_rules() -> set[str]:
    return {r.id for r in RULES.values() if r.blocking}


def iter_change_rules() -> Iterable[Rule]:
    return [r for r in RULES.values() if not r.per_file]
