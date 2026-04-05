-- ============================================================
-- Bybit Dump Detector — Initial Database Schema
-- Run: psql -U dumpuser -d dumpdetector -f init_db.sql
-- ============================================================

-- symbols: master list of tracked instruments
CREATE TABLE IF NOT EXISTS symbols (
    id                  SERIAL PRIMARY KEY,
    symbol              VARCHAR(32) UNIQUE NOT NULL,
    base_asset          VARCHAR(16) NOT NULL,
    quote_asset         VARCHAR(16) NOT NULL DEFAULT 'USDT',
    is_active           BOOLEAN DEFAULT TRUE,
    listing_date        TIMESTAMPTZ,
    volume_24h_usdt     FLOAT DEFAULT 0.0,
    last_price          FLOAT DEFAULT 0.0,
    price_change_24h_pct FLOAT DEFAULT 0.0,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_symbols_active ON symbols(is_active);

-- signals: fired alerts with full context
CREATE TABLE IF NOT EXISTS signals (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(32) NOT NULL,
    signal_type     VARCHAR(32) NOT NULL,
    risk_level      VARCHAR(16) NOT NULL,
    score           FLOAT NOT NULL,
    triggered_count INTEGER DEFAULT 0,
    top_reasons     VARCHAR(256),
    factors_json    JSONB,
    features_json   JSONB,
    price_at_signal FLOAT DEFAULT 0.0,
    alert_sent      BOOLEAN DEFAULT FALSE,
    ts              TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals(symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC);

-- overvalued_snapshots: periodic ranked list
CREATE TABLE IF NOT EXISTS overvalued_snapshots (
    id                  SERIAL PRIMARY KEY,
    batch_id            VARCHAR(36) NOT NULL,
    rank                INTEGER NOT NULL,
    symbol              VARCHAR(32) NOT NULL,
    score               FLOAT NOT NULL,
    risk_level          VARCHAR(16) NOT NULL,
    price               FLOAT DEFAULT 0.0,
    price_change_24h_pct FLOAT DEFAULT 0.0,
    volume_24h_usdt     FLOAT DEFAULT 0.0,
    rsi                 FLOAT DEFAULT 50.0,
    vwap_extension_pct  FLOAT DEFAULT 0.0,
    top_reasons         VARCHAR(256),
    features_json       JSONB,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ovv_batch ON overvalued_snapshots(batch_id);
CREATE INDEX IF NOT EXISTS idx_ovv_created ON overvalued_snapshots(created_at DESC);

-- candle_features: per-symbol per-candle aggregates
CREATE TABLE IF NOT EXISTS candle_features (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(32) NOT NULL,
    interval        VARCHAR(4) NOT NULL,
    candle_ts       TIMESTAMPTZ NOT NULL,
    open            FLOAT,
    high            FLOAT,
    low             FLOAT,
    close           FLOAT,
    volume          FLOAT,
    turnover        FLOAT,
    rsi_14          FLOAT,
    atr_14          FLOAT,
    vwap            FLOAT,
    vwap_extension_pct FLOAT,
    volume_zscore   FLOAT,
    risk_score      FLOAT,
    UNIQUE (symbol, interval, candle_ts)
);
CREATE INDEX IF NOT EXISTS idx_candle_sym_ts ON candle_features(symbol, candle_ts DESC);

-- user_settings: per-user Telegram preferences
CREATE TABLE IF NOT EXISTS user_settings (
    id                      SERIAL PRIMARY KEY,
    telegram_user_id        BIGINT UNIQUE NOT NULL,
    username                VARCHAR(64),
    alerts_enabled          BOOLEAN DEFAULT TRUE,
    min_score_to_alert      FLOAT DEFAULT 50.0,
    alert_cooldown_minutes  INTEGER DEFAULT 60,
    notify_early_warning    BOOLEAN DEFAULT FALSE,
    notify_overheated       BOOLEAN DEFAULT TRUE,
    notify_reversal_risk    BOOLEAN DEFAULT TRUE,
    notify_dump_started     BOOLEAN DEFAULT TRUE,
    language                VARCHAR(8) DEFAULT 'en',
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- watchlists: per-user symbol lists
CREATE TABLE IF NOT EXISTS watchlists (
    id                  SERIAL PRIMARY KEY,
    telegram_user_id    BIGINT NOT NULL,
    symbol              VARCHAR(32) NOT NULL,
    added_at            TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (telegram_user_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlists(telegram_user_id);

-- alert_history: audit trail of sent messages
CREATE TABLE IF NOT EXISTS alert_history (
    id                  SERIAL PRIMARY KEY,
    telegram_user_id    BIGINT NOT NULL,
    symbol              VARCHAR(32) NOT NULL,
    signal_type         VARCHAR(32) NOT NULL,
    score               FLOAT NOT NULL,
    message_id          INTEGER,
    sent_at             TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alert_hist_user ON alert_history(telegram_user_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_hist_sym ON alert_history(symbol, sent_at DESC);

-- ── Retention cleanup functions ───────────────────────────────
-- Run via cron or pg_cron extension
-- DELETE FROM signals WHERE created_at < NOW() - INTERVAL '30 days';
-- DELETE FROM candle_features WHERE candle_ts < NOW() - INTERVAL '30 days';
-- DELETE FROM overvalued_snapshots WHERE created_at < NOW() - INTERVAL '7 days';
-- DELETE FROM alert_history WHERE sent_at < NOW() - INTERVAL '30 days';
