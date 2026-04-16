# Bybit Dump Detector 🔍

Telegram bot that monitors speculative coins on Bybit for overheating and dump risk.

Not a price prediction tool. It is an anomaly detector that combines multiple analytical factors into a composite risk score from 0 to 100.

***

## What It Does

- Monitors speculative Bybit markets using live market data
- Computes a multi-factor overheating and dump-risk profile per coin
- Produces a composite Risk Score from 0 to 100
- Sends Telegram alerts for Early Warning, Overheated, Reversal Risk, and Dump Started scenarios
- Maintains ranked overvalued coin lists
- Supports per-user settings, watchlists, and signal filtering
- Includes strategy runtime controls and auto-short related flows

***

## Project Status

Active development. The core detection pipeline, scoring engine, bot commands, user settings, and alert delivery are already implemented. The current focus is calibration, reliability, observability, and preparing a production-quality dataset for ML-assisted scoring.

***

## Quick Start (Docker)

### 1. Prerequisites

- Docker + Docker Compose
- Telegram Bot Token (from @BotFather)
- Bybit API Key (optional for some public data flows, but recommended for more stable access)

### 2. Configure

```bash
cp .env.example .env
# Edit .env:
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_ALLOWED_USERS=your_telegram_user_id
#   BYBIT_API_KEY=...
#   BYBIT_API_SECRET=...
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
# Runs automatically via the migrate service
# Or manually:
docker-compose run --rm migrate
```

### 5. Use the Bot

Open Telegram, find your bot, send `/start`

***

## Dev Environment (тестовый экземпляр)

Dev-экземпляр работает параллельно со стабильным (prod) на том же сервере. Используется отдельный Telegram-бот, отдельная БД и Redis.

### 1. Настройка

```bash
cp .env.dev.example .env.dev
# Отредактируйте .env.dev:
#   TELEGRAM_BOT_TOKEN=<токен ВТОРОГО бота из @BotFather>
#   BYBIT_API_KEY=...
#   BYBIT_API_SECRET=...
```

### 2. Запуск dev

```bash
docker compose -f docker/docker-compose.dev.yml up -d --build
```

### 3. Логи dev

```bash
docker compose -f docker/docker-compose.dev.yml logs -f bot
docker compose -f docker/docker-compose.dev.yml logs -f analyzer
```

### 4. Работа с веткой dev

```bash
# Переключиться на dev-ветку и пересобрать
git checkout dev
docker compose -f docker/docker-compose.dev.yml up -d --build

# Перенести изменения в стабильный
git checkout main
git merge dev
docker compose -f docker/docker-compose.yml up -d --build
```

### 5. Остановка dev

```bash
docker compose -f docker/docker-compose.dev.yml down
```

### Порты dev vs prod

| Сервис   | Prod  | Dev   |
|----------|-------|-------|
| Postgres | 5433  | 5434  |
| Redis    | 6380  | 6381  |

***

## Local Development

```bash
# Install dependencies
pip install -e ".[dev]"

# Start infrastructure only
cd docker && docker-compose up -d postgres redis

# Run all services in one process (dev mode)
cp .env.example .env
python -m app.main all

# Or run services separately:
python -m app.main ingestion
python -m app.main analyzer
python -m app.main bot
```

### Run Tests

```bash
pytest tests/ -v

# With coverage
pytest tests/ --cov=app --cov-report=html
```

***

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message and main menu |
| `/help` | Help and command reference |
| `/signals` | Recent risk alerts |
| `/overvalued` | Top overvalued coins right now |
| `/coin SYMBOL` | Full diagnostics for one coin |
| `/watchlist` | Your personal watchlist |
| `/add SYMBOL` | Add coin to watchlist |
| `/remove SYMBOL` | Remove coin from watchlist |
| `/settings` | Configure alert preferences |
| `/status` | Bot and service health snapshot |

***

## Detection Model

The current engine is rule-based and combines multiple market structure signals into one score.

### Implemented feature groups

- Trade-flow features: buy/sell imbalance, large trade clustering, CVD, liquidation cascade detection
- Candle features: RSI, ATR, VWAP extension, price acceleration, consecutive green candles, wick analysis, momentum loss
- Order book features: bid/ask depth, imbalance, spread expansion, bid thinning
- Context features: trend filter, OI change metrics, funding data where available

### Risk levels

```text
Score   Level
0–24    LOW
25–49   MODERATE
50–74   HIGH
75–100  CRITICAL
```

### Signal types

- Early Warning
- Overheated
- Reversal Risk
- Dump Started

The model currently uses heuristic thresholds and adaptive normalization. Threshold calibration on collected live data is still in progress.

***

## Alert Logic

Alerts are filtered per user.

Supported controls include:
- Alerts enabled/disabled
- Quiet mode
- Minimum score threshold
- Signal-type specific preferences

The system also supports runtime strategy configuration and auto-short related flows for actionable signal types.

***

## Universe Filter

The tracked market universe is filtered to avoid major coins and low-quality symbols.

Typical filters include:
- USDT quote symbols
- Trading status only
- Excluded majors
- Minimum 24h volume threshold
- Minimum listing age threshold

Universe refresh and exact thresholds are configurable.

***

## Architecture

```text
Bybit WS / REST  →  IngestionService  →  Redis / in-memory state
                                         ↓
                               AnalyzerService / ScoringEngine
                                         ↓
                              AlertManager  →  Telegram Bot
                                         ↓
                             PostgreSQL / signal persistence
```

Main runtime parts:
- `IngestionService` collects and updates market state
- `FeatureCalculator` builds per-symbol analytical features
- `ScoringEngine` converts features into a risk score and signal type
- `AlertManager` saves and broadcasts alerts
- Telegram bot handlers provide command UX, settings, watchlists, and navigation

***

## Project Structure

```text
bybit-dump-detector/
├── app/
│   ├── main.py
│   ├── analytics/
│   │   ├── features.py
│   │   └── orderbook.py
│   ├── bot/
│   │   ├── dispatcher.py
│   │   ├── formatters.py
│   │   ├── middleware.py
│   │   ├── user_store.py
│   │   ├── keyboards/
│   │   └── handlers/
│   │       ├── commands.py
│   │       ├── signals.py
│   │       ├── overvalued.py
│   │       ├── coin.py
│   │       ├── settings.py
│   │       ├── watchlist.py
│   │       ├── history.py
│   │       ├── strategy.py
│   │       └── auto_shorts.py
│   ├── bybit/
│   ├── config/
│   ├── db/
│   ├── scoring/
│   │   └── engine.py
│   ├── services/
│   │   ├── ingestion.py
│   │   ├── analyzer.py
│   │   ├── alert_manager.py
│   │   └── runtime_config.py
│   └── utils/
├── docker/
├── docs/
├── scripts/
├── tests/
├── .env.example
├── pyproject.toml
└── README.md
```

***

## Configuration Reference

All config is provided through `.env`. See `.env.example` for the full list.

Common options include:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_ALLOWED_USERS` | Comma-separated allowed Telegram user IDs |
| `BYBIT_NETWORK` | `mainnet` or `testnet` |
| `UNIVERSE_MIN_24H_VOLUME_USDT` | Minimum daily volume |
| `UNIVERSE_MIN_LISTING_AGE_DAYS` | Minimum listing age |
| `SCORE_ALERT_THRESHOLD` | Minimum score for alerts |
| `ALERT_COOLDOWN_MINUTES` | Per-symbol cooldown |
| `OVERVALUED_TOP_N` | Ranked overvalued list size |

***

## Roadmap

### Done
- [x] Project scaffold, Docker setup, configuration system, and environment template
- [x] FeatureCalculator with real trade, candle, order book, OI, CVD, liquidation, and trend-context features
- [x] Rule-based ScoringEngine with 17 weighted factors, adaptive thresholds, anti-noise logic, and signal classification
- [x] Telegram bot dispatcher, middleware, formatters, keyboards, and command handlers
- [x] Commands for `/start`, `/help`, `/status`, `/signals`, `/overvalued`, `/coin`, `/watchlist`, `/add`, `/remove`, and `/settings`
- [x] Per-user alert filtering with quiet mode, minimum score threshold, and signal-type preferences
- [x] Signal persistence hooks, Redis integration, and candle restore after restart
- [x] Runtime strategy configuration and auto-short related handlers

### In Progress
- [ ] Calibrate scoring thresholds on real collected market data
- [ ] Expand automated test coverage and validate end-to-end production scenarios
- [ ] Improve reconnect behavior, recovery flow, and long-running service stability
- [ ] Measure false-positive rate and tune signal quality in live conditions
- [ ] Validate spot-to-perpetual mapping quality for OI and funding-based signals

### Next
- [ ] Add proper Alembic migrations
- [ ] Add health checks and operational observability
- [ ] Add Prometheus metrics for scoring cycles, alerts, and WS health
- [ ] Add retention and cleanup jobs for old signals and temporary analytics data
- [ ] Write production deployment documentation for VPS and container-based setups
- [ ] Build a dataset collection pipeline for ML training from live signals and post-signal outcomes

### Later
- [ ] Train and compare first ML models, such as Logistic Regression and LightGBM
- [ ] Introduce retraining and evaluation workflows with precision/recall monitoring
- [ ] Revisit universe filters and scoring weights after enough live history is collected
- [ ] Split roadmap and changelog responsibilities so README stays concise and current

***

## ML Evolution Path

After enough live data is collected, the next step is to build a supervised dataset:

```text
features_at_signal_time + post_signal_price_change → label (dump? yes/no)
```

Planned direction:
- Train Logistic Regression or LightGBM on collected signal snapshots
- Compare ML probability against the current rule-based score
- Evaluate precision, recall, and threshold sensitivity
- Add retraining and monitoring only after the baseline dataset is stable

***

## Limitations & Caveats

1. Spot and perp signals are not equally available for all symbols.
2. Thresholds are still being calibrated on live market behavior.
3. Very new listings or ultra-fast pumps can still be missed depending on universe refresh timing.
4. This is a risk-monitoring and anomaly-detection tool, not financial advice and not guaranteed prediction.

***

## License

Add your preferred license here.