"""
Strategy: RSI Divergence
Best for: Catching reversals at market turning points

Logic:
- Bullish divergence: price makes lower low but RSI makes higher low → BUY
- Bearish divergence: price makes higher high but RSI makes lower high → SELL
- One of the most reliable reversal signals in technical analysis

Valid CoinGecko days: 1, 7, 14, 30, 90, 180, 365
"""

import requests
import json
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
        gains = [max(window[j]-window[j-1], 0) for j in range(1, len(window))]
        losses = [max(window[j-1]-window[j], 0) for j in range(1, len(window))]
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0: rsi_values.append(100.0)
        elif avg_gain == 0: rsi_values.append(1.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(round(100 - (100 / (1 + rs)), 2))
    return rsi_values


def find_divergence(closes, rsi_series, lookback=10):
    if len(closes) < lookback or len(rsi_series) < lookback:
        return {"type": "none", "strength": 0, "details": "insufficient data"}
    pw = closes[-lookback:]
    rw = rsi_series[-lookback:]
    cur_p, cur_r = pw[-1], rw[-1]
    prev_low_p = min(pw[:-3]) if len(pw) > 3 else pw[0]
    prev_low_r = min(rw[:-3]) if len(rw) > 3 else rw[0]
    prev_high_p = max(pw[:-3]) if len(pw) > 3 else pw[0]
    prev_high_r = max(rw[:-3]) if len(rw) > 3 else rw[0]
    if cur_p < prev_low_p and cur_r > prev_low_r:
        pd = round((prev_low_p - cur_p) / prev_low_p * 100, 2)
        rd = round(cur_r - prev_low_r, 2)
        return {"type": "bullish", "strength": min(100, int(pd*10+rd*2)),
                "details": f"price -{pd}% but RSI +{rd}pts"}
    if cur_p > prev_high_p and cur_r < prev_high_r:
        pd = round((cur_p - prev_high_p) / prev_high_p * 100, 2)
        rd = round(prev_high_r - cur_r, 2)
        return {"type": "bearish", "strength": min(100, int(pd*10+rd*2)),
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


SYSTEM_PROMPT = """You are a trading signal engine using RSI Divergence strategy.
Output ONLY a raw JSON object.

BUY: divergence.type = bullish (price lower low, RSI higher low)
SELL: divergence.type = bearish (price higher high, RSI lower high)
HOLD: divergence.type = none OR strength < 20

CONFIDENCE (start 50): +20 divergence detected, +10/+20/+25 by strength tier,
+15 EMA trend confirms, +10 RSI extreme, HOLD if no divergence.

Output: {"signal":"BUY","confidence":78,"reasoning":"Bullish RSI divergence — selling exhausted"}"""


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
    div = ind["divergence"]
    header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a") as f:
        if header: f.write("timestamp_dubai,symbol,price,signal,confidence,rsi,div_type,div_strength,ema9,ema21,trend,webhook_fired,reasoning\n")
        f.write(f'{ts},{symbol},{ind["price"]},{signal["signal"]},{signal["confidence"]},'
                f'{ind["rsi"]},{div["type"]},{div["strength"]},{ind["ema9"]},{ind["ema21"]},'
                f'{ind["trend"]},{fired},"{signal.get("reasoning","").replace(chr(34),chr(39))}"\n')
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
