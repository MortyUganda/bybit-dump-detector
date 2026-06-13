"""
Bybit Trading client (боевой ордер-флоу) на основе pybit V5 HTTP.

Отделён от market-data BybitRestClient (app/bybit/rest_client.py), который
read-only и не трогается. Здесь — приватные эндпоинты, требующие API-ключей:
- баланс (get_wallet_balance),
- позиции (get_positions),
- установка плеча (set_leverage),
- рыночный вход в шорт (place_order Sell),
- reduce-only закрытие (place_order reduceOnly),
- лимитный reduce-only SL (place_order Limit reduceOnly),
- отмена ордера (cancel_order),
- instrument info (get_instruments_info → фильтры qtyStep/tickSize).

pybit — СИНХРОННЫЙ клиент, поэтому каждый вызов оборачивается в asyncio.to_thread,
чтобы не блокировать event loop. pybit импортируется ЛЕНИВО (в start()), чтобы
модуль можно было импортировать и тестировать без установленного pybit.

КРИТИЧНО ДЛЯ РЕАЛЬНЫХ ДЕНЕГ: все закрывающие ордера идут с reduce_only=True.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.services.real_short_logic import InstrumentFilter
from app.utils.logging import get_logger

logger = get_logger(__name__)

CATEGORY = "linear"


class BybitTradeError(Exception):
    """Ошибка торгового API Bybit (ненулевой retCode или сетевой сбой)."""


class BybitTradeClient:
    """
    Async-обёртка над синхронным pybit.unified_trading.HTTP.

    Использование:
        client = BybitTradeClient(api_key, api_secret, testnet=True)
        await client.start()
        bal = await client.get_available_usdt()
    """

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._testnet = testnet
        self._http: Any = None

    @property
    def testnet(self) -> bool:
        return self._testnet

    async def start(self) -> None:
        """Инициализировать pybit HTTP-сессию (ленивый импорт pybit)."""
        if self._http is not None:
            return
        try:
            from pybit.unified_trading import HTTP  # type: ignore
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise BybitTradeError(
                "pybit не установлен — реальная торговля недоступна"
            ) from exc

        self._http = HTTP(
            testnet=self._testnet,
            api_key=self._api_key,
            api_secret=self._api_secret,
        )
        logger.info("Bybit trade client started", testnet=self._testnet)

    async def stop(self) -> None:
        self._http = None

    async def _call(self, method_name: str, **kwargs: Any) -> dict:
        """Вызвать метод pybit в thread и проверить retCode."""
        if self._http is None:
            raise BybitTradeError("Trade client not started — call .start() first")
        method = getattr(self._http, method_name)
        try:
            resp = await asyncio.to_thread(lambda: method(**kwargs))
        except Exception as exc:
            raise BybitTradeError(f"{method_name} failed: {exc}") from exc
        if not isinstance(resp, dict):
            raise BybitTradeError(f"{method_name}: unexpected response type")
        if resp.get("retCode") != 0:
            raise BybitTradeError(
                f"{method_name}: retCode={resp.get('retCode')} retMsg={resp.get('retMsg')}"
            )
        return resp

    # ── Instrument info ────────────────────────────────────────────

    async def get_instrument_filter(self, symbol: str) -> InstrumentFilter | None:
        """Получить фильтры qtyStep/tickSize/minOrderQty для символа."""
        try:
            resp = await self._call(
                "get_instruments_info", category=CATEGORY, symbol=symbol
            )
            items = resp.get("result", {}).get("list", [])
            if not items:
                return None
            return InstrumentFilter.from_bybit(items[0])
        except BybitTradeError as exc:
            logger.warning("Instrument info failed", symbol=symbol, error=str(exc))
            return None

    # ── Balance ────────────────────────────────────────────────────

    async def get_available_usdt(self) -> float | None:
        """Доступный баланс USDT (UNIFIED аккаунт). None при ошибке."""
        try:
            resp = await self._call(
                "get_wallet_balance", accountType="UNIFIED", coin="USDT"
            )
            lst = resp.get("result", {}).get("list", [])
            if not lst:
                return None
            coins = lst[0].get("coin", [])
            for c in coins:
                if c.get("coin") == "USDT":
                    for key in ("availableToWithdraw", "availableBalance", "walletBalance"):
                        val = c.get(key)
                        if val not in (None, ""):
                            return float(val)
            return None
        except BybitTradeError as exc:
            logger.warning("Wallet balance failed", error=str(exc))
            return None

    # ── Positions ──────────────────────────────────────────────────

    async def get_position(self, symbol: str) -> dict | None:
        """Текущая позиция по символу (или None)."""
        try:
            resp = await self._call("get_positions", category=CATEGORY, symbol=symbol)
            items = resp.get("result", {}).get("list", [])
            for it in items:
                if float(it.get("size", 0) or 0) != 0:
                    return it
            return None
        except BybitTradeError as exc:
            logger.warning("Get position failed", symbol=symbol, error=str(exc))
            return None

    # ── Leverage ───────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """
        Установить плечо. Bybit возвращает retCode 110043 если плечо не менялось —
        это не ошибка, проглатываем.
        """
        try:
            await self._call(
                "set_leverage",
                category=CATEGORY,
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except BybitTradeError as exc:
            if "110043" in str(exc) or "leverage not modified" in str(exc).lower():
                return
            logger.warning("Set leverage failed", symbol=symbol, error=str(exc))

    # ── Orders ─────────────────────────────────────────────────────

    async def open_short_market(
        self,
        symbol: str,
        qty: float,
        order_link_id: str | None = None,
    ) -> dict:
        """
        Открыть шорт рыночным ордером (Sell, side=Sell).
        order_link_id — клиентский ID для идемпотентности на стороне биржи.
        """
        params: dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": _fmt_qty(qty),
            "reduceOnly": False,
        }
        if order_link_id:
            params["orderLinkId"] = order_link_id
        resp = await self._call("place_order", **params)
        return resp.get("result", {})

    async def place_reduce_only_sl(
        self,
        symbol: str,
        qty: float,
        sl_price: float,
        order_link_id: str | None = None,
    ) -> dict:
        """
        Лимитный reduce-only ордер на уровне SL.
        Для закрытия шорта сторона = Buy (обратная входу). reduceOnly=True —
        нельзя перевернуть позицию.
        """
        params: dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Limit",
            "qty": _fmt_qty(qty),
            "price": _fmt_price(sl_price),
            "reduceOnly": True,
            "timeInForce": "GTC",
        }
        if order_link_id:
            params["orderLinkId"] = order_link_id
        resp = await self._call("place_order", **params)
        return resp.get("result", {})

    async def close_short_market(
        self,
        symbol: str,
        qty: float,
        order_link_id: str | None = None,
    ) -> dict:
        """Закрыть шорт рыночным reduce-only ордером (Buy)."""
        params: dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market",
            "qty": _fmt_qty(qty),
            "reduceOnly": True,
        }
        if order_link_id:
            params["orderLinkId"] = order_link_id
        resp = await self._call("place_order", **params)
        return resp.get("result", {})

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        """Отменить ордер (напр. SL при закрытии по TP)."""
        try:
            await self._call(
                "cancel_order", category=CATEGORY, symbol=symbol, orderId=order_id
            )
        except BybitTradeError as exc:
            # Уже исполнен/отменён — не критично
            logger.debug("Cancel order non-fatal", symbol=symbol, error=str(exc))

    async def get_closed_pnl(self, symbol: str, limit: int = 5) -> list[dict]:
        """История закрытого P&L для сверки реального результата."""
        try:
            resp = await self._call(
                "get_closed_pnl", category=CATEGORY, symbol=symbol, limit=limit
            )
            return resp.get("result", {}).get("list", [])
        except BybitTradeError as exc:
            logger.debug("Closed pnl failed", symbol=symbol, error=str(exc))
            return []


def _fmt_qty(qty: float) -> str:
    """Форматирование qty без экспоненты и хвостовых нулей."""
    s = f"{qty:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _fmt_price(price: float) -> str:
    s = f"{price:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"
