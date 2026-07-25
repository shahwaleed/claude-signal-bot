"""
strategy_fib_cycle.py — BTC Fibonacci Cycle Swing Strategy
===========================================================
Completely isolated from the live bollinger system.
Runs via fib_cycle.yml — a separate GitHub Actions workflow.

Logic validated in backtest 2017-2026:
  BULL CYCLE: price > daily EMA200
  TREND CONFIRM: ADX >= 20 on 4h
  ENTRY: pullback to Fib 38.2/50/61.8% of last major swing (>20% move)
  EXIT: 1.618 Fibonacci extension target
  STOP: below Fib 78.6% level (swing invalidated)
  LEVERAGE: 2x in bull cycle via 3Commas bot
"""

import os, json, math, time
from datetime import datetime, timezone
import requests

WEBHOOK_URL    = os.environ.get("FIB_WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("FIB_WEBHOOK_SECRET", "")
BOT_UUID_BTC   = os.environ.get("FIB_BOT_UUID_BTC", "")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
COINGECKO_KEY  = os.environ.get("COINGECKO_API_KEY", "")
GITHUB_OWNER   = "shahwaleed"
GITHUB_REPO    = "claude-signal-bot"
STATE_FILE     = "fib_cycle_state.json"

ADX_MIN       = 20
SWING_THRESH  = 0.10
MIN_SWING_PCT = 0.20
RSI_MAX_ENTRY = 55
FIB_ENTRIES   = [(0.382, "38.2%"), (0.500, "50.0%"), (0.618, "61.8%")]
FIB_STOP      = 0.786
FIB_TARGET    = 0.618   # swing_high + 0.618 * range = 1.618 extension


# ── Indicators ────────────────────────────────────────────────────────
def ema(closes, p):
    if len(closes) < p: return closes[-1] if closes else 0
    k = 2 / (p + 1); e = sum(closes[:p]) / p
    for c in closes[p:]: e = c * k + e * (1 - k)
    return e

def rsi(closes, p=14):
    if len(closes) < p + 1: return 50.0
    g = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    l = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[-p:]) / p, sum(l[-p:]) / p
    if ag == 0 and al == 0: return 50.0
    if al == 0: return 100.0
    if ag == 0: return 1.0
    return 100 - (100 / (1 + ag / al))

def calc_adx(candles, p=14):
    if len(candles) < p * 2 + 2: return 0
    pdm, mdm, tr = [], [], []
    for i in range(1, len(candles)):
        up = candles[i]["high"] - candles[i-1]["high"]
        dn = candles[i-1]["low"] - candles[i]["low"]
        pdm.append(up if up > dn and up > 0 else 0)
        mdm.append(dn if dn > up and dn > 0 else 0)
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    def ws(v):
        s = [sum(v[:p])]
        for x in v[p:]: s.append(s[-1] - s[-1] / p + x)
        return s
    at, pd2, md2 = ws(tr), ws(pdm), ws(mdm)
    dx = []
    for a_, pp, mm in zip(at, pd2, md2):
        if a_ == 0: dx.append(0); continue
        pdi, mdi = 100 * pp / a_, 100 * mm / a_
        den = pdi + mdi
        dx.append(0 if den == 0 else 100 * abs(pdi - mdi) / den)
    if len(dx) < p: return 0
    av = sum(dx[:p]) / p
    for d in dx[p:]: av = (av * (p - 1) + d) / p
    return av

def detect_swings(candles, thresh=SWING_THRESH):
    if len(candles) < 10: return []
    swings = []; direction = None
    last_ext = candles[0]["close"]; last_ts = candles[0]["ts"]
    for c in candles[1:]:
        price = c["close"]
        if direction is None:
            if price > last_ext * (1 + thresh):
                direction = "up"; last_ext = price; last_ts = c["ts"]
            elif price < last_ext * (1 - thresh):
                direction = "down"; last_ext = price; last_ts = c["ts"]
        elif direction == "up":
            if price > last_ext: last_ext = price; last_ts = c["ts"]
            elif price < last_ext * (1 - thresh):
                swings.append({"ts": last_ts, "price": last_ext, "type": "high"})
                direction = "down"; last_ext = price; last_ts = c["ts"]
        else:
            if price < last_ext: last_ext = price; last_ts = c["ts"]
            elif price > last_ext * (1 + thresh):
                swings.append({"ts": last_ts, "price": last_ext, "type": "low"})
                direction = "up"; last_ext = price; last_ts = c["ts"]
    return swings


# ── Data fetch ────────────────────────────────────────────────────────
def fetch_binance(interval, limit):
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
                         timeout=15)
        r.raise_for_status()
        return [{"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                 "low": float(c[3]), "close": float(c[4])} for c in r.json()]
    except Exception as e:
        print(f"  Binance {interval} error: {e}"); return []

def fetch_coingecko_daily(days):
    try:
        h = {"x-cg-demo-api-key": COINGECKO_KEY} if COINGECKO_KEY else {}
        r = requests.get("https://api.coingecko.com/api/v3/coins/bitcoin/ohlc",
                         params={"vs_currency": "usd", "days": str(days)},
                         headers=h, timeout=15)
        r.raise_for_status()
        return [{"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                 "low": float(c[3]), "close": float(c[4])} for c in r.json()]
    except Exception as e:
        print(f"  CoinGecko error: {e}"); return []


# ── State persistence ─────────────────────────────────────────────────
def load_state():
    try:
        import base64
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{STATE_FILE}"
        r = requests.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return json.loads(base64.b64decode(d["content"]).decode()), d["sha"]
    except Exception as e:
        print(f"  State load error: {e}")
    return {"position": None, "last_run": None, "last_signal": None}, None

def save_state(state, sha=None):
    try:
        import base64
        content = base64.b64encode(json.dumps(state, indent=2).encode()).decode()
        payload = {"message": f"Fib state: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
                   "content": content}
        if sha: payload["sha"] = sha
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{STATE_FILE}"
        r = requests.put(url, headers={"Authorization": f"token {GITHUB_TOKEN}",
                                        "Accept": "application/vnd.github.v3+json"},
                         json=payload, timeout=10)
        r.raise_for_status()
        print(f"  State saved (sha={r.json()['content']['sha'][:8]})")
    except Exception as e:
        print(f"  State save error: {e}")


# ── Signal dispatch ───────────────────────────────────────────────────
def send_signal(action, price, tp_pct, sl_pct):
    if not WEBHOOK_URL:
        print(f"  [DRY RUN] {action} @ ${price:,.0f}  TP={tp_pct:.1f}%  SL={sl_pct:.1f}%")
        return True
    payload = {
        "secret": WEBHOOK_SECRET, "max_lag": 300,
        "timestamp": int(time.time()), "trigger_price": str(price),
        "tv_exchange": "BINANCE", "tv_instrument": "BTCUSDT",
        "action": action, "bot_uuid": BOT_UUID_BTC,
        "take_profit": {"enabled": True, "percentage": round(tp_pct, 2)},
        "stop_loss":   {"enabled": True, "percentage": round(sl_pct, 2)},
    }
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print(f"  Signal sent: {action} → HTTP {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"  Signal error: {e}"); return False


# ── Main ──────────────────────────────────────────────────────────────
def run():
    now = datetime.now(tz=timezone.utc)
    print(f"\n{'='*62}")
    print(f"Fib Cycle Strategy  —  {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*62}")

    state, sha = load_state()
    pos = state.get("position")
    print(f"  Position: {'OPEN @ ${:,.0f}'.format(pos['entry']) if pos else 'FLAT (in cash)'}")

    print("\n[1] Fetching live BTC data...")
    c1d = fetch_binance("1d", 250)
    if len(c1d) < 201:
        print("  Binance daily failed — trying CoinGecko...")
        c1d = fetch_coingecko_daily(90)
    c4h = fetch_binance("4h", 150)
    if len(c4h) < 50: c4h = c1d   # fallback to daily for indicators

    if len(c1d) < 201:
        print("  FATAL: insufficient data"); return

    price = c1d[-1]["close"]
    d_closes = [c["close"] for c in c1d]
    e200 = ema(d_closes, 200)
    e50  = ema(d_closes, 50)
    d_rsi = rsi(d_closes, 14)
    adx_v = calc_adx(c4h[-50:], 14) if len(c4h) >= 35 else 0
    rsi_4h = rsi([c["close"] for c in c4h[-30:]], 14) if len(c4h) >= 15 else d_rsi

    is_bull  = price > e200
    trend_ok = adx_v >= ADX_MIN

    # Swing detection
    candles_for_swing = c4h if len(c4h) > 100 else c1d
    swings = detect_swings(candles_for_swing, thresh=SWING_THRESH)

    # Find most recent valid low→high pair (>20% swing)
    swing_low = swing_high = None
    for i in range(len(swings) - 1, 0, -1):
        s1, s2 = swings[i-1], swings[i]
        if s1["type"] == "low" and s2["type"] == "high":
            pct = (s2["price"] - s1["price"]) / s1["price"]
            if pct >= MIN_SWING_PCT and s2["ts"] < c1d[-1]["ts"]:
                swing_low, swing_high = s1, s2; break

    # Fibonacci levels
    fibs = {}
    if swing_low and swing_high:
        rng = swing_high["price"] - swing_low["price"]
        fibs = {
            "low":    swing_low["price"],
            "high":   swing_high["price"],
            "f382":   swing_high["price"] - 0.382 * rng,
            "f500":   swing_high["price"] - 0.500 * rng,
            "f618":   swing_high["price"] - 0.618 * rng,
            "f786":   swing_high["price"] - 0.786 * rng,
            "target": swing_high["price"] + FIB_TARGET * rng,
        }

    print(f"\n[2] Analysis:")
    print(f"  Price:   ${price:,.0f}   EMA200: ${e200:,.0f}   Gap: ${abs(price-e200):,.0f} {'above' if price>e200 else 'below'}")
    print(f"  Cycle:   {'🟢 BULL' if is_bull else '🔴 BEAR (cash mode)'}")
    print(f"  ADX 4h:  {adx_v:.1f}  {'✅' if trend_ok else '❌ < 20'}")
    print(f"  RSI 4h:  {rsi_4h:.1f}  {'✅' if rsi_4h <= RSI_MAX_ENTRY else '❌ > 55'}")
    if fibs:
        sl_dt = datetime.fromtimestamp(swing_low["ts"]/1000, tz=timezone.utc).strftime('%Y-%m-%d')
        sh_dt = datetime.fromtimestamp(swing_high["ts"]/1000, tz=timezone.utc).strftime('%Y-%m-%d')
        pct_swing = (fibs['high'] - fibs['low']) / fibs['low'] * 100
        print(f"  Swing:   ${fibs['low']:,.0f} ({sl_dt}) → ${fibs['high']:,.0f} ({sh_dt}) +{pct_swing:.0f}%")
        print(f"  Fibs:    38.2%=${fibs['f382']:,.0f}  50%=${fibs['f500']:,.0f}  61.8%=${fibs['f618']:,.0f}")
        print(f"  Stop:    ${fibs['f786']:,.0f} (78.6%)")
        print(f"  Target:  ${fibs['target']:,.0f} (1.618 ext)")

    print(f"\n[3] Signal decision:")
    signal_fired = None

    # ── EXIT ──────────────────────────────────────────────────────────
    if pos:
        hit_tp    = price >= pos["target"]
        hit_stop  = price <= pos["stop"]
        bear_exit = not is_bull

        if hit_tp:
            pnl = (price - pos["entry"]) / pos["entry"] * 100
            print(f"  ✅ TARGET REACHED  entry=${pos['entry']:,.0f} → ${price:,.0f} (+{pnl:.1f}%)")
            send_signal("sell", price, 0, 0)
            signal_fired = "sell_tp"
        elif hit_stop:
            pnl = (price - pos["entry"]) / pos["entry"] * 100
            print(f"  ❌ STOP HIT  entry=${pos['entry']:,.0f} → ${price:,.0f} ({pnl:.1f}%)")
            send_signal("sell", price, 0, 0)
            signal_fired = "sell_stop"
        elif bear_exit:
            pnl = (price - pos["entry"]) / pos["entry"] * 100
            print(f"  ⚠️  BEAR CYCLE EXIT  entry=${pos['entry']:,.0f} → ${price:,.0f} ({pnl:.1f}%)")
            send_signal("sell", price, 0, 0)
            signal_fired = "sell_cycle"
        else:
            pnl = (price - pos["entry"]) / pos["entry"] * 100
            dist_to_tp   = (pos["target"] - price) / price * 100
            dist_to_stop = (price - pos["stop"]) / price * 100
            print(f"  📊 Holding:  P&L={pnl:+.1f}%  | to TP: +{dist_to_tp:.1f}% | to stop: -{dist_to_stop:.1f}%")

        if signal_fired:
            state["position"] = None
            state["last_signal"] = {"action": signal_fired, "price": price, "ts": now.isoformat()}

    # ── ENTRY ──────────────────────────────────────────────────────────
    elif not pos:
        if not is_bull:
            gap = e200 - price
            print(f"  🔴 BEAR CYCLE — in cash. Need +${gap:,.0f} to reclaim EMA200.")
        elif not trend_ok:
            print(f"  ⏳ Bull cycle confirmed but ADX {adx_v:.1f} < {ADX_MIN}. Waiting for trend.")
        elif not fibs:
            print(f"  ⏳ No valid swing structure. Waiting for a major swing to form.")
        elif rsi_4h > RSI_MAX_ENTRY:
            print(f"  ⏳ RSI {rsi_4h:.1f} too high. Waiting for pullback.")
        else:
            # Check Fib entry levels
            triggered = None
            for level, name in FIB_ENTRIES:
                fib_p = fibs["low"] + (1 - level) * (fibs["high"] - fibs["low"])
                # Price within ±1% of Fib level and price < swing high
                if (price <= fib_p * 1.010 and
                        price >= fib_p * 0.988 and
                        price < fibs["high"]):
                    triggered = (level, name, fib_p); break

            if triggered:
                level, name, entry_p = triggered
                stop_p   = fibs["f786"] * 0.995
                target_p = fibs["target"]
                sl_pct   = abs(entry_p - stop_p) / entry_p * 100
                tp_pct   = abs(target_p - entry_p) / entry_p * 100

                print(f"  🎯 ENTRY SIGNAL — Fib {name}")
                print(f"     Entry:  ${entry_p:,.0f}")
                print(f"     Stop:   ${stop_p:,.0f} (-{sl_pct:.1f}%)")
                print(f"     Target: ${target_p:,.0f} (+{tp_pct:.1f}%)")
                print(f"     R:R:    {tp_pct/sl_pct:.2f}:1")
                send_signal("buy", entry_p, tp_pct, sl_pct)
                state["position"] = {
                    "entry": entry_p, "stop": stop_p, "target": target_p,
                    "fib": name, "entry_dt": now.isoformat()
                }
                state["last_signal"] = {"action": "buy", "price": entry_p,
                                         "fib": name, "ts": now.isoformat()}
                signal_fired = "buy"
            else:
                # Show distances to each entry level
                print(f"  ⏳ Watching for Fib pullback entry:")
                for level, name in FIB_ENTRIES:
                    fib_p = fibs["low"] + (1 - level) * (fibs["high"] - fibs["low"])
                    dist = (price - fib_p) / fib_p * 100
                    marker = " ← nearest" if level == 0.382 else ""
                    print(f"     {name}: ${fib_p:,.0f}  (price is {dist:+.1f}% away){marker}")

    state["last_run"] = now.isoformat()
    save_state(state, sha)
    print(f"\n{'='*62}\n")


if __name__ == "__main__":
    run()
