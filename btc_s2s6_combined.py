#!/usr/bin/env python3
"""
BTC-S2-S6-COMBINED — Live deployment of the validated S2 + S6 strategies.

Strategy:
  S2 (fade large instant moves):
    - Fires when Coinbase BTC moves >= $15 between consecutive CB ticks (within 1.5s)
    - Buys the OPPOSITE side (fade) at the ask via taker FAK
    - Holds to window resolution

  S6 (momentum continuation):
    - Tracks BTC price from window start (P0)
    - Fires when |current_btc - P0| >= $30
    - Buys the WINNING side (aligned with cumulative move) at the ask via taker FAK
    - Holds to window resolution

  Both can fire in the same window. When they do, ~77% of the time they're same direction
  (constructive double-up). 23% they're opposite (effective hedge).

Per-trade size: 7 shares
No TP, no SL, no hedge — pure hold-to-resolution via OpenClaw/cast redemption cron.

Validated performance (paper engine on 277 windows, 2026-05-22):
  S6 solo: +$221/day at 10sh, 75% WR
  S2 solo: +$100/day at 10sh, 77% WR
  Combined: +$322/day at 10sh
  Combined at 7sh: estimated ~+$225/day
"""

import asyncio
import json
import time
import os
import sys
import csv
from datetime import datetime, timezone, timedelta

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

try:
    from dotenv import load_dotenv
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-dotenv", "-q"])
    from dotenv import load_dotenv

load_dotenv()

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, OrderType, BalanceAllowanceParams
from py_clob_client_v2.order_builder.constants import BUY, SELL

# ── Config ─────────────────────────────────────────────
SHARES_PER_TRADE = 7

# S2: fade large instant CB moves
S2_THRESHOLD = 15.0           # CB move threshold in dollars
S2_MAX_GAP_MS = 1500          # max ms between consecutive CB ticks

# S6: momentum on cumulative window move
S6_THRESHOLD = 30.0           # cumulative BTC move from window start

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", os.getenv("FUNDER", ""))
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CB_WS_URL = "wss://ws-feed.exchange.coinbase.com"

BOT_NAME = "BTC-S2-S6"
LOG_FILE = "logs/btc_s2s6_trades.jsonl"
SUMMARY_FILE = "logs/btc_s2s6_summary.json"
os.makedirs("logs", exist_ok=True)

sys.stdout.reconfigure(line_buffering=True)


def log_msg(msg: str):
    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def snap_price(p: float) -> float:
    """Snap to nearest $0.01 tick."""
    return round(round(p * 100) / 100, 2)


def calc_taker_fee(price: float, shares: float) -> float:
    return 0.022 * min(price, 1 - price) * shares


async def get_market_for_window(window_start: int):
    """Look up the BTC 5-min market for a given window-start unix timestamp."""
    slug = f"btc-updown-5m-{window_start}"
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(f"https://gamma-api.polymarket.com/markets?slug={slug}",
                             headers={"User-Agent": "Mozilla/5.0"},
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    if data and len(data) > 0:
                        m = data[0]
                        tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                        outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
                        up_idx = 0 if outcomes[0] == "Up" else 1
                        dn_idx = 1 - up_idx
                        return {
                            "up": tokens[up_idx], "down": tokens[dn_idx],
                            "question": m.get("question", ""), "window_start": window_start,
                            "condition_id": m.get("conditionId", ""),
                        }
        except Exception as e:
            log_msg(f"[GAMMA] Error: {str(e)[:100]}")
    return None


class S2S6Bot:
    def __init__(self):
        self.client = None
        if PRIVATE_KEY:
            self.client = ClobClient(CLOB_HOST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
                                     signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS)
            try:
                self.client.set_api_creds(self.client.create_or_derive_api_key())
                log_msg(f"[CLOB] Auth OK — LIVE execution ready")
            except Exception as e:
                log_msg(f"[CLOB] Auth failed: {e}")
                self.client = None
        # WS-fed market state
        self.up_bid = 0.0
        self.up_ask = 0.0
        self.down_bid = 0.0
        self.down_ask = 0.0
        # CB state
        self.cb_price = 0.0
        self.prev_cb_price = 0.0
        self.prev_cb_ms = 0
        # Window state
        self.p0 = None
        self.window_start_ms = 0
        # Trade flags (reset per window)
        self.s2_fired = False
        self.s6_fired = False
        # Stats
        self.bankroll_start = 0.0
        self.trade_id = 0
        self.log_file = open(LOG_FILE, "a")

    def get_balance(self) -> float:
        if not self.client:
            return 0
        try:
            params = BalanceAllowanceParams(asset_type="COLLATERAL", token_id="", signature_type=2)
            self.client.update_balance_allowance(params)
            result = self.client.get_balance_allowance(params)
            return int(result.get("balance", "0")) / 1_000_000
        except Exception as e:
            log_msg(f"[BAL] Error: {str(e)[:80]}")
            return 0

    def pre_approve(self, up_token: str, down_token: str):
        if not self.client:
            return
        for token in [up_token, down_token]:
            try:
                params = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token,
                                                signature_type=SIGNATURE_TYPE)
                self.client.update_balance_allowance(params)
            except Exception:
                pass

    def place_taker_buy(self, token_id: str, ask_price: float, shares: int, strategy: str) -> dict:
        """Place a taker FAK buy at the ask. Returns dict with success + fill info."""
        self.trade_id += 1
        tid = self.trade_id
        result = {"tid": tid, "strategy": strategy, "token_id": token_id,
                  "ask": ask_price, "shares": shares, "filled_size": 0,
                  "fill_price": 0, "fee": 0, "time_ms": int(time.time() * 1000)}
        if not self.client:
            log_msg(f"[NO-CLIENT] {strategy} #{tid} would buy {shares}sh @ ${ask_price:.2f}")
            return result
        try:
            args = OrderArgs(price=snap_price(ask_price), size=shares, side=BUY, token_id=token_id)
            signed = self.client.create_order(args)
            resp = self.client.post_order(signed, OrderType.FAK)
            order_id = resp.get("orderID", "")
            # Check actual fill (FAK fills what's available)
            time.sleep(0.3)  # let settle
            try:
                order = self.client.get_order(order_id)
                if order:
                    matched = float(order.get("size_matched", 0))
                    result["filled_size"] = matched
                    result["fill_price"] = float(order.get("price", ask_price))
                    result["fee"] = calc_taker_fee(result["fill_price"], matched)
                    log_msg(f"[{strategy}] #{tid} BOUGHT {matched:.0f}sh @ ${result['fill_price']:.2f} "
                            f"(fee ${result['fee']:.3f})")
            except Exception:
                pass
        except Exception as e:
            log_msg(f"[{strategy}-FAIL] #{tid} {str(e)[:100]}")
        return result

    def record_trade(self, trade: dict, exit_price: float = 0, reason: str = ""):
        """Append trade to JSONL log."""
        rec = {
            "tid": trade["tid"], "strategy": trade["strategy"],
            "token_id": trade["token_id"], "shares": trade["filled_size"],
            "fill_price": trade["fill_price"], "fee": trade["fee"],
            "exit_price": exit_price, "reason": reason,
            "expected_pnl": (exit_price - trade["fill_price"]) * trade["filled_size"] - trade["fee"],
            "time": datetime.now(timezone.utc).isoformat(),
        }
        self.log_file.write(json.dumps(rec) + "\n")
        self.log_file.flush()


async def run_window(bot: S2S6Bot, window_start: int):
    window_end = window_start + 300
    market = await get_market_for_window(window_start)
    if not market:
        log_msg(f"[NO-MARKET] window {window_start}")
        return
    up_token = market["up"]
    down_token = market["down"]
    log_msg(f"[WINDOW] {market['question']}")

    bot.pre_approve(up_token, down_token)
    # Reset per-window state
    bot.up_bid = bot.up_ask = bot.down_bid = bot.down_ask = 0
    bot.cb_price = bot.prev_cb_price = 0
    bot.prev_cb_ms = 0
    bot.p0 = None
    bot.window_start_ms = int(window_start * 1000)
    bot.s2_fired = False
    bot.s6_fired = False

    held_trades = []  # [(trade_dict, direction_label)]
    running = True

    async def cb_ws():
        nonlocal running
        try:
            async with websockets.connect(CB_WS_URL, ping_interval=20, ping_timeout=15) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]
                }))
                log_msg("[CB-WS] Connected")
                while running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError:
                        if time.time() >= window_end - 5:
                            return
                        continue
                    try:
                        msg = json.loads(raw)
                        if msg.get("type") != "ticker" or "price" not in msg:
                            continue
                        new_price = float(msg["price"])
                        now_ms = int(time.time() * 1000)

                        # Capture P0
                        if bot.p0 is None:
                            bot.p0 = new_price
                            log_msg(f"[P0] BTC opening price: ${new_price:.2f}")

                        # ── S2 trigger: instantaneous |move| >= $15 between consecutive ticks ──
                        if (bot.prev_cb_price > 0
                                and (now_ms - bot.prev_cb_ms) <= S2_MAX_GAP_MS
                                and not bot.s2_fired):
                            move = new_price - bot.prev_cb_price
                            if abs(move) >= S2_THRESHOLD:
                                # Fade — buy OPPOSITE direction
                                if move > 0:  # CB went UP → fade by buying DOWN
                                    fade_token = down_token
                                    fade_ask = bot.down_ask
                                    fade_label = "DN"
                                else:
                                    fade_token = up_token
                                    fade_ask = bot.up_ask
                                    fade_label = "UP"
                                if fade_ask > 0 and fade_ask < 1:
                                    log_msg(f"[S2-SIGNAL] CB {'UP' if move > 0 else 'DN'} ${move:+.2f} | "
                                            f"FADE {fade_label} @ ${fade_ask:.2f}")
                                    trade = bot.place_taker_buy(fade_token, fade_ask,
                                                                SHARES_PER_TRADE, "S2-FADE")
                                    if trade["filled_size"] > 0:
                                        held_trades.append((trade, fade_label))
                                    bot.s2_fired = True

                        # ── S6 trigger: cumulative |move from P0| >= $30 ──
                        if bot.p0 is not None and not bot.s6_fired:
                            cum_move = new_price - bot.p0
                            if abs(cum_move) >= S6_THRESHOLD:
                                # Momentum — buy ALIGNED direction
                                if cum_move > 0:  # BTC trending UP → buy UP
                                    mom_token = up_token
                                    mom_ask = bot.up_ask
                                    mom_label = "UP"
                                else:
                                    mom_token = down_token
                                    mom_ask = bot.down_ask
                                    mom_label = "DN"
                                if mom_ask > 0 and mom_ask < 1:
                                    log_msg(f"[S6-SIGNAL] BTC cum ${cum_move:+.2f} from P0 | "
                                            f"MOMENTUM {mom_label} @ ${mom_ask:.2f}")
                                    trade = bot.place_taker_buy(mom_token, mom_ask,
                                                                SHARES_PER_TRADE, "S6-MOMENTUM")
                                    if trade["filled_size"] > 0:
                                        held_trades.append((trade, mom_label))
                                    bot.s6_fired = True

                        bot.prev_cb_price = new_price
                        bot.prev_cb_ms = now_ms
                        bot.cb_price = new_price
                    except Exception:
                        pass
        except Exception as e:
            log_msg(f"[CB-WS] Error: {str(e)[:80]}")

    async def poly_ws():
        nonlocal running
        try:
            async with websockets.connect(POLY_WS_URL, ping_interval=20, ping_timeout=15) as ws:
                await ws.send(json.dumps({"assets_ids": [up_token, down_token], "type": "market"}))
                log_msg("[POLY-WS] Connected")
                while running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError:
                        if time.time() >= window_end - 5:
                            return
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    def update_from(item):
                        if not isinstance(item, dict):
                            return
                        aid = item.get("asset_id", "")
                        # Snapshot format
                        bids = item.get("bids", [])
                        asks = item.get("asks", [])
                        if bids:
                            try:
                                bb = max(float(b["price"]) for b in bids if isinstance(b, dict) and "price" in b)
                                if aid == up_token: bot.up_bid = bb
                                elif aid == down_token: bot.down_bid = bb
                            except Exception:
                                pass
                        if asks:
                            try:
                                ba = min(float(a["price"]) for a in asks if isinstance(a, dict) and "price" in a)
                                if aid == up_token: bot.up_ask = ba
                                elif aid == down_token: bot.down_ask = ba
                            except Exception:
                                pass
                        # price_changes format
                        for pc in item.get("price_changes", []):
                            if not isinstance(pc, dict):
                                continue
                            aid2 = pc.get("asset_id", aid)
                            bb = pc.get("best_bid")
                            ba = pc.get("best_ask")
                            if bb is not None:
                                if aid2 == up_token: bot.up_bid = float(bb)
                                elif aid2 == down_token: bot.down_bid = float(bb)
                            if ba is not None:
                                if aid2 == up_token: bot.up_ask = float(ba)
                                elif aid2 == down_token: bot.down_ask = float(ba)

                    if isinstance(msg, list):
                        for item in msg: update_from(item)
                    else:
                        update_from(msg)
                        if "price_changes" in msg:
                            update_from(msg)  # handle top-level price_changes
        except Exception as e:
            log_msg(f"[POLY-WS] Error: {str(e)[:80]}")

    # Run for the window duration
    timer_task = asyncio.create_task(asyncio.sleep(max(1, window_end - time.time())))
    cb_task = asyncio.create_task(cb_ws())
    poly_task = asyncio.create_task(poly_ws())
    try:
        await timer_task
    finally:
        running = False
        try: cb_task.cancel()
        except Exception: pass
        try: poly_task.cancel()
        except Exception: pass

    # Record trades at window end
    if held_trades:
        log_msg(f"[WINDOW-END] {len(held_trades)} held positions")
        for trade, dir_label in held_trades:
            final_bid = bot.up_bid if dir_label == "UP" else bot.down_bid
            if final_bid >= 0.95:
                expected_redemption = 1.00
            elif final_bid <= 0.05:
                expected_redemption = 0.00
            else:
                expected_redemption = final_bid
            bot.record_trade(trade, expected_redemption,
                             f"HOLD-RESOLUTION ({dir_label}, final_bid=${final_bid:.2f})")
            pnl = (expected_redemption - trade["fill_price"]) * trade["filled_size"] - trade["fee"]
            log_msg(f"[RESOLVE] {trade['strategy']} #{trade['tid']} {dir_label} | "
                    f"entry ${trade['fill_price']:.2f} → expected ${expected_redemption:.2f} | "
                    f"P&L ${pnl:+.2f}")
    else:
        log_msg(f"[WINDOW-END] No trades fired this window")


async def main():
    log_msg(f"=== {BOT_NAME} starting ===")
    bot = S2S6Bot()
    bot.bankroll_start = bot.get_balance()
    log_msg(f"[WALLET] Balance: ${bot.bankroll_start:.2f}")

    if not bot.client:
        log_msg("[WARN] No CLOB client — running in DRY mode (no orders placed)")

    while True:
        now = int(time.time())
        # Wait until next 5-min window boundary + small buffer
        next_window = ((now // 300) + 1) * 300
        wait = next_window - time.time()
        if wait > 0:
            log_msg(f"[SYNC] Waiting {wait:.0f}s for next window")
            await asyncio.sleep(wait + 2)
        window_start = ((int(time.time()) // 300)) * 300
        await run_window(bot, window_start)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log_msg("Stopped by user")
