#!/usr/bin/env python3
"""
BTC-COINBASE-SCALP — Scalp Polymarket tokens on Coinbase BTC price signals
==========================================================================
Strategy:
  - Monitor Coinbase BTC/USD websocket for moves > $5 in 1 second
  - Buy corresponding Polymarket side (UP if BTC rises, DOWN if BTC drops)
  - GTC maker limit buy at current best bid (0% fee)
  - Cancel buy if unfilled after 4 seconds
  - On fill: place GTC maker TP sell at entry + $0.04
  - Websocket monitors SL at entry - $0.03 → FAK taker sell
  - Sequential: one trade at a time
  - No trades after T+280 (20s before window end)
  - Force exit open positions before resolution
  - Entry price bounds: $0.15 - $0.85 only

PAPER MODE REALISM:
  - 125ms simulated latency on every operation (order place, fill check, cancel)
  - Book depth from websocket used to determine fill/no-fill
  - Ask-side depth checked for buy fills (we need sellers at our price)
  - Bid-side depth checked for TP sell fills (we need buyers at our price)
  - Slippage: price checked AFTER latency delay, not at signal time
  - Taker fee simulated on SL/force-exit sells (~2% of min(price, 1-price))

REVISIT:
  - If TP sell never fills and price drifts down, currently handled by SL.
    Consider executing a market order at entry price instead.
  - Partial TP fills: currently waits for full fill.
    Consider selling remainder at market.
  - Concurrent positions: currently sequential, one at a time.
"""

import asyncio
import json
import time
import os
import sys
import csv
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

try:
    from dotenv import load_dotenv
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-dotenv", "-q"])
    from dotenv import load_dotenv

load_dotenv()

CLOB_AVAILABLE = False
try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import OrderArgs, OrderType, BalanceAllowanceParams
    from py_clob_client_v2.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────
CB_TRIGGER = 5.00           # Coinbase move threshold (dollars)
TP_OFFSET = 0.04            # Take profit: entry + $0.04
SL_OFFSET = 0.03            # Stop loss: entry - $0.03
SHARES_PER_TRADE = 5        # Shares per trade
FILL_TIMEOUT = 4            # Cancel unfilled buy after 4 seconds
MIN_ENTRY_PRICE = 0.15      # Don't buy below this
MAX_ENTRY_PRICE = 0.85      # Don't buy above this
CUTOFF_BEFORE_END = 20      # No new trades within 20s of window end
FORCE_EXIT_BEFORE_END = 5   # Force exit any open position 5s before end

# Paper simulation
PAPER_LATENCY = 0.125       # 125ms simulated execution latency per operation
PAPER_TAKER_FEE_MULT = 0.022  # ~2.2% of min(price, 1-price) for taker orders

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PAPER_MODE = os.getenv("BTC_SCALP_LIVE", "0") != "1"
POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CB_WS_URL = "wss://ws-feed.exchange.coinbase.com"

os.makedirs("logs", exist_ok=True)
BOT_NAME = "BTC-SCALP" if not PAPER_MODE else "BTC-SCALP-PAPER"
LOG_FILE = "logs/btc_scalp_trades.jsonl"
SUMMARY_FILE = "logs/btc_scalp_summary.json"

sys.stdout.reconfigure(line_buffering=True)


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def log_msg(msg):
    print(f"  [{ts()}] {msg}", flush=True)

def snap_price(price):
    return round(max(0.01, min(0.99, round(price * 100) / 100)), 2)

def calc_taker_fee(price, shares):
    """Approximate Polymarket taker fee."""
    return round(min(price, 1.0 - price) * PAPER_TAKER_FEE_MULT * shares, 4)


async def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        async with aiohttp.ClientSession() as s:
            async with s.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                              data=data, headers={"Content-Type": "application/json"},
                              timeout=aiohttp.ClientTimeout(total=10)):
                pass
    except Exception:
        pass


async def get_book(token_id):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://clob.polymarket.com/book?token_id={token_id}",
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    bb = max((float(b["price"]) for b in bids), default=0)
                    ba = min((float(a["price"]) for a in asks), default=0)
                    return {"bid": bb, "ask": ba}
    except Exception:
        pass
    return None


class MarketFinder:
    def __init__(self):
        self.market = None

    async def refresh(self):
        try:
            now = int(time.time())
            window_start = (now // 300) * 300
            slug = f"btc-updown-5m-{window_start}"
            async with aiohttp.ClientSession() as s:
                async with s.get(f"https://gamma-api.polymarket.com/markets?slug={slug}",
                                 headers={"User-Agent": "Mozilla/5.0"},
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            m = data[0]
                            tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                            outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
                            up = tokens[0] if outcomes[0] == "Up" else tokens[1]
                            down = tokens[1] if outcomes[0] == "Up" else tokens[0]
                            self.market = {
                                "up": up, "down": down,
                                "question": m.get("question", ""),
                                "window_start": window_start,
                            }
        except Exception as e:
            log_msg(f"[MKT] {e}")


class BTCScalpBot:
    def __init__(self):
        self.mf = MarketFinder()
        self.client = None
        self.bankroll = 100.0
        self.starting_bankroll = 100.0
        self.peak = 100.0
        self.max_dd = 0
        self.wins = 0
        self.losses = 0
        self.flat = 0
        self.trade_count = 0
        self.tp_count = 0
        self.sl_count = 0
        self.timeout_count = 0
        self.fill_timeout_count = 0
        self.partial_fill_count = 0
        self.start_time = time.time()
        self.log_file = open(LOG_FILE, "a")

        # Shared state — updated by websockets
        self.cb_price = 0.0
        self.prev_cb_price = 0.0
        self.cb_updated_at = 0.0
        self.up_bid = 0.0
        self.down_bid = 0.0
        self.up_ask = 0.0
        self.down_ask = 0.0
        # Book depth: {price: size} for bids and asks per side
        self.up_bids_depth = {}    # {0.55: 100, 0.54: 50, ...}
        self.up_asks_depth = {}    # {0.56: 80, 0.57: 120, ...}
        self.down_bids_depth = {}
        self.down_asks_depth = {}
        self.in_trade = False

    def _get_depth(self, direction, side):
        """Get depth dict: direction=UP/DN, side=bids/asks."""
        if direction == "UP":
            return self.up_bids_depth if side == "bids" else self.up_asks_depth
        else:
            return self.down_bids_depth if side == "bids" else self.down_asks_depth

    def _available_at_price(self, direction, side, price):
        """How many shares available at exactly this price level?"""
        depth = self._get_depth(direction, side)
        return depth.get(price, 0)

    def _available_at_or_better(self, direction, side, price):
        """How many shares available at this price or better?
        For asks (buying): shares at price or lower.
        For bids (selling): shares at price or higher."""
        depth = self._get_depth(direction, side)
        total = 0
        for p, sz in depth.items():
            if side == "asks" and p <= price:
                total += sz
            elif side == "bids" and p >= price:
                total += sz
        return total

    def init_clob(self):
        if PAPER_MODE:
            log_msg("[CLOB] PAPER MODE — realistic simulation")
            log_msg(f"[CLOB] Latency: {PAPER_LATENCY*1000:.0f}ms | Book depth fills | Taker fee on SL")
            return
        if not CLOB_AVAILABLE or not PRIVATE_KEY:
            log_msg("[CLOB] No client — paper mode")
            return
        try:
            self.client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                                     signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS)
            self.client.set_api_creds(self.client.create_or_derive_api_key())
            log_msg("[CLOB] Auth OK — LIVE execution ready")
            try:
                params = BalanceAllowanceParams(asset_type="COLLATERAL", token_id="", signature_type=2)
                result = self.client.get_balance_allowance(params)
                bal = int(result.get("balance", "0")) / 1_000_000
                if bal > 0:
                    self.bankroll = bal
                    self.peak = bal
                    self.starting_bankroll = bal
                    log_msg(f"[WALLET] Balance: ${bal:.2f}")
            except:
                pass
        except Exception as e:
            log_msg(f"[CLOB] Auth fail: {e}")

    async def run(self):
        log_msg(f"[INIT] {BOT_NAME}")
        log_msg(f"[INIT] Trigger: CB move > ${CB_TRIGGER} | TP: +${TP_OFFSET} | SL: -${SL_OFFSET}")
        log_msg(f"[INIT] Shares: {SHARES_PER_TRADE} | Fill timeout: {FILL_TIMEOUT}s | Entry: ${MIN_ENTRY_PRICE}-${MAX_ENTRY_PRICE}")
        mode = "LIVE" if not PAPER_MODE else "PAPER"
        log_msg(f"[INIT] {mode} mode — Bank: ${self.bankroll:.2f}")

        while True:
            try:
                now = time.time()
                nxt = (int(now) // 300 + 1) * 300
                wait = nxt - now
                if wait > 0 and wait < 300:
                    log_msg(f"[SYNC] Waiting {wait:.0f}s for window boundary...")
                    await asyncio.sleep(wait)
                await asyncio.sleep(2)

                await self.mf.refresh()
                if not self.mf.market:
                    log_msg("[LOOP] No market found")
                    continue

                mkt = dict(self.mf.market)
                window_start = mkt["window_start"]
                window_end = window_start + 300
                log_msg(f"[LOOP] Window start | {mkt['question'][:50]} | Bank: ${self.bankroll:.2f}")

                await self._trade_window(mkt, window_start, window_end)
                self._print_status()

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(5)

    async def _trade_window(self, mkt, window_start, window_end):
        """Run one 5-minute trading window."""
        up_token = mkt["up"]
        down_token = mkt["down"]
        cutoff_time = window_end - CUTOFF_BEFORE_END
        force_exit_time = window_end - FORCE_EXIT_BEFORE_END

        self.prev_cb_price = 0.0
        self.cb_updated_at = 0.0
        # Reset book depth for new window tokens
        self.up_bids_depth = {}
        self.up_asks_depth = {}
        self.down_bids_depth = {}
        self.down_asks_depth = {}

        signal_queue = asyncio.Queue()

        # ── COINBASE WEBSOCKET ──
        async def cb_ws():
            retry = 0
            while time.time() < window_end:
                try:
                    async with websockets.connect(CB_WS_URL, ping_interval=20, ping_timeout=15) as ws:
                        await ws.send(json.dumps({
                            "type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]
                        }))
                        if retry == 0:
                            log_msg("[CB-WS] Connected")
                        else:
                            log_msg(f"[CB-WS] Reconnected ({retry})")
                        retry = 0

                        async for raw in ws:
                            if time.time() >= window_end:
                                return
                            try:
                                msg = json.loads(raw)
                                if msg.get("type") == "ticker" and "price" in msg:
                                    new_price = float(msg["price"])
                                    now = time.time()

                                    if self.prev_cb_price > 0 and now - self.cb_updated_at <= 1.5:
                                        move = new_price - self.prev_cb_price
                                        if abs(move) >= CB_TRIGGER and not self.in_trade and now < cutoff_time:
                                            direction = "UP" if move > 0 else "DN"
                                            await signal_queue.put({
                                                "direction": direction,
                                                "cb_move": move,
                                                "cb_price": new_price,
                                                "time": now,
                                            })

                                    self.prev_cb_price = new_price
                                    self.cb_price = new_price
                                    self.cb_updated_at = now
                            except:
                                pass
                except Exception as e:
                    if time.time() < window_end:
                        retry += 1
                        log_msg(f"[CB-WS] Dropped: {str(e)[:40]} — reconnecting ({retry})...")
                        await asyncio.sleep(min(retry * 0.5, 3))

        # ── POLYMARKET WEBSOCKET (price + depth tracking) ──
        async def poly_ws():
            retry = 0
            while time.time() < window_end + 10:
                try:
                    async with websockets.connect(POLY_WS_URL, ping_interval=20, ping_timeout=15,
                                                  close_timeout=5) as ws:
                        await ws.send(json.dumps({"assets_ids": [up_token, down_token], "type": "market"}))
                        if retry == 0:
                            log_msg("[POLY-WS] Connected — tracking bids, asks, and depth")
                        else:
                            log_msg(f"[POLY-WS] Reconnected ({retry})")
                        retry = 0

                        async for raw in ws:
                            if time.time() >= window_end + 10:
                                return
                            try:
                                msg = json.loads(raw)
                            except:
                                continue
                            items = []
                            if isinstance(msg, list):
                                items = [m for m in msg if isinstance(m, dict)]
                            elif isinstance(msg, dict) and ("bids" in msg or "asks" in msg):
                                items = [msg]
                            for item in items:
                                aid = item.get("asset_id", "")
                                is_up = aid == up_token
                                is_down = aid == down_token
                                if not is_up and not is_down:
                                    continue

                                # Update bids
                                bids_data = item.get("bids", [])
                                if bids_data:
                                    depth = self.up_bids_depth if is_up else self.down_bids_depth
                                    for b in bids_data:
                                        if isinstance(b, dict) and "price" in b and "size" in b:
                                            p = float(b["price"])
                                            sz = float(b["size"])
                                            if sz > 0:
                                                depth[p] = sz
                                            elif p in depth:
                                                del depth[p]
                                    try:
                                        bb = max(depth.keys()) if depth else 0
                                        if is_up:
                                            self.up_bid = bb
                                        else:
                                            self.down_bid = bb
                                    except ValueError:
                                        pass

                                # Update asks
                                asks_data = item.get("asks", [])
                                if asks_data:
                                    depth = self.up_asks_depth if is_up else self.down_asks_depth
                                    for a in asks_data:
                                        if isinstance(a, dict) and "price" in a and "size" in a:
                                            p = float(a["price"])
                                            sz = float(a["size"])
                                            if sz > 0:
                                                depth[p] = sz
                                            elif p in depth:
                                                del depth[p]
                                    try:
                                        ba = min(depth.keys()) if depth else 0
                                        if is_up:
                                            self.up_ask = ba
                                        else:
                                            self.down_ask = ba
                                    except ValueError:
                                        pass

                except Exception as e:
                    if time.time() < window_end + 10:
                        retry += 1
                        log_msg(f"[POLY-WS] Dropped: {str(e)[:40]} — reconnecting ({retry})...")
                        await asyncio.sleep(min(retry * 0.5, 3))

        # ── TRADE EXECUTOR ──
        async def trade_executor():
            while time.time() < window_end:
                try:
                    signal = await asyncio.wait_for(signal_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if self.in_trade:
                    continue
                if time.time() >= cutoff_time:
                    continue

                direction = signal["direction"]
                side_bid = self.up_bid if direction == "UP" else self.down_bid

                # Check entry bounds
                if side_bid < MIN_ENTRY_PRICE or side_bid > MAX_ENTRY_PRICE:
                    log_msg(f"[SKIP] {direction} bid ${side_bid:.2f} outside ${MIN_ENTRY_PRICE}-${MAX_ENTRY_PRICE}")
                    continue

                entry_price = snap_price(side_bid)
                tp_price = snap_price(entry_price + TP_OFFSET)
                sl_price = snap_price(entry_price - SL_OFFSET)

                self.in_trade = True
                self.trade_count += 1
                tid = self.trade_count

                log_msg(f"[SIGNAL] #{tid} CB {direction} ${signal['cb_move']:+,.2f} | "
                        f"Entry ${entry_price:.2f} | TP ${tp_price:.2f} | SL ${sl_price:.2f}")

                await self._execute_trade(tid, direction, entry_price, tp_price, sl_price,
                                          signal["cb_move"], mkt["question"], window_start, window_end, force_exit_time)

                self.in_trade = False

        await asyncio.gather(cb_ws(), poly_ws(), trade_executor(), return_exceptions=True)

    async def _execute_trade(self, tid, direction, entry_price, tp_price, sl_price,
                             cb_move, question, window_start, window_end, force_exit_time):
        """Execute a single trade: buy → wait for fill → TP/SL → record."""
        t_start = time.time()
        window_elapsed = round(t_start - window_start, 1)  # seconds into the 5-min window
        token = None  # Not needed for paper but kept for live

        # ── STEP 1: Place GTC limit buy ──
        buy_order_id = None
        if self.client and not PAPER_MODE:
            try:
                token_id = self.mf.market["up"] if direction == "UP" else self.mf.market["down"]
                args = OrderArgs(price=entry_price, size=SHARES_PER_TRADE, side=BUY, token_id=token_id)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.GTC)
                buy_order_id = resp.get("orderID", "")
                log_msg(f"[BUY] #{tid} GTC {SHARES_PER_TRADE}sh @ ${entry_price:.2f} order={buy_order_id[:10]}...")
            except Exception as e:
                log_msg(f"[BUY] #{tid} FAILED: {str(e)[:80]}")
                self._record_trade(tid, direction, entry_price, entry_price, 0, 0, "BUY-FAIL",
                                   0, cb_move, question, window_elapsed)
                return
        else:
            # Paper: simulate 125ms latency for order placement
            await asyncio.sleep(PAPER_LATENCY)
            log_msg(f"[PAPER-BUY] #{tid} GTC {SHARES_PER_TRADE}sh {direction} @ ${entry_price:.2f} "
                    f"(+{PAPER_LATENCY*1000:.0f}ms latency)")

        # ── STEP 2: Wait for fill (max FILL_TIMEOUT seconds) ──
        filled = False
        fill_price = entry_price
        fill_shares = 0

        for check in range(FILL_TIMEOUT * 4):  # Check every 0.25s for finer granularity
            if self.client and not PAPER_MODE and buy_order_id:
                try:
                    order = self.client.get_order(buy_order_id)
                    if order:
                        matched = float(order.get("size_matched", 0))
                        if matched >= SHARES_PER_TRADE:
                            fill_price = float(order.get("price", entry_price))
                            fill_shares = matched
                            filled = True
                            break
                except:
                    pass
            else:
                # Paper: realistic fill simulation
                # We placed a GTC buy at entry_price. For this to fill, there must be
                # sellers (asks) at or below our price. Check ask-side depth.
                current_bid = self.up_bid if direction == "UP" else self.down_bid
                current_ask = self.up_ask if direction == "UP" else self.down_ask

                # Our GTC buy sits at entry_price. It fills when:
                # 1. The ask drops to our price (someone market-sells into us), OR
                # 2. There are already asks at or below our price
                if current_ask > 0 and current_ask <= entry_price:
                    # Asks exist at or below our price — check depth
                    available = self._available_at_or_better(direction, "asks", entry_price)
                    if available >= SHARES_PER_TRADE:
                        fill_shares = SHARES_PER_TRADE
                        fill_price = entry_price  # We're maker, fill at our price
                        filled = True
                        # Simulate fill check latency
                        await asyncio.sleep(PAPER_LATENCY)
                        break
                    elif available > 0:
                        # Partial fill
                        fill_shares = min(available, SHARES_PER_TRADE)
                        fill_price = entry_price
                        filled = True
                        self.partial_fill_count += 1
                        await asyncio.sleep(PAPER_LATENCY)
                        log_msg(f"[PAPER-PARTIAL] #{tid} Only {fill_shares:.0f}/{SHARES_PER_TRADE}sh filled "
                                f"(ask depth: {available:.0f})")
                        break
                elif current_bid >= entry_price:
                    # Bid is at or above our buy price — market has moved through us
                    # In reality our order would have filled as the price crossed
                    available = self._available_at_or_better(direction, "asks", entry_price + 0.01)
                    fill_shares = SHARES_PER_TRADE  # Assume full fill on cross
                    fill_price = entry_price
                    filled = True
                    await asyncio.sleep(PAPER_LATENCY)
                    break

            await asyncio.sleep(0.25)

        if not filled:
            if self.client and not PAPER_MODE and buy_order_id:
                try:
                    self.client.cancel_orders([buy_order_id])
                except:
                    pass
            else:
                await asyncio.sleep(PAPER_LATENCY)  # Cancel latency
            self.fill_timeout_count += 1
            log_msg(f"[UNFILL] #{tid} No fill after {FILL_TIMEOUT}s — cancelled")
            self._record_trade(tid, direction, entry_price, entry_price, 0, 0, "UNFILLED",
                               0, cb_move, question, window_elapsed)
            return

        # Recalculate TP/SL based on actual fill price
        tp_price = snap_price(fill_price + TP_OFFSET)
        sl_price = snap_price(fill_price - SL_OFFSET)
        fill_time = time.time() - t_start

        log_msg(f"[FILL] #{tid} {fill_shares:.0f}sh @ ${fill_price:.2f} in {fill_time:.1f}s | "
                f"TP ${tp_price:.2f} | SL ${sl_price:.2f}")

        # ── STEP 3: Pre-approve token for selling ──
        if self.client and not PAPER_MODE:
            try:
                token_id = self.mf.market["up"] if direction == "UP" else self.mf.market["down"]
                params = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id,
                                               signature_type=SIGNATURE_TYPE)
                self.client.update_balance_allowance(params)
            except:
                pass

        # ── STEP 4: Place GTC TP sell ──
        tp_order_id = None
        if self.client and not PAPER_MODE:
            try:
                token_id = self.mf.market["up"] if direction == "UP" else self.mf.market["down"]
                args = OrderArgs(price=tp_price, size=int(fill_shares), side=SELL, token_id=token_id)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.GTC)
                tp_order_id = resp.get("orderID", "")
                log_msg(f"[TP] #{tid} GTC sell {int(fill_shares)}sh @ ${tp_price:.2f} order={tp_order_id[:10]}...")
            except Exception as e:
                log_msg(f"[TP] #{tid} FAILED: {str(e)[:80]}")
        else:
            await asyncio.sleep(PAPER_LATENCY)  # TP placement latency
            log_msg(f"[PAPER-TP] #{tid} GTC sell {int(fill_shares)}sh @ ${tp_price:.2f}")

        # ── STEP 5: Monitor for TP fill or SL trigger ──
        exited = False
        exit_price = fill_price
        exit_reason = "TIMEOUT"
        taker_fee = 0

        while not exited and time.time() < window_end:
            now = time.time()

            # Force exit before resolution
            if now >= force_exit_time:
                log_msg(f"[FORCE-EXIT] #{tid} Window ending — exiting at market")
                if tp_order_id and self.client and not PAPER_MODE:
                    try:
                        self.client.cancel_orders([tp_order_id])
                    except:
                        pass
                current_bid = self.up_bid if direction == "UP" else self.down_bid
                if self.client and not PAPER_MODE:
                    try:
                        token_id = self.mf.market["up"] if direction == "UP" else self.mf.market["down"]
                        sell_p = snap_price(current_bid - 0.01)
                        args = OrderArgs(price=sell_p, size=int(fill_shares), side=SELL, token_id=token_id)
                        signed = self.client.create_order(args)
                        self.client.post_order(signed, OrderType.FOK)
                        exit_price = sell_p
                    except Exception as e:
                        log_msg(f"[FORCE-EXIT] #{tid} Sell failed: {str(e)[:60]}")
                        exit_price = current_bid
                else:
                    await asyncio.sleep(PAPER_LATENCY)  # Cancel + sell latency
                    # Paper: sell at bid - 0.01 (slippage), pay taker fee
                    exit_price = snap_price(current_bid - 0.01)
                    taker_fee = calc_taker_fee(exit_price, fill_shares)
                    log_msg(f"[PAPER-FORCE-EXIT] #{tid} Sell @ ${exit_price:.2f} (fee ${taker_fee:.3f})")
                exit_reason = "FORCE-EXIT"
                exited = True
                break

            # Check TP fill
            if self.client and not PAPER_MODE and tp_order_id:
                try:
                    order = self.client.get_order(tp_order_id)
                    if order:
                        matched = float(order.get("size_matched", 0))
                        if matched >= fill_shares:
                            exit_price = tp_price
                            exit_reason = "TP"
                            exited = True
                            break
                except:
                    pass
            else:
                # Paper: TP fills when bid reaches our TP price AND there's depth
                current_bid = self.up_bid if direction == "UP" else self.down_bid
                if current_bid >= tp_price:
                    # Check: are there buyers at our TP price?
                    bid_depth = self._available_at_or_better(direction, "bids", tp_price)
                    if bid_depth >= fill_shares:
                        await asyncio.sleep(PAPER_LATENCY)  # Fill detection latency
                        exit_price = tp_price
                        exit_reason = "TP"
                        # TP is maker sell — 0% fee
                        exited = True
                        break
                    elif bid_depth > 0:
                        # Partial TP — for now wait for full fill per spec
                        pass

            # Check SL trigger (websocket bid)
            current_bid = self.up_bid if direction == "UP" else self.down_bid
            if current_bid <= sl_price and sl_price > 0.01:
                log_msg(f"[SL-TRIGGER] #{tid} Bid ${current_bid:.2f} <= SL ${sl_price:.2f}")

                if self.client and not PAPER_MODE:
                    # Cancel TP
                    if tp_order_id:
                        try:
                            self.client.cancel_orders([tp_order_id])
                        except:
                            pass
                    # Re-approve + FAK sell
                    try:
                        token_id = self.mf.market["up"] if direction == "UP" else self.mf.market["down"]
                        params = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id,
                                                       signature_type=SIGNATURE_TYPE)
                        self.client.update_balance_allowance(params)
                    except:
                        pass
                    try:
                        sell_p = snap_price(current_bid - 0.01)
                        args = OrderArgs(price=sell_p, size=int(fill_shares), side=SELL, token_id=token_id)
                        signed = self.client.create_order(args)
                        self.client.post_order(signed, OrderType.FOK)
                        exit_price = sell_p
                    except Exception as e:
                        log_msg(f"[SL-SELL] #{tid} FAILED: {str(e)[:60]}")
                        exit_price = current_bid
                else:
                    # Paper: simulate cancel TP + SL sell with latency
                    await asyncio.sleep(PAPER_LATENCY * 2)  # Cancel TP + place SL sell
                    # Re-read bid after latency (price may have moved further)
                    current_bid_after = self.up_bid if direction == "UP" else self.down_bid
                    # SL is taker — sell at bid - 0.01 with slippage
                    exit_price = snap_price(min(current_bid, current_bid_after) - 0.01)
                    if exit_price < 0.01:
                        exit_price = 0.01
                    taker_fee = calc_taker_fee(exit_price, fill_shares)
                    log_msg(f"[PAPER-SL] #{tid} Sell @ ${exit_price:.2f} after {PAPER_LATENCY*2*1000:.0f}ms "
                            f"(bid moved ${current_bid:.2f}→${current_bid_after:.2f}, fee ${taker_fee:.3f})")

                exit_reason = "SL"
                exited = True
                break

            # Check for resolution
            if current_bid >= 0.95:
                if tp_order_id and self.client and not PAPER_MODE:
                    try:
                        self.client.cancel_orders([tp_order_id])
                    except:
                        pass
                exit_price = 0.99
                exit_reason = "RES-WIN"
                exited = True
                break
            if current_bid <= 0.05:
                if tp_order_id and self.client and not PAPER_MODE:
                    try:
                        self.client.cancel_orders([tp_order_id])
                    except:
                        pass
                exit_price = 0.01
                exit_reason = "RES-LOSS"
                exited = True
                break

            await asyncio.sleep(0.3)

        # ── STEP 6: Record trade ──
        hold_time = time.time() - t_start
        self._record_trade(tid, direction, fill_price, exit_price, fill_shares, taker_fee,
                           exit_reason, hold_time, cb_move, question, window_elapsed)

    def _record_trade(self, tid, direction, entry, exit_price, shares, taker_fee, reason,
                      hold_time, cb_move, question, window_elapsed=0):
        cost = round(entry * shares, 4)
        revenue = round(exit_price * shares, 4)
        pnl = round(revenue - cost - taker_fee, 4)

        if reason in ("UNFILLED", "BUY-FAIL"):
            pnl = 0
            log_msg(f"[{reason}] #{tid} {direction} — no position taken")
        else:
            self.bankroll = round(self.bankroll + pnl, 4)

            if pnl > 0.001:
                self.wins += 1
            elif pnl < -0.001:
                self.losses += 1
            else:
                self.flat += 1

            if reason == "TP":
                self.tp_count += 1
            elif reason == "SL":
                self.sl_count += 1
            elif reason in ("TIMEOUT", "FORCE-EXIT", "RES-WIN", "RES-LOSS"):
                self.timeout_count += 1

            if self.bankroll > self.peak:
                self.peak = self.bankroll
            dd = (self.peak - self.bankroll) / self.peak * 100 if self.peak > 0 else 0
            if dd > self.max_dd:
                self.max_dd = dd

            total = self.wins + self.losses + self.flat
            wr = self.wins / total * 100 if total else 0
            fee_str = f" (fee ${taker_fee:.3f})" if taker_fee > 0 else ""

            log_msg(f"[{reason}] #{tid} {direction} | ${entry:.2f} → ${exit_price:.2f}{fee_str} | "
                    f"P&L ${pnl:+.2f} | {hold_time:.1f}s | T+{window_elapsed:.0f}s | "
                    f"bank ${self.bankroll:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%)")

        # Log to file
        try:
            self.log_file.write(json.dumps({
                "id": tid, "direction": direction, "entry": entry, "exit": exit_price,
                "shares": shares, "cost": cost, "revenue": revenue, "taker_fee": taker_fee,
                "pnl": pnl, "reason": reason, "hold_time": round(hold_time, 2),
                "cb_move": round(cb_move, 2),
                "window_elapsed": round(window_elapsed, 1),
                "entry_bucket": "low" if entry <= 0.30 else "mid" if entry <= 0.60 else "high",
                "bankroll": self.bankroll, "question": question,
                "paper": PAPER_MODE,
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except:
            pass

        self._write_summary()

        # Telegram
        if reason not in ("UNFILLED", "BUY-FAIL"):
            icon = "\U0001F7E2" if pnl > 0 else "\U0001F534" if pnl < 0 else "\u26AA"
            total = self.wins + self.losses + self.flat
            wr = self.wins / total * 100 if total else 0
            asyncio.create_task(send_telegram(
                f"{icon} <b>{BOT_NAME} #{tid}</b>\n"
                f"{direction} | ${entry:.2f} → ${exit_price:.2f} ({reason})\n"
                f"P&L: ${pnl:+.2f} | Hold: {hold_time:.1f}s\n"
                f"CB move: ${cb_move:+,.2f}\n"
                f"Bank: ${self.bankroll:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%)"))

        # Sync wallet (live only)
        if self.client and not PAPER_MODE:
            try:
                params = BalanceAllowanceParams(asset_type="COLLATERAL", token_id="", signature_type=2)
                result = self.client.get_balance_allowance(params)
                bal = int(result.get("balance", "0")) / 1_000_000
                if bal > 0:
                    self.bankroll = bal
            except:
                pass

    def _print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses + self.flat
        wr = self.wins / total * 100 if total else 0

        print(f"\n  [{ts()}] {'='*60}", flush=True)
        print(f"  {BOT_NAME} | {elapsed:.0f}min | {'LIVE' if not PAPER_MODE else 'PAPER'}", flush=True)
        print(f"  CB trigger: >${CB_TRIGGER} | TP: +${TP_OFFSET} | SL: -${SL_OFFSET} | {SHARES_PER_TRADE}sh", flush=True)
        print(f"  Bank: ${self.bankroll:.2f} (${self.bankroll - self.starting_bankroll:+.2f}) | "
              f"Peak: ${self.peak:.2f} | DD: {self.max_dd:.1f}%", flush=True)
        print(f"  Trades: {total} ({self.wins}W/{self.losses}L/{self.flat}F) WR: {wr:.0f}% | "
              f"TP: {self.tp_count} | SL: {self.sl_count} | Timeout: {self.timeout_count} | "
              f"Unfilled: {self.fill_timeout_count} | Partial: {self.partial_fill_count}", flush=True)
        print(flush=True)

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses + self.flat
        wr = self.wins / total * 100 if total else 0
        summary = {
            "bot": BOT_NAME,
            "mode": "LIVE" if self.client and not PAPER_MODE else "PAPER",
            "elapsed_minutes": round(elapsed, 1),
            "trades": total, "wins": self.wins, "losses": self.losses, "flat": self.flat,
            "win_rate": round(wr, 1),
            "tp_count": self.tp_count, "sl_count": self.sl_count,
            "timeout_count": self.timeout_count, "fill_timeout_count": self.fill_timeout_count,
            "partial_fill_count": self.partial_fill_count,
            "bankroll": self.bankroll, "starting_bankroll": self.starting_bankroll,
            "pnl_total": round(self.bankroll - self.starting_bankroll, 4),
            "peak": self.peak, "max_dd": round(self.max_dd, 1),
            "config": {
                "cb_trigger": CB_TRIGGER, "tp_offset": TP_OFFSET, "sl_offset": SL_OFFSET,
                "shares": SHARES_PER_TRADE, "fill_timeout": FILL_TIMEOUT,
                "entry_range": f"${MIN_ENTRY_PRICE}-${MAX_ENTRY_PRICE}",
                "paper_latency_ms": PAPER_LATENCY * 1000,
            },
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        try:
            tmp = SUMMARY_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(summary, f, indent=2)
            os.replace(tmp, SUMMARY_FILE)
        except:
            pass


async def main():
    bot = BTCScalpBot()
    bot.init_clob()
    await bot.run()

if __name__ == "__main__":
    asyncio.run(main())
