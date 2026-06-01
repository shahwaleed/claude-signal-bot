"""
Strategy: Bollinger Band Mean Reversion
Best for: Choppy, sideways, ranging markets (current June 2026 conditions)

Logic:
- Price touches/breaks lower Bollinger Band + RSI oversold → BUY (expect bounce to middle band)
- Price touches/breaks upper Bollinger Band + RSI overbought → SELL (expect drop to middle band)
- Take profit target = middle band (20-period SMA) — dynamic, not fixed %
- Stop loss = 3% fixed

Indicators:
- Bollinger Bands (20-period SMA ± 2 standard deviations)
- RSI-14 (standard for mean reversion, less noisy than RSI-7)
- Band width (squeeze detection — tight bands = explosive move coming)
- %B (where price sits within the bands — 0=lower, 0.5=middle, 1=upper)

CoinGecko OHLC valid days values: 1, 7, 14, 30, 90, 180, 365
- days=1  → 30-min candles (~48 candles)
- days=7  → 4-hour candles (~42 candles) ← used here for BB-20 + RSI-14
"""

import requests
import json
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

SYMBOLS        = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
BB_PERIOD      = 20
BB_STD         = 2.0
RSI_PERIOD     = 14
STOP_LOSS      = 3.0
MIN_CONFIDENCE = 65
LOG_FILE       = "trade_log.csv"


def get_candles(symbol, days=7):
    """
    Fetch OHLC from CoinGecko.
    Valid days values: 1, 7, 14, 30, 90, 180, 365
    days=7 returns 4-hour candles (~42 candles) — enough for BB-20 + RSI-14
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
        candles.append({"time": ts.strftime("%Y-%m-%d %H:%M"), "open": float(c[1]),
                        "high": float(c[2]), "low": float(c[3]), "close": float(c[4])})
    return candles


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
        return 1.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


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


SYSTEM_PROMPT = """You are a professional crypto trading signal engine using Bollinger Band Mean Reversion strategy.
Output ONLY a raw JSON object. No text, no markdown, no explanation.

STRATEGY: Bollinger Band Mean Reversion
Best for choppy, ranging markets. Price reverts to the mean (middle band) after stretching to extremes.

SIGNAL RULES:

BUY conditions (expect bounce up to middle band):
- percent_b <= 0.05 (price at or below lower band) AND RSI < 40: strong BUY
- percent_b <= 0.15 (price near lower band) AND RSI < 35: BUY
- RSI < 25: override BUY (extremely oversold regardless of band position)
- squeeze = true + percent_b < 0.2: anticipatory BUY (breakout likely upward if already near lower)

SELL conditions (expect drop down to middle band):
- percent_b >= 0.95 (price at or above upper band) AND RSI > 60: strong SELL
- percent_b >= 0.85 (price near upper band) AND RSI > 65: SELL
- RSI > 75: override SELL (extremely overbought regardless of band position)

HOLD conditions:
- percent_b between 0.2 and 0.8 (price in middle of bands) — no edge
- RSI between 35 and 65 with no band touch — neutral
- squeeze = true with price in middle — wait for breakout direction

CONFIDENCE SCORING (start at 50):
- Price touching/breaking lower band (percent_b <= 0.05): +25
- Price near lower band (percent_b <= 0.15): +15
- Price touching/breaking upper band (percent_b >= 0.95): +25
- Price near upper band (percent_b >= 0.85): +15
- RSI below 30: +20 for BUY signal
- RSI below 40: +10 for BUY signal
- RSI above 70: +20 for SELL signal
- RSI above 60: +10 for SELL signal
- Squeeze detected: +10 (high volatility incoming)
- Override condition (RSI < 25 or RSI > 75): set confidence to minimum 80

take_profit_pct should be the distance from current price to the middle band as a percentage.
Minimum take_profit_pct: 0.5. Maximum: 5.0.

Required output format:
{"signal":"BUY","confidence":78,"take_profit_pct":1.2,"reasoning":"Price at lower band with RSI oversold, expecting mean reversion to middle band"}"""


def ask_claude(symbol, indicators):
    msg = (f"Symbol: {symbol}\nCurrent price: {indicators['price']}\n"
           f"Upper Band: {indicators['upper']}\nMiddle Band (SMA-20): {indicators['middle']}\n"
           f"Lower Band: {indicators['lower']}\nBandwidth: {indicators['bandwidth']}%\n"
           f"Percent-B: {indicators['percent_b']} (0=lower band, 0.5=middle, 1=upper band)\n"
           f"RSI-14: {indicators['rsi']}\nSqueeze detected: {indicators['squeeze']}\n"
           f"Distance from lower band: {indicators['dist_lower_pct']}%\n"
           f"Distance from upper band: {indicators['dist_upper_pct']}%\n"
           f"Apply Bollinger Band Mean Reversion rules and return your signal as JSON.")
    payload = {"model": "claude-sonnet-4-6", "max_tokens": 200, "system": SYSTEM_PROMPT,
               "messages": [{"role": "user", "content": msg}]}
    headers = {"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
               "anthropic-version": "2023-06-01"}
    response = requests.post("https://api.anthropic.com/v1/messages",
                             headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    raw_text = response.json()["content"][0]["text"].strip()
    if raw_text.startswith("```"):
        parts = raw_text.split("```")
        raw_text = parts[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
    return json.loads(raw_text.strip())


def fire_webhook(signal_str, current_price, symbol, take_profit_pct):
    action = "enter_long" if signal_str == "BUY" else "enter_short"
    ticker = TICKER_MAP.get(symbol, symbol)
    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    tp_pct = take_profit_pct if signal_str == "BUY" else -take_profit_pct
    payload = {
        "secret": WEBHOOK_SECRET, "max_lag": "300", "timestamp": now_iso,
        "trigger_price": str(current_price), "tv_exchange": "BINANCE",
        "tv_instrument": ticker, "action": action, "bot_uuid": BOT_UUIDS[symbol],
        "take_profit": {"enabled": True, "steps": [{"order_type": "market",
                        "price_percent": tp_pct, "volume_percent": 100}]},
        "stop_loss": {"enabled": True, "order_type": "market",
                      "trigger_price_percent": STOP_LOSS}
    }
    response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    if response.status_code == 200:
        print(f"  Webhook {action}: SUCCESS (TP: {tp_pct}%, SL: -{STOP_LOSS}%)")
    elif response.status_code == 429:
        print(f"  Webhook: RATE LIMITED (429)")
    elif response.status_code == 418:
        print(f"  Webhook: BLOCKED (418)")
    else:
        print(f"  Webhook: FAILED [{response.status_code}] {response.text}")
    return response.status_code == 200


def log_result(symbol, signal, indicators, fired):
    timestamp = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    write_header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a") as f:
        if write_header:
            f.write("timestamp_dubai,symbol,price,signal,confidence,"
                    "upper_band,middle_band,lower_band,percent_b,"
                    "bandwidth,squeeze,rsi14,take_profit_pct,webhook_fired,reasoning\n")
        reasoning = signal.get("reasoning", "").replace('"', "'")
        tp = signal.get("take_profit_pct", "")
        f.write(f'{timestamp},{symbol},{indicators["price"]},{signal["signal"]},'
                f'{signal["confidence"]},{indicators["upper"]},{indicators["middle"]},'
                f'{indicators["lower"]},{indicators["percent_b"]},{indicators["bandwidth"]},'
                f'{indicators["squeeze"]},{indicators["rsi"]},{tp},{fired},"{reasoning}"\n')
    print(f"  [{timestamp} Dubai] {symbol} | {signal['signal']} | "
          f"Confidence: {signal['confidence']}% | %B: {indicators['percent_b']} | "
          f"RSI: {indicators['rsi']} | TP: {tp}% | Fired: {fired}")


def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*58}")
    print(f"Bollinger Band Mean Reversion — {now} Dubai time")
    print(f"{'='*58}")
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
            continue
    print(f"\n{'='*58}")
    print("Run complete.")
    print(f"{'='*58}\n")


if __name__ == "__main__":
    run()
