"""
Strategy: Advanced EMA Crossover
- Multi-timeframe confirmation (30min + 4hour must agree)
- RSI divergence detection (price vs RSI direction)
- EMA 9/21 crossover
- RSI-7 filter
- SAR (Stop-and-Reverse): if opposite position is open, close it first then open new direction
"""

import requests
import json
import re
import time
import os
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
CANDLES_30M    = 30
CANDLES_4H     = 30
TAKE_PROFIT    = 1.5
STOP_LOSS      = 3.0
MIN_CONFIDENCE = 65
LOG_FILE       = "trade_log.csv"

open_positions = {s: None for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]}


def load_positions_from_log():
    if not os.path.exists(LOG_FILE):
        return
    last = {}
    try:
        with open(LOG_FILE, "r") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 5:
                    continue
                symbol  = parts[1]
                signal  = parts[3]
                fired   = parts[13] if len(parts) > 13 else "False"
                if symbol in open_positions and signal in ("BUY", "SELL") and fired == "True":
                    last[symbol] = signal
        for sym, sig in last.items():
            open_positions[sym] = sig
        print("  [SAR] Loaded positions from log:", open_positions)
    except Exception as e:
        print(f"  [SAR] Could not load positions from log: {e}")


def get_candles(symbol, days):
    coin_id = COINGECKO_IDS[symbol]
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": str(days)}
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    response = requests.get(url, params=params, headers=headers, timeout=15)
    response.raise_for_status()
    return [{"time": datetime.fromtimestamp(c[0]/1000, tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M"),
             "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])}
            for c in response.json()]


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
    if avg_loss == 0: return 100.0
    if avg_gain == 0: return 1.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)

def calculate_rsi_series(closes, period=7):
    return [calculate_rsi(closes[:i], period) for i in range(period + 1, len(closes) + 1)]

def detect_divergence(closes, rsi_series, lookback=5):
    if len(closes) < lookback or len(rsi_series) < lookback:
        return None
    rc = closes[-lookback:]
    rr = rsi_series[-lookback:]
    if rc[-1] < min(rc[:-1]) and not rr[-1] < min(rr[:-1]): return "bullish"
    if rc[-1] > max(rc[:-1]) and not rr[-1] > max(rr[:-1]): return "bearish"
    return None

def get_indicators(candles_30m, candles_4h):
    closes_30m = [c["close"] for c in candles_30m[-30:]]
    closes_4h  = [c["close"] for c in candles_4h[-30:]]
    ema9_30m  = calculate_ema(closes_30m, 9)
    ema21_30m = calculate_ema(closes_30m, 21)
    rsi7_30m  = calculate_rsi(closes_30m, 7)
    ema9_4h   = calculate_ema(closes_4h, 9)
    ema21_4h  = calculate_ema(closes_4h, 21)
    rsi7_4h   = calculate_rsi(closes_4h, 7)
    divergence = detect_divergence(closes_30m, calculate_rsi_series(closes_30m, 7))
    return {"ema9_30m": ema9_30m, "ema21_30m": ema21_30m, "rsi7_30m": rsi7_30m,
            "ema9_4h": ema9_4h, "ema21_4h": ema21_4h, "rsi7_4h": rsi7_4h,
            "trend_4h": "bullish" if ema9_4h > ema21_4h else "bearish",
            "divergence": divergence}


SYSTEM_PROMPT = """You are a professional crypto trading signal engine. Output ONLY a raw JSON object.

STRICT RULES:
- Your ENTIRE response must be one JSON object, nothing else
- No text before or after the JSON
- No markdown, no backticks, no explanation

STRATEGY: Advanced EMA + Multi-Timeframe + RSI Divergence

SIGNAL RULES:
1. PRIMARY (30-min EMA crossover): EMA9 > EMA21 = bullish, EMA9 < EMA21 = bearish
2. CONFIRMATION (4h trend must agree): if 30m and 4h disagree → HOLD
3. RSI FILTER: RSI7 < 65 for BUY, RSI7 > 35 for SELL
4. RSI DIVERGENCE: bullish/bearish divergence → +15 confidence, opposing → -10
5. OVERRIDE: RSI7_30m > 75 → SELL, RSI7_30m < 25 → BUY

CONFIDENCE: start 50, +15 30m EMA, +20 4h trend, +10 RSI zone, +15 divergence, -10/-20 disagreement

Output: {"signal":"BUY","confidence":75,"reasoning":"30m bullish EMA crossover confirmed by 4h uptrend"}"""


def parse_claude_json(raw_text):
    """Robustly extract first valid JSON object from Claude's response."""
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        parts = raw_text.split("```")
        raw_text = parts[1]
        if raw_text.startswith("json"): raw_text = raw_text[4:]
        raw_text = raw_text.strip()
    match = re.search(r'\{[^{}]*\}', raw_text, re.DOTALL)
    if match: return json.loads(match.group())
    return json.loads(raw_text)


def ask_claude(symbol, indicators):
    msg = (f"Symbol: {symbol}\n--- 30-minute timeframe ---\n"
           f"EMA-9: {indicators['ema9_30m']}\nEMA-21: {indicators['ema21_30m']}\n"
           f"RSI-7: {indicators['rsi7_30m']}\nRSI Divergence: {indicators['divergence'] or 'none'}\n"
           f"--- 4-hour timeframe ---\nEMA-9: {indicators['ema9_4h']}\nEMA-21: {indicators['ema21_4h']}\n"
           f"RSI-7: {indicators['rsi7_4h']}\n4H Trend: {indicators['trend_4h']}\n---\n"
           f"Apply all strategy rules and return your signal as JSON.")
    response = requests.post("https://api.anthropic.com/v1/messages",
                             headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                                      "anthropic-version": "2023-06-01"},
                             json={"model": "claude-sonnet-4-6", "max_tokens": 250,
                                   "system": SYSTEM_PROMPT,
                                   "messages": [{"role": "user", "content": msg}]}, timeout=30)
    response.raise_for_status()
    return parse_claude_json(response.json()["content"][0]["text"])


def send_close_webhook(symbol, current_price):
    current = open_positions.get(symbol)
    if current is None: return False
    close_action = "exit_long" if current == "BUY" else "exit_short"
    payload = {"secret": WEBHOOK_SECRET, "max_lag": "300",
               "timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
               "trigger_price": str(current_price), "tv_exchange": "BINANCE",
               "tv_instrument": TICKER_MAP.get(symbol, symbol), "action": close_action,
               "bot_uuid": BOT_UUIDS[symbol]}
    r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    if r.status_code == 200:
        print(f"  [SAR] Closed {current} position for {symbol} ({close_action})")
        open_positions[symbol] = None
        return True
    print(f"  [SAR] Close failed [{r.status_code}]: {r.text}")
    return False


def fire_webhook(signal_str, current_price, symbol):
    current = open_positions.get(symbol)
    if current == signal_str:
        print(f"  [SAR] Already in {signal_str} for {symbol} — skipping")
        return False
    if current is not None and current != signal_str:
        print(f"  [SAR] Reversing {current} → {signal_str} for {symbol}")
        if send_close_webhook(symbol, current_price):
            print(f"  [SAR] Waiting 5s before opening {signal_str}...")
            time.sleep(5)
        else:
            print(f"  [SAR] Close failed — aborting reversal for {symbol}")
            return False
    action = "enter_long" if signal_str == "BUY" else "enter_short"
    tp_pct = TAKE_PROFIT if signal_str == "BUY" else -TAKE_PROFIT
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
        print(f"  Webhook {action}: SUCCESS")
        open_positions[symbol] = signal_str
        return True
    print(f"  Webhook: {'RATE LIMITED' if r.status_code==429 else f'FAILED [{r.status_code}]'} {r.text}")
    return False


def log_result(symbol, signal, indicators, price, fired):
    timestamp = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    write_header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a") as f:
        if write_header:
            f.write("timestamp_dubai,symbol,price,signal,confidence,"
                    "ema9_30m,ema21_30m,rsi7_30m,divergence,"
                    "ema9_4h,ema21_4h,rsi7_4h,trend_4h,webhook_fired,reasoning\n")
        reasoning = signal.get("reasoning", "").replace('"', "'")
        f.write(f'{timestamp},{symbol},{price},{signal["signal"]},{signal["confidence"]},'
                f'{indicators["ema9_30m"]},{indicators["ema21_30m"]},{indicators["rsi7_30m"]},'
                f'{indicators["divergence"] or "none"},{indicators["ema9_4h"]},{indicators["ema21_4h"]},'
                f'{indicators["rsi7_4h"]},{indicators["trend_4h"]},{fired},"{reasoning}"\n')
    print(f"  [{timestamp} Dubai] {symbol} | {signal['signal']} | "
          f"Confidence: {signal['confidence']}% | 4H: {indicators['trend_4h']} | "
          f"Divergence: {indicators['divergence'] or 'none'} | Fired: {fired}")


def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*56}\nAdvanced EMA Strategy — {now} Dubai time\n{'='*56}")
    load_positions_from_log()
    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            print(f"  Fetching 30m candles...")
            candles_30m = get_candles(symbol, days=1)
            time.sleep(2)
            print(f"  Fetching 4h candles...")
            candles_4h  = get_candles(symbol, days=14)
            time.sleep(2)
            current_price = candles_30m[-1]["close"]
            print(f"  Latest close: ${current_price:,.4f}")
            indicators = get_indicators(candles_30m, candles_4h)
            print(f"  30m → EMA9: {indicators['ema9_30m']} | EMA21: {indicators['ema21_30m']} | RSI7: {indicators['rsi7_30m']}")
            print(f"  4h  → Trend: {indicators['trend_4h']} | RSI7: {indicators['rsi7_4h']}")
            print(f"  Divergence: {indicators['divergence'] or 'none'}")
            print(f"  [SAR] Current tracked position: {open_positions.get(symbol, 'None')}")
            signal = ask_claude(symbol, indicators)
            print(f"  Signal: {signal['signal']} | Confidence: {signal['confidence']}% | {signal.get('reasoning','')}")
            webhook_fired = False
            if signal["signal"] in ("BUY", "SELL") and signal["confidence"] >= MIN_CONFIDENCE:
                webhook_fired = fire_webhook(signal["signal"], current_price, symbol)
            else:
                print(f"  HOLD — no webhook fired.")
            log_result(symbol, signal, indicators, current_price, webhook_fired)
            time.sleep(3)
        except Exception as e:
            print(f"  ERROR on {symbol}: {e}")
    print(f"\n{'='*56}\nRun complete.\n{'='*56}\n")


if __name__ == "__main__":
    run()
