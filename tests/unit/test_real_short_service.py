"""
Тесты RealShortService.on_ml_open — гейты безопасности и маппинг ml_short→real.

Bybit API и БД ЗАМОКАНЫ полностью — НИ ОДНОГО реального вызова.
Проверяем:
- real_enabled=False → ни одного ордера (жёсткий гейт);
- идемпотентность: дубль по ml_signal_id → skip;
- kill-switch (лимит позиций) → skip;
- успешный путь: вход Sell + reduce-only SL, маппинг ml_signal_id/ml_position_id;
- whitelist/blacklist → skip.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.services.real_short_config import DEFAULT_REAL_SHORT_CONFIG
from app.services.real_short_logic import InstrumentFilter
from app.services.real_short_service import RealShortService


class FakeRedis:
    """Минимальный async Redis для конфига + cooldown."""

    def __init__(self, cfg: dict):
        self._store = {"runtime_config:real_short": json.dumps(cfg)}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value


class FakeTradeClient:
    def __init__(self):
        self.orders: list[dict] = []
        self.filter = InstrumentFilter("BTCUSDT", qty_step=0.001, min_order_qty=0.001, tick_size=0.5)
        self.balance = 10_000.0

    async def get_instrument_filter(self, symbol):
        return self.filter

    async def get_available_usdt(self):
        return self.balance

    async def set_leverage(self, symbol, leverage):
        self.orders.append({"type": "set_leverage", "leverage": leverage})

    async def open_short_market(self, symbol, qty, order_link_id=None):
        self.orders.append({"type": "entry", "side": "Sell", "qty": qty, "reduce_only": False})
        return {"orderId": "entry-1"}

    async def place_reduce_only_sl(self, symbol, qty, sl_price, order_link_id=None):
        self.orders.append({"type": "sl", "side": "Buy", "reduce_only": True, "price": sl_price})
        return {"orderId": "sl-1"}

    async def close_short_market(self, symbol, qty, order_link_id=None):
        self.orders.append({"type": "close", "side": "Buy", "reduce_only": True})
        return {"orderId": "close-1"}

    async def cancel_order(self, symbol, order_id):
        self.orders.append({"type": "cancel", "order_id": order_id})

    async def get_closed_pnl(self, symbol, limit=5):
        return [{"closedPnl": "1.5"}]


def _make_service(cfg_overrides: dict | None = None) -> tuple[RealShortService, FakeTradeClient]:
    cfg = dict(DEFAULT_REAL_SHORT_CONFIG)
    cfg.update(cfg_overrides or {})
    svc = RealShortService(redis=FakeRedis(cfg), bot=None)

    fake_client = FakeTradeClient()
    svc._get_trade_client = AsyncMock(return_value=fake_client)

    # Замокать БД-зависимые помощники
    svc._has_real_for_signal = AsyncMock(return_value=False)
    svc._count_open_real = AsyncMock(return_value=0)
    svc._realized_pnl_today = AsyncMock(return_value=0.0)
    svc._insert_position = AsyncMock(return_value=101)
    svc._set_sl_order_id = AsyncMock()
    svc._mark_position_open = AsyncMock()
    svc._mark_position_failed = AsyncMock()
    svc._log_order = AsyncMock()
    svc._notify_opened = AsyncMock()
    svc._notify_kill_switch = AsyncMock()
    return svc, fake_client


async def test_disabled_places_no_orders():
    svc, client = _make_service({"real_enabled": False})
    result = await svc.on_ml_open(1, 10, "BTCUSDT", 100.0)
    assert result is None
    assert client.orders == []
    svc._insert_position.assert_not_called()


async def test_idempotency_duplicate_signal_skips():
    svc, client = _make_service({"real_enabled": True})
    svc._has_real_for_signal = AsyncMock(return_value=True)
    result = await svc.on_ml_open(1, 10, "BTCUSDT", 100.0)
    assert result is None
    assert client.orders == []


async def test_kill_switch_max_open_skips():
    svc, client = _make_service({"real_enabled": True, "real_max_open_positions": 3})
    svc._count_open_real = AsyncMock(return_value=3)
    result = await svc.on_ml_open(1, 10, "BTCUSDT", 100.0)
    assert result is None
    assert client.orders == []


async def test_kill_switch_daily_loss_disables_real():
    svc, client = _make_service({
        "real_enabled": True,
        "real_max_daily_loss_usdt": 50.0,
    })
    svc._realized_pnl_today = AsyncMock(return_value=-60.0)
    result = await svc.on_ml_open(1, 10, "BTCUSDT", 100.0)
    assert result is None
    assert client.orders == []
    svc._notify_kill_switch.assert_awaited()
    # real_enabled авто-выключен
    cfg = json.loads(svc._redis._store["runtime_config:real_short"])
    assert cfg["real_enabled"] is False


async def test_blacklist_skips():
    svc, client = _make_service({"real_enabled": True, "real_block_symbols": ["BTCUSDT"]})
    result = await svc.on_ml_open(1, 10, "BTCUSDT", 100.0)
    assert result is None
    assert client.orders == []


async def test_whitelist_restricts():
    svc, client = _make_service({"real_enabled": True, "real_allow_symbols": ["ETHUSDT"]})
    result = await svc.on_ml_open(1, 10, "BTCUSDT", 100.0)
    assert result is None
    assert client.orders == []


async def test_happy_path_opens_short_with_reduce_only_sl():
    svc, client = _make_service({
        "real_enabled": True,
        "real_margin_usdt": 20.0,
        "real_leverage": 10,
        "real_cooldown_sec": 0,
    })
    result = await svc.on_ml_open(ml_signal_id=1, ml_position_id=10, symbol="BTCUSDT", entry_price=100.0)
    assert result == 101

    types = [o["type"] for o in client.orders]
    assert "entry" in types
    assert "sl" in types

    entry = next(o for o in client.orders if o["type"] == "entry")
    assert entry["side"] == "Sell"
    assert entry["reduce_only"] is False

    sl = next(o for o in client.orders if o["type"] == "sl")
    assert sl["reduce_only"] is True  # критично: SL reduce-only

    # маппинг ml_signal_id/ml_position_id передан в insert
    insert_kwargs = svc._insert_position.call_args.kwargs
    assert insert_kwargs["ml_signal_id"] == 1
    assert insert_kwargs["ml_position_id"] == 10
    assert insert_kwargs["symbol"] == "BTCUSDT"
    # notional = margin × leverage = 200; qty = 200/100 = 2.0
    assert insert_kwargs["qty"] == 2.0

    svc._mark_position_open.assert_awaited()
    svc._notify_opened.assert_awaited()


async def test_insufficient_balance_skips_orders():
    svc, client = _make_service({"real_enabled": True, "real_margin_usdt": 20.0})
    client.balance = 5.0  # < margin
    result = await svc.on_ml_open(1, 10, "BTCUSDT", 100.0)
    assert result is None
    # позиция вставлена не должна быть (сайзинг отклонён ДО insert)
    svc._insert_position.assert_not_called()
    assert client.orders == []


async def test_close_uses_reduce_only_and_cancels_sl():
    svc, client = _make_service({"real_enabled": True})
    svc._mark_position_closed = AsyncMock()
    svc._notify_closed = AsyncMock()
    pos = {
        "id": 101, "symbol": "BTCUSDT", "qty": 2.0, "entry_price": 100.0,
        "leverage": 10, "testnet": True, "tp_price": 99.0, "sl_price": 101.0,
        "sl_order_id": "sl-1",
    }
    svc._get_open_by_ml_position = AsyncMock(return_value=pos)
    await svc.on_ml_close(ml_position_id=10, exit_price=99.0, close_reason="tp")

    types = [o["type"] for o in client.orders]
    assert "cancel" in types  # SL отменён перед закрытием
    close = next(o for o in client.orders if o["type"] == "close")
    assert close["reduce_only"] is True
    svc._mark_position_closed.assert_awaited()
