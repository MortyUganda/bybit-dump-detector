#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ENTRY SIGNAL TEST — ищем ТОЧКУ ВХОДА с честным преимуществом вниз.

Диагноз доказан: вход СРАЗУ по сигналу (на пике перегрева) убыточен при любой
TP/SL. Гипотеза: бот входит слишком рано — на пике пампа, до разворота. Цена
сначала дёргается вверх (выбивает стоп), потом падает. Значит надо входить
ПОЗЖЕ — после подтверждения, что памп выдохся.

Проверяем на УЖЕ СОБРАННЫХ минутных свечах (minute_klines.csv) разные правила
входа, честно тик-за-тиком, и сравниваем с базовым 'вход сразу':

  baseline    : вход на 0-й минуте по close (как сейчас)
  first_red   : вход на close первой КРАСНОЙ свечи (close<open) — памп выдохся
  break_low   : вход когда цена пробила low предыдущей свечи (начало падения)
  pullback    : вход после локального пика — цена сделала макс и пошла вниз
  delay_N     : вход просто через N минут (контроль: помогает ли само ожидание)

Для каждого правила: для каждого сигнала находим точку входа в окне 60м,
дальше симулируем шорт с фикс. TP/SL до конца окна. Если правило не сработало
(нет красной свечи / нет пробоя) — сделки нет (бот бы пропустил).

Сравниваем: сколько сделок, WR, pnl/сделку (10x, -комиссия), t-стат.
ВАЖНО: при коллизии TP и SL в одной свече — SL первым (пессимизм).

Запуск:
    python scripts/entry_signal_test.py --klines minute_klines.csv
    python scripts/entry_signal_test.py --klines minute_klines.csv --tp 1.0 --sl 1.5

Зависимости: pandas numpy
"""
from __future__ import annotations
import argparse, os, sys
import numpy as np, pandas as pd


def wilson(w, n, z=1.96):
    if n == 0: return 0.0, 0.0, 0.0
    p = w / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def tstat(x):
    x = np.asarray(x, float); x = x[~np.isnan(x)]
    if len(x) < 2 or x.std(ddof=1) == 0: return 0.0
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def find_entry(candles, rule, param=0):
    """candles: list (minute, open, high, low, close) ascending.
    Возвращает (entry_idx, entry_price) или (None, None) если правило не сработало.
    entry_idx — индекс свечи, ПОСЛЕ которой начинаем проверять TP/SL.
    """
    if not candles:
        return None, None
    if rule == "baseline":
        return 0, candles[0][4]  # вход по close 0-й свечи, проверяем с idx 0+1
    if rule == "delay":
        for i, c in enumerate(candles):
            if c[0] >= param:
                return i, c[4]
        return None, None
    if rule == "first_red":
        # первая красная свеча (close<open) — вход по её close
        for i, c in enumerate(candles):
            if c[4] < c[1]:
                return i, c[4]
        return None, None
    if rule == "break_low":
        # вход когда low текущей свечи < low предыдущей (нисходящий пробой)
        for i in range(1, len(candles)):
            if candles[i][3] < candles[i - 1][3]:
                return i, candles[i][4]
        return None, None
    if rule == "pullback":
        # ждём локальный пик: цена росла, затем свеча закрылась ниже своего open
        # И ниже максимума предыдущей. Вход по close такой свечи.
        peak = candles[0][2]
        for i in range(1, len(candles)):
            c = candles[i]
            if c[2] >= peak:
                peak = c[2]; continue
            # цена ниже пика и свеча красная
            if c[4] < c[1] and c[2] < peak:
                return i, c[4]
        return None, None
    return None, None


def simulate(candles, entry_idx, ep, tp, sl, lev, fee):
    """Шорт от entry_idx+1 до конца. TP падение tp%, SL рост sl%."""
    tp_price = ep * (1 - tp / 100.0)
    sl_price = ep * (1 + sl / 100.0)
    for c in candles[entry_idx + 1:]:
        _, o, hi, lo, cl = c
        hit_sl = hi >= sl_price
        hit_tp = lo <= tp_price
        if hit_sl and hit_tp:
            return -sl * lev - fee
        if hit_sl:
            return -sl * lev - fee
        if hit_tp:
            return +tp * lev - fee
    move = (ep - candles[-1][4]) / ep * 100.0
    return move * lev - fee


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--klines", default="minute_klines.csv")
    ap.add_argument("--tp", type=float, default=1.0)
    ap.add_argument("--sl", type=float, default=1.5)
    ap.add_argument("--fee", type=float, default=0.6)
    ap.add_argument("--lev", type=float, default=10.0)
    args = ap.parse_args()

    kpath = args.klines
    if not os.path.exists(kpath):
        base = kpath.rsplit(".", 1)[0]
        for alt in (base + ".csv", base + ".parquet"):
            if os.path.exists(alt): kpath = alt; break
        else:
            print(f"не найден {args.klines}, сначала fetch_minute_klines.py"); sys.exit(1)
    kl = pd.read_parquet(kpath) if kpath.endswith(".parquet") else pd.read_csv(kpath)
    print(f"свечей: {len(kl)}, сигналов: {kl['signal_id'].nunique()}")
    print(f"TP={args.tp}% SL={args.sl}% (R:R {args.tp/args.sl:.2f}), плечо {args.lev:.0f}x, комиссия {args.fee}%\n")

    grouped = {}
    for sid, g in kl.groupby("signal_id"):
        g = g.sort_values("minute")
        grouped[str(sid)] = list(zip(g["minute"], g["open"], g["high"], g["low"], g["close"]))

    rules = [("baseline (вход сразу)", "baseline", 0),
             ("delay_5 (через 5 мин)", "delay", 5),
             ("delay_15 (через 15 мин)", "delay", 15),
             ("first_red (1я красная)", "first_red", 0),
             ("break_low (пробой low)", "break_low", 0),
             ("pullback (откат от пика)", "pullback", 0)]

    print("=" * 80)
    print(f"СРАВНЕНИЕ ПРАВИЛ ВХОДА (TP={args.tp}% SL={args.sl}%)")
    print("=" * 80)
    print(f"  {'правило':28s}{'сделок':>8}{'%вошли':>8}{'WR':>8}{'pnl/сд':>9}{'сумма%':>10}{'t':>7}")
    total = len(grouped)
    results = {}
    for label, rule, param in rules:
        pnls = []
        for sid, candles in grouped.items():
            ei, ep = find_entry(candles, rule, param)
            if ei is None or ep is None or ep <= 0:
                continue
            if ei + 1 >= len(candles):
                continue
            r = simulate(candles, ei, ep, args.tp, args.sl, args.lev, args.fee)
            if r is not None:
                pnls.append(r)
        p = np.array(pnls)
        if len(p) == 0:
            print(f"  {label:28s}{'0':>8}"); continue
        wr, lo, hi = wilson(int((p > 0).sum()), len(p))
        results[label] = (len(p), wr, p.mean(), p.sum(), tstat(p))
        flag = "  ✅" if (p.mean() > 0 and tstat(p) > 2) else ("  ~" if p.mean() > 0 else "")
        print(f"  {label:28s}{len(p):>8}{len(p)/total:>7.0%}{wr:>7.1%}{p.mean():>+8.2f}%{p.sum():>+9.0f}%{tstat(p):>7.2f}{flag}")

    print("\n" + "=" * 80)
    print("ВЫВОД")
    print("=" * 80)
    best = max(results.items(), key=lambda kv: kv[1][2]) if results else None
    base = results.get("baseline (вход сразу)")
    if best and best[1][2] > 0 and best[1][4] > 2:
        print(f"  ✅ Найдена точка входа с преимуществом: '{best[0]}' → {best[1][2]:+.2f}%/сделку "
              f"(t={best[1][4]:.2f}, n={best[1][0]}).")
        if base:
            print(f"     Базовый 'вход сразу' давал {base[2]:+.2f}%. Отложенный вход РЕШАЕТ проблему.")
        print("     Можно перестраивать триггер: вход по этому правилу, не по перегреву.")
    elif best and best[1][2] > 0:
        print(f"  🟡 Лучшее '{best[0]}' даёт {best[1][2]:+.2f}%/сделку, но t={best[1][4]:.2f}<2 (слабо).")
        print("     Направление верное, но edge маленький. Нужно комбинировать с отбором монет/режима.")
    else:
        print("  ❌ Ни одна точка входа не даёт честного преимущества вниз.")
        print("     Это значит: на ЭТОМ наборе сигналов краткосрочный шорт не имеет edge.")
        print("     Проблема не в тайминге входа, а в ОТБОРЕ монет — детектор ловит не те.")
        print("     Следующий шаг: менять КРИТЕРИЙ отбора (что вообще считать кандидатом на шорт).")


if __name__ == "__main__":
    main()