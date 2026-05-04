"""
Обучение LightGBM на auto_shorts + canceled_signals и расчёт AUC.
Time-based cross-validation для честной оценки.
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit

# === Настройки по умолчанию ===
DEFAULT_MIN_TRADE_ID = 1  # с какой сделки берём данные (когда заработали OB-фичи)
DEFAULT_N_SPLITS = 5
RANDOM_STATE = 42


def find_latest_csv(pattern: str) -> str | None:
    """Найти самый свежий CSV по glob-паттерну."""
    files = glob.glob(pattern)
    if not files:
        return None
    return max(files, key=lambda p: Path(p).stat().st_mtime)

# Ключевые фичи — без них дропаем строку
KEY_FEATURES = [
    "score",
    "entry_score",
    "realized_vol_1h",
    "price_change_at_entry",
]

# Все фичи для модели (числовые признаки в момент входа)
FEATURE_COLS = [
    "score", "entry_score", "min_score_at_entry", "triggered_count",
    "f_rsi", "f_rsi_5m", "f_vwap_extension", "f_volume_zscore",
    "f_trade_imbalance", "f_large_buy_cluster", "f_large_sell_cluster",
    "f_price_acceleration", "f_consecutive_greens", "f_ob_bid_thinning",
    "f_spread_expansion", "f_momentum_loss", "f_upper_wick", "f_funding_rate",
    "f_cvd_divergence", "f_liquidation_cascade",
    "realized_vol_1h", "volume_24h_usdt",
    "price_change_at_entry", "price_change_5m", "price_change_1h",
    "spread_pct", "bid_depth_change_5m", "btc_change_15m",
    "funding_rate_at_signal", "oi_change_pct_at_signal", "trend_strength_1h",
    "ob_bid_volume_top10", "ob_ask_volume_top10",
    "ob_imbalance_top10", "ob_spread_bps",
]

TARGET = "ml_label"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Outcome ML: AUC по auto_shorts")
    p.add_argument("--csv", default=None,
                   help="Путь к auto_shorts CSV (по умолчанию — последний auto_shorts_*.csv)")
    p.add_argument("--min-id", type=int, default=DEFAULT_MIN_TRADE_ID,
                   help=f"Минимальный id сделки (default {DEFAULT_MIN_TRADE_ID})")
    p.add_argument("--splits", type=int, default=DEFAULT_N_SPLITS,
                   help=f"Число фолдов TimeSeriesSplit (default {DEFAULT_N_SPLITS})")
    return p.parse_args()


def load_and_prepare(path: str, min_id: int) -> pd.DataFrame:
    df = pd.read_csv(path)
    print(f"Загружено: {len(df)} строк")

    df = df[df["id"] >= min_id].copy()
    df = df[df["status"] == "closed"]
    df = df[df[TARGET].notna()]
    df["entry_ts"] = pd.to_datetime(df["entry_ts"])
    df = df.sort_values("entry_ts").reset_index(drop=True)

    print(f"После фильтра (id>={min_id}, closed): {len(df)} сделок")
    print(f"WR: {df[TARGET].mean():.1%} "
          f"({int(df[TARGET].sum())}W / {int((df[TARGET]==0).sum())}L)")
    return df


def filter_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    # Дропаем строки без ключевых фичей
    mask = df[KEY_FEATURES].notna().all(axis=1)
    df = df.loc[mask].copy()
    print(f"После фильтра по ключевым фичам: {len(df)} сделок")

    feature_cols = [c for c in FEATURE_COLS if c in df.columns]
    df[feature_cols] = df[feature_cols].fillna(0.0)

    X = df[feature_cols]
    y = df[TARGET].astype(int)
    return X, y


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
            "num_leaves": 15,
            "min_data_in_leaf": 5,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "verbose": -1,
            "seed": RANDOM_STATE,
        }

        model = lgb.train(
            params,
            lgb.Dataset(X_tr, label=y_tr),
            num_boost_round=200,
            valid_sets=[lgb.Dataset(X_te, label=y_te)],
            callbacks=[lgb.early_stopping(20, verbose=False)],
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


def feature_importance(X: pd.DataFrame, y: pd.Series) -> None:
    print("\n=== Важности фичей (модель на всех данных) ===")
    params = {
        "objective": "binary", "metric": "auc",
        "learning_rate": 0.05, "num_leaves": 15,
        "min_data_in_leaf": 5, "verbose": -1, "seed": RANDOM_STATE,
    }
    model = lgb.train(params, lgb.Dataset(X, label=y), num_boost_round=200)
    imp = sorted(
        zip(X.columns, model.feature_importance(importance_type="gain")),
        key=lambda x: -x[1],
    )
    for name, val in imp[:20]:
        print(f"  {name:30s} {val:.1f}")


def main() -> None:
    args = parse_args()

    csv_path = args.csv or find_latest_csv("auto_shorts_*.csv")
    if not csv_path or not Path(csv_path).exists():
        print("Не найден auto_shorts CSV. Укажи путь через --csv")
        sys.exit(1)
    print(f"CSV: {csv_path}")

    df = load_and_prepare(csv_path, args.min_id)
    if len(df) < 20:
        print("Слишком мало сделок для обучения")
        sys.exit(1)

    X, y = filter_features(df)
    print(f"Фичей: {X.shape[1]}, сделок: {X.shape[0]}")

    print(f"\n=== TimeSeriesSplit AUC (n_splits={args.splits}) ===")
    res = cross_val_auc(X, y, args.splits)
    print(f"\nСредний AUC: {res['mean_auc']:.3f} ± {res['std_auc']:.3f}")
    print(f"По фолдам:   {[f'{a:.3f}' for a in res['fold_aucs']]}")

    feature_importance(X, y)


if __name__ == "__main__":
    main()