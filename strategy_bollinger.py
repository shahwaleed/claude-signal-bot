"""
Strategy: Bollinger Band Mean Reversion
Best for: Choppy, sideways, ranging markets

Logic:
- Price touches/breaks lower BB + RSI oversold → BUY
- Price touches/breaks upper BB + RSI overbought → SELL
- Take profit = middle band, Stop loss = 3%

CoinGecko OHLC: days=7 → 4h candles (~42) for BB-20 + RSI-14
"""

import requests
import json
import re
import csv
import time
import os
import math
from datetime import datetime, timezone, timedelta

DUBAI_TZ = timezone(timedelta(hours=4))
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
TICKER_MAP = {"BTCUSDT": "BTCUSDT", "ETHUSDT": "ETHUSDT", "SOLUSDT": "SOLUSDT", "XRPUSDT": "XRPUSDT"}
COINGECKO_IDS = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "SOLUSDT": "solana", "XRPUSDT": "ripple"}
SYMBOLS        = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
BB_PERIOD      = 20
BB_STD         = 2.0
RSI_PERIOD     = 14
STOP_LOSS      = 3.0
MIN_CONFIDENCE = 65
LOG_FILE       = "trade_log.csv"


def get_candles(symbol, days=7):
    coin_id = COINGECKO_IDS[symbol]
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": str(days)}
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    response = requests.get(url, params=params, headers=headers, timeout=15)
    response.raise_for_status()
    return [{"time": datetime.fromtimestamp(c[0]/1000, tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M"),
             "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])}
            for c in response.json()]


def calculate_bollinger_bands(closes, period=20, num_std=2.0):
    if len(closes) < period:
        return None, None, None, None, None
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = round(middle + num_std * std, 4)
    middle = round(middle, 4)
    lower = round(middle - num_std * std, 4)
    bandwidth = round((upper - lower) / middle * 100, 4) if middle != 0 else 0
    price = closes[-1]
    percent_b = round((price - lower) / (upper - lower), 4) if (upper - lower) != 0 else 0.5
    return upper, middle, lower, bandwidth, percent_b


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100.0
    if avg_gain == 0: return 1.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def get_indicators(candles):
    closes = [c["close"] for c in candles]
    current_price = closes[-1]
    upper, middle, lower, bandwidth, percent_b = calculate_bollinger_bands(closes, BB_PERIOD, BB_STD)
    rsi = calculate_rsi(closes, RSI_PERIOD)
    squeeze = bandwidth is not None and bandwidth < 3.0
    dist_from_lower = round((current_price - lower) / current_price * 100, 2) if lower else 0
    dist_from_upper = round((upper - current_price) / current_price * 100, 2) if upper else 0
    return {"price": current_price, "upper": upper, "middle": middle, "lower": lower,
            "bandwidth": bandwidth, "percent_b": percent_b, "rsi": rsi,
            "squeeze": squeeze, "dist_lower_pct": dist_from_lower, "dist_upper_pct": dist_from_upper}


def parse_claude_json(raw_text):
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        parts = raw_text.split("```")
        raw_text = parts[1]
        if raw_text.startswith("json"): raw_text = raw_text[4:]
        raw_text = raw_text.strip()
    match = re.search(r'\{[^{}]*\}', raw_text, re.DOTALL)
    if match: return json.loads(match.group())
    return json.loads(raw_text)


SYSTEM_PROMPT = """You are a professional crypto trading signal engine using Bollinger Band Mean Reversion strategy.
Output ONLY a raw JSON object. No text, no markdown, no explanation.

STRATEGY: Bollinger Band Mean Reversion
Best for choppy, ranging markets. Price reverts to mean (middle band) after stretching to extremes.

BUY conditions:
- percent_b <= 0.05 AND RSI < 40: strong BUY
- percent_b <= 0.15 AND RSI < 35: BUY
- RSI < 25: override BUY (extremely oversold)
- squeeze=true + percent_b < 0.2: anticipatory BUY

SELL conditions:
- percent_b >= 0.95 AND RSI > 60: strong SELL
- percent_b >= 0.85 AND RSI > 65: SELL
- RSI > 75: override SELL (extremely overbought)

HOLD: percent_b 0.2-0.8, RSI 35-65 with no band touch

CONFIDENCE (start 50):
+25 band break (<=0.05 or >=0.95), +15 near band, +20 RSI<30 or >70, +10 RSI<40 or >60,
+10 squeeze, min 80 on override (RSI<25 or >75)

take_profit_pct = distance to middle band. Min 0.5, Max 5.0.

Output: {"signal":"BUY","confidence":78,"take_profit_pct":1.2,"reasoning":"Price at lower band with RSI oversold"}"""


def ask_claude(symbol, indicators):
    msg = (f"Symbol: {symbol}\nCurrent price: {indicators['price']}\n"
           f"Upper Band: {indicators['upper']}\nMiddle Band (SMA-20): {indicators['middle']}\n"
           f"Lower Band: {indicators['lower']}\nBandwidth: {indicators['bandwidth']}%\n"
           f"Percent-B: {indicators['percent_b']} (0=lower, 0.5=middle, 1=upper)\n"
           f"RSI-14: {indicators['rsi']}\nSqueeze: {indicators['squeeze']}\n"
           f"Distance from lower: {indicators['dist_lower_pct']}%\n"
           f"Distance from upper: {indicators['dist_upper_pct']}%\nReturn signal as JSON.")
    response = requests.post("https://api.anthropic.com/v1/messages",
                             headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                                      "anthropic-version": "2023-06-01"},
                             json={"model": "claude-sonnet-4-6", "max_tokens": 200, "system": SYSTEM_PROMPT,
                                   "messages": [{"role": "user", "content": msg}]}, timeout=30)
    response.raise_for_status()
    return parse_claude_json(response.json()["content"][0]["text"])


def fire_webhook(signal_str, current_price, symbol, take_profit_pct):
    action = "enter_long" if signal_str == "BUY" else "enter_short"
    tp_pct = take_profit_pct if signal_str == "BUY" else -take_profit_pct
    payload = {"secret": WEBHOOK_SECRET, "max_lag": "300",
               "timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
               "trigger_price": str(current_price), "tv_exchange": "BINANCE",
               "tv_instrument": TICKER_MAP.get(symbol, symbol), "action": action,
               "bot_uuid": BOT_UUIDS[symbol],
               "take_profit": {"enabled": True, "steps": [{"order_type": "market",
                               "price_percent": tp_pct, "volume_percent": 100}]},
               "stop_loss": {"enabled": True, "order_type": "market", "trigger_price_percent": STOP_LOSS}}
    r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    if r.status_code == 200:
        print(f"  Webhook {action}: SUCCESS (TP: {tp_pct}%, SL: -{STOP_LOSS}%)")
    elif r.status_code == 429:
        print(f"  Webhook: RATE LIMITED (429)")
    elif r.status_code == 418:
        print(f"  Webhook: BLOCKED (418)")
    else:
        print(f"  Webhook: FAILED [{r.status_code}] {r.text}")
    return r.status_code == 200


def log_result(symbol, signal, indicators, fired):
    timestamp = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    write_header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        if write_header:
            writer.writerow(["timestamp_dubai", "symbol", "price", "signal", "confidence",
                             "upper_band", "middle_band", "lower_band", "percent_b",
                             "bandwidth", "squeeze", "rsi14", "take_profit_pct",
                             "webhook_fired", "reasoning"])
        writer.writerow([timestamp, symbol, indicators["price"], signal["signal"],
                         signal["confidence"], indicators["upper"], indicators["middle"],
                         indicators["lower"], indicators["percent_b"], indicators["bandwidth"],
                         indicators["squeeze"], indicators["rsi"],
                         signal.get("take_profit_pct", ""), fired,
                         signal.get("reasoning", "")])
    print(f"  [{timestamp} Dubai] {symbol} | {signal['signal']} | "
          f"Confidence: {signal['confidence']}% | %B: {indicators['percent_b']} | "
          f"RSI: {indicators['rsi']} | TP: {signal.get('take_profit_pct','')}% | Fired: {fired}")


def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*58}\nBollinger Band Mean Reversion — {now} Dubai time\n{'='*58}")
    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            candles = get_candles(symbol, days=7)
            time.sleep(2)
            indicators = get_indicators(candles)
            print(f"  Price: ${indicators['price']:,.4f}")
            print(f"  Bands: Upper={indicators['upper']} | Middle={indicators['middle']} | Lower={indicators['lower']}")
            print(f"  %B: {indicators['percent_b']} | RSI-14: {indicators['rsi']} | BW: {indicators['bandwidth']}% | Squeeze: {indicators['squeeze']}")
            signal = ask_claude(symbol, indicators)
            tp_pct = signal.get("take_profit_pct", 1.5)
            print(f"  Signal: {signal['signal']} | Confidence: {signal['confidence']}% | TP: {tp_pct}% | {signal.get('reasoning','')}")
            webhook_fired = False
            if signal["signal"] in ("BUY", "SELL") and signal["confidence"] >= MIN_CONFIDENCE:
                webhook_fired = fire_webhook(signal["signal"], indicators["price"], symbol, tp_pct)
            else:
                print(f"  HOLD — no webhook fired.")
            log_result(symbol, signal, indicators, webhook_fired)
            time.sleep(3)
        except Exception as e:
            print(f"  ERROR on {symbol}: {e}")
    print(f"\n{'='*58}\nRun complete.\n{'='*58}\n")


if __name__ == "__main__":
    run()
