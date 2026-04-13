"""
Migration: Create ML views for data export and model training.

- ml_opened_vs_canceled: UNION of opened trades (auto_shorts) and
  canceled signals, with a common feature set for entry-decision models.
- ml_opened_only_profitable: closed trades from auto_shorts for PnL models.

Safe to run multiple times — uses CREATE OR REPLACE VIEW.

Run: python -m app.db.migrations.create_ml_views
"""
import asyncio

from app.db.session import engine
from app.utils.logging import get_logger, setup_logging

setup_logging("INFO")
logger = get_logger(__name__)

CREATE_VIEW_OPENED_VS_CANCELED_SQL = """
CREATE OR REPLACE VIEW ml_opened_vs_canceled AS

SELECT
    'opened'                    AS source_type,
    1                           AS entry_decision_label,
    a.id,
    a.symbol,
    a.signal_type,
    a.entry_ts                  AS ts,
    a.score                     AS signal_score,
    a.entry_score               AS final_score,
    a.min_score_at_entry,
    a.entry_mode                AS entry_mode_candidate,
    a.triggered_count,
    a.entry_delay_sec,
    a.signal_price,
    a.entry_price               AS final_price,
    a.price_change_at_entry     AS price_change_pct,
    NULL::VARCHAR(32)           AS cancel_reason,
    -- factor scores
    a.f_rsi,
    a.f_vwap_extension,
    a.f_volume_zscore,
    a.f_trade_imbalance,
    a.f_large_buy_cluster,
    a.f_price_acceleration,
    a.f_consecutive_greens,
    a.f_ob_bid_thinning,
    a.f_spread_expansion,
    a.f_momentum_loss,
    a.f_upper_wick,
    a.f_funding_rate,
    a.f_rsi_5m,
    a.f_large_sell_cluster,
    a.f_cvd_divergence,
    a.f_liquidation_cascade,
    -- market context
    a.realized_vol_1h,
    a.volume_24h_usdt,
    a.price_change_5m,
    a.price_change_1h,
    a.spread_pct,
    a.bid_depth_change_5m,
    a.btc_change_15m,
    a.funding_rate_at_signal,
    a.oi_change_pct_at_signal,
    a.trend_strength_1h,
    -- retrospective prices
    a.price_15m,
    a.price_30m,
    a.price_60m,
    a.price_15m_ts,
    a.price_30m_ts,
    a.price_60m_ts,
    -- outcome (only for opened)
    a.pnl_pct,
    a.ml_label,
    a.close_reason
FROM auto_shorts a

UNION ALL

SELECT
    'canceled'                  AS source_type,
    0                           AS entry_decision_label,
    c.id,
    c.symbol,
    c.signal_type,
    c.signal_ts                 AS ts,
    c.score                     AS signal_score,
    c.final_score,
    c.min_score_at_entry,
    c.entry_mode_candidate,
    c.triggered_count,
    c.entry_delay_sec,
    c.signal_price,
    c.final_price,
    c.price_change_pct,
    c.cancel_reason,
    -- factor scores
    c.f_rsi,
    c.f_vwap_extension,
    c.f_volume_zscore,
    c.f_trade_imbalance,
    c.f_large_buy_cluster,
    c.f_price_acceleration,
    c.f_consecutive_greens,
    c.f_ob_bid_thinning,
    c.f_spread_expansion,
    c.f_momentum_loss,
    c.f_upper_wick,
    c.f_funding_rate,
    c.f_rsi_5m,
    c.f_large_sell_cluster,
    c.f_cvd_divergence,
    c.f_liquidation_cascade,
    -- market context
    c.realized_vol_1h,
    c.volume_24h_usdt,
    c.price_change_5m,
    c.price_change_1h,
    c.spread_pct,
    c.bid_depth_change_5m,
    c.btc_change_15m,
    c.funding_rate_at_signal,
    c.oi_change_pct_at_signal,
    c.trend_strength_1h,
    -- retrospective prices
    c.price_15m,
    c.price_30m,
    c.price_60m,
    c.price_15m_ts,
    c.price_30m_ts,
    c.price_60m_ts,
    -- outcome (N/A for canceled)
    NULL::DOUBLE PRECISION      AS pnl_pct,
    NULL::INTEGER               AS ml_label,
    NULL::VARCHAR(20)           AS close_reason
FROM canceled_signals c
"""

CREATE_VIEW_OPENED_PROFITABLE_SQL = """
CREATE OR REPLACE VIEW ml_opened_only_profitable AS
SELECT
    a.id,
    a.symbol,
    a.signal_type,
    a.entry_ts,
    a.exit_ts,
    a.score,
    a.entry_score,
    a.min_score_at_entry,
    a.entry_mode,
    a.triggered_count,
    a.entry_delay_sec,
    a.signal_price,
    a.entry_price,
    a.exit_price,
    a.price_change_at_entry,
    a.close_reason,
    a.pnl_pct,
    a.ml_label,
    a.leverage,
    a.tp_pct,
    a.sl_pct,
    -- factor scores
    a.f_rsi,
    a.f_vwap_extension,
    a.f_volume_zscore,
    a.f_trade_imbalance,
    a.f_large_buy_cluster,
    a.f_price_acceleration,
    a.f_consecutive_greens,
    a.f_ob_bid_thinning,
    a.f_spread_expansion,
    a.f_momentum_loss,
    a.f_upper_wick,
    a.f_funding_rate,
    a.f_rsi_5m,
    a.f_large_sell_cluster,
    a.f_cvd_divergence,
    a.f_liquidation_cascade,
    -- market context
    a.realized_vol_1h,
    a.volume_24h_usdt,
    a.price_change_5m,
    a.price_change_1h,
    a.spread_pct,
    a.bid_depth_change_5m,
    a.btc_change_15m,
    a.funding_rate_at_signal,
    a.oi_change_pct_at_signal,
    a.trend_strength_1h,
    -- retrospective prices
    a.price_15m,
    a.price_30m,
    a.price_60m,
    a.price_15m_ts,
    a.price_30m_ts,
    a.price_60m_ts
FROM auto_shorts a
WHERE a.status = 'closed'
"""


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(CREATE_VIEW_OPENED_VS_CANCELED_SQL))
        logger.info("View ml_opened_vs_canceled created")
        await conn.execute(text(CREATE_VIEW_OPENED_PROFITABLE_SQL))
        logger.info("View ml_opened_only_profitable created")
    logger.info("ML views migration completed successfully")


if __name__ == "__main__":
    asyncio.run(run_migration())
