"""The manifest a ship files before it sails."""

from .cargo import heaviest
from .fees import voyage_fees
from .ports import distance, known


def sea_miles(route):
    """The whole voyage, port to port."""
    return sum(distance(a, b) for a, b in zip(route, route[1:]))


def build_manifest(ship, cargo, route):
    """Everything the harbour masters ask for."""
    unknown = [p for p in route if not known(p)]
    if unknown:
        raise ValueError(f"no such port: {', '.join(unknown)}")
    return {
        "ship": ship,
        "route": route,
        "miles": sea_miles(route),
        "heaviest": heaviest(cargo)["kind"],
        "fees": voyage_fees(route, cargo),
    }
