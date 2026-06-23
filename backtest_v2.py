"""
Full System Backtest v2 — claude-signal-bot
Run from: /Users/Waleed-Macbook-Air/Documents/Python Scripts/
Data in:  /Users/Waleed-Macbook-Air/Documents/Python Scripts/data/

Usage:
    cd "/Users/Waleed-Macbook-Air/Documents/Python Scripts"
    pip3 install pandas numpy
    python3 backtest_v2.py

Settings:
    Starting capital: $1,000
    Allocation per bot: 25% of portfolio
    Fee: 0.1% per trade (entry + exit)
    Period: 2020-08-11 to 2026-04-30

Fix v2: Only generate a new signal when no position is open.
    In production, a position stays open until 3Commas hits TP or SL.
    SAR (reverse) only applies to ema_advanced and vwap strategies.
"""

import pandas as pd
import numpy as np
import json
from datetime import datetime
from collections import defaultdict

STARTING_CAPITAL   = 1000.0
ALLOCATION_PER_BOT = 0.25
FEE_RATE           = 0.001
SYMBOLS            = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
BACKTEST_START     = "2020-08-11"
BACKTEST_END       = "2026-04-30"
ANALYZER_INTERVAL  = 12
DATA_DIR           = "data"

STRATEGY_CONFIG = {
    "bollinger":      {"sl": 3.0, "long_only": True},
    "rsi_divergence": {"sl": 3.0, "long_only": False},
    "vwap":           {"sl": 3.0, "long_only": False},
    "ema_advanced":   {"sl": 3.0, "long_only": False},
    "ema_basic":      {"sl": 3.0, "long_only": False},
}

SAR_STRATEGIES = {"ema_advanced", "vwap"}


# ── INDICATORS ────────────────────────────────────────────────────────
def ema(s, p):    return s.ewm(span=p, adjust=False).mean()
def rsi(s, p=14):
    d = s.diff(); g = d.clip(lower=0); l = (-d).clip(lower=0)
    ag = g.ewm(com=p-1, adjust=False).mean()
    al = l.ewm(com=p-1, adjust=False).mean()
    return (100 - 100/(1 + ag/al.replace(0,np.nan))).fillna(50.0)

def bb(s, p=20, n=2.0):
    m = s.rolling(p).mean(); sd = s.rolling(p).std()
    u, l2 = m+n*sd, m-n*sd
    pb = ((s-l2)/(u-l2)).fillna(0.5)
    return u, m, l2, pb

def atr_pct(df, p=14):
    h,l,c = df['high'],df['low'],df['close']
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.rolling(p).mean() / c * 100

def vwap(df):
    tp = (df['high']+df['low']+df['close'])/3
    w  = (df['high']-df['low']).clip(lower=1e-6)
    return (tp*w).rolling(48).sum() / w.rolling(48).sum()

def divergence(close, rsi_vals, lb=10):
    res = pd.Series(None, index=close.index, dtype=object)
    cv, rv = close.values, rsi_vals.values
    for i in range(lb, len(cv)):
        cw, rw = cv[i-lb:i+1], rv[i-lb:i+1]
        if cw[-1] < cw[:-1].min() and rw[-1] > rw[:-1].min(): res.iloc[i]='bullish'
        elif cw[-1] > cw[:-1].max() and rw[-1] < rw[:-1].max(): res.iloc[i]='bearish'
    return res


# ── LOAD & PRECOMPUTE ─────────────────────────────────────────────────
def load(sym):
    out = {}
    for tf in ['30m','4h','1d']:
        df = pd.read_csv(f"{DATA_DIR}/{sym}_{tf}.csv")
        df['dt'] = pd.to_datetime(df['open_time_ms'], unit='ms', utc=True)
        df = df.set_index('dt').sort_index()[['open','high','low','close','volume']].astype(float)
        out[tf] = df
    return out

def precompute(sym, dfs):
    c30 = dfs['30m']['close']
    c4h = dfs['4h']['close']
    c1d = dfs['1d']['close']
    r14_30 = rsi(c30,14); r7_30 = rsi(c30,7)
    r14_4h = rsi(c4h,14)
    _,bm,_,bpb = bb(c30,20,2.0)
    vw = vwap(dfs['30m'])
    ch24 = c30.pct_change(48)*100
    div_30 = divergence(c30, r14_30, lb=10)
    div_4h = divergence(c4h, r14_4h, lb=20)
    return {
        'e9_30':  ema(c30,9),  'e21_30': ema(c30,21),
        'e9_4h':  ema(c4h,9),  'e21_4h': ema(c4h,21),
        'e9_1d':  ema(c1d,9),  'e21_1d': ema(c1d,21),
        'r14_30': r14_30,       'r7_30':  r7_30,
        'r14_4h': r14_4h,
        'atr_4h': atr_pct(dfs['4h'],14),
        'bb_mid': bm,           'bb_pb':  bpb,
        'vwap':   vw,           'ch24':   ch24,
        'div_30': div_30,       'div_4h': div_4h,
        'close_30m': c30,
        'high_30m':  dfs['30m']['high'],
        'low_30m':   dfs['30m']['low'],
    }


# ── GET LATEST VALUE ──────────────────────────────────────────────────
def gv(series, ts):
    loc = series.index.searchsorted(ts, side='right') - 1
    return series.iloc[loc] if loc >= 0 else np.nan


# ── MARKET ANALYZER ───────────────────────────────────────────────────
def analyze(ts, ind_all):
    rsi4h, atrs = [], []
    n_crash=n_ob=n_rec=n_al=n_bd=n_bd2 = 0
    for sym in SYMBOLS:
        i = ind_all[sym]
        r4  = gv(i['r14_4h'], ts); r30 = gv(i['r7_30'],  ts)
        at  = gv(i['atr_4h'], ts); dv  = gv(i['div_4h'], ts)
        e9_30=gv(i['e9_30'],ts); e21_30=gv(i['e21_30'],ts)
        e9_4h=gv(i['e9_4h'],ts); e21_4h=gv(i['e21_4h'],ts)
        e9_1d=gv(i['e9_1d'],ts); e21_1d=gv(i['e21_1d'],ts)
        if any(pd.isna(v) for v in [r4,r30,at,e9_30,e21_30]): continue
        t30=e9_30>e21_30; t4h=e9_4h>e21_4h; t1d=e9_1d>e21_1d
        al = t30 and t4h and t1d
        cm = r4<25 and dv!='bullish'
        ob = r4>75 and dv!='bearish'
        rc = not cm and r4<35 and r30>40
        if cm: n_crash+=1
        if ob: n_ob+=1
        if rc: n_rec+=1
        if al and 40<=r4<=70: n_al+=1
        if dv=='bullish': n_bd+=1
        if dv=='bearish': n_bd2+=1
        rsi4h.append(r4); atrs.append(at)
    if not rsi4h: return "bollinger"
    ar,aa = np.mean(rsi4h), np.mean(atrs)
    if n_bd>=3 and ar<50:  return "rsi_divergence"
    if n_bd2>=3 and ar>50: return "rsi_divergence"
    if n_crash>=2:         return "bollinger"
    if n_ob>=2:            return "bollinger"
    if n_rec>=2:           return "bollinger"
    if n_al>=3 and aa>1.5: return "vwap"
    if n_al>=3 and aa<=1.5:return "ema_advanced"
    if 35<=ar<=65:         return "bollinger"
    return "ema_basic"


# ── SIGNAL ────────────────────────────────────────────────────────────
def signal(strategy, sym, ts, i):
    if strategy == "bollinger":
        r14=gv(i['r14_30'],ts); t1d=gv(i['e9_1d'],ts)>gv(i['e21_1d'],ts)
        t4h=gv(i['e9_4h'],ts)>gv(i['e21_4h'],ts)
        ch=gv(i['ch24'],ts); pb=gv(i['bb_pb'],ts)
        rth=15 if sym=="XRPUSDT" else 20
        tpmin=2.5 if sym=="XRPUSDT" else 1.5
        if not t1d: return "HOLD",None
        if not pd.isna(ch) and ch<=-8: return "HOLD",None
        if pd.isna(r14) or r14>=rth: return "HOLD",None
        if not t4h: return "HOLD",None
        if t4h and not pd.isna(pb) and pb>=0.5: return "HOLD",None
        return "BUY", tpmin

    elif strategy == "rsi_divergence":
        dv=gv(i['div_30'],ts); r14=gv(i['r14_30'],ts)
        if dv=='bullish' and not pd.isna(r14) and r14<50: return "BUY",2.5
        if dv=='bearish' and not pd.isna(r14) and r14>50: return "SELL",2.5
        return "HOLD",None

    elif strategy == "vwap":
        e9=gv(i['e9_30'],ts); e21=gv(i['e21_30'],ts)
        r14=gv(i['r14_30'],ts); vw=gv(i['vwap'],ts)
        if any(pd.isna(v) for v in [e9,e21,r14,vw]): return "HOLD",None
        pvv=(e9-vw)/vw*100; es=(e9-e21)/e21*100
        if abs(pvv)<0.3: return "HOLD",None
        if pvv>0 and e9<e21: return "HOLD",None
        if pvv<0 and e9>e21: return "HOLD",None
        if pvv>0 and r14>=65: return "HOLD",None
        if pvv<0 and r14<=35: return "HOLD",None
        if pvv>0 and es>0: return "BUY",2.0
        if pvv<0 and es<0: return "SELL",2.0
        return "HOLD",None

    elif strategy == "ema_advanced":
        e9_30=gv(i['e9_30'],ts); e21_30=gv(i['e21_30'],ts)
        r7=gv(i['r7_30'],ts)
        e9_4h=gv(i['e9_4h'],ts); e21_4h=gv(i['e21_4h'],ts)
        if any(pd.isna(v) for v in [e9_30,e21_30,r7,e9_4h,e21_4h]): return "HOLD",None
        if r7<25: return "BUY",1.5
        if r7>75: return "SELL",1.5
        t30=e9_30>e21_30; t4h=e9_4h>e21_4h
        if t30!=t4h: return "HOLD",None
        return ("BUY" if t30 else "SELL"),1.5

    elif strategy == "ema_basic":
        e9=gv(i['e9_30'],ts); e21=gv(i['e21_30'],ts); r7=gv(i['r7_30'],ts)
        if any(pd.isna(v) for v in [e9,e21,r7]): return "HOLD",None
        if r7<25: return "BUY",1.5
        if r7>75: return "SELL",1.5
        if e9>e21 and r7<65: return "BUY",1.5
        if e9<e21 and r7>35: return "SELL",1.5
        return "HOLD",None

    return "HOLD",None


# ── BACKTEST ──────────────────────────────────────────────────────────
def run():
    print("Loading data...")
    dfs_all = {sym: load(sym) for sym in SYMBOLS}
    print("Precomputing indicators...")
    ind_all = {sym: precompute(sym, dfs_all[sym]) for sym in SYMBOLS}

    tl = dfs_all["BTCUSDT"]["30m"].loc[BACKTEST_START:BACKTEST_END].index
    print(f"Timeline: {tl[0].date()} to {tl[-1].date()} ({len(tl):,} bars)\n")

    portfolio = STARTING_CAPITAL
    positions = {sym: None for sym in SYMBOLS}
    strategy  = "bollinger"
    last_a    = -ANALYZER_INTERVAL
    trades    = []
    equity    = []
    usage     = defaultdict(int)

    for i, ts in enumerate(tl):
        if i - last_a >= ANALYZER_INTERVAL:
            strategy = analyze(ts, ind_all); usage[strategy]+=1; last_a=i

        cfg = STRATEGY_CONFIG[strategy]

        for sym in SYMBOLS:
            ind = ind_all[sym]
            cs = ind['close_30m']
            if ts not in cs.index: continue
            price = cs.loc[ts]
            hi    = ind['high_30m'].loc[ts]
            lo    = ind['low_30m'].loc[ts]
            pos   = positions[sym]

            # Check exits
            if pos:
                exited=False; ep=price; er=""
                if pos['dir']=='long':
                    if pos['tp'] and hi>=pos['tp']: ep=pos['tp']; er="TP"; exited=True
                    elif lo<=pos['sl']:             ep=pos['sl']; er="SL"; exited=True
                else:
                    if pos['tp'] and lo<=pos['tp']: ep=pos['tp']; er="TP"; exited=True
                    elif hi>=pos['sl']:             ep=pos['sl']; er="SL"; exited=True
                if exited:
                    pnl = (ep-pos['e'])/pos['e']*pos['sz'] if pos['dir']=='long' else (pos['e']-ep)/pos['e']*pos['sz']
                    net = pnl - pos['sz']*FEE_RATE
                    portfolio+=net
                    trades.append({'sym':sym,'dir':pos['dir'],'e':pos['e'],'x':ep,
                                   'edt':str(pos['edt']),'xdt':str(ts),'pnl':net,'r':er,'s':strategy})
                    positions[sym]=None; pos=None

            # Only generate signal when no position is open.
            # In production, a position stays open until 3Commas hits TP or SL.
            # SAR (reverse) is only valid for ema_advanced and vwap strategies.
            if pos is not None and strategy not in SAR_STRATEGIES:
                continue  # already in a trade, wait for TP/SL exit

            sig, tp_pct = signal(strategy, sym, ts, ind)
            if cfg['long_only'] and sig=='SELL': sig='HOLD'

            if sig in ('BUY','SELL'):
                d = 'long' if sig=='BUY' else 'short'
                # SAR: close opposite position first (ema_advanced and vwap only)
                if pos and pos['dir']!=d and strategy in SAR_STRATEGIES:
                    pnl=(price-pos['e'])/pos['e']*pos['sz'] if pos['dir']=='long' else (pos['e']-price)/pos['e']*pos['sz']
                    net=pnl-pos['sz']*FEE_RATE
                    portfolio+=net
                    trades.append({'sym':sym,'dir':pos['dir'],'e':pos['e'],'x':price,
                                   'edt':str(pos['edt']),'xdt':str(ts),'pnl':net,'r':'SAR','s':strategy})
                    positions[sym]=None; pos=None
                if pos: continue
                sz=portfolio*ALLOCATION_PER_BOT; portfolio-=sz*FEE_RATE
                tp_p = price*(1+tp_pct/100) if tp_pct and d=='long' else price*(1-tp_pct/100) if tp_pct else None
                sl_p = price*(1-cfg['sl']/100) if d=='long' else price*(1+cfg['sl']/100)
                positions[sym]={'dir':d,'e':price,'edt':ts,'tp':tp_p,'sl':sl_p,'sz':sz}

        if i%48==0:
            unr=0
            for sym in SYMBOLS:
                pos=positions[sym]
                if not pos: continue
                p=ind_all[sym]['close_30m'].loc[ts] if ts in ind_all[sym]['close_30m'].index else pos['e']
                unr += (p-pos['e'])/pos['e']*pos['sz'] if pos['dir']=='long' else (pos['e']-p)/pos['e']*pos['sz']
            equity.append({'dt':str(ts)[:10],'eq':round(portfolio+unr,2)})

        if i%20000==0 and i>0:
            print(f"  {str(ts)[:16]} bar={i:,} port=${portfolio:,.0f} trades={len(trades)} strat={strategy}")

    # Close remaining
    for sym in SYMBOLS:
        pos=positions[sym]
        if not pos: continue
        price=ind_all[sym]['close_30m'].iloc[-1]
        pnl=(price-pos['e'])/pos['e']*pos['sz'] if pos['dir']=='long' else (pos['e']-price)/pos['e']*pos['sz']
        net=pnl-pos['sz']*FEE_RATE; portfolio+=net
        trades.append({'sym':sym,'dir':pos['dir'],'e':pos['e'],'x':price,
                       'edt':str(pos['edt']),'xdt':str(tl[-1]),'pnl':net,'r':'END','s':strategy})

    return portfolio, trades, equity, usage


def report(fp, trades, equity, usage):
    n=len(trades); w=[t for t in trades if t['pnl']>0]; l=[t for t in trades if t['pnl']<=0]
    wr=len(w)/n*100 if n else 0
    aw=np.mean([t['pnl'] for t in w]) if w else 0
    al=np.mean([t['pnl'] for t in l]) if l else 0
    gp=sum(t['pnl'] for t in w); gl=abs(sum(t['pnl'] for t in l))
    pf=gp/gl if gl>0 else float('inf')
    peak=STARTING_CAPITAL; mdd=0
    for p in equity:
        eq=p['eq']
        if eq>peak: peak=eq
        mdd=max(mdd,(peak-eq)/peak*100)
    d0=datetime.strptime(equity[0]['dt'],'%Y-%m-%d') if equity else None
    d1=datetime.strptime(equity[-1]['dt'],'%Y-%m-%d') if equity else None
    years=(d1-d0).days/365.25 if d0 and d1 else 0
    cagr=((fp/STARTING_CAPITAL)**(1/years)-1)*100 if years>0 else 0
    ret=(fp-STARTING_CAPITAL)/STARTING_CAPITAL*100

    by_r=defaultdict(lambda:{'n':0,'pnl':0.0})
    for t in trades:
        by_r[t['r']]['n']+=1; by_r[t['r']]['pnl']+=t['pnl']
    by_s=defaultdict(lambda:{'n':0,'pnl':0.0,'w':0})
    for t in trades:
        by_s[t['sym']]['n']+=1; by_s[t['sym']]['pnl']+=t['pnl']
        by_s[t['sym']]['w']+=(1 if t['pnl']>0 else 0)

    total_runs=sum(usage.values())

    print(f"\n{'='*60}")
    print("BACKTEST RESULTS — claude-signal-bot full system")
    print(f"{'='*60}")
    print(f"Period:           {BACKTEST_START} to {BACKTEST_END} ({years:.1f} years)")
    print(f"Starting capital: ${STARTING_CAPITAL:,.2f}")
    print(f"Final portfolio:  ${fp:,.2f}")
    print(f"Total return:     {ret:+.1f}%")
    print(f"CAGR:             {cagr:+.1f}%/year")
    print(f"Max drawdown:     -{mdd:.1f}%")
    print()
    print(f"Total trades:     {n:,}")
    print(f"Win rate:         {wr:.1f}%")
    print(f"Avg win:          ${aw:+.2f}")
    print(f"Avg loss:         ${al:+.2f}")
    print(f"Profit factor:    {pf:.2f}")
    print(f"Gross profit:     ${gp:+,.2f}")
    print(f"Gross loss:       -${gl:,.2f}")
    print()
    print("Exit reasons:")
    for r,d in sorted(by_r.items()):
        print(f"  {r:6s}: {d['n']:5d} trades | net PnL: ${d['pnl']:+,.2f}")
    print()
    print("Per-asset:")
    for sym,d in sorted(by_s.items()):
        print(f"  {sym}: {d['n']:5d} trades | WR: {d['w']/d['n']*100:.0f}% | PnL: ${d['pnl']:+,.2f}")
    print()
    print("Strategy selection:")
    for s,c in sorted(usage.items(),key=lambda x:-x[1]):
        print(f"  {s:20s}: {c/total_runs*100:.1f}% ({c:,} periods)")
    print()
    res={
        "summary":{"period":f"{BACKTEST_START} to {BACKTEST_END}","years":round(years,2),
                   "starting":STARTING_CAPITAL,"final":round(fp,2),"return_pct":round(ret,2),
                   "cagr_pct":round(cagr,2),"max_dd_pct":round(mdd,2),"trades":n,
                   "win_rate":round(wr,2),"avg_win":round(aw,4),"avg_loss":round(al,4),
                   "profit_factor":round(pf,3),"gross_profit":round(gp,2),"gross_loss":round(gl,2)},
        "exit_reasons":{k:{"count":v["n"],"pnl":round(v["pnl"],2)} for k,v in by_r.items()},
        "by_asset":{k:{"count":v["n"],"pnl":round(v["pnl"],2),"win_rate":round(v["w"]/v["n"]*100,1) if v["n"] else 0} for k,v in by_s.items()},
        "strategy_pct":{k:round(v/total_runs*100,1) for k,v in usage.items()},
        "equity_curve":equity,
    }
    with open("backtest_results.json","w") as f: json.dump(res,f,indent=2)
    print("Results saved to backtest_results.json")
    print("="*60)

if __name__=="__main__":
    fp,trades,equity,usage=run()
    report(fp,trades,equity,usage)
