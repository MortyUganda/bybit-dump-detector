"""
Bybit REST client wrapper using pybit.
Handles: ticker data, candles, orderbook, instrument info, 24h stats.

DATA SOURCES (REST):
- GET /v5/market/tickers          — 24h volume, price, turnover
- GET /v5/market/kline            — OHLCV candles
- GET /v5/market/orderbook        — Orderbook depth
- GET /v5/market/instruments-info — Symbol metadata (listing date etc.)
- GET /v5/market/recent-trade     — Recent trades (REST fallback)
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

BYBIT_BASE_URL = "https://api.bybit.com"
BYBIT_TESTNET_URL = "https://api-testnet.bybit.com"


class BybitRestClient:
    """
    Async REST client for Bybit public market data endpoints.
    Authentication not required for market data (read-only).
    """

    def __init__(self) -> None:
        self._base_url = BYBIT_TESTNET_URL if settings.bybit_testnet else BYBIT_BASE_URL
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(
            base_url=self._base_url,
            connector=connector,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10),
        )
        logger.info("Bybit REST client started", base_url=self._base_url)

    async def stop(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("Bybit REST client stopped")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _get(self, path: str, params: dict | None = None) -> dict:
        assert self._session, "Client not started — call .start() first"
        async with self._session.get(path, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data.get("retCode") != 0:
                raise ValueError(f"Bybit API error: {data.get('retMsg')} (path={path})")
            return data

    # ── Instruments ───────────────────────────────────────────────

    async def get_instruments(self, category: str = "spot") -> list[dict]:
        """
        Fetch all tradeable instruments for a category (spot/linear).
        Returns: list of instrument info dicts.
        Used by: UniverseManager
        Frequency: every 5 min
        Store: only filtered symbol list
        """
        result = []
        cursor = None
        while True:
            params: dict[str, Any] = {"category": category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/v5/market/instruments-info", params)
            items = data["result"]["list"]
            result.extend(items)
            cursor = data["result"].get("nextPageCursor")
            if not cursor:
                break
        logger.debug("Fetched instruments", category=category, count=len(result))
        return result

    # ── Tickers (24h stats) ───────────────────────────────────────

    async def get_tickers(self, category: str = "spot") -> list[dict]:
        """
        24h volume, turnover, price change, high/low, last price.
        Used by: UniverseManager (volume filter) + ScoringEngine
        Frequency: every 30s per REST poll (WS preferred)
        Store: only aggregated stats per symbol
        """
        data = await self._get("/v5/market/tickers", {"category": category})
        return data["result"]["list"]

    async def get_ticker(self, symbol: str, category: str = "spot") -> dict | None:
        """Single symbol ticker."""
        data = await self._get("/v5/market/tickers", {"category": category, "symbol": symbol})
        items = data["result"]["list"]
        return items[0] if items else None

    # ── Candles ───────────────────────────────────────────────────

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1",  # 1m candles
        limit: int = 200,
        category: str = "spot",
    ) -> list[dict]:
        """
        OHLCV candles.
        Used by: FeatureCalculator (RSI, VWAP, ATR, wick patterns)
        Frequency: every 60s per REST poll (WS preferred for 1m)
        Store: last 200 candles per symbol in Redis, daily OHLCV in Postgres
        Intervals: 1, 3, 5, 15, 60, 240, D
        """
        data = await self._get(
            "/v5/market/kline",
            {"category": category, "symbol": symbol, "interval": interval, "limit": limit},
        )
        raw = data["result"]["list"]
        # Bybit returns: [startTime, open, high, low, close, volume, turnover]
        return [
            {
                "ts": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "turnover": float(row[6]),
            }
            for row in raw
        ]

    # ── Orderbook ─────────────────────────────────────────────────

    async def get_orderbook(
        self, symbol: str, limit: int = 25, category: str = "spot"
    ) -> dict:
        """
        Orderbook snapshot (bids + asks).
        Used by: FeatureCalculator (OB imbalance, bid support thinning, spread)
        Frequency: every 5s per REST poll (WS preferred)
        Store: only computed features (imbalance ratio, spread, depth USDT)
        """
        data = await self._get(
            "/v5/market/orderbook",
            {"category": category, "symbol": symbol, "limit": limit},
        )
        raw = data["result"]
        return {
            "symbol": symbol,
            "ts": int(raw["ts"]),
            "bids": [[float(p), float(q)] for p, q in raw["b"]],
            "asks": [[float(p), float(q)] for p, q in raw["a"]],
        }

    # ── Recent Trades (REST fallback) ─────────────────────────────

    async def get_recent_trades(
        self, symbol: str, limit: int = 200, category: str = "spot"
    ) -> list[dict]:
        """
        Recent public trades. Used when WS trade stream not yet connected.
        WS is preferred source; this is REST fallback.
        Store: raw ticks in Redis rolling buffer (last 1000 per symbol)
        """
        data = await self._get(
            "/v5/market/recent-trade",
            {"category": category, "symbol": symbol, "limit": limit},
        )
        return [
            {
                "ts": int(t["time"]),
                "price": float(t["price"]),
                "qty": float(t["size"]),
                "side": t["side"],  # Buy | Sell
                "exec_id": t.get("execId", ""),
            }
            for t in data["result"]["list"]
        ]

    # ── Open Interest (Linear/Perpetual only) ─────────────────────

    async def get_open_interest(
        self, symbol: str, interval: str = "5min", limit: int = 50
    ) -> list[dict]:
        """
        Open Interest history for linear perpetuals.
        NOTE: Only available for linear category, NOT spot.
        Used for: OI spike detection (confirms leveraged speculation)
        Frequency: every 5 min
        Store: aggregated OI delta in candle_features table
        """
        try:
            data = await self._get(
                "/v5/market/open-interest",
                {"category": "linear", "symbol": symbol, "intervalTime": interval, "limit": limit},
            )
            return data["result"]["list"]
        except Exception as e:
            logger.warning("OI not available for symbol", symbol=symbol, error=str(e))
            return []

    # ── Funding Rate ──────────────────────────────────────────────

    async def get_funding_rate(self, symbol: str, limit: int = 10) -> list[dict]:
        """
        Funding rate history for linear perpetuals.
        Extreme positive funding = crowded longs = dump risk.
        NOTE: Only linear perpetuals.
        """
        try:
            data = await self._get(
                "/v5/market/funding/history",
                {"category": "linear", "symbol": symbol, "limit": limit},
            )
            return data["result"]["list"]
        except Exception as e:
            logger.warning("Funding rate not available", symbol=symbol, error=str(e))
            return []
