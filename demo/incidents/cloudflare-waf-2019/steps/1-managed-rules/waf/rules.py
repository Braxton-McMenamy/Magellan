"""Managed WAF rules: each is a regular expression run against request data at the edge.
A rule in "block" mode refuses the requests it matches; a rule in "simulate" mode only logs
them. Every rule runs on every request, whatever its mode."""

import re

RULES = {
    "sqli-union-select": ("block", re.compile(r"(?i)\bunion\b\s+(?:all\s+)?\bselect\b")),
    "xss-script-tag": ("block", re.compile(r"(?i)<\s*script\b")),
    "path-traversal": ("block", re.compile(r"(?:\.\./){2,}")),
}


def matching_rules(payload: str) -> list[tuple[str, str]]:
    """``(rule, mode)`` for every rule that matches the payload."""
    return [(name, mode) for name, (mode, rule) in RULES.items() if rule.search(payload)]
