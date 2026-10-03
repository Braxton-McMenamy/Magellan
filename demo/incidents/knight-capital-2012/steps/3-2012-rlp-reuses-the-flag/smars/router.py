from smars.flags import RLP
from smars.market import send_child
from smars.orders import ParentOrder
from smars.rlp import rlp


def route(order: ParentOrder) -> None:
    if order.flags & RLP:
        rlp(order)
    else:
        order.filled += send_child(order.symbol, order.qty)
