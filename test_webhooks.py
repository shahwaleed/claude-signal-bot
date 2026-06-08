"""
test_webhooks.py
Fires a real enter_long webhook to all 4 bots using current market prices.
Bypasses all signal filters — tests the 3Commas connection directly.

Run manually from GitHub Actions: Actions tab → Test Webhooks → Run workflow
Or locally: python3 test_webhooks.py

What it tests:
  - Correct tv_instrument format (SOL/USDT not SOLUSDT)
  - Correct bot_uuid for each pair
  - 3Commas webhook endpoint reachability
  - Whether each bot accepts or rejects the signal

Expected results:
  - 200 SUCCESS: bot received the signal, check 3Commas UI for trade status
  - Non-200: connection or authentication problem

Note: this fires REAL signals to the demo account.
Any bot with no active position will open a trade at current price.
"""

import requests, os, json, time
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
TV_INSTRUMENTS = {
    "BTCUSDT": "BTC/USDT",
    "ETHUSDT": "ETH/USDT",
    "SOLUSDT": "SOL/USDT",
    "XRPUSDT": "XRP/USDT",
}
COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "SOLUSDT": "solana",
    "XRPUSDT": "ripple",
}
TP_PCT = 2.0
SL_PCT = 3.0


def get_current_price(symbol):
    """Fetch current price from CoinGecko OHLC (last candle close)."""
    cid = COINGECKO_IDS[symbol]
    url = f"https://api.coingecko.com/api/v3/coins/{cid}/ohlc"
    r = requests.get(url, params={"vs_currency": "usd", "days": "1"}, timeout=15)
    r.raise_for_status()
    return float(r.json()[-1][4])


print("\n" + "="*60)
print("3Commas Webhook Test")
print("="*60)
print(f"Timestamp: {datetime.now(tz=timezone(timedelta(hours=4))).strftime('%Y-%m-%d %H:%M:%S')} Dubai")
print()

# Step 1: Fetch real current prices
print("Fetching current prices from CoinGecko...")
prices = {}
for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
    try:
        price = get_current_price(symbol)
        prices[symbol] = price
        print(f"  {symbol}: ${price:,.4f}")
        time.sleep(2)  # respect CoinGecko rate limit
    except Exception as e:
        print(f"  {symbol}: price fetch failed — {e}")
        prices[symbol] = None

print()

# Step 2: Fire webhooks with real prices
print("Firing enter_long to all 4 bots...")
results = []

for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
    price = prices.get(symbol)
    tv    = TV_INSTRUMENTS[symbol]

    if price is None:
        print(f"  {symbol} ({tv}): SKIPPED — could not fetch price")
        results.append((symbol, False, 0, "price fetch failed"))
        continue

    payload = {
        "secret":        WEBHOOK_SECRET,
        "max_lag":       "300",
        "timestamp":     datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "trigger_price": str(price),
        "tv_exchange":   "BINANCE",
        "tv_instrument": tv,
        "action":        "enter_long",
        "bot_uuid":      BOT_UUIDS[symbol],
        "take_profit":   {"enabled": True, "steps": [{"order_type": "market",
                           "price_percent": TP_PCT, "volume_percent": 100}]},
        "stop_loss":     {"enabled": True, "order_type": "market",
                          "trigger_price_percent": SL_PCT},
    }

    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        status = r.status_code
        body   = r.text[:200]
        ok     = status == 200
        result = "SUCCESS" if ok else f"FAILED [{status}]"
        print(f"  {symbol} ({tv}) @ ${price:,.4f}: {result}")
        if not ok:
            print(f"    Response: {body}")
        results.append((symbol, ok, status, body))
    except Exception as e:
        print(f"  {symbol} ({tv}): ERROR — {e}")
        results.append((symbol, False, 0, str(e)))

# Step 3: Summary
print()
print("="*60)
passed = sum(1 for _, ok, _, _ in results if ok)
print(f"Results: {passed}/{len(results)} bots responded with 200 OK")
print("="*60)
print()
print("What to check in 3Commas:")
print("  ✅ Bot opened a new trade — connection working perfectly")
print("  ⚠️  Signal shows 'Rejected' — bot already has active trade (normal)")
print("  ❌  Signal missing entirely — something else is wrong")

# Fail workflow only on non-200 (real connection problems)
non200 = [(s, status, body) for s, _, status, body in results if status != 200]
if non200:
    print()
    print("❌ Connection problems detected:")
    for sym, status, body in non200:
        print(f"  {sym}: [{status}] {body[:100]}")
    exit(1)
else:
    print()
    print("✅ All bots reachable. Check 3Commas UI for trade status.")
