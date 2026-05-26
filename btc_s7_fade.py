#!/usr/bin/env python3
"""
BTC-S7-FADE — Live deployment of the highest-margin validated strategy (S7 = S2 fade only).

Strategy:
  On CB BTC move >= $18 between consecutive Coinbase ticks (within 1.5s):
    - Identify CB direction (UP or DN)
    - Buy the OPPOSITE side (fade) at the ask using "market order" FAK
    - Hold to window resolution
    - One trade per window, first signal wins

Why this strategy:
  - Validated in realistic paper engine (with 150ms latency + stale-ask rejection)
  - 79% WR (n=86 filled trades)
  - +$0.221/share avg P&L
  - 22.1 percentage points margin above break-even WR (57%)
  - +$195/day at 10sh, ~$137/day at 7sh in paper

Per-trade size: 7 shares
Entry: taker FAK with +$0.05 cap (gets price improvement on actual best ask)
Exit: hold to window resolution (no TP, no SL, no hedge)

This is S2-only — we deliberately dropped S6 because its margin (1pp) is too
thin to survive live execution variance.
"""

import asyncio
import json
import time
import os
import sys
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
from py_clob_client_v2.order_builder.constants import BUY

from order_engine import LiveOrderEngine, Side, OrderType as EngineOrderType, OrderStatus

# ── Config ─────────────────────────────────────────────
import math

SHARES_PER_TRADE = 6
S7_THRESHOLD = 18.0           # CB move threshold (raised from S2's $15 for more margin)
MAX_GAP_MS = 1500             # Max ms between consecutive CB ticks
FAK_PRICE_CAP_MARKUP = 0.05   # "Market order" — bid up to $0.05 above recorded ask
MIN_NOTIONAL_TARGET = 1.20    # Scale shares up so notional >= $1.20 (Polymarket $1 min + cushion)
MAX_FADE_ASK = 0.60           # Skip signals where fade ask > this — expensive fades are EV-negative at 54% WR CI lower bound

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", os.getenv("FUNDER", ""))
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CB_WS_URL = "wss://ws-feed.exchange.coinbase.com"

BOT_NAME = "BTC-S7-FADE"
LOG_FILE = "logs/btc_s7_trades.jsonl"
SUMMARY_FILE = "logs/btc_s7_summary.json"
os.makedirs("logs", exist_ok=True)

sys.stdout.reconfigure(line_buffering=True)


def log_msg(msg: str):
    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def snap_price(p: float) -> float:
    return round(round(p * 100) / 100, 2)


def calc_taker_fee(price: float, shares: float) -> float:
    # Crypto taker fee per Polymarket docs: fee = feeRate × p × (1-p) × shares (parabolic).
    # Crypto feeRate = 0.07. Was 0.022 × min(p,1-p) — wrong twice (rate AND shape).
    # Verified against docs 2026-05-26.
    return 0.07 * price * (1 - price) * shares


async def get_market_for_window(window_start: int):
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


class S7Bot:
    def __init__(self):
        self.client = None
        self.engine = None
        if PRIVATE_KEY:
            self.client = ClobClient(CLOB_HOST, chain_id=CHAIN_ID, key=PRIVATE_KEY,
                                     signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS)
            try:
                self.client.set_api_creds(self.client.create_or_derive_api_key())
                self.engine = LiveOrderEngine(self.client, signature_type=SIGNATURE_TYPE)
                log_msg(f"[CLOB] Auth OK — LIVE execution ready")
            except Exception as e:
                log_msg(f"[CLOB] Auth failed: {e}")
                self.client = None
                self.engine = None
        self.up_bid = 0.0
        self.up_ask = 0.0
        self.down_bid = 0.0
        self.down_ask = 0.0
        self.cb_price = 0.0
        self.prev_cb_price = 0.0
        self.prev_cb_ms = 0
        self.fired_this_window = False
        self.trade_id = 0
        self.bankroll_start = 0.0
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

    def place_market_order(self, token_id: str, ask_price: float, shares: int,
                           token_label: str = "", signal_at_ms: int = 0) -> dict:
        """
        Single FAK with price-improvement (Polymarket "market order" equivalent).
        Bids up to $ask + $0.05 to absorb latency-induced ask movement. Polymarket
        fills at actual best ask (not our limit). If real ask jumped > $0.05 during
        latency, FAK rejects — safer than escalation chains.

        Delegates to LiveOrderEngine so paper-mode bots can use the identical call
        path with PaperOrderEngine instead.

        Timing instrumentation (added 2026-05-26):
          signal_at_ms     — when our code decided to act (caller-provided)
          placed_at_ms     — local time JUST before SDK place_order call
          response_at_ms   — local time IMMEDIATELY after SDK returns
          latency_ms       — response_at_ms - placed_at_ms (signing + net + match)
          signal_to_placed_ms — placed_at_ms - signal_at_ms (our decision overhead)
        These flow into the trade record written to btc_s7_trades.jsonl.
        """
        self.trade_id += 1
        tid = self.trade_id
        result = {"tid": tid, "token_id": token_id, "ask": ask_price, "shares": shares,
                  "filled_size": 0, "fill_price": 0, "fee": 0,
                  "time_ms": int(time.time() * 1000),
                  # NEW: timing fields (always present; 0 means "not measured")
                  "signal_at_ms": signal_at_ms,
                  "placed_at_ms": 0, "response_at_ms": 0,
                  "latency_ms": 0, "signal_to_placed_ms": 0,
                  "outcome": "no_client"}
        if not self.engine:
            log_msg(f"[NO-CLIENT] #{tid} would buy {shares}sh @ ~${ask_price:.2f}")
            return result

        bid_price = snap_price(min(ask_price + FAK_PRICE_CAP_MARKUP, 0.98))
        # Option B: scale shares up to meet Polymarket's $1 min order notional.
        # Cheap-fade entries (e.g., $0.10) at the base 6 shares = $0.60 → rejected.
        # Scale: shares = max(SHARES_PER_TRADE, ceil(MIN_NOTIONAL_TARGET / bid_price))
        scaled_shares = max(shares, math.ceil(MIN_NOTIONAL_TARGET / bid_price)) if bid_price > 0 else shares
        if scaled_shares != shares:
            log_msg(f"[S7-SCALE] #{tid} bid ${bid_price:.2f} too cheap for {shares}sh "
                    f"(notional ${shares * bid_price:.2f}) — scaling to {scaled_shares}sh "
                    f"(notional ${scaled_shares * bid_price:.2f})")
        result["shares"] = scaled_shares
        # NEW: capture placement timestamp immediately before the SDK call.
        placed_at_ms = int(time.time() * 1000)
        result["placed_at_ms"] = placed_at_ms
        if signal_at_ms:
            result["signal_to_placed_ms"] = placed_at_ms - signal_at_ms
        try:
            order = self.engine.place_order(
                token_id=token_id, token_label=token_label,
                side=Side.BUY, price=bid_price, size=scaled_shares,
                order_type=EngineOrderType.FAK,
            )
            # NEW: capture response timestamp the instant the SDK returns
            response_at_ms = int(time.time() * 1000)
            result["response_at_ms"] = response_at_ms
            result["latency_ms"] = response_at_ms - placed_at_ms
            if order.filled_size > 0:
                result["filled_size"] = order.filled_size
                result["fill_price"] = order.fill_avg_price
                result["fee"] = calc_taker_fee(result["fill_price"], order.filled_size)
                result["outcome"] = "filled"
                log_msg(f"[S7-FADE] #{tid} BOUGHT {order.filled_size:.0f}sh @ "
                        f"${result['fill_price']:.2f} (cap ${bid_price:.2f}, "
                        f"fee ${result['fee']:.3f}) "
                        f"latency={result['latency_ms']}ms "
                        f"sig_to_place={result['signal_to_placed_ms']}ms")
            else:
                result["outcome"] = f"nofill_{order.status.name}"
                log_msg(f"[S7-NOFILL] #{tid} FAK at ${bid_price:.2f} — no match "
                        f"(status={order.status.name}) latency={result['latency_ms']}ms")
        except Exception as e:
            # NEW: capture response_at_ms even on exception so we know how long the SDK took to throw
            response_at_ms = int(time.time() * 1000)
            result["response_at_ms"] = response_at_ms
            result["latency_ms"] = response_at_ms - placed_at_ms
            err = str(e)[:120]
            if "no orders found to match" in err:
                result["outcome"] = "rejected_no_match"
                log_msg(f"[S7-NOFILL] #{tid} real ask jumped > ${bid_price:.2f} during latency "
                        f"({result['latency_ms']}ms)")
            elif "min size" in err or "invalid amount" in err:
                result["outcome"] = "rejected_min_notional"
                log_msg(f"[S7-FAIL] #{tid} {err} ({result['latency_ms']}ms)")
            else:
                result["outcome"] = "exception"
                log_msg(f"[S7-FAIL] #{tid} {err} ({result['latency_ms']}ms)")
        return result

    def record_trade(self, trade: dict, exit_price: float, direction: str, reason: str):
        rec = {
            "tid": trade["tid"], "strategy": "S7-FADE", "direction": direction,
            "token_id": trade["token_id"], "shares": trade["filled_size"],
            "fill_price": trade["fill_price"], "fee": trade["fee"],
            "exit_price": exit_price, "reason": reason,
            "expected_pnl": (exit_price - trade["fill_price"]) * trade["filled_size"] - trade["fee"],
            "time": datetime.now(timezone.utc).isoformat(),
            # NEW: timing instrumentation
            "signal_at_ms": trade.get("signal_at_ms", 0),
            "placed_at_ms": trade.get("placed_at_ms", 0),
            "response_at_ms": trade.get("response_at_ms", 0),
            "latency_ms": trade.get("latency_ms", 0),
            "signal_to_placed_ms": trade.get("signal_to_placed_ms", 0),
            "outcome": trade.get("outcome", ""),
        }
        self.log_file.write(json.dumps(rec) + "\n")
        self.log_file.flush()

    def record_attempt(self, trade: dict, direction: str, reason: str):
        """Write a record for an attempt that DIDN'T result in a held position
        (rejections, no-fills, min-notional skips). Lets us measure full latency
        distribution including unfilled paths."""
        rec = {
            "tid": trade["tid"], "strategy": "S7-FADE", "direction": direction,
            "token_id": trade["token_id"], "shares": trade["shares"],
            "fill_price": 0, "fee": 0,
            "exit_price": 0, "reason": reason,
            "expected_pnl": 0,
            "time": datetime.now(timezone.utc).isoformat(),
            "signal_at_ms": trade.get("signal_at_ms", 0),
            "placed_at_ms": trade.get("placed_at_ms", 0),
            "response_at_ms": trade.get("response_at_ms", 0),
            "latency_ms": trade.get("latency_ms", 0),
            "signal_to_placed_ms": trade.get("signal_to_placed_ms", 0),
            "outcome": trade.get("outcome", ""),
        }
        self.log_file.write(json.dumps(rec) + "\n")
        self.log_file.flush()


async def run_window(bot: S7Bot, window_start: int):
    window_end = window_start + 300
    market = await get_market_for_window(window_start)
    if not market:
        log_msg(f"[NO-MARKET] window {window_start}")
        return
    up_token = market["up"]
    down_token = market["down"]
    log_msg(f"[WINDOW] {market['question']}")

    bot.pre_approve(up_token, down_token)
    bot.up_bid = bot.up_ask = bot.down_bid = bot.down_ask = 0
    bot.cb_price = bot.prev_cb_price = 0
    bot.prev_cb_ms = 0
    bot.fired_this_window = False

    held_trade = None
    held_direction = None
    running = True

    async def cb_ws():
        nonlocal held_trade, held_direction
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

                        if (bot.prev_cb_price > 0
                                and (now_ms - bot.prev_cb_ms) <= MAX_GAP_MS
                                and not bot.fired_this_window):
                            move = new_price - bot.prev_cb_price
                            if abs(move) >= S7_THRESHOLD:
                                # Fade — buy OPPOSITE direction
                                if move > 0:  # CB UP → fade by buying DOWN
                                    fade_token = down_token
                                    fade_ask = bot.down_ask
                                    fade_label = "DN"
                                else:
                                    fade_token = up_token
                                    fade_ask = bot.up_ask
                                    fade_label = "UP"
                                if fade_ask > 0 and fade_ask < 1:
                                    # Cap entry to keep EV positive at the 54% CI lower bound.
                                    # Paper-validated: dropping fade>$0.60 doubles worst-case
                                    # EV ($23 → $40/day) with no loss in observed P&L.
                                    if fade_ask > MAX_FADE_ASK:
                                        log_msg(f"[S7-SKIP] CB {'UP' if move > 0 else 'DN'} "
                                                f"${move:+.2f} | fade {fade_label} ask "
                                                f"${fade_ask:.2f} > cap ${MAX_FADE_ASK:.2f}")
                                        bot.fired_this_window = True
                                        continue
                                    # NEW: capture the precise ms when our code decided
                                    signal_at_ms = int(time.time() * 1000)
                                    log_msg(f"[S7-SIGNAL] CB {'UP' if move > 0 else 'DN'} "
                                            f"${move:+.2f} | FADE {fade_label} @ ${fade_ask:.2f} "
                                            f"@ {signal_at_ms}ms")
                                    trade = bot.place_market_order(fade_token, fade_ask,
                                                                   SHARES_PER_TRADE,
                                                                   token_label=fade_label,
                                                                   signal_at_ms=signal_at_ms)
                                    if trade["filled_size"] > 0:
                                        held_trade = trade
                                        held_direction = fade_label
                                    else:
                                        # NEW: log unfilled/rejected attempts so we capture
                                        # latency distribution across ALL paths
                                        bot.record_attempt(trade, fade_label,
                                                           f"NOFILL ({trade.get('outcome', '')})")
                                    bot.fired_this_window = True

                        bot.prev_cb_price = new_price
                        bot.prev_cb_ms = now_ms
                        bot.cb_price = new_price
                    except Exception:
                        pass
        except Exception as e:
            log_msg(f"[CB-WS] Error: {str(e)[:80]}")

    async def poly_ws():
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
        except Exception as e:
            log_msg(f"[POLY-WS] Error: {str(e)[:80]}")

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

    if held_trade:
        final_bid = bot.up_bid if held_direction == "UP" else bot.down_bid
        if final_bid >= 0.95:
            expected_redemption = 1.00
        elif final_bid <= 0.05:
            expected_redemption = 0.00
        else:
            expected_redemption = final_bid
        bot.record_trade(held_trade, expected_redemption, held_direction,
                         f"HOLD-RESOLUTION ({held_direction}, final_bid=${final_bid:.2f})")
        pnl = (expected_redemption - held_trade["fill_price"]) * held_trade["filled_size"] - held_trade["fee"]
        log_msg(f"[RESOLVE] #{held_trade['tid']} {held_direction} | "
                f"entry ${held_trade['fill_price']:.2f} → expected ${expected_redemption:.2f} | "
                f"P&L ${pnl:+.2f}")
    else:
        log_msg(f"[WINDOW-END] No S7 trigger this window")


async def main():
    log_msg(f"=== {BOT_NAME} starting ===")
    log_msg(f"=== S7 = S2 fade-only at $18 threshold, base {SHARES_PER_TRADE}sh per trade ===")
    log_msg(f"=== Option B: scales shares up to keep notional >= ${MIN_NOTIONAL_TARGET:.2f} (Polymarket $1 min) ===")
    log_msg(f"=== Paper-validated: 70% WR (CI [54-83%]), +$0.20/share, ~$27/day ===")
    bot = S7Bot()
    bot.bankroll_start = bot.get_balance()
    log_msg(f"[WALLET] Balance: ${bot.bankroll_start:.2f}")

    if not bot.client:
        log_msg("[WARN] No CLOB client — running in DRY mode (no orders placed)")

    while True:
        now = int(time.time())
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
