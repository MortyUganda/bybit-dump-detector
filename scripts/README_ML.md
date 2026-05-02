# ML-скрипты — быстрый запуск

Запускать **из корня проекта** (`C:\Users\Sergei\Desktop\bybit-dump-detector`).

Понадобится `python` в PATH с пакетами: `pandas`, `numpy`, `scikit-learn`, `lightgbm`.

```powershell
pip install pandas numpy scikit-learn lightgbm
```

---

## Самый частый сценарий: выгрузить свежие данные и прогнать обе модели

```powershell
.\scripts\run_ml.ps1 -Mode all
```

Что делает:
1. Запускает `export_trades_csv` внутри dev2 bot → выгружает `auto_shorts.csv` и `canceled_signals.csv` в `/app/exports`
2. Копирует их в текущую директорию через `docker cp`
3. Запускает `train_outcome_model.py` (AUC по реальным сделкам)
4. Запускает `train_decision_model.py` (унифицированная модель прибыльности)

---

## Только обучение на уже имеющихся CSV

Файлы должны лежать в текущей директории (`auto_shorts_*.csv`, `canceled_signals_*.csv`) — скрипты сами найдут самые свежие по времени модификации.

```powershell
.\scripts\run_ml.ps1
```

или по-отдельности:

```powershell
.\scripts\run_ml.ps1 -Mode outcome
.\scripts\run_ml.ps1 -Mode decision
```

---

## Только выгрузка CSV (без обучения)

```powershell
.\scripts\run_ml.ps1 -Mode export
```

---

## Параметры

| Параметр | Дефолт | Что делает |
|---|---|---|
| `-Mode` | `run` | `run` / `outcome` / `decision` / `export` / `all` |
| `-MinId` | `449` | Минимальный id для outcome (фильтр старых данных без OB-фичей) |
| `-Splits` | `5` | Число фолдов TimeSeriesSplit |
| `-AutoCsv` | (latest) | Принудительный путь к auto_shorts CSV |
| `-CanceledCsv` | (latest) | Принудительный путь к canceled_signals CSV |

Примеры:

```powershell
# Обучить только на сделках начиная с id=700
.\scripts\run_ml.ps1 -Mode outcome -MinId 700

# Использовать конкретный CSV
.\scripts\run_ml.ps1 -Mode decision -AutoCsv "exports\auto_shorts_20260510_120000.csv"

# Больше фолдов когда накопится 1500+ сделок
.\scripts\run_ml.ps1 -Splits 8
```

---

## Альтернатива: cmd.exe / PS без скриптов

Если PowerShell-скрипт не работает (политика выполнения), используй .bat:

```cmd
scripts\run_ml.bat all
scripts\run_ml.bat outcome
scripts\run_ml.bat decision
```

Или вызывать Python напрямую:

```cmd
python -m scripts.train_outcome_model
python -m scripts.train_outcome_model --csv my.csv --min-id 500 --splits 8

python -m scripts.train_decision_model
python -m scripts.train_decision_model --auto-csv my_auto.csv --canceled-csv my_canc.csv
```

---

## Что какой скрипт делает

### `train_outcome_model.py`
Учит **на реально открытых сделках**: `ml_label=1` (TP_hit) vs `ml_label=0` (SL_hit).
Только `auto_shorts` с id≥449 (когда уже работали OB-фичи).

**Что показывает:**
- AUC по 5 фолдам (Time-aware)
- Топ-20 фичей по gain importance

**Когда полезен:** оценить «насколько хорошо могу предсказать исход СВОЕЙ сделки».

### `train_decision_model.py`
Учит **на полной выборке прибыльности** — auto_shorts + canceled_signals:
- positive: `auto.ml_label=1` или `canceled.would_hit_tp=true`
- negative: `auto.ml_label=0` или `canceled.would_hit_sl=true`
- skip: canceled с `neither` (мутный класс)

**Что показывает:**
- AUC по фолдам
- Feature importance
- OOF-симуляция ML-фильтра по разным `proba`-порогам (0.45..0.70)

**Когда полезен:** найти оптимальный порог для production ML-фильтра.

---

## Если ошибки

**`docker cp dd_bot_dev2:/app/exports/...` не находит контейнер:**
Имя контейнера может отличаться — проверь `docker compose -p dev2 ps` и поправь в `.bat`/`.ps1` если нужно.

**`Слишком мало сделок для обучения`:**
Накопится ещё. Минимум для outcome — 50 сделок с `id≥449` после деплоя OB-фичей.

**`Невозможно: один класс в выборке`:**
В одном из фолдов все сделки win или все loss. Подожди ещё данных.

---

## Новый датасет: `all_opened_signals`

Файл `all_opened_signals_*.csv` — **золотой датасет** для ML.

Содержит shadow-paper сделку по **каждому** risk-сигналу:
- TP/SL всегда 10%/10%, без таймаута
- Поле `would_have_opened` — открыл бы реальный бот (прошёл все фильтры)
- Поле `actual_blocked_by` — причина блокировки (`trend_filter`, `cancel_drop`, `score_dropped`, `strategy_disabled`, `duplicate` и т.д.), NULL если открыл
- `linked_auto_short_id` / `linked_canceled_signal_id` — связь с реальными таблицами

**ML-target:** `ml_label` = 1 (tp_hit, прибыльный) или 0 (sl_hit, убыточный) — **без bias** текущих фильтров.

Экспортируется автоматически через `export_trades_csv.py` вместе с остальными.
