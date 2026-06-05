#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REGIME ANALYSIS — поиск прибыльных режимов/сегментов short-бота.

ML-фильтр мёртв (honest AUC 0.494). Этот скрипт ищет прибыль ТАМ, ГДЕ ОНА ЕСТЬ:
по рыночным режимам, символам и времени. Считает не абстрактный WR, а РЕАЛЬНЫЕ
ДЕНЬГИ на сделку (по фактическому pnl_pct, который уже с плечом и проскальзыванием).

Главный вопрос: есть ли подмножество условий с положительным матожиданием?

Запуск:
    python scripts/regime_analysis.py --auto auto_shorts_XXXX.csv
    python scripts/regime_analysis.py --auto ... --fee 0.06   # комиссия в % на круг

Зависимости: pandas, numpy
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd

pd.set_option("display.width", 200)


def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return 0.0, 0.0, 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def seg_stats(df, fee_pct):
    """Статистика по сегменту: n, WR(+CI), средний pnl на сделку (минус комиссия)."""
    n = len(df)
    if n == 0:
        return None
    wins = int((df["_win"] == 1).sum())
    wr, lo, hi = wilson_ci(wins, n)
    # реальный pnl минус комиссия на круг (round-turn)
    pnl_net = df["_pnl"] - fee_pct
    avg = pnl_net.mean()
    # t-стат для среднего pnl (прибыль значимо > 0?)
    se = pnl_net.std(ddof=1) / np.sqrt(n) if n > 1 else np.nan
    t = avg / se if se and se > 0 else 0.0
    total = pnl_net.sum()
    return dict(n=n, wr=wr, wr_lo=lo, wr_hi=hi, avg=avg, t=t, total=total)


def print_buckets(df, col, fee_pct, q=5, label=None):
    """Разбивка по квантильным бакетам одной фичи."""
    label = label or col
    sub = df[df[col].notna()].copy()
    if len(sub) < q * 20:
        print(f"\n[{label}] мало данных ({len(sub)}), пропуск")
        return
    try:
        sub["_bucket"] = pd.qcut(sub[col], q, duplicates="drop")
    except ValueError:
        print(f"\n[{label}] не удалось разбить на бакеты")
        return
    print(f"\n{'='*78}\nРЕЖИМ: {label}\n{'='*78}")
    print(f"{'диапазон':<28} {'n':>6} {'WR':>7} {'95% CI':>16} {'pnl/сделку':>11} {'t':>6}")
    rows = []
    for b, g in sub.groupby("_bucket", observed=True):
        s = seg_stats(g, fee_pct)
        flag = ""
        if s["avg"] > 0 and s["t"] > 2:
            flag = "  ✅ ПРИБЫЛЬ (t>2)"
        elif s["avg"] > 0:
            flag = "  ~ плюс (не значимо)"
        print(f"{str(b):<28} {s['n']:>6} {s['wr']:>6.1%} "
              f"[{s['wr_lo']:>5.1%},{s['wr_hi']:>5.1%}] {s['avg']:>+10.2f}% {s['t']:>6.2f}{flag}")
        rows.append((str(b), s))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", default=None)
    ap.add_argument("--fee", type=float, default=0.06,
                    help="комиссия на круг в %% от позиции (Bybit taker ~0.055%% x2 x плечо). "
                         "0.06 = консервативно. Для 10x плеча реальная комиссия на маржу ~1.1%%.")
    ap.add_argument("--fee-leverage", type=float, default=10.0,
                    help="плечо для пересчёта комиссии на маржу")
    args = ap.parse_args()

    auto = args.auto or (sorted(glob.glob("auto_shorts_*.csv"), key=os.path.getmtime, reverse=True) or [None])[0]
    if not auto:
        print("Не найден auto_shorts CSV. Укажи --auto")
        sys.exit(1)

    # комиссия на маржу = taker_fee% x 2 (вход+выход) x плечо
    fee_on_margin = args.fee * args.fee_leverage
    print(f"auto_shorts: {auto}")
    print(f"комиссия: {args.fee}% на круг × плечо {args.fee_leverage} = {fee_on_margin:.2f}% на маржу/сделку\n")

    A = pd.read_csv(auto)
    A = A[A["close_reason"].notna()].copy()
    A["_win"] = A["close_reason"].astype(str).str.startswith("tp").astype(int)
    A["_pnl"] = A["pnl_pct"].astype(float)  # уже с плечом

    # ── базовая экономика ──
    print("="*78)
    print("БАЗОВАЯ ЭКОНОМИКА (все сделки, без фильтра)")
    print("="*78)
    s = seg_stats(A, fee_on_margin)
    print(f"  n={s['n']}, WR={s['wr']:.1%} [{s['wr_lo']:.1%}, {s['wr_hi']:.1%}]")
    print(f"  средний pnl/сделку (после комиссии): {s['avg']:+.2f}%   t={s['t']:.2f}")
    print(f"  суммарный pnl: {s['total']:+.1f}%")
    if s["avg"] < 0:
        print("  ⚠️  БАЗОВАЯ СТРАТЕГИЯ УБЫТОЧНА. Ищем прибыльные сегменты ниже.")
    print(f"  для безубытка при R:R 1:1 нужен WR ≈ {50 + fee_on_margin/(2*A['_pnl'].abs().mean())*100:.1f}%")

    # ── режимы рынка ──
    for col, lab in [
        ("btc_change_24h", "BTC изменение 24ч (режим тренда)"),
        ("btc_change_1h", "BTC изменение 1ч"),
        ("btc_change_4h", "BTC изменение 4ч"),
        ("realized_vol_1h", "Волатильность 1ч"),
        ("btc_adx_1h", "BTC ADX 1ч (сила тренда)"),
        ("btc_atr_pct_1h", "BTC ATR%% 1ч"),
        ("score", "Score сигнала"),
    ]:
        if col in A.columns:
            print_buckets(A, col, fee_on_margin, q=5, label=lab)

    # ── время суток ──
    if "entry_ts" in A.columns:
        A["_hour"] = pd.to_datetime(A["entry_ts"], errors="coerce", utc=True).dt.hour
        print(f"\n{'='*78}\nРЕЖИМ: Час суток (UTC)\n{'='*78}")
        print(f"{'час':<6} {'n':>6} {'WR':>7} {'pnl/сделку':>11} {'t':>6}")
        for h, g in A.groupby("_hour"):
            if len(g) < 50:
                continue
            st = seg_stats(g, fee_on_margin)
            flag = "  ✅" if st["avg"] > 0 and st["t"] > 1.5 else ""
            print(f"{int(h):<6} {st['n']:>6} {st['wr']:>6.1%} {st['avg']:>+10.2f}% {st['t']:>6.2f}{flag}")

    # ── символы: топ прибыльных / убыточных ──
    print(f"\n{'='*78}\nСИМВОЛЫ (мин. 30 сделок): ТОП-15 прибыльных и ТОП-15 убыточных\n{'='*78}")
    sym_rows = []
    for sym, g in A.groupby("symbol"):
        if len(g) < 30:
            continue
        st = seg_stats(g, fee_on_margin)
        sym_rows.append((sym, st["n"], st["wr"], st["avg"], st["total"], st["t"]))
    sym_df = pd.DataFrame(sym_rows, columns=["symbol", "n", "wr", "avg", "total", "t"])
    if len(sym_df):
        sym_df = sym_df.sort_values("avg", ascending=False)
        print("\n  ТОП прибыльных:")
        print(f"  {'symbol':<14} {'n':>5} {'WR':>6} {'pnl/сделку':>11} {'сумма':>9} {'t':>6}")
        for _, r in sym_df.head(15).iterrows():
            print(f"  {r['symbol']:<14} {int(r['n']):>5} {r['wr']:>5.1%} {r['avg']:>+10.2f}% {r['total']:>+8.1f}% {r['t']:>6.2f}")
        print("\n  ТОП убыточных:")
        for _, r in sym_df.tail(15).iloc[::-1].iterrows():
            print(f"  {r['symbol']:<14} {int(r['n']):>5} {r['wr']:>5.1%} {r['avg']:>+10.2f}% {r['total']:>+8.1f}% {r['t']:>6.2f}")
        prof = sym_df[sym_df["avg"] > 0]
        print(f"\n  символов с положительным pnl/сделку: {len(prof)}/{len(sym_df)}")

    # ── комбинированный фильтр-кандидат ──
    print(f"\n{'='*78}\nКОМБО-ФИЛЬТР: лучшая зона режима (авто-поиск)\n{'='*78}")
    best = None
    for c24_lo, c24_hi in [(-99, -3), (-99, -2), (-3, 0), (0, 99)]:
        for vol_lo in [0, 0.2, 0.25]:
            m = (A["btc_change_24h"].between(c24_lo, c24_hi)) & (A["realized_vol_1h"] >= vol_lo)
            g = A[m]
            if len(g) < 200:
                continue
            st = seg_stats(g, fee_on_margin)
            tag = f"btc_24h∈[{c24_lo},{c24_hi}] & vol≥{vol_lo}"
            if best is None or st["avg"] > best[1]["avg"]:
                best = (tag, st)
            mark = "  ✅" if st["avg"] > 0 and st["t"] > 2 else ""
            print(f"  {tag:<34} n={st['n']:>5} WR={st['wr']:.1%} pnl={st['avg']:+.2f}% t={st['t']:.2f}{mark}")
    if best:
        print(f"\n  ЛУЧШАЯ зона: {best[0]} → pnl/сделку {best[1]['avg']:+.2f}%, t={best[1]['t']:.2f}")

    print("\nГОТОВО.")
    print("Ищи строки с ✅ (pnl/сделку > 0 и t > 2) — это статзначимо прибыльные режимы.")
    print("Если таких нет нигде — проблема в СТРАТЕГИИ ВХОДА, а не в фильтре.")


if __name__ == "__main__":
    main()