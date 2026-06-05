#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REVERSAL SIGNAL TEST — судьба проекта без переписывания engine.py.

КОНТЕКСТ (доказано 6 раз):
  На всём потоке сигналов краткосрочный шорт убыточен. score ловит ПЕРЕГРЕВ,
  а перегрев не предсказывает падение. Ни один сегмент (BTC-режим, vol, score,
  funding, OI, час, символ) не даёт pnl>0 с t>2 на честных минутных тиках.

ЕДИНСТВЕННАЯ ЖИВАЯ ЗАЦЕПКА:
  Снапшот-модель на КРУПНЫХ падениях (>=1.5%) дала AUC 0.68, и тянули её
  f_upper_wick, f_liquidation_cascade, realized_vol_1h, btc_change —
  признаки РАЗВОРОТА и РЕЖИМА, а не перегрева. На честных тиках НЕ проверено.

ГИПОТЕЗА:
  Если шортить НЕ все сигналы, а только те, где в момент сигнала видны признаки
  РАЗВОРОТА (большой верхний фитиль + всплеск объёма + растяжение от VWAP +
  потеря импульса + подходящий режим BTC) — даст ли ЭТОТ поднабор pnl>0
  на честных минутных тиках?

ЧТО ДЕЛАЕМ:
  1. Честный tick-by-tick шорт-бэктест (вход на 0-й минуте, при коллизии TP+SL
     в одной свече — SL первым, пессимизм). PnL каждого сигнала.
  2. Подтягиваем признаки разворота из canceled CSV по signal_id (== str(id)).
  3. Прогоняем НАБОР правил-фильтров разворота (одиночные пороги + комбинации).
     Для каждого: сколько сигналов прошло, WR (Wilson CI), pnl/сделку, t-стат,
     сравнение с базовой линией. Помечаем ✅ pnl>0 и t>2.
  4. Honest OOF: простая логрег-модель ТОЛЬКО на признаках разворота, прогноз
     "крупное падение", затем шортим топ-X% по proba и считаем честный pnl.
     Это проверяет: можно ли ВООБЩЕ отделить падающие монеты этими признаками.

ВЫВОД:
  Если хоть одно правило/порог proba даёт стабильный pnl>0 (t>2) с экономическим
  смыслом → новый критерий для engine.py, проект жив. Если нет → шорт мёртв и на
  развороте; закрываем, не переписывая engine.py.

Запуск (локально):
    python scripts/reversal_signal_test.py \
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


def simulate(candles, ep, tp, sl, lev, fee):
    """Шорт от 1-й свечи до конца окна. TP=падение tp%, SL=рост sl%.
    candles: list (minute, open, high, low, close) ascending.
    При коллизии TP+SL в одной свече — SL первым (пессимизм)."""
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


def line(label, p, base_mean):
    """Печатает строку статистики поднабора p (массив pnl) против базы."""
    if len(p) == 0:
        print(f"  {label:38s}{'нет сделок':>12}")
        return None
    wr, lo, hi = wilson(int((p > 0).sum()), len(p))
    t = tstat(p)
    delta = p.mean() - base_mean
    flag = "  ✅" if (p.mean() > 0 and t > 2) else ("  ~" if p.mean() > 0 else "")
    print(f"  {label:38s}{len(p):>7}{wr:>7.1%}{lo:>8.1%}{p.mean():>+9.2f}%"
          f"{delta:>+9.2f}%{t:>7.2f}{flag}")
    return (label, len(p), p.mean(), t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--klines", default="minute_klines.csv")
    ap.add_argument("--canceled", default="canceled_signals_20260605_130117.csv")
    ap.add_argument("--tp", type=float, default=1.0)
    ap.add_argument("--sl", type=float, default=1.5)
    ap.add_argument("--fee", type=float, default=0.6)
    ap.add_argument("--lev", type=float, default=10.0)
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--gap", type=int, default=50, help="purge gap для OOF по времени")
    args = ap.parse_args()

    # ---- свечи (auto-fallback .csv/.parquet) ----
    kpath = args.klines
    if not os.path.exists(kpath):
        base = kpath.rsplit(".", 1)[0]
        for alt in (base + ".csv", base + ".parquet"):
            if os.path.exists(alt):
                kpath = alt
                break
        else:
            print(f"не найден {args.klines}")
            sys.exit(1)
    kl = pd.read_parquet(kpath) if kpath.endswith(".parquet") else pd.read_csv(kpath)
    kl["signal_id"] = kl["signal_id"].astype(str)
    print(f"свечей: {len(kl)}, сигналов: {kl['signal_id'].nunique()}")

    if not os.path.exists(args.canceled):
        print(f"не найден {args.canceled}")
        sys.exit(1)
    cz = pd.read_csv(args.canceled, low_memory=False)
    cz["signal_id"] = cz["id"].astype(str)
    print(f"canceled строк: {len(cz)}, колонок: {cz.shape[1]}")
    print(f"TP={args.tp}% SL={args.sl}% (R:R {args.tp/args.sl:.2f}), плечо {args.lev:.0f}x, "
          f"комиссия {args.fee}%\n")

    # ---- честный бэктест ----
    rows = []
    for sid, g in kl.groupby("signal_id"):
        g = g.sort_values("minute")
        candles = list(zip(g["minute"], g["open"], g["high"], g["low"], g["close"]))
        if len(candles) < 2:
            continue
        ep = candles[0][4]
        if ep <= 0:
            continue
        rows.append((str(sid), simulate(candles, ep, args.tp, args.sl, args.lev, args.fee)))
    bt = pd.DataFrame(rows, columns=["signal_id", "pnl"])

    # признаки разворота
    rev_cols = ["f_upper_wick", "f_liquidation_cascade", "f_volume_zscore",
                "f_vwap_extension", "f_momentum_loss", "f_rsi", "realized_vol_1h",
                "realized_vol_1h_z", "btc_change_15m", "btc_change_1h",
                "btc_change_4h", "price_change_pct", "signal_ts"]
    have = ["signal_id"] + [c for c in rev_cols if c in cz.columns]
    m = bt.merge(cz[have], on="signal_id", how="left")
    for c in have:
        if c not in ("signal_id", "signal_ts"):
            m[c] = pd.to_numeric(m[c], errors="coerce")

    base = m["pnl"].values
    base_mean = base.mean()
    wr, lo, hi = wilson(int((base > 0).sum()), len(base))
    print("=" * 92)
    print("БАЗОВАЯ ЛИНИЯ (весь поток)")
    print("=" * 92)
    print(f"  сделок={len(base)}  WR={wr:.1%} [{lo:.1%}–{hi:.1%}]  pnl/сд={base_mean:+.2f}%  "
          f"сумма={base.sum():+.0f}%  t={tstat(base):.2f}")
    print(f"  порог безубытка WR≈{(args.sl+args.fee/args.lev)/(args.tp+args.sl)*100:.1f}% "
          f"при R:R {args.tp/args.sl:.2f}\n")

    found = []
    hdr = (f"  {'правило':38s}{'n':>7}{'WR':>7}{'WR_low':>8}{'pnl/сд':>9}{'Δ к базе':>9}{'t':>7}")

    # ---- 1) одиночные пороги признаков разворота ----
    print("=" * 92)
    print("ОДИНОЧНЫЕ ПОРОГИ ПРИЗНАКОВ РАЗВОРОТА")
    print("=" * 92)
    print(hdr)
    rules1 = []
    if "f_upper_wick" in m: rules1 += [
        ("upper_wick > 1.0", m["f_upper_wick"] > 1.0),
        ("upper_wick > 2.0", m["f_upper_wick"] > 2.0),
        ("upper_wick > 3.0", m["f_upper_wick"] > 3.0)]
    if "f_liquidation_cascade" in m: rules1 += [
        ("liq_cascade > 0.75", m["f_liquidation_cascade"] > 0.75),
        ("liq_cascade > 0.85", m["f_liquidation_cascade"] > 0.85)]
    if "f_volume_zscore" in m: rules1 += [
        ("vol_zscore > 3", m["f_volume_zscore"] > 3),
        ("vol_zscore > 5", m["f_volume_zscore"] > 5)]
    if "f_vwap_extension" in m: rules1 += [
        ("vwap_extension > 2", m["f_vwap_extension"] > 2),
        ("vwap_extension > 3", m["f_vwap_extension"] > 3)]
    if "f_momentum_loss" in m: rules1 += [
        ("momentum_loss == 1", m["f_momentum_loss"] >= 1)]
    if "realized_vol_1h_z" in m: rules1 += [
        ("realized_vol_z > 2", m["realized_vol_1h_z"] > 2),
        ("realized_vol_z > 3", m["realized_vol_1h_z"] > 3)]
    if "f_rsi" in m: rules1 += [
        ("rsi > 80 (экстрем перекуп)", m["f_rsi"] > 80)]
    if "price_change_pct" in m: rules1 += [
        ("price_change_pct > 2 (резкий памп)", m["price_change_pct"] > 2)]
    for label, mask in rules1:
        r = line(label, m.loc[mask.fillna(False), "pnl"].values, base_mean)
        if r and r[2] > 0 and r[3] > 2: found.append(r)

    # ---- 2) комбинации разворота ----
    print("\n" + "=" * 92)
    print("КОМБИНАЦИИ ПРИЗНАКОВ РАЗВОРОТА")
    print("=" * 92)
    print(hdr)
    def g(col): return m[col] if col in m else pd.Series(False, index=m.index)
    rules2 = [
        ("wick>1 & vol_z>3",
         (g("f_upper_wick") > 1) & (g("f_volume_zscore") > 3)),
        ("wick>2 & vwap_ext>2",
         (g("f_upper_wick") > 2) & (g("f_vwap_extension") > 2)),
        ("wick>1 & vol_z>3 & vwap_ext>1.5",
         (g("f_upper_wick") > 1) & (g("f_volume_zscore") > 3) & (g("f_vwap_extension") > 1.5)),
        ("liq_casc>0.8 & vol_z>3",
         (g("f_liquidation_cascade") > 0.8) & (g("f_volume_zscore") > 3)),
        ("wick>2 & momentum_loss & vol_z>3",
         (g("f_upper_wick") > 2) & (g("f_momentum_loss") >= 1) & (g("f_volume_zscore") > 3)),
        ("разворот + BTC не растёт (btc_1h<0.3)",
         (g("f_upper_wick") > 1) & (g("f_volume_zscore") > 3) & (g("btc_change_1h") < 0.3)),
        ("сильный памп+разворот: pchg>2 & wick>2",
         (g("price_change_pct") > 2) & (g("f_upper_wick") > 2)),
    ]
    for label, mask in rules2:
        r = line(label, m.loc[mask.fillna(False), "pnl"].values, base_mean)
        if r and r[2] > 0 and r[3] > 2: found.append(r)

    # ---- 3) honest OOF логрег на признаках разворота ----
    print("\n" + "=" * 92)
    print("HONEST OOF: логрег на признаках разворота → шорт топ-X% по proba")
    print("=" * 92)
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        feats = [c for c in ["f_upper_wick", "f_liquidation_cascade", "f_volume_zscore",
                             "f_vwap_extension", "f_momentum_loss", "f_rsi",
                             "realized_vol_1h_z", "btc_change_15m", "btc_change_1h",
                             "btc_change_4h", "price_change_pct"] if c in m]
        # таргет: сделка прибыльна (pnl>0) на ЧЕСТНЫХ тиках
        d = m.dropna(subset=feats).copy()
        d = d.reset_index(drop=True)
        # порядок по времени для purged OOF
        if "signal_ts" in d:
            d["_t"] = pd.to_datetime(d["signal_ts"], errors="coerce", utc=True)
            d = d.sort_values("_t").reset_index(drop=True)
        y = (d["pnl"].values > 0).astype(int)
        X = d[feats].values
        n = len(d)
        proba = np.full(n, np.nan)
        fold = n // args.splits
        for k in range(args.splits):
            te0, te1 = k * fold, (n if k == args.splits - 1 else (k + 1) * fold)
            te = np.arange(te0, te1)
            tr = np.array([i for i in range(n)
                           if i < te0 - args.gap or i >= te1 + args.gap])
            if len(tr) < 50 or len(np.unique(y[tr])) < 2:
                continue
            sc = StandardScaler().fit(X[tr])
            lr = LogisticRegression(max_iter=1000, C=0.5, class_weight="balanced")
            lr.fit(sc.transform(X[tr]), y[tr])
            proba[te] = lr.predict_proba(sc.transform(X[te]))[:, 1]
        d["proba"] = proba
        dd = d.dropna(subset=["proba"])
        # AUC
        try:
            from sklearn.metrics import roc_auc_score
            auc = roc_auc_score((dd["pnl"] > 0).astype(int), dd["proba"])
            print(f"  honest OOF AUC (proba прибыльности): {auc:.3f}  (0.5 = шум)\n")
        except Exception:
            pass
        print(hdr.replace("правило", "топ по proba"))
        for frac in (0.05, 0.10, 0.20, 0.30, 0.50):
            thr = dd["proba"].quantile(1 - frac)
            sub = dd.loc[dd["proba"] >= thr, "pnl"].values
            r = line(f"топ {int(frac*100)}% по proba (>= {thr:.3f})", sub, base_mean)
            if r and r[2] > 0 and r[3] > 2: found.append(r)
    except ImportError:
        print("  sklearn не установлен — пропускаю OOF-модель")

    # ---- ИТОГ ----
    print("\n" + "=" * 92)
    print("ВЫВОД")
    print("=" * 92)
    if found:
        print("  ✅ Найдены правила РАЗВОРОТА со стабильным преимуществом вниз (pnl>0, t>2):")
        for label, n, mean, t in sorted(found, key=lambda x: -x[2]):
            print(f"     • {label}: {mean:+.2f}%/сделку, t={t:.2f}, n={n}")
        print("\n  ПРОЕКТ ЖИВ. Это новый критерий для engine.py.")
        print("  ВАЖНО: проверь экономический смысл и достаточный n (>200 лучше).")
        print("  Следующий шаг — встроить условие в детектор и собрать НОВЫЕ данные")
        print("  для out-of-sample подтверждения (на старых данных правило могло")
        print("  переоткрыться: много правил → одно случайно пройдёт t>2).")
    else:
        print("  ❌ Гипотеза разворота МЕРТВА на честных минутных тиках.")
        print("     Ни одно правило/порог по признакам разворота (upper_wick, ликвидации,")
        print("     vol-всплеск, VWAP-растяжение, momentum_loss, BTC-режим), ни OOF-модель")
        print("     на этих признаках не дают прибыльного поднабора.")
        print("     Снапшот-AUC 0.68 на 'крупных падениях' был иллюзией: он мерил")
        print("     ДОСТИЖИМОСТЬ цели вниз, игнорируя стоп и порядок тиков. На честных")
        print("     данных преимущества нет.")
        print("")
        print("     ФИНАЛЬНЫЙ ВЕРДИКТ: краткосрочный шорт на этом потоке нерентабелен")
        print("     ни фильтрами, ни сменой критерия на разворот. Инфраструктура")
        print("     (snapshot→данные→бэктест→ML) исправна и переиспользуема, но сама")
        print("     ИДЕЯ 'ловить разворот альтов на минутках по этим признакам' не несёт")
        print("     торгового преимущества при R:R≈0.67 и комиссии 0.6%.")


if __name__ == "__main__":
    main()
