from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = PROJECT_ROOT / "docker"

RAW_ROOT = PROJECT_ROOT / "ml" / "data" / "raw"
RAW_CURRENT = RAW_ROOT / "current"
RAW_ARCHIVE = RAW_ROOT / "archive"

RAW_CURRENT.mkdir(parents=True, exist_ok=True)
RAW_ARCHIVE.mkdir(parents=True, exist_ok=True)


def export_view_to_csv(view: str, csv_path: Path) -> None:
    """
    COPY view/table -> CSV через docker compose exec postgres.
    """
    psql_cmd = [
        "docker",
        "compose",
        "-f",
        str(DOCKER_DIR / "docker-compose.yml"),
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "dumpuser",
        "-d",
        "dumpdetector",
        "-c",
        f"COPY (SELECT * FROM {view}) TO STDOUT WITH CSV HEADER",
    ]

    print(f"Exporting {view} -> {csv_path} ...")
    with csv_path.open("wb") as f:
        proc = subprocess.run(psql_cmd, stdout=f, check=True)
    print(f"  Done (returncode={proc.returncode})")


def csv_to_parquet(csv_path: Path, parquet_path: Path) -> None:
    print(f"Converting {csv_path.name} -> {parquet_path.name} ...")
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(parquet_path, index=False)
    print(f"  Rows: {len(df)}")


def main() -> None:
    today = dt.date.today().isoformat()  # '2026-04-13'
    archive_dir = RAW_ARCHIVE / today
    archive_dir.mkdir(parents=True, exist_ok=True)

    # --- CSV во временный архив ---
    csv_entry = archive_dir / "ml_opened_vs_canceled.csv"
    csv_pnl = archive_dir / "ml_opened_only_profitable.csv"
    csv_canceled = archive_dir / "canceled_signals.csv"

    # --- Parquet в архив ---
    pq_entry_archive = archive_dir / "ml_opened_vs_canceled.parquet"
    pq_pnl_archive = archive_dir / "ml_opened_only_profitable.parquet"
    pq_canceled_archive = archive_dir / "canceled_signals.parquet"

    # --- Parquet в current/ ---
    pq_entry_current = RAW_CURRENT / "ml_opened_vs_canceled.parquet"
    pq_pnl_current = RAW_CURRENT / "ml_opened_only_profitable.parquet"
    pq_canceled_current = RAW_CURRENT / "canceled_signals.parquet"

    # 1. Экспорт CSV из Postgres
    export_view_to_csv("ml_opened_vs_canceled", csv_entry)
    export_view_to_csv("ml_opened_only_profitable", csv_pnl)
    # здесь можно использовать либо саму таблицу, либо вьюху, если сделаешь:
    export_view_to_csv("canceled_signals", csv_canceled)

    # 2. CSV -> Parquet (в архив)
    csv_to_parquet(csv_entry, pq_entry_archive)
    csv_to_parquet(csv_pnl, pq_pnl_archive)
    csv_to_parquet(csv_canceled, pq_canceled_archive)

    # 3. Обновляем current/ snapshots
    print("Updating current/ snapshots ...")
    pq_entry_current.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pq_entry_archive, pq_entry_current)
    shutil.copy2(pq_pnl_archive, pq_pnl_current)
    shutil.copy2(pq_canceled_archive, pq_canceled_current)

    print("All done.")
    print(f"Archive: {archive_dir}")
    print(f"Current: {pq_entry_current}")
    print(f"         {pq_pnl_current}")
    print(f"         {pq_canceled_current}")


if __name__ == "__main__":
    main()

#----------------------------------------------------------------------------------------
# #Как пользоваться
# Запуск из корня проекта:
########### powershell ################

# cd C:\Users\Sergei\Desktop\bybit-dump-detector
# python .\ml\scripts\export_views_to_parquet.py


#----------------------------------------------------------------------------------------
# В ноутбуках читаешь стабильно из current:
# python

# import pandas as pd
# from pathlib import Path

# base = Path("ml/data/raw/current")

# df_entry = pd.read_parquet(base / "ml_opened_vs_canceled.parquet")
# df_pnl = pd.read_parquet(base / "ml_opened_only_profitable.parquet")
#----------------------------------------------------------------------------------------