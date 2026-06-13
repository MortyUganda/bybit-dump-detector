"""
Юнит-тесты чистой логики Real-shorts (app/services/real_short_logic.py).

Покрывают то, что при ошибке стоит реальных денег:
- сайзинг (margin × leverage → qty);
- округление qty/price под фильтры инструмента;
- цены TP/SL для шорта;
- whitelist/blacklist;
- kill-switch.

Без БД / Redis / сети — поэтому модуль импортируется напрямую.
"""
from __future__ import annotations

import pytest

from app.services.real_short_logic import (
    InstrumentFilter,
    compute_position_size,
    evaluate_kill_switch,
    pnl_usdt_for_short,
    quantize_price,
    quantize_qty,
    round_step_down,
    short_sl_price,
    short_tp_price,
    symbol_allowed,
)


# ── InstrumentFilter.from_bybit ───────────────────────────────────────

def test_instrument_filter_from_bybit():
    info = {
        "symbol": "BTCUSDT",
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "100"},
        "priceFilter": {"tickSize": "0.5"},
    }
    flt = InstrumentFilter.from_bybit(info)
    assert flt.symbol == "BTCUSDT"
    assert flt.qty_step == 0.001
    assert flt.min_order_qty == 0.001
    assert flt.tick_size == 0.5
    assert flt.max_order_qty == 100.0


def test_instrument_filter_missing_fields():
    flt = InstrumentFilter.from_bybit({"symbol": "X"})
    assert flt.qty_step == 0.0
    assert flt.tick_size == 0.0
    assert flt.max_order_qty is None


# ── Округление ────────────────────────────────────────────────────────

def test_round_step_down():
    assert round_step_down(1.2345, 0.001) == 1.234
    assert round_step_down(10.0, 0.0) == 10.0  # step=0 → без изменений
    assert round_step_down(0.0009, 0.001) == 0.0


def test_quantize_qty_rounds_down():
    flt = InstrumentFilter("X", qty_step=0.01, min_order_qty=0.01, tick_size=0.1)
    # 5.678 → вниз до 5.67 (не 5.68, чтобы не превысить маржу)
    assert quantize_qty(5.678, flt) == 5.67


def test_quantize_qty_below_min_returns_zero():
    flt = InstrumentFilter("X", qty_step=0.01, min_order_qty=1.0, tick_size=0.1)
    # 0.5 < minOrderQty=1.0 → 0.0 (вход невозможен)
    assert quantize_qty(0.5, flt) == 0.0


def test_quantize_qty_caps_at_max():
    flt = InstrumentFilter("X", qty_step=0.1, min_order_qty=0.1, tick_size=0.1, max_order_qty=10.0)
    assert quantize_qty(25.0, flt) == 10.0


def test_quantize_price_to_ticksize():
    flt = InstrumentFilter("X", qty_step=0.001, min_order_qty=0.001, tick_size=0.5)
    assert quantize_price(100.3, flt) == 100.5
    assert quantize_price(100.2, flt) == 100.0


# ── Сайзинг ───────────────────────────────────────────────────────────

def test_compute_position_size_basic():
    # margin=20, lev=10 → notional=200; entry=100 → raw qty=2.0
    flt = InstrumentFilter("X", qty_step=0.001, min_order_qty=0.001, tick_size=0.01)
    res = compute_position_size(20.0, 10, 100.0, flt)
    assert res.ok
    assert res.qty == 2.0
    assert res.notional_usdt == pytest.approx(200.0)
    assert res.required_margin_usdt == pytest.approx(20.0)


def test_compute_position_size_rounds_qty_down():
    # notional=200, entry=3 → raw 66.666..., step 0.1 → 66.6
    flt = InstrumentFilter("X", qty_step=0.1, min_order_qty=0.1, tick_size=0.001)
    res = compute_position_size(20.0, 10, 3.0, flt)
    assert res.ok
    assert res.qty == 66.6
    assert res.notional_usdt == pytest.approx(66.6 * 3.0)
    # required margin пересчитан по фактическому notional (а не запрошенной марже)
    assert res.required_margin_usdt == pytest.approx(66.6 * 3.0 / 10)


def test_compute_position_size_insufficient_balance():
    flt = InstrumentFilter("X", qty_step=0.001, min_order_qty=0.001, tick_size=0.01)
    res = compute_position_size(20.0, 10, 100.0, flt, available_balance_usdt=5.0)
    assert not res.ok
    assert res.reason == "insufficient_balance"
    assert res.qty == 0.0


def test_compute_position_size_below_min_qty():
    # entry очень высокая → qty < minOrderQty
    flt = InstrumentFilter("X", qty_step=0.001, min_order_qty=1.0, tick_size=0.01)
    res = compute_position_size(20.0, 10, 1_000_000.0, flt)
    assert not res.ok
    assert res.reason == "below_min_qty"


def test_compute_position_size_bad_price():
    flt = InstrumentFilter("X", qty_step=0.001, min_order_qty=0.001, tick_size=0.01)
    res = compute_position_size(20.0, 10, 0.0, flt)
    assert not res.ok
    assert res.reason == "bad_price"


def test_compute_position_size_bad_params():
    flt = InstrumentFilter("X", qty_step=0.001, min_order_qty=0.001, tick_size=0.01)
    assert compute_position_size(0.0, 10, 100.0, flt).reason == "bad_params"
    assert compute_position_size(20.0, 0, 100.0, flt).reason == "bad_params"


# ── TP/SL для шорта ───────────────────────────────────────────────────

def test_short_tp_price_below_entry():
    flt = InstrumentFilter("X", qty_step=0.001, min_order_qty=0.001, tick_size=0.01)
    # TP для шорта: цена падает на 1%
    assert short_tp_price(100.0, 1.0, flt) == 99.0


def test_short_sl_price_above_entry():
    flt = InstrumentFilter("X", qty_step=0.001, min_order_qty=0.001, tick_size=0.01)
    # SL для шорта: цена растёт на 1%
    assert short_sl_price(100.0, 1.0, flt) == 101.0


def test_tp_sl_quantized_to_ticksize():
    flt = InstrumentFilter("X", qty_step=0.001, min_order_qty=0.001, tick_size=0.5)
    # 99.0 кратно 0.5 уже; проверим нестандартный entry
    tp = short_tp_price(99.3, 1.0, flt)  # 98.307 → tick 0.5 → 98.5
    assert tp % 0.5 == pytest.approx(0.0, abs=1e-9)


def test_pnl_usdt_for_short():
    # шорт прибылен когда exit < entry
    assert pnl_usdt_for_short(100.0, 99.0, 2.0) == pytest.approx(2.0)
    assert pnl_usdt_for_short(100.0, 101.0, 2.0) == pytest.approx(-2.0)


# ── whitelist / blacklist ─────────────────────────────────────────────

def test_symbol_allowed_default_all():
    assert symbol_allowed("BTCUSDT", [], []) is True


def test_symbol_blacklist_priority():
    assert symbol_allowed("BTCUSDT", ["BTCUSDT"], ["BTCUSDT"]) is False


def test_symbol_whitelist_restricts():
    assert symbol_allowed("ETHUSDT", ["BTCUSDT"], []) is False
    assert symbol_allowed("BTCUSDT", ["BTCUSDT"], []) is True


def test_symbol_allowed_case_insensitive():
    assert symbol_allowed("btcusdt", [], ["BTCUSDT"]) is False


# ── kill-switch ───────────────────────────────────────────────────────

def test_kill_switch_daily_loss_trips():
    d = evaluate_kill_switch(
        realized_pnl_today_usdt=-50.0,
        max_daily_loss_usdt=50.0,
        open_positions=0,
        max_open_positions=3,
    )
    assert d.tripped
    assert d.reason == "daily_loss_limit"


def test_kill_switch_daily_loss_not_tripped_when_profit():
    d = evaluate_kill_switch(10.0, 50.0, 0, 3)
    assert not d.tripped


def test_kill_switch_max_open_positions():
    d = evaluate_kill_switch(0.0, 50.0, 3, 3)
    assert d.tripped
    assert d.reason == "max_open_positions"


def test_kill_switch_disabled_limits():
    # max_daily_loss=0 и max_open=0 → проверки выключены
    d = evaluate_kill_switch(-1000.0, 0.0, 100, 0)
    assert not d.tripped


def test_kill_switch_open_below_limit():
    d = evaluate_kill_switch(0.0, 50.0, 2, 3)
    assert not d.tripped
