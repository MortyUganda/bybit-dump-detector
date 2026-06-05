#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TP/SL BACKTEST (минутные свечи) — ЧЕСТНАЯ симуляция шорта тик-за-тиком.

В отличие от tp_sl_optimizer (работал на экстремумах без порядка), здесь мы
идём по 1-минутным свечам ПОСЛЕДОВАТЕЛЬНО и на каждой проверяем что сработало
ПЕРВЫМ — TP или SL — по реальным high/low. Это убирает смещение узких стопов.

Правило внутри свечи (консервативно для шорта): если свеча задела и SL (high
вверх) и TP (low вниз) в одну минуту — считаем, что ПЕРВЫМ сработал SL
(пессимистично, против нас). Так мы не переоцениваем стратегию.

Перебирает TP/SL и опциональный отложенный вход. Если дать --proba (csv с
колонками signal_id, proba от ml_forward), фильтрует сделки по порогу proba и
показывает, как ML-фильтр + асимметрия TP/SL работают вместе.

Запуск:
    python scripts/tp_sl_backtest.py --klines minute_klines.parquet --signals canceled_signals_XXXX.csv
    python scripts/tp_sl_backtest.py --klines minute_klines.parquet --signals XXXX.csv --proba proba.csv

Зависимости: pandas numpy  (pyarrow для parquet)
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


def simulate_trade(candles, entry_price, tp, sl, lev, fee, entry_delay_min=0):
    """candles: list of (minute, open, high, low, close) ascending.
    Шорт: TP при падении на tp%, SL при росте на sl%.
    entry_delay_min: войти не на 0-й минуте, а позже (по close той минуты).
    Возвращает pnl% (с плечом, минус комиссия) либо None если не вошли."""
    if not candles:
        return None
    # выбираем цену входа
    ep = entry_price
    start_idx = 0
    if entry_delay_min > 0:
        # найти свечу >= delay; вход по её close, а TP/SL проверяем СО СЛЕДУЮЩЕЙ свечи
        for idx, c in enumerate(candles):
            if c[0] >= entry_delay_min:
                ep = c[4]; start_idx = idx + 1; break
        else:
            return None
        if start_idx >= len(candles):
            return None
    tp_price = ep * (1 - tp / 100.0)   # шорт: цель ниже
    sl_price = ep * (1 + sl / 100.0)   # стоп выше
    for c in candles[start_idx:]:
        _, o, hi, lo, cl = c
        hit_sl = hi >= sl_price
        hit_tp = lo <= tp_price
        if hit_sl and hit_tp:
            return -sl * lev - fee          # пессимизм: SL первым
        if hit_sl:
            return -sl * lev - fee
        if hit_tp:
            return +tp * lev - fee
    # не сработало за окно — закрытие по последней close
    move = (ep - candles[-1][4]) / ep * 100.0
    return move * lev - fee


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--klines", required=True)
    ap.add_argument("--signals", required=True)
    ap.add_argument("--proba", default=None, help="csv: signal_id,proba (опц., ML-фильтр)")
    ap.add_argument("--proba-thr", type=float, default=0.6)
    ap.add_argument("--fee", type=float, default=0.6)
    ap.add_argument("--lev", type=float, default=10.0)
    args = ap.parse_args()

    # устойчивость: если указанный файл не найден, пробуем другое расширение
    kpath = args.klines
    if not os.path.exists(kpath):
        base = kpath.rsplit(".", 1)[0]
        for alt in (base + ".csv", base + ".parquet"):
            if os.path.exists(alt):
                print(f"(файл {kpath} не найден, использую {alt})")
                kpath = alt; break
        else:
            print(f"ОШИБКА: не найден файл свечей ({args.klines}). Сначала запусти fetch_minute_klines.py"); sys.exit(1)
    kl = pd.read_parquet(kpath) if kpath.endswith(".parquet") else pd.read_csv(kpath)
    sig = pd.read_csv(args.signals)
    print(f"свечей: {len(kl)}  сигналов с свечами: {kl['signal_id'].nunique()}")

    # entry price по сигналу
    sid_col = "id" if "id" in sig.columns else None
    price_col = "signal_price" if "signal_price" in sig.columns else "entry_price"
    sig["_sid"] = sig[sid_col].astype(str) if sid_col else None
    entry_map = dict(zip(sig["_sid"], sig[price_col])) if sid_col else {}

    proba_map = {}
    if args.proba and os.path.exists(args.proba):
        pr = pd.read_csv(args.proba)
        proba_map = dict(zip(pr["signal_id"].astype(str), pr["proba"]))
        print(f"proba загружено: {len(proba_map)} (порог {args.proba_thr})")

    # группируем свечи по сигналу
    grouped = {}
    for sid, g in kl.groupby("signal_id"):
        g = g.sort_values("minute")
        grouped[str(sid)] = list(zip(g["minute"], g["open"], g["high"], g["low"], g["close"]))

    def run(tp, sl, delay, use_proba=False):
        pnls = []
        for sid, candles in grouped.items():
            if use_proba and proba_map.get(sid, 1.0) < args.proba_thr:
                continue
            ep = entry_map.get(sid)
            if ep is None or not (ep > 0):
                ep = candles[0][1] if candles else None  # fallback: open первой свечи
            if ep is None: continue
            r = simulate_trade(candles, ep, tp, sl, args.lev, args.fee, delay)
            if r is not None:
                pnls.append(r)
        return np.array(pnls)

    print("\n" + "=" * 78)
    print("ЧЕСТНЫЙ ПЕРЕБОР TP/SL (минутные свечи, тик-за-тиком, SL первым при коллизии)")
    print("=" * 78)
    rows = []
    for tp in [0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]:
        for sl in [0.5, 0.8, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]:
            p = run(tp, sl, 0)
            if len(p) < 50: continue
            rows.append((tp, sl, len(p), (p > 0).mean(), p.mean(), p.sum(), tstat(p)))
    rows.sort(key=lambda r: -r[4])
    print(f"  {'TP%':>5}{'SL%':>6}{'R:R':>6}{'сделок':>8}{'WR':>7}{'pnl/сд':>9}{'сумма%':>10}{'t':>7}")
    for tp, sl, n, wr, avg, tot, t in rows[:15]:
        flag = "  ✅" if (avg > 0 and t > 2) else ("  ~" if avg > 0 else "")
        print(f"  {tp:>5.1f}{sl:>6.1f}{tp/sl:>6.2f}{n:>8}{wr:>6.1%}{avg:>+8.2f}%{tot:>+9.0f}%{t:>7.2f}{flag}")

    if proba_map:
        print("\n" + "=" * 78)
        print(f"ML-ФИЛЬТР + TP/SL (только сделки с proba>={args.proba_thr})")
        print("=" * 78)
        rows2 = []
        for tp in [0.8, 1.0, 1.5, 2.0, 2.5, 3.0]:
            for sl in [0.8, 1.0, 1.5, 2.0, 3.0]:
                p = run(tp, sl, 0, use_proba=True)
                if len(p) < 30: continue
                rows2.append((tp, sl, len(p), (p > 0).mean(), p.mean(), p.sum(), tstat(p)))
        rows2.sort(key=lambda r: -r[4])
        print(f"  {'TP%':>5}{'SL%':>6}{'сделок':>8}{'WR':>7}{'pnl/сд':>9}{'сумма%':>10}{'t':>7}")
        for tp, sl, n, wr, avg, tot, t in rows2[:10]:
            flag = "  ✅" if (avg > 0 and t > 2) else ("  ~" if avg > 0 else "")
            print(f"  {tp:>5.1f}{sl:>6.1f}{n:>8}{wr:>6.1%}{avg:>+8.2f}%{tot:>+9.0f}%{t:>7.2f}{flag}")

    print("\n" + "=" * 78)
    print("ВЫВОД")
    print("=" * 78)
    if rows and rows[0][4] > 0 and rows[0][6] > 2:
        b = rows[0]
        print(f"  ✅ Прибыльно: TP={b[0]}% SL={b[1]}% → {b[4]:+.2f}%/сделку (t={b[6]:.2f}, n={b[2]}).")
        print("     Это ЧЕСТНЫЙ результат на минутных свечах. Можно внедрять в бот.")
        if proba_map:
            print("     Сравни с блоком ML-фильтра выше — если там pnl выше, комбинируй.")
    elif rows and rows[0][4] > 0:
        b = rows[0]
        print(f"  🟡 Лучшая TP={b[0]}% SL={b[1]}%: {b[4]:+.2f}%/сделку, но t={b[6]:.2f}<2.")
        print("     Слабо значимо. Комбинируй с ML-фильтром или меняй триггер.")
    else:
        print("  ❌ Даже на честных минутных данных прибыльной TP/SL нет.")
        print("     Окончательно: проблема в ТРИГГЕРЕ входа, не в выходе.")


if __name__ == "__main__":
    main()
