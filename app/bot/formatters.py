"""
Message formatters — convert domain objects to Telegram HTML strings.
"""

from __future__ import annotations

from app.analytics.features import CoinFeatures
from app.bot.keyboards import risk_level_emoji, signal_type_emoji
from app.scoring.engine import RiskScore, SignalType

SIGNAL_TYPE_LABEL = {
    SignalType.EARLY_WARNING: "⚠️ Early Warning",
    SignalType.OVERHEATED: "🔥 Overheated",
    SignalType.REVERSAL_RISK: "⬇️ Reversal Risk",
    SignalType.DUMP_STARTED: "💥 Dump Started",
}

FACTOR_LABELS = {
    "rsi": "RSI overbought",
    "vwap_extension": "Price above VWAP",
    "volume_zscore": "Volume spike",
    "trade_imbalance": "Buy dominance",
    "large_buy_cluster": "Large buy cluster",
    "price_acceleration": "Price acceleration",
    "consecutive_greens": "Consecutive green candles",
    "ob_bid_thinning": "Bid depth thinning",
    "spread_expansion": "Spread expansion",
    "momentum_loss": "Momentum loss",
    "upper_wick": "Upper wick rejection",
    "funding_rate": "High funding rate",
}


def format_risk_alert(score: RiskScore) -> str:
    """Full alert message for a triggered signal."""
    level_em = risk_level_emoji(score.level.value)
    signal_label = SIGNAL_TYPE_LABEL.get(score.signal_type, "📊 Signal")

    top_reasons_text = ""
    for f in sorted(score.factors, key=lambda x: -x.contribution):
        if f.triggered:
            label = FACTOR_LABELS.get(f.name, f.name)
            top_reasons_text += f"  • {label} ({f.contribution:.1f}pts)\n"
            if top_reasons_text.count("•") >= 3:
                break

    features = score.features_snapshot
    price_line = f"${features.last_price:.6g}" if features and features.last_price else "N/A"
    rsi_line = f"{features.rsi_14_1m:.1f}" if features else "N/A"
    vwap_line = f"+{features.vwap_extension_pct:.1f}%" if features else "N/A"

    lines = [
        f"{level_em} <b>{signal_label}</b>",
        f"<b>Symbol:</b> <code>{score.symbol}</code>",
        f"<b>Risk Score:</b> {score.score:.0f}/100 ({score.level.value.upper()})",
        f"<b>Price:</b> {price_line}",
        f"<b>RSI:</b> {rsi_line}  |  <b>VWAP Ext:</b> {vwap_line}",
        "",
        f"<b>Top Reasons:</b>",
        top_reasons_text.rstrip(),
    ]

    # ── Context lines from features_snapshot ──────────────────────
    if features:
        context_lines = []

        # BTC context
        btc_change = features.btc_change_15m
        if btc_change is not None:
            context_lines.append(
                f"📌 BTC: <b>{btc_change:+.1f}%</b> (15m)"
            )

        # Funding rate
        funding = features.funding_rate
        if funding is not None:
            context_lines.append(f"💸 Funding: <b>{funding:.4f}%</b>")

        # Trend context
        trend_ctx = features.trend_context
        if trend_ctx and trend_ctx.trend_strength is not None:
            ts_val = trend_ctx.trend_strength
            if ts_val > 0.3:
                context_lines.append(
                    f"📈 Тренд 1h: аптренд (<b>{ts_val:+.1f}</b>)"
                )
            elif ts_val < -0.3:
                context_lines.append(
                    f"📉 Тренд 1h: даунтренд (<b>{ts_val:+.1f}</b>)"
                )
            else:
                context_lines.append(
                    f"➡️ Тренд 1h: боковик (<b>{ts_val:+.1f}</b>)"
                )

        # CVD divergence
        cvd = features.cvd_divergence
        if cvd is not None and abs(cvd) > 0.2:
            cvd_label = "медвежья" if cvd < 0 else "бычья"
            context_lines.append(
                f"⚡ CVD дивергенция: {cvd_label} (<b>{cvd:.2f}</b>)"
            )

        if context_lines:
            lines.append("")
            lines.extend(context_lines)

    lines.append("")
    lines.append(
        f"<i>⚠️ Не является финансовой рекомендацией. "
        f"Score ≥50, {score.triggered_count} факторов.</i>"
    )

    return "\n".join(lines)


def format_overvalued_list(items: list[dict], page: int = 0, total: int = 0) -> str:
    if not items:
        return "📊 <b>Переоценённые монеты</b>\n\n<i>Сейчас нет монет с повышенным риском.</i>"

    lines = ["📊 <b>Переоценённые монеты</b> — рейтинг по уровню риска\n"]

    for i, item in enumerate(items, start=1 + page * 10):
        em = risk_level_emoji(item.get("risk_level", "low"))
        sym = item.get("symbol", "?")
        score = item.get("score", 0)
        rsi = item.get("rsi", 0)
        vwap = item.get("vwap_extension_pct", 0)
        change = item.get("price_change_24h_pct", 0)

        # Убираем USDT из символа для ссылки
        base = sym.replace("USDT", "")

        # Ссылка на Bybit фьючерсы
        bybit_url = f"https://www.bybit.com/trade/usdt/{base}USDT"

        change_em = "🟢" if change >= 0 else "🔴"

        lines.append(
            f'{i}. <a href="{bybit_url}">{sym}</a> {em} <b>{score:.0f}</b> '
            f"| RSI:{rsi:.0f} | VWAP+{vwap:.1f}% | {change_em}{change:+.1f}%"
        )

    lines.append("\n<i>Обновляется каждые 5 мин. Используй /coin SYMBOL для деталей.</i>")
    return "\n".join(lines)


def format_coin_diagnostic(symbol: str, score: RiskScore, features: CoinFeatures) -> str:
    em = risk_level_emoji(score.level.value)
    signal_label = SIGNAL_TYPE_LABEL.get(score.signal_type, "No active signal")

    factor_lines = []
    for f in sorted(score.factors, key=lambda x: -x.contribution):
        bar = "▓" * int(f.normalized * 5) + "░" * (5 - int(f.normalized * 5))
        label = FACTOR_LABELS.get(f.name, f.name)
        factor_lines.append(f"  {bar} {label}: {f.contribution:.1f}pts")

    return (
        f"🔍 <b>{symbol} Diagnostics</b>\n\n"
        f"{em} <b>Risk Score: {score.score:.0f}/100</b> ({score.level.value.upper()})\n"
        f"🎯 Signal: {signal_label}\n\n"
        f"<b>Key Metrics:</b>\n"
        f"  Price: ${features.last_price:.6g}\n"
        f"  RSI (1m): {features.rsi_14_1m:.1f}\n"
        f"  VWAP Ext: {features.vwap_extension_pct:+.2f}%\n"
        f"  Volume Z-Score: {features.volume_zscore_1m:.2f}σ\n"
        f"  Buy Imbalance: {features.trade_imbalance_5m:.2f}\n"
        f"  Large Buys (5m): {features.large_buy_count_5m}\n"
        f"  Green Candles: {features.consecutive_green_candles}\n"
        f"  Bid Depth Δ: {features.bid_depth_change_5m:+.1f}%\n"
        f"  Spread: {features.spread_pct:.3f}%\n"
        f"  Momentum Loss: {'Yes ⚠️' if features.momentum_loss_signal else 'No'}\n\n"
        f"<b>Factor Breakdown:</b>\n"
        + "\n".join(factor_lines[:8])
        + f"\n\n<i>Use /add {symbol} to watchlist for priority alerts.</i>"
    )


def format_signal_list(signals: list[dict]) -> str:
    if not signals:
        return "📡 <b>Recent Signals</b>\n\n<i>No signals yet.</i>"

    lines = ["📡 <b>Recent Signals</b>\n"]
    for s in signals:
        em = risk_level_emoji(s.get("risk_level", "low"))
        sig_em = signal_type_emoji(s.get("signal_type", ""))
        lines.append(
            f"{sig_em} <code>{s['symbol']}</code> {em} {s['score']:.0f}pts "
            f"— {s.get('signal_type', '').replace('_', ' ').title()}"
        )
    return "\n".join(lines)
