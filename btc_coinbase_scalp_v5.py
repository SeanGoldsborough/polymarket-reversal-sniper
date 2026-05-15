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
  - PRICE HIT COUNTER: Track how many times price touches TP, BE, or SL levels
    before making exit decision. E.g., if price hits SL 3 times but keeps bouncing
    back, that's different from hitting it once and crashing through. Use touch count
    + direction to decide: sell at TP/BE/SL or hold for more data.
  - DYNAMIC ORDER SIZING: Before placing a buy, check ask-side depth and size
    the order to match available liquidity. E.g., if best ask has 80 shares,
    buy 80 not 100. Scale from 30sh up to 100sh based on book depth.
    Benefits: zero slippage (always fill at best price), more shares when book
    is deep, fewer when thin. Implement when scaling up from 30 to 100 shares.
  - VOLATILITY GATE OPTION 3: Track ratio of direction changes to net movement
    over rolling window. High reversals / low net = choppy (skip). Few reversals /
    high net = trending (trade). Can compute from CB feed in real-time. V6 uses
    option 2 (range vs net move + reversal count). Option 3 adds per-window
    tracking across many windows to identify patterns by time of day.
    Use the volume_by_hour.csv data to determine which hours have best depth.
  - MULTI-ASSET SCALING: After V2 BTC proves profitable, duplicate for ETH.
    Run 50sh BTC + 50sh ETH instead of 100sh on one book.
    ETH has good liquidity (50sh slip $0.003, 100sh slip $0.011).
    SOL/XRP/DOGE books are too thin — don't use.
    Need separate Coinbase ETH/USD websocket feed for signals.
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
SL_OFFSET = 0.02            # Stop loss: entry - $0.02 (tighter — data shows minimal TP loss, better recovery)
TIERED_WAIT = 0.05          # Check fill status quickly, react to price
TIER_COUNT = 3              # Split shares into 3 tiers
MAX_HOLD_SECONDS = 30       # Hard exit after 30 seconds if TP not hit — don't hold to resolution
SHARES_PER_TRADE = 69       # Shares per trade (was 5, scaling up based on book depth data)
FILL_TIMEOUT = 4            # Cancel unfilled buy after 4 seconds
MIN_ENTRY_PRICE = 0.30      # Don't buy below this (was 0.15, filtered by data)
MAX_ENTRY_PRICE = 0.85      # Don't buy above this
ENTRY_WINDOW_SECONDS = 60   # Only trade in first 60 seconds of window
CUTOFF_BEFORE_END = 20      # No new trades within 20s of window end
FORCE_EXIT_BEFORE_END = 5   # Force exit any open position 5s before end

# Paper simulation
PAPER_LATENCY = 0.050       # 50ms simulated execution latency (actual API benchmarked at 22-50ms)
PAPER_TAKER_FEE_MULT = 0.022  # ~2.2% of min(price, 1-price) for taker orders

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PAPER_MODE = os.getenv("BTC_SCALP_V5_LIVE", "0") != "1"
POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CB_WS_URL = "wss://ws-feed.exchange.coinbase.com"

os.makedirs("logs", exist_ok=True)
BOT_NAME = "BTC-SCALP-V5" if not PAPER_MODE else "BTC-SCALP-V5-PAPER"
LOG_FILE = "logs/btc_scalp_v5_trades.jsonl"
SUMMARY_FILE = "logs/btc_scalp_v5_summary.json"

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
        self.in_trade = False
        # Event-driven SL + BE: websocket sets these when bid crosses thresholds
        self.sl_triggered = None
        self.be_triggered = None
        self.active_sl_price = 0
        self.active_be_price = 0
        self.active_sl_direction = ""

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
        log_msg(f"[INIT] Shares: {SHARES_PER_TRADE} | Fill timeout: {FILL_TIMEOUT}s | Entry: ${MIN_ENTRY_PRICE}-${MAX_ENTRY_PRICE} | First {ENTRY_WINDOW_SECONDS}s only")
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

                        while time.time() < window_end:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                            except asyncio.TimeoutError:
                                continue
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

        # ── POLYMARKET WEBSOCKET ──
        # Uses price_changes messages which include best_bid/best_ask directly.
        # Initial snapshot (bids/asks arrays) used for first read only.
        async def poly_ws():
            retry = 0
            while time.time() < window_end + 10:
                try:
                    async with websockets.connect(POLY_WS_URL, ping_interval=20, ping_timeout=15,
                                                  close_timeout=5) as ws:
                        await ws.send(json.dumps({"assets_ids": [up_token, down_token], "type": "market"}))
                        if retry == 0:
                            log_msg("[POLY-WS] Connected — using price_changes best_bid/best_ask")
                        else:
                            log_msg(f"[POLY-WS] Reconnected ({retry})")
                        retry = 0

                        while time.time() < window_end + 10:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                            except asyncio.TimeoutError:
                                continue
                            try:
                                msg = json.loads(raw)
                            except:
                                continue

                            # Handle initial snapshot (list with bids/asks arrays)
                            if isinstance(msg, list):
                                for item in msg:
                                    if not isinstance(item, dict):
                                        continue
                                    aid = item.get("asset_id", "")
                                    bids_data = item.get("bids", [])
                                    asks_data = item.get("asks", [])
                                    if bids_data:
                                        try:
                                            bb = max(float(b["price"]) for b in bids_data if isinstance(b, dict) and "price" in b)
                                            if aid == up_token:
                                                self.up_bid = bb
                                            elif aid == down_token:
                                                self.down_bid = bb
                                        except ValueError:
                                            pass
                                    if asks_data:
                                        try:
                                            ba = min(float(a["price"]) for a in asks_data if isinstance(a, dict) and "price" in a)
                                            if aid == up_token:
                                                self.up_ask = ba
                                            elif aid == down_token:
                                                self.down_ask = ba
                                        except ValueError:
                                            pass
                                continue

                            if not isinstance(msg, dict):
                                continue

                            # Handle snapshot message (has bids/asks + asset_id)
                            if "bids" in msg and "asset_id" in msg:
                                aid = msg["asset_id"]
                                bids_data = msg.get("bids", [])
                                asks_data = msg.get("asks", [])
                                if bids_data:
                                    try:
                                        bb = max(float(b["price"]) for b in bids_data if isinstance(b, dict) and "price" in b)
                                        if aid == up_token:
                                            self.up_bid = bb
                                        elif aid == down_token:
                                            self.down_bid = bb
                                    except ValueError:
                                        pass
                                if asks_data:
                                    try:
                                        ba = min(float(a["price"]) for a in asks_data if isinstance(a, dict) and "price" in a)
                                        if aid == up_token:
                                            self.up_ask = ba
                                        elif aid == down_token:
                                            self.down_ask = ba
                                    except ValueError:
                                        pass

                            # Handle price_changes messages (the real-time updates)
                            price_changes = msg.get("price_changes", [])
                            for pc in price_changes:
                                if not isinstance(pc, dict):
                                    continue
                                aid = pc.get("asset_id", "")
                                best_bid = pc.get("best_bid")
                                best_ask = pc.get("best_ask")

                                if aid == up_token:
                                    if best_bid is not None:
                                        self.up_bid = float(best_bid)
                                    if best_ask is not None:
                                        self.up_ask = float(best_ask)
                                elif aid == down_token:
                                    if best_bid is not None:
                                        self.down_bid = float(best_bid)
                                    if best_ask is not None:
                                        self.down_ask = float(best_ask)

                                # Instant SL detection from websocket
                                if (self.sl_triggered is not None and
                                    not self.sl_triggered.is_set() and
                                    best_bid is not None and
                                    self.active_sl_price > 0):
                                    bid_val = float(best_bid)
                                    if self.active_sl_direction == "UP" and aid == up_token and bid_val <= self.active_sl_price:
                                        self.sl_triggered.set()
                                    elif self.active_sl_direction == "DN" and aid == down_token and bid_val <= self.active_sl_price:
                                        self.sl_triggered.set()

                                # Instant BE detection from websocket (only active after SL triggers)
                                if (self.be_triggered is not None and
                                    not self.be_triggered.is_set() and
                                    best_bid is not None and
                                    self.active_be_price > 0):
                                    bid_val = float(best_bid)
                                    if self.active_sl_direction == "UP" and aid == up_token and bid_val >= self.active_be_price:
                                        self.be_triggered.set()
                                    elif self.active_sl_direction == "DN" and aid == down_token and bid_val >= self.active_be_price:
                                        self.be_triggered.set()

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

                # Check entry time window (first 60s only)
                window_elapsed = time.time() - window_start
                if window_elapsed > ENTRY_WINDOW_SECONDS:
                    continue

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

                try:
                    await self._execute_trade(tid, direction, entry_price, tp_price, sl_price,
                                              signal["cb_move"], mkt["question"], window_start, window_end, force_exit_time)
                except Exception as e:
                    log_msg(f"[TRADE-ERROR] #{tid} {str(e)[:80]}")
                finally:
                    self.in_trade = False

        await asyncio.gather(cb_ws(), poly_ws(), trade_executor(), return_exceptions=True)

    async def _execute_trade(self, tid, direction, entry_price, tp_price, sl_price,
                             cb_move, question, window_start, window_end, force_exit_time):
        """Execute a single trade: buy → wait for fill → TP/SL → record."""
        t_start = time.time()
        window_elapsed = round(t_start - window_start, 1)  # seconds into the 5-min window
        token = None  # Not needed for paper but kept for live
        taker_fee = 0

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
                # Paper: GTC buy at best bid fills when bid is at or near our price
                # In reality, a maker buy at best bid fills almost immediately
                current_bid = self.up_bid if direction == "UP" else self.down_bid

                # Fill if bid is within $0.01 of our entry (normal spread)
                if current_bid > 0 and current_bid >= entry_price - 0.01:
                    fill_shares = SHARES_PER_TRADE
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

        trade_token = self.mf.market["up"] if direction == "UP" else self.mf.market["down"]

        # ── STEP 4: Place 4 tiered GTC TP sells ──
        tier_count = 4
        tier_shares_list = [int(fill_shares) // tier_count + (1 if i < int(fill_shares) % tier_count else 0) for i in range(tier_count)]
        tier_offsets = [0.01, 0.02, 0.03, 0.04]
        tier_prices_list = [snap_price(fill_price + off) for off in tier_offsets]
        tier_filled = [False] * tier_count

        log_msg(f"[V5-TIERS] #{tid} Placing 4 TP tiers: " +
                " | ".join([f"{tier_shares_list[i]}sh@${tier_prices_list[i]:.2f}" for i in range(tier_count)]))

        if not PAPER_MODE and self.client:
            for i in range(tier_count):
                try:
                    args = OrderArgs(price=tier_prices_list[i], size=tier_shares_list[i], side=SELL, token_id=trade_token)
                    signed = self.client.create_order(args)
                    self.client.post_order(signed, OrderType.GTC)
                except:
                    pass
        else:
            await asyncio.sleep(PAPER_LATENCY)
            log_msg(f"[V5-PAPER] #{tid} 4 tier orders placed")

        # ── STEP 5: Monitor — wait for fills, cascade on SL ──
        total_sold = 0
        total_revenue = 0.0
        cascade_started = False
        sl_bounce_data = None
        min_bid_during = fill_price
        max_bid_during = fill_price

        try:  # Catch any crash in monitoring to ensure trade always records

            while total_sold < int(fill_shares) and time.time() < window_end:
                await asyncio.sleep(0.05)

                _bid = self.up_bid if direction == "UP" else self.down_bid
                if _bid <= 0:
                    continue

                # Track min/max
                if _bid < min_bid_during:
                    min_bid_during = _bid
                if _bid > max_bid_during:
                    max_bid_during = _bid

                # Force exit before resolution
                if time.time() >= window_end - FORCE_EXIT_BEFORE_END:
                    unsold = int(fill_shares) - total_sold
                    if unsold > 0:
                        _bid_now = self.up_bid if direction == "UP" else self.down_bid
                        sell_p = snap_price(_bid_now - 0.01)
                        if sell_p < 0.01:
                            sell_p = 0.01
                        total_revenue += unsold * sell_p
                        total_sold += unsold
                        taker_fee += calc_taker_fee(sell_p, unsold)
                        log_msg(f"[V5-FORCE] #{tid} Window ending, sold {unsold}sh @ ${sell_p:.2f}")
                    break

                # Check tier fills (highest first — if bid >= tier price, that tier fills)
                for i in range(tier_count):
                    if tier_filled[i]:
                        continue
                    if _bid >= tier_prices_list[i]:
                        tier_filled[i] = True
                        total_sold += tier_shares_list[i]
                        total_revenue += tier_shares_list[i] * tier_prices_list[i]
                        log_msg(f"[V5-FILL] #{tid} Tier {i+1}: {tier_shares_list[i]}sh @ ${tier_prices_list[i]:.2f} | "
                                f"bid=${_bid:.2f} | {total_sold}/{int(fill_shares)} sold")

                # All sold?
                if total_sold >= int(fill_shares):
                    break

                # CASCADE: bid dropped to SL level — start selling unfilled tiers at market
                if _bid <= fill_price - SL_OFFSET and not cascade_started:
                    cascade_started = True
                    log_msg(f"[V5-CASCADE] #{tid} Bid ${_bid:.2f} hit SL — cascading unfilled tiers")

                    # Hold for bounce like V4 — wait until bid returns to entry
                    # If it returns, sell unfilled tiers at BE or better
                    # If window runs out, force exit
                    cascade_wait_start = time.time()
                    while time.time() < window_end - FORCE_EXIT_BEFORE_END:
                        await asyncio.sleep(0.05)
                        _bid_now = self.up_bid if direction == "UP" else self.down_bid

                        if _bid_now <= 0:
                            continue
                        if _bid_now < min_bid_during:
                            min_bid_during = _bid_now
                        if _bid_now > max_bid_during:
                            max_bid_during = _bid_now

                        # Check if any unfilled tiers can now fill (price recovered)
                        for i in range(tier_count):
                            if tier_filled[i]:
                                continue
                            if _bid_now >= tier_prices_list[i]:
                                tier_filled[i] = True
                                total_sold += tier_shares_list[i]
                                total_revenue += tier_shares_list[i] * tier_prices_list[i]
                                log_msg(f"[V5-BOUNCE-FILL] #{tid} Tier {i+1}: {tier_shares_list[i]}sh @ ${tier_prices_list[i]:.2f} on bounce | {total_sold}/{int(fill_shares)} sold")

                        # Price returned to BE — sell remaining unfilled tiers at entry price
                        if _bid_now >= fill_price:
                            for i in range(tier_count):
                                if tier_filled[i]:
                                    continue
                                tier_filled[i] = True
                                total_sold += tier_shares_list[i]
                                total_revenue += tier_shares_list[i] * fill_price
                                taker_fee += calc_taker_fee(fill_price, tier_shares_list[i])
                                log_msg(f"[V5-BE] #{tid} Tier {i+1}: {tier_shares_list[i]}sh @ ${fill_price:.2f} (BE) | {total_sold}/{int(fill_shares)} sold")

                        if total_sold >= int(fill_shares):
                            break

                    # Force-sell any remaining unsold shares
                    unsold = int(fill_shares) - total_sold
                    if unsold > 0:
                        _bid_now = self.up_bid if direction == "UP" else self.down_bid
                        sell_p = snap_price(_bid_now - 0.01) if _bid_now > 0.01 else 0.01
                        total_revenue += unsold * sell_p
                        total_sold += unsold
                        taker_fee += calc_taker_fee(sell_p, unsold)
                        log_msg(f"[V5-CASCADE-EXIT] #{tid} Force sold {unsold}sh @ ${sell_p:.2f}")

                    break  # Exit outer loop

            # Force-sell if outer loop timed out without cascade
            unsold = int(fill_shares) - total_sold
            if unsold > 0:
                _bid_now = self.up_bid if direction == "UP" else self.down_bid
                sell_p = snap_price(_bid_now - 0.01) if _bid_now > 0.01 else 0.01
                total_revenue += unsold * sell_p
                total_sold += unsold
                taker_fee += calc_taker_fee(sell_p, unsold)
                log_msg(f"[V5-TIMEOUT-EXIT] #{tid} Force sold {unsold}sh @ ${sell_p:.2f}")

        except Exception as e:
            log_msg(f"[V5-ERROR] #{tid} Monitoring crashed: {str(e)[:80]}")
            import traceback
            traceback.print_exc()
            # Ensure remaining shares are accounted for
            unsold = int(fill_shares) - total_sold
            if unsold > 0:
                total_revenue += unsold * fill_price  # Assume BE as fallback
                total_sold += unsold

        # Calculate result
        if total_sold > 0:
            exit_price = total_revenue / total_sold
        else:
            exit_price = fill_price

        filled_count = sum(1 for f in tier_filled if f)
        exit_reason = f"V5-{filled_count}T"
        if cascade_started:
            exit_reason += "+C"

        log_msg(f"[V5-DONE] #{tid} {total_sold}/{int(fill_shares)}sh sold | avg ${exit_price:.3f} | "
                f"P&L ${total_revenue - fill_price * int(fill_shares):+.2f} | {filled_count}/4 tiers")

        # Clear SL + BE events
        self.sl_triggered = None
        self.be_triggered = None
        self.active_sl_price = 0
        self.active_be_price = 0
        self.active_sl_direction = ""

        # ── STEP 6: Record trade (MUST always execute) ──
        hold_time = time.time() - t_start
        try:
            self._record_trade(tid, direction, fill_price, exit_price, fill_shares, taker_fee,
                               exit_reason, hold_time, cb_move, question, window_elapsed,
                               min_bid_during, max_bid_during, sl_bounce_data)
        except Exception as e:
            log_msg(f"[RECORD-ERROR] #{tid} {str(e)[:80]}")

        # HTTP book check (non-critical, after recording)
        try:
            http_book_end = await get_book(trade_token)
            ws_bid_now = self.up_bid if direction == "UP" else self.down_bid
            http_bid = http_book_end.get('bid', 0) if http_book_end else 0
            log_msg(f"[DEBUG] #{tid} EXIT | WS bid=${ws_bid_now:.2f} | HTTP bid=${http_bid:.2f}")
        except:
            pass

    def _record_trade(self, tid, direction, entry, exit_price, shares, taker_fee, reason,
                      hold_time, cb_move, question, window_elapsed=0,
                      min_bid=0, max_bid=0, sl_bounce=None):
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
            elif reason in ("TIMEOUT", "FORCE-EXIT", "MAX-HOLD", "RES-WIN", "RES-LOSS"):
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
                    f"min ${min_bid:.2f} max ${max_bid:.2f} | "
                    f"bank ${self.bankroll:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%)")

        # Log to file
        try:
            self.log_file.write(json.dumps({
                "id": tid, "direction": direction, "entry": entry, "exit": exit_price,
                "shares": shares, "cost": cost, "revenue": revenue, "taker_fee": taker_fee,
                "pnl": pnl, "reason": reason, "hold_time": round(hold_time, 2),
                "cb_move": round(cb_move, 2),
                "window_elapsed": round(window_elapsed, 1),
                "min_bid": min_bid, "max_bid": max_bid,
                "sl_bounce": sl_bounce,
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
