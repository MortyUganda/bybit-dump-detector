"""
ML scoring layer for bybit-dump-detector.
Trains a LightGBM classifier on historical AutoShort outcomes.
Uses the ml_label field (1=profitable, 0=loss) as the target.
"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

FEATURE_COLS = [
    "f_rsi", "f_rsi_5m", "f_vwap_extension", "f_volume_zscore",
    "f_trade_imbalance", "f_large_buy_cluster", "f_large_sell_cluster",
    "f_price_acceleration", "f_consecutive_greens", "f_ob_bid_thinning",
    "f_spread_expansion", "f_momentum_loss", "f_upper_wick", "f_funding_rate",
    "btc_change_15m", "funding_rate_at_signal", "oi_change_pct_at_signal",
    "trend_strength_1h",
]

MODEL_PATH = Path("ml_model/lgbm_scorer.pkl")


class MLScorer:
    """
    Gradient-boosted tree classifier trained on paper trade outcomes.
    Falls back gracefully when model is not trained yet (returns 0.5).
    Requires: pip install lightgbm pandas
    """

    def __init__(self) -> None:
        self.model = None
        self._load_model()

    def _load_model(self) -> None:
        if MODEL_PATH.exists():
            try:
                with open(MODEL_PATH, "rb") as f:
                    self.model = pickle.load(f)
                logger.info("ML scorer loaded from %s", MODEL_PATH)
            except Exception as e:
                logger.warning("Failed to load ML model: %s", e)

    def is_ready(self) -> bool:
        return self.model is not None

    def predict_probability(self, feature_dict: dict) -> float:
        """
        Returns probability (0-1) that this short will be profitable.
        Returns 0.5 (neutral) if model not trained yet.
        """
        if not self.is_ready():
            return 0.5
        try:
            import pandas as pd
            X = pd.DataFrame([feature_dict])[FEATURE_COLS].fillna(0)
            return float(self.model.predict_proba(X)[0][1])
        except Exception as e:
            logger.warning("ML prediction failed: %s", e)
            return 0.5

    def train_from_db(self, engine) -> bool:
        """
        Trains the model from AutoShort records in PostgreSQL.
        Requires at least 100 closed trades with ml_label set.
        Call this manually or schedule weekly.
        Returns True if training succeeded.
        """
        try:
            import pandas as pd
            import lightgbm as lgb
            from sqlalchemy import text

            with engine.connect() as conn:
                df = pd.read_sql(
                    text("""
                        SELECT f_rsi, f_rsi_5m, f_vwap_extension, f_volume_zscore,
                               f_trade_imbalance, f_large_buy_cluster, f_large_sell_cluster,
                               f_price_acceleration, f_consecutive_greens, f_ob_bid_thinning,
                               f_spread_expansion, f_momentum_loss, f_upper_wick, f_funding_rate,
                               btc_change_15m, funding_rate_at_signal, oi_change_pct_at_signal,
                               trend_strength_1h, ml_label
                        FROM auto_shorts
                        WHERE ml_label IS NOT NULL AND status = 'closed'
                    """),
                    conn,
                )

            if len(df) < 100:
                logger.warning("Not enough data to train ML model: %d records", len(df))
                return False

            X = df[FEATURE_COLS].fillna(0)
            y = df["ml_label"]

            clf = lgb.LGBMClassifier(
                n_estimators=200,
                max_depth=5,
                learning_rate=0.05,
                class_weight="balanced",
                random_state=42,
            )
            clf.fit(X, y)

            MODEL_PATH.parent.mkdir(exist_ok=True)
            with open(MODEL_PATH, "wb") as f:
                pickle.dump(clf, f)

            self.model = clf
            logger.info("ML model trained on %d samples, saved to %s", len(df), MODEL_PATH)
            return True

        except ImportError:
            logger.error("lightgbm or pandas not installed. Run: pip install lightgbm pandas")
            return False
        except Exception as e:
            logger.error("ML training failed: %s", e)
            return False
