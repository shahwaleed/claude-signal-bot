"""
Strategy: Breakout Momentum
Best for: Strong trending markets after consolidation periods

Logic:
- Detect when price breaks out of a consolidation range with momentum
- Use ATR to measure volatility and confirm breakout strength
- Enter in direction of breakout, ride the momentum

Valid CoinGecko days: 1, 7, 14, 30, 90, 180, 365
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
TICKER_MAP = {"BTCUSDT": "BTCUSDT", "ETHUSDT": "ETHUSDT", "SOLUSDT": "SOLUSDT", "XRPUSDT": "XRPUSDT"}
COINGECKO_IDS = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "SOLUSDT": "solana", "XRPUSDT": "ripple"}
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
TAKE_PROFIT = 3.0
STOP_LOSS = 2.0
MIN_CONFIDENCE = 68
LOG_FILE = "trade_log.csv"
CONSOL_PERIOD = 14
ATR_PERIOD = 14


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


def calculate_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return round(sum(trs[-period:]) / min(len(trs), period), 4) if trs else 0


def get_indicators(candles):
    closes = [c["close"] for c in candles]
    cur = closes[-1]
    ch = candles[-1]["high"]
    cl = candles[-1]["low"]
    prev = candles[:-1]
    c_high = max(c["high"] for c in prev[-CONSOL_PERIOD:])
    c_low  = min(c["low"]  for c in prev[-CONSOL_PERIOD:])
    mid = (c_high + c_low) / 2
    range_pct = round((c_high - c_low) / mid * 100, 4) if mid > 0 else 0
    atr = calculate_atr(candles, ATR_PERIOD)
    atr_pct = round(atr / cur * 100, 4) if cur > 0 else 0
    cur_range = ch - cl
    range_vs_atr = round(cur_range / atr, 4) if atr > 0 else 0
    bo_up = cur > c_high
    bo_dn = cur < c_low
    bo_pct_up   = round((cur - c_high) / c_high * 100, 4) if bo_up else 0
    bo_pct_dn   = round((c_low - cur)  / c_low  * 100, 4) if bo_dn else 0
    momentum = "up" if closes[-1] > closes[-3] else "down" if closes[-1] < closes[-3] else "flat"
    return {"price": cur, "consol_high": round(c_high,4), "consol_low": round(c_low,4),
            "range_pct": range_pct, "atr": atr, "atr_pct": atr_pct, "range_vs_atr": range_vs_atr,
            "breakout_up": bo_up, "breakout_down": bo_dn,
            "breakout_pct_up": bo_pct_up, "breakout_pct_down": bo_pct_dn, "momentum": momentum}


SYSTEM_PROMPT = """You are a trading signal engine using Breakout Momentum strategy.
Output ONLY a raw JSON object.

BUY: breakout_up=true AND breakout_pct_up > 0.3% AND range_vs_atr > 1.2 AND momentum=up
SELL: breakout_down=true AND breakout_pct_down > 0.3% AND range_vs_atr > 1.2 AND momentum=down
HOLD: no breakout, or breakout_pct < 0.3%, or range_vs_atr < 0.8

CONFIDENCE (start 50): +20 breakout confirmed, +10/>0.5%, +20/>1.0%,
+15 range_vs_atr>1.5, +20 range_vs_atr>2.0, +10 momentum confirms,
+10 tight prior consolidation (<3%)

Output: {"signal":"BUY","confidence":75,"reasoning":"Price broke above consolidation with strong momentum"}"""


def ask_claude(symbol, ind):
    msg = (f"Symbol: {symbol}\nPrice: {ind['price']}\n"
           f"Consol high: {ind['consol_high']} | Consol low: {ind['consol_low']}\n"
           f"Range: {ind['range_pct']}% | ATR%: {ind['atr_pct']}% | Range vs ATR: {ind['range_vs_atr']}x\n"
           f"Breakout up: {ind['breakout_up']} (+{ind['breakout_pct_up']}%)\n"
           f"Breakout down: {ind['breakout_down']} (+{ind['breakout_pct_down']}%)\n"
           f"Momentum: {ind['momentum']}\nReturn signal as JSON.")
    resp = requests.post("https://api.anthropic.com/v1/messages",
                         headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                                  "anthropic-version": "2023-06-01"},
                         json={"model": "claude-sonnet-4-6", "max_tokens": 200, "system": SYSTEM_PROMPT,
                               "messages": [{"role": "user", "content": msg}]}, timeout=30)
    resp.raise_for_status()
    raw = resp.json()["content"][0]["text"].strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw.strip())


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
        if header: f.write("timestamp_dubai,symbol,price,signal,confidence,consol_high,consol_low,range_pct,atr_pct,range_vs_atr,breakout_up,breakout_down,momentum,webhook_fired,reasoning\n")
        f.write(f'{ts},{symbol},{ind["price"]},{signal["signal"]},{signal["confidence"]},'  
                f'{ind["consol_high"]},{ind["consol_low"]},{ind["range_pct"]},{ind["atr_pct"]},'  
                f'{ind["range_vs_atr"]},{ind["breakout_up"]},{ind["breakout_down"]},'  
                f'{ind["momentum"]},{fired},"{signal.get("reasoning","").replace(chr(34),chr(39))}"\n')
    print(f"  [{ts} Dubai] {symbol} | {signal['signal']} | {signal['confidence']}% | bo_up:{ind['breakout_up']}({ind['breakout_pct_up']}%) | Fired:{fired}")


def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*56}\nBreakout Momentum Strategy — {now} Dubai time\n{'='*56}")
    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            candles = get_candles(symbol, days=7)
            time.sleep(2)
            ind = get_indicators(candles)
            print(f"  Price: ${ind['price']:,.4f} | Range: {ind['consol_low']}-{ind['consol_high']} ({ind['range_pct']}%)")
            print(f"  ATR%: {ind['atr_pct']}% | RangeVsATR: {ind['range_vs_atr']}x | Momentum: {ind['momentum']}")
            print(f"  Breakout up: {ind['breakout_up']}(+{ind['breakout_pct_up']}%) | down: {ind['breakout_down']}(+{ind['breakout_pct_down']}%)")
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
