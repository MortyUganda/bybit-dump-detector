from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.db.session import engine


SQL = """
CREATE TABLE IF NOT EXISTS canceled_signals (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(32) NOT NULL,
    signal_type VARCHAR(32) NOT NULL DEFAULT 'unknown',
    cancel_reason VARCHAR(32) NOT NULL,

    signal_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decision_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    signal_price DOUBLE PRECISION NOT NULL,
    final_price DOUBLE PRECISION NOT NULL,
    price_change_pct DOUBLE PRECISION NOT NULL,

    score DOUBLE PRECISION NOT NULL,
    final_score DOUBLE PRECISION NOT NULL,
    min_score_at_entry DOUBLE PRECISION NOT NULL,

    entry_mode_candidate VARCHAR(32) NOT NULL DEFAULT 'direct',
    triggered_count INTEGER NULL,

    entry_delay_sec INTEGER NULL,
    monitor_attempts INTEGER NULL,
    monitor_interval_sec INTEGER NULL,
    stabilization_threshold_pct DOUBLE PRECISION NULL,
    max_rise_pct DOUBLE PRECISION NULL,
    max_entry_drop_pct DOUBLE PRECISION NULL,

    f_rsi DOUBLE PRECISION NULL,
    f_vwap_extension DOUBLE PRECISION NULL,
    f_volume_zscore DOUBLE PRECISION NULL,
    f_trade_imbalance DOUBLE PRECISION NULL,
    f_large_buy_cluster DOUBLE PRECISION NULL,
    f_price_acceleration DOUBLE PRECISION NULL,
    f_consecutive_greens DOUBLE PRECISION NULL,
    f_ob_bid_thinning DOUBLE PRECISION NULL,
    f_spread_expansion DOUBLE PRECISION NULL,
    f_momentum_loss DOUBLE PRECISION NULL,
    f_upper_wick DOUBLE PRECISION NULL,
    f_funding_rate DOUBLE PRECISION NULL,
    f_rsi_5m DOUBLE PRECISION NULL,
    f_large_sell_cluster DOUBLE PRECISION NULL,
    f_cvd_divergence DOUBLE PRECISION NULL,
    f_liquidation_cascade DOUBLE PRECISION NULL,

    realized_vol_1h DOUBLE PRECISION NULL,
    volume_24h_usdt DOUBLE PRECISION NULL,
    price_change_5m DOUBLE PRECISION NULL,
    price_change_1h DOUBLE PRECISION NULL,
    spread_pct DOUBLE PRECISION NULL,
    bid_depth_change_5m DOUBLE PRECISION NULL,
    btc_change_15m DOUBLE PRECISION NULL,
    funding_rate_at_signal DOUBLE PRECISION NULL,
    oi_change_pct_at_signal DOUBLE PRECISION NULL,
    trend_strength_1h DOUBLE PRECISION NULL
);

CREATE INDEX IF NOT EXISTS ix_canceled_signals_symbol
    ON canceled_signals(symbol);

CREATE INDEX IF NOT EXISTS ix_canceled_signals_cancel_reason
    ON canceled_signals(cancel_reason);

CREATE INDEX IF NOT EXISTS ix_canceled_signals_signal_ts
    ON canceled_signals(signal_ts);
"""


async def main() -> None:
    async with engine.begin() as conn:
        for stmt in SQL.split(";"):
            stmt = stmt.strip()
            if stmt:
                await conn.execute(text(stmt))
    print("canceled_signals table created")


if __name__ == "__main__":
    asyncio.run(main())