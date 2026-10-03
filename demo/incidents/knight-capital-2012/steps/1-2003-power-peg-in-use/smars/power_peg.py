from smars.market import send_child
from smars.orders import ParentOrder


def track_cumulative(order: ParentOrder, filled: int) -> None:
    order.filled += filled


def power_peg(order: ParentOrder) -> None:
    """Keep sending child orders until the parent order is filled."""
    while order.filled < order.qty:
        filled = send_child(order.symbol, order.qty - order.filled)
        track_cumulative(order, filled)
