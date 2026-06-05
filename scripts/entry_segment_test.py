#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ENTRY SEGMENT TEST — ищем СЕГМЕНТ сигналов с честным преимуществом вниз.

Диагноз доказан 5 раз: на ВСЁМ потоке сигналов краткосрочный шорт убыточен —
нет прибыльного TP/SL, нет прибыльного правила входа, WR ~47-50% (монетка).
score ловит "перегретые" монеты, а перегрев не предсказывает падение.

ПОСЛЕДНЯЯ ГИПОТЕЗА перед тем как признать поток мёртвым:
а что если шорт работает НЕ ВЕЗДЕ, а только в КОНКРЕТНЫХ УСЛОВИЯХ?
Например: только когда BTC падает, только на высокой волатильности,
только на определённых монетах, только в определённые часы.

Если найдём сегмент с pnl>0 и t>2 на ЧЕСТНЫХ минутных данных —
это новый КРИТЕРИЙ ОТБОРА: фильтровать сигналы по этому условию.
Если ни один сегмент не прибылен — шорт на этом потоке сигналов мёртв,
надо менять сам детектор (engine.py), а не фильтры.

ЧТО ДЕЛАЕМ:
  1. Честный тик-за-тиком минутный шорт-бэктест (вход baseline на 0-й минуте,
     при коллизии TP+SL в одной свече — SL первым, пессимизм). PnL каждой сделки.
  2. Подтягиваем сегментные признаки из canceled CSV по signal_id (== str(id)):
     btc_change_15m/1h, realized_vol_1h, score, funding_rate_at_signal,
     oi_change_pct_at_signal, symbol, час суток.
  3. Бьём сделки по сегментам и для каждого: n, WR (Wilson CI), pnl/сделку, t-стат.
  4. Помечаем ✅ сегменты с pnl>0 И t>2 (стабильное преимущество).

Запуск (локально, где лежат minute_klines.csv и canceled CSV):
    python scripts/entry_segment_test.py \
        --klines minute_klines.csv \
        --canceled canceled_signals_20260605_130117.csv \
        --tp 1.0 --sl 1.5

Зависимости: pandas numpy
"""
from __future__ import annotations
import argparse, os, sys
import numpy as np, pandas as pd


def wilson(w, n, z=1.96):
    if n == 0:
        return 0.0, 0.0, 0.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def tstat(x):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    if len(x) < 2 or x.std(ddof=1) == 0:
        return 0.0
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def simulate(candles, ep, tp, sl, lev, fee):
    """Шорт от 1-й свечи до конца окна. TP=падение tp%, SL=рост sl%.
    candles: list (minute, open, high, low, close) ascending.
    При коллизии TP+SL в одной свече — SL первым (пессимизм)."""
    tp_price = ep * (1 - tp / 100.0)
    sl_price = ep * (1 + sl / 100.0)
    for c in candles[1:]:
        _, o, hi, lo, cl = c
        hit_sl = hi >= sl_price
        hit_tp = lo <= tp_price
        if hit_sl:
            return -sl * lev - fee
        if hit_tp:
            return +tp * lev - fee
    move = (ep - candles[-1][4]) / ep * 100.0
    return move * lev - fee


def report_segment(name, df, min_n=20, pnl_col="pnl"):
    """Печатает таблицу по уникальным значениям/бакетам сегмента."""
    print("\n" + "=" * 84)
    print(f"СЕГМЕНТ: {name}")
    print("=" * 84)
    print(f"  {'значение':28s}{'сделок':>8}{'WR':>8}{'WR_low':>9}{'pnl/сд':>10}{'сумма%':>11}{'t':>7}")
    found = []
    for val, g in df.groupby("seg", observed=True):
        p = g[pnl_col].dropna().values
        if len(p) < min_n:  # слишком мало для вывода
            continue
        wr, lo, hi = wilson(int((p > 0).sum()), len(p))
        t = tstat(p)
        flag = "  ✅" if (p.mean() > 0 and t > 2) else ("  ~" if p.mean() > 0 else "")
        label = str(val)[:27]
        print(f"  {label:28s}{len(p):>8}{wr:>7.1%}{lo:>8.1%}{p.mean():>+9.2f}%{p.sum():>+10.0f}%{t:>7.2f}{flag}")
        if p.mean() > 0 and t > 2:
            found.append((name, val, len(p), p.mean(), t))
    return found


def make_buckets(series, edges, labels):
    """Бакетирование числового признака по границам."""
    return pd.cut(series, bins=edges, labels=labels, include_lowest=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--klines", default="minute_klines.csv")
    ap.add_argument("--canceled", default="canceled_signals_20260605_130117.csv")
    ap.add_argument("--tp", type=float, default=1.0)
    ap.add_argument("--sl", type=float, default=1.5)
    ap.add_argument("--fee", type=float, default=0.6)
    ap.add_argument("--lev", type=float, default=10.0)
    ap.add_argument("--min-n", type=int, default=20, help="мин. сделок чтобы показать сегмент")
    args = ap.parse_args()

    # ---- загрузка свечей (auto-fallback .csv/.parquet) ----
    kpath = args.klines
    if not os.path.exists(kpath):
        base = kpath.rsplit(".", 1)[0]
        for alt in (base + ".csv", base + ".parquet"):
            if os.path.exists(alt):
                kpath = alt
                break
        else:
            print(f"не найден {args.klines}, сначала запусти fetch_minute_klines.py")
            sys.exit(1)
    kl = pd.read_parquet(kpath) if kpath.endswith(".parquet") else pd.read_csv(kpath)
    kl["signal_id"] = kl["signal_id"].astype(str)
    print(f"свечей: {len(kl)}, сигналов: {kl['signal_id'].nunique()}")

    # ---- загрузка canceled для сегментных признаков ----
    if not os.path.exists(args.canceled):
        print(f"не найден {args.canceled} — нужен для сегментных признаков")
        sys.exit(1)
    cz = pd.read_csv(args.canceled, low_memory=False)
    cz["signal_id"] = cz["id"].astype(str)
    print(f"canceled строк: {len(cz)}, колонок: {cz.shape[1]}")
    print(f"TP={args.tp}% SL={args.sl}% (R:R {args.tp/args.sl:.2f}), плечо {args.lev:.0f}x, "
          f"комиссия {args.fee}%\n")

    # ---- честный бэктест: pnl на сигнал ----
    rows = []
    for sid, g in kl.groupby("signal_id"):
        g = g.sort_values("minute")
        candles = list(zip(g["minute"], g["open"], g["high"], g["low"], g["close"]))
        if len(candles) < 2:
            continue
        ep = candles[0][4]
        if ep <= 0:
            continue
        pnl = simulate(candles, ep, args.tp, args.sl, args.lev, args.fee)
        rows.append((str(sid), pnl))
    bt = pd.DataFrame(rows, columns=["signal_id", "pnl"])
    print(f"отыграно сделок: {len(bt)}")

    # ---- базовая линия: весь поток ----
    p_all = bt["pnl"].values
    wr, lo, hi = wilson(int((p_all > 0).sum()), len(p_all))
    print("\n" + "=" * 84)
    print("БАЗОВАЯ ЛИНИЯ (весь поток, вход сразу)")
    print("=" * 84)
    print(f"  сделок={len(p_all)}  WR={wr:.1%} [{lo:.1%}–{hi:.1%}]  "
          f"pnl/сд={p_all.mean():+.2f}%  сумма={p_all.sum():+.0f}%  t={tstat(p_all):.2f}")
    print(f"  (порог безубытка WR≈52.6% при R:R 1:1.5 с учётом комиссии)")

    # ---- merge признаков ----
    feat_cols = ["signal_id", "symbol", "score", "realized_vol_1h",
                 "btc_change_15m", "btc_change_1h", "funding_rate_at_signal",
                 "oi_change_pct_at_signal", "f_rsi", "signal_ts"]
    have = [c for c in feat_cols if c in cz.columns]
    m = bt.merge(cz[have], on="signal_id", how="left")
    matched = m["symbol"].notna().sum() if "symbol" in m.columns else 0
    print(f"\nсопоставлено с canceled: {matched}/{len(m)} сделок")

    all_found = []

    # ---- 1) режим BTC (по btc_change_1h) ----
    if "btc_change_1h" in m.columns:
        d = m.dropna(subset=["btc_change_1h"]).copy()
        d["seg"] = make_buckets(
            d["btc_change_1h"],
            [-100, -1.0, -0.3, 0.3, 1.0, 100],
            ["BTC падает <-1%", "BTC слаб.вниз", "BTC боковик", "BTC слаб.вверх", "BTC растёт >1%"])
        all_found += report_segment("режим BTC (btc_change_1h)", d, args.min_n)

    # ---- 2) краткосрочный импульс BTC (btc_change_15m) ----
    if "btc_change_15m" in m.columns:
        d = m.dropna(subset=["btc_change_15m"]).copy()
        d["seg"] = make_buckets(
            d["btc_change_15m"],
            [-100, -0.5, -0.1, 0.1, 0.5, 100],
            ["BTC15 пад<-0.5", "BTC15 -0.5..-0.1", "BTC15 флэт", "BTC15 0.1..0.5", "BTC15 рост>0.5"])
        all_found += report_segment("импульс BTC 15м (btc_change_15m)", d, args.min_n)

    # ---- 3) волатильность (realized_vol_1h, по квантилям) ----
    if "realized_vol_1h" in m.columns:
        d = m.dropna(subset=["realized_vol_1h"]).copy()
        if d["realized_vol_1h"].nunique() >= 5:
            d["seg"] = pd.qcut(d["realized_vol_1h"], 5,
                               labels=["vol Q1 низк", "vol Q2", "vol Q3", "vol Q4", "vol Q5 выс"],
                               duplicates="drop")
            all_found += report_segment("волатильность realized_vol_1h (квинтили)", d, args.min_n)

    # ---- 4) score (по квантилям) ----
    if "score" in m.columns:
        d = m.dropna(subset=["score"]).copy()
        if d["score"].nunique() >= 5:
            d["seg"] = pd.qcut(d["score"], 5,
                               labels=["score Q1 низк", "score Q2", "score Q3", "score Q4", "score Q5 выс"],
                               duplicates="drop")
            all_found += report_segment("score (квинтили)", d, args.min_n)

    # ---- 5) funding_rate_at_signal ----
    if "funding_rate_at_signal" in m.columns:
        d = m.dropna(subset=["funding_rate_at_signal"]).copy()
        if d["funding_rate_at_signal"].nunique() >= 5:
            d["seg"] = pd.qcut(d["funding_rate_at_signal"].rank(method="first"), 4,
                               labels=["funding Q1", "funding Q2", "funding Q3", "funding Q4"])
            all_found += report_segment("funding_rate (квартили)", d, args.min_n)

    # ---- 6) oi_change_pct_at_signal ----
    if "oi_change_pct_at_signal" in m.columns:
        d = m.dropna(subset=["oi_change_pct_at_signal"]).copy()
        if d["oi_change_pct_at_signal"].nunique() >= 5:
            d["seg"] = pd.qcut(d["oi_change_pct_at_signal"].rank(method="first"), 4,
                               labels=["OI Q1 низк", "OI Q2", "OI Q3", "OI Q4 выс"])
            all_found += report_segment("oi_change_pct (квартили)", d, args.min_n)

    # ---- 7) час суток (UTC) ----
    if "signal_ts" in m.columns:
        d = m.dropna(subset=["signal_ts"]).copy()
        ts = pd.to_datetime(d["signal_ts"], errors="coerce", utc=True)
        d = d[ts.notna()].copy()
        d["seg"] = ts[ts.notna()].dt.hour.map(lambda h: f"час {h:02d}h UTC")
        all_found += report_segment("час суток (UTC)", d, args.min_n)

    # ---- 8) топ-символы (только с >=min_n сделок) ----
    if "symbol" in m.columns:
        d = m.dropna(subset=["symbol"]).copy()
        vc = d["symbol"].value_counts()
        keep = vc[vc >= args.min_n].index
        d = d[d["symbol"].isin(keep)].copy()
        if len(d):
            d["seg"] = d["symbol"]
            all_found += report_segment(f"символы (>= {args.min_n} сделок)", d, args.min_n)

    # ---- ИТОГ ----
    print("\n" + "=" * 84)
    print("ВЫВОД")
    print("=" * 84)
    if all_found:
        print("  ✅ Найдены сегменты со СТАБИЛЬНЫМ преимуществом вниз (pnl>0 и t>2):")
        for name, val, n, mean, t in sorted(all_found, key=lambda x: -x[3]):
            print(f"     • {name} = '{val}': {mean:+.2f}%/сделку, t={t:.2f}, n={n}")
        print("\n  ЭТО НОВЫЙ КРИТЕРИЙ ОТБОРА. Следующий шаг — фильтровать сигналы")
        print("  по этому условию в engine.py и проверить out-of-sample на новых данных.")
        print("  ВНИМАНИЕ: проверь, что сегмент не переоткрыт случайно (много сегментов →")
        print("  один-два пройдут порог t>2 по случайности). Доверяй только если есть")
        print("  экономический смысл (напр. 'BTC падает' → шорты альтов логично работают).")
    else:
        print("  ❌ Ни один сегмент не даёт стабильного преимущества вниз (pnl>0 и t>2).")
        print("     Это финальное доказательство: на ЭТОМ потоке сигналов шорт мёртв")
        print("     в любых условиях рынка/волатильности/времени/монеты.")
        print("     Фильтрами поток не спасти — проблема в самом ДЕТЕКТОРЕ (engine.py):")
        print("     он отбирает монеты по перегреву, а перегрев не предсказывает падение.")
        print("     Единственный путь к прибыли — переписать критерий сигнала:")
        print("     ловить РАЗВОРОТ (upper_wick, ликвидации, vol-всплеск + BTC-режим),")
        print("     а не перегрев. Это уже не фильтр поверх старого, а новый мозг детектора.")


if __name__ == "__main__":
    main()
