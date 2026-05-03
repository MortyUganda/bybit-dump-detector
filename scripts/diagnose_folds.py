"""
Диагностика временных фолдов decision-модели.

Показывает для каждого фолда TimeSeriesSplit (5 фолдов по умолчанию):
  - временной диапазон (от/до)
  - размер train/test
  - WR в train и test
  - распределение source (auto_short / canceled / all_opened) в test

Цель: понять почему AUC скачет между фолдами.
Если WR_train ≠ WR_test или меняется состав source — это concept drift.

Запуск из корня проекта:
    python -m scripts.diagnose_folds
    python -m scripts.diagnose_folds --splits 8

Использует те же CSV что и train_decision_model.py
(берёт самые свежие auto_shorts_*.csv / canceled_signals_*.csv / all_opened_signals_*.csv).
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np
from sklearn.model_selection import TimeSeriesSplit

from scripts.train_decision_model import (
    load_entered,
    load_canceled,
    load_all_opened,
    merge_datasets,
    TARGET,
)


def _latest(pattern: str) -> str | None:
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--splits", type=int, default=5)
    parser.add_argument("--auto-csv", default="")
    parser.add_argument("--canceled-csv", default="")
    parser.add_argument("--all-opened-csv", default="")
    args = parser.parse_args()

    auto_path = args.auto_csv or _latest("auto_shorts_*.csv")
    canc_path = args.canceled_csv or _latest("canceled_signals_*.csv")
    ao_path = args.all_opened_csv or _latest("all_opened_signals_*.csv")

    if not auto_path or not canc_path:
        print("Не найдены CSV. Запусти .\\scripts\\run_ml.ps1 -Mode export сначала.")
        sys.exit(1)

    print(f"auto_shorts:        {os.path.basename(auto_path)}")
    print(f"canceled_signals:   {os.path.basename(canc_path)}")
    print(f"all_opened_signals: {os.path.basename(ao_path) if ao_path else '— нет —'}\n")

    df_e = load_entered(auto_path)
    df_c = load_canceled(canc_path)
    df_ao = load_all_opened(ao_path) if ao_path else None
    df, _feature_cols = merge_datasets(df_e, df_c, df_ao)

    df = df.sort_values("signal_ts").reset_index(drop=True)
    print(f"\nВсего сигналов: {len(df)}, общий WR: {df[TARGET].mean()*100:.1f}%")
    print(f"Период: {df['signal_ts'].min()} … {df['signal_ts'].max()}\n")

    # ====== Просто разбивка на N равных кусков по времени ======
    print("=" * 78)
    print(f"  Разбивка датасета на {args.splits} равных временных кусков")
    print("=" * 78)
    n = len(df)
    indices = np.array_split(np.arange(n), args.splits)
    for i, idx in enumerate(indices, 1):
        c = df.iloc[idx]
        wr = c[TARGET].mean() * 100
        sources = c["source"].value_counts().to_dict()
        src_str = ", ".join(f"{k}={v}" for k, v in sources.items())
        print(
            f"Часть {i}: n={len(c):4d}, "
            f"WR={wr:5.1f}%, "
            f"{c['signal_ts'].min().strftime('%Y-%m-%d %H:%M')} … "
            f"{c['signal_ts'].max().strftime('%Y-%m-%d %H:%M')}"
        )
        print(f"           sources: {src_str}")

    # ====== TimeSeriesSplit точно как в train_decision_model.py ======
    print()
    print("=" * 78)
    print(f"  TimeSeriesSplit n_splits={args.splits} (как в train_decision_model)")
    print("=" * 78)

    tscv = TimeSeriesSplit(n_splits=args.splits)
    for fold, (tr_idx, te_idx) in enumerate(tscv.split(df), 1):
        tr = df.iloc[tr_idx]
        te = df.iloc[te_idx]
        wr_tr = tr[TARGET].mean() * 100
        wr_te = te[TARGET].mean() * 100
        delta = wr_te - wr_tr
        sources_te = te["source"].value_counts().to_dict()
        src_str = ", ".join(f"{k}={v}" for k, v in sources_te.items())
        warn = "  ⚠ DRIFT" if abs(delta) > 7 else ""
        print(
            f"Fold {fold}: train n={len(tr):4d} WR={wr_tr:5.1f}% | "
            f"test n={len(te):4d} WR={wr_te:5.1f}% | "
            f"Δ={delta:+5.1f}%{warn}"
        )
        print(
            f"         test period: "
            f"{te['signal_ts'].min().strftime('%Y-%m-%d %H:%M')} … "
            f"{te['signal_ts'].max().strftime('%Y-%m-%d %H:%M')}"
        )
        print(f"         test sources: {src_str}")

    print(
        "\nКак читать:\n"
        "  - Если в Fold X тест-WR резко отличается от train-WR (Δ>7%) — это concept drift.\n"
        "    Модель училась на одном распределении, проверяется на другом.\n"
        "  - Если в Fold X состав sources сильно отличается от других фолдов\n"
        "    (например только canceled или только auto_short) — это причина разброса AUC.\n"
        "  - Решение: больше данных одной эпохи / cutoff по дате релиза."
    )


if __name__ == "__main__":
    main()
