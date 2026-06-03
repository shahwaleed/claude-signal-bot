"""
Strategy: Advanced EMA Crossover
- Multi-timeframe confirmation (30min + 4hour must agree)
- RSI divergence detection (price vs RSI direction)
- EMA 9/21 crossover + RSI-7 filter
- SAR (Stop-and-Reverse): close opposite position before opening new one
"""

import requests
import json
import re
import csv
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
TAKE_PROFIT    = 1.5
STOP_LOSS      = 3.0
MIN_CONFIDENCE = 65
LOG_FILE       = "trade_log.csv"

open_positions = {s: None for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]}

# Column index of webhook_fired in the EMA Advanced trade log:
# timestamp,symbol,price,signal,confidence,ema9_30m,ema21_30m,rsi7_30m,divergence,
# ema9_4h,ema21_4h,rsi7_4h,trend_4h,webhook_fired,reasoning
FIRED_COL = 13


def load_positions_from_log():
    """Seed open_positions from last fired signal per symbol."""
    if not os.path.exists(LOG_FILE):
        return
    last = {}
    try:
        with open(LOG_FILE, "r", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < FIRED_COL + 1:
                    continue
                symbol = row[1]
                signal = row[3]
                fired  = row[FIRED_COL]
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
    for price in closes[period:]: ema = price * k + ema * (1 - k)
    return round(ema, 4)


def calculate_rsi(closes, period=7):
    """RSI-7. Returns 50.0 if insufficient data (guard against short lists)."""
    if len(closes) < period + 1:
        return 50.0
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100.0
    if ag == 0: return 1.0
    return round(100 - (100 / (1 + ag/al)), 2)


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
    ema9_30m   = calculate_ema(closes_30m, 9)
    ema21_30m  = calculate_ema(closes_30m, 21)
    rsi7_30m   = calculate_rsi(closes_30m, 7)
    ema9_4h    = calculate_ema(closes_4h, 9)
    ema21_4h   = calculate_ema(closes_4h, 21)
    rsi7_4h    = calculate_rsi(closes_4h, 7)
    divergence = detect_divergence(closes_30m, calculate_rsi_series(closes_30m, 7))
    return {"ema9_30m": ema9_30m, "ema21_30m": ema21_30m, "rsi7_30m": rsi7_30m,
            "ema9_4h": ema9_4h, "ema21_4h": ema21_4h, "rsi7_4h": rsi7_4h,
            "trend_4h": "bullish" if ema9_4h > ema21_4h else "bearish",
            "divergence": divergence}


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


SYSTEM_PROMPT = """You are a professional crypto trading signal engine. Output ONLY a raw JSON object.
No text, no markdown, no backticks. One JSON object only.

STRATEGY: Advanced EMA + Multi-Timeframe + RSI Divergence

STEP 1 — MANDATORY HOLD CHECK (evaluate before anything else):
If 30m EMA direction and 4h trend DISAGREE → output HOLD immediately, confidence 50.
Do not proceed to scoring. This is the primary false-signal filter.

STEP 2 — OVERRIDE (only if Step 1 passes):
- RSI7_30m < 25 → BUY, confidence 95 (extremely oversold)
- RSI7_30m > 75 → SELL, confidence 95 (extremely overbought)

STEP 3 — NORMAL SIGNAL + CONFIDENCE SCORING (only if Steps 1-2 pass):
Signal direction: EMA9_30m > EMA21_30m AND 4h bullish → BUY
                  EMA9_30m < EMA21_30m AND 4h bearish → SELL

Confidence (start 50):
+15  30m EMA confirms signal direction
+20  4h trend confirms signal direction
+10  RSI in safe zone (RSI < 65 for BUY, RSI > 35 for SELL)
+15  divergence confirms signal direction (bullish div → BUY, bearish div → SELL)
-10  divergence opposes signal (minor conflict)
-20  RSI opposes signal strongly (RSI > 60 for BUY, RSI < 40 for SELL)

Cap confidence at 100. MIN_CONFIDENCE to fire = 65.

Output: {"signal":"BUY","confidence":85,"reasoning":"30m bullish EMA crossover confirmed by 4h uptrend, RSI not overbought"}"""


def ask_claude(symbol, indicators):
    msg = (f"Symbol: {symbol}\n--- 30-minute timeframe ---\n"
           f"EMA-9: {indicators['ema9_30m']}\nEMA-21: {indicators['ema21_30m']}\n"
           f"RSI-7: {indicators['rsi7_30m']}\nRSI Divergence: {indicators['divergence'] or 'none'}\n"
           f"--- 4-hour timeframe ---\nEMA-9: {indicators['ema9_4h']}\nEMA-21: {indicators['ema21_4h']}\n"
           f"RSI-7: {indicators['rsi7_4h']}\n4H Trend: {indicators['trend_4h']}\n---\n"
           f"Return signal as JSON.")
    response = requests.post("https://api.anthropic.com/v1/messages",
                             headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                                      "anthropic-version": "2023-06-01"},
                             json={"model": "claude-sonnet-4-6", "max_tokens": 250, "system": SYSTEM_PROMPT,
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
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        if write_header:
            writer.writerow(["timestamp_dubai", "symbol", "price", "signal", "confidence",
                             "ema9_30m", "ema21_30m", "rsi7_30m", "divergence",
                             "ema9_4h", "ema21_4h", "rsi7_4h", "trend_4h",
                             "webhook_fired", "reasoning"])
        writer.writerow([timestamp, symbol, price, signal["signal"], signal["confidence"],
                         indicators["ema9_30m"], indicators["ema21_30m"], indicators["rsi7_30m"],
                         indicators["divergence"] or "none",
                         indicators["ema9_4h"], indicators["ema21_4h"], indicators["rsi7_4h"],
                         indicators["trend_4h"], fired, signal.get("reasoning", "")])
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
