"""
Унифицированная модель прибыльности.

Объединяет:
  - auto_shorts (реально открытые позиции) — label = ml_label
  - canceled_signals (отменённые сигналы) — label из synthetic исхода:
        would_hit_tp == True  → 1 (выиграл бы)
        would_hit_sl == True  → 0 (проиграл бы)
        neither               → skip (мутный класс, не учим)

Цель: предсказать «был бы прибыльным сигнал» вне зависимости от того,
вошёл бот или нет. Это даёт устойчивый ML-фильтр на полной выборке.
"""

from __future__ import annotations

import argparse
import glob
import pickle
import sys
from pathlib import Path
from datetime import date, datetime
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit

# === Настройки по умолчанию ===
DEFAULT_N_SPLITS = 5
RANDOM_STATE = 42


def find_latest_csv(pattern: str) -> str | None:
    """Найти самый свежий CSV по glob-паттерну."""
    files = glob.glob(pattern)
    if not files:
        return None
    return max(files, key=lambda p: Path(p).stat().st_mtime)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Decision ML: прибыльность (auto_shorts + canceled_signals + all_opened_signals)"
    )
    p.add_argument("--auto-csv", default=None,
                   help="Путь к auto_shorts CSV (default: последний auto_shorts_*.csv)")
    p.add_argument("--canceled-csv", default=None,
                   help="Путь к canceled_signals CSV (default: последний canceled_signals_*.csv)")
    p.add_argument("--all-opened-csv", default=None,
                   help="Путь к all_opened_signals CSV (default: последний all_opened_signals_*.csv)")
    p.add_argument("--splits", type=int, default=DEFAULT_N_SPLITS,
                   help=f"Число фолдов (default {DEFAULT_N_SPLITS})")
    return p.parse_args()

# Общие фичи (присутствуют во всех таблицах в момент сигнала)
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

TARGET = "label"


def load_entered(path: str) -> pd.DataFrame:
    """auto_shorts — берём только закрытые сделки с известным ml_label."""
    df = pd.read_csv(path)
    print(f"auto_shorts всего: {len(df)} строк")

    df = df[df["status"] == "closed"].copy()
    df = df[df["ml_label"].notna()].copy()
    df["signal_ts"] = pd.to_datetime(df["entry_ts"])
    df[TARGET] = df["ml_label"].astype(int)
    df["source"] = "auto_short"

    print(f"  закрытых с ml_label: {len(df)} "
          f"(W={int(df[TARGET].sum())}, L={int((df[TARGET]==0).sum())})")
    return df


def load_canceled(path: str) -> pd.DataFrame:
    """
    canceled_signals — синтетический исход:
      would_hit_tp → 1
      would_hit_sl → 0
      neither      → отбрасываем
    """
    df = pd.read_csv(path)
    print(f"canceled_signals всего: {len(df)} строк")

    df["signal_ts"] = pd.to_datetime(df["signal_ts"])

    # Будем использовать только записи с однозначным исходом
    tp = df["would_hit_tp"] == True   # noqa: E712
    sl = df["would_hit_sl"] == True   # noqa: E712

    out = df[tp | sl].copy()
    out[TARGET] = tp[tp | sl].astype(int).values
    out["source"] = "canceled"

    skipped = len(df) - len(out)
    print(f"  с однозначным исходом: {len(out)} "
          f"(W={int(out[TARGET].sum())}, L={int((out[TARGET]==0).sum())})")
    print(f"  пропущено мутных (neither/NaN): {skipped}")
    return out


def load_all_opened(path: str) -> pd.DataFrame:
    """
    all_opened_signals — shadow-paper записи:
      status == "closed", close_reason == "tp_hit" → 1, "sl_hit" → 0, rest → skip.
      Dedup: исключаем записи с linked_auto_short_id или linked_canceled_signal_id
      (они дублируют данные из auto_shorts / canceled_signals).
    """
    df = pd.read_csv(path)
    print(f"all_opened_signals всего: {len(df)} строк")

    # Только закрытые
    df = df[df["status"] == "closed"].copy()
    print(f"  закрытых: {len(df)}")

    # Dedup: оставляем только «чистые» shadow-paper (не привязанные к auto_short/canceled)
    if "linked_auto_short_id" in df.columns:
        df = df[df["linked_auto_short_id"].isna()].copy()
    if "linked_canceled_signal_id" in df.columns:
        df = df[df["linked_canceled_signal_id"].isna()].copy()
    print(f"  после dedup (без linked): {len(df)}")

    # Метки: tp_hit → 1, sl_hit → 0, остальные → skip
    mask_tp = df["close_reason"] == "tp_hit"
    mask_sl = df["close_reason"] == "sl_hit"
    df = df[mask_tp | mask_sl].copy()
    df[TARGET] = mask_tp[mask_tp | mask_sl].astype(int).values

    df["signal_ts"] = pd.to_datetime(df["entry_ts"])
    df["source"] = "all_opened"

    print(f"  с однозначным исходом: {len(df)} "
          f"(W={int(df[TARGET].sum())}, L={int((df[TARGET] == 0).sum())})")
    return df


def merge_datasets(
    df_entered: pd.DataFrame,
    df_canceled: pd.DataFrame,
    df_all_opened: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    # Intersect фичей по всем доступным источникам
    all_dfs = [df_entered, df_canceled]
    if df_all_opened is not None and len(df_all_opened) > 0:
        all_dfs.append(df_all_opened)

    feature_cols = [
        c for c in COMMON_FEATURES
        if all(c in d.columns for d in all_dfs)
    ]
    print(f"\nОбщих фичей: {len(feature_cols)}")

    cols = feature_cols + ["signal_ts", TARGET, "source"]
    df = pd.concat(
        [d[cols] for d in all_dfs],
        ignore_index=True,
    )
    df = df.sort_values("signal_ts").reset_index(drop=True)

    print(f"\nОбъединённый датасет: {len(df)} сигналов")
    print(f"  win  (label=1): {int(df[TARGET].sum())}")
    print(f"  loss (label=0): {int((df[TARGET]==0).sum())}")
    print(f"  WR общий:       {df[TARGET].mean():.1%}")
    print("  по источникам:")
    for src, g in df.groupby("source"):
        print(f"    {src:12s} n={len(g):4d}, WR={g[TARGET].mean():.1%}")

    df[feature_cols] = df[feature_cols].fillna(0.0)
    return df, feature_cols


def cross_val_auc(X: pd.DataFrame, y: pd.Series, n_splits: int) -> dict:
    if len(X) < n_splits * 2:
        n_splits = max(2, len(X) // 5)
        print(f"Слишком мало данных, n_splits={n_splits}")

    tscv = TimeSeriesSplit(n_splits=n_splits)
    fold_aucs = []
    oof_proba = np.zeros(len(X))
    oof_mask = np.zeros(len(X), dtype=bool)

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
        oof_proba[te] = proba
        oof_mask[te] = True
        print(f"Fold {fold}: train={len(X_tr)}, test={len(X_te)}, AUC={auc:.3f}")

    return {
        "fold_aucs": fold_aucs,
        "mean_auc": float(np.mean(fold_aucs)) if fold_aucs else float("nan"),
        "std_auc": float(np.std(fold_aucs)) if fold_aucs else float("nan"),
        "oof_proba": oof_proba,
        "oof_mask": oof_mask,
    }


def feature_importance(X: pd.DataFrame, y: pd.Series, n_top: int = 20) -> None:
    print("\n=== Важности фичей (модель на всех данных) ===")
    params = {
        "objective": "binary", "metric": "auc",
        "learning_rate": 0.05, "num_leaves": 31,
        "min_data_in_leaf": 10, "verbose": -1, "seed": RANDOM_STATE,
    }
    model = lgb.train(params, lgb.Dataset(X, label=y), num_boost_round=300)
    imp = sorted(
        zip(X.columns, model.feature_importance(importance_type="gain")),
        key=lambda x: -x[1],
    )
    for name, val in imp[:n_top]:
        print(f"  {name:30s} {val:.1f}")


def threshold_analysis(
    df: pd.DataFrame, y: pd.Series, oof_proba: np.ndarray, oof_mask: np.ndarray
) -> None:
    """
    OOF-симуляция ML-фильтра: при каком proba-пороге WR/EV выше всего.
    """
    print("\n=== ML-фильтр на OOF ===")
    sub = df.loc[oof_mask].copy()
    sub["proba"] = oof_proba[oof_mask]
    sub["y"] = y.loc[oof_mask].values

    base_wr = sub["y"].mean()
    print(f"Без фильтра: n={len(sub)}, WR={base_wr:.1%}")
    for thr in [0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
        f = sub[sub["proba"] >= thr]
        if len(f) == 0:
            print(f"  proba>={thr:.2f}: нет сигналов")
            continue
        wr = f["y"].mean()
        kept_pct = len(f) / len(sub) * 100
        print(f"  proba>={thr:.2f}: n={len(f):4d} ({kept_pct:5.1f}%), "
              f"WR={wr:.1%} (Δ={wr-base_wr:+.1%})")


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

    df_e = load_entered(auto_csv)
    df_c = load_canceled(canceled_csv)

    df_ao = None
    if all_opened_csv and Path(all_opened_csv).exists():
        print(f"all_opened_signals CSV:   {all_opened_csv}")
        df_ao = load_all_opened(all_opened_csv)
    else:
        print("all_opened_signals CSV:   не найден — пропускаем")

    df, feature_cols = merge_datasets(df_e, df_c, df_ao)
    if len(df) < 30:
        print("Слишком мало данных")
        sys.exit(1)

    X = df[feature_cols]
    y = df[TARGET].astype(int)

    print(f"\n=== TimeSeriesSplit AUC (n_splits={args.splits}) ===")
    res = cross_val_auc(X, y, args.splits)
    print(f"\nСредний AUC: {res['mean_auc']:.3f} ± {res['std_auc']:.3f}")
    print(f"По фолдам:   {[f'{a:.3f}' for a in res['fold_aucs']]}")

    feature_importance(X, y)
    threshold_analysis(df, y, res["oof_proba"], res["oof_mask"])

    # ── Обучение финальной модели на ВСЕХ данных и сохранение ──
    print("\n=== Обучение финальной модели на всех данных ===")
    from lightgbm import LGBMClassifier

    clf = LGBMClassifier(
        objective="binary",
        metric="auc",
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=10,
        subsample=0.8,
        colsample_bytree=0.8,
        subsample_freq=1,
        n_estimators=300,
        random_state=RANDOM_STATE,
        verbose=-1,
    )
    clf.fit(X, y)

    model_dir = Path("models")
    model_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    current_time = now.strftime("%H%M%S")
    model_path = model_dir / f"decision_model_{date.today()}_{current_time}.pkl"

    with open(model_path, "wb") as f:
        pickle.dump(clf, f)

    print(f"\n✅ Модель сохранена: {model_path} "
          f"(n={len(X)}, AUC={res['mean_auc']:.3f})")


if __name__ == "__main__":
    main()
