from smars.market import send_child
from smars.orders import ParentOrder


def rlp(order: ParentOrder) -> None:
    """Route a retail order into the NYSE Retail Liquidity Program."""
    order.filled += send_child(order.symbol, order.qty)
