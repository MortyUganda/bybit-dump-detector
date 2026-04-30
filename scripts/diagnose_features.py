"""Диагностика: какие features заполнены и почему пустые."""
import asyncio
import json

import redis.asyncio as aioredis

from app.config import get_settings


FIELDS = [
    "spread_pct", "bid_depth_change_5m", "bid_depth_usdt", "ask_depth_usdt",
    "ob_imbalance", "volume_24h_usdt", "momentum_loss_signal",
    "f_ob_bid_thinning", "f_spread_expansion", "f_momentum_loss",
    "consecutive_green_candles", "rsi_1m", "vwap_extension",
    "price_change_5m", "price_change_1h",
]


def fmt(v):
    if v is None:
        return "—"
    try:
        fv = float(v)
        return f"{fv:.3g}" if fv != 0 else "0"
    except Exception:
        return str(v)[:18]


async def fetch_features(r, key: str) -> dict:
    """Универсально: hash, JSON-string, или nothing."""
    t = await r.type(key)
    if t == "hash":
        return await r.hgetall(key)
    if t == "string":
        raw = await r.get(key)
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw[:200]}
    return {"_type": t}


async def main():
    s = get_settings()
    r = aioredis.from_url(s.redis_url, decode_responses=True)

    keys = await r.keys("features:*")
    print(f"Найдено {len(keys)} ключей features в Redis")

    if not keys:
        print("Нет данных features.")
        await r.aclose()
        return

    # Тип ключей
    t0 = await r.type(keys[0])
    print(f"Тип features-ключей: {t0}")

    # Возьмём первые 5
    print(f"\n=== Заполнение по 5 символам ===")
    header = f"{'symbol':14s} | " + " | ".join(f"{k[:14]:>14s}" for k in FIELDS)
    print(header)
    print("-" * len(header))

    samples = keys[:5]
    full_dump = None
    for key in samples:
        symbol = key.split(":")[-1]
        data = await fetch_features(r, key)
        if full_dump is None:
            full_dump = (symbol, data)
        row = [fmt(data.get(f)) for f in FIELDS]
        print(f"{symbol[:14]:14s} | " + " | ".join(f"{x:>14s}" for x in row))

    # Полный дамп одного символа
    if full_dump:
        sym, data = full_dump
        print(f"\n=== ПОЛНЫЙ DUMP {sym} (все поля) ===")
        if isinstance(data, dict):
            for k in sorted(data.keys()):
                print(f"  {k}: {data[k]}")

    # Глобальная статистика по всем ключам — сколько ненулевых
    print(f"\n=== Статистика по ВСЕМ {len(keys)} символам ===")
    counters = {f: {"present": 0, "nonzero": 0} for f in FIELDS}
    for key in keys:
        data = await fetch_features(r, key)
        if not isinstance(data, dict):
            continue
        for f in FIELDS:
            v = data.get(f)
            if v is None or v == "":
                continue
            counters[f]["present"] += 1
            try:
                if float(v) != 0:
                    counters[f]["nonzero"] += 1
            except Exception:
                pass
    print(f"{'feature':30s} | present | nonzero")
    print("-" * 56)
    for f in FIELDS:
        c = counters[f]
        print(f"{f:30s} | {c['present']:7d} | {c['nonzero']:7d}")

    # OB ключи
    ob_keys = await r.keys("ob:*")
    print(f"\n=== Order book: {len(ob_keys)} ключей ===")
    if ob_keys:
        for k in ob_keys[:3]:
            t = await r.type(k)
            print(f"  {k} (тип={t})")
            if t == "string":
                raw = await r.get(k)
                try:
                    p = json.loads(raw)
                    bids = p.get("bids", [])
                    asks = p.get("asks", [])
                    print(f"    bids={len(bids)} asks={len(asks)} ts={p.get('timestamp')}")
                    if bids:
                        print(f"    best bid: {bids[0]}  best ask: {asks[0]}")
                except Exception as e:
                    print(f"    JSON parse err: {e}, raw[:100]={raw[:100]}")
            elif t == "hash":
                d = await r.hgetall(k)
                print(f"    fields: {list(d.keys())[:10]}")

    # Доп: есть ли отдельные ключи orderbook:* / depth:* / ticker:*
    for prefix in ("orderbook:", "depth:", "ticker:", "ob_snapshot:"):
        cnt = len(await r.keys(prefix + "*"))
        if cnt:
            print(f"  Доп. ключи {prefix}*: {cnt}")

    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
