"""
Strategy: Advanced EMA Crossover
- Multi-timeframe confirmation (30min + 4hour must agree)
- RSI divergence detection (price vs RSI direction)
- EMA 9/21 crossover
- RSI-7 filter

Upgrade from basic strategy:
- Adds 4h trend filter to eliminate false signals in choppy markets
- Adds RSI divergence for higher probability reversal detection
- Same webhook/3Commas setup, drop-in replacement
"""

import requests
import json
import time
import os
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────
# TIMEZONE
# ─────────────────────────────────────────
DUBAI_TZ = timezone(timedelta(hours=4))

# ─────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")

WEBHOOK_URL    = "https://api.3commas.io/signal_bots/webhooks"
WEBHOOK_SECRET = "eyJhbGciOiJIUzI1NiJ9.eyJzaWduYWxzX3NvdXJjZV9pZCI6MTMwNTYyfQ.DnbuKVB9cslOFa5l1WtrKH1PFsvacsV0Vfkh_e3E_DY"

BOT_UUIDS = {
    "BTCUSDT": "67d3e022-7414-4ef8-8b6c-3d5c56a09667",
    "ETHUSDT": "edac8b79-ac23-4af5-a6eb-666432a0cb57",
    "SOLUSDT": "3d72a934-50a2-4fd6-bbd2-0e678c841ef4",
    "XRPUSDT": "e798e648-fab5-4b94-82af-052228fa9ed1",
}

TICKER_MAP = {
    "BTCUSDT": "BTCUSDT",
    "ETHUSDT": "ETHUSDT",
    "SOLUSDT": "SOLUSDT",
    "XRPUSDT": "XRPUSDT",
}

COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "SOLUSDT": "solana",
    "XRPUSDT": "ripple",
}

SYMBOLS       = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
CANDLES_30M   = 30   # 30-min candles for entry signal
CANDLES_4H    = 30   # 4-hour candles for trend confirmation
TAKE_PROFIT   = 1.5
STOP_LOSS     = 3.0
MIN_CONFIDENCE = 65  # higher threshold for this strategy

LOG_FILE = "trade_log.csv"

# ─────────────────────────────────────────
# STEP 1 — Fetch OHLC from CoinGecko
# ─────────────────────────────────────────

def get_candles(symbol, days):
    """
    days=1  → returns ~48 x 30-min candles
    days=14 → returns ~30 x 4-hour candles
    """
    coin_id = COINGECKO_IDS[symbol]
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": str(days)}
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    response = requests.get(url, params=params, headers=headers, timeout=15)
    response.raise_for_status()
    raw = response.json()
    candles = []
    for c in raw:
        ts = datetime.fromtimestamp(c[0] / 1000, tz=DUBAI_TZ)
        candles.append({
            "time":  ts.strftime("%Y-%m-%d %H:%M"),
            "open":  float(c[1]),
            "high":  float(c[2]),
            "low":   float(c[3]),
            "close": float(c[4]),
        })
    return candles


# ─────────────────────────────────────────
# STEP 2 — Indicators
# ─────────────────────────────────────────

def calculate_ema(closes, period):
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 4)

def calculate_rsi(closes, period=7):
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 1.0  # extremely oversold — return 1 instead of 0 to avoid JSON issues
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_rsi_series(closes, period=7):
    """Return last N RSI values for divergence detection."""
    rsi_values = []
    for i in range(period + 1, len(closes) + 1):
        rsi_values.append(calculate_rsi(closes[:i], period))
    return rsi_values

def detect_divergence(closes, rsi_series, lookback=5):
    """
    Bullish divergence: price makes lower low but RSI makes higher low → BUY signal
    Bearish divergence: price makes higher high but RSI makes lower high → SELL signal
    Returns: 'bullish', 'bearish', or None
    """
    if len(closes) < lookback or len(rsi_series) < lookback:
        return None

    recent_closes = closes[-lookback:]
    recent_rsi    = rsi_series[-lookback:]

    price_low_now   = recent_closes[-1] < min(recent_closes[:-1])
    rsi_low_now     = recent_rsi[-1]    < min(recent_rsi[:-1])
    price_high_now  = recent_closes[-1] > max(recent_closes[:-1])
    rsi_high_now    = recent_rsi[-1]    > max(recent_rsi[:-1])

    # Bullish: price lower low but RSI NOT lower low (higher low)
    if price_low_now and not rsi_low_now:
        return "bullish"

    # Bearish: price higher high but RSI NOT higher high (lower high)
    if price_high_now and not rsi_high_now:
        return "bearish"

    return None

def get_indicators(candles_30m, candles_4h):
    """Calculate all indicators for both timeframes."""
    closes_30m = [c["close"] for c in candles_30m[-30:]]
    closes_4h  = [c["close"] for c in candles_4h[-30:]]

    # 30-min indicators (entry signals)
    ema9_30m  = calculate_ema(closes_30m, 9)
    ema21_30m = calculate_ema(closes_30m, 21)
    rsi7_30m  = calculate_rsi(closes_30m, 7)

    # 4-hour indicators (trend confirmation)
    ema9_4h   = calculate_ema(closes_4h, 9)
    ema21_4h  = calculate_ema(closes_4h, 21)
    rsi7_4h   = calculate_rsi(closes_4h, 7)

    # RSI divergence on 30-min
    rsi_series_30m  = calculate_rsi_series(closes_30m, 7)
    divergence      = detect_divergence(closes_30m, rsi_series_30m)

    # 4h trend direction
    trend_4h = "bullish" if ema9_4h > ema21_4h else "bearish"

    return {
        "ema9_30m":   ema9_30m,
        "ema21_30m":  ema21_30m,
        "rsi7_30m":   rsi7_30m,
        "ema9_4h":    ema9_4h,
        "ema21_4h":   ema21_4h,
        "rsi7_4h":    rsi7_4h,
        "trend_4h":   trend_4h,
        "divergence": divergence,
    }


# ─────────────────────────────────────────
# STEP 3 — Ask Claude
# ─────────────────────────────────────────

SYSTEM_PROMPT = """You are a professional crypto trading signal engine. Output ONLY a raw JSON object.

STRICT RULES:
- Your ENTIRE response must be one JSON object, nothing else
- No text before or after the JSON
- No markdown, no backticks, no explanation

STRATEGY: Advanced EMA + Multi-Timeframe + RSI Divergence

SIGNAL RULES (all conditions evaluated together):
1. PRIMARY (30-min EMA crossover):
   - EMA9_30m > EMA21_30m = bullish bias
   - EMA9_30m < EMA21_30m = bearish bias

2. CONFIRMATION (4-hour trend must agree):
   - 4h trend = bullish → only BUY signals pass
   - 4h trend = bearish → only SELL signals pass
   - If 30-min and 4h disagree → HOLD (this is the key false signal filter)

3. RSI FILTER (30-min):
   - RSI7 < 65 required for BUY
   - RSI7 > 35 required for SELL

4. RSI DIVERGENCE (bonus confidence boost):
   - Bullish divergence present → adds +15 confidence to BUY
   - Bearish divergence present → adds +15 confidence to SELL
   - Divergence opposing signal → subtract 10 confidence

5. OVERRIDE:
   - RSI7_30m > 75 → SELL regardless (extremely overbought)
   - RSI7_30m < 25 → BUY regardless (extremely oversold)

CONFIDENCE SCORING:
- Start at 50
- 30m EMA agrees with signal: +15
- 4h trend agrees with signal: +20 (most important)
- RSI in safe zone: +10
- Divergence confirms: +15
- Any disagreement: -10 to -20

Required output:
{"signal":"BUY","confidence":75,"reasoning":"30m bullish EMA crossover confirmed by 4h uptrend, RSI not overbought"}"""


def ask_claude(symbol, indicators):
    msg = (
        f"Symbol: {symbol}\n"
        f"--- 30-minute timeframe ---\n"
        f"EMA-9: {indicators['ema9_30m']}\n"
        f"EMA-21: {indicators['ema21_30m']}\n"
        f"RSI-7: {indicators['rsi7_30m']}\n"
        f"RSI Divergence: {indicators['divergence'] or 'none'}\n"
        f"--- 4-hour timeframe ---\n"
        f"EMA-9: {indicators['ema9_4h']}\n"
        f"EMA-21: {indicators['ema21_4h']}\n"
        f"RSI-7: {indicators['rsi7_4h']}\n"
        f"4H Trend: {indicators['trend_4h']}\n"
        f"---\n"
        f"Apply all strategy rules and return your signal as JSON."
    )
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 250,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": msg}]
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=payload,
        timeout=30
    )
    response.raise_for_status()
    raw_text = response.json()["content"][0]["text"].strip()
    if raw_text.startswith("```"):
        parts = raw_text.split("```")
        raw_text = parts[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
    return json.loads(raw_text.strip())


# ─────────────────────────────────────────
# STEP 4 — Fire webhook
# ─────────────────────────────────────────

def fire_webhook(signal_str, current_price, symbol):
    action = "enter_long" if signal_str == "BUY" else "enter_short"
    ticker = TICKER_MAP.get(symbol, symbol)
    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    tp_pct = TAKE_PROFIT if signal_str == "BUY" else -TAKE_PROFIT
    sl_pct = STOP_LOSS

    payload = {
        "secret":        WEBHOOK_SECRET,
        "max_lag":       "300",
        "timestamp":     now_iso,
        "trigger_price": str(current_price),
        "tv_exchange":   "BINANCE",
        "tv_instrument": ticker,
        "action":        action,
        "bot_uuid":      BOT_UUIDS[symbol],
        "take_profit": {
            "enabled": True,
            "steps": [{
                "order_type": "market",
                "price_percent": tp_pct,
                "volume_percent": 100
            }]
        },
        "stop_loss": {
            "enabled": True,
            "order_type": "market",
            "trigger_price_percent": sl_pct
        }
    }

    response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    if response.status_code == 200:
        print(f"  Webhook {action}: SUCCESS")
    elif response.status_code == 429:
        print(f"  Webhook: RATE LIMITED (429)")
    elif response.status_code == 418:
        print(f"  Webhook: BLOCKED (418)")
    else:
        print(f"  Webhook: FAILED [{response.status_code}] {response.text}")
    return response.status_code == 200


# ─────────────────────────────────────────
# STEP 5 — Log
# ─────────────────────────────────────────

def log_result(symbol, signal, indicators, price, fired):
    timestamp = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    write_header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a") as f:
        if write_header:
            f.write("timestamp_dubai,symbol,price,signal,confidence,"
                    "ema9_30m,ema21_30m,rsi7_30m,divergence,"
                    "ema9_4h,ema21_4h,rsi7_4h,trend_4h,"
                    "webhook_fired,reasoning\n")
        reasoning = signal.get("reasoning", "").replace('"', "'")
        f.write(
            f'{timestamp},{symbol},{price},{signal["signal"]},'
            f'{signal["confidence"]},'
            f'{indicators["ema9_30m"]},{indicators["ema21_30m"]},'
            f'{indicators["rsi7_30m"]},{indicators["divergence"] or "none"},'
            f'{indicators["ema9_4h"]},{indicators["ema21_4h"]},'
            f'{indicators["rsi7_4h"]},{indicators["trend_4h"]},'
            f'{fired},"{reasoning}"\n'
        )
    print(f"  [{timestamp} Dubai] {symbol} | {signal['signal']} | "
          f"Confidence: {signal['confidence']}% | 4H: {indicators['trend_4h']} | "
          f"Divergence: {indicators['divergence'] or 'none'} | Fired: {fired}")


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*56}")
    print(f"Advanced EMA Strategy — {now} Dubai time")
    print(f"{'='*56}")

    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            # Fetch both timeframes
            print(f"  Fetching 30m candles...")
            candles_30m = get_candles(symbol, days=1)
            time.sleep(2)  # respect CoinGecko rate limit
            print(f"  Fetching 4h candles...")
            candles_4h  = get_candles(symbol, days=14)
            time.sleep(2)

            current_price = candles_30m[-1]["close"]
            print(f"  Latest close: ${current_price:,.4f}")

            # Calculate all indicators
            indicators = get_indicators(candles_30m, candles_4h)
            print(f"  30m → EMA9: {indicators['ema9_30m']} | EMA21: {indicators['ema21_30m']} | RSI7: {indicators['rsi7_30m']}")
            print(f"  4h  → Trend: {indicators['trend_4h']} | RSI7: {indicators['rsi7_4h']}")
            print(f"  Divergence: {indicators['divergence'] or 'none'}")

            # Ask Claude
            signal = ask_claude(symbol, indicators)
            print(f"  Signal: {signal['signal']} | Confidence: {signal['confidence']}% | {signal.get('reasoning','')}")

            # Fire webhook if confidence meets threshold
            webhook_fired = False
            if signal["signal"] in ("BUY", "SELL") and signal["confidence"] >= MIN_CONFIDENCE:
                webhook_fired = fire_webhook(signal["signal"], current_price, symbol)
            else:
                print(f"  HOLD — no webhook fired.")

            log_result(symbol, signal, indicators, current_price, webhook_fired)
            time.sleep(3)

        except Exception as e:
            print(f"  ERROR on {symbol}: {e}")
            continue

    print(f"\n{'='*56}")
    print("Run complete.")
    print(f"{'='*56}\n")


if __name__ == "__main__":
    run()
