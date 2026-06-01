"""
Autonomous Market Analyzer
Runs twice daily (8am and 8pm Dubai time)
Analyzes market conditions and auto-switches strategy in GitHub workflow
"""

import requests
import json
import os
import re
import base64
from datetime import datetime, timezone, timedelta

DUBAI_TZ          = timezone(timedelta(hours=4))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER      = "shahwaleed"
GITHUB_REPO       = "claude-signal-bot"
WORKFLOW_PATH     = ".github/workflows/signal_bot.yml"

COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple"}

STRATEGIES = ["bollinger", "ema_advanced", "vwap", "rsi_divergence", "breakout", "ema_basic"]


def get_ohlc(coin_id, days):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    r = requests.get(url, params={"vs_currency": "usd", "days": str(days)},
                     headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def ema(closes, p):
    k = 2/(p+1)
    e = sum(closes[:p])/p
    for c in closes[p:]: e = c*k + e*(1-k)
    return round(e, 4)


def rsi(closes, p=14):
    if len(closes) < p+1: return 50.0
    g = [max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    l = [max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    ag, al = sum(g[-p:])/p, sum(l[-p:])/p
    if al == 0: return 100.0
    if ag == 0: return 1.0
    return round(100-(100/(1+ag/al)), 2)


def bb_width(closes, p=20):
    import math
    if len(closes) < p: return 5.0
    w = closes[-p:]
    m = sum(w)/p
    std = math.sqrt(sum((x-m)**2 for x in w)/p)
    return round((m+2*std-(m-2*std))/m*100, 4) if m > 0 else 5.0


def atr_pct(candles, p=14):
    trs = [max(candles[i][2]-candles[i][3], abs(candles[i][2]-candles[i-1][4]),
               abs(candles[i][3]-candles[i-1][4])) for i in range(1,len(candles))]
    a = sum(trs[-p:])/min(len(trs),p) if trs else 0
    cur = candles[-1][4]
    return round(a/cur*100, 4) if cur > 0 else 2.0


def analyze_market():
    import time
    data = {}
    for sym, cid in COINGECKO_IDS.items():
        print(f"  Fetching {sym}...")
        try:
            c30m = get_ohlc(cid, 1);   time.sleep(2)
            c4h  = get_ohlc(cid, 7);   time.sleep(2)
            c1d  = get_ohlc(cid, 30);  time.sleep(2)
            cl30 = [c[4] for c in c30m]
            cl4h = [c[4] for c in c4h]
            cl1d = [c[4] for c in c1d]
            e9_30,  e21_30  = ema(cl30,9), ema(cl30,21)
            e9_4h,  e21_4h  = ema(cl4h,9), ema(cl4h,21)
            e9_1d,  e21_1d  = ema(cl1d,9), ema(cl1d,21)
            t30 = "bullish" if e9_30 > e21_30 else "bearish"
            t4h = "bullish" if e9_4h > e21_4h else "bearish"
            t1d = "bullish" if e9_1d > e21_1d else "bearish"
            h7d = max(c[2] for c in c4h)
            l7d = min(c[3] for c in c4h)
            data[sym] = {
                "price":        cl30[-1],
                "change_24h":   round((cl30[-1]-cl30[0])/cl30[0]*100, 2) if cl30[0] else 0,
                "change_7d":    round((cl4h[-1]-cl4h[0])/cl4h[0]*100, 2) if cl4h[0] else 0,
                "trend_30m":    t30, "trend_4h": t4h, "trend_1d": t1d,
                "aligned":      t30 == t4h == t1d,
                "rsi_30m":      rsi(cl30), "rsi_4h": rsi(cl4h), "rsi_1d": rsi(cl1d),
                "bb_width_4h":  bb_width(cl4h),
                "atr_pct_4h":   atr_pct(c4h),
                "range_7d_pct": round((h7d-l7d)/l7d*100, 2) if l7d else 0,
            }
        except Exception as e:
            print(f"  ERROR {sym}: {e}")
    return data


ANALYSIS_PROMPT = """You are an expert crypto market analyst. Analyze conditions and recommend the best trading strategy.
Output ONLY a raw JSON object.

Available strategies:
- bollinger: choppy/ranging, BB width < 4%, 7d range < 8%, trends NOT aligned
- ema_advanced: clear trend, trends aligned across timeframes, RSI 40-65
- vwap: strong institutional trend, expanding ATR, consistent direction
- rsi_divergence: extremes (RSI < 30 or > 70 on 4h/daily), exhausted trend
- breakout: consolidating, BB width < 3%, low ATR about to expand
- ema_basic: simple trending, use as fallback

Output: {"recommended_strategy":"bollinger","confidence":82,"market_condition":"choppy/ranging","reasoning":"Brief reason","secondary_strategy":"ema_advanced","key_signals":["signal1","signal2"]}"""


def ask_claude(market_data):
    summary = "MARKET CONDITIONS:\n\n"
    for sym, d in market_data.items():
        summary += (f"=== {sym} ===\nPrice: ${d['price']:,.4f} | 24h: {d['change_24h']}% | 7d: {d['change_7d']}%\n"
                    f"Trends: 30m={d['trend_30m']} 4h={d['trend_4h']} 1d={d['trend_1d']} aligned={d['aligned']}\n"
                    f"RSI: 30m={d['rsi_30m']} 4h={d['rsi_4h']} 1d={d['rsi_1d']}\n"
                    f"BB width 4h: {d['bb_width_4h']}% | ATR%: {d['atr_pct_4h']}% | 7d range: {d['range_7d_pct']}%\n\n")
    summary += f"Strategies: {', '.join(STRATEGIES)}\nRecommend the best strategy."
    resp = requests.post("https://api.anthropic.com/v1/messages",
                         headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                                  "anthropic-version": "2023-06-01"},
                         json={"model": "claude-sonnet-4-6", "max_tokens": 400,
                               "system": ANALYSIS_PROMPT,
                               "messages": [{"role": "user", "content": summary}]}, timeout=30)
    resp.raise_for_status()
    raw = resp.json()["content"][0]["text"].strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw.strip())


def get_workflow():
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{WORKFLOW_PATH}"
    r = requests.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}",
                                    "Accept": "application/vnd.github.v3+json"}, timeout=10)
    r.raise_for_status()
    data = r.json()
    return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]


def update_workflow(new_strategy, sha, reasoning):
    content, _ = get_workflow()
    updated = re.sub(
        r"STRATEGY: \$\{\{ github\.event\.inputs\.strategy \|\| '[^']+' \}\}",
        f"STRATEGY: ${{{{ github.event.inputs.strategy || '{new_strategy}' }}}}",
        content
    )
    if updated == content:
        print(f"  Already set to {new_strategy}")
        return False
    encoded = base64.b64encode(updated.encode()).decode()
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M Dubai")
    r = requests.put(
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{WORKFLOW_PATH}",
        headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"},
        json={"message": f"Auto-switch to {new_strategy} — {now}\nReason: {reasoning[:100]}",
              "content": encoded, "sha": sha}, timeout=10
    )
    r.raise_for_status()
    print(f"  Workflow updated to: {new_strategy}")
    return True


def log_decision(rec):
    ts = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log = "strategy_log.csv"
    header = not os.path.exists(log) or os.path.getsize(log) == 0
    with open(log, "a") as f:
        if header: f.write("timestamp_dubai,strategy,confidence,market_condition,reasoning\n")
        r = rec.get("reasoning","").replace('"',"'")
        f.write(f'{ts},{rec["recommended_strategy"]},{rec["confidence"]},{rec.get("market_condition","")},"{r}"\n')


def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*60}\nMarket Analyzer — {now} Dubai time\n{'='*60}")
    print("\n[1/3] Collecting market data...")
    data = analyze_market()
    if not data:
        print("ERROR: no data — aborting")
        return
    print("\n[2/3] Asking Claude for strategy recommendation...")
    rec = ask_claude(data)
    print(f"\n  Strategy: {rec['recommended_strategy'].upper()}")
    print(f"  Confidence: {rec['confidence']}%")
    print(f"  Market: {rec.get('market_condition','')}")
    print(f"  Reason: {rec.get('reasoning','')}")
    print(f"  Signals: {rec.get('key_signals',[])}")
    print("\n[3/3] Updating GitHub workflow...")
    try:
        _, sha = get_workflow()
        update_workflow(rec["recommended_strategy"], sha, rec.get("reasoning",""))
    except Exception as e:
        print(f"  ERROR updating workflow: {e}")
    log_decision(rec)
    print(f"\n{'='*60}\nAnalysis complete.\n{'='*60}\n")


if __name__ == "__main__":
    run()
