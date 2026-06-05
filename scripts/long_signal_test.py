#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LONG SIGNAL TEST — а что если детектор ловит РОСТ, а не падение?

КОНТЕКСТ (доказано 7 раз):
  Краткосрочный ШОРТ на этом потоке сигналов нерентабелен — ни фильтрами,
  ни ML, ни сменой критерия на разворот. OOF AUC ~0.51 (шум).
  КЛЮЧЕВОЕ НАБЛЮДЕНИЕ: WR против шорта ~55% → монеты после сигнала чаще
  РАСТУТ или стоят, чем валятся. delay_15 показывал рост сразу после сигнала.

ГИПОТЕЗА:
  Детектор отбирает монеты с сильным импульсом ВВЕРХ (перегрев = памп идёт).
  Возможно он случайно нашёл сигнал ПРОДОЛЖЕНИЯ РОСТА, а не разворота.
  Тогда на тех же сигналах ЛОНГ может быть прибылен.

ЧТО ДЕЛАЕМ:
  Зеркало short-теста. ЛОНГ от 0-й минуты: TP=рост tp%, SL=падение sl%.
  При коллизии TP+SL в одной свече — SL первым (пессимизм). Честно tick-by-tick.
  1. Базовая линия лонга (весь поток) + прямое сравнение ЛОНГ vs ШОРТ.
  2. Правила входа (baseline / задержки / откат / пробой).
  3. Сегменты (BTC-режим, импульс BTC, vol, score, час, символ).
  4. Honest OOF логрег на признаках → лонг топ-X% по proba.
  Помечаем ✅ pnl>0 и t>2.

ВЫВОД:
  Если ЛОНГ даёт стабильный pnl>0 (t>2) — детектор ловит продолжение роста,
  надо РАЗВЕРНУТЬ бота в лонг (или открыть лонги через MlService). Проект жив.
  Если и лонг мёртв — поток сигналов не несёт направленного преимущества вообще.

Запуск (локально):
    python scripts/long_signal_test.py \
        --klines minute_klines.csv \
        --canceled canceled_signals_20260605_130117.csv \
        --tp 1.0 --sl 1.5

Зависимости: pandas numpy scikit-learn
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


def sim_long(candles, ep, tp, sl, lev, fee):
    """ЛОНГ от 1-й свечи. TP=рост tp% (high достигает вверх), SL=падение sl% (low вниз).
    При коллизии в одной свече — SL первым (пессимизм). pnl от роста цены."""
    tp_price = ep * (1 + tp / 100.0)
    sl_price = ep * (1 - sl / 100.0)
    for c in candles[1:]:
        _, o, hi, lo, cl = c
        if lo <= sl_price:       # стоп вниз сработал первым
            return -sl * lev - fee
        if hi >= tp_price:       # тейк вверх
            return +tp * lev - fee
    move = (candles[-1][4] - ep) / ep * 100.0   # лонг: профит от роста
    return move * lev - fee


def sim_short(candles, ep, tp, sl, lev, fee):
    """ШОРТ (для прямого сравнения). TP=падение, SL=рост, SL первым."""
    tp_price = ep * (1 - tp / 100.0)
    sl_price = ep * (1 + sl / 100.0)
    for c in candles[1:]:
        _, o, hi, lo, cl = c
        if hi >= sl_price:
            return -sl * lev - fee
        if lo <= tp_price:
            return +tp * lev - fee
    move = (ep - candles[-1][4]) / ep * 100.0
    return move * lev - fee


def stat_line(label, p, base_mean=None):
    if len(p) == 0:
        print(f"  {label:34s}{'нет сделок':>12}")
        return None
    wr, lo, hi = wilson(int((p > 0).sum()), len(p))
    t = tstat(p)
    extra = f"{(p.mean()-base_mean):>+9.2f}%" if base_mean is not None else " " * 10
    flag = "  ✅" if (p.mean() > 0 and t > 2) else ("  ~" if p.mean() > 0 else "")
    print(f"  {label:34s}{len(p):>7}{wr:>7.1%}{lo:>8.1%}{p.mean():>+9.2f}%{extra}{t:>7.2f}{flag}")
    return (label, len(p), p.mean(), t)


def make_buckets(series, edges, labels):
    return pd.cut(series, bins=edges, labels=labels, include_lowest=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--klines", default="minute_klines.csv")
    ap.add_argument("--canceled", default="canceled_signals_20260605_130117.csv")
    ap.add_argument("--tp", type=float, default=1.0)
    ap.add_argument("--sl", type=float, default=1.5)
    ap.add_argument("--fee", type=float, default=0.6)
    ap.add_argument("--lev", type=float, default=10.0)
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--gap", type=int, default=50)
    args = ap.parse_args()

    kpath = args.klines
    if not os.path.exists(kpath):
        base = kpath.rsplit(".", 1)[0]
        for alt in (base + ".csv", base + ".parquet"):
            if os.path.exists(alt):
                kpath = alt; break
        else:
            print(f"не найден {args.klines}"); sys.exit(1)
    kl = pd.read_parquet(kpath) if kpath.endswith(".parquet") else pd.read_csv(kpath)
    kl["signal_id"] = kl["signal_id"].astype(str)
    print(f"свечей: {len(kl)}, сигналов: {kl['signal_id'].nunique()}")

    if not os.path.exists(args.canceled):
        print(f"не найден {args.canceled}"); sys.exit(1)
    cz = pd.read_csv(args.canceled, low_memory=False)
    cz["signal_id"] = cz["id"].astype(str)
    print(f"canceled строк: {len(cz)}, колонок: {cz.shape[1]}")
    print(f"TP={args.tp}% SL={args.sl}% (R:R {args.tp/args.sl:.2f}), плечо {args.lev:.0f}x, "
          f"комиссия {args.fee}%  [ЛОНГ]\n")

    # ---- бэктест: и лонг, и шорт на одних свечах ----
    rows = []
    for sid, gdf in kl.groupby("signal_id"):
        gdf = gdf.sort_values("minute")
        candles = list(zip(gdf["minute"], gdf["open"], gdf["high"], gdf["low"], gdf["close"]))
        if len(candles) < 2:
            continue
        ep = candles[0][4]
        if ep <= 0:
            continue
        rows.append((str(sid),
                     sim_long(candles, ep, args.tp, args.sl, args.lev, args.fee),
                     sim_short(candles, ep, args.tp, args.sl, args.lev, args.fee)))
    bt = pd.DataFrame(rows, columns=["signal_id", "pnl", "pnl_short"])

    feat = ["signal_id", "symbol", "score", "realized_vol_1h", "btc_change_15m",
            "btc_change_1h", "f_rsi", "f_upper_wick", "f_volume_zscore",
            "f_vwap_extension", "price_change_pct", "signal_ts"]
    have = [c for c in feat if c in cz.columns]
    m = bt.merge(cz[have], on="signal_id", how="left")
    for c in have:
        if c not in ("signal_id", "symbol", "signal_ts"):
            m[c] = pd.to_numeric(m[c], errors="coerce")

    L = m["pnl"].values
    S = m["pnl_short"].values
    be = (args.sl + args.fee/args.lev) / (args.tp + args.sl) * 100
    print("=" * 88)
    print("ЛОНГ vs ШОРТ на ОДНИХ свечах (весь поток)")
    print("=" * 88)
    for name, p in (("ЛОНГ ", L), ("ШОРТ ", S)):
        wr, lo, hi = wilson(int((p > 0).sum()), len(p))
        print(f"  {name}: n={len(p)} WR={wr:.1%} [{lo:.1%}–{hi:.1%}] "
              f"pnl/сд={p.mean():+.2f}% сумма={p.sum():+.0f}% t={tstat(p):.2f}")
    print(f"  порог безубытка WR≈{be:.1f}% при R:R {args.tp/args.sl:.2f}")
    print(f"  средний ход за окно (без TP/SL, в %): "
          f"лонг-сделки опираются на рост цены\n")

    base_mean = L.mean()
    found = []
    hdr = (f"  {'правило / сегмент':34s}{'n':>7}{'WR':>7}{'WR_low':>8}{'pnl/сд':>9}{'Δбаза':>10}{'t':>7}")

    # ---- правила входа (лонг) ----
    print("=" * 88)
    print("ПРАВИЛА ВХОДА (ЛОНГ)")
    print("=" * 88)
    print(hdr)
    # baseline уже = L. Доп. правила требуют пересчёта по свечам:
    def backtest_rule(delay=0, first_green=False, breakout=False):
        out = []
        for sid, gdf in kl.groupby("signal_id"):
            gdf = gdf.sort_values("minute")
            cs = list(zip(gdf["minute"], gdf["open"], gdf["high"], gdf["low"], gdf["close"]))
            if len(cs) < 2 + delay:
                continue
            start = 1 + delay
            if first_green:  # ждём первую зелёную свечу
                j = None
                for i in range(1, len(cs)):
                    if cs[i][4] > cs[i][1]:
                        j = i; break
                if j is None or j >= len(cs) - 1:
                    continue
                start = j + 1
            if breakout:  # вход при пробое хая 1-й свечи
                hi0 = cs[0][2]
                j = None
                for i in range(1, len(cs)):
                    if cs[i][2] > hi0:
                        j = i; break
                if j is None or j >= len(cs) - 1:
                    continue
                start = j + 1
            if start >= len(cs):
                continue
            ep = cs[start - 1][4]
            if ep <= 0:
                continue
            out.append(sim_long(cs[start - 1:], ep, args.tp, args.sl, args.lev, args.fee))
        return np.array(out)

    stat_line("baseline (вход сразу)", L, base_mean)
    for lbl, kw in [("delay_5 (через 5 мин)", dict(delay=5)),
                    ("delay_15 (через 15 мин)", dict(delay=15)),
                    ("first_green (1-я зелёная)", dict(first_green=True)),
                    ("breakout (пробой хая)", dict(breakout=True))]:
        r = stat_line(lbl, backtest_rule(**kw), base_mean)
        if r and r[2] > 0 and r[3] > 2: found.append(r)

    # ---- сегменты (лонг) ----
    def seg_report(name, d):
        print("\n" + "=" * 88)
        print(f"СЕГМЕНТ (ЛОНГ): {name}")
        print("=" * 88)
        print(hdr)
        loc = []
        for val, g2 in d.groupby("seg", observed=True):
            p = g2["pnl"].dropna().values
            if len(p) < args.min_n:
                continue
            r = stat_line(str(val)[:33], p, base_mean)
            if r and r[2] > 0 and r[3] > 2:
                loc.append((name + ": " + str(val), r[1], r[2], r[3]))
        return loc

    if "btc_change_1h" in m:
        d = m.dropna(subset=["btc_change_1h"]).copy()
        d["seg"] = make_buckets(d["btc_change_1h"], [-100,-1,-0.3,0.3,1,100],
            ["BTC пад<-1%","BTC слаб.вниз","BTC боковик","BTC слаб.вверх","BTC рост>1%"])
        found += seg_report("режим BTC", d)
    if "realized_vol_1h" in m:
        d = m.dropna(subset=["realized_vol_1h"]).copy()
        if d["realized_vol_1h"].nunique() >= 5:
            d["seg"] = pd.qcut(d["realized_vol_1h"],5,labels=["vol Q1","vol Q2","vol Q3","vol Q4","vol Q5"],duplicates="drop")
            found += seg_report("волатильность (квинтили)", d)
    if "score" in m:
        d = m.dropna(subset=["score"]).copy()
        if d["score"].nunique() >= 5:
            d["seg"] = pd.qcut(d["score"],5,labels=["score Q1","score Q2","score Q3","score Q4","score Q5"],duplicates="drop")
            found += seg_report("score (квинтили)", d)
    if "signal_ts" in m:
        d = m.dropna(subset=["signal_ts"]).copy()
        ts = pd.to_datetime(d["signal_ts"], errors="coerce", utc=True)
        d = d[ts.notna()].copy(); d["seg"] = ts[ts.notna()].dt.hour.map(lambda h: f"час {h:02d}h")
        found += seg_report("час суток (UTC)", d)
    if "symbol" in m:
        d = m.dropna(subset=["symbol"]).copy()
        vc = d["symbol"].value_counts(); keep = vc[vc >= args.min_n].index
        d = d[d["symbol"].isin(keep)].copy()
        if len(d):
            d["seg"] = d["symbol"]; found += seg_report(f"символы (>= {args.min_n})", d)

    # ---- honest OOF логрег → лонг топ-X% ----
    print("\n" + "=" * 88)
    print("HONEST OOF: логрег на признаках → ЛОНГ топ-X% по proba")
    print("=" * 88)
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.metrics import roc_auc_score
        feats = [c for c in ["f_rsi","f_upper_wick","f_volume_zscore","f_vwap_extension",
                             "realized_vol_1h","btc_change_15m","btc_change_1h",
                             "price_change_pct","score"] if c in m]
        d = m.dropna(subset=feats).copy()
        if "signal_ts" in d:
            d["_t"] = pd.to_datetime(d["signal_ts"], errors="coerce", utc=True)
            d = d.sort_values("_t")
        d = d.reset_index(drop=True)
        y = (d["pnl"].values > 0).astype(int)
        X = d[feats].values; n = len(d)
        proba = np.full(n, np.nan); fold = n // args.splits
        for k in range(args.splits):
            te0, te1 = k*fold, (n if k == args.splits-1 else (k+1)*fold)
            te = np.arange(te0, te1)
            tr = np.array([i for i in range(n) if i < te0-args.gap or i >= te1+args.gap])
            if len(tr) < 50 or len(np.unique(y[tr])) < 2:
                continue
            sc = StandardScaler().fit(X[tr])
            lr = LogisticRegression(max_iter=1000, C=0.5, class_weight="balanced")
            lr.fit(sc.transform(X[tr]), y[tr])
            proba[te] = lr.predict_proba(sc.transform(X[te]))[:, 1]
        d["proba"] = proba; dd = d.dropna(subset=["proba"])
        auc = roc_auc_score((dd["pnl"]>0).astype(int), dd["proba"])
        print(f"  honest OOF AUC (proba прибыльности лонга): {auc:.3f}  (0.5 = шум)\n")
        print(hdr.replace("правило / сегмент", "топ по proba"))
        for frac in (0.05, 0.10, 0.20, 0.30, 0.50):
            thr = dd["proba"].quantile(1-frac)
            r = stat_line(f"топ {int(frac*100)}% (>= {thr:.3f})", dd.loc[dd["proba"]>=thr,"pnl"].values, base_mean)
            if r and r[2] > 0 and r[3] > 2: found.append(r)
    except ImportError:
        print("  sklearn не установлен — пропускаю OOF")

    print("\n" + "=" * 88)
    print("ВЫВОД")
    print("=" * 88)
    if found:
        print("  ✅ Найдены ЛОНГ-правила/сегменты со стабильным преимуществом (pnl>0, t>2):")
        for label, n, mean, t in sorted(found, key=lambda x: -x[2]):
            print(f"     • {label}: {mean:+.2f}%/сделку, t={t:.2f}, n={n}")
        print("\n  ДЕТЕКТОР ЛОВИТ ПРОДОЛЖЕНИЕ РОСТА, А НЕ РАЗВОРОТ. Проект разворачивается")
        print("  в ЛОНГ. Следующий шаг — открывать лонги (а не шорты) на этих сигналах,")
        print("  встроить лучшее правило/порог proba и собрать НОВЫЕ данные для")
        print("  out-of-sample подтверждения (на старых данных правило могло переоткрыться).")
    else:
        print("  ❌ И ЛОНГ мёртв. Поток сигналов не несёт НАПРАВЛЕННОГО преимущества —")
        print("     ни вниз (шорт, 7 доказательств), ни вверх (лонг, этот тест).")
        print("     Сигнал отбирает волатильные перегретые монеты, но их дальнейшее")
        print("     направление за 60-мин окно — случайно. Это финальный вывод по идее")
        print("     детектора на минутном горизонте: торгового edge нет ни в какую сторону.")
        print("     Остаются варианты: другой горизонт (5-15м / 4-24ч) или другой класс")
        print("     стратегий (фандинг-арбитраж), но НЕ этот сигнал на минутках.")


if __name__ == "__main__":
    main()
