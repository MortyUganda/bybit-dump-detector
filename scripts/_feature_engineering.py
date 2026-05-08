"""
Engineered features для decision/outcome моделей.

Три группы:
  1. Symbol-specific historical (WR, trades count, avg pnl)
  2. Time-of-day (hour, day, session, is_weekend)
  3. Funding rate dynamics (change, sign flip)

Все фичи рассчитываются ТОЛЬКО по прошлым записям (no look-ahead).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ── Группа 1: Symbol-specific historical ────────────────────────


def _add_symbol_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Для каждого сигнала — статистика по ПРОШЛЫМ закрытым сделкам того же символа.

    Требует колонки: symbol, entry_ts, ml_label (или label), pnl_pct.
    Если symbol/entry_ts отсутствуют — пропускает.
    """
    if "symbol" not in df.columns:
        print("  ⚠️ Group 1 (symbol-specific): SKIPPED — нет колонки 'symbol'")
        return df

    ts_col = None
    for c in ("entry_ts", "signal_ts"):
        if c in df.columns:
            ts_col = c
            break
    if ts_col is None:
        print("  ⚠️ Group 1 (symbol-specific): SKIPPED — нет entry_ts/signal_ts")
        return df

    df = df.copy()
    df["_ts"] = pd.to_datetime(df[ts_col])
    df = df.sort_values("_ts").reset_index(drop=True)

    # Определяем колонку с результатом (1/0) и pnl
    label_col = None
    for c in ("label", "ml_label"):
        if c in df.columns:
            label_col = c
            break

    # pnl_pct — основной источник PnL; fallback на label (1/0) как прокси
    pnl_col = None
    for c in ("pnl_pct", "label", "ml_label"):
        if c in df.columns:
            pnl_col = c
            break

    # Инициализируем результаты NaN
    df["symbol_recent_wr_20"] = np.nan
    df["symbol_recent_wr_5"] = np.nan
    df["symbol_trades_count_24h"] = np.nan
    df["symbol_avg_pnl_5"] = np.nan

    for sym, grp in df.groupby("symbol"):
        idxs = grp.index.tolist()
        ts_arr = grp["_ts"].values

        if label_col is not None:
            labels = grp[label_col].values
        else:
            labels = None

        if pnl_col is not None:
            pnls = grp[pnl_col].values
        else:
            pnls = None

        for pos, idx in enumerate(idxs):
            current_ts = ts_arr[pos]

            # --- WR последних 20 закрытых ---
            if labels is not None and pos >= 5:
                window_20 = labels[max(0, pos - 20):pos]
                valid = window_20[~np.isnan(window_20.astype(float))]
                if len(valid) >= 5:
                    df.at[idx, "symbol_recent_wr_20"] = float(valid.sum()) / len(valid)

            # --- WR последних 5 закрытых ---
            if labels is not None and pos >= 3:
                window_5 = labels[max(0, pos - 5):pos]
                valid = window_5[~np.isnan(window_5.astype(float))]
                if len(valid) >= 3:
                    df.at[idx, "symbol_recent_wr_5"] = float(valid.sum()) / len(valid)

            # --- Количество сделок этого символа за 24h ---
            window_start = current_ts - np.timedelta64(24, "h")
            past_ts = ts_arr[:pos]
            cnt = int(((past_ts >= window_start) & (past_ts < current_ts)).sum())
            df.at[idx, "symbol_trades_count_24h"] = float(cnt)

            # --- Средний pnl последних 5 ---
            if pnls is not None and pos >= 3:
                window_pnl = pnls[max(0, pos - 5):pos]
                valid_pnl = window_pnl[~np.isnan(window_pnl.astype(float))]
                if len(valid_pnl) >= 3:
                    df.at[idx, "symbol_avg_pnl_5"] = float(np.mean(valid_pnl))

    df.drop(columns=["_ts"], inplace=True)
    return df


# ── Группа 2: Time-of-day ───────────────────────────────────────


def _add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Из entry_ts / signal_ts:
      hour_of_day, day_of_week, session (0-3), is_weekend
    """
    ts_col = None
    for c in ("entry_ts", "signal_ts"):
        if c in df.columns:
            ts_col = c
            break
    if ts_col is None:
        print("  ⚠️ Group 2 (time-of-day): SKIPPED — нет entry_ts/signal_ts")
        return df

    df = df.copy()
    ts = pd.to_datetime(df[ts_col])

    df["hour_of_day"] = ts.dt.hour
    df["day_of_week"] = ts.dt.dayofweek  # 0=Monday, 6=Sunday

    # Session: 0=asia(00-07), 1=europe(08-12), 2=overlap(13-16), 3=america(17-23)
    hour = ts.dt.hour
    df["session"] = np.select(
        [
            hour < 8,                  # asia: 00:00-07:59
            (hour >= 8) & (hour < 13),  # europe: 08:00-12:59
            (hour >= 13) & (hour < 17),  # overlap: 13:00-16:59
            hour >= 17,                 # america: 17:00-23:59
        ],
        [0, 1, 2, 3],
        default=0,
    )

    df["is_weekend"] = (ts.dt.dayofweek >= 5).astype(int)

    return df


# ── Группа 3: Funding rate dynamics ─────────────────────────────


def _add_funding_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Если есть funding_rate_at_signal и исторические данные — считаем:
      funding_change_1h, funding_sign_changed.

    Если нет данных для расчёта — пропускаем с предупреждением.
    """
    if "funding_rate_at_signal" not in df.columns:
        print("  ⚠️ Group 3 (funding dynamics): SKIPPED — нет funding_rate_at_signal в CSV")
        return df

    # Проверяем наличие готовых полей для funding 1h ago
    has_prev = (
        "funding_rate_at_signal_1h_ago" in df.columns
        or "funding_rate_at_signal_prev" in df.columns
    )

    if has_prev:
        prev_col = (
            "funding_rate_at_signal_1h_ago"
            if "funding_rate_at_signal_1h_ago" in df.columns
            else "funding_rate_at_signal_prev"
        )
        df = df.copy()
        cur = df["funding_rate_at_signal"].astype(float)
        prev = df[prev_col].astype(float)
        df["funding_change_1h"] = cur - prev
        df["funding_sign_changed"] = (
            (np.sign(cur) != np.sign(prev)) & prev.notna() & cur.notna()
        ).astype(int)
        return df

    # Нет готового prev — пробуем рассчитать по символу из тех же данных
    if "symbol" not in df.columns:
        # funding_change_1h не рассчитан — нет исторических данных в CSV
        print("  ⚠️ funding_change_1h не рассчитан — нет исторических данных в CSV")
        return df

    ts_col = None
    for c in ("entry_ts", "signal_ts"):
        if c in df.columns:
            ts_col = c
            break

    if ts_col is None:
        print("  ⚠️ funding_change_1h не рассчитан — нет timestamp в CSV")
        return df

    df = df.copy()
    df["_ts_fund"] = pd.to_datetime(df[ts_col])
    df = df.sort_values("_ts_fund").reset_index(drop=True)

    df["funding_change_1h"] = np.nan
    df["funding_sign_changed"] = np.nan

    for sym, grp in df.groupby("symbol"):
        idxs = grp.index.tolist()
        ts_arr = grp["_ts_fund"].values
        fr_arr = grp["funding_rate_at_signal"].values.astype(float)

        for pos, idx in enumerate(idxs):
            if pos == 0:
                continue
            current_ts = ts_arr[pos]
            # Ищем ближайшую запись 1h назад (±30min)
            target_ts = current_ts - np.timedelta64(1, "h")
            past_ts = ts_arr[:pos]
            past_fr = fr_arr[:pos]

            # Берём ближайшую по времени запись в окне [target-30m, target+30m]
            diffs = np.abs(past_ts - target_ts)
            min_diff = diffs.min()
            if min_diff <= np.timedelta64(30, "m"):
                closest_idx = int(np.argmin(diffs))
                prev_fr = past_fr[closest_idx]
                cur_fr = fr_arr[pos]
                if not (np.isnan(prev_fr) or np.isnan(cur_fr)):
                    df.at[idx, "funding_change_1h"] = cur_fr - prev_fr
                    df.at[idx, "funding_sign_changed"] = int(
                        np.sign(cur_fr) != np.sign(prev_fr)
                    )

    df.drop(columns=["_ts_fund"], inplace=True)

    # Проверяем, сколько удалось рассчитать
    filled = df["funding_change_1h"].notna().sum()
    total = len(df)
    if filled == 0:
        print("  ⚠️ funding_change_1h не рассчитан — нет исторических данных в CSV")
        df.drop(columns=["funding_change_1h", "funding_sign_changed"], inplace=True)
    else:
        print(f"  ✅ funding_change_1h рассчитан: {filled}/{total} строк ({filled/total:.0%})")

    return df


# ── Публичный API ────────────────────────────────────────────────


# Списки фичей по группам (для добавления в COMMON_FEATURES)
GROUP1_FEATURES = [
    "symbol_recent_wr_20",
    "symbol_recent_wr_5",
    "symbol_trades_count_24h",
    "symbol_avg_pnl_5",
]

GROUP2_FEATURES = [
    "hour_of_day",
    "day_of_week",
    "session",
    "is_weekend",
]

GROUP3_FEATURES = [
    "funding_change_1h",
    "funding_sign_changed",
]


def add_engineered_features(
    df: pd.DataFrame,
    include_funding: bool = False,
) -> pd.DataFrame:
    """
    Добавляет engineered features.

    Группы:
      1. Symbol-specific historical (WR, trades count, avg pnl)
      2. Time-of-day (hour, day, session, is_weekend)
      3. Funding rate dynamics — ТОЛЬКО если include_funding=True
         (по умолчанию отключена: 83% NaN на текущих данных)

    Возвращает df с новыми колонками.
    Печатает в stdout отчёт о добавленных фичах и NaN-rate.
    """
    print("\n📊 Engineered features added:")

    # Group 1: symbol-specific
    cols_before_g1 = set(df.columns)
    df = _add_symbol_features(df)
    added_g1 = [c for c in GROUP1_FEATURES if c in df.columns and c not in cols_before_g1]
    if added_g1:
        print(f"  Group 1 (symbol-specific): {', '.join(added_g1)}")
    else:
        print("  Group 1 (symbol-specific): SKIPPED")

    # Group 2: time-of-day
    cols_before_g2 = set(df.columns)
    df = _add_time_features(df)
    added_g2 = [c for c in GROUP2_FEATURES if c in df.columns and c not in cols_before_g2]
    if added_g2:
        print(f"  Group 2 (time-of-day): {', '.join(added_g2)}")
    else:
        print("  Group 2 (time-of-day): SKIPPED")

    # Group 3: funding dynamics (только если include_funding=True)
    added_g3: list[str] = []
    if include_funding:
        cols_before_g3 = set(df.columns)
        df = _add_funding_features(df)
        added_g3 = [c for c in GROUP3_FEATURES if c in df.columns and c not in cols_before_g3]
        if added_g3:
            print(f"  Group 3 (funding dynamics): {', '.join(added_g3)}")
        else:
            print("  Group 3 (funding dynamics): SKIPPED (нет исторических funding в CSV)")
    else:
        print("  Group 3 (funding dynamics): SKIPPED (include_funding=False, 83% NaN)")

    # NaN-rate report
    all_added = added_g1 + added_g2 + added_g3
    if all_added:
        print("NaN-rate по новым фичам:")
        for col in all_added:
            nan_rate = df[col].isna().mean()
            note = ""
            if col == "symbol_recent_wr_20":
                note = " (новые символы)"
            elif col == "symbol_trades_count_24h":
                note = ""
            print(f"  {col}: {nan_rate:.0%}{note}")

    return df
