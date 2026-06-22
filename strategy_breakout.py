"""
Strategy: Breakout Momentum
Best for: Markets in tight consolidation about to make a big move
Log file: trade_log_breakout.csv

Fix (June 21 2026): ask_claude() can return a JSON response missing the
"confidence" field (observed in production on a sibling strategy file:
KeyError: 'confidence' crashed the run). validate_signal_response() now
checks the parsed response has valid signal/confidence fields before
anything else touches them; if not, it returns a safe HOLD/0 result
instead of crashing, so one malformed response no longer aborts the run.

Fix (June 22 2026): write_run_summary() writes a structured JSON summary
to run_summary.json at the end of each run. The workflow then commits it
to run_summaries/YYYY-MM-DD_HH-MM_strategy[_manual].json in the repo so
Claude can read run history via GitHub MCP without needing browser access.
"""

import requests, json, re, csv, time, os, math
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
TICKER_MAP    = {"BTCUSDT": "BTCUSDT", "ETHUSDT": "ETHUSDT", "SOLUSDT": "SOLUSDT", "XRPUSDT": "XRPUSDT"}
COINGECKO_IDS = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "SOLUSDT": "solana", "XRPUSDT": "ripple"}
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
TAKE_PROFIT    = 3.0
STOP_LOSS      = 2.0
MIN_CONFIDENCE = 65
LOG_FILE       = "trade_log_breakout.csv"
SUMMARY_FILE   = "run_summary.json"


def get_candles(symbol, days=7):
    url = f"https://api.coingecko.com/api/v3/coins/{COINGECKO_IDS[symbol]}/ohlc"
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    r = requests.get(url, params={"vs_currency": "usd", "days": str(days)}, headers=headers, timeout=15)
    r.raise_for_status()
    return [{"time": datetime.fromtimestamp(c[0]/1000, tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M"),
             "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])}
            for c in r.json()]


def calc_atr(candles, period=14):
    trs = [max(candles[i]["high"]-candles[i]["low"],
               abs(candles[i]["high"]-candles[i-1]["close"]),
               abs(candles[i]["low"]-candles[i-1]["close"]))
           for i in range(1, len(candles))]
    return sum(trs[-period:])/min(len(trs), period) if trs else 0


def get_indicators(candles):
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    price  = closes[-1]
    lb     = min(20, len(candles)-1)
    ph     = highs[-lb-1:-1]; pl = lows[-lb-1:-1]
    rh     = max(ph) if ph else highs[-1]
    rl     = min(pl) if pl else lows[-1]
    rng    = round((rh-rl)/rl*100, 4) if rl else 0
    a      = calc_atr(candles)
    ap     = round(a/price*100, 4) if price else 0
    rva    = round(rng/ap, 4) if ap else 0
    win    = closes[-lb-1:-1] if len(closes) > lb else closes[:-1]
    if len(win) >= 2:
        m = sum(win)/len(win)
        std = math.sqrt(sum((x-m)**2 for x in win)/len(win))
        bbw = round((m+2*std-(m-2*std))/m*100, 4) if m else 5.0
    else:
        bbw = 5.0
    bu = price > rh*1.003; bd = price < rl*0.997
    bup = round((price-rh)/rh*100, 4) if bu else 0
    bdp = round((rl-price)/rl*100, 4) if bd else 0
    if len(closes) >= 4:   mom = "up" if closes[-1]>closes[-4] else "down" if closes[-1]<closes[-4] else "flat"
    elif len(closes) >= 2: mom = "up" if closes[-1]>closes[-2] else "down" if closes[-1]<closes[-2] else "flat"
    else:                  mom = "flat"
    return {"price": price, "range_high": rh, "range_low": rl, "range_pct": rng,
            "atr_pct": ap, "range_vs_atr": rva, "bb_width": bbw,
            "breakout_up": bu, "breakout_down": bd, "bo_up_pct": bup, "bo_down_pct": bdp, "momentum": mom}


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


SYSTEM_PROMPT = """You are a trading signal engine using Breakout Momentum strategy.
Output ONLY a raw JSON object. No text, no markdown.

BUY: breakout_up=True AND momentum=up AND range_vs_atr>=0.8
SELL: breakout_down=True AND momentum=down AND range_vs_atr>=0.8
HOLD: no confirmed breakout OR range_vs_atr<0.8

CONFIDENCE (start 50): +20 breakout confirmed, +15 momentum confirms,
+10 range_vs_atr>=1.2, +10 bb_width<4%, -10 range_vs_atr<0.8, -10 momentum conflicts.

Output: {"signal":"BUY","confidence":72,"reasoning":"Price broke above consolidation high"}"""


def ask_claude(symbol, ind):
    msg = (f"Symbol: {symbol}\nPrice: {ind['price']}\n"
           f"Range (prior 20): {ind['range_low']}-{ind['range_high']} ({ind['range_pct']}%)\n"
           f"ATR%: {ind['atr_pct']}% | RangeVsATR: {ind['range_vs_atr']}x | Momentum: {ind['momentum']}\n"
           f"Breakout up: {ind['breakout_up']}(+{ind['bo_up_pct']}%) | down: {ind['breakout_down']}(+{ind['bo_down_pct']}%)\n"
           f"BB width: {ind['bb_width']}%\nReturn JSON.")
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                      json={"model": "claude-sonnet-4-6", "max_tokens": 200, "system": SYSTEM_PROMPT,
                            "messages": [{"role": "user", "content": msg}]}, timeout=30)
    r.raise_for_status()
    return parse_claude_json(r.json()["content"][0]["text"])


def fire_webhook(signal_str, price, symbol):
    action = "enter_long" if signal_str == "BUY" else "enter_short"
    tp = TAKE_PROFIT if signal_str == "BUY" else -TAKE_PROFIT
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
                        "range_pct","atr_pct","range_vs_atr","bb_width",
                        "bo_up_pct","bo_down_pct","momentum","webhook_fired","reasoning"])
        w.writerow([ts, symbol, ind["price"], signal["signal"], signal["confidence"],
                    ind["range_pct"], ind["atr_pct"], ind["range_vs_atr"], ind["bb_width"],
                    ind["bo_up_pct"], ind["bo_down_pct"], ind["momentum"], fired,
                    signal.get("reasoning","")])
    print(f"  [{ts}] {symbol} | {signal['signal']} | {signal['confidence']}% | bo_up:{ind['bo_up_pct']}% bo_down:{ind['bo_down_pct']}% | Fired:{fired}")


def write_run_summary(summary):
    """
    Writes run_summary.json to disk. The workflow then commits this file
    to run_summaries/YYYY-MM-DD_HH-MM_strategy[_manual].json in the repo
    so Claude can read full run history via GitHub MCP.
    """
    with open(SUMMARY_FILE, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Run summary written to {SUMMARY_FILE}")


def run():
    now = datetime.now(tz=DUBAI_TZ)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*56}\nBreakout Momentum Strategy — {now_str} Dubai time\n{'='*56}")

    summary = {
        "run_at_dubai": now.strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": "breakout",
        "symbols": {},
        "signals_fired": 0,
        "errors": [],
        "invalids": [],
    }

    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            candles = get_candles(symbol, days=7); time.sleep(2)
            ind = get_indicators(candles)
            print(f"  Price: ${ind['price']:,.4f} | Range: {ind['range_low']}-{ind['range_high']} ({ind['range_pct']}%)")
            print(f"  ATR%:{ind['atr_pct']}% RangeVsATR:{ind['range_vs_atr']}x Momentum:{ind['momentum']}")
            print(f"  Breakout up:{ind['breakout_up']}(+{ind['bo_up_pct']}%) down:{ind['breakout_down']}(+{ind['bo_down_pct']}%)")
            raw_signal = ask_claude(symbol, ind)
            signal = validate_signal_response(raw_signal)
            is_invalid = signal["confidence"] == 0 and signal["reasoning"].startswith("[INVALID")
            if is_invalid:
                print(f"  ⚠️  {signal['reasoning']}")
                summary["invalids"].append(symbol)
            else:
                print(f"  Signal:{signal['signal']} Conf:{signal['confidence']}% | {signal.get('reasoning','')}")
            fired = False
            if not is_invalid and signal["signal"] in ("BUY","SELL") and signal["confidence"] >= MIN_CONFIDENCE:
                fired = fire_webhook(signal["signal"], ind["price"], symbol)
                if fired:
                    summary["signals_fired"] += 1
            else:
                print("  HOLD — no webhook fired.")
            log_result(symbol, signal, ind, fired)
            summary["symbols"][symbol] = {
                "signal": signal["signal"],
                "confidence": signal["confidence"],
                "fired": fired,
                "invalid": is_invalid,
            }
            time.sleep(3)
        except Exception as e:
            print(f"  ERROR: {e}")
            summary["errors"].append({"symbol": symbol, "error": str(e)})
            summary["symbols"][symbol] = {
                "signal": "ERROR",
                "confidence": 0,
                "fired": False,
                "invalid": False,
            }

    write_run_summary(summary)
    print(f"\n{'='*56}\nRun complete.\n{'='*56}\n")


if __name__ == "__main__":
    run()
