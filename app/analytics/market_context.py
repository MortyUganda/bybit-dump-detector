"""
Market Context — BTC/ETH market state for signal filtering.

When BTC is rallying hard, all alts pump together.
Shorting alts during a BTC rally is a losing strategy.
This module provides a lightweight BTC momentum check.
"""
from __future__ import annotations

import time

from app.utils.logging import get_logger

logger = get_logger(__name__)

# Suppress all alt short signals if BTC 15m change exceeds this %
BTC_PUMP_THRESHOLD = 1.0


class MarketContext:
    """Fetches and caches BTC/ETH market state for signal filtering."""

    def __init__(self, rest_client) -> None:
        self._rest = rest_client
        self._btc_change_15m: float = 0.0
        self._last_update: float = 0.0

    async def refresh(self) -> None:
        """Refresh every 60s."""
        now = time.time()
        if now - self._last_update < 60:
            return
        try:
            candles = await self._rest.get_klines(
                "BTCUSDT", interval="15", limit=2, category="linear",
            )
            if len(candles) >= 2:
                prev_close = float(candles[-2]["close"])
                curr_close = float(candles[-1]["close"])
                if prev_close > 0:
                    self._btc_change_15m = (curr_close - prev_close) / prev_close * 100
            self._last_update = now
        except Exception as e:
            logger.warning("BTC context refresh failed", error=str(e))

    @property
    def btc_change_15m(self) -> float:
        return self._btc_change_15m

    def should_suppress_shorts(self) -> bool:
        """Return True if BTC is pumping — suppress all alt short signals."""
        return self._btc_change_15m > BTC_PUMP_THRESHOLD
