"""A helper for rule tests: run every rule over a snippet and keep one rule's findings."""

import ast
import textwrap

import magellan_lite.rules  # noqa: F401  registers the rules
from magellan_lite.findings import run_file_rules


def found(rule_id: str, source: str, path: str = "m.py") -> list:
    """The findings ``rule_id`` reports on ``source`` (indent the snippet freely), as if it
    were the file at ``path``."""
    tree = ast.parse(textwrap.dedent(source).lstrip("\n"))
    findings, errors = run_file_rules(tree, path)
    assert not errors, errors
    return [f for f in findings if f.rule == rule_id]
