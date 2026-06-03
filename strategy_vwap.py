"""
Strategy: VWAP + EMA Trend Following
Best for: Trending markets with institutional participation

Logic:
- VWAP acts as dynamic support/resistance
- Price above VWAP + EMA9 > EMA21 → BUY
- Price below VWAP + EMA9 < EMA21 → SELL
- SAR: close opposite position before opening new one
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
TAKE_PROFIT    = 2.0
STOP_LOSS      = 3.0
MIN_CONFIDENCE = 65
LOG_FILE       = "trade_log.csv"

open_positions = {s: None for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]}

# Column index of webhook_fired in the VWAP trade log:
# timestamp,symbol,price,signal,confidence,vwap,price_vs_vwap,ema9,ema21,ema_spread,rsi14,webhook_fired,reasoning
FIRED_COL = 11


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


def get_candles(symbol, days=1):
    coin_id = COINGECKO_IDS[symbol]
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": str(days)}
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    return [{"time": datetime.fromtimestamp(c[0]/1000, tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M"),
             "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])}
            for c in r.json()]


def calculate_ema(closes, period):
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]: ema = price * k + ema * (1 - k)
    return round(ema, 4)


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100.0
    if ag == 0: return 1.0
    return round(100 - (100 / (1 + ag/al)), 2)


def calculate_vwap(candles):
    total_tp_w, total_w = 0, 0
    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        w  = max(c["high"] - c["low"], 0.0001)
        total_tp_w += tp * w
        total_w    += w
    return round(total_tp_w / total_w if total_w > 0 else candles[-1]["close"], 4)


def get_indicators(candles):
    closes = [c["close"] for c in candles]
    price  = closes[-1]
    ema9   = calculate_ema(closes, 9)
    ema21  = calculate_ema(closes, 21)
    rsi14  = calculate_rsi(closes, 14)
    vwap   = calculate_vwap(candles)
    return {"price": price, "vwap": vwap, "ema9": ema9, "ema21": ema21, "rsi14": rsi14,
            "price_vs_vwap": round((price - vwap) / vwap * 100, 4),
            "ema_spread":    round((ema9 - ema21) / ema21 * 100, 4)}


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


SYSTEM_PROMPT = """You are a professional crypto trading signal engine using VWAP + EMA strategy.
Output ONLY a raw JSON object. No text, no markdown, no explanation.

STRATEGY: VWAP + EMA Trend Following

STEP 1 — MANDATORY HOLD CHECK (evaluate before anything else):
If any of these are true → output HOLD immediately, confidence 50. Do not proceed to scoring.
- price_vs_vwap > 0 (above VWAP) but EMA9 < EMA21 (bearish EMA) — signals disagree
- price_vs_vwap < 0 (below VWAP) but EMA9 > EMA21 (bullish EMA) — signals disagree
- |price_vs_vwap| < 0.3% AND |ema_spread| < 0.2% — price too close to VWAP and EMAs too close

STEP 2 — RSI FILTER (apply before scoring):
- BUY signal: RSI >= 65 → HOLD (overbought, don't enter long)
- SELL signal: RSI <= 35 → HOLD (oversold, don't enter short)

STEP 3 — SIGNAL + CONFIDENCE SCORING:
BUY:  price_vs_vwap > 0 AND ema_spread > 0 (both confirm bullish)
SELL: price_vs_vwap < 0 AND ema_spread < 0 (both confirm bearish)

Confidence (start 50):
+20  VWAP condition met (price on correct side)
+15  EMA confirms direction (ema_spread confirms)
+10  RSI in safe zone (RSI < 65 for BUY, RSI > 35 for SELL)
+10  Large deviation: |price_vs_vwap| > 1.0% (strong trend)
+10  RSI extreme: RSI < 30 for BUY (deeply oversold), RSI > 70 for SELL (deeply overbought)

Cap confidence at 100.

Output: {"signal":"BUY","confidence":85,"reasoning":"Price 1.5% above VWAP with EMA-9 > EMA-21 confirming uptrend, RSI not overbought"}"""


def ask_claude(symbol, ind):
    msg = (f"Symbol: {symbol}\nPrice: {ind['price']}\nVWAP: {ind['vwap']}\n"
           f"price_vs_vwap: {ind['price_vs_vwap']}%\n"
           f"EMA9: {ind['ema9']}\nEMA21: {ind['ema21']}\n"
           f"ema_spread: {ind['ema_spread']}%\nRSI-14: {ind['rsi14']}\n"
           f"Return signal as JSON.")
    resp = requests.post("https://api.anthropic.com/v1/messages",
                         headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                                  "anthropic-version": "2023-06-01"},
                         json={"model": "claude-sonnet-4-6", "max_tokens": 200, "system": SYSTEM_PROMPT,
                               "messages": [{"role": "user", "content": msg}]}, timeout=30)
    resp.raise_for_status()
    return parse_claude_json(resp.json()["content"][0]["text"])


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


def fire_webhook(signal_str, price, symbol):
    """
    SAR logic:
    - Same direction already open → skip duplicate entry
    - Opposite direction open → close first, wait 5s, then open new
    - No position open → open directly
    """
    current = open_positions.get(symbol)

    if current == signal_str:
        print(f"  [SAR] Already in {signal_str} for {symbol} — skipping")
        return False

    if current is not None and current != signal_str:
        print(f"  [SAR] Reversing {current} → {signal_str} for {symbol}")
        if send_close_webhook(symbol, price):
            print(f"  [SAR] Waiting 5s before opening {signal_str}...")
            time.sleep(5)
        else:
            print(f"  [SAR] Close failed — aborting reversal for {symbol}")
            return False

    action  = "enter_long" if signal_str == "BUY" else "enter_short"
    tp_pct  = TAKE_PROFIT if signal_str == "BUY" else -TAKE_PROFIT
    payload = {"secret": WEBHOOK_SECRET, "max_lag": "300",
               "timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
               "trigger_price": str(price), "tv_exchange": "BINANCE",
               "tv_instrument": TICKER_MAP.get(symbol, symbol), "action": action,
               "bot_uuid": BOT_UUIDS[symbol],
               "take_profit": {"enabled": True, "steps": [{"order_type": "market",
                               "price_percent": tp_pct, "volume_percent": 100}]},
               "stop_loss": {"enabled": True, "order_type": "market", "trigger_price_percent": STOP_LOSS}}
    r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    if r.status_code == 200:
        print(f"  Webhook {action}: SUCCESS (TP:{tp_pct}%)")
        open_positions[symbol] = signal_str
        return True
    print(f"  Webhook {action}: {'RATE LIMITED' if r.status_code==429 else f'FAILED [{r.status_code}]'} {r.text}")
    return False


def log_result(symbol, signal, ind, fired):
    ts = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        if header:
            writer.writerow(["timestamp_dubai", "symbol", "price", "signal", "confidence",
                             "vwap", "price_vs_vwap", "ema9", "ema21", "ema_spread",
                             "rsi14", "webhook_fired", "reasoning"])
        writer.writerow([ts, symbol, ind["price"], signal["signal"], signal["confidence"],
                         ind["vwap"], ind["price_vs_vwap"], ind["ema9"], ind["ema21"],
                         ind["ema_spread"], ind["rsi14"], fired,
                         signal.get("reasoning", "")])
    print(f"  [{ts} Dubai] {symbol} | {signal['signal']} | {signal['confidence']}% | vsVWAP:{ind['price_vs_vwap']}% | Fired:{fired}")


def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*56}\nVWAP + EMA Strategy — {now} Dubai time\n{'='*56}")
    load_positions_from_log()
    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            candles = get_candles(symbol, days=1)
            time.sleep(2)
            ind = get_indicators(candles)
            print(f"  Price: ${ind['price']:,.4f} | VWAP: {ind['vwap']} | vsVWAP: {ind['price_vs_vwap']}% | EMA spread: {ind['ema_spread']}% | RSI: {ind['rsi14']}")
            print(f"  [SAR] Current tracked position: {open_positions.get(symbol, 'None')}")
            signal = ask_claude(symbol, ind)
            print(f"  Signal: {signal['signal']} | {signal['confidence']}% | {signal.get('reasoning','')}")
            fired = False
            if signal["signal"] in ("BUY", "SELL") and signal["confidence"] >= MIN_CONFIDENCE:
                fired = fire_webhook(signal["signal"], ind["price"], symbol)
            else:
                print("  HOLD — no webhook fired.")
            log_result(symbol, signal, ind, fired)
            time.sleep(3)
        except Exception as e:
            print(f"  ERROR: {e}")
    print(f"\n{'='*56}\nRun complete.\n{'='*56}\n")


if __name__ == "__main__":
    run()
