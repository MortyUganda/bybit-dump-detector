"""
Тесты BybitTradeClient с ЗАМОКАННЫМ pybit HTTP — НИ ОДНОГО реального вызова API.

Проверяем:
- парсинг instrument-info → InstrumentFilter;
- парсинг баланса;
- что вход идёт Sell/Market reduceOnly=False;
- что ВСЕ закрывающие ордера идут reduceOnly=True (критично для реальных денег);
- проброс retCode != 0 как ошибки;
- проглатывание 110043 (leverage not modified).
"""
from __future__ import annotations

import pytest

from app.bybit.trade_client import BybitTradeClient, BybitTradeError


class FakeHTTP:
    """Подмена pybit.unified_trading.HTTP — записывает вызовы, отдаёт заготовки."""

    def __init__(self, **kwargs):
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, dict] = {}

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return self.responses.get(name, {"retCode": 0, "result": {}})

    def get_instruments_info(self, **kw):
        return self._record("get_instruments_info", **kw)

    def get_wallet_balance(self, **kw):
        return self._record("get_wallet_balance", **kw)

    def get_positions(self, **kw):
        return self._record("get_positions", **kw)

    def set_leverage(self, **kw):
        return self._record("set_leverage", **kw)

    def place_order(self, **kw):
        return self._record("place_order", **kw)

    def cancel_order(self, **kw):
        return self._record("cancel_order", **kw)

    def get_closed_pnl(self, **kw):
        return self._record("get_closed_pnl", **kw)


async def _client_with_fake() -> tuple[BybitTradeClient, FakeHTTP]:
    client = BybitTradeClient("k", "s", testnet=True)
    fake = FakeHTTP()
    client._http = fake  # inject — обходим pybit и .start()
    return client, fake


async def test_get_instrument_filter_parses():
    client, fake = await _client_with_fake()
    fake.responses["get_instruments_info"] = {
        "retCode": 0,
        "result": {"list": [{
            "symbol": "BTCUSDT",
            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"},
            "priceFilter": {"tickSize": "0.5"},
        }]},
    }
    flt = await client.get_instrument_filter("BTCUSDT")
    assert flt is not None
    assert flt.qty_step == 0.001
    assert flt.tick_size == 0.5


async def test_get_available_usdt_parses():
    client, fake = await _client_with_fake()
    fake.responses["get_wallet_balance"] = {
        "retCode": 0,
        "result": {"list": [{"coin": [
            {"coin": "USDT", "availableToWithdraw": "123.45", "walletBalance": "200"},
        ]}]},
    }
    bal = await client.get_available_usdt()
    assert bal == 123.45


async def test_open_short_market_is_sell_not_reduce_only():
    client, fake = await _client_with_fake()
    await client.open_short_market("BTCUSDT", 1.5, "rs_1_entry")
    name, kw = fake.calls[-1]
    assert name == "place_order"
    assert kw["side"] == "Sell"
    assert kw["orderType"] == "Market"
    assert kw["reduceOnly"] is False
    assert kw["orderLinkId"] == "rs_1_entry"


async def test_sl_order_is_reduce_only_limit_buy():
    client, fake = await _client_with_fake()
    await client.place_reduce_only_sl("BTCUSDT", 1.5, 101.0, "rs_1_sl")
    name, kw = fake.calls[-1]
    assert kw["side"] == "Buy"
    assert kw["orderType"] == "Limit"
    assert kw["reduceOnly"] is True
    assert kw["timeInForce"] == "GTC"


async def test_close_short_market_is_reduce_only():
    client, fake = await _client_with_fake()
    await client.close_short_market("BTCUSDT", 1.5, "rs_1_close")
    name, kw = fake.calls[-1]
    assert kw["side"] == "Buy"
    assert kw["orderType"] == "Market"
    assert kw["reduceOnly"] is True


async def test_retcode_nonzero_raises():
    client, fake = await _client_with_fake()
    fake.responses["place_order"] = {"retCode": 10001, "retMsg": "bad"}
    with pytest.raises(BybitTradeError):
        await client.open_short_market("BTCUSDT", 1.0)


async def test_set_leverage_swallows_110043():
    client, fake = await _client_with_fake()
    fake.responses["set_leverage"] = {"retCode": 110043, "retMsg": "leverage not modified"}
    # Не должно бросать
    await client.set_leverage("BTCUSDT", 10)


async def test_not_started_raises():
    client = BybitTradeClient("k", "s", testnet=True)
    with pytest.raises(BybitTradeError):
        await client.open_short_market("BTCUSDT", 1.0)
