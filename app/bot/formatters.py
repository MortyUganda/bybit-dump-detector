"""
Message formatters — convert domain objects to Telegram HTML strings.
"""
from __future__ import annotations

from app.scoring.engine import RiskScore, SignalType
from app.analytics.features import CoinFeatures
from app.bot.keyboards import risk_level_emoji, signal_type_emoji


SIGNAL_TYPE_LABEL = {
    SignalType.EARLY_WARNING: "⚠️ Early Warning",
    SignalType.OVERHEATED:    "🔥 Overheated",
    SignalType.REVERSAL_RISK: "⬇️ Reversal Risk",
    SignalType.DUMP_STARTED:  "💥 Dump Started",
}

FACTOR_LABELS = {
    "rsi":                "RSI overbought",
    "vwap_extension":     "Price above VWAP",
    "volume_zscore":      "Volume spike",
    "trade_imbalance":    "Buy dominance",
    "large_buy_cluster":  "Large buy cluster",
    "price_acceleration": "Price acceleration",
    "consecutive_greens": "Consecutive green candles",
    "ob_bid_thinning":    "Bid depth thinning",
    "spread_expansion":   "Spread expansion",
    "momentum_loss":      "Momentum loss",
    "upper_wick":         "Upper wick rejection",
    "funding_rate":       "High funding rate",
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

    return (
        f"{level_em} <b>{signal_label}</b>\n"
        f"<b>Symbol:</b> <code>{score.symbol}</code>\n"
        f"<b>Risk Score:</b> {score.score:.0f}/100 ({score.level.value.upper()})\n"
        f"<b>Price:</b> {price_line}\n"
        f"<b>RSI:</b> {rsi_line}  |  <b>VWAP Ext:</b> {vwap_line}\n\n"
        f"<b>Top Reasons:</b>\n{top_reasons_text}\n"
        f"<i>⚠️ Not financial advice. Risk score ≥50 with {score.triggered_count} factors.</i>"
    )


def format_overvalued_list(items: list[dict], page: int = 0, total: int = 0) -> str:
    if not items:
        return "📊 <b>Переоценённые монеты</b>\n\n<i>Сейчас нет монет, отмеченных как высокорисковые.</i>"

    lines = ["📊 <b>Переоценённые монеты</b> — рейтинг по уровню риска\n"]
    for i, item in enumerate(items, start=1 + page * 10):
        em = risk_level_emoji(item.get("risk_level", "low"))
        sym = item.get("symbol", "?")
        score = item.get("score", 0)
        rsi = item.get("rsi", 0)
        vwap = item.get("vwap_extension_pct", 0)
        change = item.get("price_change_24h_pct", 0)
        lines.append(
            f"{i}. <code>{sym}</code> {em} <b>{score:.0f}</b> "
            f"| RSI:{rsi:.0f} | VWAP+{vwap:.1f}% | 24ч:{change:+.1f}%"
        )

    lines.append(f"\n<i>Обновляется каждые 5 минут. Используй /coin SYMBOL для деталей.</i>")
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
