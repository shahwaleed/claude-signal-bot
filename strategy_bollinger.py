"""
Strategy: Bollinger Band Mean Reversion
Best for: Choppy, sideways, ranging markets
Log file: trade_log_bollinger.csv
"""

import requests, json, re, csv, time, os, math
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
TICKER_MAP     = {"BTCUSDT": "BTCUSDT", "ETHUSDT": "ETHUSDT", "SOLUSDT": "SOLUSDT", "XRPUSDT": "XRPUSDT"}
COINGECKO_IDS  = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "SOLUSDT": "solana", "XRPUSDT": "ripple"}
SYMBOLS        = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
BB_PERIOD      = 20
BB_STD         = 2.0
RSI_PERIOD     = 14
STOP_LOSS      = 3.0
MIN_CONFIDENCE = 65
LOG_FILE       = "trade_log_bollinger.csv"   # strategy-specific log
TP_MIN         = 0.5
TP_MAX         = 5.0


def get_candles(symbol, days=7):
    url = f"https://api.coingecko.com/api/v3/coins/{COINGECKO_IDS[symbol]}/ohlc"
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    r = requests.get(url, params={"vs_currency": "usd", "days": str(days)}, headers=headers, timeout=15)
    r.raise_for_status()
    return [{"time": datetime.fromtimestamp(c[0]/1000, tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M"),
             "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])}
            for c in r.json()]


def calculate_bollinger_bands(closes, period=20, num_std=2.0):
    if len(closes) < period: return None, None, None, None, None
    w = closes[-period:]; m = sum(w)/period
    std = math.sqrt(sum((x-m)**2 for x in w)/period)
    u = round(m+num_std*std, 4); m = round(m, 4); lo = round(m-num_std*std, 4)
    bw = round((u-lo)/m*100, 4) if m else 0
    pr = closes[-1]
    pb = round((pr-lo)/(u-lo), 4) if (u-lo) else 0.5
    return u, m, lo, bw, pb


def calculate_rsi(closes, period=14):
    if len(closes) < period+1: return 50.0
    g = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    l = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[-period:])/period, sum(l[-period:])/period
    if al == 0: return 100.0
    if ag == 0: return 1.0
    return round(100-(100/(1+ag/al)), 2)


def get_indicators(candles):
    closes = [c["close"] for c in candles]
    price  = closes[-1]
    upper, middle, lower, bw, pb = calculate_bollinger_bands(closes, BB_PERIOD, BB_STD)
    rsi    = calculate_rsi(closes, RSI_PERIOD)
    squeeze = bw is not None and bw < 3.0
    dl = round((price-lower)/price*100, 2) if lower else 0
    du = round((upper-price)/price*100, 2) if upper else 0
    return {"price": price, "upper": upper, "middle": middle, "lower": lower,
            "bandwidth": bw, "percent_b": pb, "rsi": rsi, "squeeze": squeeze,
            "dist_lower_pct": dl, "dist_upper_pct": du}


def parse_claude_json(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip()
    m = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
    if m: return json.loads(m.group())
    return json.loads(raw)


SYSTEM_PROMPT = """You are a crypto trading signal engine using Bollinger Band Mean Reversion.
Output ONLY a raw JSON object. No text, no markdown.

BUY: percent_b<=0.05 AND RSI<40 | percent_b<=0.15 AND RSI<35 | RSI<25 override
SELL: percent_b>=0.95 AND RSI>60 | percent_b>=0.85 AND RSI>65 | RSI>75 override
HOLD: percent_b 0.2-0.8, RSI 35-65

CONFIDENCE (start 50): +25 band break, +15 near band, +20 RSI<30/>70, +10 RSI<40/>60, +10 squeeze, min 80 on override.
take_profit_pct = distance to middle band. Min 0.5, Max 5.0. Never 0 or negative.

Output: {"signal":"BUY","confidence":78,"take_profit_pct":1.2,"reasoning":"Price at lower band"}"""


def ask_claude(symbol, ind):
    msg = (f"Symbol: {symbol}\nPrice: {ind['price']}\nUpper: {ind['upper']}\nMiddle: {ind['middle']}\n"
           f"Lower: {ind['lower']}\nBandwidth: {ind['bandwidth']}%\nPercent-B: {ind['percent_b']}\n"
           f"RSI-14: {ind['rsi']}\nSqueeze: {ind['squeeze']}\n"
           f"Dist lower: {ind['dist_lower_pct']}%\nDist upper: {ind['dist_upper_pct']}%\nReturn JSON.")
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                      json={"model": "claude-sonnet-4-6", "max_tokens": 200, "system": SYSTEM_PROMPT,
                            "messages": [{"role": "user", "content": msg}]}, timeout=30)
    r.raise_for_status()
    return parse_claude_json(r.json()["content"][0]["text"])


def fire_webhook(signal_str, price, symbol, take_profit_pct):
    tp = round(max(TP_MIN, min(TP_MAX, float(take_profit_pct))), 2)
    if signal_str == "SELL": tp = -tp
    action = "enter_long" if signal_str == "BUY" else "enter_short"
    payload = {"secret": WEBHOOK_SECRET, "max_lag": "300",
               "timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
               "trigger_price": str(price), "tv_exchange": "BINANCE",
               "tv_instrument": TICKER_MAP.get(symbol, symbol), "action": action,
               "bot_uuid": BOT_UUIDS[symbol],
               "take_profit": {"enabled": True, "steps": [{"order_type": "market", "price_percent": tp, "volume_percent": 100}]},
               "stop_loss": {"enabled": True, "order_type": "market", "trigger_price_percent": STOP_LOSS}}
    r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    print(f"  Webhook {action}: {'SUCCESS' if r.status_code==200 else f'FAILED [{r.status_code}]'} (TP:{tp}%, SL:-{STOP_LOSS}%)")
    return r.status_code == 200


def log_result(symbol, signal, ind, fired):
    ts = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        if header:
            w.writerow(["timestamp_dubai","symbol","price","signal","confidence",
                        "upper_band","middle_band","lower_band","percent_b",
                        "bandwidth","squeeze","rsi14","take_profit_pct","webhook_fired","reasoning"])
        w.writerow([ts, symbol, ind["price"], signal["signal"], signal["confidence"],
                    ind["upper"], ind["middle"], ind["lower"], ind["percent_b"],
                    ind["bandwidth"], ind["squeeze"], ind["rsi"],
                    signal.get("take_profit_pct",""), fired, signal.get("reasoning","")])
    print(f"  [{ts}] {symbol} | {signal['signal']} | {signal['confidence']}% | %B:{ind['percent_b']} | RSI:{ind['rsi']} | Fired:{fired}")


def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*58}\nBollinger Band Mean Reversion — {now} Dubai time\n{'='*58}")
    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            candles = get_candles(symbol, days=7); time.sleep(2)
            ind = get_indicators(candles)
            print(f"  Price: ${ind['price']:,.4f}")
            print(f"  Bands: U={ind['upper']} M={ind['middle']} L={ind['lower']}")
            print(f"  %B:{ind['percent_b']} RSI:{ind['rsi']} BW:{ind['bandwidth']}% Squeeze:{ind['squeeze']}")
            signal = ask_claude(symbol, ind)
            tp = signal.get("take_profit_pct", 1.5)
            print(f"  Signal:{signal['signal']} Conf:{signal['confidence']}% TP:{tp}% | {signal.get('reasoning','')}")
            fired = False
            if signal["signal"] in ("BUY","SELL") and signal["confidence"] >= MIN_CONFIDENCE:
                fired = fire_webhook(signal["signal"], ind["price"], symbol, tp)
            else:
                print("  HOLD — no webhook fired.")
            log_result(symbol, signal, ind, fired)
            time.sleep(3)
        except Exception as e:
            print(f"  ERROR: {e}")
    print(f"\n{'='*58}\nRun complete.\n{'='*58}\n")


if __name__ == "__main__":
    run()
