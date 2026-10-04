"""Ports the fleet calls at, and the sea distance between them."""

PORTS = {
    "Seville": {"country": "Spain", "rate_per_tonne": 3.10},
    "Rio": {"country": "Brazil", "rate_per_tonne": 2.75},
    "Puerto San Julian": {"country": "Argentina", "rate_per_tonne": 1.90},
    "Guam": {"country": "United States", "rate_per_tonne": 4.40},
    "Cebu": {"country": "Philippines", "rate_per_tonne": 2.20},
}

#: nautical miles, one way
DISTANCES = {
    ("Seville", "Rio"): 4870,
    ("Rio", "Puerto San Julian"): 1730,
    ("Puerto San Julian", "Guam"): 8960,
    ("Guam", "Cebu"): 1290,
}


def distance(a, b):
    """Nautical miles between two ports, either way round."""
    return DISTANCES.get((a, b)) or DISTANCES[(b, a)]


def known(port):
    return port in PORTS
