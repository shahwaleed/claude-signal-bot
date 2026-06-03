"""
Autonomous Market Analyzer
Runs 4x daily aligned to professional trading session opens (Dubai time):
  6:00 AM  — before Asia close
  12:00 PM — midday
  6:00 PM  — before NY open
  12:00 AM — end of NY session

Architecture:
  Writes chosen strategy to config.json which signal_bot.yml reads at runtime.
"""

import requests
import json
import os
import base64
from datetime import datetime, timezone, timedelta

DUBAI_TZ          = timezone(timedelta(hours=4))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER      = "shahwaleed"
GITHUB_REPO       = "claude-signal-bot"
CONFIG_PATH       = "config.json"

COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple"}
STRATEGIES    = ["bollinger", "ema_advanced", "vwap", "rsi_divergence", "breakout", "ema_basic"]


# ─────────────────────────────────────────
# MARKET DATA
# ─────────────────────────────────────────

def get_ohlc(coin_id, days):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    r = requests.get(url, params={"vs_currency": "usd", "days": str(days)},
                     headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def calc_ema(closes, p):
    k = 2 / (p + 1)
    e = sum(closes[:p]) / p
    for c in closes[p:]:
        e = c * k + e * (1 - k)
    return round(e, 4)


def calc_rsi(closes, p=14):
    if len(closes) < p + 1:
        return 50.0
    g = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    l = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[-p:]) / p, sum(l[-p:]) / p
    if al == 0: return 100.0
    if ag == 0: return 1.0
    return round(100 - (100 / (1 + ag / al)), 2)


def calc_bb_width(closes, p=20):
    import math
    if len(closes) < p:
        return 5.0
    w = closes[-p:]
    m = sum(w) / p
    std = math.sqrt(sum((x - m) ** 2 for x in w) / p)
    return round((m + 2*std - (m - 2*std)) / m * 100, 4) if m > 0 else 5.0


def calc_atr_pct(candles, p=14):
    trs = [max(candles[i][2] - candles[i][3],
               abs(candles[i][2] - candles[i-1][4]),
               abs(candles[i][3] - candles[i-1][4]))
           for i in range(1, len(candles))]
    a = sum(trs[-p:]) / min(len(trs), p) if trs else 0
    cur = candles[-1][4]
    return round(a / cur * 100, 4) if cur > 0 else 2.0


def detect_rsi_divergence(candles_30m):
    """
    Check if bullish RSI divergence is forming:
    Price making lower lows while RSI makes higher lows.
    Returns True only if pattern is confirmed, not just oversold.
    """
    if len(candles_30m) < 20:
        return False
    closes = [c[4] for c in candles_30m]
    # Calculate RSI series
    rsi_series = []
    for i in range(15, len(closes)):
        rsi_series.append(calc_rsi(closes[:i], 14))

    if len(rsi_series) < 10:
        return False

    # Look for: recent price lower than 10 candles ago, but RSI higher
    recent_price = closes[-1]
    past_price   = min(closes[-10:-1])
    recent_rsi   = rsi_series[-1]
    past_rsi     = min(rsi_series[-10:-1]) if len(rsi_series) >= 10 else rsi_series[0]

    price_lower_low = recent_price < past_price
    rsi_higher_low  = recent_rsi > past_rsi

    return price_lower_low and rsi_higher_low


def analyze_market():
    import time
    data = {}
    for sym, cid in COINGECKO_IDS.items():
        print(f"  Fetching {sym}...")
        try:
            c30m = get_ohlc(cid, 1);  time.sleep(3)
            c4h  = get_ohlc(cid, 7);  time.sleep(3)
            c1d  = get_ohlc(cid, 30); time.sleep(3)
            cl30 = [c[4] for c in c30m]
            cl4h = [c[4] for c in c4h]
            cl1d = [c[4] for c in c1d]
            t30 = "bullish" if calc_ema(cl30, 9) > calc_ema(cl30, 21) else "bearish"
            t4h = "bullish" if calc_ema(cl4h, 9) > calc_ema(cl4h, 21) else "bearish"
            t1d = "bullish" if calc_ema(cl1d, 9) > calc_ema(cl1d, 21) else "bearish"
            h7d = max(c[2] for c in c4h)
            l7d = min(c[3] for c in c4h)

            rsi_30m = calc_rsi(cl30)
            rsi_4h  = calc_rsi(cl4h)
            rsi_1d  = calc_rsi(cl1d)

            # Key addition: actually check if divergence is forming
            divergence_forming = detect_rsi_divergence(c30m)

            data[sym] = {
                "price":              cl30[-1],
                "change_24h":         round((cl30[-1] - cl30[0]) / cl30[0] * 100, 2) if cl30[0] else 0,
                "change_7d":          round((cl4h[-1] - cl4h[0]) / cl4h[0] * 100, 2) if cl4h[0] else 0,
                "trend_30m":          t30, "trend_4h": t4h, "trend_1d": t1d,
                "aligned":            t30 == t4h == t1d,
                "rsi_30m":            rsi_30m,
                "rsi_4h":             rsi_4h,
                "rsi_1d":             rsi_1d,
                "bb_width_4h":        calc_bb_width(cl4h),
                "atr_pct_4h":         calc_atr_pct(c4h),
                "range_7d_pct":       round((h7d - l7d) / l7d * 100, 2) if l7d else 0,
                "divergence_forming": divergence_forming,
                "crash_mode":         rsi_30m < 20 and rsi_4h < 30 and not divergence_forming,
            }
        except Exception as e:
            print(f"  ERROR {sym}: {e}")
    return data


# ─────────────────────────────────────────
# CLAUDE ANALYSIS
# ─────────────────────────────────────────

ANALYSIS_PROMPT = """You are an expert crypto market analyst. Analyze market conditions and recommend the best trading strategy.
Output ONLY a raw JSON object. No text, no markdown, no explanation.

Available strategies and PRECISE conditions for each:

- bollinger: Mean reversion. USE WHEN: RSI < 25 (extreme oversold) OR RSI > 75 (extreme overbought) AND price outside bands. 
  IMPORTANT: In crash conditions (RSI < 20, trends all bearish, no divergence), bollinger is BETTER than rsi_divergence because 
  it has an RSI override that fires BUY signals immediately. Use bollinger when market is deeply oversold regardless of trend.

- ema_advanced: Trending market. USE WHEN: trends aligned across 30m+4h+daily, RSI between 35-65, ATR expanding.
  Do NOT use when all trends are bearish — it will fire SELL signals which fail on Spot.

- vwap: Strong institutional trend. USE WHEN: clear directional move, price consistently one side of VWAP.
  Similar to ema_advanced but better for intraday trending sessions.

- rsi_divergence: Reversal hunter. USE WHEN: RSI extreme AND divergence_forming=true for at least 2 assets.
  CRITICAL: Do NOT pick this strategy just because RSI is oversold. It ONLY fires signals when price makes 
  a lower low while RSI makes a higher low simultaneously. Without divergence_forming=true, it will HOLD all day
  and generate zero signals. If divergence_forming=false for most assets, do NOT pick this strategy.

- breakout: Momentum. USE WHEN: BB width < 3% (tight squeeze), low ATR, big move expected.
  Not useful in already-trending or already-crashed markets.

- ema_basic: Simple fallback. Use only if nothing else fits.

DECISION RULES (in order of priority):
1. If most assets show divergence_forming=true → rsi_divergence
2. If most assets show crash_mode=true (RSI < 20, no divergence) → bollinger (RSI override will fire BUYs)
3. If BB width < 3% for most assets → breakout
4. If trends aligned bullish across all timeframes, RSI 35-65 → ema_advanced or vwap
5. If ranging/choppy, mixed trends → bollinger
6. Otherwise → ema_basic

Output format:
{"recommended_strategy":"bollinger","confidence":82,"market_condition":"crash/oversold — bollinger RSI override active","reasoning":"One sentence explaining why this strategy will actually generate signals","secondary_strategy":"rsi_divergence","key_signals":["signal1","signal2","signal3"]}"""


def ask_claude(market_data):
    summary = "CURRENT MARKET CONDITIONS:\n\n"
    for sym, d in market_data.items():
        summary += (
            f"=== {sym} ===\n"
            f"Price: ${d['price']:,.4f} | 24h: {d['change_24h']}% | 7d: {d['change_7d']}%\n"
            f"Trends: 30m={d['trend_30m']} | 4h={d['trend_4h']} | 1d={d['trend_1d']} | aligned={d['aligned']}\n"
            f"RSI: 30m={d['rsi_30m']} | 4h={d['rsi_4h']} | 1d={d['rsi_1d']}\n"
            f"BB width (4h): {d['bb_width_4h']}% | ATR%: {d['atr_pct_4h']}% | 7d range: {d['range_7d_pct']}%\n"
            f"divergence_forming: {d['divergence_forming']} | crash_mode: {d['crash_mode']}\n\n"
        )
    summary += f"Available strategies: {', '.join(STRATEGIES)}\nAnalyze and recommend the best strategy."

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01"},
        json={"model": "claude-sonnet-4-6", "max_tokens": 400,
              "system": ANALYSIS_PROMPT,
              "messages": [{"role": "user", "content": summary}]},
        timeout=30
    )
    resp.raise_for_status()
    raw = resp.json()["content"][0]["text"].strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw.strip())


# ─────────────────────────────────────────
# CONFIG.JSON
# ─────────────────────────────────────────

def get_config_sha():
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{CONFIG_PATH}"
    r = requests.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}",
                                    "Accept": "application/vnd.github.v3+json"}, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()["sha"]


def write_config(rec):
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    config = {
        "strategy":           rec["recommended_strategy"],
        "secondary_strategy": rec.get("secondary_strategy", ""),
        "confidence":         rec["confidence"],
        "market_condition":   rec.get("market_condition", ""),
        "reasoning":          rec.get("reasoning", ""),
        "key_signals":        rec.get("key_signals", []),
        "updated_at_dubai":   now,
    }
    content = json.dumps(config, indent=2)
    encoded = base64.b64encode(content.encode()).decode()
    sha = get_config_sha()
    payload = {
        "message": f"Auto-strategy: {rec['recommended_strategy']} — {now} Dubai",
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{CONFIG_PATH}"
    r = requests.put(url,
                     headers={"Authorization": f"token {GITHUB_TOKEN}",
                              "Accept": "application/vnd.github.v3+json"},
                     json=payload, timeout=10)
    r.raise_for_status()
    print(f"  config.json updated: strategy = {rec['recommended_strategy']}")


# ─────────────────────────────────────────
# LOG
# ─────────────────────────────────────────

def log_decision(rec):
    ts = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log = "strategy_log.csv"
    header = not os.path.exists(log) or os.path.getsize(log) == 0
    with open(log, "a") as f:
        if header:
            f.write("timestamp_dubai,strategy,secondary,confidence,market_condition,reasoning\n")
        r = rec.get("reasoning", "").replace('"', "'")
        f.write(
            f'{ts},{rec["recommended_strategy"]},{rec.get("secondary_strategy","")},'
            f'{rec["confidence"]},{rec.get("market_condition","")},"{r}"\n'
        )


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*60}")
    print(f"Market Analyzer — {now} Dubai time")
    print(f"{'='*60}")

    print("\n[1/3] Collecting market data across 3 timeframes...")
    data = analyze_market()
    if not data:
        print("ERROR: no market data — aborting")
        return

    # Print divergence and crash mode summary
    print("\n  Strategy signal summary:")
    for sym, d in data.items():
        print(f"  {sym}: RSI_30m={d['rsi_30m']} | divergence_forming={d['divergence_forming']} | crash_mode={d['crash_mode']}")

    print("\n[2/3] Claude analyzing conditions...")
    rec = ask_claude(data)
    print(f"\n  Recommended: {rec['recommended_strategy'].upper()}")
    print(f"  Secondary:   {rec.get('secondary_strategy','').upper()}")
    print(f"  Confidence:  {rec['confidence']}%")
    print(f"  Market:      {rec.get('market_condition','')}")
    print(f"  Reasoning:   {rec.get('reasoning','')}")
    print(f"  Key signals: {rec.get('key_signals',[])}")

    print("\n[3/3] Writing strategy to config.json...")
    try:
        write_config(rec)
    except Exception as e:
        print(f"  ERROR writing config: {e}")

    log_decision(rec)

    print(f"\n{'='*60}")
    print("Analysis complete. Signal bot will use new strategy on next run.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    run()
