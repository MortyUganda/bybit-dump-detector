"""
v2: эксперименты с чистотой target, feature selection, sliding window.
Сравнивает с baseline (текущий train_decision_model.py).
НЕ сохраняет модель — только отчёт. Сохранение делает train_decision_model.py.

Эксперименты:
  EXP 0 — Baseline: объединённый датасет, все фичи (как в train_decision_model.py)
  EXP 1 — Чистый target: только auto_shorts, status=closed, close_reason ∈ {tp_hit, sl_hit}
  EXP 2 — EXP1 + feature selection (top-K по importance)
  EXP 3 — EXP2 + sliding window (последние N дней)

CLI: --auto-csv X --canceled-csv Y --all-opened-csv Z
     --window-days N (default 14)
     --top-features K (default 20)
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit

# === Настройки ===
DEFAULT_N_SPLITS = 5
RANDOM_STATE = 42
TARGET = "label"

COMMON_FEATURES = [
    "score",
    "f_rsi", "f_rsi_5m", "f_vwap_extension", "f_volume_zscore",
    "f_trade_imbalance", "f_large_buy_cluster", "f_large_sell_cluster",
    "f_price_acceleration", "f_consecutive_greens", "f_ob_bid_thinning",
    "f_spread_expansion", "f_momentum_loss", "f_upper_wick", "f_funding_rate",
    "f_cvd_divergence", "f_liquidation_cascade",
    "realized_vol_1h", "volume_24h_usdt",
    "price_change_5m", "price_change_1h", "spread_pct",
    "bid_depth_change_5m", "btc_change_15m",
    "funding_rate_at_signal", "oi_change_pct_at_signal", "trend_strength_1h",
    # OB features
    "ob_bid_volume_top10", "ob_ask_volume_top10",
    "ob_imbalance_top10", "ob_spread_bps",
    "ob_bid_wall_price", "ob_bid_wall_size",
    "ob_ask_wall_price", "ob_ask_wall_size",
    # Z-score нормализация
    "spread_pct_z", "bid_depth_change_5m_z", "realized_vol_1h_z",
    "volume_24h_usdt_z", "oi_change_pct_z",
    # Режимные BTC-фичи
    "btc_change_1h", "btc_change_4h", "btc_change_24h",
    "btc_adx_1h", "btc_atr_pct_1h",
    # Контекст
    "recent_wr_20",
    # Adverse move
    "adverse_move_pct",
]

LGB_PARAMS = {
    "objective": "binary",
    "metric": "auc",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 10,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "verbose": -1,
    "seed": RANDOM_STATE,
}


# ── Утилиты ──────────────────────────────────────────────────────


def find_latest_csv(pattern: str) -> str | None:
    files = glob.glob(pattern)
    if not files:
        return None
    return max(files, key=lambda p: Path(p).stat().st_mtime)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Decision ML v2: эксперименты с target/features/window"
    )
    p.add_argument("--auto-csv", default=None,
                   help="Путь к auto_shorts CSV (default: последний auto_shorts_*.csv)")
    p.add_argument("--canceled-csv", default=None,
                   help="Путь к canceled_signals CSV (default: последний canceled_signals_*.csv)")
    p.add_argument("--all-opened-csv", default=None,
                   help="Путь к all_opened_signals CSV (default: последний all_opened_signals_*.csv)")
    p.add_argument("--splits", type=int, default=DEFAULT_N_SPLITS,
                   help=f"Число фолдов TimeSeriesSplit (default {DEFAULT_N_SPLITS})")
    p.add_argument("--top-features", type=int, default=20,
                   help="Число топ-фичей для EXP 2/3 (default 20)")
    p.add_argument("--window-days", type=int, default=14,
                   help="Окно в днях для EXP 3 (default 14)")
    return p.parse_args()


# ── Загрузка данных ──────────────────────────────────────────────


def load_auto_shorts(path: str) -> pd.DataFrame:
    """auto_shorts — закрытые с ml_label."""
    df = pd.read_csv(path)
    df = df[df["status"] == "closed"].copy()
    df = df[df["ml_label"].notna()].copy()
    df["signal_ts"] = pd.to_datetime(df["entry_ts"])
    df[TARGET] = df["ml_label"].astype(int)
    df["source"] = "auto_short"
    return df


def load_canceled(path: str) -> pd.DataFrame:
    """canceled_signals — синтетический исход."""
    df = pd.read_csv(path)
    df["signal_ts"] = pd.to_datetime(df["signal_ts"])
    tp = df["would_hit_tp"] == True   # noqa: E712
    sl = df["would_hit_sl"] == True   # noqa: E712
    out = df[tp | sl].copy()
    out[TARGET] = tp[tp | sl].astype(int).values
    out["source"] = "canceled"
    return out


def load_all_opened(path: str) -> pd.DataFrame:
    """all_opened_signals — shadow-paper, dedup."""
    df = pd.read_csv(path)
    df = df[df["status"] == "closed"].copy()
    if "linked_auto_short_id" in df.columns:
        df = df[df["linked_auto_short_id"].isna()].copy()
    if "linked_canceled_signal_id" in df.columns:
        df = df[df["linked_canceled_signal_id"].isna()].copy()
    mask_tp = df["close_reason"] == "tp_hit"
    mask_sl = df["close_reason"] == "sl_hit"
    df = df[mask_tp | mask_sl].copy()
    df[TARGET] = mask_tp[mask_tp | mask_sl].astype(int).values
    df["signal_ts"] = pd.to_datetime(df["entry_ts"])
    df["source"] = "all_opened"
    return df


def load_auto_clean(path: str) -> pd.DataFrame:
    """auto_shorts с чистым target: status=closed, close_reason ∈ {tp_hit, sl_hit}."""
    df = pd.read_csv(path)
    df = df[df["status"] == "closed"].copy()
    df = df[df["close_reason"].isin(["tp_hit", "sl_hit"])].copy()
    df["signal_ts"] = pd.to_datetime(df["entry_ts"])
    df[TARGET] = (df["close_reason"] == "tp_hit").astype(int)
    df["source"] = "auto_short_clean"
    return df


# ── Подготовка датасетов ─────────────────────────────────────────


def intersect_features(dfs: list[pd.DataFrame]) -> list[str]:
    return [c for c in COMMON_FEATURES if all(c in d.columns for d in dfs)]


def merge_baseline(df_auto: pd.DataFrame, df_canceled: pd.DataFrame,
                   df_all_opened: pd.DataFrame | None) -> tuple[pd.DataFrame, list[str]]:
    """Baseline-датасет: объединение всех источников (как в v1)."""
    all_dfs = [df_auto, df_canceled]
    if df_all_opened is not None and len(df_all_opened) > 0:
        all_dfs.append(df_all_opened)
    feature_cols = intersect_features(all_dfs)
    cols = feature_cols + ["signal_ts", TARGET, "source"]
    df = pd.concat([d[cols] for d in all_dfs], ignore_index=True)
    df = df.sort_values("signal_ts").reset_index(drop=True)
    df[feature_cols] = df[feature_cols].fillna(0.0)
    return df, feature_cols


def prepare_clean(df_clean: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Датасет только из чистых auto_shorts."""
    feature_cols = [c for c in COMMON_FEATURES if c in df_clean.columns]
    cols = feature_cols + ["signal_ts", TARGET, "source"]
    df = df_clean[cols].copy().sort_values("signal_ts").reset_index(drop=True)
    df[feature_cols] = df[feature_cols].fillna(0.0)
    return df, feature_cols


# ── Кросс-валидация ──────────────────────────────────────────────


def cross_val_auc(X: pd.DataFrame, y: pd.Series, n_splits: int) -> dict:
    if len(X) < n_splits * 2:
        n_splits = max(2, len(X) // 5)

    tscv = TimeSeriesSplit(n_splits=n_splits)
    fold_aucs: list[float] = []
    oof_proba = np.zeros(len(X))
    oof_mask = np.zeros(len(X), dtype=bool)

    for fold, (tr, te) in enumerate(tscv.split(X), 1):
        X_tr, X_te = X.iloc[tr], X.iloc[te]
        y_tr, y_te = y.iloc[tr], y.iloc[te]

        if y_tr.nunique() < 2 or y_te.nunique() < 2:
            continue

        model = lgb.train(
            LGB_PARAMS,
            lgb.Dataset(X_tr, label=y_tr),
            num_boost_round=300,
            valid_sets=[lgb.Dataset(X_te, label=y_te)],
            callbacks=[lgb.early_stopping(30, verbose=False)],
        )

        proba = model.predict(X_te)
        auc = roc_auc_score(y_te, proba)
        fold_aucs.append(auc)
        oof_proba[te] = proba
        oof_mask[te] = True

    return {
        "fold_aucs": fold_aucs,
        "mean_auc": float(np.mean(fold_aucs)) if fold_aucs else float("nan"),
        "std_auc": float(np.std(fold_aucs)) if fold_aucs else float("nan"),
        "oof_proba": oof_proba,
        "oof_mask": oof_mask,
    }


def get_feature_importance(X: pd.DataFrame, y: pd.Series) -> list[tuple[str, float]]:
    """Обучить модель на всех данных → вернуть importance (gain)."""
    model = lgb.train(LGB_PARAMS, lgb.Dataset(X, label=y), num_boost_round=300)
    imp = sorted(
        zip(X.columns, model.feature_importance(importance_type="gain")),
        key=lambda x: -x[1],
    )
    return imp


# ── Один эксперимент (для параллельного запуска) ─────────────────


def run_experiment(name: str, X: pd.DataFrame, y: pd.Series,
                   feature_cols: list[str], n_splits: int) -> dict:
    """Запускает кросс-валидацию + importance + threshold analysis."""
    res = cross_val_auc(X, y, n_splits)
    imp = get_feature_importance(X, y)

    # threshold analysis
    sub_mask = res["oof_mask"]
    thresholds = {}
    if sub_mask.any():
        oof_y = y.loc[sub_mask].values
        oof_p = res["oof_proba"][sub_mask]
        base_wr = float(oof_y.mean())
        thresholds["base_wr"] = base_wr
        thresholds["base_n"] = int(sub_mask.sum())
        for thr in [0.50, 0.55, 0.60, 0.65, 0.70]:
            mask_thr = oof_p >= thr
            if mask_thr.sum() == 0:
                thresholds[thr] = None
            else:
                wr = float(oof_y[mask_thr].mean())
                thresholds[thr] = {
                    "n": int(mask_thr.sum()),
                    "wr": wr,
                    "delta": wr - base_wr,
                    "kept_pct": float(mask_thr.sum() / len(oof_y) * 100),
                }

    return {
        "name": name,
        "n": len(X),
        "features": list(feature_cols),
        "n_features": len(feature_cols),
        "mean_auc": res["mean_auc"],
        "std_auc": res["std_auc"],
        "fold_aucs": sorted(res["fold_aucs"]),
        "importance_top10": imp[:10],
        "thresholds": thresholds,
    }


# ── Вывод результатов ────────────────────────────────────────────


def print_experiment_detail(exp: dict) -> None:
    name = exp["name"]
    print(f"\n{'─' * 60}")
    print(f"  {name}")
    print(f"  n={exp['n']}, фичей={exp['n_features']}, "
          f"AUC={exp['mean_auc']:.3f} ± {exp['std_auc']:.3f}")
    print(f"  folds: {[f'{a:.3f}' for a in exp['fold_aucs']]}")

    print("\n  Топ-10 feature importance:")
    for feat_name, val in exp["importance_top10"]:
        print(f"    {feat_name:30s} {val:.1f}")

    thr = exp["thresholds"]
    if thr:
        print("\n  ML-фильтр на OOF:")
        print(f"    Без фильтра: n={thr['base_n']}, WR={thr['base_wr']:.1%}")
        for t in [0.50, 0.55, 0.60, 0.65, 0.70]:
            info = thr.get(t)
            if info is None:
                print(f"    proba>={t:.2f}: нет сигналов")
            else:
                print(f"    proba>={t:.2f}: n={info['n']:4d} ({info['kept_pct']:5.1f}%), "
                      f"WR={info['wr']:.1%} (Δ={info['delta']:+.1%})")


def print_comparison(results: list[dict]) -> None:
    print(f"\n{'=' * 70}")
    print("=== Сравнение экспериментов ===")
    print(f"{'=' * 70}")
    header = f"{'EXP':<30s} {'n':>6s}   {'mean_AUC':>8s}  {'std':>6s}   folds (sorted)"
    print(header)
    print("─" * 90)
    for r in results:
        folds_str = "[" + ", ".join(f"{a:.3f}" for a in r["fold_aucs"]) + "]"
        print(f"{r['name']:<30s} {r['n']:>6d}   {r['mean_auc']:>8.3f}  "
              f"{r['std_auc']:>6.3f}   {folds_str}")

    # Лучший
    valid = [r for r in results if not np.isnan(r["mean_auc"])]
    if not valid:
        print("\nНет валидных результатов.")
        return

    best = max(valid, key=lambda r: r["mean_auc"])
    baseline = results[0]

    delta = best["mean_auc"] - baseline["mean_auc"]
    print(f"\n✅ Лучший: {best['name']} с AUC={best['mean_auc']:.3f} ± {best['std_auc']:.3f}")
    print(f"🎯 Δ к baseline: {delta:+.3f}")

    # Рекомендация
    print("\n📋 Рекомендация:")
    if best["name"] == baseline["name"]:
        print("  - Baseline остаётся лучшим → изменений не требуется")
    elif "EXP 3" in best["name"] and delta > 0.03:
        print(f"  - EXP 3 лучший с разрывом {delta:+.3f} > 0.03 → перейти на этот config в проде")
    elif "EXP 1" in best["name"]:
        print("  - EXP 1 лучший → достаточно почистить target в основном скрипте")
    elif abs(delta) < 0.02:
        print(f"  - Разница {abs(delta):.3f} < 0.02 → оставить baseline")
    else:
        print(f"  - {best['name']} лучше baseline на {delta:+.3f} → рассмотреть миграцию config в прод")


# ── Main ─────────────────────────────────────────────────────────


def main() -> None:
    args = parse_args()

    auto_csv = args.auto_csv or find_latest_csv("auto_shorts_*.csv")
    canceled_csv = args.canceled_csv or find_latest_csv("canceled_signals_*.csv")
    all_opened_csv = args.all_opened_csv or find_latest_csv("all_opened_signals_*.csv")

    if not auto_csv or not Path(auto_csv).exists():
        print("Не найден auto_shorts CSV. Укажи через --auto-csv")
        sys.exit(1)
    if not canceled_csv or not Path(canceled_csv).exists():
        print("Не найден canceled_signals CSV. Укажи через --canceled-csv")
        sys.exit(1)

    print(f"auto_shorts CSV:          {auto_csv}")
    print(f"canceled_signals CSV:     {canceled_csv}")

    # ── Загрузка ──
    df_auto = load_auto_shorts(auto_csv)
    df_canceled = load_canceled(canceled_csv)

    df_all_opened = None
    if all_opened_csv and Path(all_opened_csv).exists():
        print(f"all_opened_signals CSV:   {all_opened_csv}")
        df_all_opened = load_all_opened(all_opened_csv)
    else:
        print("all_opened_signals CSV:   не найден — пропускаем")

    df_clean = load_auto_clean(auto_csv)

    # ── Подготовка датасетов ──
    # EXP 0 — baseline
    df_base, feat_base = merge_baseline(df_auto, df_canceled, df_all_opened)

    # EXP 1 — чистый target
    df_c1, feat_c1 = prepare_clean(df_clean)

    if len(df_c1) < 30:
        print(f"WARN: чистый auto_shorts содержит {len(df_c1)} строк — EXP 1/2/3 могут быть нестабильны")

    n_splits = args.splits
    top_k = args.top_features
    window_days = args.window_days

    print(f"\nПараметры: splits={n_splits}, top_features={top_k}, window_days={window_days}")
    print(f"Baseline: n={len(df_base)}, фичей={len(feat_base)}")
    print(f"Clean target: n={len(df_c1)}, фичей={len(feat_c1)}")

    # ── EXP 0: Baseline ──
    print(f"\n{'=' * 60}")
    print("Запуск EXP 0 (baseline)...")
    exp0 = run_experiment(
        "EXP 0 (baseline)",
        df_base[feat_base], df_base[TARGET].astype(int),
        feat_base, n_splits,
    )

    # ── EXP 1: Чистый target ──
    print("Запуск EXP 1 (чистый target)...")
    exp1 = run_experiment(
        "EXP 1 (чистый target)",
        df_c1[feat_c1], df_c1[TARGET].astype(int),
        feat_c1, n_splits,
    )

    # ── EXP 2: feature selection ──
    # Сначала получаем importance на данных EXP 1
    print("Запуск EXP 2 (feature selection)...")
    imp_all = get_feature_importance(df_c1[feat_c1], df_c1[TARGET].astype(int))
    top_features = [name for name, _ in imp_all[:top_k]]
    # Отсечь фичи с нулевой важностью
    top_features = [name for name, val in imp_all[:top_k] if val > 0]
    if len(top_features) < 5:
        top_features = [name for name, _ in imp_all[:5]]
    print(f"  top-{top_k} фичей (отобрано {len(top_features)}): {top_features[:5]}...")

    df_c2, _ = prepare_clean(df_clean)
    exp2 = run_experiment(
        f"EXP 2 (+top-{len(top_features)} фичей)",
        df_c2[top_features], df_c2[TARGET].astype(int),
        top_features, n_splits,
    )

    # ── EXP 3: sliding window ──
    print(f"Запуск EXP 3 (sliding window {window_days}d)...")
    df_c3, _ = prepare_clean(df_clean)
    max_ts = df_c3["signal_ts"].max()
    cutoff = max_ts - pd.Timedelta(days=window_days)
    df_c3 = df_c3[df_c3["signal_ts"] > cutoff].reset_index(drop=True)

    if len(df_c3) < 10:
        print(f"  WARN: после window-фильтра осталось {len(df_c3)} строк")
        exp3 = {
            "name": f"EXP 3 (+{window_days}d window)",
            "n": len(df_c3),
            "features": top_features,
            "n_features": len(top_features),
            "mean_auc": float("nan"),
            "std_auc": float("nan"),
            "fold_aucs": [],
            "importance_top10": [],
            "thresholds": {},
        }
    else:
        exp3 = run_experiment(
            f"EXP 3 (+{window_days}d window)",
            df_c3[top_features], df_c3[TARGET].astype(int),
            top_features, n_splits,
        )

    # ── Вывод деталей ──
    results = [exp0, exp1, exp2, exp3]
    for r in results:
        print_experiment_detail(r)

    # ── Сравнительная таблица ──
    print_comparison(results)

    print(f"\n{'=' * 60}")
    print("Готово. Модель НЕ сохранена — это экспериментальный отчёт.")
    print("Для сохранения модели используйте train_decision_model.py")


if __name__ == "__main__":
    main()
