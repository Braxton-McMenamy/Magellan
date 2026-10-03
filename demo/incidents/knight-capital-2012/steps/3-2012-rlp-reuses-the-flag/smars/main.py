from smars.orders import ParentOrder
from smars.router import route


def on_parent_order(symbol: str, qty: int, flags: int) -> int:
    order = ParentOrder(symbol, qty, flags)
    route(order)
    return order.filled
