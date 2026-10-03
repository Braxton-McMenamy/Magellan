"""A helper for rule tests: run every rule over a snippet and keep one rule's findings."""

import ast
import textwrap

import magellan_lite.rules  # noqa: F401  registers the rules
from magellan_lite.findings import run_file_rules


def found(rule_id: str, source: str) -> list:
    """The findings ``rule_id`` reports on ``source`` (indent the snippet freely)."""
    tree = ast.parse(textwrap.dedent(source).lstrip("\n"))
    findings, errors = run_file_rules(tree, "m.py")
    assert not errors, errors
    return [f for f in findings if f.rule == rule_id]
