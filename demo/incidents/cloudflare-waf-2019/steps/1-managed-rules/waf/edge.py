from waf.rules import matching_rules


def handle_request(body: str) -> int:
    return 403 if matching_rules(body) else 200
