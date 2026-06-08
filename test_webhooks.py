"""
test_webhooks.py
Fires a real enter_long webhook to all 4 bots and reports the result.
Bypasses all signal filters — tests the 3Commas connection directly.

Run manually from GitHub Actions: Actions tab → Test Webhooks → Run workflow
Or locally: python3 test_webhooks.py

What it tests:
  - Correct tv_instrument format (SOL/USDT not SOLUSDT)
  - Correct bot_uuid for each pair
  - 3Commas webhook endpoint reachability
  - Whether each bot accepts or rejects the signal

Expected results:
  - 200 SUCCESS: bot accepted the signal, trade opened
  - 200 but Rejected in 3Commas UI: bot has an active trade or is paused
  - Non-200: connection/auth problem

Note: this fires REAL signals to the demo account.
Any bot with no active position will open a trade at current price.
"""

import requests, os, json
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
# Test TP/SL values
TP_PCT  = 2.0
SL_PCT  = 3.0

results = []

print("\n" + "="*60)
print("3Commas Webhook Test")
print("="*60)
print(f"Timestamp: {datetime.now(tz=timezone(timedelta(hours=4))).strftime('%Y-%m-%d %H:%M:%S')} Dubai")
print(f"Firing enter_long to all 4 bots...\n")

for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
    tv = TV_INSTRUMENTS[symbol]
    uuid = BOT_UUIDS[symbol]

    payload = {
        "secret":        WEBHOOK_SECRET,
        "max_lag":       "300",
        "timestamp":     datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "trigger_price": "1.0",   # placeholder price for test
        "tv_exchange":   "BINANCE",
        "tv_instrument": tv,
        "action":        "enter_long",
        "bot_uuid":      uuid,
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
        print(f"  {symbol} ({tv}): {result}")
        if not ok:
            print(f"    Response: {body}")
        results.append((symbol, ok, status, body))
    except Exception as e:
        print(f"  {symbol} ({tv}): ERROR — {e}")
        results.append((symbol, False, 0, str(e)))

print("\n" + "="*60)
passed = sum(1 for _, ok, _, _ in results if ok)
print(f"Results: {passed}/{len(results)} bots responded with 200")
print("="*60)

# Fail the workflow if any bot failed
if passed < len(results):
    print("\n⚠️  Some bots failed. Check above for details.")
    print("Common causes:")
    print("  - Bot has active trade (Rejected in 3Commas UI) — this is OK")
    print("  - Wrong bot_uuid or secret — fix in strategy files")
    print("  - 3Commas API down — try again later")
    # Don't exit 1 — a rejection from 3Commas (200 status) is still a working connection
    # Only non-200 means a real connection problem
    non200 = [s for s,ok,status,_ in results if status != 200]
    if non200:
        print(f"\n❌ Non-200 responses for: {non200}")
        exit(1)
    else:
        print("\n✅ All bots reachable (200 OK). Check 3Commas UI for trade status.")
else:
    print("\n✅ All bots accepted the signal.")
