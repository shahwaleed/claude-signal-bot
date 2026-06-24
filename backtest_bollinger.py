"""
backtest_bollinger.py
Backtests strategy_bollinger.py signal logic on historical 30m + 4h candles.
No Claude API calls — pure rule-based signal replication.

Key things being tested:
  1. Overall P&L and win rate across 4 symbols (2017-2026)
  2. Compounding portfolio — each trade uses 25% of current portfolio value
  3. Fees — 0.1% entry + 0.1% exit per trade
  4. Monthly P&L breakdown — net dollar return per month

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

STARTING_CAPITAL = 1000.0
ALLOCATION       = 0.25      # 25% of portfolio per bot
FEE_RATE         = 0.001     # 0.1% per side

STOP_LOSS        = 3.0
TP_MIN, TP_MAX   = 0.5, 5.0
MAX_HOLD_CANDLES = 48

MIN_TS       = 1_000_000_000_000
MAX_TS       = 2_000_000_000_000
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


# ── Signal generator ──────────────────────────────────────────

def get_signal(closes, trend_4h="unknown", consec_sl=0, symbol="", daily_trend="unknown", change_24h=0.0):
    rsi = calc_rsi(closes, 14)
    _, middle, _, _, pb = calc_bb(closes, 20, 2.0)
    price = closes[-1]
    if pb is None: pb = 0.5

    if consec_sl >= 1:
        return None, 0, 0, "reentry_blocked"
    if daily_trend == "bearish":
        return None, 0, 0, "daily_trend_bearish"
    if change_24h <= -8.0:
        return None, 0, 0, "volatility_crash_blocked"

    rsi_buy_threshold = 15 if symbol == "XRPUSDT" else 20

    if rsi > 80:
        return None, 0, 0, "sell_blocked_all_pairs"

    if rsi >= rsi_buy_threshold:
        return None, 0, 0, "base_entry_rsi_not_extreme"

    if trend_4h == "bearish":
        return None, 0, 0, "rsi_override_blocked_bearish"
    if trend_4h == "bullish" and pb >= 0.5:
        return None, 0, 0, "rsi_override_blocked_above_mid"

    signal = "BUY"
    conf = 95 if rsi < 10 else 90 if rsi < 15 else 80

    if middle and price > 0:
        tp = round(abs(price - middle) / price * 100, 2)
        tp = clamp_tp(tp)
    else:
        tp = 1.5

    min_tp = 2.5 if symbol == "XRPUSDT" else 1.5
    if tp < min_tp:
        return None, 0, 0, "tp_too_small"

    return signal, conf, tp, "ok"


# ── Trade simulator ───────────────────────────────────────────

def simulate_trade(direction, entry, tp_pct, future_candles):
    if direction == "BUY":
        tp_price = entry*(1+tp_pct/100)
        sl_price = entry*(1-STOP_LOSS/100)
    else:
        tp_price = entry*(1-tp_pct/100)
        sl_price = entry*(1+STOP_LOSS/100)

    for i, c in enumerate(future_candles[:MAX_HOLD_CANDLES]):
        hi, lo = c[2], c[3]
        if direction == "BUY":
            if lo <= sl_price:  return "sl",      sl_price,  i+1
            if hi >= tp_price:  return "tp",      tp_price,  i+1
        else:
            if hi >= sl_price:  return "sl",      sl_price,  i+1
            if lo <= tp_price:  return "tp",      tp_price,  i+1

    exit_price = future_candles[min(MAX_HOLD_CANDLES-1,len(future_candles)-1)][4] if future_candles else entry
    return "timeout", exit_price, min(MAX_HOLD_CANDLES, len(future_candles))


# ── Data loader ───────────────────────────────────────────────

def load_csv(path):
    rows = []
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                ts = int(row["open_time_ms"])
                if ts > US_THRESHOLD: ts = ts // 1000
                if not (MIN_TS <= ts <= MAX_TS): continue
                rows.append([ts, float(row["open"]), float(row["high"]),
                             float(row["low"]), float(row["close"]), float(row["volume"])])
            except: continue
    return rows


# ── Trend helpers ─────────────────────────────────────────────

def count_consecutive_sl(trades, symbol, entry_ts, window_hours=6):
    window_ms = window_hours * 60 * 60 * 1000
    count = 0
    for t in reversed(trades):
        if t["symbol"] != symbol: continue
        if t["exit_ts"] > entry_ts: continue
        if entry_ts - t["exit_ts"] > window_ms: break
        if t["result"] == "sl": count += 1
        else: break
    return count

def get_trend_context(candles_4h, ts_ms):
    hist = [c for c in candles_4h if c[0] <= ts_ms][-50:]
    if len(hist) < 22: return "unknown"
    closes = [c[4] for c in hist]
    return "bullish" if calc_ema(closes, 9) > calc_ema(closes, 21) else "bearish"

def get_daily_trend(candles_1d, ts_ms):
    hist = [c for c in candles_1d if c[0] <= ts_ms][-30:]
    if len(hist) < 22: return "unknown"
    closes = [c[4] for c in hist]
    return "bullish" if calc_ema(closes, 9) > calc_ema(closes, 21) else "bearish"


# ── Main backtest ─────────────────────────────────────────────

def run():
    os.makedirs(RESULTS_DIR, exist_ok=True)

    START_MS = 1502150400000  # 2017-08-08
    END_MS   = 1746057600000  # 2026-04-30

    print("Loading data...")
    data = {}
    for sym in SYMBOLS:
        path_30m = os.path.join(DATA_DIR, f"{sym}_30m.csv")
        path_4h  = os.path.join(DATA_DIR, f"{sym}_4h.csv")
        path_1d  = os.path.join(DATA_DIR, f"{sym}_1d.csv")
        if not os.path.exists(path_30m):
            print(f"  Missing {path_30m}"); return
        c30m = [c for c in load_csv(path_30m) if START_MS <= c[0] <= END_MS]
        c4h  = load_csv(path_4h)
        c1d  = load_csv(path_1d) if os.path.exists(path_1d) else []
        data[sym] = {"30m": c30m, "4h": c4h, "1d": c1d}
        print(f"  {sym}: {len(c30m)} 30m  {len(c4h)} 4h  {len(c1d)} 1d")

    raw_trades = []
    LOOKBACK = 50

    print("\nSimulating signals...")
    for sym in SYMBOLS:
        candles_30m = data[sym]["30m"]
        candles_4h  = data[sym]["4h"]
        n = 0
        for i in range(LOOKBACK, len(candles_30m)-1):
            candle    = candles_30m[i]
            ts_ms     = candle[0]
            closes    = [c[4] for c in candles_30m[i-LOOKBACK:i+1]]
            entry     = candle[4]
            trend_4h  = get_trend_context(candles_4h, ts_ms)
            daily     = get_daily_trend(data[sym]["1d"], ts_ms)
            consec    = count_consecutive_sl(raw_trades, sym, ts_ms)
            ch24      = (closes[-1]-closes[-48])/closes[-48]*100 if len(closes)>=48 else 0.0
            sig, conf, tp, skip = get_signal(closes, trend_4h, consec, sym, daily, ch24)
            if sig is None: continue

            future = candles_30m[i+1:]
            result, exit_price, held = simulate_trade(sig, entry, tp, future)
            exit_ts = future[held-1][0] if held <= len(future) else ts_ms
            pnl_pct = (exit_price-entry)/entry*100 if sig=="BUY" else (entry-exit_price)/entry*100

            raw_trades.append({
                "symbol": sym, "direction": sig,
                "entry": entry, "exit_price": exit_price,
                "tp_pct": tp, "result": result,
                "pnl_pct": round(pnl_pct, 4),
                "entry_ts": ts_ms, "exit_ts": exit_ts,
            })
            n += 1
        print(f"  {sym}: {n} trades")

    # ── COMPOUNDING PORTFOLIO SIMULATION ──────────────────────
    # Sort all trades by ENTRY time to simulate concurrent positions correctly.
    # At entry: deduct fee on allocated size.
    # At exit:  apply P&L and deduct exit fee.
    # Portfolio value is the running cash balance (unrealized P&L not tracked
    # between entry and exit — size is locked at entry-time portfolio value).

    print("\nSimulating compounding portfolio...")
    raw_trades.sort(key=lambda t: t["entry_ts"])

    portfolio = STARTING_CAPITAL
    open_positions = {}   # sym -> {size_usd, entry_ts, ...}
    completed = []        # final trades with dollar P&L

    # We need to process events in time order (both entries and exits)
    events = []
    for i, t in enumerate(raw_trades):
        events.append(("entry", t["entry_ts"], i, t))
        events.append(("exit",  t["exit_ts"],  i, t))
    events.sort(key=lambda e: (e[1], 0 if e[0]=="entry" else 1))

    for ev_type, ev_ts, idx, t in events:
        if ev_type == "entry":
            # Don't re-enter if already in a position for this symbol
            if t["symbol"] in open_positions:
                continue
            size_usd = portfolio * ALLOCATION
            entry_fee = size_usd * FEE_RATE
            portfolio -= entry_fee
            open_positions[t["symbol"]] = {
                "idx": idx, "size_usd": size_usd,
                "entry_ts": t["entry_ts"], "entry": t["entry"],
                "tp_pct": t["tp_pct"], "result": t["result"],
                "pnl_pct": t["pnl_pct"], "exit_ts": t["exit_ts"],
                "exit_price": t["exit_price"],
            }
        else:  # exit
            pos = open_positions.get(t["symbol"])
            if pos is None: continue
            # Make sure this exit matches the open position
            if pos["entry_ts"] != t["entry_ts"]: continue

            pnl_usd  = pos["size_usd"] * pos["pnl_pct"] / 100
            exit_fee = pos["size_usd"] * FEE_RATE
            net_usd  = pnl_usd - exit_fee
            portfolio += pos["size_usd"] + net_usd  # return capital + net profit

            dt_entry = datetime.fromtimestamp(pos["entry_ts"]/1000, tz=timezone.utc)
            dt_exit  = datetime.fromtimestamp(pos["exit_ts"]/1000,  tz=timezone.utc)
            completed.append({
                "symbol":        t["symbol"],
                "direction":     t["direction"],
                "entry_dt":      dt_entry.strftime("%Y-%m-%d %H:%M"),
                "exit_dt":       dt_exit.strftime("%Y-%m-%d %H:%M"),
                "entry_price":   round(pos["entry"], 4),
                "exit_price":    round(pos["exit_price"], 4),
                "tp_pct":        pos["tp_pct"],
                "result":        pos["result"],
                "pnl_pct":       pos["pnl_pct"],
                "size_usd":      round(pos["size_usd"], 4),
                "pnl_usd":       round(net_usd, 4),
                "portfolio_after": round(portfolio, 2),
                "exit_month":    dt_exit.strftime("%Y-%m"),
            })
            del open_positions[t["symbol"]]

    # Save detailed trades CSV
    trades_csv = os.path.join(RESULTS_DIR, "bollinger_trades.csv")
    if completed:
        with open(trades_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(completed[0].keys()))
            w.writeheader(); w.writerows(completed)

    # ── MONTHLY BREAKDOWN ──────────────────────────────────────
    monthly = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl_usd": 0.0, "start_portfolio": None, "end_portfolio": 0.0})
    for t in completed:
        m = t["exit_month"]
        monthly[m]["trades"] += 1
        if t["result"] == "tp": monthly[m]["wins"] += 1
        monthly[m]["pnl_usd"] += t["pnl_usd"]
        monthly[m]["end_portfolio"] = t["portfolio_after"]

    # Fill start portfolio for each month
    prev_end = STARTING_CAPITAL
    for m in sorted(monthly.keys()):
        monthly[m]["start_portfolio"] = prev_end
        prev_end = monthly[m]["end_portfolio"]

    # ── REPORT ────────────────────────────────────────────────
    total     = len(completed)
    wins      = sum(1 for t in completed if t["result"]=="tp")
    losses    = sum(1 for t in completed if t["result"]=="sl")
    timeouts  = sum(1 for t in completed if t["result"]=="timeout")
    win_rate  = wins/total*100 if total else 0
    total_pnl_usd  = sum(t["pnl_usd"] for t in completed)
    total_fees = sum(t["size_usd"]*FEE_RATE*2 for t in completed)
    final_portfolio = portfolio
    total_return = (final_portfolio - STARTING_CAPITAL) / STARTING_CAPITAL * 100

    # CAGR
    if completed:
        first_dt = datetime.strptime(completed[0]["entry_dt"],  "%Y-%m-%d %H:%M")
        last_dt  = datetime.strptime(completed[-1]["exit_dt"],  "%Y-%m-%d %H:%M")
        years    = (last_dt - first_dt).days / 365.25
        cagr     = ((final_portfolio/STARTING_CAPITAL)**(1/years)-1)*100 if years>0 else 0
    else:
        years = 0; cagr = 0

    # Max drawdown
    peak = STARTING_CAPITAL; mdd = 0.0
    running = STARTING_CAPITAL
    for t in completed:
        running = t["portfolio_after"]
        if running > peak: peak = running
        dd = (peak - running) / peak * 100
        if dd > mdd: mdd = dd

    # Monthly stats
    profitable_months = sum(1 for d in monthly.values() if d["pnl_usd"] > 0)
    avg_monthly_pnl   = sum(d["pnl_usd"] for d in monthly.values()) / len(monthly) if monthly else 0
    best_month  = max(monthly.items(), key=lambda x: x[1]["pnl_usd"]) if monthly else None
    worst_month = min(monthly.items(), key=lambda x: x[1]["pnl_usd"]) if monthly else None

    lines = []
    lines.append("="*65)
    lines.append("BOLLINGER BACKTEST — COMPOUNDING PORTFOLIO WITH FEES")
    lines.append("="*65)
    lines.append(f"Period:           {completed[0]['entry_dt'][:7]} → {completed[-1]['exit_dt'][:7]} ({years:.1f} years)")
    lines.append(f"Starting capital: ${STARTING_CAPITAL:,.2f}")
    lines.append(f"Final portfolio:  ${final_portfolio:,.2f}")
    lines.append(f"Total return:     {total_return:+.1f}%")
    lines.append(f"CAGR:             {cagr:+.1f}%/year")
    lines.append(f"Max drawdown:     -{mdd:.1f}%")
    lines.append(f"Total fees paid:  ${total_fees:,.2f}")
    lines.append("")
    lines.append(f"Total trades:     {total}")
    lines.append(f"Win rate:         {win_rate:.1f}%  ({wins} TP / {losses} SL / {timeouts} timeout)")
    lines.append(f"Net P&L:          ${total_pnl_usd:+,.2f}")
    lines.append(f"Avg per trade:    ${total_pnl_usd/total:+.2f}" if total else "")
    lines.append("")

    # Per symbol
    lines.append("PER SYMBOL:")
    for sym in SYMBOLS:
        st = [t for t in completed if t["symbol"]==sym]
        if not st: continue
        sw   = sum(1 for t in st if t["result"]=="tp")
        spnl = sum(t["pnl_usd"] for t in st)
        lines.append(f"  {sym:<10} {len(st):>4} trades  WR={sw/len(st)*100:>5.1f}%  Net=${spnl:>+8.2f}")

    lines.append("")
    lines.append(f"MONTHLY BREAKDOWN ({len(monthly)} months):")
    lines.append(f"  Profitable months: {profitable_months}/{len(monthly)} ({profitable_months/len(monthly)*100:.0f}%)")
    lines.append(f"  Avg monthly P&L:   ${avg_monthly_pnl:+.2f}")
    if best_month:
        lines.append(f"  Best month:        {best_month[0]}  ${best_month[1]['pnl_usd']:+.2f}")
    if worst_month:
        lines.append(f"  Worst month:       {worst_month[0]}  ${worst_month[1]['pnl_usd']:+.2f}")
    lines.append("")
    lines.append(f"  {'Month':<8} {'Trades':>6} {'WR':>6} {'P&L':>10} {'Portfolio':>12}")
    lines.append(f"  {'-'*8} {'-'*6} {'-'*6} {'-'*10} {'-'*12}")
    for m in sorted(monthly.keys()):
        d = monthly[m]
        wr = d["wins"]/d["trades"]*100 if d["trades"] else 0
        lines.append(f"  {m:<8} {d['trades']:>6} {wr:>5.0f}% {d['pnl_usd']:>+10.2f} {d['end_portfolio']:>12.2f}")

    lines.append("")
    lines.append("NOTES:")
    lines.append("  - Each trade uses 25% of portfolio value at entry time")
    lines.append("  - Fees: 0.1% entry + 0.1% exit per trade")
    lines.append("  - Entry at 30m candle close, TP/SL checked intrabar")
    lines.append("  - consec_sl >= 1 blocks re-entry on same symbol")
    lines.append("  - Max hold: 48 candles (24 hours)")

    report = "\n".join(lines)
    print("\n" + report)

    report_path = os.path.join(RESULTS_DIR, "bollinger_report.txt")
    with open(report_path, "w") as f: f.write(report+"\n")
    print(f"\nTrades saved → {trades_csv}")
    print(f"Report saved → {report_path}")

if __name__ == "__main__":
    run()
