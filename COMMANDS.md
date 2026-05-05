# Bybit Dump Detector — шпаргалка команд

Последнее обновление: 2026-05-05

Три окружения работают параллельно:
- **dev2** — стабильная ветка с фильтрами + ML-gate (порт postgres 5435, redis 6382)
- **dev3-no-filters** — экспериментальная без фильтров отмены (порт postgres 5445, redis 6392)
- **main** — production с trailing stop, отдельная БД

---

## 🐳 Docker

| Действие | dev2 | dev3-no-filters | main |
|---|---|---|---|
| Запуск всего | `docker compose -p dev2 -f docker/docker-compose.dev2.yml up -d --build` | `docker compose -p dev3 -f docker/docker-compose.dev3.yml --env-file .env.dev3 up -d --build` | `docker compose up -d --build` |
| Перезапуск бота | `docker compose -p dev2 -f docker/docker-compose.dev2.yml up -d --build bot` | `docker compose -p dev3 -f docker/docker-compose.dev3.yml --env-file .env.dev3 up -d --build bot` | `docker compose up -d --build bot` |
| Остановить | `docker compose -p dev2 -f docker/docker-compose.dev2.yml down` | `docker compose -p dev3 -f docker/docker-compose.dev3.yml down` | `docker compose down` |
| Список контейнеров | `docker compose -p dev2 -f docker/docker-compose.dev2.yml ps` | `docker compose -p dev3 -f docker/docker-compose.dev3.yml ps` | `docker compose ps` |

> ВАЖНО: всегда с флагом `--build`, иначе бот запустится на старом образе.

---

## 📜 Логи

| Действие | dev2 | dev3-no-filters |
|---|---|---|
| Бот (live) | `docker compose -p dev2 -f docker/docker-compose.dev2.yml logs -f bot` | `docker compose -p dev3 -f docker/docker-compose.dev3.yml logs -f bot` |
| Analyzer (live) | `docker compose -p dev2 -f docker/docker-compose.dev2.yml logs -f analyzer` | `docker compose -p dev3 -f docker/docker-compose.dev3.yml logs -f analyzer` |
| Поиск ML-gate | `docker compose -p dev2 -f docker/docker-compose.dev2.yml logs bot \| findstr "ML-gate"` | n/a (нет ML-gate) |
| Поиск BTC suppress | `... logs analyzer \| findstr "BTC suppress"` | то же |
| Последние 200 строк | `... logs --tail=200 bot` | то же |

---

## 🗄️ Postgres

```powershell
# dev2
docker compose -p dev2 -f docker/docker-compose.dev2.yml exec postgres psql -U dumpuser -d dumpdetector

# dev3
docker compose -p dev3 -f docker/docker-compose.dev3.yml exec postgres psql -U dumpuser -d dumpdetector
```

### Быстрая статистика
```powershell
docker compose -p dev2 -f docker/docker-compose.dev2.yml exec postgres psql -U dumpuser -d dumpdetector -c "SELECT COUNT(*) AS auto_shorts FROM auto_shorts; SELECT COUNT(*) AS canceled FROM canceled_signals; SELECT COUNT(*) AS all_opened FROM all_opened_signals;"
```

### WR за последние сутки
```sql
SELECT
  date_trunc('hour', entry_ts) AS h,
  COUNT(*) AS n,
  AVG(ml_label::int) AS wr
FROM auto_shorts
WHERE status='closed' AND entry_ts > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 1;
```

### Топ блокировок
```sql
SELECT actual_blocked_by, COUNT(*) FROM canceled_signals
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 2 DESC;
```

---

## 🔧 Redis (runtime_config)

### Просмотр настроек
```powershell
docker compose -p dev2 -f docker/docker-compose.dev2.yml exec redis redis-cli HGETALL runtime_config
```

### Изменение настроек

| Настройка | Команда |
|---|---|
| ML порог (dev2) | `... exec redis redis-cli HSET runtime_config ml_decision_threshold 0.65` |
| Минимальный score | `... exec redis redis-cli HSET runtime_config min_score_to_enter 45` |
| Adverse move порог | `... exec redis redis-cli HSET runtime_config adverse_move_threshold_pct 0.2` |

### Полный сброс кэша Redis (осторожно!)
```powershell
docker compose -p dev2 -f docker/docker-compose.dev2.yml exec redis redis-cli FLUSHDB
```

---

## 🤖 ML-пайплайн (dev2)

```powershell
.\scripts\run_ml.ps1 -Mode all       # export CSV + outcome + decision (+ сохранит models/decision_model.pkl)
.\scripts\run_ml.ps1 -Mode decision  # только decision на свежих CSV
.\scripts\run_ml.ps1 -Mode outcome   # только outcome
.\scripts\run_ml.ps1 -Mode export    # только выгрузка CSV
.\scripts\run_ml.ps1 -Mode diagnose  # drift по фолдам
.\scripts\run_ml.ps1 -MinId 700      # outcome с другим min_id
.\scripts\run_ml.ps1 -Splits 8       # кастомное число фолдов
```

После пересборки модели: перезапустить бота с `--build`, чтобы ML-gate подхватил новый pkl.

---

## 📦 Экспорт CSV из контейнера

```powershell
# dev2
docker cp $(docker compose -p dev2 -f docker/docker-compose.dev2.yml ps -q bot):/app/exports/. ./exports/

# dev3
docker cp $(docker compose -p dev3 -f docker/docker-compose.dev3.yml ps -q bot):/app/exports/. ./exports/
```

---

## 🌿 Git

```powershell
git fetch origin
git checkout dev2 && git pull origin dev2
git checkout dev3-no-filters && git pull origin dev3-no-filters
git log --oneline -10
git status
```

> Push: только в `dev2` и `dev3-no-filters`. Никогда в `main`.

---

## 🚨 Аварийные команды

| Проблема | Команда |
|---|---|
| Бот упал, нужны логи | `docker compose -p dev2 -f docker/docker-compose.dev2.yml logs --tail=500 bot` |
| Зависла миграция | `docker compose -p dev2 -f docker/docker-compose.dev2.yml restart bot` |
| Перезапуск Postgres | `docker compose -p dev2 -f docker/docker-compose.dev2.yml restart postgres` |
| Очистка ML таблиц (dev2) | `... exec postgres psql -U dumpuser -d dumpdetector -c "TRUNCATE auto_shorts, canceled_signals, all_opened_signals RESTART IDENTITY CASCADE;"` |
| Удалить контейнеры + volumes (потеря данных!) | `docker compose -p dev2 -f docker/docker-compose.dev2.yml down -v` |

---

## 🎯 Telegram настройки (dev2)

В чате с ботом:
- `/strategy` → меню настроек
  - Минимальный score
  - ML порог (decision_threshold)
  - Adverse move %
  - Включение/отключение фильтров
- `/auto_shorts` — список активных авто-шортов
- `/stats` — статистика WR

---

## 📊 Ключевые пороги (рекомендации на 2026-05-05)

| Параметр | Текущее | Рекомендация | Причина |
|---|---|---|---|
| `min_score_to_enter` | 45 | 45 | Оставить |
| `ml_decision_threshold` | 0.50 | **0.65** | На 2184 сделках лучший WR +4.6% |
| `adverse_move_threshold_pct` | 0.2 | 0.2 | Оставить |
| `SCORE_MIN_THRESHOLD` (signals.py) | 35 | 35 | Не менять (фильтр мусора) |

---

## ⚠️ Правила работы

1. **Всегда с `--build`** при перезапуске бота
2. **Перед debug** убедиться что код в контейнере свежий: `git log --oneline -1` локально совпадает с тем что в контейнере
3. **dev2 и dev3 имеют разные БД** — данные не смешивать
4. **Push только в dev2 и dev3-no-filters**, никогда не в main
5. **pyflakes + py_compile** после каждой пачки изменений
