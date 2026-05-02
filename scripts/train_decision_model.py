"""
Decision Model: вход vs отмена сигнала.
Positive class — auto_shorts (вошли).
Negative class — canceled_signals (отменили).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, precision_recall_curve
from sklearn.model_selection import TimeSeriesSplit

# === Настройки ===
AUTO_SHORTS_CSV = "auto_shorts_20260501_054403-2.csv"
CANCELED_CSV = "canceled_signals_20260501_054403.csv"
N_SPLITS = 5
RANDOM_STATE = 42

# Общие фичи, которые есть в обеих таблицах в момент сигнала
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
    "ob_bid_volume_top10", "ob_ask_volume_top10",
    "ob_imbalance_top10", "ob_spread_bps",
]

TARGET = "entered"


def load_entered(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    print(f"auto_shorts: {len(df)} строк")

    # время сигнала — в auto_shorts его нет, берём entry_ts (момент сделки)
    df["signal_ts"] = pd.to_datetime(df["entry_ts"])
    df[TARGET] = 1

    # выравниваем имена со staring score
    if "entry_score" in df.columns and "score" in df.columns:
        # используем score как общий
        pass

    return df


def load_canceled(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    print(f"canceled_signals: {len(df)} строк")

    df["signal_ts"] = pd.to_datetime(df["signal_ts"])
    df[TARGET] = 0

    return df


def merge_datasets(df_entered: pd.DataFrame, df_canceled: pd.DataFrame) -> pd.DataFrame:
    feature_cols = [c for c in COMMON_FEATURES
                    if c in df_entered.columns and c in df_canceled.columns]

    print(f"\nОбщих фичей: {len(feature_cols)}")

    cols = feature_cols + ["signal_ts", TARGET]
    df_e = df_entered[cols].copy()
    df_c = df_canceled[cols].copy()

    df = pd.concat([df_e, df_c], ignore_index=True)
    df = df.sort_values("signal_ts").reset_index(drop=True)

    print(f"Объединённый датасет: {len(df)} строк")
    print(f"  entered=1: {int(df[TARGET].sum())}")
    print(f"  canceled=0: {int((df[TARGET] == 0).sum())}")

    # NaN -> 0 (некритичные фичи)
    df[feature_cols] = df[feature_cols].fillna(0.0)
    return df, feature_cols


def cross_val_auc(X: pd.DataFrame, y: pd.Series, n_splits: int) -> dict:
    if len(X) < n_splits * 2:
        n_splits = max(2, len(X) // 5)
        print(f"Слишком мало данных, n_splits={n_splits}")

    tscv = TimeSeriesSplit(n_splits=n_splits)
    fold_aucs = []

    for fold, (tr, te) in enumerate(tscv.split(X), 1):
        X_tr, X_te = X.iloc[tr], X.iloc[te]
        y_tr, y_te = y.iloc[tr], y.iloc[te]

        if y_tr.nunique() < 2 or y_te.nunique() < 2:
            print(f"Fold {fold}: пропуск (один класс)")
            continue

        params = {
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
            "is_unbalance": True,
        }

        model = lgb.train(
            params,
            lgb.Dataset(X_tr, label=y_tr),
            num_boost_round=300,
            valid_sets=[lgb.Dataset(X_te, label=y_te)],
            callbacks=[lgb.early_stopping(30, verbose=False)],
        )

        proba = model.predict(X_te)
        auc = roc_auc_score(y_te, proba)
        fold_aucs.append(auc)
        print(f"Fold {fold}: train={len(X_tr)}, test={len(X_te)}, AUC={auc:.3f}")

    return {
        "fold_aucs": fold_aucs,
        "mean_auc": float(np.mean(fold_aucs)) if fold_aucs else float("nan"),
        "std_auc": float(np.std(fold_aucs)) if fold_aucs else float("nan"),
    }


def feature_importance(X: pd.DataFrame, y: pd.Series, n_top: int = 20) -> None:
    print("\n=== Важности фичей (decision model) ===")
    params = {
        "objective": "binary", "metric": "auc",
        "learning_rate": 0.05, "num_leaves": 31,
        "min_data_in_leaf": 10, "verbose": -1, "seed": RANDOM_STATE,
        "is_unbalance": True,
    }
    model = lgb.train(params, lgb.Dataset(X, label=y), num_boost_round=300)
    imp = sorted(
        zip(X.columns, model.feature_importance(importance_type="gain")),
        key=lambda x: -x[1],
    )
    for name, val in imp[:n_top]:
        print(f"  {name:30s} {val:.1f}")


def threshold_analysis(X: pd.DataFrame, y: pd.Series) -> None:
    """Простая оценка: на каком threshold модель даёт лучший precision на 'входить'."""
    print("\n=== Threshold analysis (как фильтр) ===")
    params = {
        "objective": "binary", "metric": "auc",
        "learning_rate": 0.05, "num_leaves": 31,
        "min_data_in_leaf": 10, "verbose": -1, "seed": RANDOM_STATE,
        "is_unbalance": True,
    }
    # Простой train/test 80/20 по времени
    split_idx = int(len(X) * 0.8)
    X_tr, X_te = X.iloc[:split_idx], X.iloc[split_idx:]
    y_tr, y_te = y.iloc[:split_idx], y.iloc[split_idx:]

    if y_tr.nunique() < 2 or y_te.nunique() < 2:
        print("Невозможно: один класс в выборке")
        return

    model = lgb.train(params, lgb.Dataset(X_tr, label=y_tr), num_boost_round=300)
    proba = model.predict(X_te)

    for thr in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75]:
        pred = (proba >= thr).astype(int)
        if pred.sum() == 0:
            print(f"  thr={thr:.2f}: нет сигналов")
            continue
        precision = (pred & y_te.values).sum() / pred.sum()
        recall = (pred & y_te.values).sum() / max(y_te.sum(), 1)
        print(f"  thr={thr:.2f}: precision={precision:.2%} "
              f"recall={recall:.2%} signals={int(pred.sum())}/{len(pred)}")


def main() -> None:
    if not Path(AUTO_SHORTS_CSV).exists() or not Path(CANCELED_CSV).exists():
        print("Не найден один из CSV. Проверь пути.")
        sys.exit(1)

    df_e = load_entered(AUTO_SHORTS_CSV)
    df_c = load_canceled(CANCELED_CSV)

    df, feature_cols = merge_datasets(df_e, df_c)
    if len(df) < 30:
        print("Слишком мало данных")
        sys.exit(1)

    X = df[feature_cols]
    y = df[TARGET].astype(int)

    print(f"\n=== TimeSeriesSplit AUC (n_splits={N_SPLITS}) ===")
    res = cross_val_auc(X, y, N_SPLITS)
    print(f"\nСредний AUC: {res['mean_auc']:.3f} ± {res['std_auc']:.3f}")
    print(f"По фолдам:   {[f'{a:.3f}' for a in res['fold_aucs']]}")

    feature_importance(X, y)
    threshold_analysis(X, y)


if __name__ == "__main__":
    main()