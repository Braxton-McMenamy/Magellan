from waf.rules import matching_rules

#: matches of rules in simulate mode: logged for review, never blocked
SIMULATED: list[str] = []


def handle_request(body: str) -> int:
    hits = matching_rules(body)
    SIMULATED.extend(name for name, mode in hits if mode == "simulate")
    return 403 if any(mode == "block" for _, mode in hits) else 200
