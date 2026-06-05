#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ML FORWARD — учим модель отличать монеты, которые РЕАЛЬНО УПАДУТ, на ЧИСТОМ
forward-таргете вместо шумного TP/SL.

Почему canceled_signals, а не auto_shorts:
  - В canceled форвардные цены заполнены на 97-99% (price_15m/30m/60m,
    price_min_60m, price_max_60m). В auto_shorts они 17-58% (сделки закрывались
    рано), поэтому честный forward-таргет там не построить.
  - В canceled funding_rate_at_signal и oi_change_pct_at_signal — РЕАЛЬНЫЕ,
    вариативные (277 и 945 уникальных значений), а не константа 0.0001 как в auto.

Таргеты (для ШОРТА — выгодно падение):
  drop      : упала ли монета на >= THR% движения цены за 60м (по price_min_60m)
  drop_first: реальный исход — достигла ли цель падения (-THR%) РАНЬШЕ, чем
              стоп роста (+THR%). Сравнение price_min_60m vs price_max_60m
              взвешенное по тому, что для шорта стоп обычно бьёт первым при росте.
  net60     : чистое движение к 60й минуте было вниз (price_60m < signal_price)

Сравниваем 3 набора фичей честно (purge-gap CV + permutation importance):
  A) baseline       — сырые фичи (как сейчас в боте)
  B) +exhaustion    — baseline + композиты исчерпания
  C) +exh +funding/oi — добавляем РЕАЛЬНЫЕ опережающие funding/OI фичи

Имя монеты НЕ используется — модель должна обобщать на новые символы.

Запуск:
    python scripts/ml_forward.py --canceled canceled_signals_XXXX.csv --thr 1.0 --target drop
    python scripts/ml_forward.py --canceled canceled_signals_XXXX.csv --thr 1.0 --target drop_first

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

# переиспользуем готовые утилиты из v2 (тот же каталог)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ml_features_v2 import add_exhaustion_features, purged_cv, wilson, PARAMS  # noqa


# базовые сырые фичи, доступные в МОМЕНТ сигнала (без утечки будущего)
BASE_FEATS = [
    "f_rsi", "f_rsi_5m", "f_vwap_extension", "f_volume_zscore", "f_trade_imbalance",
    "f_large_buy_cluster", "f_large_sell_cluster", "f_price_acceleration",
    "f_consecutive_greens", "f_ob_bid_thinning", "f_spread_expansion",
    "f_momentum_loss", "f_upper_wick", "f_cvd_divergence", "f_liquidation_cascade",
    "realized_vol_1h", "realized_vol_1h_z", "volume_24h_usdt_z",
    "price_change_5m", "price_change_1h", "spread_pct", "spread_pct_z",
    "bid_depth_change_5m", "bid_depth_change_5m_z", "trend_strength_1h",
    "ob_bid_volume_top10", "ob_ask_volume_top10", "ob_imbalance_top10",
    "ob_spread_bps", "ob_bid_wall_size", "ob_ask_wall_size",
    "btc_change_15m", "btc_change_1h", "btc_change_4h", "btc_change_24h",
    "btc_adx_1h", "btc_atr_pct_1h", "recent_wr_20", "score",
    "max_rise_pct", "max_entry_drop_pct", "triggered_count",
]

# РЕАЛЬНЫЕ опережающие фичи (живые в canceled, не константы)
LEAD_FEATS = ["funding_rate_at_signal", "oi_change_pct_at_signal"]


def add_lead_features(df: pd.DataFrame) -> list[str]:
    """Производные от funding/OI — опережающие индикаторы перекоса рынка."""
    new = []
    fr = df["funding_rate_at_signal"] if "funding_rate_at_signal" in df.columns else pd.Series(0.0, index=df.index)
    oi = df["oi_change_pct_at_signal"] if "oi_change_pct_at_signal" in df.columns else pd.Series(0.0, index=df.index)
    rsi = df["f_rsi"] if "f_rsi" in df.columns else pd.Series(50.0, index=df.index)
    # funding в б.п. (масштаб)
    df["lx_funding_bps"] = fr * 10000
    # экстремальность funding (|отклонение| от нейтрали) — перегруженность одной стороны
    df["lx_funding_abs"] = (fr - 0.0001).abs() * 10000
    # положительный funding + перегрев = лонги перегружены, кандидат на падение
    df["lx_funding_x_overheat"] = (fr * 10000) * (rsi / 100.0)
    # рост OI + перегрев = свежие лонги на пампе = уязвимы для каскада
    df["lx_oi_surge"] = oi.clip(-20, 50)
    df["lx_oi_x_overheat"] = oi.clip(-20, 50) * (rsi / 100.0)
    # OI растёт но funding высокий = лонг-сквиз setup
    df["lx_oi_x_funding"] = oi.clip(-20, 50) * (fr * 10000)
    new += ["lx_funding_bps", "lx_funding_abs", "lx_funding_x_overheat",
            "lx_oi_surge", "lx_oi_x_overheat", "lx_oi_x_funding"]
    for c in new:
        df[c] = df[c].replace([np.inf, -np.inf], np.nan)
    return new


def build_target(df: pd.DataFrame, thr: float, kind: str):
    """Строит forward-таргет из реальных цен. thr — % движения цены (не P&L).

    Возвращает (label, pnl_pct_per_trade_with_leverage).
    Плечо 10x: движение цены × 10 = P&L. TP при падении thr%, SL при росте thr%.
    """
    sp = df["signal_price"].astype(float)
    pmin = df["price_min_60m"].astype(float)
    pmax = df["price_max_60m"].astype(float)
    p60 = df["price_60m"].astype(float)
    drop = (sp - pmin) / sp * 100.0           # макс падение, % движения
    rise = (pmax - sp) / sp * 100.0           # макс рост (риск против шорта)
    net60 = (sp - p60) / sp * 100.0           # чистое движение вниз к 60й мин

    if kind == "tp_first":
        # САМЫЙ ЧЕСТНЫЙ: готовая симуляция бота по реальной хронологии цен.
        # synthetic_close_reason: tp_hit=прибыль шорта, sl_hit/expired=убыток.
        # Это отвечает на вопрос 'достигнет ли TP РАНЬШЕ SL' по реальным тикам.
        reason = df["synthetic_close_reason"].astype(str) if "synthetic_close_reason" in df.columns else pd.Series("", index=df.index)
        label = (reason == "tp_hit").astype(int)
    elif kind == "drop":
        # упала ли минимум на thr% (достижимость цели падения)
        label = (drop >= thr).astype(int)
    elif kind == "net60":
        label = (net60 >= thr).astype(int)
    elif kind == "drop_first":
        # реалистичный исход для шорта с TP=-thr / SL=+thr.
        # допущение: если за 60м цель падения достигнута (drop>=thr) И рост не
        # превысил стоп (rise<thr) → точно TP. если оба достигнуты — неизвестно
        # что первым, считаем по знаку чистого движения к 60й мин (консервативно).
        hit_tp = drop >= thr
        hit_sl = rise >= thr
        label = np.where(
            hit_tp & ~hit_sl, 1,
            np.where(~hit_tp & hit_sl, 0,
                     np.where(hit_tp & hit_sl, (net60 > 0).astype(int),
                              (net60 > 0).astype(int)))
        ).astype(int)
    else:
        raise ValueError(kind)

    # P&L на сделку при плече 10x: TP=+thr*10%, SL=-thr*10%
    LEV = 10
    pnl = np.where(label == 1, thr * LEV, -thr * LEV).astype(float)
    return pd.Series(label, index=df.index), pd.Series(pnl, index=df.index), drop, rise


def eval_set(df, feats, splits, gap, name):
    feats = [c for c in feats if c in df.columns and df[c].notna().sum() > 0]
    auc, sd = purged_cv(df, feats, n_splits=splits, gap_frac=gap)
    print(f"  {name:24s} AUC = {auc:.3f} ± {sd:.3f}  ({len(feats)} фичей)")
    return auc, sd, feats


def thr_table_fwd(y, proba, pnl, fee=0.6):
    mask = ~np.isnan(proba)
    y = np.asarray(y)[mask]; p = np.asarray(proba)[mask]; pnl = np.asarray(pnl)[mask]
    base = y.mean()
    # порог безубытка при R:R 1:1 и комиссии: WR > (1+fee/(thr*lev))/2 ~ зависит,
    # покажем фактический pnl за вычетом комиссии
    print(f"\n  Без фильтра: n={len(y)}, доля_падений={base:.1%}, pnl/сделку={pnl.mean()-fee:+.2f}%")
    print(f"  {'порог':>6}{'n':>7}{'доля↓':>8}{'95%CI':>16}{'pnl/сделку':>12}")
    for thr in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
        sel = p >= thr; n = int(sel.sum())
        if n < 10: continue
        wr, lo, hi = wilson(int(y[sel].sum()), n)
        avg = pnl[sel].mean() - fee
        flag = "  ✅" if (lo > 0.5 and avg > 0) else ""
        print(f"  {thr:>6.2f}{n:>7}{wr:>7.1%}[{lo:>5.1%},{hi:>5.1%}]{avg:>+11.2f}%{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--canceled", default=None)
    ap.add_argument("--thr", type=float, default=1.0, help="порог движения цены %% (TP/SL)")
    ap.add_argument("--target", default="drop", choices=["drop", "drop_first", "net60", "tp_first"])
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--gap", type=float, default=0.02)
    args = ap.parse_args()

    canc = args.canceled or (sorted(glob.glob("canceled_signals_*.csv"),
                                     key=os.path.getmtime, reverse=True) or [None])[0]
    if not canc:
        print("нет canceled CSV, укажи --canceled"); sys.exit(1)
    print(f"canceled_signals: {canc}")
    print(f"таргет: {args.target}, порог движения: {args.thr}% (P&L при 10x = {args.thr*10:.0f}%)\n")

    df = pd.read_csv(canc)
    # только строки с валидным форвардом
    need = ["signal_price", "price_min_60m", "price_max_60m", "price_60m"]
    df = df[df[need].notna().all(axis=1) & (df["signal_price"] > 0)].copy()
    ts = pd.to_datetime(df["signal_ts"], errors="coerce", utc=True)
    df = df[ts.notna()].copy()
    df["_ts"] = ts[ts.notna()].values
    df = df.sort_values("_ts").reset_index(drop=True)

    # для tp_first учим только на сделках с известным исходом симуляции
    if args.target == "tp_first" and "synthetic_close_reason" in df.columns:
        df = df[df["synthetic_close_reason"].notna()].copy().reset_index(drop=True)

    label, pnl, drop, rise = build_target(df, args.thr, args.target)
    df["_label"] = label.values
    df["_pnl"] = pnl.values

    print(f"всего сигналов с форвардом: {len(df)}")
    print(f"доля 'упадёт' (таргет=1): {df['_label'].mean():.1%}")
    print(f"медианное падение={drop.median():.2f}%, медианный рост={rise.median():.2f}% "
          f"(рост>падение => база убыточна)\n")

    exh = add_exhaustion_features(df)
    lead = add_lead_features(df)

    print("=" * 64)
    print(f"ЧЕСТНЫЙ AUC (purge-gap CV) — таргет '{args.target}', порог {args.thr}%")
    print("=" * 64)
    a_auc, a_sd, _ = eval_set(df, BASE_FEATS, args.splits, args.gap, "A) baseline")
    b_auc, b_sd, _ = eval_set(df, BASE_FEATS + exh, args.splits, args.gap, "B) +exhaustion")
    c_feats = BASE_FEATS + exh + LEAD_FEATS + lead
    c_auc, c_sd, c_used = eval_set(df, c_feats, args.splits, args.gap, "C) +funding/OI")

    best_name, best_feats = max(
        [("A) baseline", [c for c in BASE_FEATS if c in df.columns]),
         ("B) +exhaustion", [c for c in BASE_FEATS + exh if c in df.columns]),
         ("C) +funding/OI", c_used)],
        key=lambda kv: purged_cv(df, [c for c in kv[1] if df[c].notna().sum() > 0],
                                  n_splits=args.splits, gap_frac=args.gap)[0])
    print(f"\nЛучший набор: {best_name}")

    # OOF proba лучшего набора для таблицы порогов
    used = [c for c in best_feats if c in df.columns and df[c].notna().sum() > 0]
    _, _, oof = purged_cv(df, used, n_splits=args.splits, gap_frac=args.gap, return_oof=True)
    print("=" * 64)
    print(f"ТАБЛИЦА ПОРОГОВ — {best_name}")
    print("=" * 64)
    thr_table_fwd(df["_label"].values, oof, df["_pnl"].values)

    # permutation importance на наборе C (видим вклад funding/OI)
    print("\n" + "=" * 64)
    print("PERMUTATION IMPORTANCE (набор C: что реально несёт сигнал)")
    print("=" * 64)
    used_c = [c for c in c_feats if c in df.columns and df[c].notna().sum() > 0]
    n = len(df); split = int(n * 0.7)
    Xtr, Xte = df[used_c].iloc[:split], df[used_c].iloc[split:]
    ytr, yte = df["_label"].iloc[:split], df["_label"].iloc[split:]
    m = lgb.LGBMClassifier(**PARAMS); m.fit(Xtr.values, ytr.values)
    r = permutation_importance(m, Xte.values, yte.values, scoring="roc_auc",
                               n_repeats=8, random_state=0)
    imp = sorted(zip(used_c, r.importances_mean), key=lambda x: -x[1])[:20]
    print("  топ-20 по вкладу в AUC (out-of-sample):")
    lead_in_top = 0
    for i, (f, v) in enumerate(imp, 1):
        tag = ""
        if f in (LEAD_FEATS + lead): tag = "  ★FUNDING/OI"; lead_in_top += 1
        elif f.startswith("fx_"): tag = "  ★EXH"
        print(f"  {i:2d}. {f:28s} {v:+.4f}{tag}")
    print(f"\n  Funding/OI фичей в топ-20: {lead_in_top}")

    # вердикт
    print("\n" + "=" * 64)
    print("ВЕРДИКТ")
    print("=" * 64)
    best_auc = max(a_auc, b_auc, c_auc)
    print(f"  baseline={a_auc:.3f}  +exhaustion={b_auc:.3f}  +funding/OI={c_auc:.3f}")
    print(f"  лучший honest AUC = {best_auc:.3f}")
    if best_auc >= 0.58:
        print("  ✅ СИЛЬНЫЙ сигнал. Forward-таргет работает — модель различает падения.")
        print("     Дальше: собирать эти фичи в боте в реальном времени + торговать по proba.")
    elif best_auc >= 0.54:
        print("  🟡 ЕСТЬ сигнал. Forward-таргет лучше TP/SL. Стоит:")
        print("     добавить funding/OI в сбор бота, накопить больше данных, дообучить.")
    elif best_auc >= 0.515:
        print("  🟠 СЛАБЫЙ сигнал. Лучше случайного, но мало для торговли.")
        print("     Нужны новые источники данных или пересмотр триггера.")
    else:
        print("  ❌ Сигнала нет даже на чистом forward-таргете.")
        print("     Проблема глубже фичей — в самом триггере входа (что считать сигналом).")


if __name__ == "__main__":
    main()