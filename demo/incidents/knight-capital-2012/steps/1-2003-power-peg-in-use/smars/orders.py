from dataclasses import dataclass


@dataclass
class ParentOrder:
    symbol: str
    qty: int
    flags: int = 0
    filled: int = 0
