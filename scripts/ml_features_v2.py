#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ML FEATURES V2 — учим модель отличать монеты, которые УПАДУТ (хороший шорт)
от тех, что продолжат расти (плохой шорт).

Идея: базовый score ловит "перегретость", но перегрев ≠ падение. Поэтому
строим композитные признаки ИСЧЕРПАНИЯ пампа (exhaustion) — не "RSI высокий",
а "RSI высокий И моментум выдыхается И покупатели уходят И стенка бидов тонкая".
Имя монеты НЕ используем — чтобы модель обобщала на новые символы.

Сравниваем три набора фичей честно (purge-gap TimeSeriesSplit + permutation importance):
  A) baseline      — сырые фичи как есть (как сейчас в боте)
  B) +exhaustion   — baseline + новые композитные признаки исчерпания
  C) exhaustion-only — только новые, чтобы увидеть их чистую силу

Цель: понять, поднимается ли honest AUC выше ~0.52-0.53. Если да — направление
верное (есть извлекаемый сигнал про падение). Если нет — проблема в триггере.

Запуск:
    python scripts/ml_features_v2.py --auto auto_shorts_XXXX.csv

Зависимости: pandas numpy scikit-learn lightgbm
"""
from __future__ import annotations
import argparse, glob, os, sys, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
try:
    import lightgbm as lgb
except ImportError:
    print("нужен lightgbm: pip install lightgbm"); sys.exit(1)
from sklearn.metrics import roc_auc_score
from sklearn.inspection import permutation_importance


# базовые сырые фичи (доступны в момент сигнала, не утечка)
BASE_FEATS = [
    "f_rsi", "f_rsi_5m", "f_vwap_extension", "f_volume_zscore", "f_trade_imbalance",
    "f_large_buy_cluster", "f_large_sell_cluster", "f_price_acceleration",
    "f_consecutive_greens", "f_ob_bid_thinning", "f_spread_expansion",
    "f_momentum_loss", "f_upper_wick", "f_funding_rate", "f_cvd_divergence",
    "f_liquidation_cascade", "realized_vol_1h", "realized_vol_1h_z",
    "volume_24h_usdt_z", "price_change_5m", "price_change_1h", "spread_pct",
    "spread_pct_z", "bid_depth_change_5m", "bid_depth_change_5m_z",
    "oi_change_pct_at_signal", "oi_change_pct_z", "trend_strength_1h",
    "ob_bid_volume_top10", "ob_ask_volume_top10", "ob_imbalance_top10",
    "ob_spread_bps", "ob_bid_wall_size", "ob_ask_wall_size",
    "btc_change_15m", "btc_change_1h", "btc_change_4h", "btc_change_24h",
    "btc_adx_1h", "btc_atr_pct_1h", "recent_wr_20", "score",
    "price_change_at_entry", "triggered_count",
]


def safe(df, col, default=0.0):
    return df[col] if col in df.columns else pd.Series(default, index=df.index)


def add_exhaustion_features(df: pd.DataFrame) -> list[str]:
    """Композитные признаки ИСЧЕРПАНИЯ пампа (гипотезы 'монета упадёт')."""
    new = []

    # 1. EXHAUSTION SCORE: перегрев + признаки разворота одновременно.
    #    высокий RSI, но моментум теряется и появляется верхний фитиль.
    rsi = safe(df, "f_rsi")
    df["fx_overheat_reversal"] = (
        (rsi / 100.0) * safe(df, "f_momentum_loss") * (1 + safe(df, "f_upper_wick"))
    )
    new.append("fx_overheat_reversal")

    # 2. RSI divergence: RSI(5m) уже падает относительно RSI(общий) — ранний разворот
    df["fx_rsi_rollover"] = safe(df, "f_rsi") - safe(df, "f_rsi_5m")
    new.append("fx_rsi_rollover")

    # 3. BUYER EXHAUSTION: покупатели выдыхаются — крупные продажи > крупных покупок
    buy = safe(df, "f_large_buy_cluster"); sell = safe(df, "f_large_sell_cluster")
    df["fx_sell_dominance"] = (sell - buy) / (buy + sell + 1)
    new.append("fx_sell_dominance")

    # 4. ORDERBOOK FRAGILITY: биды тонкие + аски толстые = цена легко падает
    bidv = safe(df, "ob_bid_volume_top10"); askv = safe(df, "ob_ask_volume_top10")
    df["fx_ob_top_heavy"] = (askv - bidv) / (askv + bidv + 1)
    df["fx_bid_thinning_strong"] = safe(df, "f_ob_bid_thinning") * (1 + safe(df, "f_spread_expansion"))
    new += ["fx_ob_top_heavy", "fx_bid_thinning_strong"]

    # 5. STRETCH: насколько цена растянута от VWAP с учётом скорости (поздний памп)
    df["fx_vwap_stretch_accel"] = safe(df, "f_vwap_extension") * safe(df, "f_price_acceleration")
    new.append("fx_vwap_stretch_accel")

    # 6. PARABOLIC: резкий рост за 5м относительно 1ч (вертикальный памп = откат вероятен)
    pc5 = safe(df, "price_change_5m"); pc1h = safe(df, "price_change_1h")
    df["fx_parabolic_ratio"] = pc5 / (pc1h.abs() + 1e-6)
    new.append("fx_parabolic_ratio")

    # 7. CONSEC GREENS overdone: много зелёных подряд = близко к истощению
    df["fx_greens_overdone"] = safe(df, "f_consecutive_greens") * (rsi / 100.0)
    new.append("fx_greens_overdone")

    # 8. FUNDING PRESSURE: высокий funding + перегрев = лонги перегружены = шорт ок
    df["fx_funding_overload"] = safe(df, "f_funding_rate") * 10000 * (rsi / 100.0)
    new.append("fx_funding_overload")

    # 9. OI SURGE: рост OI на пампе = свежие лонги, уязвимы для каскада ликвидаций
    df["fx_oi_surge_x_overheat"] = safe(df, "oi_change_pct_at_signal") * (rsi / 100.0)
    new.append("fx_oi_surge_x_overheat")

    # 10. LIQUIDATION SETUP: каскад ликвидаций + растянутость
    df["fx_liq_setup"] = safe(df, "f_liquidation_cascade") * safe(df, "f_vwap_extension")
    new.append("fx_liq_setup")

    # 11. РЕЖИМ-ВЗАИМОДЕЙСТВИЕ: перегрев монеты × состояние BTC
    #     (шорт перегретой монеты опаснее когда BTC сам растёт)
    df["fx_overheat_vs_btc"] = (rsi / 100.0) * safe(df, "btc_change_1h")
    df["fx_overheat_vs_btc24"] = (rsi / 100.0) * safe(df, "btc_change_24h")
    new += ["fx_overheat_vs_btc", "fx_overheat_vs_btc24"]

    # 12. VOLATILITY-ADJUSTED stretch: растянутость на высокой воле менее надёжна
    df["fx_stretch_per_vol"] = safe(df, "f_vwap_extension") / (safe(df, "realized_vol_1h") + 0.05)
    new.append("fx_stretch_per_vol")

    # заменить inf/nan
    for c in new:
        df[c] = df[c].replace([np.inf, -np.inf], np.nan)
    return new


PARAMS = dict(objective="binary", n_estimators=400, learning_rate=0.02,
              num_leaves=31, min_child_samples=60, subsample=0.8,
              colsample_bytree=0.8, reg_lambda=2.0, verbose=-1)


def purged_cv(df, feats, n_splits=5, gap_frac=0.02, return_oof=False):
    n = len(df); gap = max(1, int(n * gap_frac)); fold = n // (n_splits + 1)
    X = df[feats].values; y = df["_label"].values
    oof = np.full(n, np.nan); aucs = []
    for k in range(1, n_splits + 1):
        tr_end = fold * k; te_start = tr_end + gap; te_end = min(te_start + fold, n)
        if te_start >= n: break
        tr = np.arange(0, tr_end); te = np.arange(te_start, te_end)
        if len(te) < 30: continue
        m = lgb.LGBMClassifier(**PARAMS); m.fit(X[tr], y[tr])
        p = m.predict_proba(X[te])[:, 1]; oof[te] = p
        try:
            aucs.append(roc_auc_score(y[te], p))
        except ValueError: pass
    return (np.mean(aucs) if aucs else np.nan, np.std(aucs) if aucs else 0, oof) if return_oof \
        else (np.mean(aucs) if aucs else np.nan, np.std(aucs) if aucs else 0)


def wilson(w, n, z=1.96):
    if n == 0: return 0, 0, 0
    p = w/n; d = 1+z*z/n; c = (p+z*z/(2*n))/d
    h = z*np.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return p, max(0,c-h), min(1,c+h)


def thr_table(y, proba, pnl):
    mask = ~np.isnan(proba); y=np.asarray(y)[mask]; p=np.asarray(proba)[mask]; pnl=np.asarray(pnl)[mask]
    base = y.mean()
    print(f"\n  Без фильтра: n={len(y)}, WR={base:.1%}, pnl/сделку={pnl.mean()-0.6:+.2f}%")
    print(f"  {'порог':>6}{'n':>7}{'WR':>8}{'95%CI':>16}{'pnl/сделку':>12}")
    for thr in [0.50,0.55,0.60,0.65,0.70,0.75,0.80]:
        sel = p>=thr; n=int(sel.sum())
        if n<10: continue
        wr,lo,hi = wilson(int(y[sel].sum()),n)
        avg = pnl[sel].mean()-0.6  # минус комиссия на маржу
        flag = "  ✅" if (lo>0.526 and avg>0) else ""
        print(f"  {thr:>6.2f}{n:>7}{wr:>7.1%}[{lo:>5.1%},{hi:>5.1%}]{avg:>+11.2f}%{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", default=None)
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--gap", type=float, default=0.02)
    args = ap.parse_args()
    auto = args.auto or (sorted(glob.glob("auto_shorts_*.csv"), key=os.path.getmtime, reverse=True) or [None])[0]
    if not auto: print("нет CSV, укажи --auto"); sys.exit(1)
    print(f"auto_shorts: {auto}\n")

    df = pd.read_csv(auto)
    df = df[df["close_reason"].notna()].copy()
    df["_label"] = df["close_reason"].astype(str).str.startswith("tp").astype(int)
    df["_pnl"] = df["pnl_pct"].astype(float)
    df["_ts"] = pd.to_datetime(df["entry_ts"], errors="coerce", utc=True)
    df = df[df["_ts"].notna()].sort_values("_ts").reset_index(drop=True)

    base = [c for c in BASE_FEATS if c in df.columns and df[c].notna().sum()>0]
    exh = add_exhaustion_features(df)
    print(f"baseline фичей: {len(base)}, новых exhaustion: {len(exh)}")
    print(f"всего сделок: {len(df)}, WR={df['_label'].mean():.1%}\n")

    sets = {
        "A) baseline":        base,
        "B) +exhaustion":     base + exh,
        "C) exhaustion-only": exh,
    }
    print("="*64); print("ЧЕСТНЫЙ AUC (purge-gap CV)"); print("="*64)
    results = {}
    for name, feats in sets.items():
        auc, sd, oof = purged_cv(df, feats, args.splits, args.gap, return_oof=True)
        results[name] = (auc, sd, oof, feats)
        print(f"  {name:<20} AUC = {auc:.3f} ± {sd:.3f}  ({len(feats)} фичей)")

    # таблица порогов для лучшего набора
    best_name = max(results, key=lambda k: results[k][0])
    print(f"\nЛучший набор: {best_name}")
    print("="*64); print(f"ТАБЛИЦА ПОРОГОВ — {best_name}"); print("="*64)
    thr_table(df["_label"].values, results[best_name][2], df["_pnl"].values)

    # permutation importance новых фичей в наборе B
    print("\n"+"="*64); print("PERMUTATION IMPORTANCE (набор B, какие фичи реально несут сигнал)"); print("="*64)
    feats_b = sets["B) +exhaustion"]
    n=len(df); split=int(n*0.7)
    Xtr,Xte = df[feats_b].iloc[:split].values, df[feats_b].iloc[split:].values
    ytr,yte = df["_label"].iloc[:split].values, df["_label"].iloc[split:].values
    m=lgb.LGBMClassifier(**PARAMS); m.fit(Xtr,ytr)
    try:
        r = permutation_importance(m, Xte, yte, n_repeats=10, random_state=0, scoring="roc_auc")
        imp = sorted(zip(feats_b, r.importances_mean), key=lambda x:-x[1])
        print("  топ-20 по вкладу в AUC (out-of-sample):")
        for i,(f,v) in enumerate(imp[:20],1):
            tag = "  ★NEW" if f.startswith("fx_") else ""
            print(f"  {i:>2}. {f:<28} {v:>+.4f}{tag}")
        new_in_top = [f for f,_ in imp[:20] if f.startswith("fx_")]
        print(f"\n  Новых exhaustion-фичей в топ-20: {len(new_in_top)}")
    except Exception as e:
        print("  permutation_importance не удалось:", e)

    print("\n"+"="*64); print("ВЕРДИКТ"); print("="*64)
    a = results["A) baseline"][0]; b = results["B) +exhaustion"][0]
    print(f"  baseline AUC={a:.3f}  →  +exhaustion AUC={b:.3f}  (Δ={b-a:+.3f})")
    if b >= 0.53:
        print("  ✅ Сигнал есть. Направление верное — exhaustion-фичи помогают.")
        print("     Дальше: собирать эти фичи в боте, добавить funding/OI/тренд монеты.")
    elif b > a + 0.01:
        print("  ~ Слабое улучшение. Направление перспективное, но нужны доп. данные")
        print("     (funding, OI динамика, история тренда монеты, long/short ratio).")
    else:
        print("  ❌ Улучшения нет. В текущих данных нет сигнала о падении.")
        print("     Корень — в ТРИГГЕРЕ входа: он ловит не те ситуации.")
        print("     Нужно менять детектор (что считать сигналом), а не фильтр.")


if __name__ == "__main__":
    main()