# Architecture: Bybit Dump Detector

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         BYBIT EXCHANGE                          │
│  WebSocket (public/spot, public/linear)                         │
│    → publicTrade.{symbol}  — real-time trades                   │
│    → tickers.{symbol}      — price/vol updates                  │
│    → orderbook.25.{symbol} — OB snapshots + deltas              │
│  REST API                                                       │
│    → /v5/market/kline      — OHLCV candles (1m, 5m, 15m)       │
│    → /v5/market/instruments-info — symbol metadata              │
│    → /v5/market/tickers    — 24h stats for universe filter      │
│    → /v5/market/open-interest — OI for linear perps             │
└──────────────────────────────────────┬──────────────────────────┘
                                       │
                         ┌─────────────▼─────────────┐
                         │     INGESTION SERVICE      │
                         │   BybitWSClient (async)    │
                         │   BybitRestClient (async)  │
                         │   UniverseManager          │
                         │   OrderbookAnalyzer        │
                         │   FeatureCalculator x N    │
                         └─────────────┬─────────────┘
                                       │ CoinFeatures (dataclass)
                                       │
                         ┌─────────────▼─────────────┐
                         │       REDIS CACHE          │
                         │  features:{symbol}  TTL 5m │
                         │  score:{symbol}     TTL 5m │
                         │  cooldown:{sym}:{type}     │
                         │  overvalued:latest         │
                         └─────────────┬─────────────┘
                                       │
                         ┌─────────────▼─────────────┐
                         │     ANALYZER SERVICE       │
                         │   ScoringEngine (rule-based│
                         │   Overvalued Ranker        │
                         │   Cooldown Checker         │
                         └──────┬────────────┬────────┘
                                │            │
               ┌────────────────▼──┐  ┌──────▼──────────────┐
               │   POSTGRESQL DB   │  │   ALERT MANAGER      │
               │  signals          │  │   AlertManager       │
               │  overvalued_snapshots  Telegram broadcast   │
               │  candle_features  │  └──────┬──────────────┘
               │  user_settings    │         │
               │  watchlists       │  ┌──────▼──────────────┐
               │  alert_history    │  │   TELEGRAM BOT       │
               └───────────────────┘  │   aiogram 3.x        │
                                      │   /signals           │
                                      │   /overvalued        │
                                      │   /coin SYMBOL       │
                                      │   /watchlist         │
                                      │   /settings          │
                                      └─────────────────────┘
```

## Service Deployment

```
docker-compose services:
  postgres   — PostgreSQL 16 (persistent storage)
  redis      — Redis 7 (feature cache + cooldown)
  ingestion  — WS + REST data pipeline
  analyzer   — scoring loop + overvalued ranking
  bot        — Telegram bot
  migrate    — runs once on startup, creates tables
```

## Data Sources Decision Matrix

| Data | Source | Frequency | Store | Why |
|---|---|---|---|---|
| Trade ticks | WS publicTrade | Real-time | Redis rolling buffer | Buy/sell imbalance, large trades |
| Price/vol | WS tickers | On change | Redis | Live price updates |
| Orderbook | WS orderbook.25 | Real-time deltas | In-memory mirror | OB imbalance, spread, bid thinning |
| OHLCV 1m | REST kline | 60s refresh | Last 120 in memory | RSI, ATR, VWAP, patterns |
| OHLCV 5m | REST kline | 60s refresh | Last 100 in memory | RSI 5m, volume history |
| 24h stats | REST tickers | 5m (universe) | Redis + Postgres | Volume filter, price change |
| Instruments | REST instruments | 5m | Memory (universe) | Symbol filtering |
| Open Interest | REST oi | 5m | Candle features | Leveraged speculation proxy |
| Funding Rate | REST funding | 5m | Per-signal snapshot | Crowded longs |

## Feature → Signal Taxonomy

```
EARLY WARNING (score 30–49, 2+ factors):
  - RSI approaching overbought (65–75)
  - Moderate volume spike (1.5–2.5σ)
  - Buy imbalance building
  
OVERHEATED (score 50+, RSI+VWAP+Volume all triggered):
  - RSI > 75
  - Price > 3% above VWAP
  - Volume > 2.5σ spike
  - 5+ consecutive green candles

REVERSAL RISK (momentum loss + wick + OB thinning):
  - Momentum stall after impulse
  - Upper wick > 1x body
  - Bid depth shrinking > 20%

DUMP STARTED (price falling + OB collapse):
  - 5m price change < -3%
  - Bid depth change < -40%
  - Trade imbalance negative (sell-heavy)
```

## Risk Score Formula

```
score = Σ (factor_normalized × factor_weight × 100)

Capped at [0, 100].
Suppressed to max 30 if triggered_count < 3 (anti-noise).

Factor normalizations: linear, clipped to [0, 1].
  normalized(x) = clamp((x - low_thresh) / (high_thresh - low_thresh), 0, 1)
```

## Calibration Notes

All thresholds are defaults based on common shitcoin behavior patterns.
**Require recalibration after 1 week of live data** by analyzing:
- False positive rate (alerts where price didn't fall)
- False negative rate (large dumps not caught)
- Distribution of each factor value across universe

Recommended calibration tool: collect 7 days of (features, next_1h_price_change) pairs,
then optimize thresholds via grid search minimizing FP rate at fixed recall.
