"""
Ingestion Service — the data pipeline.

Responsibilities:
1. Maintain WebSocket connections to Bybit for all universe symbols
2. Route WS messages to FeatureCalculators
3. Periodically fetch candles + orderbook via REST to fill in history
4. Write aggregated features to Redis (for fast reads by analyzer + bot)

Architecture:
  WS Trades   → on_trade()   → FeatureCalculator.update_trade()
  WS Tickers  → on_ticker()  → update price/vol in feature calc
  WS OB       → on_orderbook() → OrderbookAnalyzer → FeatureCalculator.update_orderbook()
  REST Candles → every 60s   → FeatureCalculator.update_candles()
  REST Tickers → every 30s   → universe volume filter update

Feature state is stored only in-memory (Redis) — no raw tick persistence by default.
TODO: Optional raw tick logging to TimescaleDB for backtesting.
"""
from __future__ import annotations

import asyncio
import json
from typing import Dict

import redis.asyncio as aioredis

from app.analytics.features import CandleData, CoinFeatures, FeatureCalculator, TradeTick
from app.analytics.orderbook import OrderbookAnalyzer
from app.bybit.rest_client import BybitRestClient
from app.bybit.universe import UniverseManager
from app.bybit.ws_client import BybitWSClient
from app.config import get_settings
from app.utils.logging import get_logger
from app.utils.time_utils import utcnow_ts

logger = get_logger(__name__)
settings = get_settings()

# Redis key patterns
REDIS_FEATURES_KEY = "features:{symbol}"   # JSON of CoinFeatures
REDIS_FEATURES_TTL = 300                    # 5 min — stale after this


class IngestionService:
    """
    Runs the full ingestion pipeline for all universe symbols.
    Designed to run as a background task indefinitely.
    """

    def __init__(
        self,
        rest: BybitRestClient,
        universe: UniverseManager,
        redis: aioredis.Redis,
    ) -> None:
        self._rest = rest
        self._universe = universe
        self._redis = redis
        self._calculators: Dict[str, FeatureCalculator] = {}
        self._ob_analyzer = OrderbookAnalyzer()
        self._ws_spot: BybitWSClient | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        # Initial universe + candle history load
        await self._universe.refresh()
        await self._bootstrap_calculators()

        # Start WebSocket
        self._ws_spot = BybitWSClient(
            category="linear",
            on_trade=self._on_trade,
            on_ticker=self._on_ticker,
            on_orderbook=self._on_orderbook,
        )
        await self._ws_spot.start()

        # Subscribe to current universe
        symbols = list(self._universe.symbols)
        if symbols:
            await self._ws_spot.subscribe_trades(symbols)
            await self._ws_spot.subscribe_tickers(symbols)
            await self._ws_spot.subscribe_orderbook(symbols)

        logger.info("Ingestion service started", symbols=len(symbols))

        # Background tasks
        asyncio.create_task(self._candle_refresh_loop())
        asyncio.create_task(self._universe_sync_loop())
        asyncio.create_task(self._trend_refresh_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws_spot:
            await self._ws_spot.stop()

    # ── WebSocket callbacks ───────────────────────────────────────

    async def _on_trade(self, symbol: str, tick: dict) -> None:
        """Called for each public trade from WS."""
        if not self._universe.is_active(symbol):
            return

        calc = self._get_or_create_calculator(symbol)
        try:
            trade = TradeTick(
                ts=float(tick.get("T", 0)) / 1000,  # ms → s
                price=float(tick.get("p", 0)),
                qty=float(tick.get("v", 0)),
                side=tick.get("S", "Buy"),  # Buy | Sell
                usdt_value=float(tick.get("p", 0)) * float(tick.get("v", 0)),
            )
            calc.update_trade(trade)
        except (KeyError, ValueError, TypeError) as e:
            logger.debug("Trade parse error", symbol=symbol, error=str(e))

    async def _on_ticker(self, symbol: str, data: dict) -> None:
        """Called on ticker update from WS."""
        if not self._universe.is_active(symbol):
            return
        calc = self._get_or_create_calculator(symbol)
        try:
            vol = float(data.get("turnover24h", 0))
            calc.update_24h_volume(vol)
        except (ValueError, TypeError):
            pass

    async def _on_orderbook(self, symbol: str, msg: dict) -> None:
        """Called on orderbook update from WS."""
        if not self._universe.is_active(symbol):
            return
        snapshot = self._ob_analyzer.handle_ws_message(symbol, msg)
        if snapshot:
            calc = self._get_or_create_calculator(symbol)
            calc.update_orderbook(snapshot)

    # ── REST refresh loops ────────────────────────────────────────

    async def _candle_refresh_loop(self) -> None:
        """Fetch and update 1m/5m candles for all symbols every 60s."""
        while self._running:
            try:
                symbols = list(self._universe.symbols)
                for i, symbol in enumerate(symbols):
                    if not self._running:
                        break
                    await self._refresh_candles(symbol)

                    # Сохраняем свечи в Redis после обновления
                    calc = self._calculators.get(symbol)
                    if calc:
                        await calc.save_to_redis(self._redis)

                    if i % 10 == 9:
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.error("Candle refresh loop error", error=str(e))
            await asyncio.sleep(60)


    async def _refresh_candles(self, symbol: str) -> None:
        calc = self._get_or_create_calculator(symbol)
        try:
            raw_1m = await self._rest.get_klines(symbol, interval="1", limit=120, category="linear")
            candles_1m = [CandleData(**c) for c in raw_1m]
            calc.update_candles(candles_1m, "1")

            raw_5m = await self._rest.get_klines(symbol, interval="5", limit=100, category="linear")
            candles_5m = [CandleData(**c) for c in raw_5m]
            calc.update_candles(candles_5m, "5")
        except Exception as e:
            logger.debug("Candle refresh failed", symbol=symbol, error=str(e))

    async def _trend_refresh_loop(self) -> None:
        """Fetch 1h candles and update trend context every 5 minutes."""
        while self._running:
            try:
                symbols = list(self._universe.symbols)
                batch_size = 20
                for i in range(0, len(symbols), batch_size):
                    if not self._running:
                        break
                    batch = symbols[i: i + batch_size]
                    tasks = [self._refresh_trend(sym) for sym in batch]
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await asyncio.sleep(1.0)
            except Exception as e:
                logger.error("Trend refresh loop error", error=str(e))
            await asyncio.sleep(300)

    async def _refresh_trend(self, symbol: str) -> None:
        """Fetch 1h candles for a symbol and update trend context."""
        calc = self._calculators.get(symbol)
        if not calc:
            return
        try:
            raw_1h = await self._rest.get_klines(
                symbol, interval="60", limit=60, category="linear",
            )
            candles_1h = [CandleData(**c) for c in raw_1h]
            calc.update_trend(candles_1h)
        except Exception as e:
            logger.debug("Trend refresh failed", symbol=symbol, error=str(e))

    async def _universe_sync_loop(self) -> None:
        """Re-subscribe WS when universe changes."""
        prev_symbols: set[str] = set()
        while self._running:
            await asyncio.sleep(settings.universe_refresh_interval)
            await self._universe.refresh()
            current = self._universe.symbols

            added = current - prev_symbols
            removed = prev_symbols - current

            if added and self._ws_spot:
                await self._ws_spot.subscribe_trades(list(added))
                await self._ws_spot.subscribe_tickers(list(added))
                await self._ws_spot.subscribe_orderbook(list(added))
                for sym in added:
                    asyncio.create_task(self._refresh_candles(sym))

            if removed and self._ws_spot:
                await self._ws_spot.unsubscribe(list(removed))

            prev_symbols = set(current)

    # ── Feature publishing ────────────────────────────────────────

    async def publish_features(self, symbol: str) -> CoinFeatures | None:
        """Compute features for a symbol and write to Redis."""
        calc = self._calculators.get(symbol)
        if not calc:
            return None
        try:
            features = calc.compute()
            key = REDIS_FEATURES_KEY.format(symbol=symbol)
            await self._redis.setex(key, REDIS_FEATURES_TTL, json.dumps(features.__dict__))
            return features
        except Exception as e:
            logger.debug("Feature publish error", symbol=symbol, error=str(e))
            return None

    async def get_all_features(self) -> list[CoinFeatures]:
        """Compute and return features for all active symbols."""
        results = []
        for symbol in self._universe.symbols:
            f = await self.publish_features(symbol)
            if f:
                results.append(f)
        return results

    # ── Helpers ───────────────────────────────────────────────────

    async def _bootstrap_calculators(self) -> None:
        """Pre-load candle history on startup."""
        symbols = list(self._universe.symbols)
        logger.info("Bootstrapping calculators", count=len(symbols))

        # Сначала пробуем восстановить из Redis
        restored = 0
        for symbol in symbols:
            calc = self._get_or_create_calculator(symbol)
            if await calc.restore_from_redis(self._redis):
                restored += 1

        logger.info("Restored from Redis", count=restored, total=len(symbols))

        # Для оставшихся — загружаем с REST
        tasks = [self._refresh_candles(sym) for sym in symbols]
        batch_size = 20
        for i in range(0, len(tasks), batch_size):
            await asyncio.gather(*tasks[i: i + batch_size], return_exceptions=True)
            await asyncio.sleep(1.0)

    def _get_or_create_calculator(self, symbol: str) -> FeatureCalculator:
        if symbol not in self._calculators:
            self._calculators[symbol] = FeatureCalculator(symbol)
        return self._calculators[symbol]
