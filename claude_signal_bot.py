"""
Claude Signal Bot — EMA Basic fallback strategy
BTC/ETH/SOL/XRP
Strategy: EMA 9/21 crossover + RSI-7 filter
Log file: trade_log_ema_basic.csv

Fix (June 21 2026): ask_claude() can return a JSON response missing the
"confidence" field (observed in production on a sibling strategy file:
KeyError: 'confidence' crashed the run). validate_signal_response() now
checks the parsed response has valid signal/confidence fields before
anything else touches them; if not, it returns a safe HOLD/0 result
instead of crashing, so one malformed response no longer aborts the run.
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
# Routed through a Cloudflare Worker relay because GitHub Actions IPs are
# blocked by 3Commas (confirmed June 20 2026). Falls back to the direct
# 3Commas URL if the relay secret isn't set (e.g. local runs).
WEBHOOK_URL    = os.environ.get("WEBHOOK_RELAY_URL", "https://api.3commas.io/signal_bots/webhooks")
WEBHOOK_SECRET = "eyJhbGciOiJIUzI1NiJ9.eyJzaWduYWxzX3NvdXJjZV9pZCI6MTMwNTYyfQ.DnbuKVB9cslOFa5l1WtrKH1PFsvacsV0Vfkh_e3E_DY"
BOT_UUIDS = {
    "BTCUSDT": "67d3e022-7414-4ef8-8b6c-3d5c56a09667",
    "ETHUSDT": "edac8b79-ac23-4af5-a6eb-666432a0cb57",
    "SOLUSDT": "3d72a934-50a2-4fd6-bbd2-0e678c841ef4",
    "XRPUSDT": "e798e648-fab5-4b94-82af-052228fa9ed1",
}
TICKER_MAP    = {"BTCUSDT":"BTCUSDT","ETHUSDT":"ETHUSDT","SOLUSDT":"SOLUSDT","XRPUSDT":"XRPUSDT"}
COINGECKO_IDS = {"BTCUSDT":"bitcoin","ETHUSDT":"ethereum","SOLUSDT":"solana","XRPUSDT":"ripple"}
SYMBOLS         = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]
TAKE_PROFIT_PCT = 1.5
STOP_LOSS_PCT   = 3.0
MIN_CONFIDENCE  = 60
LOG_FILE        = "trade_log_ema_basic.csv"   # strategy-specific log


def get_candles(symbol, limit=30):
    url = f"https://api.coingecko.com/api/v3/coins/{COINGECKO_IDS[symbol]}/ohlc"
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    r = requests.get(url, params={"vs_currency":"usd","days":"1"}, headers=headers, timeout=15)
    r.raise_for_status()
    candles = []
    for c in r.json()[-limit:]:
        ts = datetime.fromtimestamp(c[0]/1000, tz=DUBAI_TZ)
        candles.append({"time":ts.strftime("%Y-%m-%d %H:%M"),
                        "open":float(c[1]),"high":float(c[2]),"low":float(c[3]),"close":float(c[4])})
    return candles


def calculate_ema(closes, period):
    k = 2/(period+1); e = sum(closes[:period])/period
    for p in closes[period:]: e = p*k + e*(1-k)
    return round(e, 4)


def calculate_rsi(closes, period=7):
    if len(closes) < period+1: return 50.0
    g = [max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    l = [max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    ag, al = sum(g[-period:])/period, sum(l[-period:])/period
    if al==0: return 100.0
    if ag==0: return 1.0
    return round(100-(100/(1+ag/al)),2)


def get_indicators(candles):
    closes = [c["close"] for c in candles]
    return calculate_ema(closes,9), calculate_ema(closes,21), calculate_rsi(closes,7)


def parse_claude_json(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip()
    m = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
    if m: return json.loads(m.group())
    return json.loads(raw)


def validate_signal_response(raw_signal):
    """
    Guarantees a safe, complete dict is returned: {"signal", "confidence",
    "reasoning"}. If raw_signal is missing required fields, has the wrong
    types, or "signal" isn't BUY/SELL/HOLD, returns a safe HOLD/0 result
    instead of letting a KeyError (or similar) crash the run. The
    "reasoning" field explains what was wrong, so it's visible in logs/CSV.
    """
    if not isinstance(raw_signal, dict):
        return {"signal": "HOLD", "confidence": 0,
                "reasoning": f"[INVALID: response was not a dict, got {type(raw_signal).__name__}]"}
    signal = raw_signal.get("signal")
    confidence = raw_signal.get("confidence")
    reasoning = raw_signal.get("reasoning", "")
    if signal not in ("BUY", "SELL", "HOLD"):
        return {"signal": "HOLD", "confidence": 0,
                "reasoning": f"[INVALID: signal field missing or not BUY/SELL/HOLD, got {signal!r}] {reasoning}"}
    if not isinstance(confidence, (int, float)):
        return {"signal": "HOLD", "confidence": 0,
                "reasoning": f"[INVALID: confidence field missing or not numeric, got {confidence!r}] {reasoning}"}
    return {"signal": signal, "confidence": confidence, "reasoning": reasoning}


SYSTEM_PROMPT = """You are a trading signal engine. Output ONLY a raw JSON object.
No text, no markdown, no backticks.

RULES:
- EMA9 > EMA21 AND RSI7 < 65: BUY
- EMA9 < EMA21 AND RSI7 > 35: SELL
- RSI7 > 75: SELL override
- RSI7 < 25: BUY override
- Otherwise: HOLD

Output: {"signal":"BUY","confidence":75,"reasoning":"EMA-9 above EMA-21 with RSI not overbought"}"""


def ask_claude(symbol, ema9, ema21, rsi7):
    msg = f"Symbol: {symbol}\nEMA-9: {ema9}\nEMA-21: {ema21}\nRSI-7: {rsi7}\nReturn signal as JSON."
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01"},
                      json={"model":"claude-sonnet-4-6","max_tokens":150,"system":SYSTEM_PROMPT,
                            "messages":[{"role":"user","content":msg}]},timeout=30)
    r.raise_for_status()
    return parse_claude_json(r.json()["content"][0]["text"])


def fire_webhook(signal_str, price, symbol):
    action = "enter_long" if signal_str=="BUY" else "enter_short"
    tp = TAKE_PROFIT_PCT if signal_str=="BUY" else -TAKE_PROFIT_PCT
    r = requests.post(WEBHOOK_URL, json={"secret":WEBHOOK_SECRET,"max_lag":"300",
               "timestamp":datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
               "trigger_price":str(price),"tv_exchange":"BINANCE",
               "tv_instrument":TICKER_MAP.get(symbol,symbol),"action":action,
               "bot_uuid":BOT_UUIDS[symbol],
               "take_profit":{"enabled":True,"steps":[{"order_type":"market","price_percent":tp,"volume_percent":100}]},
               "stop_loss":{"enabled":True,"order_type":"market","trigger_price_percent":STOP_LOSS_PCT}},timeout=10)
    print(f"  Webhook {action}: {'SUCCESS' if r.status_code==200 else f'FAILED [{r.status_code}]'}")
    return r.status_code==200


def log_result(symbol, signal, ema9, ema21, rsi7, price, fired):
    ts = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE)==0
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        if header:
            w.writerow(["timestamp_dubai","symbol","price","signal","confidence","ema9","ema21","rsi7","webhook_fired","reasoning"])
        w.writerow([ts,symbol,price,signal["signal"],signal["confidence"],ema9,ema21,rsi7,fired,signal.get("reasoning","")])
    print(f"  [{ts}] {symbol} | {signal['signal']} | {signal['confidence']}% | ${price:,.4f} | Fired:{fired}")


def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*52}\nClaude Signal Bot (EMA Basic) — {now} Dubai time\n{'='*52}")
    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            candles = get_candles(symbol, 30)
            price = candles[-1]["close"]
            print(f"  Close: ${price:,.4f}")
            ema9, ema21, rsi7 = get_indicators(candles)
            print(f"  EMA9:{ema9} EMA21:{ema21} RSI7:{rsi7}")
            raw_signal = ask_claude(symbol, ema9, ema21, rsi7)
            signal = validate_signal_response(raw_signal)
            if signal["confidence"]==0 and signal["reasoning"].startswith("[INVALID"):
                print(f"  ⚠️  {signal['reasoning']}")
            else:
                print(f"  Signal:{signal['signal']} Conf:{signal['confidence']}% | {signal.get('reasoning','')}")
            fired = False
            if signal["signal"] in ("BUY","SELL") and signal["confidence"]>=MIN_CONFIDENCE:
                fired = fire_webhook(signal["signal"], price, symbol)
            else:
                print("  HOLD — no webhook fired.")
            log_result(symbol, signal, ema9, ema21, rsi7, price, fired)
            time.sleep(3)
        except Exception as e:
            print(f"  ERROR: {e}")
    print(f"\n{'='*52}\nRun complete.\n{'='*52}\n")


if __name__ == "__main__":
    run()
