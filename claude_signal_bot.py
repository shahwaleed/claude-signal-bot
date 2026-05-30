"""
Claude Signal Bot — BTC/USDT
Connects Binance (Testnet) -> Claude AI -> 3Commas webhook
Strategy: EMA 9/21 crossover + RSI-7 filter
Run every 15 minutes via cron or scheduler
"""

import requests
import json
import time
import os
from datetime import datetime, timezone, timedelta

DUBAI_TZ = timezone(timedelta(hours=4))  # UAE is UTC+4


# ─────────────────────────────────────────
# INDICATOR CALCULATIONS (pure Python)
# ─────────────────────────────────────────

def calculate_ema(closes, period):
    """Calculate EMA for a given period."""
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period  # seed with SMA
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)

def calculate_rsi(closes, period=7):
    """Calculate RSI for a given period."""
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def get_indicators(candles):
    """Extract closing prices and compute all indicators."""
    closes = [c["close"] for c in candles]
    ema9  = calculate_ema(closes, 9)
    ema21 = calculate_ema(closes, 21)
    rsi7  = calculate_rsi(closes, 7)
    return ema9, ema21, rsi7

# ─────────────────────────────────────────
# CONFIGURATION — fill these in
# ─────────────────────────────────────────

ANTHROPIC_API_KEY = "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"   # https://console.anthropic.com

# Binance Testnet (paper trading) — no real funds
BINANCE_BASE_URL = "https://testnet.binance.vision/api"
# For real trading later, swap to: "https://api.binance.com/api"

# 3Commas webhook (from your Signal Bot setup)
WEBHOOK_URL = "https://api.3commas.io/signal_bots/webhooks"

WEBHOOK_SECRET = "eyJhbGciOiJIUzI1NiJ9.eyJzaWduYWxzX3NvdXJjZV9pZCI6MTMwNTYyfQ.DnbuKVB9cslOFa5l1WtrKH1PFsvacsV0Vfkh_e3E_DY"

BOT_UUIDS = {
    "BTCUSDT": "67d3e022-7414-4ef8-8b6c-3d5c56a09667",
    "ETHUSDT": "edac8b79-ac23-4af5-a6eb-666432a0cb57",
    "SOLUSDT": "3d72a934-50a2-4fd6-bbd2-0e678c841ef4",
    "XRPUSDT": "e798e648-fab5-4b94-82af-052228fa9ed1",
}

# 3Commas expects ticker in "BASE/QUOTE" format
TICKER_MAP = {
    "BTCUSDT": "BTCUSDT",
    "ETHUSDT": "ETHUSDT",
    "SOLUSDT": "SOLUSDT",
    "XRPUSDT": "XRPUSDT",
}

SYMBOLS  = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
INTERVAL = "30m"
CANDLES  = 30

# ─────────────────────────────────────────
# STEP 1 — Fetch OHLCV data from Binance
# ─────────────────────────────────────────

def get_candles(symbol, interval, limit):
    url = f"{BINANCE_BASE_URL}/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    raw = response.json()
    candles = []
    for c in raw:
        ts = datetime.fromtimestamp(c[0] / 1000, tz=DUBAI_TZ)
        candles.append({
            "time":   ts.strftime("%Y-%m-%d %H:%M"),
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]),
        })
    return candles


# ─────────────────────────────────────────
# STEP 2 — Call Claude for a signal
# ─────────────────────────────────────────

SYSTEM_PROMPT = """You are a trading signal engine. Output ONLY a raw JSON object.

STRICT RULES:
- Your ENTIRE response must be one JSON object, nothing else
- No text before or after the JSON
- No markdown, no backticks, no explanation

You will receive pre-calculated indicators. Make a trading decision based on:
- If EMA-9 > EMA-21 AND RSI-7 < 65: signal = BUY
- If EMA-9 < EMA-21 AND RSI-7 > 35: signal = SELL
- If RSI-7 > 75: signal = SELL (extremely overbought)
- If RSI-7 < 25: signal = BUY (extremely oversold)
- Otherwise: signal = HOLD

Be decisive. HOLD only when signals genuinely conflict.

Required output format:
{"signal":"BUY","confidence":75,"reasoning":"EMA-9 above EMA-21 with RSI not overbought"}"""


def ask_claude(symbol, ema9, ema21, rsi7):
    user_message = (
        f"Symbol: {symbol}\n"
        f"EMA-9: {ema9}\n"
        f"EMA-21: {ema21}\n"
        f"RSI-7: {rsi7}\n"
        f"Return your trading signal as JSON."
    )

    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 150,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message}]
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

    # Strip accidental markdown fences
    if raw_text.startswith("```"):
        parts = raw_text.split("```")
        raw_text = parts[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]

    return json.loads(raw_text.strip())


# ─────────────────────────────────────────
# STEP 3 — Fire webhook to 3Commas
# ─────────────────────────────────────────

def fire_webhook(signal, current_price, symbol="BTCUSDT"):
    # For reversal bot: BUY = enter_long (closes short + opens long)
    #                   SELL = enter_short (closes long + opens short)
    action = "enter_long" if signal["signal"] == "BUY" else "enter_short"
    ticker = TICKER_MAP.get(symbol, symbol)
    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")  # webhook always UTC
    payload = {
        "secret":        WEBHOOK_SECRET,
        "max_lag":       "300",
        "timestamp":     now_iso,
        "trigger_price": str(current_price),
        "tv_exchange":   "BINANCE",
        "tv_instrument": ticker,
        "action":        action,
        "bot_uuid":      BOT_UUIDS[symbol]
    }
    print(f"Payload: {json.dumps(payload)}")
    response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    print(f"3Commas response [{response.status_code}]: '{response.text}'")
    if response.status_code == 200:
        print(f"Webhook: SUCCESS")
    elif response.status_code == 429:
        print(f"Webhook: RATE LIMITED (429) — too many requests, slow down")
    elif response.status_code == 418:
        print(f"Webhook: TEMPORARILY BLOCKED (418) — wait 2 min to 3 days before retrying")
    else:
        print(f"Webhook: FAILED [{response.status_code}] {response.text}")
    return response.status_code == 200


# ─────────────────────────────────────────
# STEP 4 — Log every run
# ─────────────────────────────────────────

def log_result(signal, fired, current_price):
    timestamp = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_log.csv")
    write_header = not os.path.exists(log_file) or os.path.getsize(log_file) == 0
    with open(log_file, "a") as f:
        if write_header:
            f.write("timestamp,price,signal,confidence,ema9,ema21,rsi7,webhook_fired,reasoning\n")
        f.write(
            f"{timestamp},{current_price},{signal['signal']},"
            f"{signal['confidence']},{signal.get('ema9','')},"
            f"{signal.get('ema21','')},{signal.get('rsi7','')},"
            f"{fired},\"{signal.get('reasoning','')}\"\n"
        )
    print(f"[{timestamp}] {signal.get('symbol','BTC')} | {signal['signal']} | Confidence: {signal['confidence']}% | Price: ${current_price:,.2f} | Webhook fired: {fired}")


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*50}")
    print(f"Claude Signal Bot — {now} UTC")
    print(f"{'='*50}")

    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            print(f"Fetching candles...")
            candles = get_candles(symbol, INTERVAL, CANDLES)
            current_price = candles[-1]["close"]
            print(f"Latest close: ${current_price:,.2f}")

            ema9, ema21, rsi7 = get_indicators(candles)
            print(f"EMA9: {ema9} | EMA21: {ema21} | RSI7: {rsi7}")

            print("Asking Claude for signal...")
            signal = ask_claude(symbol, ema9, ema21, rsi7)
            signal["symbol"] = symbol
            signal["ema9"]   = ema9
            signal["ema21"]  = ema21
            signal["rsi7"]   = rsi7
            print(f"Signal: {signal['signal']} | Confidence: {signal['confidence']}% | RSI7: {signal.get('rsi7', 'N/A')}")
            print(f"Reasoning: {signal.get('reasoning', '')}")

            webhook_fired = False
            if signal["signal"] in ("BUY", "SELL") and signal["confidence"] >= 60:
                print(f"Firing webhook to 3Commas: {signal['signal']}...")
                webhook_fired = fire_webhook(signal, current_price, symbol)
            else:
                print("HOLD — no webhook fired.")

            log_result(signal, webhook_fired, current_price)
            time.sleep(2)  # small delay between pairs to avoid rate limits

        except Exception as e:
            print(f"ERROR on {symbol}: {e}")
            continue


if __name__ == "__main__":
    run()
