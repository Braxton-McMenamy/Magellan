"""Harbour fees: every port charges by the tonne, plus a hazard surcharge."""

from .cargo import hazardous, total_weight
from .ports import PORTS

HAZARD_SURCHARGE = 120.0


def harbour_fee(port, cargo):
    """What a port charges to unload this cargo."""
    tonnes = total_weight(cargo) / 1000
    fee = tonnes * PORTS[port]["rate_per_tonne"]
    if hazardous(cargo):
        fee += HAZARD_SURCHARGE
    return round(fee, 2)


def voyage_fees(route, cargo):
    """Fees at every port the ship unloads at (all but the first)."""
    return {port: harbour_fee(port, cargo) for port in route[1:]}
