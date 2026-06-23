"""
backtest_bollinger.py
Backtests strategy_bollinger.py signal logic on historical 30m + 4h candles.
No Claude API calls — pure rule-based signal replication.

Key things being tested:
  1. Overall P&L and win rate across 4 symbols (2024-2026)
  2. Falling-knife detection: how often does bollinger re-enter immediately
     after a stop-loss during a sustained downtrend?
  3. Per-regime performance: crash / recovering / ranging / overbought
  4. Does the RSI<25 override help or hurt overall?

Usage:
    python3 backtest_bollinger.py

Output:
    results/bollinger_trades.csv
    results/bollinger_report.txt
"""

import csv, os, math
from datetime import datetime, timezone
from collections import defaultdict

DATA_DIR    = "data"
RESULTS_DIR = "results"
SYMBOLS     = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

# Strategy parameters (must match live config)
STOP_LOSS      = 3.0   # %
MIN_CONFIDENCE = 65
TP_MIN, TP_MAX = 0.5, 5.0
MAX_HOLD_CANDLES = 48  # 24 hours max hold

MIN_TS = 1_000_000_000_000
MAX_TS = 2_000_000_000_000
US_THRESHOLD = 1_000_000_000_000_000

# ── Indicators ────────────────────────────────────────────────

def calc_rsi(closes, p=14):
    if len(closes)<p+1: return 50.0
    g=[max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    l=[max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    ag,al=sum(g[-p:])/p,sum(l[-p:])/p
    if ag==0 and al==0: return 50.0
    if al==0: return 100.0
    if ag==0: return 1.0
    return round(100-(100/(1+ag/al)),2)

def calc_ema(closes, p):
    if len(closes)<p: return closes[-1] if closes else 0
    k=2/(p+1); e=sum(closes[:p])/p
    for c in closes[p:]: e=c*k+e*(1-k)
    return round(e,4)

def calc_bb(closes, p=20, s=2.0):
    if len(closes)<p: return None,None,None,None,0.5
    w=closes[-p:]; m=sum(w)/p
    std=math.sqrt(sum((x-m)**2 for x in w)/p)
    upper=round(m+s*std,4); middle=round(m,4); lower=round(m-s*std,4)
    bw=round((upper-lower)/middle*100,4) if middle else 0
    pr=closes[-1]
    pb=round((pr-lower)/(upper-lower),4) if (upper-lower) else 0.5
    return upper,middle,lower,bw,pb

def clamp_tp(v):
    if v is None: return TP_MIN
    return round(max(TP_MIN,min(TP_MAX,float(v))),2)

# ── Signal generator (mirrors SYSTEM_PROMPT rules exactly) ───

def get_signal(closes, trend_4h="unknown", consec_sl=0, symbol="", daily_trend="unknown", change_24h=0.0):
    """
    Bollinger signal v9 — pair+direction optimised from 2-year backtest data.

    Profitable combinations (from backtest analysis):
      ETHUSDT  BUY:  +0.993% avg  → keep, all conditions
      ETHUSDT  SELL: +0.127% avg  → keep
      XRPUSDT  BUY:  +0.229% avg  → keep
      BTCUSDT  BUY:  +0.020% avg  → keep (marginal but positive)
      BTCUSDT  SELL: -0.001% avg  → DROP (near zero, not worth the noise)
      SOLUSDT  BUY:  -0.218% avg  → DROP
      SOLUSDT  SELL: -0.080% avg  → DROP
      XRPUSDT  SELL: -0.667% avg  → DROP (worst signal in dataset)

    Additional filters:
      - Circuit breaker at >=5 consecutive SLs
      - Daily regime filter: BUY blocked when 1d trend bearish
      - 4h trend filter: BUY blocked when 4h trend bearish
      - pb < 0.5 filter: BUY blocked if price above middle band in bull trend
      - Minimum TP 1.5% (1:2 risk:reward vs 3% SL)
    """
    rsi = calc_rsi(closes, 14)
    _, middle, _, _, pb = calc_bb(closes, 20, 2.0)
    price = closes[-1]
    if pb is None: pb = 0.5

    # Block ALL re-entries (consec>=1) — 8yr data shows they lose (-0.308% avg)
    # Capitulation re-entry edge only existed in 2024-2026, not across full history
    # Base entries (consec=0) are the edge: +0.463% avg over 8 years
    if consec_sl >= 1:
        return None, 0, 0, "reentry_blocked"

    # Daily regime filter — BUY blocked when 1d trend bearish
    if daily_trend == "bearish":
        return None, 0, 0, "daily_trend_bearish"

    # VOLATILITY CIRCUIT BREAKER — block during black swan events
    if change_24h <= -8.0:
        return None, 0, 0, "volatility_crash_blocked"

    # RSI thresholds
    rsi_buy_threshold  = 15 if symbol == "XRPUSDT" else 20
    rsi_sell_threshold = 80

    # SELL blocked for all pairs — loses across all 4 pairs over 8 years
    if rsi > rsi_sell_threshold:
        return None, 0, 0, "sell_blocked_all_pairs"

    # Base entry selectivity — RSI must be below buy threshold
    if consec_sl == 0:
        base_buy_thresh = 15 if symbol == "XRPUSDT" else 20
        if rsi >= base_buy_thresh:
            return None, 0, 0, "base_entry_rsi_not_extreme"

    # Determine signal
    if rsi < rsi_buy_threshold:
        if trend_4h == "bearish":
            return None, 0, 0, "rsi_override_blocked_bearish"
        if trend_4h == "bullish" and pb >= 0.5:
            return None, 0, 0, "rsi_override_blocked_above_mid"
        signal = "BUY"
        conf = 95 if rsi < 10 else 90 if rsi < 15 else 80
    elif rsi > rsi_sell_threshold:
        # Already blocked above — this branch never reached
        return None, 0, 0, "sell_blocked_all_pairs"
    else:
        return None, 0, 0, "no_signal"

    # v17: no pair+direction blocks — testing all combos on full 8-year data

    # Dynamic TP = distance to middle band
    if middle and price > 0:
        tp = round(abs(price - middle) / price * 100, 2)
        tp = clamp_tp(tp)
    else:
        tp = 1.5

    # Minimum TP — XRP requires 2.5% (data: TP<2.5% avg -0.591%), others 1.5%
    min_tp = 2.5 if symbol == "XRPUSDT" else 1.5
    if tp < min_tp:
        return None, 0, 0, "tp_too_small"

    return signal, conf, tp, "ok"

# ── Trade simulator ───────────────────────────────────────────

def simulate_trade(direction, entry, tp_pct, future_candles):
    """Simulate entry at close, check each future candle for TP/SL."""
    if direction=="BUY":
        tp_price=entry*(1+tp_pct/100)
        sl_price=entry*(1-STOP_LOSS/100)
    else:
        tp_price=entry*(1-tp_pct/100)
        sl_price=entry*(1+STOP_LOSS/100)

    for i,c in enumerate(future_candles[:MAX_HOLD_CANDLES]):
        high,low=c[2],c[3]
        if direction=="BUY":
            if low<=sl_price:  return "sl",  sl_price,  i+1
            if high>=tp_price: return "tp",  tp_price,  i+1
        else:
            if high>=sl_price: return "sl",  sl_price,  i+1
            if low<=tp_price:  return "tp",  tp_price,  i+1

    exit_price=future_candles[min(MAX_HOLD_CANDLES-1,len(future_candles)-1)][4] if future_candles else entry
    return "timeout", exit_price, min(MAX_HOLD_CANDLES, len(future_candles))

# ── Data loader ───────────────────────────────────────────────

def load_csv(path):
    rows=[]
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                ts=int(row["open_time_ms"])
                if ts>US_THRESHOLD: ts=ts//1000
                if not (MIN_TS<=ts<=MAX_TS): continue
                rows.append([ts,float(row["open"]),float(row["high"]),
                             float(row["low"]),float(row["close"]),float(row["volume"])])
            except: continue
    return rows

# ── Falling knife detector ────────────────────────────────────

def is_falling_knife(trades, symbol, entry_ts, lookback_hours=6):
    """
    Check if this entry follows a recent SL on the same symbol.
    Returns True if there was a stop-loss within lookback_hours before this entry.
    """
    lookback_ms = lookback_hours * 60 * 60 * 1000
    for t in reversed(trades):
        if t["symbol"] != symbol: continue
        if t["exit_ts"] > entry_ts: continue
        if entry_ts - t["exit_ts"] > lookback_ms: break
        if t["result"] == "sl":
            return True
    return False

def count_consecutive_sl(trades, symbol, entry_ts, window_hours=24):
    """Count consecutive SLs on same symbol before this entry."""
    window_ms = window_hours * 60 * 60 * 1000
    count = 0
    for t in reversed(trades):
        if t["symbol"] != symbol: continue
        if t["exit_ts"] > entry_ts: continue
        if entry_ts - t["exit_ts"] > window_ms: break
        if t["result"] == "sl":
            count += 1
        else:
            break  # stop counting at first non-SL
    return count

# ── Trend context ─────────────────────────────────────────────

def get_trend_context(candles_4h, ts_ms):
    """Get 4h EMA trend at time of signal."""
    hist = [c for c in candles_4h if c[0] <= ts_ms][-50:]
    if len(hist) < 22: return "unknown"
    closes = [c[4] for c in hist]
    e9 = calc_ema(closes, 9); e21 = calc_ema(closes, 21)
    return "bullish" if e9 > e21 else "bearish"

# ── Analyzer mode detector ───────────────────────────────────

def get_analyzer_mode(all_4h, ts_ms):
    """
    Compute what mode the market analyzer would have selected at ts_ms.
    Returns: "bollinger", "rsi_divergence", "trend", or "ema_basic"
    """
    asset_modes = {}
    for sym, candles_4h in all_4h.items():
        hist = [c for c in candles_4h if c[0] <= ts_ms][-50:]
        if len(hist) < 30: continue
        closes = [c[4] for c in hist]
        rsi_4h = calc_rsi(closes, 14)
        # 30m RSI: use 4h as proxy (we don't have per-symbol 30m here)
        rsi_30m = calc_rsi(closes[-12:], 14)  # last 12 4h = ~2 days
        div = False  # simplified — divergence detection is complex
        crash = rsi_4h < 25 and not div
        overbought = rsi_4h > 75 and not div
        recovering = (not crash) and rsi_4h < 35 and rsi_30m > 40 and not div
        asset_modes[sym] = {
            "rsi_4h": rsi_4h, "crash": crash,
            "overbought": overbought, "recovering": recovering,
        }

    if len(asset_modes) < 3: return "unknown"

    n_crash  = sum(1 for d in asset_modes.values() if d["crash"])
    n_over   = sum(1 for d in asset_modes.values() if d["overbought"])
    n_rec    = sum(1 for d in asset_modes.values() if d["recovering"])
    avg_rsi  = sum(d["rsi_4h"] for d in asset_modes.values()) / len(asset_modes)

    if n_crash >= 2:   return "bollinger"   # rule 3
    if n_over  >= 2:   return "bollinger"   # rule 4
    if n_rec   >= 2:   return "bollinger"   # rule 5
    if 35 <= avg_rsi <= 65: return "bollinger"  # rule 8 (ranging)
    return "other"  # trend/divergence/ema_basic


# ── Daily trend context ──────────────────────────────────────

def get_daily_trend(candles_1d, ts_ms):
    """
    Get 1d EMA trend at time of signal.
    Returns "bullish" if EMA9 > EMA21 on daily, "bearish" otherwise.
    Regime filter — only fire RSI override in bullish daily regime.
    In sustained bear markets (2018, 2022) RSI<25 = trend continuation not reversal.
    """
    hist = [c for c in candles_1d if c[0] <= ts_ms][-30:]
    if len(hist) < 22: return "unknown"
    closes = [c[4] for c in hist]
    e9 = calc_ema(closes, 9); e21 = calc_ema(closes, 21)
    return "bullish" if e9 > e21 else "bearish"


# ── Main backtest ─────────────────────────────────────────────

def run():
    os.makedirs(RESULTS_DIR, exist_ok=True)

    # Load data — full history 2017-2026 (8 year backtest)
    START_MS = 1502150400000  # 2017-08-08 (Binance launch)
    END_MS   = 1746057600000  # 2026-04-30

    print("Loading data...")
    data = {}
    for sym in SYMBOLS:
        path_30m = os.path.join(DATA_DIR, f"{sym}_30m.csv")
        path_4h  = os.path.join(DATA_DIR, f"{sym}_4h.csv")
        path_1d  = os.path.join(DATA_DIR, f"{sym}_1d.csv")
        if not os.path.exists(path_30m):
            print(f"  ⚠️  Missing {path_30m}"); return
        c30m = [c for c in load_csv(path_30m) if START_MS <= c[0] <= END_MS]
        c4h  = [c for c in load_csv(path_4h)]
        c1d  = load_csv(path_1d) if os.path.exists(path_1d) else []
        data[sym] = {"30m": c30m, "4h": c4h, "1d": c1d}
        print(f"  {sym}: {len(c30m)} 30m candles, {len(c4h)} 4h candles, {len(c1d)} 1d candles")

    # Load all 4 symbols' 4h data for analyzer mode detection
    all_4h = {sym: data[sym]["4h"] for sym in SYMBOLS}

    all_trades = []
    LOOKBACK = 50  # candles for indicator calculation

    # All 4 symbols — full reversal mode (BUY=long, SELL=short)
    BOLLINGER_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

    print("\nSimulating trades...")
    for sym in BOLLINGER_SYMBOLS:
        candles_30m = data[sym]["30m"]
        candles_4h  = data[sym]["4h"]
        n = 0

        for i in range(LOOKBACK, len(candles_30m)-1):
            candle   = candles_30m[i]
            ts_ms    = candle[0]
            closes   = [c[4] for c in candles_30m[i-LOOKBACK:i+1]]
            entry    = candle[4]  # enter at close of signal candle

            # FIX 1+3: pass trend context, daily regime, and consecutive SL count
            trend_ctx   = get_trend_context(candles_4h, ts_ms)
            daily_trend = get_daily_trend(data[sym]["1d"], ts_ms)
            consec      = count_consecutive_sl(all_trades, sym, ts_ms, window_hours=6)
            # 24hr price change — 48 candles of 30m = 24 hours
            change_24h  = (closes[-1] - closes[-48]) / closes[-48] * 100 if len(closes) >= 48 else 0.0
            sig, conf, tp, skip_reason = get_signal(closes, trend_ctx, consec, sym, daily_trend, change_24h)
            if sig is None: continue

            # Determine what the market analyzer would have selected
            analyzer_mode = get_analyzer_mode(all_4h, ts_ms)

            future = candles_30m[i+1:]
            result, exit_price, candles_held = simulate_trade(sig, entry, tp, future)

            exit_ts = future[candles_held-1][0] if candles_held <= len(future) else ts_ms
            pnl = (exit_price-entry)/entry*100 if sig=="BUY" else (entry-exit_price)/entry*100

            # Context
            trend_4h    = get_trend_context(candles_4h, ts_ms)
            fell_knife  = is_falling_knife(all_trades, sym, ts_ms, lookback_hours=6)
            consec_sl   = count_consecutive_sl(all_trades, sym, ts_ms, window_hours=24)
            rsi_at_sig  = calc_rsi(closes, 14)
            override    = rsi_at_sig < 25 or rsi_at_sig > 75

            dt = datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc)
            all_trades.append({
                "datetime_utc":   dt.strftime("%Y-%m-%d %H:%M"),
                "symbol":         sym,
                "direction":      sig,
                "confidence":     conf,
                "entry_price":    round(entry, 4),
                "exit_price":     round(exit_price, 4),
                "tp_pct":         tp,
                "sl_pct":         STOP_LOSS,
                "result":         result,
                "pnl_pct":        round(pnl, 4),
                "candles_held":   candles_held,
                "rsi_at_signal":  rsi_at_sig,
                "trend_4h":       trend_4h,
                "rsi_override":   override,
                "falling_knife":  fell_knife,
                "consec_sl_before": consec_sl,
                "entry_ts":       ts_ms,
                "exit_ts":        exit_ts,
                "analyzer_mode":  analyzer_mode,
                "skip_reason":    skip_reason,
                "daily_trend":    daily_trend,
            })
            n += 1

        print(f"  {sym}: {n} signals generated")

    # Save trades CSV
    trades_csv = os.path.join(RESULTS_DIR, "bollinger_trades.csv")
    if all_trades:
        with open(trades_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()))
            w.writeheader(); w.writerows(all_trades)
        print(f"\nSaved {len(all_trades)} trades → {trades_csv}")

    # ── Analysis ──────────────────────────────────────────────
    total  = len(all_trades)
    if total == 0:
        print("\n⚠️  No trades generated — filters too restrictive for this dataset")
        return
    wins   = sum(1 for t in all_trades if t["result"]=="tp")
    losses = sum(1 for t in all_trades if t["result"]=="sl")
    timeouts = sum(1 for t in all_trades if t["result"]=="timeout")
    total_pnl   = sum(t["pnl_pct"] for t in all_trades)
    avg_pnl     = total_pnl/total if total else 0
    win_rate    = wins/total*100 if total else 0

    lines = []
    lines.append("="*65)
    lines.append("BOLLINGER BACKTEST REPORT v18 (all pairs BUY only) — full history")
    lines.append("="*65)
    lines.append(f"Total trades:  {total}")
    lines.append(f"Win rate:      {win_rate:.1f}% ({wins} TP / {losses} SL / {timeouts} timeout)")
    lines.append(f"Avg P&L/trade: {avg_pnl:+.3f}%")
    lines.append(f"Total P&L:     {total_pnl:+.2f}%")
    lines.append("")

    # Per symbol
    lines.append("PER SYMBOL:")
    for sym in SYMBOLS:
        st = [t for t in all_trades if t["symbol"]==sym]
        if not st: continue
        sw = sum(1 for t in st if t["result"]=="tp")
        sp = sum(t["pnl_pct"] for t in st)
        lines.append(f"  {sym:<10} {len(st):>4} trades  WR={sw/len(st)*100:>5.1f}%  P&L={sp:>+8.2f}%  avg={sp/len(st):>+6.3f}%")

    # RSI override trades
    ov = [t for t in all_trades if t["rsi_override"]]
    ov_wins = sum(1 for t in ov if t["result"]=="tp")
    ov_pnl  = sum(t["pnl_pct"] for t in ov)
    lines.append("")
    lines.append(f"RSI OVERRIDE TRADES (RSI<25 or RSI>75): {len(ov)} trades")
    if ov:
        lines.append(f"  Win rate: {ov_wins/len(ov)*100:.1f}%  Total P&L: {ov_pnl:+.2f}%  Avg: {ov_pnl/len(ov):+.3f}%")

    # Non-override trades
    nov = [t for t in all_trades if not t["rsi_override"]]
    nov_wins = sum(1 for t in nov if t["result"]=="tp")
    nov_pnl  = sum(t["pnl_pct"] for t in nov)
    lines.append(f"BAND-TOUCH TRADES (no override): {len(nov)} trades")
    if nov:
        lines.append(f"  Win rate: {nov_wins/len(nov)*100:.1f}%  Total P&L: {nov_pnl:+.2f}%  Avg: {nov_pnl/len(nov):+.3f}%")

    lines.append("")
    lines.append("NOTES:")
    lines.append("  - Entry at 30m candle close, exit at first TP/SL candle touch")
    lines.append("  - No fees included in P&L% figures")
    lines.append("  - All 4 symbols traded simultaneously")
    lines.append("  - Timeout = 24hr max hold, exits at last candle close")
    lines.append("  - consec_sl >= 1 blocks re-entry (8yr data: re-entries lose -0.308% avg)")

    report = "\n".join(lines)
    print("\n" + report)
    report_path = os.path.join(RESULTS_DIR, "bollinger_report.txt")
    with open(report_path, "w") as f: f.write(report+"\n")
    print(f"\nReport saved → {report_path}")

if __name__ == "__main__":
    run()
