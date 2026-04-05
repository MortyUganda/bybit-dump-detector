"""
Orderbook state management — handles WS delta/snapshot updates.
Maintains a local order book mirror per symbol.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class OrderbookLevel:
    price: float
    qty: float


class OrderbookState:
    """
    Local mirror of the Bybit orderbook for one symbol.
    Handles snapshot + delta updates from WS.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._bids: dict[float, float] = {}  # price -> qty
        self._asks: dict[float, float] = {}

    def apply_snapshot(self, data: dict) -> None:
        """Full replacement — called on initial WS snapshot."""
        self._bids = {float(p): float(q) for p, q in data.get("b", [])}
        self._asks = {float(p): float(q) for p, q in data.get("a", [])}

    def apply_delta(self, data: dict) -> None:
        """Incremental update — called on subsequent WS deltas."""
        for p, q in data.get("b", []):
            price, qty = float(p), float(q)
            if qty == 0:
                self._bids.pop(price, None)
            else:
                self._bids[price] = qty

        for p, q in data.get("a", []):
            price, qty = float(p), float(q)
            if qty == 0:
                self._asks.pop(price, None)
            else:
                self._asks[price] = qty

    def get_snapshot(self, levels: int = 25) -> dict:
        """Return current state as sorted bids/asks."""
        bids = sorted(self._bids.items(), key=lambda x: -x[0])[:levels]
        asks = sorted(self._asks.items(), key=lambda x: x[0])[:levels]
        return {
            "bids": [[p, q] for p, q in bids],
            "asks": [[p, q] for p, q in asks],
        }


class OrderbookAnalyzer:
    """
    Manages orderbook state for all tracked symbols.
    """

    def __init__(self) -> None:
        self._books: dict[str, OrderbookState] = defaultdict(lambda: OrderbookState(""))

    def handle_ws_message(self, symbol: str, msg: dict) -> dict | None:
        """
        Process a WS orderbook message.
        Returns updated snapshot dict or None.
        """
        if symbol not in self._books:
            self._books[symbol] = OrderbookState(symbol)

        book = self._books[symbol]
        msg_type = msg.get("type", "delta")
        data = msg.get("data", {})

        if msg_type == "snapshot":
            book.apply_snapshot(data)
        else:
            book.apply_delta(data)

        return book.get_snapshot()

    def get_snapshot(self, symbol: str, levels: int = 25) -> dict | None:
        if symbol not in self._books:
            return None
        return self._books[symbol].get_snapshot(levels)
