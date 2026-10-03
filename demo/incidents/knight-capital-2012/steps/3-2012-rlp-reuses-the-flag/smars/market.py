def send_child(symbol: str, qty: int) -> int:
    """Send one child order to the exchange; returns the shares filled."""
    return min(qty, 100)
