#!/usr/bin/env python3
"""
Tick Recorder — Capture every websocket price update for BTC 5-min windows.
Records: timestamp_ms, window_elapsed_ms, up_bid, up_ask, down_bid, down_ask, coinbase_btc
Runs continuously across multiple windows, saves to CSV.
"""

import asyncio
import json
import time
import os
import csv
import sys
from datetime import datetime, timezone

try:
    import aiohttp
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "aiohttp", "-q"])
    import aiohttp

try:
    import websockets
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CB_WS_URL = "wss://ws-feed.exchange.coinbase.com"
OUTPUT_DIR = "/home/ubuntu/reports/ticks"
TRADES_DIR = "/home/ubuntu/reports/trades"

sys.stdout.reconfigure(line_buffering=True)

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TRADES_DIR, exist_ok=True)


async def get_market():
    now = int(time.time())
    window = (now // 300) * 300
    slug = f"btc-updown-5m-{window}"
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://gamma-api.polymarket.com/markets?slug={slug}",
                         headers={"User-Agent": "Mozilla/5.0"},
                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                if data and len(data) > 0:
                    m = data[0]
                    tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                    outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
                    up = tokens[0] if outcomes[0] == "Up" else tokens[1]
                    down = tokens[1] if outcomes[0] == "Up" else tokens[0]
                    return {"up": up, "down": down, "question": m.get("question", ""), "window_start": window}
    return None


async def main():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Tick Recorder starting", flush=True)
    print(f"Output: {OUTPUT_DIR}/", flush=True)

    window_count = 0

    while True:
        # Wait for next window
        now = time.time()
        nxt = (int(now) // 300 + 1) * 300
        wait = nxt - now
        if wait > 0 and wait < 300:
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Waiting {wait:.0f}s for next window...", flush=True)
            await asyncio.sleep(wait)
        await asyncio.sleep(2)

        mkt = await get_market()
        if not mkt:
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] No market found", flush=True)
            continue

        window_start = mkt["window_start"]
        window_end = window_start + 300
        window_count += 1
        up_token = mkt["up"]
        down_token = mkt["down"]

        # Shared state
        state = {
            "up_bid": 0.0, "up_ask": 0.0,
            "up_bid_size": 0.0, "up_ask_size": 0.0,
            "down_bid": 0.0, "down_ask": 0.0,
            "down_bid_size": 0.0, "down_ask_size": 0.0,
            "cb_price": 0.0,
            "ticks": [],
            "trades": [],
            "running": True,
        }

        tick_count = [0]

        def record_tick(source):
            """Record current state as a tick."""
            now = time.time()
            elapsed_ms = int((now - window_start) * 1000)
            tick_count[0] += 1
            state["ticks"].append({
                "tick": tick_count[0],
                "time_utc": datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3],
                "elapsed_ms": elapsed_ms,
                "up_bid": state["up_bid"],
                "up_bid_size": state["up_bid_size"],
                "up_ask": state["up_ask"],
                "up_ask_size": state["up_ask_size"],
                "down_bid": state["down_bid"],
                "down_bid_size": state["down_bid_size"],
                "down_ask": state["down_ask"],
                "down_ask_size": state["down_ask_size"],
                "cb_price": state["cb_price"],
                "source": source,
            })

        # Coinbase websocket
        async def cb_ws():
            try:
                async with websockets.connect(CB_WS_URL, ping_interval=20, ping_timeout=15) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]
                    }))
                    while state["running"]:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            if time.time() >= window_end + 10:
                                return
                            continue
                        try:
                            msg = json.loads(raw)
                            if msg.get("type") == "ticker" and "price" in msg:
                                state["cb_price"] = float(msg["price"])
                                record_tick("CB")
                        except:
                            pass
            except Exception as e:
                print(f"  [CB-WS] Error: {e}", flush=True)

        # Polymarket websocket
        async def poly_ws():
            try:
                async with websockets.connect(POLY_WS_URL, ping_interval=20, ping_timeout=15,
                                              close_timeout=5) as ws:
                    await ws.send(json.dumps({"assets_ids": [up_token, down_token], "type": "market"}))
                    while state["running"]:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            if time.time() >= window_end + 10:
                                return
                            continue
                        try:
                            msg = json.loads(raw)
                        except:
                            continue

                        # ── last_trade_price (per-trade execution event) ──
                        # Polymarket market WS emits these when a maker/taker match occurs.
                        # Captures: timestamp, asset_id, price, size, side, fee_rate_bps
                        def _try_capture_trade(item):
                            if not isinstance(item, dict):
                                return
                            evt = item.get("event_type", "")
                            if evt != "last_trade_price":
                                return
                            try:
                                state["trades"].append({
                                    "tick_index": tick_count[0],  # nearest book tick for ordering
                                    "time_utc": datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3],
                                    "elapsed_ms": int((time.time() - window_start) * 1000),
                                    "asset_id": item.get("asset_id", ""),
                                    "side": item.get("side", ""),  # taker side (BUY/SELL)
                                    "size": float(item.get("size", 0)),
                                    "price": float(item.get("price", 0)),
                                    "fee_rate_bps": item.get("fee_rate_bps", 0),
                                    "timestamp_ms": item.get("timestamp", 0),
                                })
                            except (TypeError, ValueError):
                                pass

                        # Trade events may come standalone OR within a list
                        if isinstance(msg, dict):
                            _try_capture_trade(msg)
                        elif isinstance(msg, list):
                            for item in msg:
                                _try_capture_trade(item)

                        updated = False

                        # Initial snapshot
                        if isinstance(msg, list):
                            for item in msg:
                                if not isinstance(item, dict):
                                    continue
                                aid = item.get("asset_id", "")
                                bids = item.get("bids", [])
                                asks = item.get("asks", [])
                                if bids:
                                    try:
                                        bb = max(float(b["price"]) for b in bids if isinstance(b, dict) and "price" in b)
                                        if aid == up_token:
                                            state["up_bid"] = bb
                                        elif aid == down_token:
                                            state["down_bid"] = bb
                                        updated = True
                                    except:
                                        pass
                                if asks:
                                    try:
                                        ba = min(float(a["price"]) for a in asks if isinstance(a, dict) and "price" in a)
                                        if aid == up_token:
                                            state["up_ask"] = ba
                                        elif aid == down_token:
                                            state["down_ask"] = ba
                                        updated = True
                                    except:
                                        pass
                            if updated:
                                record_tick("POLY-SNAP")
                            continue

                        if not isinstance(msg, dict):
                            continue

                        # Snapshot with asset_id
                        if "bids" in msg and "asset_id" in msg:
                            aid = msg["asset_id"]
                            bids = msg.get("bids", [])
                            asks = msg.get("asks", [])
                            if bids:
                                try:
                                    bb = max(float(b["price"]) for b in bids if isinstance(b, dict) and "price" in b)
                                    if aid == up_token:
                                        state["up_bid"] = bb
                                    elif aid == down_token:
                                        state["down_bid"] = bb
                                    updated = True
                                except:
                                    pass
                            if asks:
                                try:
                                    ba = min(float(a["price"]) for a in asks if isinstance(a, dict) and "price" in a)
                                    if aid == up_token:
                                        state["up_ask"] = ba
                                    elif aid == down_token:
                                        state["down_ask"] = ba
                                    updated = True
                                except:
                                    pass

                        # price_changes
                        for pc in msg.get("price_changes", []):
                            if not isinstance(pc, dict):
                                continue
                            aid = pc.get("asset_id", "")
                            bb = pc.get("best_bid")
                            ba = pc.get("best_ask")
                            sz = pc.get("size")
                            side = pc.get("side")
                            if aid == up_token:
                                if bb is not None:
                                    state["up_bid"] = float(bb)
                                    updated = True
                                if ba is not None:
                                    state["up_ask"] = float(ba)
                                    updated = True
                                if sz is not None and side == "BUY":
                                    state["up_bid_size"] = float(sz)
                                elif sz is not None and side == "SELL":
                                    state["up_ask_size"] = float(sz)
                            elif aid == down_token:
                                if bb is not None:
                                    state["down_bid"] = float(bb)
                                    updated = True
                                if ba is not None:
                                    state["down_ask"] = float(ba)
                                    updated = True
                                if sz is not None and side == "BUY":
                                    state["down_bid_size"] = float(sz)
                                elif sz is not None and side == "SELL":
                                    state["down_ask_size"] = float(sz)

                        if updated:
                            record_tick("POLY")

            except Exception as e:
                print(f"  [POLY-WS] Error: {e}", flush=True)

        # Timer to stop
        async def timer():
            while time.time() < window_end + 10:
                await asyncio.sleep(1)
            state["running"] = False

        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Window {window_count}: {mkt['question'][:50]}", flush=True)

        await asyncio.gather(cb_ws(), poly_ws(), timer(), return_exceptions=True)

        # Save ticks to CSV
        ticks = state["ticks"]
        if ticks:
            fname = f"{OUTPUT_DIR}/ticks_{window_start}.csv"
            with open(fname, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=ticks[0].keys())
                w.writeheader()
                w.writerows(ticks)

        # Save trade events to CSV (separate file for clarity)
        trades = state["trades"]
        if trades:
            tfname = f"{TRADES_DIR}/trades_{window_start}.csv"
            with open(tfname, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=trades[0].keys())
                w.writeheader()
                w.writerows(trades)
            print(f"  {len(trades)} trade events saved to {tfname}", flush=True)

            # Quick stats
            up_bids = [t["up_bid"] for t in ticks if t["up_bid"] > 0]
            if up_bids:
                up_min = min(up_bids)
                up_max = max(up_bids)
                up_range = up_max - up_min
                # Count direction changes (crosses)
                crosses = 0
                prev_direction = None
                for i in range(1, len(up_bids)):
                    if up_bids[i] > up_bids[i-1]:
                        d = "up"
                    elif up_bids[i] < up_bids[i-1]:
                        d = "down"
                    else:
                        continue
                    if prev_direction and d != prev_direction:
                        crosses += 1
                    prev_direction = d

                print(f"  {len(ticks)} ticks | UP: ${up_min:.2f}-${up_max:.2f} (range ${up_range:.2f}) | "
                      f"{crosses} direction changes | saved {fname}", flush=True)
            else:
                print(f"  {len(ticks)} ticks saved to {fname}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
