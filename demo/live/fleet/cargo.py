"""Cargo: what a ship carries, and what it weighs."""

HAZARDOUS = {"fuel", "acid", "fireworks"}


# DEMO 3 (critical): give total_weight a required `unit` parameter.
#   Put the cursor on the `def` line below and press Ctrl+Shift+K (delete the line),
#   then press Ctrl+/ on the commented line under it. fees.py still calls the old way.
def total_weight(items):
# def total_weight(items, unit):
    """Total weight of a ship's cargo, in kilograms."""
    return sum(item["kg"] * item.get("count", 1) for item in items)


def hazardous(items):
    """The items that need a hazard placard."""
    return [item for item in items if item["kind"] in HAZARDOUS]


def heaviest(items):
    """The single heaviest line on the manifest."""
    return max(items, key=lambda item: item["kg"] * item.get("count", 1))
