"""
Strategy: Advanced EMA Crossover
- Multi-timeframe confirmation (30min + 4hour must agree)
- RSI divergence detection (lookback=8 for reliability on 30m)
- EMA 9/21 crossover + RSI-7 filter
- SAR (Stop-and-Reverse): close opposite position before opening new one
Log file: trade_log_ema_advanced.csv

Fix: flat market RSI returns 50.0 (neutral) not 100.0
Fix: system prompt now explicitly handles the 30m/4h "agree" case in Step 1
     (previously only the disagree case was spelled out, which left agree
     cases ambiguous and let the model's reasoning drift from its own
     final JSON output) and requires the final signal/confidence fields to
     match the stated reasoning conclusion.
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
TICKER_MAP = {"BTCUSDT":"BTCUSDT","ETHUSDT":"ETHUSDT","SOLUSDT":"SOLUSDT","XRPUSDT":"XRPUSDT"}
COINGECKO_IDS = {"BTCUSDT":"bitcoin","ETHUSDT":"ethereum","SOLUSDT":"solana","XRPUSDT":"ripple"}
SYMBOLS        = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]
TAKE_PROFIT    = 1.5
STOP_LOSS      = 3.0
MIN_CONFIDENCE = 65
LOG_FILE       = "trade_log_ema_advanced.csv"

open_positions = {s: None for s in ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]}

# timestamp,symbol,price,signal,confidence,ema9_30m,ema21_30m,rsi7_30m,divergence,
# ema9_4h,ema21_4h,rsi7_4h,trend_4h,webhook_fired,reasoning
FIRED_COL = 13


def load_positions_from_log():
    if not os.path.exists(LOG_FILE): return
    last = {}
    try:
        with open(LOG_FILE, "r", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < FIRED_COL+1: continue
                symbol=row[1]; signal=row[3]; fired=row[FIRED_COL]
                if symbol in open_positions and signal in ("BUY","SELL") and fired=="True":
                    last[symbol] = signal
        for sym,sig in last.items(): open_positions[sym]=sig
        print("  [SAR] Loaded positions from log:", open_positions)
    except Exception as e:
        print(f"  [SAR] Could not load positions from log: {e}")


def get_candles(symbol, days):
    url = f"https://api.coingecko.com/api/v3/coins/{COINGECKO_IDS[symbol]}/ohlc"
    headers = {"x-cg-demo-api-key":COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    r = requests.get(url, params={"vs_currency":"usd","days":str(days)}, headers=headers, timeout=15)
    r.raise_for_status()
    return [{"time":datetime.fromtimestamp(c[0]/1000,tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M"),
             "open":float(c[1]),"high":float(c[2]),"low":float(c[3]),"close":float(c[4])}
            for c in r.json()]


def calculate_ema(closes, period):
    k=2/(period+1); e=sum(closes[:period])/period
    for p in closes[period:]: e=p*k+e*(1-k)
    return round(e,4)


def calculate_rsi(closes, period=7):
    """
    RSI with flat market fix: ag==0 AND al==0 returns 50.0 (neutral).
    Old code returned 100.0 on flat markets due to al==0 branch firing first.
    """
    if len(closes)<period+1: return 50.0
    g=[max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    l=[max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    ag,al=sum(g[-period:])/period,sum(l[-period:])/period
    if ag==0 and al==0: return 50.0   # flat market → neutral
    if al==0: return 100.0
    if ag==0: return 1.0
    return round(100-(100/(1+ag/al)),2)


def calculate_rsi_series(closes, period=7):
    return [calculate_rsi(closes[:i],period) for i in range(period+1,len(closes)+1)]


def detect_divergence(closes, rsi_series, lookback=8):
    """
    RSI divergence with lookback=8 (4 hours on 30m candles).
    Compares current candle against all 7 prior candles (rc[:-1]).
    """
    if len(closes)<lookback or len(rsi_series)<lookback: return None
    rc=closes[-lookback:]; rr=rsi_series[-lookback:]
    if rc[-1]<min(rc[:-1]) and not rr[-1]<min(rr[:-1]): return "bullish"
    if rc[-1]>max(rc[:-1]) and not rr[-1]>max(rr[:-1]): return "bearish"
    return None


def get_indicators(candles_30m, candles_4h):
    c30 = [c["close"] for c in candles_30m[-30:]]
    c4h = [c["close"] for c in candles_4h[-30:]]
    ema9_30m  = calculate_ema(c30, 9)
    ema21_30m = calculate_ema(c30, 21)
    rsi7_30m  = calculate_rsi(c30, 7)
    ema9_4h   = calculate_ema(c4h, 9)
    ema21_4h  = calculate_ema(c4h, 21)
    rsi7_4h   = calculate_rsi(c4h, 7)
    trend_4h  = "bullish" if ema9_4h > ema21_4h else "bearish"
    divergence = detect_divergence(c30, calculate_rsi_series(c30, 7))
    return {"ema9_30m":ema9_30m, "ema21_30m":ema21_30m, "rsi7_30m":rsi7_30m,
            "ema9_4h":ema9_4h, "ema21_4h":ema21_4h, "rsi7_4h":rsi7_4h,
            "trend_4h":trend_4h, "divergence":divergence}


def parse_claude_json(raw):
    raw=raw.strip()
    if raw.startswith("```"):
        raw=raw.split("```")[1]
        if raw.startswith("json"): raw=raw[4:]
        raw=raw.strip()
    m=re.search(r'\{[^{}]*\}',raw,re.DOTALL)
    if m: return json.loads(m.group())
    return json.loads(raw)


SYSTEM_PROMPT = """You are a professional crypto trading signal engine. Output ONLY a raw JSON object.
No text, no markdown, no backticks. One JSON object only.

STRATEGY: Advanced EMA + Multi-Timeframe + RSI Divergence

30m direction is bullish if EMA9_30m > EMA21_30m, else bearish.
(4h direction is given to you directly as "Trend".)

STEP 1 — MANDATORY HOLD CHECK:
If 30m direction and 4h Trend DISAGREE (one bullish, one bearish) → HOLD immediately, confidence 50.
If 30m direction and 4h Trend AGREE, proceed to STEP 2.

STEP 2 — OVERRIDE:
- RSI7_30m < 25 → BUY, confidence 95
- RSI7_30m > 75 → SELL, confidence 95
(Overrides apply regardless of Step 1's outcome.)

STEP 3 — SIGNAL + CONFIDENCE (start 50):
BUY: EMA9_30m > EMA21_30m AND 4h bullish
SELL: EMA9_30m < EMA21_30m AND 4h bearish
+15 30m EMA, +20 4h trend, +10 RSI zone,
+15 divergence confirms, -10 divergence opposes, -20 RSI strongly opposes.
Cap at 100.

FINAL CHECK (mandatory before you output): re-read your own reasoning.
The "signal" and "confidence" fields you output MUST exactly match the
conclusion your reasoning arrives at. If your reasoning text concludes
BUY or SELL, the "signal" field must be that same BUY or SELL — never
output HOLD while your reasoning argues for a directional trade, and
never state one confidence value in your reasoning and a different one
in the "confidence" field.

Output: {"signal":"BUY","confidence":85,"reasoning":"30m bullish EMA confirmed by 4h uptrend"}"""


def ask_claude(symbol, ind):
    msg = (f"Symbol:{symbol}\n"
           f"30m: EMA9={ind['ema9_30m']} EMA21={ind['ema21_30m']} RSI7={ind['rsi7_30m']} Div={ind['divergence'] or 'none'}\n"
           f"4h: EMA9={ind['ema9_4h']} EMA21={ind['ema21_4h']} RSI7={ind['rsi7_4h']} Trend={ind['trend_4h']}\nReturn JSON.")
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01"},
                      json={"model":"claude-sonnet-4-6","max_tokens":250,"system":SYSTEM_PROMPT,
                            "messages":[{"role":"user","content":msg}]},timeout=30)
    r.raise_for_status()
    return parse_claude_json(r.json()["content"][0]["text"])


def send_close_webhook(symbol, price):
    current=open_positions.get(symbol)
    if current is None: return False
    action="exit_long" if current=="BUY" else "exit_short"
    r=requests.post(WEBHOOK_URL,json={"secret":WEBHOOK_SECRET,"max_lag":"300",
               "timestamp":datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
               "trigger_price":str(price),"tv_exchange":"BINANCE",
               "tv_instrument":TICKER_MAP.get(symbol,symbol),"action":action,
               "bot_uuid":BOT_UUIDS[symbol]},timeout=10)
    if r.status_code==200:
        print(f"  [SAR] Closed {current} for {symbol} ({action})"); open_positions[symbol]=None; return True
    print(f"  [SAR] Close failed [{r.status_code}]: {r.text}"); return False


def fire_webhook(signal_str, price, symbol):
    current=open_positions.get(symbol)
    if current==signal_str:
        print(f"  [SAR] Already in {signal_str} for {symbol} — skipping"); return False
    if current is not None:
        print(f"  [SAR] Reversing {current} → {signal_str} for {symbol}")
        if send_close_webhook(symbol,price):
            print("  [SAR] Waiting 5s..."); time.sleep(5)
        else:
            print("  [SAR] Close failed — aborting"); return False
    action="enter_long" if signal_str=="BUY" else "enter_short"
    tp=TAKE_PROFIT if signal_str=="BUY" else -TAKE_PROFIT
    r=requests.post(WEBHOOK_URL,json={"secret":WEBHOOK_SECRET,"max_lag":"300",
               "timestamp":datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
               "trigger_price":str(price),"tv_exchange":"BINANCE",
               "tv_instrument":TICKER_MAP.get(symbol,symbol),"action":action,
               "bot_uuid":BOT_UUIDS[symbol],
               "take_profit":{"enabled":True,"steps":[{"order_type":"market","price_percent":tp,"volume_percent":100}]},
               "stop_loss":{"enabled":True,"order_type":"market","trigger_price_percent":STOP_LOSS}},timeout=10)
    if r.status_code==200:
        print(f"  Webhook {action}: SUCCESS"); open_positions[symbol]=signal_str; return True
    print(f"  Webhook: {'RATE LIMITED' if r.status_code==429 else f'FAILED [{r.status_code}]'} {r.text}"); return False


def log_result(symbol, signal, ind, price, fired):
    ts=datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    header=not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE)==0
    with open(LOG_FILE,"a",newline="") as f:
        w=csv.writer(f,quoting=csv.QUOTE_MINIMAL)
        if header:
            w.writerow(["timestamp_dubai","symbol","price","signal","confidence",
                        "ema9_30m","ema21_30m","rsi7_30m","divergence",
                        "ema9_4h","ema21_4h","rsi7_4h","trend_4h","webhook_fired","reasoning"])
        w.writerow([ts,symbol,price,signal["signal"],signal["confidence"],
                    ind["ema9_30m"],ind["ema21_30m"],ind["rsi7_30m"],ind["divergence"] or "none",
                    ind["ema9_4h"],ind["ema21_4h"],ind["rsi7_4h"],ind["trend_4h"],
                    fired,signal.get("reasoning","")])
    print(f"  [{ts}] {symbol} | {signal['signal']} | {signal['confidence']}% | 4H:{ind['trend_4h']} | Div:{ind['divergence'] or 'none'} | Fired:{fired}")


def run():
    now=datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*56}\nAdvanced EMA Strategy — {now} Dubai time\n{'='*56}")
    load_positions_from_log()
    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            print("  Fetching 30m candles...")
            c30m=get_candles(symbol,days=1); time.sleep(2)
            print("  Fetching 4h candles...")
            c4h=get_candles(symbol,days=14); time.sleep(2)
            price=c30m[-1]["close"]
            print(f"  Close: ${price:,.4f}")
            ind=get_indicators(c30m,c4h)
            print(f"  30m: EMA9={ind['ema9_30m']} EMA21={ind['ema21_30m']} RSI7={ind['rsi7_30m']}")
            print(f"  4h:  Trend={ind['trend_4h']} RSI7={ind['rsi7_4h']}")
            print(f"  Div: {ind['divergence'] or 'none'} | [SAR] pos={open_positions.get(symbol,'None')}")
            signal=ask_claude(symbol,ind)
            print(f"  Signal:{signal['signal']} Conf:{signal['confidence']}% | {signal.get('reasoning','')}")
            fired=False
            if signal["signal"] in ("BUY","SELL") and signal["confidence"]>=MIN_CONFIDENCE:
                fired=fire_webhook(signal["signal"],price,symbol)
            else:
                print("  HOLD — no webhook fired.")
            log_result(symbol,signal,ind,price,fired)
            time.sleep(3)
        except Exception as e:
            print(f"  ERROR: {e}")
    print(f"\n{'='*56}\nRun complete.\n{'='*56}\n")


if __name__ == "__main__":
    run()
