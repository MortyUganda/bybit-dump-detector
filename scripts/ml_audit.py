#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ML AUDIT — честная оценка decision-модели short-бота.

Что делает:
  1. Размечает ОБА источника (auto_shorts + canceled_signals) ЕДИНЫМ событийным
     таргетом (что задело раньше — TP или SL). Убирает разрыв WR между источниками,
     который раньше создавал утечку через is_canceled_source.
  2. Учит LightGBM БЕЗ утечки (is_canceled_source выкинут, forward/synthetic/exit
     колонки выкинуты).
  3. Валидация TimeSeriesSplit С PURGE-GAP (зазор между train/test), чтобы убрать
     утечку через recent-фичи (recent_wr_20, symbol_*).
  4. Таблица порогов proba с биномиальным доверительным интервалом (Wilson 95%),
     чтобы видеть, где WR статзначим, а где шум.
  5. Сравнение honest vs leaky (с is_canceled_source) — показать масштаб утечки.

Запуск:
    python scripts/ml_audit.py --auto auto_shorts_XXXX.csv --canceled canceled_signals_XXXX.csv
    python scripts/ml_audit.py            # автопоиск свежих CSV в текущей папке

Зависимости: pandas, numpy, scikit-learn, lightgbm
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

try:
    import lightgbm as lgb
except ImportError:
    print("ОШИБКА: нужен lightgbm. Установи: pip install lightgbm")
    sys.exit(1)
from sklearn.metrics import roc_auc_score

# ─────────────────────────────────────────────────────────────────────────────
# Колонки, которые НЕЛЬЗЯ давать модели (утечка / недоступны в момент решения)
# ─────────────────────────────────────────────────────────────────────────────
LEAK_COLS = {
    # идентификаторы / мета
    "id", "symbol", "signal_type", "ob_snapshot",
    # источник — ГЛАВНАЯ утечка
    "is_canceled_source",
    # таргет и всё, что вычислено ПОСЛЕ входа (будущее)
    "ml_label", "label", "target",
    "close_reason", "cancel_reason", "synthetic_close_reason",
    "status", "exit_price", "exit_ts", "pnl_pct",
    "synthetic_pnl_pct", "would_hit_tp", "would_hit_sl",
    "time_to_tp_sec", "time_to_sl_sec",
    "price_15m", "price_30m", "price_60m",
    "price_15m_ts", "price_30m_ts", "price_60m_ts",
    "price_min_60m", "price_max_60m",
    "final_price", "final_score", "price_change_pct",
    "adverse_move_pct",  # вычисляется по движению ПОСЛЕ сигнала
    # цены/время входа — не фичи рынка
    "signal_price", "entry_price", "tp_price", "sl_price",
    "entry_ts", "signal_ts", "decision_ts", "exit_ts",
    "entry_delay_sec", "decision_delay_sec",
    "tp_pct", "sl_pct", "leverage",
    # прод-proba (пустая в наших данных и в любом случае это выход старой модели)
    "ml_proba_at_signal",
    # параметры мониторинга canceled (специфичны для отменённых — утечка источника)
    "monitor_attempts", "monitor_interval_sec", "stabilization_threshold_pct",
    "max_rise_pct", "max_entry_drop_pct", "entry_mode_candidate",
    "min_score_at_entry", "entry_score", "entry_mode", "triggered_count",
}


def wilson_ci(wins: int, n: int, z: float = 1.96):
    """Доверительный интервал Уилсона для доли (95% по умолчанию)."""
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def label_auto(df: pd.DataFrame) -> pd.Series:
    """Таргет для auto_shorts: реальный исход. tp_hit=1, sl_hit=0."""
    cr = df["close_reason"].astype(str)
    lab = pd.Series(np.nan, index=df.index)
    lab[cr.str.startswith("tp")] = 1.0
    lab[cr.str.startswith("sl")] = 0.0
    return lab


def label_canceled(df: pd.DataFrame, expired_policy: str = "drop") -> pd.Series:
    """Событийный таргет для canceled: что задело РАНЬШЕ (synthetic_close_reason).

    tp_hit=1, sl_hit=0. expired_60m:
      - 'drop'  -> NaN (исключаем неоднозначные)
      - 'sign'  -> по знаку synthetic_pnl_pct (>0 win, <=0 loss)
    """
    scr = df["synthetic_close_reason"].astype(str)
    lab = pd.Series(np.nan, index=df.index)
    lab[scr.str.startswith("tp")] = 1.0
    lab[scr.str.startswith("sl")] = 0.0
    if expired_policy == "sign" and "synthetic_pnl_pct" in df.columns:
        mask = scr.str.startswith("expired")
        lab[mask] = (df.loc[mask, "synthetic_pnl_pct"] > 0).astype(float)
    return lab


def build_dataset(auto_path: str, canceled_path: str, expired_policy: str = "drop"):
    A = pd.read_csv(auto_path)
    C = pd.read_csv(canceled_path)

    # единый timestamp для сортировки/purge
    A["_ts"] = pd.to_datetime(A["entry_ts"], errors="coerce", utc=True)
    C["_ts"] = pd.to_datetime(C["signal_ts"], errors="coerce", utc=True)

    A["_label"] = label_auto(A)
    C["_label"] = label_canceled(C, expired_policy)
    A["is_canceled_source"] = 0
    C["is_canceled_source"] = 1

    A = A[A["_label"].notna()].copy()
    C = C[C["_label"].notna()].copy()

    common = sorted(set(A.columns) & set(C.columns))
    A = A[common]
    C = C[common]
    df = pd.concat([A, C], ignore_index=True)
    df = df[df["_ts"].notna()].sort_values("_ts").reset_index(drop=True)
    return df, A, C


def select_features(df: pd.DataFrame, include_leak: bool = False):
    feats = []
    for c in df.columns:
        if c in ("_label", "_ts"):
            continue
        if c in LEAK_COLS and not (include_leak and c == "is_canceled_source"):
            continue
        if not pd.api.types.is_numeric_dtype(df[c]):
            continue
        if df[c].notna().sum() == 0:
            continue
        feats.append(c)
    return feats


def purged_ts_cv(df, feats, n_splits=5, gap_frac=0.02):
    """TimeSeriesSplit с зазором (purge): между train и test выкидываем gap строк."""
    n = len(df)
    gap = max(1, int(n * gap_frac))
    fold_size = n // (n_splits + 1)
    X = df[feats].values
    y = df["_label"].values
    oof_proba = np.full(n, np.nan)
    aucs = []
    params = dict(
        objective="binary", n_estimators=300, learning_rate=0.03,
        num_leaves=31, min_child_samples=40, subsample=0.8,
        colsample_bytree=0.8, reg_lambda=1.0, verbose=-1,
    )
    for k in range(1, n_splits + 1):
        tr_end = fold_size * k
        te_start = tr_end + gap
        te_end = te_start + fold_size
        if te_start >= n:
            break
        te_end = min(te_end, n)
        tr_idx = np.arange(0, tr_end)
        te_idx = np.arange(te_start, te_end)
        if len(te_idx) < 30:
            continue
        m = lgb.LGBMClassifier(**params)
        m.fit(X[tr_idx], y[tr_idx])
        p = m.predict_proba(X[te_idx])[:, 1]
        oof_proba[te_idx] = p
        try:
            auc = roc_auc_score(y[te_idx], p)
            aucs.append(auc)
            print(f"  Fold {k}: train={len(tr_idx)}, gap={gap}, test={len(te_idx)}, AUC={auc:.3f}")
        except ValueError:
            print(f"  Fold {k}: один класс в test — пропуск")
    return oof_proba, aucs


def threshold_table(y, proba):
    mask = ~np.isnan(proba)
    y = np.asarray(y)[mask]
    p = np.asarray(proba)[mask]
    n_all = len(y)
    wr_all = y.mean() if n_all else 0
    print(f"\nБез фильтра: n={n_all}, WR={wr_all:.1%}")
    print(f"{'порог':>6} {'n':>6} {'доля':>6} {'WR':>7} {'95% CI':>16} {'Δ':>7}")
    for thr in [0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85]:
        sel = p >= thr
        n = int(sel.sum())
        if n == 0:
            print(f"{thr:>6.2f} {0:>6} {'—':>6} {'—':>7} {'—':>16} {'—':>7}")
            continue
        wins = int(y[sel].sum())
        wr, lo, hi = wilson_ci(wins, n)
        frac = n / n_all
        delta = wr - wr_all
        flag = "" if lo > 0.5 else "  (CI пересекает 50%)"
        print(f"{thr:>6.2f} {n:>6} {frac:>5.1%} {wr:>6.1%} [{lo:>5.1%},{hi:>5.1%}] {delta:>+6.1%}{flag}")


def importances(df, feats):
    X = df[feats].values
    y = df["_label"].values
    m = lgb.LGBMClassifier(
        objective="binary", n_estimators=300, learning_rate=0.03,
        num_leaves=31, min_child_samples=40, verbose=-1,
    )
    m.fit(X, y)
    imp = sorted(zip(feats, m.feature_importances_), key=lambda x: -x[1])
    return imp


def find_csv(pattern):
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return files[0] if files else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", default=None)
    ap.add_argument("--canceled", default=None)
    ap.add_argument("--expired", choices=["drop", "sign"], default="drop",
                    help="как трактовать expired_60m в canceled")
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--gap", type=float, default=0.02, help="доля строк в purge-зазоре")
    args = ap.parse_args()

    auto = args.auto or find_csv("auto_shorts_*.csv") or find_csv("**/auto_shorts_*.csv")
    canceled = args.canceled or find_csv("canceled_signals_*.csv") or find_csv("**/canceled_signals_*.csv")
    if not auto or not canceled:
        print("Не найдены CSV. Укажи --auto и --canceled.")
        sys.exit(1)
    print(f"auto_shorts:      {auto}")
    print(f"canceled_signals: {canceled}")
    print(f"expired policy:   {args.expired}\n")

    df, A, C = build_dataset(auto, canceled, args.expired)

    # ── 1. Проверка разрыва WR между источниками (единый таргет) ──
    print("=" * 70)
    print("1) ЧЕСТНЫЙ WR ПО ИСТОЧНИКАМ (единый событийный таргет)")
    print("=" * 70)
    for name, sub in [("auto_short", df[df.is_canceled_source == 0]),
                      ("canceled  ", df[df.is_canceled_source == 1])]:
        n = len(sub)
        wr, lo, hi = wilson_ci(int(sub._label.sum()), n)
        print(f"  {name}: n={n:>6}, WR={wr:.1%}  95% CI [{lo:.1%}, {hi:.1%}]")
    wr_all, lo, hi = wilson_ci(int(df._label.sum()), len(df))
    print(f"  ВСЕГО:      n={len(df):>6}, WR={wr_all:.1%}  95% CI [{lo:.1%}, {hi:.1%}]")
    print("  → если CI источников ПЕРЕСЕКАЮТСЯ — разрыва нет, утечки источника нет.")

    feats = select_features(df, include_leak=False)
    print(f"\nФичей (без утечки): {len(feats)}")

    # ── 2. Честная валидация ──
    print("\n" + "=" * 70)
    print("2) HONEST AUC (без is_canceled_source, TimeSeriesSplit + purge gap)")
    print("=" * 70)
    oof, aucs = purged_ts_cv(df, feats, args.splits, args.gap)
    if aucs:
        print(f"\nСредний honest AUC: {np.mean(aucs):.3f} ± {np.std(aucs):.3f}")

    # ── 3. Таблица порогов с CI ──
    print("\n" + "=" * 70)
    print("3) ТАБЛИЦА ПОРОГОВ proba (OOF, с доверительным интервалом Уилсона)")
    print("=" * 70)
    threshold_table(df["_label"].values, oof)

    # ── 4. Сравнение с утечкой ──
    print("\n" + "=" * 70)
    print("4) LEAKY AUC (С is_canceled_source) — масштаб утечки")
    print("=" * 70)
    feats_leak = select_features(df, include_leak=True)
    _, aucs_leak = purged_ts_cv(df, feats_leak, args.splits, args.gap)
    if aucs_leak and aucs:
        print(f"\nLeaky AUC:  {np.mean(aucs_leak):.3f}   "
              f"Honest AUC: {np.mean(aucs):.3f}   "
              f"Разница от утечки: {np.mean(aucs_leak)-np.mean(aucs):+.3f}")

    # ── 5. Importances honest ──
    print("\n" + "=" * 70)
    print("5) ВАЖНОСТЬ ФИЧЕЙ (honest модель, топ-20)")
    print("=" * 70)
    for i, (f, v) in enumerate(importances(df, feats)[:20], 1):
        print(f"  {i:>2}. {f:<32} {v:>8.0f}")

    print("\nГОТОВО. Ключевые выводы смотри в блоках 1 (разрыв), 2 (honest AUC), 3 (CI порогов).")


if __name__ == "__main__":
    main()