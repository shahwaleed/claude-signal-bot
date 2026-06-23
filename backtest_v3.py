"""
Full System Backtest v3 — claude-signal-bot
============================================
Uses the SAME methodology as backtest_bollinger.py (which showed +338%):
  - candles_up_to(ts): slice history at each bar, use last N candles for indicators
  - simulate_trade(): step through future candles checking TP/SL on high/low intrabar
  - consec_sl tracking: block re-entry after SL (per strategy)
  - MAX_HOLD_CANDLES = 48 (24hr max hold)
  - RSI on rolling short window (last 50 candles), not EWM on full history

Market analyzer runs every 12 x 30m bars (6 hours), picks strategy using
the same deterministic rules as market_analyzer.py.

Run from: /Users/Waleed-Macbook-Air/Documents/Python Scripts/
    python3 backtest_v3.py

Settings:
    Starting capital: $1,000
    Allocation per bot: 25% of portfolio value at signal time
    Fee: 0.1% per trade (entry + exit)
    Period: 2020-08-11 to 2026-04-30
"""

import csv, os, math, json
from datetime import datetime, timezone
from collections import defaultdict

DATA_DIR    = "data"
RESULTS_DIR = "results"
SYMBOLS     = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

STARTING_CAPITAL   = 1000.0
ALLOCATION_PER_BOT = 0.25      # 25% of current portfolio per bot
FEE_RATE           = 0.001     # 0.1% per side
STOP_LOSS          = 3.0       # % SL for all strategies
MAX_HOLD_CANDLES   = 48        # 24hr max hold
ANALYZER_INTERVAL  = 12        # bars between analyzer runs (12 x 30m = 6h)
LOOKBACK           = 50        # candles for indicator calculation

BACKTEST_START_MS  = 1597104000000   # 2020-08-11
BACKTEST_END_MS    = 1746057600000   # 2026-04-30

MIN_TS = 1_000_000_000_000
MAX_TS = 2_000_000_000_000
US_THRESHOLD = 1_000_000_000_000_000


# ── INDICATORS (short rolling window, matching production) ─────────────
def calc_rsi(closes, p=14):
    if len(closes) < p+1: return 50.0
    g = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    l = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[-p:])/p, sum(l[-p:])/p
    if ag == 0 and al == 0: return 50.0
    if al == 0: return 100.0
    if ag == 0: return 1.0
    return round(100-(100/(1+ag/al)), 2)

def calc_ema(closes, p):
    if len(closes) < p: return closes[-1] if closes else 0
    k = 2/(p+1); e = sum(closes[:p])/p
    for c in closes[p:]: e = c*k + e*(1-k)
    return round(e, 4)

def calc_bb(closes, p=20, s=2.0):
    if len(closes) < p: return None, None, None, 0.5
    w = closes[-p:]; m = sum(w)/p
    std = math.sqrt(sum((x-m)**2 for x in w)/p)
    upper = m+s*std; lower = m-s*std
    pb = (closes[-1]-lower)/(upper-lower) if (upper-lower) else 0.5
    return upper, m, lower, pb

def calc_atr_pct(candles, p=14):
    if len(candles) < 2: return 2.0
    trs = [max(candles[i][2]-candles[i][3],
               abs(candles[i][2]-candles[i-1][4]),
               abs(candles[i][3]-candles[i-1][4]))
           for i in range(1, len(candles))]
    a = sum(trs[-p:]) / min(len(trs), p) if trs else 0
    return round(a/candles[-1][4]*100, 4) if candles[-1][4] > 0 else 2.0

def calc_vwap(candles):
    ttw, tw = 0, 0
    for c in candles:
        tp = (c[2]+c[3]+c[4])/3
        w = max(c[2]-c[3], 0.0001)
        ttw += tp*w; tw += w
    return ttw/tw if tw > 0 else candles[-1][4]

def detect_divergence(candles_4h, lookback=20):
    if len(candles_4h) < 20: return False
    closes = [c[4] for c in candles_4h]
    rsi_series = [calc_rsi(closes[:i], 14) for i in range(15, len(closes))]
    if len(rsi_series) < lookback: return False
    cp, cr = closes[-1], rsi_series[-1]
    pp_lo = min(closes[-lookback:-1]); pr_lo = min(rsi_series[-lookback:-1])
    pp_hi = max(closes[-lookback:-1]); pr_hi = max(rsi_series[-lookback:-1])
    if cp < pp_lo and cr > pr_lo: return "bullish"
    if cp > pp_hi and cr < pr_hi: return "bearish"
    return False


# ── DATA LOADER ────────────────────────────────────────────────────────
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

def candles_up_to(candles, ts_ms):
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo+hi)//2
        if candles[mid][0] <= ts_ms: lo = mid+1
        else: hi = mid
    return candles[:lo]


# ── TRADE SIMULATOR ────────────────────────────────────────────────────
def simulate_trade(direction, entry, tp_pct, sl_pct, future_candles):
    if direction == "long":
        tp_price = entry * (1 + tp_pct/100) if tp_pct else None
        sl_price = entry * (1 - sl_pct/100)
    else:
        tp_price = entry * (1 - tp_pct/100) if tp_pct else None
        sl_price = entry * (1 + sl_pct/100)

    for i, c in enumerate(future_candles[:MAX_HOLD_CANDLES]):
        hi, lo = c[2], c[3]
        if direction == "long":
            if lo <= sl_price: return "sl", sl_price, i+1
            if tp_price and hi >= tp_price: return "tp", tp_price, i+1
        else:
            if hi >= sl_price: return "sl", sl_price, i+1
            if tp_price and lo <= tp_price: return "tp", tp_price, i+1

    idx = min(MAX_HOLD_CANDLES-1, len(future_candles)-1)
    exit_price = future_candles[idx][4] if future_candles else entry
    return "timeout", exit_price, min(MAX_HOLD_CANDLES, len(future_candles))


# ── CONSECUTIVE SL COUNTER ─────────────────────────────────────────────
def count_consec_sl(trades_by_sym, symbol, entry_ts, window_ms=6*3600*1000):
    count = 0
    for t in reversed(trades_by_sym.get(symbol, [])):
        if t["exit_ts"] > entry_ts: continue
        if entry_ts - t["exit_ts"] > window_ms: break
        if t["result"] == "sl": count += 1
        else: break
    return count


# ── MARKET ANALYZER ────────────────────────────────────────────────────
def run_analyzer(data, ts_ms):
    rsi4h_vals, atr_vals = [], []
    n_crash = n_ob = n_rec = n_al = n_bd = n_bd2 = 0

    for sym in SYMBOLS:
        c30 = candles_up_to(data[sym]["30m"], ts_ms)[-50:]
        c4h = candles_up_to(data[sym]["4h"],  ts_ms)[-50:]
        c1d = candles_up_to(data[sym]["1d"],  ts_ms)[-30:]
        if len(c30) < 22 or len(c4h) < 22 or len(c1d) < 22: continue

        cl30 = [c[4] for c in c30]; cl4h = [c[4] for c in c4h]; cl1d = [c[4] for c in c1d]
        r4h  = calc_rsi(cl4h, 14); r30m = calc_rsi(cl30[-12:], 7)
        t30  = calc_ema(cl30, 9) > calc_ema(cl30, 21)
        t4h  = calc_ema(cl4h, 9) > calc_ema(cl4h, 21)
        t1d  = calc_ema(cl1d, 9) > calc_ema(cl1d, 21)
        aligned = t30 and t4h and t1d
        div = detect_divergence(c4h)
        at  = calc_atr_pct(c4h[-15:])

        cm = r4h < 25 and div != "bullish"
        ob = r4h > 75 and div != "bearish"
        rc = not cm and r4h < 35 and r30m > 40

        if cm:  n_crash += 1
        if ob:  n_ob += 1
        if rc:  n_rec += 1
        if aligned and 40 <= r4h <= 70: n_al += 1
        if div == "bullish": n_bd += 1
        if div == "bearish": n_bd2 += 1
        rsi4h_vals.append(r4h); atr_vals.append(at)

    if not rsi4h_vals: return "bollinger"
    ar = sum(rsi4h_vals)/len(rsi4h_vals)
    aa = sum(atr_vals)/len(atr_vals)

    if n_bd >= 3 and ar < 50:   return "rsi_divergence"
    if n_bd2 >= 3 and ar > 50:  return "rsi_divergence"
    if n_crash >= 2:             return "bollinger"
    if n_ob >= 2:                return "bollinger"
    if n_rec >= 2:               return "bollinger"
    if n_al >= 3 and aa > 1.5:  return "vwap"
    if n_al >= 3 and aa <= 1.5: return "ema_advanced"
    if 35 <= ar <= 65:           return "bollinger"
    return "ema_basic"


# ── STRATEGY SIGNAL GENERATORS ─────────────────────────────────────────
def signal_bollinger(c30m_hist, c4h_hist, c1d_hist, sym, consec_sl):
    if len(c30m_hist) < 25: return None, 0
    if consec_sl >= 1: return None, 0

    cl = [c[4] for c in c30m_hist[-50:]]
    cl4h = [c[4] for c in c4h_hist[-42:]]
    cl1d = [c[4] for c in c1d_hist[-30:]]
    if len(cl4h) < 22 or len(cl1d) < 22: return None, 0

    r14 = calc_rsi(cl, 14)
    trend_4h = "bullish" if calc_ema(cl4h, 9) > calc_ema(cl4h, 21) else "bearish"
    trend_1d = "bullish" if calc_ema(cl1d, 9) > calc_ema(cl1d, 21) else "bearish"
    _, mid, _, pb = calc_bb(cl, 20, 2.0)
    price = cl[-1]

    if trend_1d == "bearish": return None, 0
    ch24 = (cl[-1]-cl[-48])/cl[-48]*100 if len(cl) >= 48 else 0
    if ch24 <= -8.0: return None, 0
    rth = 15 if sym == "XRPUSDT" else 20
    if r14 >= rth: return None, 0
    if trend_4h == "bearish": return None, 0
    if trend_4h == "bullish" and pb >= 0.5: return None, 0

    tp_min = 2.5 if sym == "XRPUSDT" else 1.5
    tp = min(5.0, max(tp_min, abs(price-mid)/price*100)) if mid else tp_min
    if tp < tp_min: return None, 0
    return "long", tp


def signal_rsi_divergence(c30m_hist, c4h_hist, c1d_hist, sym, consec_sl):
    if len(c30m_hist) < 30: return None, 2.5
    cl = [c[4] for c in c30m_hist[-60:]]
    r14 = calc_rsi(cl, 14)
    rsi_series = [calc_rsi(cl[:i], 14) for i in range(15, len(cl))]
    if len(rsi_series) < 10: return None, 2.5
    cp = cl[-1]; cr = rsi_series[-1]
    pp_lo = min(cl[-10:-1]); pr_lo = min(rsi_series[-10:-1])
    pp_hi = max(cl[-10:-1]); pr_hi = max(rsi_series[-10:-1])
    if cp < pp_lo and cr > pr_lo and r14 < 50: return "long", 2.5
    if cp > pp_hi and cr < pr_hi and r14 > 50: return "short", 2.5
    return None, 2.5


def signal_vwap(c30m_hist, c4h_hist, c1d_hist, sym, consec_sl):
    if len(c30m_hist) < 22: return None, 2.0
    cl = [c[4] for c in c30m_hist[-30:]]
    price = cl[-1]
    e9 = calc_ema(cl, 9); e21 = calc_ema(cl, 21)
    r14 = calc_rsi(cl, 14)
    vwap = calc_vwap(c30m_hist[-48:])
    pvv = (price-vwap)/vwap*100; es = (e9-e21)/e21*100
    if abs(pvv) < 0.3: return None, 2.0
    if pvv > 0 and e9 < e21: return None, 2.0
    if pvv < 0 and e9 > e21: return None, 2.0
    if pvv > 0 and r14 >= 65: return None, 2.0
    if pvv < 0 and r14 <= 35: return None, 2.0
    if pvv > 0 and es > 0: return "long", 2.0
    if pvv < 0 and es < 0: return "short", 2.0
    return None, 2.0


def signal_ema_advanced(c30m_hist, c4h_hist, c1d_hist, sym, consec_sl):
    if len(c30m_hist) < 22: return None, 1.5
    cl30 = [c[4] for c in c30m_hist[-30:]]
    cl4h = [c[4] for c in c4h_hist[-30:]] if len(c4h_hist) >= 30 else []
    if not cl4h: return None, 1.5
    r7 = calc_rsi(cl30, 7)
    e9_30 = calc_ema(cl30, 9); e21_30 = calc_ema(cl30, 21)
    e9_4h = calc_ema(cl4h, 9); e21_4h = calc_ema(cl4h, 21)
    if r7 < 25: return "long", 1.5
    if r7 > 75: return "short", 1.5
    t30 = e9_30 > e21_30; t4h = e9_4h > e21_4h
    if t30 != t4h: return None, 1.5
    return ("long" if t30 else "short"), 1.5


def signal_ema_basic(c30m_hist, c4h_hist, c1d_hist, sym, consec_sl):
    if len(c30m_hist) < 22: return None, 1.5
    cl = [c[4] for c in c30m_hist[-30:]]
    e9 = calc_ema(cl, 9); e21 = calc_ema(cl, 21); r7 = calc_rsi(cl, 7)
    if r7 < 25: return "long", 1.5
    if r7 > 75: return "short", 1.5
    if e9 > e21 and r7 < 65: return "long", 1.5
    if e9 < e21 and r7 > 35: return "short", 1.5
    return None, 1.5


STRATEGY_FNS = {
    "bollinger":      signal_bollinger,
    "rsi_divergence": signal_rsi_divergence,
    "vwap":           signal_vwap,
    "ema_advanced":   signal_ema_advanced,
    "ema_basic":      signal_ema_basic,
}
SAR_STRATEGIES = {"ema_advanced", "vwap"}
LONG_ONLY = {"bollinger"}


# ── BACKTEST ENGINE ────────────────────────────────────────────────────
def run():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    print("Loading data...")
    data = {}
    for sym in SYMBOLS:
        data[sym] = {}
        for tf in ["30m", "4h", "1d"]:
            path = os.path.join(DATA_DIR, f"{sym}_{tf}.csv")
            if not os.path.exists(path):
                print(f"  Missing: {path}"); return
            candles = [c for c in load_csv(path)
                       if BACKTEST_START_MS <= c[0] <= BACKTEST_END_MS]
            data[sym][tf] = candles
            print(f"  {sym} {tf}: {len(candles)} candles")

    timeline = data["BTCUSDT"]["30m"]
    print(f"\nTimeline: {len(timeline):,} bars")
    print(f"  {datetime.fromtimestamp(timeline[0][0]/1000,tz=timezone.utc).date()} to "
          f"{datetime.fromtimestamp(timeline[-1][0]/1000,tz=timezone.utc).date()}")

    portfolio = STARTING_CAPITAL
    positions = {sym: None for sym in SYMBOLS}
    trades_by_sym = {sym: [] for sym in SYMBOLS}
    current_strategy = "bollinger"
    last_analyzer_bar = -ANALYZER_INTERVAL
    all_trades = []
    equity_curve = []
    strategy_usage = defaultdict(int)

    print("\nSimulating...")

    for bar_idx, bar in enumerate(timeline):
        ts_ms = bar[0]

        if bar_idx - last_analyzer_bar >= ANALYZER_INTERVAL:
            current_strategy = run_analyzer(data, ts_ms)
            strategy_usage[current_strategy] += 1
            last_analyzer_bar = bar_idx

        for sym in SYMBOLS:
            pos = positions[sym]
            sym_c30 = data[sym]["30m"]

            sym_bars_up_to = candles_up_to(sym_c30, ts_ms)
            if not sym_bars_up_to: continue
            cur_bar = sym_bars_up_to[-1]
            if cur_bar[0] != ts_ms: continue

            price = cur_bar[4]; hi = cur_bar[2]; lo = cur_bar[3]

            # Check exits
            if pos is not None:
                exited = False; ep = price; er = ""
                if pos["dir"] == "long":
                    if lo <= pos["sl_price"]: ep = pos["sl_price"]; er = "sl"; exited = True
                    elif pos["tp_price"] and hi >= pos["tp_price"]: ep = pos["tp_price"]; er = "tp"; exited = True
                else:
                    if hi >= pos["sl_price"]: ep = pos["sl_price"]; er = "sl"; exited = True
                    elif pos["tp_price"] and lo <= pos["tp_price"]: ep = pos["tp_price"]; er = "tp"; exited = True

                if not exited and bar_idx - pos["entry_bar"] >= MAX_HOLD_CANDLES:
                    ep = price; er = "timeout"; exited = True

                if exited:
                    pnl_pct = (ep-pos["entry"])/pos["entry"]*100 if pos["dir"]=="long" else (pos["entry"]-ep)/pos["entry"]*100
                    pnl_usd = pos["size_usd"]*pnl_pct/100
                    fee = pos["size_usd"]*FEE_RATE
                    net = pnl_usd - fee
                    portfolio += net
                    trade = {"sym":sym,"dir":pos["dir"],"entry":pos["entry"],"exit":ep,
                             "entry_ts":pos["entry_ts"],"exit_ts":ts_ms,
                             "size_usd":pos["size_usd"],"pnl_usd":net,"pnl_pct":round(pnl_pct,4),
                             "result":er,"strategy":current_strategy}
                    all_trades.append(trade); trades_by_sym[sym].append(trade)
                    positions[sym] = None; pos = None

            if pos is not None and current_strategy not in SAR_STRATEGIES:
                continue

            c30m_hist = candles_up_to(data[sym]["30m"], ts_ms)
            c4h_hist  = candles_up_to(data[sym]["4h"],  ts_ms)
            c1d_hist  = candles_up_to(data[sym]["1d"],  ts_ms)
            if len(c30m_hist) < 25: continue

            consec = count_consec_sl(trades_by_sym, sym, ts_ms)
            direction, tp_pct = STRATEGY_FNS[current_strategy](c30m_hist, c4h_hist, c1d_hist, sym, consec)

            if direction is None: continue
            if current_strategy in LONG_ONLY and direction == "short": continue

            if pos is not None and pos["dir"] != direction:
                pnl_pct = (price-pos["entry"])/pos["entry"]*100 if pos["dir"]=="long" else (pos["entry"]-price)/pos["entry"]*100
                pnl_usd = pos["size_usd"]*pnl_pct/100
                fee = pos["size_usd"]*FEE_RATE
                net = pnl_usd - fee; portfolio += net
                trade = {"sym":sym,"dir":pos["dir"],"entry":pos["entry"],"exit":price,
                         "entry_ts":pos["entry_ts"],"exit_ts":ts_ms,
                         "size_usd":pos["size_usd"],"pnl_usd":net,"pnl_pct":round(pnl_pct,4),
                         "result":"sar","strategy":current_strategy}
                all_trades.append(trade); trades_by_sym[sym].append(trade)
                positions[sym] = None; pos = None

            if pos is not None: continue

            size_usd = portfolio * ALLOCATION_PER_BOT
            portfolio -= size_usd * FEE_RATE

            if direction == "long":
                tp_price = price*(1+tp_pct/100) if tp_pct else None
                sl_price = price*(1-STOP_LOSS/100)
            else:
                tp_price = price*(1-tp_pct/100) if tp_pct else None
                sl_price = price*(1+STOP_LOSS/100)

            positions[sym] = {"dir":direction,"entry":price,"entry_ts":ts_ms,
                              "entry_bar":bar_idx,"tp_price":tp_price,"sl_price":sl_price,
                              "size_usd":size_usd}

        if bar_idx % 48 == 0:
            unr = 0
            for sym in SYMBOLS:
                pos = positions[sym]
                if not pos: continue
                sb = candles_up_to(data[sym]["30m"], ts_ms)
                p = sb[-1][4] if sb else pos["entry"]
                unr += (p-pos["entry"])/pos["entry"]*pos["size_usd"] if pos["dir"]=="long" else (pos["entry"]-p)/pos["entry"]*pos["size_usd"]
            equity_curve.append({"dt":datetime.fromtimestamp(ts_ms/1000,tz=timezone.utc).strftime("%Y-%m-%d"),"eq":round(portfolio+unr,2)})

        if bar_idx % 20000 == 0 and bar_idx > 0:
            dt = datetime.fromtimestamp(ts_ms/1000,tz=timezone.utc)
            print(f"  {dt.strftime('%Y-%m-%d %H:%M')} bar={bar_idx:,} port=${portfolio:,.0f} trades={len(all_trades)} strat={current_strategy}")

    for sym in SYMBOLS:
        pos = positions[sym]
        if not pos: continue
        sb = data[sym]["30m"]
        price = sb[-1][4] if sb else pos["entry"]
        pnl_pct = (price-pos["entry"])/pos["entry"]*100 if pos["dir"]=="long" else (pos["entry"]-price)/pos["entry"]*100
        net = pos["size_usd"]*pnl_pct/100 - pos["size_usd"]*FEE_RATE
        portfolio += net
        all_trades.append({"sym":sym,"dir":pos["dir"],"entry":pos["entry"],"exit":price,
                           "entry_ts":pos["entry_ts"],"exit_ts":timeline[-1][0],
                           "size_usd":pos["size_usd"],"pnl_usd":net,"pnl_pct":round(pnl_pct,4),
                           "result":"end","strategy":current_strategy})

    return portfolio, all_trades, equity_curve, strategy_usage


# ── REPORT ─────────────────────────────────────────────────────────────
def report(fp, trades, equity, usage):
    n = len(trades)
    wins = [t for t in trades if t["result"]=="tp"]
    losses = [t for t in trades if t["result"]=="sl"]
    wr = len(wins)/n*100 if n else 0
    aw = sum(t["pnl_pct"] for t in wins)/len(wins) if wins else 0
    al = sum(t["pnl_pct"] for t in losses)/len(losses) if losses else 0
    gp = sum(t["pnl_usd"] for t in wins)
    gl = abs(sum(t["pnl_usd"] for t in losses))
    pf = gp/gl if gl > 0 else float("inf")

    peak = STARTING_CAPITAL; mdd = 0
    for p in equity:
        eq = p["eq"]
        if eq > peak: peak = eq
        mdd = max(mdd, (peak-eq)/peak*100)

    d0 = datetime.strptime(equity[0]["dt"],"%Y-%m-%d") if equity else None
    d1 = datetime.strptime(equity[-1]["dt"],"%Y-%m-%d") if equity else None
    years = (d1-d0).days/365.25 if d0 and d1 else 0
    cagr = ((fp/STARTING_CAPITAL)**(1/years)-1)*100 if years > 0 else 0
    ret = (fp-STARTING_CAPITAL)/STARTING_CAPITAL*100

    by_r = defaultdict(lambda:{"n":0,"pnl":0.0})
    for t in trades: by_r[t["result"]]["n"]+=1; by_r[t["result"]]["pnl"]+=t["pnl_usd"]

    by_s = defaultdict(lambda:{"n":0,"pnl":0.0,"w":0})
    for t in trades:
        by_s[t["sym"]]["n"]+=1; by_s[t["sym"]]["pnl"]+=t["pnl_usd"]
        if t["result"]=="tp": by_s[t["sym"]]["w"]+=1

    total_runs = sum(usage.values())

    lines = [
        "="*65,
        "FULL SYSTEM BACKTEST v3 — claude-signal-bot",
        "="*65,
        f"Period:           2020-08-11 to 2026-04-30 ({years:.1f} years)",
        f"Starting capital: ${STARTING_CAPITAL:,.2f}",
        f"Final portfolio:  ${fp:,.2f}",
        f"Total return:     {ret:+.1f}%",
        f"CAGR:             {cagr:+.1f}%/year",
        f"Max drawdown:     -{mdd:.1f}%",
        "",
        f"Total trades:     {n:,}",
        f"Win rate:         {wr:.1f}%",
        f"Avg win %:        {aw:+.3f}%",
        f"Avg loss %:       {al:+.3f}%",
        f"Profit factor:    {pf:.2f}",
        f"Gross profit:     ${gp:+,.2f}",
        f"Gross loss:       -${gl:,.2f}",
        "",
        "Exit reasons:",
    ]
    for r, d in sorted(by_r.items()):
        lines.append(f"  {r:8s}: {d['n']:5d} trades | net PnL: ${d['pnl']:+,.2f}")
    lines.append("\nPer-asset:")
    for sym, d in sorted(by_s.items()):
        w = d["w"]/d["n"]*100 if d["n"] else 0
        lines.append(f"  {sym}: {d['n']:5d} trades | TP WR: {w:.0f}% | PnL: ${d['pnl']:+,.2f}")
    lines.append("\nStrategy selection:")
    for s, c in sorted(usage.items(), key=lambda x:-x[1]):
        lines.append(f"  {s:20s}: {c/total_runs*100:.1f}% ({c:,} periods)")
    lines.append("="*65)

    report_str = "\n".join(lines)
    print("\n"+report_str)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR,"backtest_v3_report.txt"),"w") as f:
        f.write(report_str+"\n")

    results = {
        "summary":{"period":"2020-08-11 to 2026-04-30","years":round(years,2),
                   "starting":STARTING_CAPITAL,"final":round(fp,2),
                   "return_pct":round(ret,2),"cagr_pct":round(cagr,2),
                   "max_dd_pct":round(mdd,2),"trades":n,"win_rate":round(wr,2),
                   "avg_win_pct":round(aw,4),"avg_loss_pct":round(al,4),
                   "profit_factor":round(pf,3)},
        "exit_reasons":{k:{"count":v["n"],"pnl":round(v["pnl"],2)} for k,v in by_r.items()},
        "by_asset":{k:{"count":v["n"],"pnl":round(v["pnl"],2),"tp_win_rate":round(v["w"]/v["n"]*100,1) if v["n"] else 0} for k,v in by_s.items()},
        "strategy_pct":{k:round(v/total_runs*100,1) for k,v in usage.items()},
        "equity_curve":equity,
    }
    with open(os.path.join(RESULTS_DIR,"backtest_v3_results.json"),"w") as f:
        json.dump(results,f,indent=2)
    print(f"\nResults saved to {RESULTS_DIR}/backtest_v3_report.txt")
    print(f"Results saved to {RESULTS_DIR}/backtest_v3_results.json")


if __name__ == "__main__":
    fp, trades, equity, usage = run()
    report(fp, trades, equity, usage)
