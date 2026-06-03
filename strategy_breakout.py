"""
Strategy: Breakout Momentum
Best for: Markets in tight consolidation about to make a big move

Logic:
- Detect consolidation (tight Bollinger Bands = squeeze)
- When price breaks out of range with momentum → BUY or SELL
- ATR confirms volatility expansion

Valid CoinGecko days: 1, 7, 14, 30, 90, 180, 365
"""

import requests
import json
import re
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
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
TAKE_PROFIT = 3.0
STOP_LOSS = 2.5
MIN_CONFIDENCE = 65
LOG_FILE = "trade_log.csv"


def get_candles(symbol, days=7):
    coin_id = COINGECKO_IDS[symbol]
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": str(days)}
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    return [{"time": datetime.fromtimestamp(c[0]/1000, tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M"),
             "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])}
            for c in r.json()]


def calc_atr(candles, period=14):
    trs = [max(candles[i]["high"]-candles[i]["low"],
               abs(candles[i]["high"]-candles[i-1]["close"]),
               abs(candles[i]["low"]-candles[i-1]["close"]))
           for i in range(1, len(candles))]
    return sum(trs[-period:]) / min(len(trs), period) if trs else 0


def get_indicators(candles):
    closes = [c["close"] for c in candles]
    highs  = [c["high"] for c in candles]
    lows   = [c["low"] for c in candles]
    price  = closes[-1]
    lookback = min(20, len(candles))
    range_high = max(highs[-lookback:])
    range_low  = min(lows[-lookback:])
    range_pct  = round((range_high - range_low) / range_low * 100, 4) if range_low else 0
    atr = calc_atr(candles)
    atr_pct = round(atr / price * 100, 4) if price else 0
    range_vs_atr = round(range_pct / atr_pct, 4) if atr_pct else 0
    # BB width
    period = 20
    window = closes[-period:] if len(closes) >= period else closes
    m = sum(window) / len(window)
    std = math.sqrt(sum((x-m)**2 for x in window) / len(window))
    bb_width = round((m+2*std-(m-2*std))/m*100, 4) if m else 5.0
    breakout_up   = price > range_high * 1.003
    breakout_down = price < range_low  * 0.997
    bo_up_pct     = round((price - range_high) / range_high * 100, 4) if breakout_up else 0
    bo_down_pct   = round((range_low - price)  / range_low  * 100, 4) if breakout_down else 0
    # Momentum: last 3 candles
    momentum = "up" if closes[-1] > closes[-3] else "down" if closes[-1] < closes[-3] else "flat"
    return {"price": price, "range_high": range_high, "range_low": range_low,
            "range_pct": range_pct, "atr_pct": atr_pct, "range_vs_atr": range_vs_atr,
            "bb_width": bb_width, "breakout_up": breakout_up, "breakout_down": breakout_down,
            "bo_up_pct": bo_up_pct, "bo_down_pct": bo_down_pct, "momentum": momentum}


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


SYSTEM_PROMPT = """You are a trading signal engine using Breakout Momentum strategy.
Output ONLY a raw JSON object. No text, no markdown, no explanation.

BUY: breakout_up=True AND momentum=up AND range_vs_atr >= 0.8
SELL: breakout_down=True AND momentum=down AND range_vs_atr >= 0.8
HOLD: no confirmed breakout OR range_vs_atr < 0.8 (not enough consolidation)

CONFIDENCE (start 50): +20 confirmed breakout, +15 momentum confirms,
+10 range_vs_atr >= 1.2, +10 bb_width < 4% (tight squeeze), -10 range_vs_atr < 0.8.

Output: {"signal":"BUY","confidence":72,"reasoning":"Price broke above consolidation high with upward momentum"}"""


def ask_claude(symbol, ind):
    msg = (f"Symbol: {symbol}\nPrice: {ind['price']}\n"
           f"Range: {ind['range_low']}-{ind['range_high']} ({ind['range_pct']}%)\n"
           f"ATR%: {ind['atr_pct']}% | RangeVsATR: {ind['range_vs_atr']}x | Momentum: {ind['momentum']}\n"
           f"Breakout up: {ind['breakout_up']}(+{ind['bo_up_pct']}%) | down: {ind['breakout_down']}(+{ind['bo_down_pct']}%)\n"
           f"BB width: {ind['bb_width']}%\nReturn signal as JSON.")
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
    header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a") as f:
        if header: f.write("timestamp_dubai,symbol,price,signal,confidence,range_pct,atr_pct,range_vs_atr,bb_width,bo_up,bo_down,momentum,webhook_fired,reasoning\n")
        r = signal.get("reasoning","").replace('"',"'")
        f.write(f'{ts},{symbol},{ind["price"]},{signal["signal"]},{signal["confidence"]},'
                f'{ind["range_pct"]},{ind["atr_pct"]},{ind["range_vs_atr"]},{ind["bb_width"]},'
                f'{ind["bo_up_pct"]},{ind["bo_down_pct"]},{ind["momentum"]},{fired},"{r}"\n')
    print(f"  [{ts} Dubai] {symbol} | {signal['signal']} | {signal['confidence']}% | bo_up:{ind['breakout_up']}({ind['bo_up_pct']}%) | Fired:{fired}")


def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*56}\nBreakout Momentum Strategy — {now} Dubai time\n{'='*56}")
    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            candles = get_candles(symbol, days=7)
            time.sleep(2)
            ind = get_indicators(candles)
            print(f"  Price: ${ind['price']:,.4f} | Range: {ind['range_low']}-{ind['range_high']} ({ind['range_pct']}%)")
            print(f"  ATR%: {ind['atr_pct']}% | RangeVsATR: {ind['range_vs_atr']}x | Momentum: {ind['momentum']}")
            print(f"  Breakout up: {ind['breakout_up']}(+{ind['bo_up_pct']}%) | down: {ind['breakout_down']}(+{ind['bo_down_pct']}%)")
            signal = ask_claude(symbol, ind)
            print(f"  Signal: {signal['signal']} | {signal['confidence']}% | {signal.get('reasoning','')}")
            fired = False
            if signal["signal"] in ("BUY","SELL") and signal["confidence"] >= MIN_CONFIDENCE:
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
