"""
Чистая (pure) бизнес-логика Real-shorts — без БД / Redis / сети.

Здесь сосредоточены ВСЕ численные расчёты, которые при ошибке стоят реальных
денег, поэтому они вынесены в отдельный модуль без тяжёлых зависимостей и
покрыты юнит-тестами:

- сайзинг позиции (margin × leverage → qty);
- округление qty/price под фильтры инструмента Bybit (qtyStep, tickSize, minOrderQty);
- расчёт цен TP/SL для шорта (tp_pct/sl_pct — это движение цены, P&L = ×leverage);
- оценка kill-switch (дневной лимит убытка + лимит открытых позиций);
- решение «можно ли открывать» (whitelist/blacklist, баланс, лимиты).

ВАЖНО про проценты: как и в ml_short, tp_pct/sl_pct задают ДВИЖЕНИЕ ЦЕНЫ в %.
Для шорта TP срабатывает когда цена ПАДАЕТ на tp_pct, SL — когда РАСТЁТ на sl_pct.
P&L по марже = движение_цены_% × leverage.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentFilter:
    """Фильтры инструмента Bybit (linear perpetual)."""

    symbol: str
    qty_step: float            # шаг количества (lotSizeFilter.qtyStep)
    min_order_qty: float       # минимальное количество (lotSizeFilter.minOrderQty)
    tick_size: float           # шаг цены (priceFilter.tickSize)
    max_order_qty: float | None = None  # lotSizeFilter.maxOrderQty (опц.)

    @staticmethod
    def from_bybit(info: dict) -> "InstrumentFilter":
        """Построить из ответа /v5/market/instruments-info (category=linear)."""
        lot = info.get("lotSizeFilter", {}) or {}
        price = info.get("priceFilter", {}) or {}
        max_qty = lot.get("maxOrderQty")
        return InstrumentFilter(
            symbol=info.get("symbol", ""),
            qty_step=float(lot.get("qtyStep", "0") or 0),
            min_order_qty=float(lot.get("minOrderQty", "0") or 0),
            tick_size=float(price.get("tickSize", "0") or 0),
            max_order_qty=float(max_qty) if max_qty not in (None, "", "0") else None,
        )


def _decimals(step: float) -> int:
    """Сколько знаков после запятой нужно, чтобы аккуратно отформатировать шаг."""
    if step <= 0:
        return 8
    s = f"{step:.12f}".rstrip("0")
    if "." in s:
        return len(s.split(".", 1)[1])
    return 0


def round_step_down(value: float, step: float) -> float:
    """Округлить ВНИЗ до ближайшего кратного step (для qty — чтобы не превысить маржу)."""
    if step <= 0:
        return value
    # +1e-9 страхует от потери последнего шага из-за float-погрешности
    n = math.floor(value / step + 1e-9)
    return round(n * step, _decimals(step))


def round_step_nearest(value: float, step: float) -> float:
    """Округлить до ближайшего кратного step (для цен TP/SL под tickSize)."""
    if step <= 0:
        return value
    n = math.floor(value / step + 0.5)
    return round(n * step, _decimals(step))


def quantize_qty(raw_qty: float, flt: InstrumentFilter) -> float:
    """
    Привести сырое количество к фильтрам инструмента.
    Возвращает 0.0 если после округления qty < minOrderQty (вход невозможен).
    """
    if raw_qty <= 0:
        return 0.0
    qty = round_step_down(raw_qty, flt.qty_step) if flt.qty_step > 0 else raw_qty
    if flt.max_order_qty and qty > flt.max_order_qty:
        qty = round_step_down(flt.max_order_qty, flt.qty_step) if flt.qty_step > 0 else flt.max_order_qty
    if flt.min_order_qty and qty < flt.min_order_qty:
        return 0.0
    return qty


def quantize_price(raw_price: float, flt: InstrumentFilter) -> float:
    """Привести цену к tickSize инструмента."""
    if raw_price <= 0:
        return raw_price
    if flt.tick_size > 0:
        return round_step_nearest(raw_price, flt.tick_size)
    return raw_price


@dataclass(frozen=True)
class SizingResult:
    qty: float
    notional_usdt: float       # qty × entry_price (объём позиции)
    required_margin_usdt: float  # notional / leverage
    ok: bool
    reason: str | None = None


def compute_position_size(
    margin_usdt: float,
    leverage: float,
    entry_price: float,
    flt: InstrumentFilter,
    available_balance_usdt: float | None = None,
) -> SizingResult:
    """
    Рассчитать размер позиции.

    Объём входа (notional) = margin_usdt × leverage.
    qty = notional / entry_price, округлённое вниз под qtyStep.
    Если available_balance задан и < margin_usdt — отказ (skip, не падать).
    """
    if entry_price <= 0:
        return SizingResult(0.0, 0.0, 0.0, False, "bad_price")
    if margin_usdt <= 0 or leverage <= 0:
        return SizingResult(0.0, 0.0, 0.0, False, "bad_params")

    if available_balance_usdt is not None and available_balance_usdt < margin_usdt:
        return SizingResult(0.0, 0.0, margin_usdt, False, "insufficient_balance")

    notional = margin_usdt * leverage
    raw_qty = notional / entry_price
    qty = quantize_qty(raw_qty, flt)
    if qty <= 0:
        return SizingResult(0.0, 0.0, margin_usdt, False, "below_min_qty")

    real_notional = qty * entry_price
    return SizingResult(
        qty=qty,
        notional_usdt=real_notional,
        required_margin_usdt=real_notional / leverage,
        ok=True,
    )


def short_tp_price(entry_price: float, tp_pct: float, flt: InstrumentFilter) -> float:
    """Цена тейк-профита для шорта: цена ПАДАЕТ на tp_pct% → закрытие в плюс."""
    raw = entry_price * (1 - tp_pct / 100.0)
    return quantize_price(raw, flt)


def short_sl_price(entry_price: float, sl_pct: float, flt: InstrumentFilter) -> float:
    """Цена стоп-лосса для шорта: цена РАСТЁТ на sl_pct% → закрытие в минус."""
    raw = entry_price * (1 + sl_pct / 100.0)
    return quantize_price(raw, flt)


def pnl_usdt_for_short(
    entry_price: float,
    exit_price: float,
    qty: float,
) -> float:
    """
    Грубая оценка реализованного P&L шорта в USDT (без учёта комиссий).
    Для шорта прибыль = (entry - exit) × qty.
    Используется только как fallback, если Bybit не отдал closedPnl.
    """
    return (entry_price - exit_price) * qty


def symbol_allowed(
    symbol: str,
    allow_symbols: list[str] | None,
    block_symbols: list[str] | None,
) -> bool:
    """
    Проверка whitelist/blacklist.
    - blacklist имеет приоритет: символ из блока запрещён всегда.
    - если whitelist непустой — разрешены ТОЛЬКО символы из него.
    """
    sym = symbol.upper()
    block = {s.upper() for s in (block_symbols or [])}
    if sym in block:
        return False
    allow = {s.upper() for s in (allow_symbols or [])}
    if allow and sym not in allow:
        return False
    return True


@dataclass(frozen=True)
class KillSwitchDecision:
    tripped: bool
    reason: str | None = None


def evaluate_kill_switch(
    realized_pnl_today_usdt: float,
    max_daily_loss_usdt: float,
    open_positions: int,
    max_open_positions: int,
) -> KillSwitchDecision:
    """
    Оценить kill-switch ПЕРЕД открытием новой реальной позиции.

    - Дневной стоп-лосс: если суммарный реализованный убыток за день достиг
      порога (realized_pnl_today <= -max_daily_loss), реальная торговля должна
      быть выключена. max_daily_loss_usdt <= 0 → проверка отключена.
    - Лимит открытых позиций: если уже открыто >= max_open_positions, новые не
      открываем. max_open_positions <= 0 → без лимита.

    Возвращает tripped=True, если открывать НЕЛЬЗЯ.
    """
    if max_daily_loss_usdt and max_daily_loss_usdt > 0:
        if realized_pnl_today_usdt <= -abs(max_daily_loss_usdt):
            return KillSwitchDecision(True, "daily_loss_limit")

    if max_open_positions and max_open_positions > 0:
        if open_positions >= max_open_positions:
            return KillSwitchDecision(True, "max_open_positions")

    return KillSwitchDecision(False, None)
