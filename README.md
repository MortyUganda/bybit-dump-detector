# Bybit Dump Detector 🔍

Telegram bot that monitors speculative coins on Bybit for overheating and dump risk.

Not a price prediction tool. An overheating detector that combines 12 analytical factors into a risk score 0–100.

---

## What It Does

- Monitors 100–400 shitcoins on Bybit spot in real-time via WebSocket
- Computes 12 risk factors per coin: RSI, VWAP extension, volume z-score, trade imbalance, large buy clusters, price acceleration, consecutive green candles, orderbook bid thinning, spread expansion, momentum loss, wick patterns, funding rate
- Produces a composite Risk Score 0–100 with anti-noise protection
- Fires 4 signal types to Telegram: Early Warning / Overheated / Reversal Risk / Dump Started
- Maintains a ranked "Overvalued Coins" list, updated every 5 minutes
- Per-user watchlists and alert preferences

---

## Quick Start (Docker)

### 1. Prerequisites

- Docker + Docker Compose
- Telegram Bot Token (from @BotFather)
- Bybit API Key (read-only, public data doesn't strictly require auth but is rate-limited without it)

### 2. Configure

```bash
cp .env.example .env
# Edit .env:
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_ALLOWED_USERS=your_telegram_user_id
#   BYBIT_API_KEY=...        # optional but recommended
#   BYBIT_API_SECRET=...     # optional but recommended
```

Get your Telegram user ID: send any message to @userinfobot

### 3. Run

```bash
cd docker
docker-compose up -d

# Check logs
docker-compose logs -f ingestion
docker-compose logs -f analyzer
docker-compose logs -f bot
```

### 4. Database Migration

```bash
# Runs automatically via the 'migrate' service
# Or manually:
docker-compose run --rm migrate
```

### 5. Use the Bot

Open Telegram, find your bot, send `/start`

---

## Local Development

```bash
# Install dependencies
pip install -e ".[dev]"

# Start infrastructure only
cd docker && docker-compose up -d postgres redis

# Run all services in one process (dev mode)
cp .env.example .env
# Fill in .env values
python -m app.main all

# Or run services separately:
python -m app.main ingestion   # terminal 1
python -m app.main analyzer    # terminal 2
python -m app.main bot         # terminal 3
```

### Run Tests

```bash
pytest tests/ -v

# With coverage
pytest tests/ --cov=app --cov-report=html
```

---

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message + menu |
| `/help` | All commands explained |
| `/signals` | Recent risk alerts (paginated) |
| `/overvalued` | Top overvalued coins right now |
| `/coin SYMBOL` | Full diagnostics for one coin (e.g. `/coin DOGEUSDT`) |
| `/watchlist` | Your personal watchlist |
| `/add SYMBOL` | Add coin to watchlist |
| `/remove SYMBOL` | Remove from watchlist |
| `/settings` | Configure alert preferences |
| `/status` | Bot health and universe size |

---

## Risk Score Explained

```
Score   Level     Action
0–24    🟢 LOW      No alert
25–49   🟡 MODERATE  No alert (unless in watchlist)
50–74   🟠 HIGH      Alert sent
75–100  🔴 CRITICAL  Urgent alert
```

**Alert fires only when**: score ≥ 50 AND ≥ 3 factors triggered (anti-noise).

**Cooldown**: same symbol + signal type will not re-alert for 60 minutes (configurable).

---

## Signal Types

| Signal | Trigger | Description |
|---|---|---|
| ⚠️ Early Warning | Score 30–49, 2+ factors | Early signs of overheating |
| 🔥 Overheated | RSI + VWAP + Volume all high | Classic pump top pattern |
| ⬇️ Reversal Risk | Momentum stall + wick + OB thinning | About to reverse |
| 💥 Dump Started | Price -3%+ + OB collapse | Dump in progress |

---

## Universe Filter

The bot only tracks coins passing all these filters:

| Filter | Default | Notes |
|---|---|---|
| Quote currency | USDT only | — |
| Exchange status | Trading | Skip suspended |
| Excluded majors | BTC ETH BNB SOL XRP ADA DOGE AVAX DOT MATIC LTC LINK UNI ATOM TRX | Configurable |
| Min 24h volume | $500k USDT | TODO: recalibrate |
| Min listing age | 14 days | Skip brand-new tokens |

Universe is refreshed every 5 minutes.

---

## Architecture

```
Bybit WS  →  IngestionService  →  Redis (features)
                                       ↓
                             AnalyzerService (scoring loop, 30s)
                                       ↓
                             AlertManager  →  Telegram Bot
                                       ↓
                             PostgreSQL (signals, overvalued, users)
```

See `docs/architecture.md` for full diagram.

---

## Project Structure

```
bybit-dump-detector/
├── app/
│   ├── main.py              — entry point (bot | ingestion | analyzer | all)
│   ├── config/
│   │   └── settings.py      — Pydantic settings from .env
│   ├── bybit/
│   │   ├── rest_client.py   — REST API client (instruments, klines, OB)
│   │   ├── ws_client.py     — WebSocket client (trades, tickers, OB)
│   │   └── universe.py      — Symbol universe manager
│   ├── analytics/
│   │   ├── features.py      — Feature calculator (12 signals)
│   │   └── orderbook.py     — OB mirror + delta/snapshot handling
│   ├── scoring/
│   │   └── engine.py        — Rule-based risk scoring (0–100)
│   ├── services/
│   │   ├── ingestion.py     — WS + REST data pipeline
│   │   ├── analyzer.py      — Scoring loop + overvalued ranking
│   │   └── alert_manager.py — Telegram broadcast
│   ├── bot/
│   │   ├── dispatcher.py    — aiogram bot + routers
│   │   ├── middleware.py    — Access control
│   │   ├── formatters.py    — Telegram message templates
│   │   ├── keyboards.py     — Inline keyboards
│   │   └── handlers/        — /start /signals /overvalued /coin etc.
│   ├── db/
│   │   ├── session.py       — SQLAlchemy async session factory
│   │   ├── models/          — ORM models (symbols, signals, users, ...)
│   │   └── migrations/      — DB migration runner
│   └── utils/
│       ├── logging.py       — Structured JSON logging (structlog)
│       └── time_utils.py    — UTC helpers
├── tests/
│   └── unit/
│       ├── test_scoring.py  — Scoring engine tests
│       └── test_features.py — Feature calculator tests
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── scripts/
│   └── init_db.sql          — PostgreSQL schema
├── docs/
│   ├── architecture.md      — System diagram + data source matrix
│   └── json_examples.md     — Signal / overvalued JSON schemas
├── .env.example
├── pyproject.toml
└── README.md
```

---

## Configuration Reference

All config via `.env` file. See `.env.example` for full list.

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | From @BotFather |
| `TELEGRAM_ALLOWED_USERS` | (empty = all) | Comma-separated Telegram IDs |
| `BYBIT_NETWORK` | mainnet | mainnet or testnet |
| `UNIVERSE_MIN_24H_VOLUME_USDT` | 500000 | Min daily volume |
| `UNIVERSE_MIN_LISTING_AGE_DAYS` | 14 | Skip new listings |
| `SCORE_ALERT_THRESHOLD` | 50 | Min score to alert |
| `ALERT_COOLDOWN_MINUTES` | 60 | Per-symbol cooldown |
| `OVERVALUED_TOP_N` | 20 | Size of overvalued list |

---

## MVP Roadmap

### Week 1 — Foundation
- [x] Project scaffold, Docker, config
- [ ] REST client: instruments, klines, tickers, orderbook
- [ ] WebSocket client: trades, tickers, OB
- [ ] Universe manager with filters
- [ ] Basic IngestionService: WS → FeatureCalculator → Redis
- [ ] Bot skeleton: /start /help /status
- [ ] Database schema created

### Week 2 — Features + Scoring + Signals
- [ ] Full FeatureCalculator: 12 factors (RSI, VWAP, ATR, volume z-score, OB imbalance, etc.)
- [ ] ScoringEngine: rule-based 0–100 with anti-noise
- [ ] AnalyzerService: scoring loop 30s
- [ ] AlertManager: Telegram broadcast with cooldown
- [ ] /signals command: real data from DB
- [ ] /overvalued command: real Redis data

### Week 3 — UX + Settings + Stability
- [ ] /coin SYMBOL: full live diagnostics
- [ ] /watchlist, /add, /remove: DB-backed
- [ ] /settings: inline keyboard settings editor
- [ ] Per-user alert preferences in DB
- [ ] Retention cleanup jobs (purge old signals)
- [ ] Reconnect stability testing
- [ ] Logging + error alerts to admin

### Week 4 — Tuning + Deployment Hardening
- [ ] Collect 7 days of scored features + post-signal price changes
- [ ] Calibrate scoring thresholds (reduce FP rate)
- [ ] Add Alembic migrations
- [ ] Health check endpoint
- [ ] Prometheus metrics (scoring cycles, alert counts, WS status)
- [ ] Production deployment docs (VPS, systemd or Docker Swarm)
- [ ] ML evolution plan: logistic regression on collected feature dataset

---

## ML Evolution Path (Week 4+)

After collecting ~1 week of live data:

```
features_at_signal_time  +  price_change_1h_after  →  label (dump? Y/N)
```

- Train: LogisticRegression or LightGBM on collected data
- Replace rule-based weight table with learned coefficients
- Evaluate: precision/recall at 0.5 threshold
- Monitor: daily retraining job

See `docs/architecture.md` for full details.

---

## Limitations & Caveats

1. **Spot only by default** — Open Interest and Funding Rate are only available for Bybit linear perpetuals. OI signals require parallel monitoring of USDT perpetual counterpart.

2. **No order book tick data** — Bybit WS provides 25-level OB snapshots. For microsecond trade clustering analysis you'd need institutional data feeds.

3. **Universe latency** — Universe refresh every 5 min. A coin listed and pumped within 5 minutes will be missed (min listing age filter also excludes very new coins by design).

4. **Thresholds need calibration** — All scoring thresholds are reasonable defaults based on general shitcoin patterns. They must be recalibrated after 1 week of live data for your specific target universe.

5. **Not financial advice** — Risk score is a statistical anomaly detector, not a guaranteed prediction. Use for informational monitoring only.
