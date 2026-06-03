"""
Strategy: RSI Divergence
Best for: Catching reversals at market turning points

Logic:
- Bullish divergence: price lower low, RSI higher low → BUY
- Bearish divergence: price higher high, RSI lower high → SELL

Valid CoinGecko days: 1, 7, 14, 30, 90, 180, 365
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
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
TAKE_PROFIT = 2.5
STOP_LOSS = 3.0
MIN_CONFIDENCE = 68
LOG_FILE = "trade_log.csv"


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


def calculate_rsi_series(closes, period=14):
    rsi_values = []
    for i in range(period + 1, len(closes) + 1):
        window = closes[:i]
        gains  = [max(window[j]-window[j-1], 0) for j in range(1, len(window))]
        losses = [max(window[j-1]-window[j], 0) for j in range(1, len(window))]
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0: rsi_values.append(100.0)
        elif avg_gain == 0: rsi_values.append(1.0)
        else: rsi_values.append(round(100 - (100 / (1 + avg_gain/avg_loss)), 2))
    return rsi_values


def find_divergence(closes, rsi_series, lookback=10):
    """
    Detect bullish or bearish RSI divergence.

    Compares current candle (pw[-1]) against ALL prior candles in the lookback
    window (pw[:-1]). Previously used pw[:-3] which excluded the 3 most recent
    prior candles, causing short-duration divergences to be missed.
    """
    if len(closes) < lookback or len(rsi_series) < lookback:
        return {"type": "none", "strength": 0, "details": "insufficient data"}
    pw = closes[-lookback:]
    rw = rsi_series[-lookback:]
    cur_p, cur_r = pw[-1], rw[-1]

    # Compare current vs all prior candles in window (excluding only current)
    prev_low_p  = min(pw[:-1])
    prev_low_r  = min(rw[:-1])
    prev_high_p = max(pw[:-1])
    prev_high_r = max(rw[:-1])

    # Bullish: price makes lower low, RSI makes higher low
    if cur_p < prev_low_p and cur_r > prev_low_r:
        pd = round((prev_low_p - cur_p) / prev_low_p * 100, 2)
        rd = round(cur_r - prev_low_r, 2)
        return {"type": "bullish", "strength": min(100, int(pd*10 + rd*2)),
                "details": f"price -{pd}% but RSI +{rd}pts"}

    # Bearish: price makes higher high, RSI makes lower high
    if cur_p > prev_high_p and cur_r < prev_high_r:
        pd = round((cur_p - prev_high_p) / prev_high_p * 100, 2)
        rd = round(prev_high_r - cur_r, 2)
        return {"type": "bearish", "strength": min(100, int(pd*10 + rd*2)),
                "details": f"price +{pd}% but RSI -{rd}pts"}

    return {"type": "none", "strength": 0, "details": "no divergence detected"}


def calculate_ema(closes, period):
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]: ema = p * k + ema * (1 - k)
    return round(ema, 4)


def get_indicators(candles):
    closes = [c["close"] for c in candles]
    rsi_series = calculate_rsi_series(closes, 14)
    div = find_divergence(closes, rsi_series, 10)
    ema9, ema21 = calculate_ema(closes, 9), calculate_ema(closes, 21)
    return {"price": closes[-1], "rsi": rsi_series[-1] if rsi_series else 50.0,
            "divergence": div, "ema9": ema9, "ema21": ema21,
            "trend": "bullish" if ema9 > ema21 else "bearish"}


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


SYSTEM_PROMPT = """You are a trading signal engine using RSI Divergence strategy.
Output ONLY a raw JSON object. No text, no markdown, no explanation.

SIGNAL RULES:
BUY:  divergence.type = "bullish" AND strength >= 20
SELL: divergence.type = "bearish" AND strength >= 20
HOLD: divergence.type = "none" OR strength < 20

CONFIDENCE SCORING (start at 50):
+20  divergence detected (type is bullish or bearish)
+10  strength 20-29  (weak divergence)
+20  strength 30-59  (moderate divergence)
+25  strength 60-100 (strong divergence)
+15  EMA trend confirms reversal direction:
       bullish divergence + bearish EMA trend (oversold in downtrend)
       bearish divergence + bullish EMA trend (overbought in uptrend)
+10  RSI extreme confirms:
       bullish divergence + RSI < 35 (deeply oversold)
       bearish divergence + RSI > 65 (deeply overbought)

Cap confidence at 100. If no divergence or strength < 20, output HOLD at confidence 50.

Output: {"signal":"BUY","confidence":85,"reasoning":"Bullish RSI divergence strength=45 — price lower low while RSI recovering from oversold, bearish EMA trend confirms reversal setup"}"""


def ask_claude(symbol, ind):
    div = ind["divergence"]
    msg = (f"Symbol: {symbol}\nPrice: {ind['price']}\nRSI-14: {ind['rsi']}\n"
           f"Divergence: {div['type']} | Strength: {div['strength']}/100 | {div['details']}\n"
           f"EMA9: {ind['ema9']} | EMA21: {ind['ema21']} | Trend: {ind['trend']}\nReturn signal as JSON.")
    resp = requests.post("https://api.anthropic.com/v1/messages",
                         headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                                  "anthropic-version": "2023-06-01"},
                         json={"model": "claude-sonnet-4-6", "max_tokens": 200, "system": SYSTEM_PROMPT,
                               "messages": [{"role": "user", "content": msg}]}, timeout=30)
    resp.raise_for_status()
    return parse_claude_json(resp.json()["content"][0]["text"])


def fire_webhook(signal_str, price, symbol):
    action = "enter_long" if signal_str == "BUY" else "enter_short"
    tp_pct = TAKE_PROFIT if signal_str == "BUY" else -TAKE_PROFIT
    payload = {"secret": WEBHOOK_SECRET, "max_lag": "300",
               "timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
               "trigger_price": str(price), "tv_exchange": "BINANCE",
               "tv_instrument": TICKER_MAP.get(symbol, symbol), "action": action,
               "bot_uuid": BOT_UUIDS[symbol],
               "take_profit": {"enabled": True, "steps": [{"order_type": "market",
                               "price_percent": tp_pct, "volume_percent": 100}]},
               "stop_loss": {"enabled": True, "order_type": "market", "trigger_price_percent": STOP_LOSS}}
    r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    print(f"  Webhook {action}: {'SUCCESS' if r.status_code==200 else f'FAILED [{r.status_code}]'} (TP:{tp_pct}%)")
    return r.status_code == 200


def log_result(symbol, signal, ind, fired):
    ts = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    div = ind["divergence"]
    write_header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        if write_header:
            writer.writerow(["timestamp_dubai", "symbol", "price", "signal", "confidence",
                             "rsi", "div_type", "div_strength", "ema9", "ema21",
                             "trend", "webhook_fired", "reasoning"])
        writer.writerow([ts, symbol, ind["price"], signal["signal"], signal["confidence"],
                         ind["rsi"], div["type"], div["strength"], ind["ema9"], ind["ema21"],
                         ind["trend"], fired, signal.get("reasoning", "")])
    print(f"  [{ts} Dubai] {symbol} | {signal['signal']} | {signal['confidence']}% | Div:{div['type']}({div['strength']}) | Fired:{fired}")


def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*56}\nRSI Divergence Strategy — {now} Dubai time\n{'='*56}")
    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            candles = get_candles(symbol, days=7)
            time.sleep(2)
            ind = get_indicators(candles)
            div = ind["divergence"]
            print(f"  Price: ${ind['price']:,.4f} | RSI: {ind['rsi']}")
            print(f"  Divergence: {div['type']} | Strength: {div['strength']} | {div['details']}")
            print(f"  Trend: {ind['trend']} (EMA9={ind['ema9']} EMA21={ind['ema21']})")
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
