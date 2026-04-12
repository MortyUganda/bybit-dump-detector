# ML-пайплайн для bybit-dump-detector

Эта папка содержит данные и артефакты для ML-экспериментов поверх живого бота.

## Структура

- `data/raw/`
  - `ml_opened_vs_canceled.parquet`  
    Снэпшот view `ml_opened_vs_canceled` из Postgres: объединённые сделки,
    куда бот зашёл (`auto_shorts`), и отменённые сигналы (`canceled_signals`).
  - `ml_opened_only_profitable.parquet`  
    Снэпшот view `ml_opened_only_profitable`: только закрытые сделки из
    `auto_shorts` с меткой профит/убыток.
- `data/processed/`
  Подготовленные датасеты для конкретных экспериментов (фильтрация,
  обработка пропусков, отбор фич).
- `notebooks/`
  EDA и baseline-модели.
- `models/`
  Сохранённые обученные модели и метрики.
- `scripts/`
  Вспомогательные скрипты (экспорт данных, подготовка датасетов и т.п.).

## Целевые переменные

### 1. Решение о входе (entry decision)

Источник: `data/raw/ml_opened_vs_canceled.parquet`.

- `entry_decision_label`:
  - `1` — бот **вошёл** в сделку (строки из `auto_shorts`);
  - `0` — бот **отменил** сигнал (строки из `canceled_signals`).
- Типовая задача: бинарная классификация *«входить / не входить»*.

Дополнительные полезные поля:
- `source_type` — `"opened"` или `"canceled"` (откуда пришла строка),
- `signal_score`, `final_score`, `min_score_at_entry`,
- фичи `f_*`, рыночные показатели (`realized_vol_1h`, `volume_24h_usdt`,
  `price_change_5m`, `price_change_1h`, `spread_pct`, `btc_change_15m`,
  `funding_rate_at_signal`, `oi_change_pct_at_signal`, `trend_strength_1h` и т.д.).

### 2. Профитность сделки (pnl model)

Источник: `data/raw/ml_opened_only_profitable.parquet`.

- `ml_label`:
  - `1` — сделка закрыта с **прибылью** (`pnl_pct > 0`);
  - `0` — сделка закрыта с **убытком**.
- Типовая задача: бинарная классификация *«будет ли сделка профитной»* среди тех,
  куда бот уже вошёл.

Полезные поля:
- `pnl_pct`, `close_reason`,
- те же фичи `f_*` и рыночные признаки, что и выше,
- параметры входа: `entry_mode`, `triggered_count`, `entry_delay_sec`,
  `score` / `entry_score` / `min_score_at_entry`.

## Обновление raw-данных

1. Обновить view в Postgres (если нужно).
2. Запустить экспорт (из каталога проекта):

   ```powershell
   cd C:\Users\Sergei\Desktop\bybit-dump-detector\docker

   docker compose exec -T postgres psql -U dumpuser -d dumpdetector ^
     -c "COPY (SELECT * FROM ml_opened_vs_canceled) TO STDOUT WITH CSV HEADER" ^
     > ..\ml_dumps\ml_opened_vs_canceled.csv

   docker compose exec -T postgres psql -U dumpuser -d dumpdetector ^
     -c "COPY (SELECT * FROM ml_opened_only_profitable) TO STDOUT WITH CSV HEADER" ^
     > ..\ml_dumps\ml_opened_only_profitable.csv
   ```

3. Конвертировать CSV → Parquet в `ml/data/raw` (см. `scripts/export_views_to_parquet.py`
   или одноразовый `convert_to_parquet.py`).

После этого все эксперименты должны читать данные **только** из `ml/data/raw` или
`ml/data/processed`, а не напрямую из боевой БД.