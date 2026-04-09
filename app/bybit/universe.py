"""
Universe Manager — determines which coins the bot actively monitors.

Filtering rules:
1. Only USDT-quoted spot pairs
2. Exclude top majors (BTC, ETH, etc.)
3. Minimum 24h volume in USDT
4. Minimum listing age (skip newly listed coins for N days)
5. Periodically refreshed (default: every 5 min)

Motivation:
- Analyzing 500+ coins in real-time is wasteful and noisy
- Filtering to speculative mid/small caps is the actual signal space
- New listings need calibration period (too volatile without history)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

from app.config import get_settings
from app.utils.logging import get_logger
from app.utils.time_utils import utcnow, ms_to_dt

logger = get_logger(__name__)
settings = get_settings()


class UniverseManager:
    """
    Maintains the set of symbols actively monitored by the bot.
    Thread-safe via asyncio lock.
    """

    def __init__(self, rest_client) -> None:
        self._rest = rest_client
        self._symbols: set[str] = set()
        self._symbol_meta: dict[str, dict] = {}  # symbol -> metadata
        self._lock = asyncio.Lock()
        self._last_refresh: datetime | None = None

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(self._symbols)

    @property
    def symbol_count(self) -> int:
        return len(self._symbols)

    async def refresh(self) -> set[str]:
        """
        Full refresh of the tradeable universe.
        Returns the new set of symbols.
        """
        async with self._lock:
            try:
                new_symbols = await self._compute_universe()
                added = new_symbols - self._symbols
                removed = self._symbols - new_symbols
                self._symbols = new_symbols
                self._last_refresh = utcnow()

                if added:
                    logger.info("Universe: added symbols", count=len(added), symbols=list(added)[:10])
                if removed:
                    logger.info("Universe: removed symbols", count=len(removed), symbols=list(removed)[:10])

                logger.info(
                    "Universe refreshed",
                    total=len(new_symbols),
                    added=len(added),
                    removed=len(removed),
                )
                return new_symbols
            except Exception as e:
                logger.error("Universe refresh failed", error=str(e))
                return self._symbols


    async def _compute_universe(self) -> set[str]:
        instruments = await self._rest.get_instruments(category="linear")
        tickers = await self._rest.get_tickers(category="linear")
        ticker_map = {t["symbol"]: t for t in tickers}


        cutoff_date = utcnow() - timedelta(days=settings.universe_min_listing_age_days)
        selected: set[str] = set()

        for inst in instruments:
            symbol: str = inst.get("symbol", "")
            base: str = inst.get("baseCoin", "").upper()
            quote: str = inst.get("quoteCoin", "").upper()
            status: str = inst.get("status", "")

            if quote != "USDT":
                continue
            if status != "Trading":
                continue
            if base in settings.excluded_base_assets:
                continue



            launch_time_ms = inst.get("launchTime")
            if launch_time_ms:
                try:
                    listing_dt = ms_to_dt(int(launch_time_ms))
                    if listing_dt > cutoff_date:
                        continue
                except (ValueError, TypeError):
                    pass

            ticker = ticker_map.get(symbol)
            if ticker is None:
                continue

            try:
                volume_24h = float(ticker.get("turnover24h", 0))
            except (ValueError, TypeError):
                continue

            if volume_24h < settings.universe_min_24h_volume_usdt:
                continue

            selected.add(symbol)
            self._symbol_meta[symbol] = {
                "base": base,
                "quote": quote,
                "volume_24h": volume_24h,
                "price": float(ticker.get("lastPrice", 0)),
                "price_change_pct": float(ticker.get("price24hPcnt", 0)) * 100,
            }

        # Лог топ-10 по объёму для диагностики
        if selected:
            top10 = sorted(
                selected,
                key=lambda s: self._symbol_meta[s]["volume_24h"],
                reverse=True,
            )[:10]
            logger.info(
                "Universe top-10 by volume",
                symbols=[
                    f"{s}(${self._symbol_meta[s]['volume_24h']/1_000_000:.1f}M)"
                    for s in top10
                ],
            )

        return selected



    async def run_forever(self) -> None:
        """Periodically refresh the universe."""
        while True:
            await self.refresh()
            await asyncio.sleep(settings.universe_refresh_interval)

    def get_meta(self, symbol: str) -> dict:
        return self._symbol_meta.get(symbol, {})

    def is_active(self, symbol: str) -> bool:
        return symbol in self._symbols
