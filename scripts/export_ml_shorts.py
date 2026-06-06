#!/usr/bin/env python3
"""
export_ml_shorts.py — выгрузка БД ML-шортов в плоский CSV для анализа/дообучения.

Запускать ЛОКАЛЬНО, где доступен Postgres бота (dumpdetector).

Что делает:
  1. Читает ml_short_signals (решения ML + JSONB-снапшот фич).
  2. Читает ml_short_positions (paper-позиции с pnl/исходами).
  3. Разворачивает features_snapshot (JSONB) в плоские колонки f_*.
  4. Объединяет signals + positions по signal_id (LEFT JOIN: остаются все сигналы,
     даже отфильтрованные/неоткрытые).
  5. Пишет три файла:
       - ml_short_signals.csv      (сырые сигналы + развёрнутые фичи)
       - ml_short_positions.csv    (сырые позиции)
       - ml_short_dataset.csv      (объединённый датасет для обучения)

Примеры:
  python export_ml_shorts.py
  python export_ml_shorts.py --dsn postgresql+psycopg2://dumpuser:strongpassword123@localhost:5432/dumpdetector
  python export_ml_shorts.py --out-dir ./export --only-closed
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd
from sqlalchemy import create_engine


def default_dsn() -> str:
    """Собирает DSN из переменных окружения (как settings.py) либо берёт дефолты бота."""
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "dumpdetector")
    user = os.getenv("POSTGRES_USER", "dumpuser")
    pwd = os.getenv("POSTGRES_PASSWORD", "strongpassword123")
    return f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{db}"


def expand_jsonb(series: pd.Series, prefix: str = "f_") -> pd.DataFrame:
    """Разворачивает колонку с dict/JSON-строкой в плоские колонки."""
    def _coerce(x):
        if isinstance(x, dict):
            return x
        if isinstance(x, str) and x.strip():
            try:
                return json.loads(x)
            except json.JSONDecodeError:
                return {}
        return {}

    flat = pd.json_normalize(series.apply(_coerce))
    return flat.add_prefix(prefix)


def main() -> int:
    ap = argparse.ArgumentParser(description="Экспорт БД ML-шортов в CSV.")
    ap.add_argument("--dsn", default=default_dsn(),
                    help="SQLAlchemy DSN (psycopg2). По умолчанию из env или дефолты бота.")
    ap.add_argument("--out-dir", default=".", help="Каталог для CSV-файлов.")
    ap.add_argument("--only-closed", action="store_true",
                    help="В dataset оставить только закрытые позиции (есть pnl_pct).")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    try:
        eng = create_engine(args.dsn)
        with eng.connect() as conn:
            signals = pd.read_sql(
                "SELECT * FROM ml_short_signals ORDER BY signal_ts", conn
            )
            positions = pd.read_sql(
                "SELECT * FROM ml_short_positions ORDER BY entry_ts", conn
            )
    except Exception as e:
        print(f"[ОШИБКА] Не удалось подключиться/прочитать БД: {e}", file=sys.stderr)
        print("Проверь DSN и что Postgres доступен. "
              "Если Postgres в Docker — пробрось порт или запусти скрипт внутри сети контейнеров.",
              file=sys.stderr)
        return 1

    print(f"[OK] ml_short_signals:   {signals.shape[0]} строк, {signals.shape[1]} колонок")
    print(f"[OK] ml_short_positions: {positions.shape[0]} строк, {positions.shape[1]} колонок")

    # --- разворачиваем JSONB-снапшот фич в сигналах ---
    if "features_snapshot" in signals.columns:
        feat = expand_jsonb(signals["features_snapshot"])
        signals_flat = pd.concat(
            [signals.drop(columns=["features_snapshot"]), feat], axis=1
        )
        print(f"[OK] развёрнуто {feat.shape[1]} фич из features_snapshot")
    else:
        signals_flat = signals
        print("[WARN] колонка features_snapshot не найдена — пропускаю разворот")

    # --- сырые выгрузки ---
    sig_path = os.path.join(args.out_dir, "ml_short_signals.csv")
    pos_path = os.path.join(args.out_dir, "ml_short_positions.csv")
    signals_flat.to_csv(sig_path, index=False)
    positions.to_csv(pos_path, index=False)
    print(f"[FILE] {sig_path}")
    print(f"[FILE] {pos_path}")

    # --- объединённый датасет: signals LEFT JOIN positions по signal_id ---
    # в positions ключ к сигналу: signal_id ; в signals — id
    pos_cols = positions.copy()
    if "signal_id" in pos_cols.columns:
        # префикс, чтобы не было коллизий имён (symbol, score, ml_proba и т.п.)
        overlap = [c for c in pos_cols.columns
                   if c in signals_flat.columns and c != "signal_id"]
        pos_cols = pos_cols.rename(columns={c: f"pos_{c}" for c in overlap})
        dataset = signals_flat.merge(
            pos_cols, how="left", left_on="id", right_on="signal_id",
            suffixes=("", "_pos"),
        )
    else:
        print("[WARN] в positions нет signal_id — объединение пропущено, "
              "dataset = только сигналы")
        dataset = signals_flat

    if args.only_closed and "pos_pnl_pct" in dataset.columns:
        before = len(dataset)
        dataset = dataset[dataset["pos_pnl_pct"].notna()].copy()
        print(f"[FILTER] only-closed: {before} -> {len(dataset)} строк")
    elif args.only_closed and "pnl_pct" in dataset.columns:
        before = len(dataset)
        dataset = dataset[dataset["pnl_pct"].notna()].copy()
        print(f"[FILTER] only-closed: {before} -> {len(dataset)} строк")

    ds_path = os.path.join(args.out_dir, "ml_short_dataset.csv")
    dataset.to_csv(ds_path, index=False)
    print(f"[FILE] {ds_path}  ({dataset.shape[0]} строк, {dataset.shape[1]} колонок)")

    # --- краткая сводка по исходам, если есть ---
    pnl_col = "pos_pnl_pct" if "pos_pnl_pct" in dataset.columns else (
        "pnl_pct" if "pnl_pct" in dataset.columns else None)
    if pnl_col:
        closed = dataset[dataset[pnl_col].notna()]
        if len(closed):
            wins = (closed[pnl_col] > 0).sum()
            print(f"\n[СВОДКА] закрытых позиций: {len(closed)} | "
                  f"win-rate: {wins/len(closed)*100:.1f}% | "
                  f"средний pnl: {closed[pnl_col].mean():.3f}%")

    print("\nГотово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
