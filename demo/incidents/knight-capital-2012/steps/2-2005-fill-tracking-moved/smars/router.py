from smars.flags import POWER_PEG
from smars.market import send_child
from smars.orders import ParentOrder
from smars.power_peg import power_peg, track_cumulative


def route(order: ParentOrder) -> None:
    track_cumulative(order, 0)
    if order.flags & POWER_PEG:
        power_peg(order)
    else:
        order.filled += send_child(order.symbol, order.qty)
