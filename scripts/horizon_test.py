#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HORIZON TEST — жив ли направленный импульс на КОРОТКОМ окне?

КОНТЕКСТ (доказано 9 раз):
  На 60-мин окне направленного edge НЕТ ни вверх, ни вниз. Шорт и лонг оба
  убыточны, OOF AUC ~0.50-0.54 (шум). pnl лонга залип на -0.38% при любом R:R
  → edge ровно нулевой.

ГИПОТЕЗА:
  Возможно сразу после сигнала есть КРАТКОВРЕМЕННЫЙ импульс (в одну сторону),
  который размывается к 60-й минуте. 60 минут — слишком длинное окно, шум
  доминирует. На 5-15 мин дрейф мог бы быть направленным.

ЧТО ДЕЛАЕМ (на УЖЕ имеющихся минутных свечах, новый сбор не нужен):
  Для каждого горизонта H in {5,10,15,30,60} мин:
    A) ЧИСТЫЙ ДРЕЙФ: средний ход цены (close[H]-close[0])/close[0]*100.
       Это главная метрика — есть ли направленное движение В ПРИНЦИПЕ,
       без искажения стопами. t-стат проверяет значимость.
       Положительный дрейф → импульс вверх (лонг), отрицательный → вниз (шорт).
    B) ЧЕСТНЫЙ ШОРТ и ЛОНГ tick-by-tick внутри окна H (TP/SL заданы),
       SL первым при коллизии. WR, pnl/сделку, t-стат.
  Печатаем таблицу по горизонтам. Помечаем ✅ дрейф или сделку с |t|>3
  (строгий порог — мы перебираем горизонты, нужна поправка на множественность).

ВЫВОД:
  Если на коротком H есть значимый направленный дрейф (|t|>3) с экономическим
  смыслом → edge живёт на коротком горизонте, бот надо переводить на него
  (быстрый вход/выход). Если дрейф ~0 на всех H → импульса нет вообще,
  сигнал не предсказывает движение ни на каком масштабе.

Запуск (локально):
    python scripts/horizon_test.py \
        --klines minute_klines.csv \
        --canceled canceled_signals_20260605_130117.csv

Опц.: --tp 1.0 --sl 1.0 (для блока B). Зависимости: pandas numpy
"""
from __future__ import annotations
import argparse, os, sys
import numpy as np, pandas as pd


def wilson(w, n, z=1.96):
    if n == 0:
        return 0.0, 0.0, 0.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def tstat(x):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    if len(x) < 2 or x.std(ddof=1) == 0:
        return 0.0
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def sim(candles, ep, tp, sl, lev, fee, side):
    """tick-by-tick. side='short': TP=падение, SL=рост. side='long': наоборот.
    candles обрезаны до горизонта. SL первым при коллизии (пессимизм)."""
    if side == "short":
        tp_price, sl_price = ep * (1 - tp/100), ep * (1 + sl/100)
        for c in candles[1:]:
            _, o, hi, lo, cl = c
            if hi >= sl_price: return -sl*lev - fee
            if lo <= tp_price: return +tp*lev - fee
        return (ep - candles[-1][4]) / ep * 100 * lev - fee
    else:
        tp_price, sl_price = ep * (1 + tp/100), ep * (1 - sl/100)
        for c in candles[1:]:
            _, o, hi, lo, cl = c
            if lo <= sl_price: return -sl*lev - fee
            if hi >= tp_price: return +tp*lev - fee
        return (candles[-1][4] - ep) / ep * 100 * lev - fee


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--klines", default="minute_klines.csv")
    ap.add_argument("--canceled", default="canceled_signals_20260605_130117.csv")
    ap.add_argument("--tp", type=float, default=1.0)
    ap.add_argument("--sl", type=float, default=1.0)
    ap.add_argument("--fee", type=float, default=0.6)
    ap.add_argument("--lev", type=float, default=10.0)
    ap.add_argument("--horizons", default="5,10,15,30,60")
    args = ap.parse_args()

    kpath = args.klines
    if not os.path.exists(kpath):
        base = kpath.rsplit(".", 1)[0]
        for alt in (base + ".csv", base + ".parquet"):
            if os.path.exists(alt):
                kpath = alt; break
        else:
            print(f"не найден {args.klines}"); sys.exit(1)
    kl = pd.read_parquet(kpath) if kpath.endswith(".parquet") else pd.read_csv(kpath)
    kl["signal_id"] = kl["signal_id"].astype(str)
    horizons = [int(x) for x in args.horizons.split(",")]
    print(f"свечей: {len(kl)}, сигналов: {kl['signal_id'].nunique()}")
    print(f"горизонты (мин): {horizons}")
    print(f"блок B: TP={args.tp}% SL={args.sl}% (R:R {args.tp/args.sl:.2f}), "
          f"плечо {args.lev:.0f}x, комиссия {args.fee}%\n")

    # подготовка: список свечей по сигналу (отсортированы)
    groups = {}
    for sid, gdf in kl.groupby("signal_id"):
        gdf = gdf.sort_values("minute")
        cs = list(zip(gdf["minute"], gdf["open"], gdf["high"], gdf["low"], gdf["close"]))
        if len(cs) >= 2 and cs[0][4] > 0:
            groups[sid] = cs

    print("=" * 96)
    print("БЛОК A: ЧИСТЫЙ ДРЕЙФ ЦЕНЫ за окно H (главная метрика, без стопов)")
    print("=" * 96)
    print(f"  {'гориз':>7}{'n':>7}{'дрейф%':>10}{'медиана%':>11}{'%вверх':>9}"
          f"{'t-стат':>9}{'вердикт':>14}")
    drift_found = []
    for H in horizons:
        moves = []
        for sid, cs in groups.items():
            # close на минуте H (или последняя доступная <= H)
            c0 = cs[0][4]
            cH = None
            for c in cs:
                if c[0] <= H:
                    cH = c[4]
                else:
                    break
            if cH is None or c0 <= 0:
                continue
            moves.append((cH - c0) / c0 * 100.0)
        moves = np.array(moves)
        if len(moves) == 0:
            continue
        t = tstat(moves)
        pct_up = (moves > 0).mean()
        verdict = ""
        if abs(t) > 3:
            verdict = "✅ вверх" if moves.mean() > 0 else "✅ вниз"
        elif abs(t) > 2:
            verdict = "~ слабо"
        print(f"  {H:>6}м{len(moves):>7}{moves.mean():>+9.3f}%{np.median(moves):>+10.3f}%"
              f"{pct_up:>8.1%}{t:>9.2f}{verdict:>14}")
        if abs(t) > 3:
            drift_found.append((H, moves.mean(), t))

    print("\n  ВАЖНО: дрейф — это средний ход БЕЗ комиссии и плеча. Чтобы он был торгуемым,")
    print(f"  |дрейф| должен покрывать комиссию {args.fee}%/плечо = "
          f"{args.fee/args.lev:.3f}% хода в одну сторону + проскальзывание.\n")

    print("=" * 96)
    print("БЛОК B: ЧЕСТНЫЙ ШОРТ и ЛОНГ tick-by-tick внутри окна H")
    print("=" * 96)
    print(f"  {'гориз':>7}{'сторона':>9}{'n':>7}{'WR':>8}{'pnl/сд':>10}{'сумма%':>11}{'t':>8}")
    trade_found = []
    for H in horizons:
        for side in ("short", "long"):
            pnls = []
            for sid, cs in groups.items():
                csH = [c for c in cs if c[0] <= H]
                if len(csH) < 2:
                    continue
                ep = csH[0][4]
                if ep <= 0:
                    continue
                pnls.append(sim(csH, ep, args.tp, args.sl, args.lev, args.fee, side))
            p = np.array(pnls)
            if len(p) == 0:
                continue
            wr, lo, hi = wilson(int((p > 0).sum()), len(p))
            t = tstat(p)
            flag = "  ✅" if (p.mean() > 0 and t > 3) else ("  ~" if p.mean() > 0 else "")
            print(f"  {H:>6}м{side:>9}{len(p):>7}{wr:>7.1%}{p.mean():>+9.2f}%"
                  f"{p.sum():>+10.0f}%{t:>8.2f}{flag}")
            if p.mean() > 0 and t > 3:
                trade_found.append((H, side, p.mean(), t))
        print()

    print("=" * 96)
    print("ВЫВОД")
    print("=" * 96)
    if drift_found or trade_found:
        print("  ✅ На КОРОТКОМ горизонте найдено направленное движение:")
        for H, mean, t in drift_found:
            d = "вверх (ЛОНГ)" if mean > 0 else "вниз (ШОРТ)"
            print(f"     • дрейф на {H}м: {mean:+.3f}% {d}, t={t:.2f}")
        for H, side, mean, t in trade_found:
            print(f"     • {side} на {H}м: {mean:+.2f}%/сделку, t={t:.2f}")
        print("\n  EDGE ЖИВЁТ НА КОРОТКОМ ГОРИЗОНТЕ. Бота нужно переводить на быстрый")
        print("  вход/выход (окно H), а не держать 60 мин. Проверь, что дрейф покрывает")
        print("  комиссию+проскальзывание, и собери НОВЫЕ данные для out-of-sample.")
        print("  ВНИМАНИЕ: на коротком окне комиссия 0.6% съедает почти весь ход —")
        print("  edge должен быть КРУПНЫМ, чтобы остаться прибыльным после издержек.")
    else:
        print("  ❌ Импульса нет НИ НА КАКОМ горизонте (5-60 мин). Дрейф цены около нуля,")
        print("     честные шорт/лонг убыточны на всех окнах. Это финальное, десятое")
        print("     доказательство: сигнал детектора НЕ предсказывает направление движения")
        print("     ни на каком временном масштабе. Монета после сигнала движется случайно.")
        print("")
        print("     ОКОНЧАТЕЛЬНЫЙ ВЕРДИКТ ПО ИДЕЕ: 'детектор перегретых монет + краткосрочная")
        print("     сделка' не имеет торгового преимущества. Инфраструктура исправна, но")
        print("     САМ СИГНАЛ пустой. Дальнейшие пути лежат вне этого сигнала:")
        print("     — длинный горизонт 4-24ч (нужны новые данные)")
        print("     — другой класс стратегий (funding-арбитраж, маркет-мейкинг)")
        print("     — принципиально другой триггер (не перегрев, а, напр., новостной/он-чейн)")


if __name__ == "__main__":
    main()
