"""
Strategy: Bollinger Band Mean Reversion — v18
Backtested: 868 trades across 8 years (Jun 2017 – Apr 2026)
Result:     +0.389% avg/trade, 45.2% WR, +338% total, ~42% annual return on allocated capital

Fix: tv_instrument uses raw symbol format (BTCUSDT not BTC/USDT) for Spot Binance webhooks
"""

import requests, json, re, csv, time, os, math
from datetime import datetime, timezone, timedelta

DUBAI_TZ          = timezone(timedelta(hours=4))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
WEBHOOK_URL       = "https://api.3commas.io/signal_bots/webhooks"
WEBHOOK_SECRET    = "eyJhbGciOiJIUzI1NiJ9.eyJzaWduYWxzX3NvdXJjZV9pZCI6MTMwNTYyfQ.DnbuKVB9cslOFa5l1WtrKH1PFsvacsV0Vfkh_e3E_DY"
BOT_UUIDS = {
    "BTCUSDT": "67d3e022-7414-4ef8-8b6c-3d5c56a09667",
    "ETHUSDT": "edac8b79-ac23-4af5-a6eb-666432a0cb57",
    "SOLUSDT": "3d72a934-50a2-4fd6-bbd2-0e678c841ef4",
    "XRPUSDT": "e798e648-fab5-4b94-82af-052228fa9ed1",
}
# Spot Binance webhook format: raw symbol (BTCUSDT), NOT slash format (BTC/USDT)
TV_INSTRUMENTS = {
    "BTCUSDT": "BTCUSDT",
    "ETHUSDT": "ETHUSDT",
    "SOLUSDT": "SOLUSDT",
    "XRPUSDT": "XRPUSDT",
}
COINGECKO_IDS = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum",
                  "SOLUSDT": "solana",  "XRPUSDT": "ripple"}
SYMBOLS     = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
STOP_LOSS   = 3.0
TP_MIN      = 0.5
TP_MAX      = 5.0
LOG_FILE    = "trade_log_bollinger.csv"

RSI_BUY_THRESHOLD = {"BTCUSDT": 20, "ETHUSDT": 20, "SOLUSDT": 20, "XRPUSDT": 15}
TP_MIN_SYMBOL     = {"BTCUSDT": 1.5, "ETHUSDT": 1.5, "SOLUSDT": 1.5, "XRPUSDT": 2.5}
VOL_CRASH_THRESHOLD = -8.0


def get_ohlc(coin_id, days):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    r = requests.get(url, params={"vs_currency": "usd", "days": str(days)},
                     headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def calc_ema(closes, p):
    if len(closes) < p: return closes[-1] if closes else 0
    k = 2 / (p + 1); e = sum(closes[:p]) / p
    for c in closes[p:]: e = c * k + e * (1 - k)
    return round(e, 4)


def calc_rsi(closes, p=14):
    if len(closes) < p + 1: return 50.0
    g = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    l = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[-p:]) / p, sum(l[-p:]) / p
    if ag == 0 and al == 0: return 50.0
    if al == 0: return 100.0
    if ag == 0: return 1.0
    return round(100 - (100 / (1 + ag / al)), 2)


def calc_bb(closes, p=20, s=2.0):
    if len(closes) < p: return None, None, None, 0, 0.5
    w = closes[-p:]; m = sum(w) / p
    std = math.sqrt(sum((x - m) ** 2 for x in w) / p)
    upper = round(m + s * std, 4); lower = round(m - s * std, 4); middle = round(m, 4)
    bw = round((upper - lower) / middle * 100, 4) if middle else 0
    pb = round((closes[-1] - lower) / (upper - lower), 4) if (upper - lower) else 0.5
    return upper, middle, lower, bw, pb


def get_trend_4h(candles_4h):
    closes = [c[4] for c in candles_4h]
    if len(closes) < 22: return "unknown"
    return "bullish" if calc_ema(closes, 9) > calc_ema(closes, 21) else "bearish"


def get_trend_1d(candles_1d):
    closes = [c[4] for c in candles_1d]
    if len(closes) < 22: return "unknown"
    return "bullish" if calc_ema(closes, 9) > calc_ema(closes, 21) else "bearish"


def get_change_24h(candles_30m):
    if len(candles_30m) < 48: return 0.0
    closes = [c[4] for c in candles_30m]
    old = closes[-48]; cur = closes[-1]
    return round((cur - old) / old * 100, 2) if old else 0.0


def check_signal(symbol, closes_30m, candles_4h, candles_1d, recent_sl=False):
    rsi_threshold = RSI_BUY_THRESHOLD[symbol]
    tp_min        = TP_MIN_SYMBOL[symbol]

    if recent_sl:
        return None, 0, "skip: re-entry after recent SL"

    trend_1d = get_trend_1d(candles_1d)
    if trend_1d == "bearish":
        return None, 0, "skip: daily trend bearish (EMA9<EMA21)"

    change_24h = get_change_24h(closes_30m if isinstance(closes_30m[0], (int, float))
                                else [c[4] for c in closes_30m])
    if change_24h <= VOL_CRASH_THRESHOLD:
        return None, 0, f"skip: volatility crash ({change_24h:.1f}% in 24hr)"

    closes = closes_30m if isinstance(closes_30m[0], (int, float)) else [c[4] for c in closes_30m]
    rsi = calc_rsi(closes, 14)
    upper, middle, lower, bw, pb = calc_bb(closes, 20, 2.0)
    price = closes[-1]
    trend_4h = get_trend_4h(candles_4h)

    if rsi > 75:
        return None, 0, f"skip: SELL blocked (RSI={rsi})"

    if rsi >= rsi_threshold:
        return None, 0, f"skip: RSI={rsi} not oversold enough (need <{rsi_threshold})"

    if trend_4h == "bearish":
        return None, 0, "skip: 4h trend bearish"

    if trend_4h == "bullish" and pb is not None and pb >= 0.5:
        return None, 0, f"skip: price above middle band in bullish trend (pb={pb:.2f})"

    if middle and price > 0:
        tp = round(abs(price - middle) / price * 100, 2)
        tp = max(tp_min, min(TP_MAX, tp))
    else:
        tp = tp_min

    if tp < tp_min:
        return None, 0, f"skip: TP {tp:.2f}% < minimum {tp_min}%"

    reason = (f"BUY: RSI={rsi} (threshold<{rsi_threshold}) | 4h={trend_4h} | "
              f"1d={trend_1d} | pb={pb:.2f} | 24hr={change_24h:.1f}% | TP={tp:.2f}%")
    return "BUY", tp, reason


def had_recent_sl(symbol, hours=6):
    if not os.path.exists(LOG_FILE): return False
    try:
        cutoff = datetime.now(tz=DUBAI_TZ) - timedelta(hours=hours)
        with open(LOG_FILE) as f:
            for row in reversed(list(csv.DictReader(f))):
                if row.get("symbol") != symbol: continue
                ts_str = row.get("timestamp_dubai", "")
                try:
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=DUBAI_TZ)
                except ValueError:
                    continue
                if ts < cutoff: break
                if row.get("webhook_fired") == "True" and row.get("result") == "sl":
                    return True
    except Exception:
        pass
    return False


def fire_webhook(symbol, price, tp_pct):
    payload = {
        "secret":        WEBHOOK_SECRET,
        "max_lag":       "300",
        "timestamp":     datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "trigger_price": str(price),
        "tv_exchange":   "BINANCE",
        "tv_instrument": TV_INSTRUMENTS[symbol],
        "action":        "enter_long",
        "bot_uuid":      BOT_UUIDS[symbol],
        "take_profit":   {"enabled": True, "steps": [{"order_type": "market",
                           "price_percent": round(tp_pct, 2), "volume_percent": 100}]},
        "stop_loss":     {"enabled": True, "order_type": "market",
                          "trigger_price_percent": STOP_LOSS},
    }
    r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    ok = r.status_code == 200
    print(f"  Webhook enter_long {TV_INSTRUMENTS[symbol]}: {'SUCCESS' if ok else f'FAILED [{r.status_code}] {r.text[:100]}'} "
          f"(TP:{tp_pct:.2f}%, SL:{STOP_LOSS}%)")
    return ok


def log_result(symbol, price, signal, tp_pct, reason, fired):
    ts = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    header = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        if header:
            w.writerow(["timestamp_dubai", "symbol", "price", "signal",
                        "take_profit_pct", "stop_loss_pct", "webhook_fired", "reason"])
        w.writerow([ts, symbol, price, signal or "HOLD",
                    round(tp_pct, 2) if tp_pct else "", STOP_LOSS, fired, reason])
    print(f"  [{ts}] {symbol} | {signal or 'HOLD'} | TP:{tp_pct:.2f}% | Fired:{fired}")
    print(f"  Reason: {reason}")


def run():
    now = datetime.now(tz=DUBAI_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*60}")
    print(f"Bollinger Band v18 — {now} Dubai time")
    print(f"Strategy: BUY only, RSI extreme oversold, all 4 pairs")
    print(f"{'='*60}")

    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")
        try:
            cid = COINGECKO_IDS[symbol]
            c30m = get_ohlc(cid, 1);  time.sleep(3)
            c4h  = get_ohlc(cid, 7);  time.sleep(3)
            c1d  = get_ohlc(cid, 90); time.sleep(3)

            closes_30m = [c[4] for c in c30m]
            price      = closes_30m[-1]
            rsi        = calc_rsi(closes_30m, 14)
            _, middle, _, bw, pb = calc_bb(closes_30m, 20, 2.0)
            trend_4h   = get_trend_4h(c4h)
            trend_1d   = get_trend_1d(c1d)
            change_24h = get_change_24h(c30m)

            print(f"  Price: ${price:,.4f}")
            print(f"  RSI-14: {rsi} | 4h: {trend_4h} | 1d: {trend_1d} | "
                  f"24hr: {change_24h:+.1f}% | %B: {pb:.2f} | BW: {bw:.2f}%")

            recent_sl = had_recent_sl(symbol, hours=6)
            if recent_sl:
                print(f"  Recent SL detected — skipping (re-entries lose over 8yr)")

            signal, tp_pct, reason = check_signal(
                symbol, closes_30m, c4h, c1d, recent_sl
            )

            fired = False
            if signal == "BUY":
                print(f"  ✅ BUY signal | TP: {tp_pct:.2f}% | SL: {STOP_LOSS}%")
                fired = fire_webhook(symbol, price, tp_pct)
            else:
                print(f"  ⏸️  HOLD — {reason}")

            log_result(symbol, price, signal, tp_pct or 0, reason, fired)
            time.sleep(2)

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback; traceback.print_exc()

    print(f"\n{'='*60}")
    print("Run complete.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    run()
