"""
Orderbook state management — handles WS delta/snapshot updates.
Maintains a local order book mirror per symbol.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


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


def make_ob_snapshot(orderbook_data: dict, current_price: float) -> dict[str, Any]:
    """Build compact OB snapshot with aggregated metrics for ML training.

    Args:
        orderbook_data: {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}
        current_price: last trade price (used for USDT conversion).

    Returns dict with snapshot + aggregated fields (all USDT-denominated).
    """
    bids = orderbook_data.get("bids") or []
    asks = orderbook_data.get("asks") or []

    top10_bids = bids[:10]
    top10_asks = asks[:10]

    # USDT volumes: price * qty
    bid_vol = sum(p * q for p, q in top10_bids)
    ask_vol = sum(p * q for p, q in top10_asks)
    total_vol = bid_vol + ask_vol

    imbalance = (bid_vol - ask_vol) / total_vol if total_vol > 0 else 0.0

    # Spread in basis points
    best_bid = top10_bids[0][0] if top10_bids else 0.0
    best_ask = top10_asks[0][0] if top10_asks else 0.0
    mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else current_price
    spread_bps = ((best_ask - best_bid) / mid * 10_000) if mid > 0 else 0.0

    # Walls — largest single level in top 20 (USDT size)
    top20_bids = bids[:20]
    top20_asks = asks[:20]

    bid_wall_price, bid_wall_size = None, None
    if top20_bids:
        best = max(top20_bids, key=lambda x: x[0] * x[1])
        bid_wall_price = best[0]
        bid_wall_size = best[0] * best[1]

    ask_wall_price, ask_wall_size = None, None
    if top20_asks:
        best = max(top20_asks, key=lambda x: x[0] * x[1])
        ask_wall_price = best[0]
        ask_wall_size = best[0] * best[1]

    return {
        "snapshot": {
            "bids": top10_bids,
            "asks": top10_asks,
        },
        "bid_volume_top10": round(bid_vol, 2),
        "ask_volume_top10": round(ask_vol, 2),
        "imbalance_top10": round(imbalance, 6),
        "spread_bps": round(spread_bps, 2),
        "bid_wall_price": bid_wall_price,
        "bid_wall_size": round(bid_wall_size, 2) if bid_wall_size else None,
        "ask_wall_price": ask_wall_price,
        "ask_wall_size": round(ask_wall_size, 2) if ask_wall_size else None,
    }
