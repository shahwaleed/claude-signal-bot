"""
test_webhooks.py
Fires a real enter_long webhook to all 4 bots using current market prices.

This version uses the EXACT payload format from the last working commit
(fa56167e, June 8 08:01) — including take_profit and stop_loss fields.

Run manually from GitHub Actions: Actions tab -> Test Webhooks -> Run workflow

NOTE: tv_instrument = raw symbol (BTCUSDT), NOT slash format (BTC/USDT).
      Slash format is 3Commas UI display only. This must never be changed.
"""

import requests, os, time
from datetime import datetime, timezone, timedelta

WEBHOOK_URL    = "https://api.3commas.io/signal_bots/webhooks"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET",
    "eyJhbGciOiJIUzI1NiJ9.eyJzaWduYWxzX3NvdXJjZV9pZCI6MTMwNTYyfQ.DnbuKVB9cslOFa5l1WtrKH1PFsvacsV0Vfkh_e3E_DY")

BOT_UUIDS = {
    "BTCUSDT": "67d3e022-7414-4ef8-8b6c-3d5c56a09667",
    "ETHUSDT": "edac8b79-ac23-4af5-a6eb-666432a0cb57",
    "SOLUSDT": "3d72a934-50a2-4fd6-bbd2-0e678c841ef4",
    "XRPUSDT": "e798e648-fab5-4b94-82af-052228fa9ed1",
}
COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "SOLUSDT": "solana",
    "XRPUSDT": "ripple",
}
STOP_LOSS = 3.0
TP_PCT    = 2.5  # fixed TP for test signals


def get_current_price(symbol):
    cid = COINGECKO_IDS[symbol]
    url = f"https://api.coingecko.com/api/v3/coins/{cid}/ohlc"
    r = requests.get(url, params={"vs_currency": "usd", "days": "1"}, timeout=15)
    r.raise_for_status()
    return float(r.json()[-1][4])


print("\n" + "="*60)
print("3Commas Webhook Test — RESTORED WORKING PAYLOAD")
print("Payload: with take_profit + stop_loss (as per fa56167e)")
print("="*60)
print(f"Timestamp: {datetime.now(tz=timezone(timedelta(hours=4))).strftime('%Y-%m-%d %H:%M:%S')} Dubai")
print()

print("Fetching current prices from CoinGecko...")
prices = {}
for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
    try:
        price = get_current_price(symbol)
        prices[symbol] = price
        print(f"  {symbol}: ${price:,.4f}")
        time.sleep(2)
    except Exception as e:
        print(f"  {symbol}: price fetch failed — {e}")
        prices[symbol] = None

print()
print("Firing enter_long to all 4 bots (with TP/SL)...")
print()

results = []
for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
    price = prices.get(symbol)
    if price is None:
        print(f"  {symbol}: SKIPPED — could not fetch price")
        results.append((symbol, False, 0, "price fetch failed"))
        continue

    # Exact payload from last working commit fa56167e
    payload = {
        "secret":        WEBHOOK_SECRET,
        "max_lag":       "300",
        "timestamp":     datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "trigger_price": str(price),
        "tv_exchange":   "BINANCE",
        "tv_instrument": symbol,
        "action":        "enter_long",
        "bot_uuid":      BOT_UUIDS[symbol],
        "take_profit":   {"enabled": True, "steps": [{"order_type": "market",
                           "price_percent": TP_PCT, "volume_percent": 100}]},
        "stop_loss":     {"enabled": True, "order_type": "market",
                          "trigger_price_percent": STOP_LOSS},
    }

    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        status = r.status_code
        body   = r.text[:300]
        ok     = status == 200
        result = "SUCCESS" if ok else f"FAILED [{status}]"
        print(f"  {symbol} @ ${price:,.4f}: {result}")
        if body:
            print(f"    Response: {body}")
        results.append((symbol, ok, status, body))
    except Exception as e:
        print(f"  {symbol}: ERROR — {e}")
        results.append((symbol, False, 0, str(e)))

print()
print("="*60)
passed = sum(1 for _, ok, _, _ in results if ok)
print(f"Results: {passed}/{len(results)} bots responded with 200 OK")
print("="*60)
print()
print("Check in 3Commas Signal Bot -> Signals tab:")
print("  ✅ Counter went above 574 = signal reached source = payload works")
print("  ✅ 'Rejected' in list = signal received but bot busy (still a win)")
print("  ❌ Counter still 574 = signal still not reaching source")

non200 = [(s, status, body) for s, _, status, body in results if status != 200]
if non200:
    print()
    print("❌ HTTP errors:")
    for sym, status, body in non200:
        print(f"  {sym}: [{status}] {body[:100]}")
    exit(1)
else:
    print()
    print("✅ All bots returned 200 OK.")
