#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FETCH MINUTE KLINES — тянет 1-минутные свечи Bybit за час после каждого сигнала.

Зачем: чтобы ЧЕСТНО проверить TP/SL, нужен реальный порядок движения цены внутри
часа (а не только экстремумы price_min/max_60m). Этот скрипт по каждому сигналу
(symbol + signal_ts) запрашивает публичный endpoint Bybit /v5/market/kline
с interval=1 за окно [signal_ts, signal_ts + 60м] и складывает всё в один файл.

ВАЖНО: запускать ЛОКАЛЬНО (у тебя на машине). Из облака Bybit CloudFront
блокирует доступ по гео (HTTP 403). У тебя в DE работает.

Особенности:
  - кэш: уже скачанные (symbol, ts) пропускаются при повторном запуске
  - троттлинг: пауза между запросами, чтобы не словить rate-limit
  - устойчивость: при обрыве просто перезапусти — продолжит с места
  - выход: minute_klines.parquet (или .csv) — long-формат:
      signal_id, symbol, signal_ts, minute (0..60), ts, open, high, low, close

Запуск:
    python scripts/fetch_minute_klines.py --canceled canceled_signals_XXXX.csv
    python scripts/fetch_minute_klines.py --canceled XXXX.csv --auto auto_shorts_XXXX.csv
    python scripts/fetch_minute_klines.py --canceled XXXX.csv --out minute_klines.parquet --sleep 0.15

Зависимости: pandas requests  (pyarrow для parquet, иначе сохранит csv)
"""
from __future__ import annotations
import argparse, os, sys, time, json
import pandas as pd, requests

BYBIT_HOSTS = ["https://api.bybit.com", "https://api.bytick.com"]
WINDOW_MIN = 60  # сколько минут после сигнала тянем


def get_klines(symbol, start_ms, end_ms, session, retries=3):
    """1-мин свечи [start,end]. Bybit отдаёт newest-first; вернём ascending list
    из [ts, open, high, low, close]."""
    params = {"category": "linear", "symbol": symbol, "interval": "1",
              "start": int(start_ms), "end": int(end_ms), "limit": 200}
    last_err = None
    for host in BYBIT_HOSTS:
        for attempt in range(retries):
            try:
                r = session.get(f"{host}/v5/market/kline", params=params, timeout=15)
                if r.status_code == 200:
                    j = r.json()
                    if j.get("retCode") == 0:
                        lst = j["result"]["list"]
                        # [ts, o, h, l, c, vol, turnover] строками; делаем ascending
                        out = [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])]
                               for x in lst]
                        out.sort(key=lambda z: z[0])
                        return out
                    last_err = j.get("retMsg")
                elif r.status_code == 429:
                    time.sleep(1.0 + attempt)  # rate limit
                    last_err = "429 rate limit"
                else:
                    last_err = f"HTTP {r.status_code}"
            except Exception as e:
                last_err = str(e)
            time.sleep(0.3 * (attempt + 1))
    return None  # не удалось


def collect(df, id_col, sym_col, ts_col, session, sleep, done_keys, writer_rows):
    n = len(df); ok = 0; fail = 0
    for i, row in df.iterrows():
        sym = str(row[sym_col]); sig = row[ts_col]
        try:
            t0 = pd.to_datetime(sig, utc=True)
        except Exception:
            continue
        sid = str(row[id_col]) if id_col in df.columns else f"{sym}_{int(t0.timestamp())}"
        key = (sym, int(t0.timestamp()))
        if key in done_keys:
            continue
        start_ms = int(t0.timestamp() * 1000)
        end_ms = start_ms + WINDOW_MIN * 60 * 1000
        kl = get_klines(sym, start_ms, end_ms, session)
        if kl is None:
            fail += 1
        else:
            for minute, (ts, o, h, l, c) in enumerate(kl):
                writer_rows.append((sid, sym, str(sig), minute, ts, o, h, l, c))
            done_keys.add(key)
            ok += 1
        if (ok + fail) % 50 == 0:
            print(f"  ...{ok+fail}/{n}  (ok={ok}, fail={fail})", flush=True)
        time.sleep(sleep)
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--canceled", default=None)
    ap.add_argument("--auto", default=None)
    ap.add_argument("--out", default="minute_klines.parquet")
    ap.add_argument("--sleep", type=float, default=0.12, help="пауза между запросами, сек")
    args = ap.parse_args()
    if not args.canceled and not args.auto:
        print("укажи --canceled и/или --auto"); sys.exit(1)

    # кэш: если файл есть — продолжаем
    done_keys = set(); existing_rows = []
    if os.path.exists(args.out):
        try:
            prev = pd.read_parquet(args.out) if args.out.endswith(".parquet") else pd.read_csv(args.out)
            for _, r in prev[["symbol", "signal_ts"]].drop_duplicates().iterrows():
                done_keys.add((str(r["symbol"]), int(pd.to_datetime(r["signal_ts"], utc=True).timestamp())))
            existing_rows = prev.values.tolist()
            print(f"кэш: {len(done_keys)} сигналов уже скачано, продолжаю\n")
        except Exception as e:
            print(f"не смог прочитать кэш ({e}), начинаю заново\n")

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    rows = []

    for tag, path, idc, symc, tsc in [
        ("canceled", args.canceled, "id", "symbol", "signal_ts"),
        ("auto", args.auto, "id", "symbol", "entry_ts"),
    ]:
        if not path: continue
        df = pd.read_csv(path)
        df = df[df[symc].notna() & df[tsc].notna()].copy()
        print(f"[{tag}] {path}: {len(df)} сигналов")
        ok, fail = collect(df, idc, symc, tsc, session, args.sleep, done_keys, rows)
        print(f"[{tag}] готово: ok={ok}, fail={fail}\n")

    all_rows = existing_rows + rows
    cols = ["signal_id", "symbol", "signal_ts", "minute", "ts", "open", "high", "low", "close"]
    out = pd.DataFrame(all_rows, columns=cols)
    try:
        if args.out.endswith(".parquet"):
            out.to_parquet(args.out, index=False)
        else:
            out.to_csv(args.out, index=False)
    except Exception:
        alt = args.out.rsplit(".", 1)[0] + ".csv"
        out.to_csv(alt, index=False); args.out = alt
        print(f"(parquet недоступен, сохранил csv)")
    print(f"СОХРАНЕНО: {args.out}  |  строк={len(out)}, сигналов={out['signal_id'].nunique()}")


if __name__ == "__main__":
    main()
