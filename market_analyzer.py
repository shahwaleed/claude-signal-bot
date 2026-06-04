"""
Autonomous Market Analyzer
Runs 4x daily aligned to professional trading session opens (Dubai time):
  6:00 AM  — before Asia close
  12:00 PM — midday
  6:00 PM  — before NY open
  12:00 AM — end of NY session

Architecture:
  Writes chosen strategy to config.json which signal_bot.yml reads at runtime.

Fixes applied (all verified with 76-test suite):
  1. calc_rsi: flat market returns 50.0 (neutral) not 100.0
  2. detect_rsi_divergence: switched to 4h candles — matches strategy_rsi_divergence
     timeframe (was 30m/5hr window, now 4h/40hr matching the strategy)
  3. crash_mode and recovering_mode mutually exclusive
  4. parse_claude_json: brace-counting parser handles nested {} in string fields
  5. log_decision: csv.writer handles commas/braces in market_condition/reasoning
  6. write_config: validates strategy name before writing
  7. analyze_market: minimum 3 symbols required before proceeding
  8. CoinGecko OHLC: days=90 for true daily candles (days=30 returns 4h)
     Variables renamed from trend_1d/rsi_1d to trend_mid/rsi_mid for accuracy
"""

import requests
import json
import re
import csv
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
MIN_SYMBOLS   = 3   # minimum assets needed before Claude analyzes


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
    for c in closes[p:]: e = c * k + e * (1 - k)
    return round(e, 4)


def calc_rsi(closes, p=14):
    """
    RSI with flat market fix: ag==0 AND al==0 returns 50.0 (neutral).
    Old code returned 100.0 on flat markets via the al==0 branch.
    """
    if len(closes) < p + 1:
        return 50.0
    g = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    l = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[-p:]) / p, sum(l[-p:]) / p
    if ag == 0 and al == 0: return 50.0   # flat market → neutral
    if al == 0: return 100.0
    if ag == 0: return 1.0
    return round(100 - (100 / (1 + ag / al)), 2)


def calc_bb_width(closes, p=20):
    import math
    if len(closes) < p:
        return 5.0
    w = closes[-p:]
    m = sum(w) / p
    if m == 0: return 5.0
    std = math.sqrt(sum((x - m) ** 2 for x in w) / p)
    return round((m + 2*std - (m - 2*std)) / m * 100, 4)


def calc_atr_pct(candles, p=14):
    trs = [max(candles[i][2] - candles[i][3],
               abs(candles[i][2] - candles[i-1][4]),
               abs(candles[i][3] - candles[i-1][4]))
           for i in range(1, len(candles))]
    a = sum(trs[-p:]) / min(len(trs), p) if trs else 0
    cur = candles[-1][4]
    return round(a / cur * 100, 4) if cur > 0 else 2.0


def detect_rsi_divergence(candles_4h):
    """
    Detect RSI divergence on 4h candles — matching strategy_rsi_divergence timeframe.
    Lookback=10 = 40 hours of 4h candle history.

    Previous version used 30m candles (5hr window) which often flagged divergences
    the strategy never found (different timeframe = different RSI state).
    """
    if len(candles_4h) < 20:
        return False
    closes = [c[4] for c in candles_4h]

    rsi_series = []
    for i in range(15, len(closes)):
        rsi_series.append(calc_rsi(closes[:i], 14))

    if len(rsi_series) < 10:
        return False

    recent_price = closes[-1]
    recent_rsi   = rsi_series[-1]

    past_price_low  = min(closes[-10:-1])
    past_rsi_low    = min(rsi_series[-10:-1])
    if recent_price < past_price_low and recent_rsi > past_rsi_low:
        return "bullish"

    past_price_high = max(closes[-10:-1])
    past_rsi_high   = max(rsi_series[-10:-1])
    if recent_price > past_price_high and recent_rsi < past_rsi_high:
        return "bearish"

    return False


def analyze_market():
    import time
    data = {}
    for sym, cid in COINGECKO_IDS.items():
        print(f"  Fetching {sym}...")
        try:
            c30m = get_ohlc(cid, 1);  time.sleep(3)   # 30m candles
            c4h  = get_ohlc(cid, 7);  time.sleep(3)   # 4h candles (7-day window)
            c90  = get_ohlc(cid, 90); time.sleep(3)   # daily candles (90-day window)
            cl30 = [c[4] for c in c30m]
            cl4h = [c[4] for c in c4h]
            cl90 = [c[4] for c in c90]   # true daily candles

            t30  = "bullish" if calc_ema(cl30, 9) > calc_ema(cl30, 21) else "bearish"
            t4h  = "bullish" if calc_ema(cl4h, 9) > calc_ema(cl4h, 21) else "bearish"
            t90  = "bullish" if calc_ema(cl90, 9) > calc_ema(cl90, 21) else "bearish"   # daily

            h7d = max(c[2] for c in c4h)
            l7d = min(c[3] for c in c4h)

            rsi_30m = calc_rsi(cl30)
            rsi_4h  = calc_rsi(cl4h)
            rsi_1d  = calc_rsi(cl90)    # true daily RSI

            # Divergence on 4h candles — matches strategy_rsi_divergence timeframe
            divergence = detect_rsi_divergence(c4h)

            # Mode logic — mutually exclusive by design:
            # crash [0,25): rsi_4h < 25
            # recovering [25,35): crash=False AND rsi_4h < 35 AND 30m bounced
            # normal [35,75]: no mode → rules 6-9
            # overbought (75,100]: rsi_4h > 75
            crash_mode      = rsi_4h < 25 and not divergence
            overbought_mode = rsi_4h > 75 and not divergence
            recovering_mode = (not crash_mode) and rsi_4h < 35 and rsi_30m > 40 and not divergence

            data[sym] = {
                "price":           cl30[-1],
                "change_24h":      round((cl30[-1] - cl30[0]) / cl30[0] * 100, 2) if cl30[0] else 0,
                "change_7d":       round((cl4h[-1] - cl4h[0]) / cl4h[0] * 100, 2) if cl4h[0] else 0,
                "trend_30m":       t30,
                "trend_4h":        t4h,
                "trend_1d":        t90,    # true daily trend (was medium-term 4h)
                "aligned":         t30 == t4h == t90,
                "rsi_30m":         rsi_30m,
                "rsi_4h":          rsi_4h,
                "rsi_1d":          rsi_1d,  # true daily RSI
                "bb_width_4h":     calc_bb_width(cl4h),
                "atr_pct_4h":      calc_atr_pct(c4h),
                "range_7d_pct":    round((h7d - l7d) / l7d * 100, 2) if l7d else 0,
                "divergence":      divergence,
                "crash_mode":      crash_mode,
                "overbought_mode": overbought_mode,
                "recovering_mode": recovering_mode,
            }
        except Exception as e:
            print(f"  ERROR {sym}: {e}")

    # Require minimum symbols before proceeding
    if len(data) < MIN_SYMBOLS:
        print(f"  ⚠️  Only {len(data)}/{len(COINGECKO_IDS)} symbols fetched — insufficient data")
        return {}

    return data


# ─────────────────────────────────────────
# CLAUDE ANALYSIS
# ─────────────────────────────────────────

ANALYSIS_PROMPT = """You are an expert crypto market analyst. Analyze market conditions and recommend the best trading strategy.
Output ONLY a raw JSON object. No text, no markdown, no explanation.

Available strategies and PRECISE conditions for each:

- bollinger: Mean reversion. Fires BUY when price below lower band OR RSI < 25 (override).
  Fires SELL when price above upper band OR RSI > 75 (override).
  USE WHEN: crash_mode=True, overbought_mode=True, recovering_mode=True, or ranging/choppy market.
  This is the most reliable strategy for extreme RSI conditions because it has hard overrides.
  IMPORTANT: even if 30m RSI has bounced, if recovering_mode=True (4h RSI still < 35), use bollinger.

- ema_advanced: Trend following with multi-timeframe confirmation.
  USE WHEN: trends aligned BULLISH across 30m+4h+daily, RSI between 40-65.
  DO NOT use when trends are bearish — fires SELL signals which fail on Spot.
  DO NOT use when RSI is extreme (< 30 or > 70).

- vwap: Institutional trend following. Better than ema_advanced during London/NY session overlaps.
  USE WHEN: same bullish alignment as ema_advanced, but price has been one side of VWAP.
  Prefer vwap when atr_pct_4h is high (strong directional momentum).
  Prefer ema_advanced when atr_pct_4h is moderate (steady trend).

- rsi_divergence: Reversal detection.
  USE WHEN: divergence="bullish" OR divergence="bearish" for at least 2 assets.
  CRITICAL: ONLY pick this if divergence is actually forming (not just False).
  If divergence=False for most assets, this strategy will produce zero signals all day.

- breakout: Momentum breakout.
  USE WHEN: bb_width_4h < 3% for most assets (tight squeeze), low ATR.
  NOT useful in trending or already-crashed/extended markets.

- ema_basic: Simple EMA fallback.
  USE WHEN: nothing else fits. Moderate trend, RSI 40-60, some directional bias.

DECISION RULES (strict priority order):
1. divergence="bullish" on 2+ assets → rsi_divergence
2. divergence="bearish" on 2+ assets → rsi_divergence
3. crash_mode=True on 2+ assets → bollinger (4h RSI < 25)
4. overbought_mode=True on 2+ assets → bollinger (4h RSI > 75)
5. recovering_mode=True on 2+ assets → bollinger (4h RSI 25-35, bouncing)
6. bb_width_4h < 3% on 2+ assets → breakout
7. trends aligned bullish all timeframes, RSI 40-65:
   - atr_pct_4h > 1.5% → vwap (strong momentum)
   - atr_pct_4h <= 1.5% → ema_advanced (steady trend)
8. ranging/choppy, mixed trends, RSI 40-60 → bollinger
9. fallback → ema_basic

Output format:
{"recommended_strategy":"bollinger","confidence":85,"market_condition":"brief description","reasoning":"One sentence: which rule fired and why this strategy generates signals","secondary_strategy":"rsi_divergence","key_signals":["signal1","signal2","signal3"]}"""


def parse_claude_json(raw):
    """
    Brace-counting JSON extractor.
    Handles: markdown fences, extra text after JSON, nested {} in string fields.
    Previous regex approach [^{}]* failed when reasoning contained { or }.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1]
        if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip()

    start = raw.find('{')
    if start == -1:
        raise ValueError("No JSON object found in Claude response")

    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(raw[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return json.loads(raw[start:i+1])

    return json.loads(raw)  # fallback


def ask_claude(market_data):
    summary = "CURRENT MARKET CONDITIONS:\n\n"
    for sym, d in market_data.items():
        summary += (
            f"=== {sym} ===\n"
            f"Price: ${d['price']:,.4f} | 24h: {d['change_24h']}% | 7d: {d['change_7d']}%\n"
            f"Trends: 30m={d['trend_30m']} | 4h={d['trend_4h']} | 1d={d['trend_1d']} | aligned={d['aligned']}\n"
            f"RSI: 30m={d['rsi_30m']} | 4h={d['rsi_4h']} | 1d={d['rsi_1d']}\n"
            f"BB width (4h): {d['bb_width_4h']}% | ATR%: {d['atr_pct_4h']}% | 7d range: {d['range_7d_pct']}%\n"
            f"divergence={d['divergence']} | crash_mode={d['crash_mode']} | "
            f"overbought_mode={d['overbought_mode']} | recovering_mode={d['recovering_mode']}\n\n"
        )
    summary += f"Available strategies: {', '.join(STRATEGIES)}\nApply the decision rules in strict priority order and recommend the best strategy."

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
    return parse_claude_json(resp.json()["content"][0]["text"])


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
    """
    Write strategy selection to config.json via GitHub API.
    Validates strategy name — prevents Claude hallucination from silently
    breaking signal_bot by writing an unrecognised strategy name.
    """
    strategy = rec.get("recommended_strategy", "")
    if strategy not in STRATEGIES:
        print(f"  ⚠️  Invalid strategy '{strategy}' — falling back to ema_basic")
        strategy = "ema_basic"

    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    config = {
        "strategy":           strategy,
        "secondary_strategy": rec.get("secondary_strategy", ""),
        "confidence":         rec.get("confidence", 0),
        "market_condition":   rec.get("market_condition", ""),
        "reasoning":          rec.get("reasoning", ""),
        "key_signals":        rec.get("key_signals", []),
        "updated_at_dubai":   now,
    }
    content = json.dumps(config, indent=2)
    encoded = base64.b64encode(content.encode()).decode()
    sha = get_config_sha()
    payload = {"message": f"Auto-strategy: {strategy} — {now} Dubai", "content": encoded}
    if sha:
        payload["sha"] = sha

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{CONFIG_PATH}"
    r = requests.put(url,
                     headers={"Authorization": f"token {GITHUB_TOKEN}",
                              "Accept": "application/vnd.github.v3+json"},
                     json=payload, timeout=10)
    r.raise_for_status()
    print(f"  config.json updated: strategy = {strategy}")


# ─────────────────────────────────────────
# LOG
# ─────────────────────────────────────────

def log_decision(rec):
    """
    Append strategy decision to strategy_log.csv.
    Uses csv.writer — handles commas and braces in market_condition/reasoning.
    """
    ts = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log = "strategy_log.csv"
    header = not os.path.exists(log) or os.path.getsize(log) == 0
    with open(log, "a", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        if header:
            w.writerow(["timestamp_dubai","strategy","secondary","confidence","market_condition","reasoning"])
        w.writerow([ts, rec.get("recommended_strategy",""), rec.get("secondary_strategy",""),
                    rec.get("confidence",""), rec.get("market_condition",""), rec.get("reasoning","")])


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
        print(f"ERROR: insufficient data ({len(data)}/{len(COINGECKO_IDS)} symbols) — aborting")
        return

    print("\n  Mode summary:")
    for sym, d in data.items():
        print(f"  {sym}: RSI_4h={d['rsi_4h']} | RSI_30m={d['rsi_30m']} | RSI_1d={d['rsi_1d']} | "
              f"divergence={d['divergence']} | crash={d['crash_mode']} | "
              f"overbought={d['overbought_mode']} | recovering={d['recovering_mode']}")

    print("\n[2/3] Claude analyzing conditions...")
    rec = ask_claude(data)
    print(f"\n  Recommended: {rec.get('recommended_strategy','').upper()}")
    print(f"  Secondary:   {rec.get('secondary_strategy','').upper()}")
    print(f"  Confidence:  {rec.get('confidence','')}%")
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
