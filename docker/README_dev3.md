# dev3-no-filters — параллельный запуск

## Запуск
```bash
docker compose -p dev3 -f docker/docker-compose.dev3.yml up -d --build
```

## Остановка
```bash
docker compose -p dev3 -f docker/docker-compose.dev3.yml down
```

## Логи
```bash
docker compose -p dev3 -f docker/docker-compose.dev3.yml logs -f bot
```

## Postgres
```bash
docker compose -p dev3 -f docker/docker-compose.dev3.yml exec postgres psql -U dumpuser -d dumpdetector
```

## Перед первым запуском

Скопируй `.env.dev3.example` в `.env.dev3` и заполни:

```bash
cp .env.dev3.example .env.dev3
```

В `.env.dev3` обязательно пропиши **отдельный** Telegram-токен:
```
TELEGRAM_BOT_TOKEN=<токен нового бота из @BotFather>
```

Иначе будет конфликт `getUpdates` с dev2.

## Порты (на хосте)

| Сервис   | dev2  | dev3  | Контейнер |
|----------|-------|-------|-----------|
| postgres | 5435  | 5445  | 5432      |
| redis    | 6382  | 6392  | 6379      |

## Volumes

| dev2                 | dev3                 |
|----------------------|----------------------|
| postgres_data_dev2   | postgres_data_dev3   |
| redis_data_dev2      | redis_data_dev3      |

## Network

Используется изолированная сеть `dd_network_dev3` (bridge).
