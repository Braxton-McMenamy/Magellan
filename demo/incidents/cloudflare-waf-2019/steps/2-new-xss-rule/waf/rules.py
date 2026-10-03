"""Managed WAF rules: each is a regular expression run against request data at the edge."""

import re

RULES = {
    "sqli-union-select": re.compile(r"(?i)\bunion\b\s+(?:all\s+)?\bselect\b"),
    "xss-script-tag": re.compile(r"(?i)<\s*script\b"),
    "path-traversal": re.compile(r"(?:\.\./){2,}"),
    # 2019-07-02: new managed rule for inline JavaScript used in XSS attacks
    "xss-inline-js": re.compile(
        r"""(?:(?:"|'|\]|\}|\\|\d|(?:nan|infinity|true|false|null|undefined|symbol|math)|`|\-|\+)+[)]*;?((?:\s|-|~|!|{}|\|\||\+)*.*(?:.*=.*)))"""),
}


def matching_rules(payload: str) -> list[str]:
    return [name for name, rule in RULES.items() if rule.search(payload)]
