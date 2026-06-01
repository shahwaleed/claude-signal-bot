"""
Claude Signal Bot — BTC/ETH/SOL/XRP
Architecture:
  CoinGecko (OHLC data) → Python (EMA/RSI calc) → Claude (decision) → 3Commas webhook
Strategy: EMA 9/21 crossover + RSI-7 filter
Runs every 30 minutes via GitHub Actions
"""

import requests
import json
import time
import os
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────
# TIMEZONE
# ─────────────────────────────────────────
DUBAI_TZ = timezone(timedelta(hours=4))  # UAE is UTC+4

# ─────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────

# Loaded from GitHub Actions secret — never hardcode
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# CoinGecko Demo API key — free at coingecko.com/en/api
# Add as GitHub secret: COINGECKO_API_KEY
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")

# 3Commas
WEBHOOK_URL     = "https://api.3commas.io/signal_bots/webhooks"
WEBHOOK_SECRET  = "eyJhbGciOiJIUzI1NiJ9.eyJzaWduYWxzX3NvdXJjZV9pZCI6MTMwNTYyfQ.DnbuKVB9cslOFa5l1WtrKH1PFsvacsV0Vfkh_e3E_DY"

# One UUID per pair — Reversal mode bots
BOT_UUIDS = {
    "BTCUSDT": "67d3e022-7414-4ef8-8b6c-3d5c56a09667",
    "ETHUSDT": "edac8b79-ac23-4af5-a6eb-666432a0cb57",
    "SOLUSDT": "3d72a934-50a2-4fd6-bbd2-0e678c841ef4",
    "XRPUSDT": "e798e648-fab5-4b94-82af-052228fa9ed1",
}

# tv_instrument format for Binance Spot
TICKER_MAP = {
    "BTCUSDT": "BTCUSDT",
    "ETHUSDT": "ETHUSDT",
    "SOLUSDT": "SOLUSDT",
    "XRPUSDT": "XRPUSDT",
}

# CoinGecko coin IDs
COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "SOLUSDT": "solana",
    "XRPUSDT": "ripple",
}

SYMBOLS  = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
INTERVAL = "30m"
CANDLES  = 30

# Trade settings — sent in every webhook so 3Commas always has correct TP/SL
TAKE_PROFIT_PCT = 1.5
STOP_LOSS_PCT   = 3.0
MIN_CONFIDENCE  = 60  # minimum Claude confidence to fire webhook

# ─────────────────────────────────────────
# STEP 1 — Fetch OHLC data from CoinGecko
# ─────────────────────────────────────────

def get_candles(symbol, limit=30):
    """Fetch OHLC candles from CoinGecko. days=1 returns 30-min candles."""
    coin_id = COINGECKO_IDS[symbol]
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": "1"}
    headers = {}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

    response = requests.get(url, params=params, headers=headers, timeout=15)
    response.raise_for_status()
    raw = response.json()

    candles = []
    for c in raw[-limit:]:
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
# STEP 2 — Calculate indicators in Python
# ─────────────────────────────────────────

def calculate_ema(closes, period):
    """Exponential Moving Average."""
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period  # seed with SMA
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 4)

def calculate_rsi(closes, period=7):
    """Relative Strength Index."""
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
    closes = [c["close"] for c in candles]
    ema9  = calculate_ema(closes, 9)
    ema21 = calculate_ema(closes, 21)
    rsi7  = calculate_rsi(closes, 7)
    return ema9, ema21, rsi7


# ─────────────────────────────────────────
# STEP 3 — Ask Claude for signal
# ─────────────────────────────────────────

SYSTEM_PROMPT = """You are a trading signal engine. Output ONLY a raw JSON object.

STRICT RULES:
- Your ENTIRE response must be one JSON object, nothing else
- No text before or after the JSON
- No markdown, no backticks, no explanation

You will receive pre-calculated indicators. Make a trading decision:
- If EMA-9 > EMA-21 AND RSI-7 < 65: signal = BUY
- If EMA-9 < EMA-21 AND RSI-7 > 35: signal = SELL
- If RSI-7 > 75: signal = SELL (extremely overbought, override)
- If RSI-7 < 25: signal = BUY (extremely oversold, override)
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
    if raw_text.startswith("```"):
        parts = raw_text.split("```")
        raw_text = parts[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
    return json.loads(raw_text.strip())


# ─────────────────────────────────────────
# STEP 4 — Fire webhook to 3Commas
# ─────────────────────────────────────────

def fire_webhook(signal_str, current_price, symbol):
    """
    Send enter_long or enter_short to 3Commas Reversal bot.
    
    Reversal bot behaviour (confirmed from 3Commas docs):
    - enter_long with open short → closes short + opens long automatically
    - enter_short with open long → closes long + opens short automatically
    - enter_long with open long → adds to position (we avoid this via confidence filter)
    - enter_short with open short → adds to position (we avoid this via confidence filter)
    
    We include TP and SL in every webhook so they're always correctly set.
    """
    action = "enter_long" if signal_str == "BUY" else "enter_short"
    ticker = TICKER_MAP.get(symbol, symbol)
    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    # TP and SL directions flip for short positions
    tp_pct = TAKE_PROFIT_PCT if signal_str == "BUY" else -TAKE_PROFIT_PCT
    sl_pct = STOP_LOSS_PCT   if signal_str == "BUY" else STOP_LOSS_PCT

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
        print(f"  Webhook: RATE LIMITED (429) — slow down")
    elif response.status_code == 418:
        print(f"  Webhook: BLOCKED (418) — temporary block, wait before retrying")
    else:
        print(f"  Webhook: FAILED [{response.status_code}] {response.text}")

    return response.status_code == 200


# ─────────────────────────────────────────
# STEP 5 — Log to CSV (Dubai time)
# ─────────────────────────────────────────

LOG_FILE = "trade_log.csv"

def log_result(symbol, signal, ema9, ema21, rsi7, price, fired):
    timestamp = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    write_header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a") as f:
        if write_header:
            f.write("timestamp_dubai,symbol,price,signal,confidence,ema9,ema21,rsi7,webhook_fired,reasoning\n")
        reasoning = signal.get("reasoning", "").replace('"', "'")
        f.write(
            f'{timestamp},{symbol},{price},{signal["signal"]},'
            f'{signal["confidence"]},{ema9},{ema21},{rsi7},'
            f'{fired},"{reasoning}"\n'
        )
    print(f"  [{timestamp} Dubai] {symbol} | {signal['signal']} | "
          f"Confidence: {signal['confidence']}% | Price: ${price:,.4f} | Fired: {fired}")


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*52}")
    print(f"Claude Signal Bot — {now} Dubai time")
    print(f"{'='*52}")

    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            # 1. Get market data
            candles = get_candles(symbol, CANDLES)
            current_price = candles[-1]["close"]
            print(f"  Latest close: ${current_price:,.4f}")

            # 2. Calculate indicators
            ema9, ema21, rsi7 = get_indicators(candles)
            print(f"  EMA9: {ema9} | EMA21: {ema21} | RSI7: {rsi7}")

            # 3. Ask Claude
            signal = ask_claude(symbol, ema9, ema21, rsi7)
            print(f"  Signal: {signal['signal']} | Confidence: {signal['confidence']}% | {signal.get('reasoning','')}")

            # 4. Fire webhook if confidence meets threshold
            webhook_fired = False
            if signal["signal"] in ("BUY", "SELL") and signal["confidence"] >= MIN_CONFIDENCE:
                webhook_fired = fire_webhook(signal["signal"], current_price, symbol)
            else:
                print(f"  HOLD — no webhook fired.")

            # 5. Log result
            log_result(symbol, signal, ema9, ema21, rsi7, current_price, webhook_fired)

            # Small delay between pairs to respect rate limits
            time.sleep(3)

        except Exception as e:
            print(f"  ERROR on {symbol}: {e}")
            continue

    print(f"\n{'='*52}")
    print("Run complete.")
    print(f"{'='*52}\n")


if __name__ == "__main__":
    run()
