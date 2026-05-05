#!/usr/bin/env python3
"""
ALPHA-1 — The Masterpiece
===========================
Multi-phase both-sides strategy for Polymarket BTC 5-min Up/Down.

Phase 1 (T+0):    Buy BOTH UP and DOWN at window open
Phase 2 (T+210):  Direction confirmed. SL the loser at bid-$0.05
Phase 3 (T+270):  If winner bid >= $0.95, add 5% to winner
Phase 4 (resolve): Winner → $1.00, loser SL'd

Every known failure mode is handled. See alpha1_failure_modes.md.
"""

import asyncio
import json
import time
import math
import sys
import os
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

try:
    import aiohttp
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "aiohttp", "-q"])
    import aiohttp

try:
    from dotenv import load_dotenv
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-dotenv", "-q"])
    from dotenv import load_dotenv

load_dotenv()

CLOB_AVAILABLE = False
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    pass

# ============================================================
# CONFIGURATION
# ============================================================
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL_A1", "65.0"))
BET_PCT = 0.10            # 10% of bankroll per window (split 5%/5%)
PHASE3_PCT = 0.05         # 5% additional on winner at T+270
MAX_BET_PER_SIDE = float(os.getenv("MAX_BET_A1", "200.0"))

# Timing
PHASE2_TRIGGER = 0.65     # SL loser when either side's bid hits this (adaptive, not fixed time)
PHASE2_FALLBACK = 240     # if neither side hits trigger by this time, SL based on leader anyway
PHASE3_TIME = 270         # seconds into window for add
PHASE3_MIN_BID = 0.95     # winner must be >= this to add
WINDOW_END = 290          # stop all activity, let resolution happen

# Risk
SL_OFFSET_LOSER = 0.05    # SL on loser = current bid - this
MAX_COMBINED_ASK = 1.03   # skip if combined ask > this
MIN_SIDE_ASK = 0.10       # skip if either side < this
MAX_SIDE_ASK = 0.90       # skip if either side > this
MAX_SPREAD = 0.06         # skip if either side's spread > this
DRAWDOWN_PAUSE = 0.30     # pause if bankroll drops 30% from peak

# CLOB
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Files
STATE_FILE = "logs/alpha1_state.json"
LOG_FILE = "logs/alpha1_trades.jsonl"
BANKROLL_FILE = "logs/alpha1_bankroll.csv"
os.makedirs("logs", exist_ok=True)

BOT_NAME = "ALPHA-1"


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def log_msg(msg):
    print(f"  [{ts()}] {msg}", flush=True)

def snap_price(price):
    return round(max(0.01, min(0.99, round(price * 100) / 100)), 2)


# ============================================================
# TELEGRAM
# ============================================================
async def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        data = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
        }).encode()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data=data,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                pass
    except Exception:
        pass


# ============================================================
# MARKET FINDER
# ============================================================
class MarketFinder:
    def __init__(self):
        self.active_market = None
        self.last_refresh = 0.0

    async def refresh(self):
        if time.time() - self.last_refresh < 25:
            return
        self.last_refresh = time.time()
        try:
            now = int(time.time())
            window_start = (now // 300) * 300
            slug = f"btc-updown-5m-{window_start}"
            url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            m = data[0]
                            tokens_raw = m.get("clobTokenIds", "[]")
                            outcomes_raw = m.get("outcomes", "[]")
                            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
                            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                            up_token = tokens[0] if outcomes[0] == "Up" else tokens[1]
                            down_token = tokens[1] if outcomes[0] == "Up" else tokens[0]
                            self.active_market = {
                                "up_token": up_token,
                                "down_token": down_token,
                                "question": m.get("question", ""),
                                "window_start": window_start,
                            }
        except Exception as e:
            log_msg(f"[MARKET] error: {e}")


# ============================================================
# BOOK READER (HTTP, not websocket — more reliable)
# ============================================================
async def get_book(token_id):
    try:
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    best_bid = float(bids[-1]["price"]) if bids else 0.0
                    best_ask = float(asks[-1]["price"]) if asks else 0.0
                    mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0.0
                    return {"best_bid": best_bid, "best_ask": best_ask, "midpoint": mid}
    except Exception:
        pass
    return None


# ============================================================
# WALLET BALANCE (via Polygon RPC, not CLOB)
# ============================================================
async def fetch_wallet_balance():
    wallet = PROXY_WALLET or FUNDER_ADDRESS
    if not wallet:
        return None
    padded = wallet.lower().replace("0x", "").zfill(64)
    call_data = f"0x70a08231{padded}"
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": USDC_CONTRACT, "data": call_data}, "latest"]
    }
    rpc = os.getenv("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc, json=payload,
                                    headers={"Content-Type": "application/json"},
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data.get("result", "0x0")
                    raw = int(result, 16)
                    return raw / 1_000_000
    except Exception:
        pass
    return None


# ============================================================
# CLOB ORDER HELPERS
# ============================================================
class OrderManager:
    def __init__(self):
        self.client = None
        self._lock = asyncio.Lock()

    def init_clob(self):
        if not CLOB_AVAILABLE or not PRIVATE_KEY:
            log_msg("[CLOB] Not available")
            return
        try:
            self.client = ClobClient(
                CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS)
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            log_msg("[CLOB] Authenticated")
        except Exception as e:
            log_msg(f"[CLOB] Auth failed: {e}")

    async def buy(self, token_id, price, size):
        """Place a GTC limit buy. Returns order_id or None."""
        async with self._lock:
            if not self.client:
                return None
            try:
                args = OrderArgs(price=price, size=size, side=BUY, token_id=token_id)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.GTC)
                oid = resp.get("orderID") or resp.get("order_id") if resp else None
                return oid
            except Exception as e:
                log_msg(f"[ORDER] Buy error: {e}")
                return None

    async def sell(self, token_id, price, size):
        """Place a GTC limit sell. Returns order_id or None."""
        async with self._lock:
            if not self.client:
                return None
            for attempt in range(3):
                try:
                    args = OrderArgs(price=price, size=size, side=SELL, token_id=token_id)
                    signed = self.client.create_order(args)
                    resp = self.client.post_order(signed, OrderType.GTC)
                    oid = resp.get("orderID") or resp.get("order_id") if resp else None
                    if oid:
                        return oid
                except Exception as e:
                    err = str(e)
                    if "balance" in err.lower() and attempt < 2:
                        # Maybe partial fill — try with fewer shares
                        size = round(size - 1, 2)
                        if size < 1:
                            return None
                        log_msg(f"[ORDER] Balance error, retrying with {size} shares")
                        await asyncio.sleep(1)
                        continue
                    log_msg(f"[ORDER] Sell error: {e}")
                    return None
            return None

    async def cancel_all(self):
        """Cancel all open orders. Retry up to 3 times."""
        async with self._lock:
            if not self.client:
                return
            for attempt in range(3):
                try:
                    self.client.cancel_all()
                    return
                except Exception as e:
                    log_msg(f"[ORDER] Cancel error (attempt {attempt+1}): {e}")
                    await asyncio.sleep(2)

    async def get_order_status(self, order_id):
        """Check if an order is still LIVE or has been MATCHED."""
        async with self._lock:
            if not self.client:
                return "unknown"
            try:
                info = self.client.get_order(order_id)
                return info.get("status", "unknown") if info else "unknown"
            except Exception:
                return "unknown"


# ============================================================
# WINDOW TRADE — represents one 5-minute trading cycle
# ============================================================
@dataclass
class WindowTrade:
    window_start: int
    up_token: str
    down_token: str
    # UP side
    up_buy_id: Optional[str] = None
    up_entry: float = 0.0
    up_shares: float = 0.0
    up_cost: float = 0.0
    up_sl_id: Optional[str] = None
    up_sl_price: float = 0.0
    up_exited: bool = False
    up_exit_price: float = 0.0
    # DOWN side
    down_buy_id: Optional[str] = None
    down_entry: float = 0.0
    down_shares: float = 0.0
    down_cost: float = 0.0
    down_sl_id: Optional[str] = None
    down_sl_price: float = 0.0
    down_exited: bool = False
    down_exit_price: float = 0.0
    # Phase 3 add
    add_token: Optional[str] = None
    add_buy_id: Optional[str] = None
    add_entry: float = 0.0
    add_shares: float = 0.0
    add_cost: float = 0.0
    add_exited: bool = False
    # State
    phase: str = "buying"  # buying, monitoring, phase2, phase3, exiting, done
    confirmed_winner: Optional[str] = None  # "UP" or "DOWN"
    opened_at: float = 0.0


# ============================================================
# THE BOT
# ============================================================
class Alpha1Bot:
    def __init__(self):
        self.market_finder = MarketFinder()
        self.orders = OrderManager()
        self.bankroll = STARTING_BANKROLL
        self.peak_bankroll = STARTING_BANKROLL
        self.max_dd_pct = 0.0
        self.trade_count = 0
        self.wins = 0
        self.losses = 0
        self.consec_losses = 0
        self.max_consec = 0
        self.total_pnl = 0.0
        self.start_time = time.time()
        self.current_trade: Optional[WindowTrade] = None
        self.paused = False
        # Init files
        self.log_file = open(LOG_FILE, "a")
        self.bankroll_file = open(BANKROLL_FILE, "a")

    async def sync_bankroll(self):
        balance = await fetch_wallet_balance()
        if balance is not None:
            old = self.bankroll
            self.bankroll = balance
            if abs(old - balance) > 0.10:
                log_msg(f"[WALLET] Synced: ${old:.2f} → ${balance:.2f}")
            self._update_dd()

    def _update_dd(self):
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll
        dd = self.peak_bankroll - self.bankroll
        dd_pct = (dd / self.peak_bankroll * 100) if self.peak_bankroll > 0 else 0
        if dd_pct > self.max_dd_pct:
            self.max_dd_pct = dd_pct
        # Drawdown pause
        if dd_pct >= DRAWDOWN_PAUSE * 100 and not self.paused:
            self.paused = True
            log_msg(f"[RISK] 🛑 PAUSED — drawdown {dd_pct:.1f}% exceeds {DRAWDOWN_PAUSE*100:.0f}% limit")
            asyncio.create_task(send_telegram(
                f"🛑 <b>{BOT_NAME} PAUSED</b>\n"
                f"Drawdown {dd_pct:.1f}% from peak ${self.peak_bankroll:.2f}\n"
                f"Current: ${self.bankroll:.2f}"))

    # ------- MAIN LOOP -------

    async def run(self):
        """Main trading loop — one window at a time."""
        while True:
            try:
                # Wait for next window
                now = time.time()
                next_window = (int(now) // 300 + 1) * 300
                wait = next_window - now
                if wait > 0:
                    await asyncio.sleep(wait)

                # Brief delay for market to be indexed
                await asyncio.sleep(2)

                if self.paused:
                    log_msg(f"[RISK] Paused. Skipping window.")
                    continue

                await self.sync_bankroll()

                if self.bankroll < 5:
                    log_msg(f"[RISK] Bankroll ${self.bankroll:.2f} too low")
                    continue

                # Refresh market
                self.market_finder.last_refresh = 0
                await self.market_finder.refresh()
                if not self.market_finder.active_market:
                    continue

                up_token = self.market_finder.active_market["up_token"]
                down_token = self.market_finder.active_market["down_token"]
                window_start = self.market_finder.active_market["window_start"]

                # Execute the 4-phase strategy
                await self._execute_window(up_token, down_token, window_start)

            except Exception as e:
                log_msg(f"[MAIN] Error: {e}")
                # Clean up any open orders
                await self.orders.cancel_all()
                self.current_trade = None
                await asyncio.sleep(5)

    async def _execute_window(self, up_token, down_token, window_start):
        """Execute all 4 phases for one window."""
        opened_at = time.time()

        # ── PHASE 1: Buy both sides ──
        up_book, down_book = await asyncio.gather(get_book(up_token), get_book(down_token))
        if not up_book or not down_book:
            return

        up_ask = up_book["best_ask"]
        down_ask = down_book["best_ask"]
        up_spread = up_ask - up_book["best_bid"]
        down_spread = down_ask - down_book["best_bid"]

        # Pre-flight checks
        combined = up_ask + down_ask
        if combined > MAX_COMBINED_ASK:
            log_msg(f"[SKIP] Combined ask ${combined:.2f} > ${MAX_COMBINED_ASK}")
            return
        if up_ask < MIN_SIDE_ASK or down_ask < MIN_SIDE_ASK:
            log_msg(f"[SKIP] Side too low: UP=${up_ask:.2f} DOWN=${down_ask:.2f}")
            return
        if up_ask > MAX_SIDE_ASK or down_ask > MAX_SIDE_ASK:
            log_msg(f"[SKIP] Side too high: UP=${up_ask:.2f} DOWN=${down_ask:.2f}")
            return
        if up_spread > MAX_SPREAD or down_spread > MAX_SPREAD:
            # Wait 10 seconds and retry
            await asyncio.sleep(10)
            up_book, down_book = await asyncio.gather(get_book(up_token), get_book(down_token))
            if not up_book or not down_book:
                return
            up_ask = up_book["best_ask"]
            down_ask = down_book["best_ask"]
            up_spread = up_ask - up_book["best_bid"]
            down_spread = down_ask - down_book["best_bid"]
            if up_spread > MAX_SPREAD or down_spread > MAX_SPREAD:
                log_msg(f"[SKIP] Spread too wide: UP={up_spread:.2f} DOWN={down_spread:.2f}")
                return

        # Calculate sizes
        half_bet = min(self.bankroll * BET_PCT / 2, MAX_BET_PER_SIDE)
        up_shares = round(half_bet / up_ask, 2)
        down_shares = round(half_bet / down_ask, 2)

        if up_shares < 1 or down_shares < 1:
            return

        # Place buy orders
        log_msg(f"[PH1] Buying both: UP ${up_ask:.2f} ({up_shares:.1f}sh) + "
                f"DOWN ${down_ask:.2f} ({down_shares:.1f}sh) = ${up_ask + down_ask:.2f}")

        if DRY_RUN:
            up_buy_id = "PAPER-UP"
            down_buy_id = "PAPER-DOWN"
        else:
            up_buy_id, down_buy_id = await asyncio.gather(
                self.orders.buy(up_token, snap_price(up_ask), up_shares),
                self.orders.buy(down_token, snap_price(down_ask), down_shares),
            )

        if not up_buy_id and not down_buy_id:
            log_msg(f"[PH1] Both buys failed — skipping window")
            return

        # Handle one side failing
        if not up_buy_id:
            log_msg(f"[PH1] UP buy failed — cancelling DOWN, skipping")
            if down_buy_id:
                await self.orders.cancel_all()
            return
        if not down_buy_id:
            log_msg(f"[PH1] DOWN buy failed — cancelling UP, skipping")
            if up_buy_id:
                await self.orders.cancel_all()
            return

        # Both buys placed
        trade = WindowTrade(
            window_start=window_start,
            up_token=up_token, up_buy_id=up_buy_id,
            up_entry=snap_price(up_ask), up_shares=up_shares,
            up_cost=round(up_shares * up_ask, 4),
            down_token=down_token, down_buy_id=down_buy_id,
            down_entry=snap_price(down_ask), down_shares=down_shares,
            down_cost=round(down_shares * down_ask, 4),
            phase="monitoring", opened_at=opened_at,
        )
        self.current_trade = trade
        self.trade_count += 1

        log_msg(f"[PH1] ✅ #{self.trade_count} Both sides bought | "
                f"cost=${trade.up_cost + trade.down_cost:.2f}")

        # ── Wait for buy to settle ──
        await asyncio.sleep(3)

        # ── MONITORING LOOP ──
        try:
            while True:
                age = time.time() - opened_at
                if age > WINDOW_END:
                    break

                up_book, down_book = await asyncio.gather(
                    get_book(up_token), get_book(down_token))
                if not up_book or not down_book:
                    await asyncio.sleep(1)
                    continue

                up_bid = up_book["best_bid"]
                down_bid = down_book["best_bid"]

                # ── PHASE 2: Adaptive SL — trigger when either side hits $0.65 ──
                # OR fallback at T+240 if market hasn't decided
                trigger_now = False
                trigger_reason = ""

                if trade.confirmed_winner is None:
                    if up_bid >= PHASE2_TRIGGER:
                        trigger_now = True
                        trigger_reason = f"UP bid ${up_bid:.2f} >= ${PHASE2_TRIGGER}"
                    elif down_bid >= PHASE2_TRIGGER:
                        trigger_now = True
                        trigger_reason = f"DOWN bid ${down_bid:.2f} >= ${PHASE2_TRIGGER}"
                    elif age >= PHASE2_FALLBACK:
                        trigger_now = True
                        trigger_reason = f"Fallback at T+{int(age)}s"

                if trigger_now and trade.confirmed_winner is None:
                    if up_bid > down_bid:
                        trade.confirmed_winner = "UP"
                        # SL on DOWN at current bid - offset
                        sl_price = snap_price(max(down_bid - SL_OFFSET_LOSER, 0.01))
                        trade.down_sl_price = sl_price
                        log_msg(f"[PH2] {trigger_reason} → UP winner | "
                                f"SL DOWN @ ${sl_price:.2f} (bid was ${down_bid:.2f})")
                        if not DRY_RUN:
                            sl_id = await self.orders.sell(
                                down_token, sl_price, down_shares)
                            trade.down_sl_id = sl_id
                            if sl_id:
                                log_msg(f"[PH2] SL order placed: {sl_id[:12]}...")
                            else:
                                log_msg(f"[PH2] ⚠️ SL order failed")
                    else:
                        trade.confirmed_winner = "DOWN"
                        sl_price = snap_price(max(up_bid - SL_OFFSET_LOSER, 0.01))
                        trade.up_sl_price = sl_price
                        log_msg(f"[PH2] {trigger_reason} → DOWN winner | "
                                f"SL UP @ ${sl_price:.2f} (bid was ${up_bid:.2f})")
                        if not DRY_RUN:
                            sl_id = await self.orders.sell(
                                up_token, sl_price, up_shares)
                            trade.up_sl_id = sl_id
                            if sl_id:
                                log_msg(f"[PH2] SL order placed: {sl_id[:12]}...")
                            else:
                                log_msg(f"[PH2] ⚠️ SL order failed")

                # ── PHASE 3: DISABLED — net negative EV, amplifies losses on reversals ──
                # if age >= PHASE3_TIME and trade.confirmed_winner and not trade.add_token:
                #     (disabled)

                # ── Check for TP (winner hits $0.99) ──
                if trade.confirmed_winner == "UP" and up_bid >= 0.99 and not trade.up_exited:
                    log_msg(f"[TP] UP bid ${up_bid:.2f} >= $0.99 — winner resolved")
                    trade.up_exited = True
                    trade.up_exit_price = 1.00
                    # Cancel UP SL if it exists (shouldn't, but safety)
                    if trade.up_sl_id:
                        await self.orders.cancel_all()
                        await asyncio.sleep(2)
                    # Sell UP shares
                    if not DRY_RUN:
                        await self.orders.sell(up_token, 0.99, trade.up_shares)
                    # Also sell add if on UP
                    if trade.add_token == up_token and not trade.add_exited:
                        trade.add_exited = True
                        if not DRY_RUN and trade.add_shares > 0:
                            await asyncio.sleep(1)
                            await self.orders.sell(up_token, 0.99, trade.add_shares)

                if trade.confirmed_winner == "DOWN" and down_bid >= 0.99 and not trade.down_exited:
                    log_msg(f"[TP] DOWN bid ${down_bid:.2f} >= $0.99 — winner resolved")
                    trade.down_exited = True
                    trade.down_exit_price = 1.00
                    if trade.down_sl_id:
                        await self.orders.cancel_all()
                        await asyncio.sleep(2)
                    if not DRY_RUN:
                        await self.orders.sell(down_token, 0.99, trade.down_shares)
                    if trade.add_token == down_token and not trade.add_exited:
                        trade.add_exited = True
                        if not DRY_RUN and trade.add_shares > 0:
                            await asyncio.sleep(1)
                            await self.orders.sell(down_token, 0.99, trade.add_shares)

                # ── Check for SL fill (loser bid dropped below SL) ──
                if trade.confirmed_winner == "UP" and trade.down_sl_price > 0:
                    if down_bid <= trade.down_sl_price and not trade.down_exited:
                        trade.down_exited = True
                        trade.down_exit_price = trade.down_sl_price
                        log_msg(f"[SL] DOWN SL filled @ ${trade.down_sl_price:.2f}")

                if trade.confirmed_winner == "DOWN" and trade.up_sl_price > 0:
                    if up_bid <= trade.up_sl_price and not trade.up_exited:
                        trade.up_exited = True
                        trade.up_exit_price = trade.up_sl_price
                        log_msg(f"[SL] UP SL filled @ ${trade.up_sl_price:.2f}")

                # ── Both sides resolved? ──
                if trade.up_exited and trade.down_exited:
                    break

                await asyncio.sleep(1)

        except Exception as e:
            log_msg(f"[MON] Error: {e}")

        # ── PHASE 4: Finalize ──
        await asyncio.sleep(2)

        # Cancel any remaining orders
        if not DRY_RUN:
            await self.orders.cancel_all()

        # Calculate P&L
        up_proceeds = trade.up_shares * trade.up_exit_price if trade.up_exited else 0
        down_proceeds = trade.down_shares * trade.down_exit_price if trade.down_exited else 0
        add_proceeds = 0
        if trade.add_token and trade.add_exited:
            add_proceeds = trade.add_shares * 1.00  # winner resolved

        # Unexited sides: if winner was confirmed but loser never exited,
        # it will be redeemed by cron. Assume $0 for P&L.
        total_proceeds = up_proceeds + down_proceeds + add_proceeds
        total_cost = trade.up_cost + trade.down_cost + trade.add_cost
        pnl = round(total_proceeds - total_cost, 4)

        self.total_pnl += pnl
        if pnl > 0:
            self.wins += 1
            self.consec_losses = 0
        else:
            self.losses += 1
            self.consec_losses += 1
            if self.consec_losses > self.max_consec:
                self.max_consec = self.consec_losses

        log_msg(f"[PH4] #{self.trade_count} {'WIN' if pnl > 0 else 'LOSS'} | "
                f"UP ${up_proceeds:.2f} DOWN ${down_proceeds:.2f} add ${add_proceeds:.2f} | "
                f"cost ${total_cost:.2f} | P&L ${pnl:+.2f}")

        # Sync bankroll from wallet (ground truth)
        await self.sync_bankroll()
        self._update_dd()

        # Log
        self._log_trade(trade, pnl, total_proceeds, total_cost)

        # Telegram on every trade
        asyncio.create_task(send_telegram(
            f"{'🟢' if pnl > 0 else '🔴'} <b>{BOT_NAME} #{self.trade_count}</b>\n"
            f"P&L: ${pnl:+.2f} | Bank: ${self.bankroll:.2f}\n"
            f"Winner: {trade.confirmed_winner or '?'} | "
            f"UP ${up_proceeds:.2f} DOWN ${down_proceeds:.2f}\n"
            f"Record: {self.wins}W/{self.losses}L ({self.wins/(self.wins+self.losses)*100:.0f}%)"
        ))

        self.current_trade = None

    def _log_trade(self, trade, pnl, proceeds, cost):
        try:
            entry = {
                "id": self.trade_count,
                "pnl": pnl,
                "proceeds": proceeds,
                "cost": cost,
                "winner": trade.confirmed_winner,
                "up_entry": trade.up_entry,
                "down_entry": trade.down_entry,
                "up_exit": trade.up_exit_price,
                "down_exit": trade.down_exit_price,
                "add_entry": trade.add_entry,
                "add_cost": trade.add_cost,
                "bankroll": self.bankroll,
                "time": datetime.now(timezone.utc).isoformat(),
            }
            self.log_file.write(json.dumps(entry) + "\n")
            self.log_file.flush()
            self.bankroll_file.write(
                f"{datetime.now(timezone.utc).isoformat()},{self.bankroll:.2f},"
                f"{self.peak_bankroll:.2f},{self.max_dd_pct:.2f}\n")
            self.bankroll_file.flush()
        except Exception:
            pass

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        pnl = self.bankroll - STARTING_BANKROLL
        bet = min(self.bankroll * BET_PCT / 2, MAX_BET_PER_SIDE)
        status = "PAUSED" if self.paused else "ACTIVE"
        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {BOT_NAME} | {elapsed:.0f}min | {status}")
        print(f"  Bankroll: ${self.bankroll:,.2f} (${pnl:+,.2f}) | Peak: ${self.peak_bankroll:,.2f}")
        print(f"  Bet: ${bet:.2f}/side | DD: {self.max_dd_pct:.1f}%")
        print(f"  Trades: {total} ({self.wins}W/{self.losses}L) | Win%: {wr:.0f}% | "
              f"Consec losses: {self.consec_losses}")
        print(f"  Max bet/side: ${MAX_BET_PER_SIDE} | Combined max: ${MAX_COMBINED_ASK}")
        open_str = f"Phase: {self.current_trade.phase}" if self.current_trade else "none"
        print(f"  Open: {open_str}")
        print()


# ============================================================
# MAIN
# ============================================================
async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        await bot.sync_bankroll()
        bot.print_status()
        # Telegram every 15 min
        elapsed = (time.time() - bot.start_time) / 60
        if int(elapsed) % 15 == 0 and int(elapsed) > 0:
            total = bot.wins + bot.losses
            wr = bot.wins / total * 100 if total else 0
            pnl = bot.bankroll - STARTING_BANKROLL
            await send_telegram(
                f"📊 <b>{BOT_NAME} Status</b> ({elapsed:.0f}min)\n"
                f"Bank: ${bot.bankroll:,.2f} (${pnl:+,.2f})\n"
                f"Peak: ${bot.peak_bankroll:,.2f} | DD: {bot.max_dd_pct:.1f}%\n"
                f"Trades: {total} ({bot.wins}W/{bot.losses}L) {wr:.0f}%\n"
                f"Bet: ${min(bot.bankroll * BET_PCT / 2, MAX_BET_PER_SIDE):.2f}/side\n"
                f"{'🛑 PAUSED' if bot.paused else '✅ ACTIVE'}"
            )


async def main():
    print("=" * 65)
    print(f"  {BOT_NAME} — The Masterpiece")
    print("=" * 65)
    mode = "PAPER" if DRY_RUN else "*** LIVE ***"
    print(f"  Mode:        {mode}")
    print(f"  Strategy:    Buy both → SL loser when winner hits ${PHASE2_TRIGGER} → hold winner")
    print(f"  Phase 3:     DISABLED (net negative EV)")
    print(f"  Bankroll:    LIVE from wallet (fallback ${STARTING_BANKROLL:,.2f})")
    print(f"  Bet sizing:  {int(BET_PCT*100)}% split 50/50 (max ${MAX_BET_PER_SIDE}/side)")
    print(f"  SL:          Loser bid - ${SL_OFFSET_LOSER} (when winner >= ${PHASE2_TRIGGER}, fallback T+{PHASE2_FALLBACK})")
    print(f"  Max combined: ${MAX_COMBINED_ASK}")
    print(f"  DD pause:    {int(DRAWDOWN_PAUSE*100)}%")
    if FUNDER_ADDRESS:
        print(f"  Wallet:      {FUNDER_ADDRESS[:10]}...{FUNDER_ADDRESS[-6:]}")
    print()

    bot = Alpha1Bot()
    bot.orders.init_clob()

    # Initial sync
    await bot.sync_bankroll()
    log_msg(f"[INIT] Bankroll: ${bot.bankroll:.2f}")

    await send_telegram(
        f"🚀 <b>{BOT_NAME} Started</b>\n"
        f"Mode: {mode}\n"
        f"Bankroll: ${bot.bankroll:.2f}\n"
        f"Strategy: Buy both → SL loser when winner >= ${PHASE2_TRIGGER} → hold winner")

    await asyncio.gather(
        bot.run(),
        run_status(bot),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
