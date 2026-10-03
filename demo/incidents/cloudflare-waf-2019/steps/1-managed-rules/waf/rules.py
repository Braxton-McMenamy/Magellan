"""Managed WAF rules: each is a regular expression run against request data at the edge."""

import re

RULES = {
    "sqli-union-select": re.compile(r"(?i)\bunion\b\s+(?:all\s+)?\bselect\b"),
    "xss-script-tag": re.compile(r"(?i)<\s*script\b"),
    "path-traversal": re.compile(r"(?:\.\./){2,}"),
}


def matching_rules(payload: str) -> list[str]:
    return [name for name, rule in RULES.items() if rule.search(payload)]
