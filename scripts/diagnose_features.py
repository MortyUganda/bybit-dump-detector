"""Диагностика: какие features заполнены и почему пустые."""
import asyncio
import json

import redis.asyncio as aioredis

from app.config import get_settings


async def main():
    s = get_settings()
    r = aioredis.from_url(s.redis_url, decode_responses=True)
    
    keys = await r.keys("features:*")
    print(f"Найдено {len(keys)} ключей features в Redis")
    
    if not keys:
        print("Нет данных features. Бот ещё не успел вычислить.")
        return
    
    # Возьмём 5 случайных
    fields_to_check = [
        "spread_pct", "bid_depth_change_5m", "bid_depth_usdt", "ask_depth_usdt",
        "ob_imbalance", "volume_24h_usdt", "momentum_loss_signal", 
        "f_ob_bid_thinning", "f_spread_expansion", "f_momentum_loss",
        "consecutive_green_candles", "rsi_1m", "vwap_extension",
    ]
    
    print(f"\nПример заполнения первых 5 символов:")
    print(f"{'symbol':15s} | " + " | ".join(f"{k[:18]:>18s}" for k in fields_to_check))
    print("-" * (15 + 3 + len(fields_to_check) * 21))
    
    for key in keys[:5]:
        symbol = key.split(":")[-1]
        data = await r.hgetall(key)
        row = []
        for f in fields_to_check:
            v = data.get(f, "—")
            if v == "—":
                row.append("—")
            else:
                try:
                    fv = float(v)
                    row.append(f"{fv:.3g}" if fv != 0 else "0")
                except:
                    row.append(str(v)[:18])
        print(f"{symbol[:15]:15s} | " + " | ".join(f"{x:>18s}" for x in row))
    
    # Проверим OB ключи
    ob_keys = await r.keys("ob:*")
    print(f"\nOrder book ключей в Redis: {len(ob_keys)}")
    if ob_keys:
        ob_data = await r.get(ob_keys[0])
        ob_parsed = json.loads(ob_data)
        print(f"Пример {ob_keys[0]}:")
        print(f"  bids: {len(ob_parsed.get('bids', []))} levels")
        print(f"  asks: {len(ob_parsed.get('asks', []))} levels")
        if ob_parsed.get('bids'):
            print(f"  best bid: {ob_parsed['bids'][0]}")
            print(f"  best ask: {ob_parsed['asks'][0]}")
    
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
