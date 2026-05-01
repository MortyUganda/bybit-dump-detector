"""
Market Context — BTC/ETH market state for signal filtering.

When BTC is rallying hard, all alts pump together.
Shorting alts during a BTC rally is a losing strategy.
This module provides a lightweight BTC momentum check.
"""
from __future__ import annotations

import asyncio
import time

import numpy as np

from app.utils.logging import get_logger

logger = get_logger(__name__)

# Suppress all alt short signals if BTC 15m change exceeds this %
# 2.0% — only suppress during strong BTC rallies, not minor moves
BTC_PUMP_THRESHOLD = 2.0


class MarketContext:
    """Fetches and caches BTC/ETH market state for signal filtering."""

    def __init__(self, rest_client) -> None:
        self._rest = rest_client
        self._btc_change_15m: float = 0.0
        self._btc_change_1h: float = 0.0
        self._btc_change_4h: float = 0.0
        self._btc_change_24h: float = 0.0
        self._btc_adx_1h: float = 0.0
        self._btc_atr_pct_1h: float = 0.0
        self._last_update: float = 0.0

    async def refresh(self) -> None:
        """Refresh every 60s."""
        now = time.time()
        if now - self._last_update < 60:
            return
        try:
            candles_15m, candles_1h, candles_4h, candles_24h = await asyncio.gather(
                self._rest.get_klines(
                    "BTCUSDT", interval="15", limit=4, category="linear",
                ),
                self._rest.get_klines(
                    "BTCUSDT", interval="60", limit=20, category="linear",
                ),
                self._rest.get_klines(
                    "BTCUSDT", interval="240", limit=4, category="linear",
                ),
                self._rest.get_klines(
                    "BTCUSDT", interval="D", limit=4, category="linear",
                ),
            )

            self._btc_change_15m = self._calc_change(candles_15m)
            self._btc_change_1h = self._calc_change(candles_1h)
            self._btc_change_4h = self._calc_change(candles_4h)
            self._btc_change_24h = self._calc_change(candles_24h)

            # ADX и ATR на 1h свечах (14 периодов)
            if len(candles_1h) >= 16:
                self._btc_adx_1h = self._calc_adx(candles_1h, 14)
                self._btc_atr_pct_1h = self._calc_atr_pct(candles_1h, 14)

            self._last_update = now
        except Exception as e:
            logger.warning("BTC context refresh failed", error=str(e))

    @staticmethod
    def _calc_change(candles: list) -> float:
        """Calculate % change from two most recent completed candles."""
        if len(candles) >= 3:
            prev_close = float(candles[-3]["close"])
            curr_close = float(candles[-2]["close"])
            if prev_close > 0:
                return (curr_close - prev_close) / prev_close * 100
        return 0.0

    @staticmethod
    def _calc_adx(candles: list, period: int = 14) -> float:
        """ADX (Average Directional Index) — Wilder's smoothing, 0-100."""
        if len(candles) < period + 2:
            return 0.0
        highs = np.array([float(c["high"]) for c in candles])
        lows = np.array([float(c["low"]) for c in candles])
        closes = np.array([float(c["close"]) for c in candles])

        # True Range, +DM, -DM
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )
        up_move = highs[1:] - highs[:-1]
        down_move = lows[:-1] - lows[1:]
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        if len(tr) < period:
            return 0.0

        # Wilder's smoothing (EMA with alpha=1/period)
        atr = float(np.mean(tr[:period]))
        smooth_plus = float(np.mean(plus_dm[:period]))
        smooth_minus = float(np.mean(minus_dm[:period]))

        dx_values = []
        for i in range(period, len(tr)):
            atr = atr - atr / period + tr[i]
            smooth_plus = smooth_plus - smooth_plus / period + plus_dm[i]
            smooth_minus = smooth_minus - smooth_minus / period + minus_dm[i]
            if atr > 0:
                plus_di = 100.0 * smooth_plus / atr
                minus_di = 100.0 * smooth_minus / atr
                di_sum = plus_di + minus_di
                if di_sum > 0:
                    dx_values.append(100.0 * abs(plus_di - minus_di) / di_sum)

        if not dx_values:
            return 0.0

        # ADX = EMA of DX (Wilder's smoothing)
        adx = float(np.mean(dx_values[:period])) if len(dx_values) >= period else float(np.mean(dx_values))
        for dx in dx_values[period:]:
            adx = adx - adx / period + dx / period
        return min(100.0, max(0.0, adx))

    @staticmethod
    def _calc_atr_pct(candles: list, period: int = 14) -> float:
        """ATR / price — нормализованная волатильность."""
        if len(candles) < period + 1:
            return 0.0
        highs = np.array([float(c["high"]) for c in candles])
        lows = np.array([float(c["low"]) for c in candles])
        closes = np.array([float(c["close"]) for c in candles])

        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )
        atr = float(np.mean(tr[-period:])) if len(tr) >= period else float(np.mean(tr))
        price = closes[-1]
        if price > 0:
            return atr / price * 100
        return 0.0

    @property
    def btc_change_15m(self) -> float:
        return self._btc_change_15m

    @property
    def btc_change_1h(self) -> float:
        return self._btc_change_1h

    @property
    def btc_change_4h(self) -> float:
        return self._btc_change_4h

    @property
    def btc_change_24h(self) -> float:
        return self._btc_change_24h

    @property
    def btc_adx_1h(self) -> float:
        return self._btc_adx_1h

    @property
    def btc_atr_pct_1h(self) -> float:
        return self._btc_atr_pct_1h

    def should_suppress_shorts(self) -> bool:
        """Return True if BTC is pumping — suppress all alt short signals."""
        return self._btc_change_15m > BTC_PUMP_THRESHOLD
