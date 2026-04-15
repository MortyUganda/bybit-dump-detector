"""
Market Context — BTC/ETH market state for signal filtering.

When BTC is rallying hard, all alts pump together.
Shorting alts during a BTC rally is a losing strategy.
This module provides a lightweight BTC momentum check.
"""
from __future__ import annotations

import asyncio
import time

from app.utils.logging import get_logger

logger = get_logger(__name__)

# Suppress all alt short signals if BTC 15m change exceeds this %
# 2.0% — only suppress during strong BTC rallies, not minor moves
BTC_PUMP_THRESHOLD = 2.0


class MarketContext:
    """Fetches and caches BTC/ETH market state for signal filtering."""

    def __init__(self, rest_client) -> None:
        self._rest = rest_client
        self._btc_change_1m: float | None = None
        self._btc_change_5m: float | None = None
        self._btc_change_15m: float | None = None
        self._btc_change_1h: float | None = None
        self._last_update: float = 0.0

    async def refresh(self) -> None:
        """Refresh every 60s."""
        now = time.time()
        if now - self._last_update < 60:
            return
        try:
            # Fetch all four intervals in parallel
            candles_1m, candles_5m, candles_15m, candles_1h = await asyncio.gather(
                self._rest.get_klines(
                    "BTCUSDT", interval="1", limit=4, category="linear",
                ),
                self._rest.get_klines(
                    "BTCUSDT", interval="5", limit=4, category="linear",
                ),
                self._rest.get_klines(
                    "BTCUSDT", interval="15", limit=4, category="linear",
                ),
                self._rest.get_klines(
                    "BTCUSDT", interval="60", limit=4, category="linear",
                ),
            )
            self._btc_change_1m = self._calc_change(candles_1m)
            self._btc_change_5m = self._calc_change(candles_5m)
            self._btc_change_15m = self._calc_change(candles_15m)
            self._btc_change_1h = self._calc_change(candles_1h)
            self._last_update = now
        except Exception as e:
            logger.warning("BTC context refresh failed", error=str(e))
            # On failure, reset to None so callers can skip rather than block
            self._btc_change_1m = None
            self._btc_change_5m = None
            self._btc_change_15m = None
            self._btc_change_1h = None

    @staticmethod
    def _calc_change(candles: list) -> float:
        """Calculate % change from two most recent completed candles."""
        if len(candles) >= 3:
            prev_close = float(candles[-3]["close"])
            curr_close = float(candles[-2]["close"])
            if prev_close > 0:
                return (curr_close - prev_close) / prev_close * 100
        return 0.0

    @property
    def btc_change_1m(self) -> float | None:
        return self._btc_change_1m

    @property
    def btc_change_5m(self) -> float | None:
        return self._btc_change_5m

    @property
    def btc_change_15m(self) -> float | None:
        return self._btc_change_15m

    @property
    def btc_change_1h(self) -> float | None:
        return self._btc_change_1h

    def should_suppress_shorts(self) -> bool:
        """Return True if BTC is pumping — suppress all alt short signals."""
        if self._btc_change_15m is None:
            return False
        return self._btc_change_15m > BTC_PUMP_THRESHOLD
