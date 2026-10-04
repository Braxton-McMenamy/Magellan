"""The crew roster: who sails on which ship."""

ROSTER = {
    "Trinidad": ["Elcano", "Pigafetta", "Barbosa"],
    "Victoria": ["Espinosa", "Albo"],
}


def crew_of(ship):
    return list(ROSTER.get(ship, []))


def signed_on(name):
    """The ship a sailor is signed on to, if any."""
    return next((ship for ship, crew in ROSTER.items() if name in crew), None)
