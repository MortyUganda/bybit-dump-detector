# train_first_model.py

import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, accuracy_score

def load_data(conn_str: str) -> pd.DataFrame:
    # тут лучше сразу SQL по auto_shorts
    query = """
    SELECT
        id,
        entry_ts,
        ml_label,
        score AS signal_score,
        -- market/volatility features
        realized_vol_1h,
        volume_24h_usdt,
        price_change_5m,
        price_change_1h,
        spread_pct,
        bid_depth_change_5m,
        btc_change_15m,
        funding_rate_at_signal,
        oi_change_pct_at_signal,
        trend_strength_1h,
        -- factors
        f_rsi,
        f_vwap_extension,
        f_volume_zscore,
        f_trade_imbalance,
        f_large_buy_cluster,
        f_price_acceleration,
        f_consecutive_greens,
        f_ob_bid_thinning,
        f_spread_expansion,
        f_momentum_loss,
        f_upper_wick,
        f_funding_rate,
        f_rsi_5m,
        f_large_sell_cluster,
        f_cvd_divergence,
        f_liquidation_cascade
    FROM auto_shorts
    WHERE status = 'closed'
      AND ml_label IS NOT NULL
    ORDER BY entry_ts
    """
    df = pd.read_sql(query, conn_str)
    return df

def filter_and_prepare(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    # ключевые фичи — без них выбрасываем строку
    key_cols = [
        "signal_score",
        "realized_vol_1h",
        "volume_24h_usdt",
        "price_change_5m",
        "price_change_1h",
        "spread_pct",
        "bid_depth_change_5m",
        "btc_change_15m",
        "funding_rate_at_signal",
        "oi_change_pct_at_signal",
        "trend_strength_1h",
    ]

    # дропаем строки с пропусками в ключевых
    mask = df[key_cols].notnull().all(axis=1)
    df = df.loc[mask].copy()

    # таргет
    y = df["ml_label"].astype(int)

    # список фичей: все числовые, кроме id, entry_ts, ml_label
    drop_cols = ["id", "entry_ts", "ml_label"]
    feature_cols = [c for c in df.columns if c not in drop_cols]

    # оставшиеся NaN (в основном в f_*) заменяем на 0
    df[feature_cols] = df[feature_cols].fillna(0.0)

    X = df[feature_cols]
    return X, y, df[["id", "entry_ts"]]

def time_based_split(X, y, meta, test_fraction: float = 0.2):
    n = len(X)
    n_test = max(1, int(n * test_fraction))
    split_idx = n - n_test

    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
    meta_train, meta_test = meta.iloc[:split_idx], meta.iloc[split_idx:]

    return X_train, X_test, y_train, y_test, meta_train, meta_test

def train_lgbm(X_train, y_train):
    train_data = lgb.Dataset(X_train, label=y_train)

    params = {
        "objective": "binary",
        "metric": ["auc"],
        "learning_rate": 0.05,
        "num_leaves": 31,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "min_data_in_leaf": 10,
        "verbose": -1,
    }

    model = lgb.train(
        params,
        train_data,
        num_boost_round=200,
        valid_sets=[train_data],
        valid_names=["train"],
    )
    return model

def evaluate(model, X_test, y_test, threshold: float = 0.5):
    proba = model.predict(X_test)
    preds = (proba >= threshold).astype(int)

    auc = roc_auc_score(y_test, proba)
    acc = accuracy_score(y_test, preds)

    return {
        "auc": auc,
        "accuracy": acc,
    }

def main():
    import os
    conn_str = os.environ.get("DATABASE_URL", "postgresql+psycopg2://dumpuser:dumppass@localhost:5433/dumpdetector")

    df = load_data(conn_str)
    X, y, meta = filter_and_prepare(df)
    X_train, X_test, y_train, y_test, meta_train, meta_test = time_based_split(X, y, meta, test_fraction=0.2)

    model = train_lgbm(X_train, y_train)
    metrics = evaluate(model, X_test, y_test)

    print("Samples:", len(X))
    print("Train:", len(X_train), "Test:", len(X_test))
    print("AUC:", metrics["auc"])
    print("ACC:", metrics["accuracy"])

    # важности фичей
    import numpy as np
    importance = model.feature_importance()
    features = X_train.columns
    idx = np.argsort(-importance)
    print("Top features:")
    for i in idx[:20]:
        print(features[i], float(importance[i]))

if __name__ == "__main__":
    main()