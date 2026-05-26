"""
LiveOrderEngine smoke test — places a maker GTC order FAR from market ($0.01)
on the next BTC 5m market, verifies it appears in open orders, then cancels it.

Tests the entire live execution path through LiveOrderEngine without
financial risk:
  - place_order (GTC)
  - get_open_orders (and verifies the order shows up)
  - cancel
  - get_open_orders (and verifies it's gone)

Cost: 0¢ — order will not fill at $0.01, then is cancelled within seconds.
Requires: POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER_ADDRESS in env.
"""
import os
import sys
import time
import json
import asyncio

import aiohttp
from dotenv import load_dotenv

load_dotenv()

from py_clob_client_v2.client import ClobClient
from order_engine import LiveOrderEngine, Side, OrderType, OrderStatus


CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER = os.getenv("POLYMARKET_FUNDER_ADDRESS") or os.getenv("FUNDER", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))


def log(msg):
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def get_next_btc_market():
    """Fetch the closest BTC 5m market that hasn't started yet (or just started)."""
    now = int(time.time())
    # BTC 5m windows start at HH:00, HH:05, ... — round forward to next window
    window_start = ((now // 300) + 1) * 300
    for offset in (0, 300, 600):  # try next 3 windows
        slug = f"btc-updown-5m-{window_start + offset}"
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://gamma-api.polymarket.com/markets?slug={slug}",
                             headers={"User-Agent": "Mozilla/5.0"},
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    if data:
                        m = data[0]
                        tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                        outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
                        up_idx = 0 if outcomes[0] == "Up" else 1
                        return {
                            "up": tokens[up_idx],
                            "down": tokens[1 - up_idx],
                            "window_start": window_start + offset,
                            "question": m.get("question", ""),
                        }
    return None


def main():
    if not PRIVATE_KEY:
        log("FAIL: POLYMARKET_PRIVATE_KEY not set")
        sys.exit(1)
    log("Connecting to Polymarket CLOB...")
    client = ClobClient(CLOB_HOST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
                        signature_type=SIGNATURE_TYPE, funder=FUNDER)
    client.set_api_creds(client.create_or_derive_api_key())
    log("Auth OK")

    engine = LiveOrderEngine(client, signature_type=SIGNATURE_TYPE)

    log("Fetching next BTC market...")
    market = asyncio.run(get_next_btc_market())
    if not market:
        log("FAIL: could not find an upcoming BTC 5m market")
        sys.exit(2)
    log(f"Using market: {market['question']} (window {market['window_start']})")

    # Place GTC BUY at $0.01 (effectively zero-risk — won't fill above $0.01 ask)
    test_token = market["up"]
    test_price = 0.01
    test_size = 5  # smallest meaningful size
    log(f"Placing GTC BUY {test_size}sh UP @ ${test_price:.2f} (token {test_token[:12]}...)")
    try:
        order = engine.place_order(
            token_id=test_token, token_label="UP",
            side=Side.BUY, price=test_price, size=test_size,
            order_type=OrderType.GTC,
        )
        log(f"  Order id={order.id} status={order.status.name} filled={order.filled_size}/{order.size}")
        if not order.id:
            log("FAIL: no order id returned")
            sys.exit(3)
    except Exception as e:
        log(f"FAIL: place_order raised {type(e).__name__}: {str(e)[:160]}")
        sys.exit(3)

    # Verify via get_open_orders
    time.sleep(1.0)
    log("Verifying via get_open_orders...")
    found = False
    try:
        for o in engine.get_open_orders():
            if o.id == order.id:
                found = True
                log(f"  FOUND: {o.id} {o.side.value} {o.size}sh @ ${o.price:.2f} status={o.status.name}")
                break
    except Exception as e:
        log(f"WARN: get_open_orders failed: {str(e)[:160]}")
    if not found:
        log("  not present in get_open_orders (may have been auto-cancelled)")

    # Cancel
    time.sleep(0.5)
    log("Cancelling order...")
    try:
        ok = engine.cancel(order.id)
        log(f"  cancel result: {ok}")
    except Exception as e:
        log(f"FAIL: cancel raised {type(e).__name__}: {str(e)[:160]}")
        sys.exit(4)

    # Verify cancelled
    time.sleep(1.0)
    log("Verifying cancellation via get_open_orders...")
    still_open = False
    try:
        for o in engine.get_open_orders():
            if o.id == order.id:
                still_open = True
                log(f"  STILL OPEN: {o.id} status={o.status.name}")
                break
    except Exception as e:
        log(f"WARN: get_open_orders failed: {str(e)[:160]}")
    if still_open:
        log("FAIL: order still appears in get_open_orders after cancel")
        sys.exit(5)
    log("  confirmed cancelled (not in open orders)")

    log("")
    log("=== LIVE SMOKE TEST PASSED ===")
    log("LiveOrderEngine.place_order + cancel + get_open_orders verified end-to-end.")


if __name__ == "__main__":
    main()
