"""
Bybit WebSocket client.
Real-time streams: trades, tickers, orderbook.

DATA SOURCES (WebSocket):
- publicTrade.{symbol}    — real-time trade ticks (price, qty, side, ts)
- tickers.{symbol}        — best bid/ask, last price, 24h stats (push on change)
- orderbook.{depth}.{sym} — orderbook updates (snapshot + delta)

NOTE: Bybit WS v5 public endpoint, no auth required.
      Spot: wss://stream.bybit.com/v5/public/spot
      Linear: wss://stream.bybit.com/v5/public/linear
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.utils.logging import get_logger
from app.utils.time_utils import utcnow_ts

logger = get_logger(__name__)
settings = get_settings()

WS_SPOT_URL = "wss://stream.bybit.com/v5/public/spot"
WS_LINEAR_URL = "wss://stream.bybit.com/v5/public/linear"
WS_TESTNET_SPOT_URL = "wss://stream-testnet.bybit.com/v5/public/spot"
WS_TESTNET_LINEAR_URL = "wss://stream-testnet.bybit.com/v5/public/linear"

# Bybit max subscriptions per WS connection
MAX_SUBS_PER_CONNECTION = 100

TradeCallback = Callable[[str, dict], Coroutine[Any, Any, None]]
TickerCallback = Callable[[str, dict], Coroutine[Any, Any, None]]
OrderbookCallback = Callable[[str, dict], Coroutine[Any, Any, None]]


class BybitWSClient:
    """
    Manages WebSocket connections to Bybit public streams.
    Automatically reconnects on disconnect.
    Supports dynamic subscribe/unsubscribe.
    """

    def __init__(
        self,
        category: str = "spot",
        on_trade: TradeCallback | None = None,
        on_ticker: TickerCallback | None = None,
        on_orderbook: OrderbookCallback | None = None,
    ) -> None:
        self.category = category
        self._on_trade = on_trade
        self._on_ticker = on_ticker
        self._on_orderbook = on_orderbook
        self._subscriptions: set[str] = set()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._reconnect_delay = 1.0

        if settings.bybit_testnet:
            self._url = WS_TESTNET_SPOT_URL if category == "spot" else WS_TESTNET_LINEAR_URL
        else:
            self._url = WS_SPOT_URL if category == "spot" else WS_LINEAR_URL

    async def start(self) -> None:
        self._running = True
        self._session = aiohttp.ClientSession()
        asyncio.create_task(self._connection_loop())
        logger.info("WebSocket client starting", category=self.category, url=self._url)

    async def stop(self) -> None:
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("WebSocket client stopped")

    async def subscribe_trades(self, symbols: list[str]) -> None:
        topics = [f"publicTrade.{sym}" for sym in symbols]
        await self._subscribe(topics)

    async def subscribe_tickers(self, symbols: list[str]) -> None:
        topics = [f"tickers.{sym}" for sym in symbols]
        await self._subscribe(topics)

    async def subscribe_orderbook(self, symbols: list[str], depth: int = 25) -> None:
        topics = [f"orderbook.{depth}.{sym}" for sym in symbols]
        await self._subscribe(topics)

    async def unsubscribe(self, symbols: list[str]) -> None:
        topics = []
        for sym in symbols:
            for prefix in ["publicTrade.", "tickers.", "orderbook.25."]:
                topics.append(f"{prefix}{sym}")
        if self._ws and not self._ws.closed:
            payload = {"op": "unsubscribe", "args": topics}
            await self._ws.send_str(json.dumps(payload))
        for t in topics:
            self._subscriptions.discard(t)

    async def _subscribe(self, topics: list[str]) -> None:
        for t in topics:
            self._subscriptions.add(t)
        if self._ws and not self._ws.closed:
            payload = {"op": "subscribe", "args": topics}
            await self._ws.send_str(json.dumps(payload))
            logger.debug("WS subscribed", topics=topics)

    async def _connection_loop(self) -> None:
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.warning(
                    "WS connection failed, reconnecting",
                    error=str(e),
                    delay=self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _connect(self) -> None:
        async with self._session.ws_connect(self._url, heartbeat=20) as ws:
            self._ws = ws
            self._reconnect_delay = 1.0
            logger.info("WebSocket connected", url=self._url)

            # Resubscribe after reconnect
            if self._subscriptions:
                payload = {"op": "subscribe", "args": list(self._subscriptions)}
                await ws.send_str(json.dumps(payload))

            async for msg in ws:
                if not self._running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WS error", error=str(ws.exception()))
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                    logger.warning("WS closed")
                    break

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("WS: failed to decode message", raw=raw[:100])
            return

        topic = data.get("topic", "")
        msg_data = data.get("data")
        if not topic or msg_data is None:
            return

        if topic.startswith("publicTrade.") and self._on_trade:
            symbol = topic.split(".", 1)[1]
            for tick in (msg_data if isinstance(msg_data, list) else [msg_data]):
                await self._on_trade(symbol, tick)

        elif topic.startswith("tickers.") and self._on_ticker:
            symbol = topic.split(".", 1)[1]
            await self._on_ticker(symbol, msg_data)

        elif topic.startswith("orderbook.") and self._on_orderbook:
            parts = topic.split(".")
            symbol = parts[2] if len(parts) >= 3 else ""
            await self._on_orderbook(symbol, {"type": data.get("type", "delta"), "data": msg_data})
