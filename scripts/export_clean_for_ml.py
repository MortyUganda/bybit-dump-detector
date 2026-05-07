"""
Фильтрация CSV для ML: убирает строки с нулями/NULL в критичных фичах.

Загружает 3 CSV (auto_shorts, canceled_signals, all_opened_signals),
применяет фильтры на ключевые рыночные фичи, дропает f_momentum_loss,
сохраняет очищенные CSV в exports/clean/.

Использование:
  python -m scripts.export_clean_for_ml
  python -m scripts.export_clean_for_ml --auto-csv X --canceled-csv Y --all-opened-csv Z
  python -m scripts.export_clean_for_ml --output-dir my_clean/
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path


def find_latest_csv(pattern: str) -> str | None:
    """Найти самый свежий CSV по glob-паттерну."""
    files = glob.glob(pattern)
    if not files:
        return None
    return max(files, key=lambda p: Path(p).stat().st_mtime)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Фильтрация CSV для ML: убирает строки с нулями/NULL в критичных фичах"
    )
    p.add_argument("--auto-csv", default=None,
                   help="Путь к auto_shorts CSV (default: последний auto_shorts_*.csv)")
    p.add_argument("--canceled-csv", default=None,
                   help="Путь к canceled_signals CSV (default: последний canceled_signals_*.csv)")
    p.add_argument("--all-opened-csv", default=None,
                   help="Путь к all_opened_signals CSV (default: последний all_opened_signals_*.csv)")
    p.add_argument("--output-dir", default="exports/clean",
                   help="Папка для очищенных CSV (default: exports/clean)")
    return p.parse_args()


# Фильтры: (имя_колонки, условие_описание, лямбда_фильтр)
# Строка ОСТАЁТСЯ если лямбда возвращает True
FILTERS = [
    (
        "spread_pct",
        "spread_pct = 0 / NULL",
        lambda df: df["spread_pct"].notna() & (df["spread_pct"] > 0),
    ),
    (
        "ob_imbalance_top10",
        "ob_imbalance NULL",
        lambda df: df["ob_imbalance_top10"].notna(),
    ),
    (
        "ob_bid_volume_top10",
        "ob_volume_top10 = 0",
        lambda df: df["ob_bid_volume_top10"] > 0,
    ),
    (
        "ob_ask_volume_top10",
        "ob_volume_top10 = 0",
        lambda df: df["ob_ask_volume_top10"] > 0,
    ),
    (
        "funding_rate_at_signal",
        "funding_rate NULL",
        lambda df: df["funding_rate_at_signal"].notna(),
    ),
    (
        "realized_vol_1h",
        "realized_vol_1h = 0",
        lambda df: df["realized_vol_1h"] > 0,
    ),
    (
        "volume_24h_usdt",
        "volume_24h_usdt = 0",
        lambda df: df["volume_24h_usdt"] > 0,
    ),
]

# Объединяем ob_bid и ob_ask в одну строку статистики
STAT_GROUPS = [
    ("spread_pct = 0 / NULL", ["spread_pct"]),
    ("ob_imbalance NULL", ["ob_imbalance_top10"]),
    ("ob_volume_top10 = 0", ["ob_bid_volume_top10", "ob_ask_volume_top10"]),
    ("funding_rate NULL", ["funding_rate_at_signal"]),
    ("realized_vol_1h = 0", ["realized_vol_1h"]),
    ("volume_24h_usdt = 0", ["volume_24h_usdt"]),
]

DROP_COLUMN = "f_momentum_loss"


def clean_csv(csv_path: str, output_dir: Path) -> tuple[int, int] | None:
    """Фильтрует один CSV файл. Возвращает (исходно, осталось) или None если колонки нет."""
    import pandas as pd

    src = Path(csv_path)
    df = pd.read_csv(csv_path)
    total = len(df)

    name = src.stem  # e.g. auto_shorts_20260507_123456

    print(f"\n{src.name}:")
    print(f"  Исходно: {total} строк")

    # Проверяем наличие критичных колонок
    missing = [c for c in ["spread_pct", "ob_imbalance_top10", "ob_bid_volume_top10",
                           "ob_ask_volume_top10", "funding_rate_at_signal",
                           "realized_vol_1h", "volume_24h_usdt"] if c not in df.columns]
    if missing:
        print(f"  Пропущены колонки: {', '.join(missing)} — пропускаем файл")
        return None

    # Считаем статистику по каждой группе фильтров
    print("  Отсеяно по фильтрам:")
    rejected_masks = {}
    for filter_col, filter_desc, filter_fn in FILTERS:
        keep_mask = filter_fn(df)
        rejected_masks[filter_col] = ~keep_mask

    # Выводим сгруппированную статистику
    for group_desc, group_cols in STAT_GROUPS:
        # Объединяем маски группы (строка отсеяна если хотя бы один фильтр из группы не прошёл)
        combined = None
        for col in group_cols:
            if col in rejected_masks:
                if combined is None:
                    combined = rejected_masks[col]
                else:
                    combined = combined | rejected_masks[col]
        if combined is not None:
            cnt = int(combined.sum())
            pct = cnt / total * 100 if total > 0 else 0
            print(f"    {group_desc + ':':<35s} {cnt} ({pct:.0f}%)")

    # Применяем все фильтры
    keep = None
    for _, _, filter_fn in FILTERS:
        mask = filter_fn(df)
        if keep is None:
            keep = mask
        else:
            keep = keep & mask

    # Уникальных отсеяно (общее пересечение)
    rejected_total = int((~keep).sum())
    print(f"  Уникальных строк отсеяно: {rejected_total} (общее пересечение)")

    df_clean = df[keep].copy()
    remaining = len(df_clean)
    pct_remaining = remaining / total * 100 if total > 0 else 0

    # Дропаем f_momentum_loss
    dropped = False
    if DROP_COLUMN in df_clean.columns:
        df_clean = df_clean.drop(columns=[DROP_COLUMN])
        dropped = True

    # Сохраняем
    output_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"{name}_clean.csv"
    out_path = output_dir / out_name
    df_clean.to_csv(out_path, index=False)

    print(f"  Осталось: {remaining} ({pct_remaining:.0f}%) → {out_path}")
    if dropped:
        print(f"  Дропнута колонка: {DROP_COLUMN}")

    return total, remaining



def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)

    auto_csv = args.auto_csv or find_latest_csv("auto_shorts_*.csv")
    canceled_csv = args.canceled_csv or find_latest_csv("canceled_signals_*.csv")
    all_opened_csv = args.all_opened_csv or find_latest_csv("all_opened_signals_*.csv")

    csvs = {
        "auto_shorts": auto_csv,
        "canceled_signals": canceled_csv,
        "all_opened_signals": all_opened_csv,
    }

    # Проверяем наличие хотя бы одного CSV
    found = {k: v for k, v in csvs.items() if v and Path(v).exists()}
    if not found:
        print("Не найдено ни одного CSV. Укажи пути через --auto-csv / --canceled-csv / --all-opened-csv")
        sys.exit(1)

    print("=== Чистка данных для ML ===")

    total_all = 0
    remaining_all = 0
    clean_paths = {}

    for key, csv_path in csvs.items():
        if not csv_path or not Path(csv_path).exists():
            print(f"\n{key}: не найден — пропускаем")
            continue

        result = clean_csv(csv_path, output_dir)
        if result is not None:
            total, remaining = result
            total_all += total
            remaining_all += remaining
            src_name = Path(csv_path).stem
            clean_paths[key] = output_dir / f"{src_name}_clean.csv"

    # Итого
    pct_total = remaining_all / total_all * 100 if total_all > 0 else 0
    print("\n=== Итого ===")
    print(f"Было: {total_all} строк")
    print(f"Осталось: {remaining_all} строк ({pct_total:.0f}%)")

    # Команда для обучения
    parts = []
    if "auto_shorts" in clean_paths:
        parts.append(f"--auto-csv {clean_paths['auto_shorts']}")
    if "canceled_signals" in clean_paths:
        parts.append(f"--canceled-csv {clean_paths['canceled_signals']}")
    if "all_opened_signals" in clean_paths:
        parts.append(f"--all-opened-csv {clean_paths['all_opened_signals']}")

    if parts:
        cmd = "python -m scripts.train_decision_model " + " ".join(parts)
        print(f"Готово к обучению: {cmd}")


if __name__ == "__main__":
    main()
