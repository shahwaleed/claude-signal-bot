"""
Strategy: RSI Divergence
Best for: Catching reversals at market turning points
Log file: trade_log_rsi_divergence.csv

Fixes applied:
  1. Flat market RSI returns 50.0 (neutral) not 100.0
  2. Strength uses round() not int() — reduces tier boundary artifacts
  3. Trend-agrees penalty (-10) for lower-quality same-direction signals
  4. tv_instrument uses slash format (SOL/USDT not SOLUSDT) for 3Commas
"""

import requests, json, re, csv, time, os
from datetime import datetime, timezone, timedelta

DUBAI_TZ = timezone(timedelta(hours=4))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
WEBHOOK_URL    = "https://api.3commas.io/signal_bots/webhooks"
WEBHOOK_SECRET = "eyJhbGciOiJIUzI1NiJ9.eyJzaWduYWxzX3NvdXJjZV9pZCI6MTMwNTYyfQ.DnbuKVB9cslOFa5l1WtrKH1PFsvacsV0Vfkh_e3E_DY"
BOT_UUIDS = {
    "BTCUSDT": "67d3e022-7414-4ef8-8b6c-3d5c56a09667",
    "ETHUSDT": "edac8b79-ac23-4af5-a6eb-666432a0cb57",
    "SOLUSDT": "3d72a934-50a2-4fd6-bbd2-0e678c841ef4",
    "XRPUSDT": "e798e648-fab5-4b94-82af-052228fa9ed1",
}
# 3Commas Signal Bots require slash format: "SOL/USDT" not "SOLUSDT"
TV_INSTRUMENTS = {
    "BTCUSDT": "BTC/USDT",
    "ETHUSDT": "ETH/USDT",
    "SOLUSDT": "SOL/USDT",
    "XRPUSDT": "XRP/USDT",
}
COINGECKO_IDS = {"BTCUSDT":"bitcoin","ETHUSDT":"ethereum","SOLUSDT":"solana","XRPUSDT":"ripple"}
SYMBOLS        = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]
TAKE_PROFIT    = 2.5
STOP_LOSS      = 3.0
MIN_CONFIDENCE = 68
LOG_FILE       = "trade_log_rsi_divergence.csv"


def get_candles(symbol, days=7):
    url=f"https://api.coingecko.com/api/v3/coins/{COINGECKO_IDS[symbol]}/ohlc"
    headers={"x-cg-demo-api-key":COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    r=requests.get(url,params={"vs_currency":"usd","days":str(days)},headers=headers,timeout=15)
    r.raise_for_status()
    return [{"time":datetime.fromtimestamp(c[0]/1000,tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M"),
             "open":float(c[1]),"high":float(c[2]),"low":float(c[3]),"close":float(c[4])} for c in r.json()]


def calculate_rsi_series(closes, period=14):
    vals=[]
    for i in range(period+1, len(closes)+1):
        w=closes[:i]
        g=[max(w[j]-w[j-1],0) for j in range(1,len(w))]
        l=[max(w[j-1]-w[j],0) for j in range(1,len(w))]
        ag,al=sum(g[-period:])/period,sum(l[-period:])/period
        if ag==0 and al==0: vals.append(50.0)
        elif al==0: vals.append(100.0)
        elif ag==0: vals.append(1.0)
        else: vals.append(round(100-(100/(1+ag/al)),2))
    return vals


def find_divergence(closes, rs, lb=10):
    if len(closes)<lb or len(rs)<lb:
        return {"type":"none","strength":0,"details":"insufficient data"}
    pw=closes[-lb:]; rw=rs[-lb:]
    cp,cr=pw[-1],rw[-1]
    plp=min(pw[:-1]); plr=min(rw[:-1])
    php=max(pw[:-1]); phr=max(rw[:-1])
    if cp<plp and cr>plr:
        pd=round((plp-cp)/plp*100,2); rd=round(cr-plr,2)
        return {"type":"bullish","strength":min(100,round(pd*10+rd*2)),"details":f"price -{pd}% RSI +{rd}pts"}
    if cp>php and cr<phr:
        pd=round((cp-php)/php*100,2); rd=round(phr-cr,2)
        return {"type":"bearish","strength":min(100,round(pd*10+rd*2)),"details":f"price +{pd}% RSI -{rd}pts"}
    return {"type":"none","strength":0,"details":"no divergence"}


def calc_ema(closes, p):
    k=2/(p+1); e=sum(closes[:p])/p
    for c in closes[p:]: e=c*k+e*(1-k)
    return round(e,4)


def get_indicators(candles):
    closes=[c["close"] for c in candles]
    rs=calculate_rsi_series(closes,14)
    div=find_divergence(closes,rs,10)
    e9,e21=calc_ema(closes,9),calc_ema(closes,21)
    return {"price":closes[-1],"rsi":rs[-1] if rs else 50.0,"divergence":div,
            "ema9":e9,"ema21":e21,"trend":"bullish" if e9>e21 else "bearish"}


def parse_claude_json(raw):
    raw=raw.strip()
    if raw.startswith("```"):
        raw=raw.split("```")[1]
        if raw.startswith("json"): raw=raw[4:]
        raw=raw.strip()
    m=re.search(r'\{[^{}]*\}',raw,re.DOTALL)
    if m: return json.loads(m.group())
    return json.loads(raw)


SYSTEM_PROMPT = """You are a trading signal engine using RSI Divergence strategy.
Output ONLY a raw JSON object. No text, no markdown.

This strategy detects REVERSALS — signals are highest quality when the EMA trend
OPPOSES the divergence direction (e.g. bearish trend + bullish divergence = likely bottom).

BUY:  divergence.type="bullish" AND strength>=20
SELL: divergence.type="bearish" AND strength>=20
HOLD: type="none" OR strength<20

CONFIDENCE (start 50):
+20 divergence detected
+10 strength 20-29 (weak divergence)
+20 strength 30-59 (moderate divergence)
+25 strength 60-100 (strong divergence)
+15 EMA trend OPPOSES signal direction — true reversal setup:
     bullish div + bearish EMA trend = oversold in downtrend (high quality)
     bearish div + bullish EMA trend = overbought in uptrend (high quality)
-10 EMA trend AGREES with signal direction — continuation, not reversal (lower quality):
     bullish div + bullish EMA trend = already going up
     bearish div + bearish EMA trend = already going down
+10 RSI extreme confirms (bullish div+RSI<35, bearish div+RSI>65)
Cap confidence at 100.

Output: {"signal":"BUY","confidence":85,"reasoning":"Bullish RSI divergence strength=45, bearish trend confirms reversal setup"}"""


def ask_claude(symbol, ind):
    div=ind["divergence"]
    msg=(f"Symbol:{symbol}\nPrice:{ind['price']}\nRSI:{ind['rsi']}\n"
         f"Div:{div['type']} Strength:{div['strength']}/100 {div['details']}\n"
         f"EMA9:{ind['ema9']} EMA21:{ind['ema21']} Trend:{ind['trend']}\nReturn JSON.")
    r=requests.post("https://api.anthropic.com/v1/messages",
                    headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01"},
                    json={"model":"claude-sonnet-4-6","max_tokens":200,"system":SYSTEM_PROMPT,
                          "messages":[{"role":"user","content":msg}]},timeout=30)
    r.raise_for_status()
    return parse_claude_json(r.json()["content"][0]["text"])


def fire_webhook(signal_str, price, symbol):
    action="enter_long" if signal_str=="BUY" else "enter_short"
    tp=TAKE_PROFIT if signal_str=="BUY" else -TAKE_PROFIT
    r=requests.post(WEBHOOK_URL,json={"secret":WEBHOOK_SECRET,"max_lag":"300",
               "timestamp":datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
               "trigger_price":str(price),"tv_exchange":"BINANCE",
               "tv_instrument":TV_INSTRUMENTS[symbol],"action":action,
               "bot_uuid":BOT_UUIDS[symbol],
               "take_profit":{"enabled":True,"steps":[{"order_type":"market","price_percent":tp,"volume_percent":100}]},
               "stop_loss":{"enabled":True,"order_type":"market","trigger_price_percent":STOP_LOSS}},timeout=10)
    print(f"  Webhook {action}: {'SUCCESS' if r.status_code==200 else f'FAILED [{r.status_code}] {r.text[:100]}'} (TP:{tp}%)")
    return r.status_code==200


def log_result(symbol, signal, ind, fired):
    ts=datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    div=ind["divergence"]
    header=not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE)==0
    with open(LOG_FILE,"a",newline="") as f:
        w=csv.writer(f,quoting=csv.QUOTE_MINIMAL)
        if header:
            w.writerow(["timestamp_dubai","symbol","price","signal","confidence",
                        "rsi","div_type","div_strength","ema9","ema21","trend","webhook_fired","reasoning"])
        w.writerow([ts,symbol,ind["price"],signal["signal"],signal["confidence"],
                    ind["rsi"],div["type"],div["strength"],ind["ema9"],ind["ema21"],
                    ind["trend"],fired,signal.get("reasoning","")])
    print(f"  [{ts}] {symbol} | {signal['signal']} | {signal['confidence']}% | Div:{div['type']}({div['strength']}) | Fired:{fired}")


def run():
    now=datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*56}\nRSI Divergence Strategy — {now} Dubai time\n{'='*56}")
    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            candles=get_candles(symbol,days=7); time.sleep(2)
            ind=get_indicators(candles); div=ind["divergence"]
            print(f"  Price:${ind['price']:,.4f} RSI:{ind['rsi']}")
            print(f"  Div:{div['type']} Strength:{div['strength']} {div['details']}")
            print(f"  Trend:{ind['trend']} EMA9={ind['ema9']} EMA21={ind['ema21']}")
            signal=ask_claude(symbol,ind)
            print(f"  Signal:{signal['signal']} Conf:{signal['confidence']}% | {signal.get('reasoning','')}")
            fired=False
            if signal["signal"] in ("BUY","SELL") and signal["confidence"]>=MIN_CONFIDENCE:
                fired=fire_webhook(signal["signal"],ind["price"],symbol)
            else:
                print("  HOLD — no webhook fired.")
            log_result(symbol,signal,ind,fired)
            time.sleep(3)
        except Exception as e:
            print(f"  ERROR: {e}")
    print(f"\n{'='*56}\nRun complete.\n{'='*56}\n")


if __name__ == "__main__":
    run()
