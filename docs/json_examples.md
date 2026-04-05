# JSON Schema Examples

## 1. Signal Event (stored in `signals` table / emitted to alerts)

```json
{
  "symbol": "SHIBUSDT",
  "ts": 1712345678.42,
  "score": 78.3,
  "level": "critical",
  "signal_type": "overheated",
  "triggered_count": 7,
  "top_reasons": ["rsi", "vwap_extension", "volume_zscore"],
  "factors": [
    { "name": "rsi",               "raw_value": 86.4, "normalized": 0.82, "contribution": 12.3 },
    { "name": "vwap_extension",    "raw_value": 7.2,  "normalized": 1.0,  "contribution": 12.0 },
    { "name": "volume_zscore",     "raw_value": 4.1,  "normalized": 1.0,  "contribution": 12.0 },
    { "name": "trade_imbalance",   "raw_value": 0.68, "normalized": 0.86, "contribution": 8.6  },
    { "name": "large_buy_cluster", "raw_value": 9.0,  "normalized": 0.9,  "contribution": 9.0  },
    { "name": "price_acceleration","raw_value": 1.8,  "normalized": 0.87, "contribution": 8.7  },
    { "name": "consecutive_greens","raw_value": 7.0,  "normalized": 0.75, "contribution": 6.0  },
    { "name": "ob_bid_thinning",   "raw_value": -18.0,"normalized": 0.6,  "contribution": 4.8  },
    { "name": "spread_expansion",  "raw_value": 0.3,  "normalized": 0.17, "contribution": 0.8  },
    { "name": "momentum_loss",     "raw_value": 0.0,  "normalized": 0.0,  "contribution": 0.0  },
    { "name": "upper_wick",        "raw_value": 0.8,  "normalized": 0.0,  "contribution": 0.0  },
    { "name": "funding_rate",      "raw_value": 0.0,  "normalized": 0.0,  "contribution": 0.0  }
  ]
}
```

## 2. Overvalued Coin Item (stored in `overvalued_snapshots` + Redis)

```json
{
  "rank": 1,
  "symbol": "SHIBUSDT",
  "score": 78.3,
  "risk_level": "critical",
  "price": 0.00002847,
  "price_change_24h_pct": 42.1,
  "volume_24h_usdt": 14800000,
  "rsi": 86.4,
  "vwap_extension_pct": 7.2,
  "top_reasons": ["rsi", "vwap_extension", "volume_zscore"],
  "signal_type": "overheated"
}
```

## 3. Coin Diagnostic Snapshot (returned by /coin command)

```json
{
  "symbol": "SHIBUSDT",
  "ts": 1712345678.42,
  "last_price": 0.00002847,
  "volume_24h_usdt": 14800000,

  "trade_features": {
    "buy_volume_5m": 450000,
    "sell_volume_5m": 120000,
    "trade_imbalance_5m": 0.58,
    "large_buy_count_5m": 9,
    "large_sell_count_5m": 1,
    "large_trade_threshold_usdt": 15000
  },

  "candle_features": {
    "rsi_14_1m": 86.4,
    "rsi_14_5m": 79.2,
    "atr_14_1m": 0.0000012,
    "realized_vol_1h_pct": 3.4,
    "vwap_15m": 0.00002641,
    "vwap_extension_pct": 7.2,
    "price_change_1m_pct": 1.4,
    "price_change_5m_pct": 8.3,
    "price_change_15m_pct": 12.1,
    "price_acceleration": 0.9,
    "consecutive_green_candles": 7,
    "upper_wick_ratio": 0.8,
    "momentum_loss_signal": false,
    "volume_decline_after_spike": false
  },

  "orderbook_features": {
    "ob_imbalance": 0.34,
    "bid_depth_usdt_top10": 38000,
    "ask_depth_usdt_top10": 25000,
    "spread_pct": 0.18,
    "bid_depth_change_5m_pct": -18.0
  },

  "risk_score": {
    "score": 78.3,
    "level": "critical",
    "signal_type": "overheated",
    "triggered_count": 7
  }
}
```

## 4. User Alert Settings

```json
{
  "telegram_user_id": 123456789,
  "username": "trader_nick",
  "alerts_enabled": true,
  "min_score_to_alert": 50,
  "alert_cooldown_minutes": 60,
  "notify_early_warning": false,
  "notify_overheated": true,
  "notify_reversal_risk": true,
  "notify_dump_started": true,
  "watchlist": ["SHIBUSDT", "PEPEUSDT", "FLOKIUSDT"],
  "language": "en"
}
```

---

## Telegram Alert Message Format

```
🔴 🔥 Overheated

Symbol: SHIBUSDT
Risk Score: 78/100 (CRITICAL)
Price: $0.00002847
RSI: 86.4  |  VWAP Ext: +7.2%

Top Reasons:
  • RSI overbought (12.3pts)
  • Price above VWAP (12.0pts)
  • Volume spike (12.0pts)

⚠️ Not financial advice. Risk score ≥50 with 7 factors.
```
