"""Voyages: the route a ship takes, and charters that renew every year."""

from datetime import date, timedelta

from .manifest import build_manifest


# DEMO 1 (mild): let callers leave the stops out.
#   Put the cursor on the `def` line below and press Ctrl+Shift+K (delete the line),
#   then press Ctrl+/ on the commented line under it.
def plan_route(origin, destination, stops=None):
# def plan_route(origin, destination, stops=[]):
    """The ports a voyage calls at, in order."""
    route = [origin]
    route.extend(stops or [])
    route.append(destination)
    return route


def depart(ship, cargo, origin, destination, stops=None):
    """File the manifest and sail."""
    return build_manifest(ship, cargo, plan_route(origin, destination, stops))


def charter_renewal(start: date) -> date:
    """A ship's charter renews on the same day the following year."""
    # DEMO 2 (high): "the same day next year". Ctrl+Shift+K on the `return` line below,
    #   then Ctrl+/ on the commented one. It works on every day but February 29.
    return start + timedelta(days=365)
    # return start.replace(year=start.year + 1)
