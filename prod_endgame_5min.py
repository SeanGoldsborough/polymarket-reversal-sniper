#!/usr/bin/env python3
"""
Polymarket ENDGAME Production Bot — BTC 5-Minute Markets
=========================================================
Same ENDGAME strategy but on 5-minute crypto markets instead of 15-minute.
5-min markets have lower fees (max 0.44% vs 1.56%) and faster resolution.

MODE:
  DRY_RUN=true  → Paper mode (default). Real data, simulated trades.
  DRY_RUN=false → Live mode. Real orders on Polymarket CLOB.

Run:  python3 prod_endgame_5min.py
Stop: Ctrl+C
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
from collections import deque

try:
    import websockets
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

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

# Telegram notifications (optional — works without it)
try:
    from telegram_notify import TelegramNotifier
except ImportError:
    TelegramNotifier = None

CLOB_AVAILABLE = False
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    pass


# ============================================================
# CONFIGURATION — v2 ENDGAME parameters + 10% Kelly
# ============================================================
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Kelly sizing
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.10"))   # 10%
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL", "500.0"))
MAX_BET_CAP = float(os.getenv("MAX_BET_CAP", "2000.0"))

# v2 parameters — adjusted for 5-minute markets
BTC_MIN_GAP = float(os.getenv("BTC_MIN_GAP_5M", "15.0"))       # same gap threshold
PRICE_PER_TICK = float(os.getenv("PRICE_PER_TICK_5M", "10.0")) # smaller ticks for 5-min
COOLDOWN = float(os.getenv("EG_COOLDOWN_5M", "5.0"))            # faster cooldown
TIME_EXIT = float(os.getenv("EG_TIME_EXIT_5M", "30.0"))         # 30s time exit
ENTRY_TOKEN_PRICE = float(os.getenv("ENTRY_TOKEN_PRICE_5M", "0.80"))

# CLOB
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))


# ============================================================
# FEE MODEL
# ============================================================
def taker_fee_15m(price: float, shares: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    fee_rate = 0.0312 * 4 * price * (1 - price)
    return round(shares * price * fee_rate, 4)

def snap_price(price: float) -> float:
    return round(max(0.01, min(0.99, round(price * 100) / 100)), 2)

def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def log_msg(msg: str):
    print(f"  [{ts()}] {msg}")


# ============================================================
# MARKET FINDER
# ============================================================
class MarketFinder:
    """Discovers active BTC 5-min markets using timestamped slugs.

    Slug format: btc-updown-5m-{timestamp}
    Fields clobTokenIds and outcomes are JSON strings, not arrays.
    """

    def __init__(self):
        self.active_market: Optional[dict] = None  # {"up_token", "down_token", "question"}
        self.last_refresh = 0.0

    async def refresh(self):
        if time.time() - self.last_refresh < 30:  # refresh every 30s (5-min markets rotate faster)
            return
        self.last_refresh = time.time()

        r = 300  # 5-minute windows
        t = int(time.time())
        timestamps = [t - t % r, t - t % r + r]  # current window, next window

        try:
            async with aiohttp.ClientSession() as session:
                for ts_val in timestamps:
                    slug = f"btc-updown-5m-{ts_val}"
                    async with session.get(
                        "https://gamma-api.polymarket.com/markets",
                        params={"slug": slug},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        markets = await resp.json()

                    if not markets:
                        continue

                    market = markets[0]

                    # Parse JSON string fields
                    tokens_raw = market.get("clobTokenIds", "[]")
                    outcomes_raw = market.get("outcomes", "[]")
                    if isinstance(tokens_raw, str):
                        tokens = json.loads(tokens_raw)
                    else:
                        tokens = tokens_raw
                    if isinstance(outcomes_raw, str):
                        outcomes = json.loads(outcomes_raw)
                    else:
                        outcomes = outcomes_raw

                    if len(tokens) >= 2 and len(outcomes) >= 2:
                        up_idx = 0
                        down_idx = 1
                        for i, outcome in enumerate(outcomes):
                            if "up" in outcome.lower():
                                up_idx = i
                            elif "down" in outcome.lower():
                                down_idx = i

                        self.active_market = {
                            "up_token": tokens[up_idx],
                            "down_token": tokens[down_idx],
                            "question": market.get("question", ""),
                            "slug": slug,
                        }
                        log_msg(f"[MARKETS] Found: {market.get('question','')[:60]}")
                        return

        except Exception as e:
            log_msg(f"[MARKETS] error: {e}")

    def get_token(self, direction: str) -> Optional[str]:
        if not self.active_market:
            return None
        return self.active_market["up_token"] if direction == "UP" else self.active_market["down_token"]


# ============================================================
# ORDER BOOK READER
# ============================================================
class BookReader:
    """Uses CLOB /price and /midpoint for unified book (not raw /book)."""

    def __init__(self):
        self.cache: dict[str, dict] = {}

    async def get_prices(self, token_id: str) -> Optional[dict]:
        cached = self.cache.get(token_id)
        if cached and time.time() - cached["time"] < 2.0:
            return cached

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{CLOB_HOST}/price",
                    params={"token_id": token_id, "side": "BUY"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return None
                    buy_data = await resp.json()

                async with session.get(
                    f"{CLOB_HOST}/price",
                    params={"token_id": token_id, "side": "SELL"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return None
                    sell_data = await resp.json()

                async with session.get(
                    f"{CLOB_HOST}/midpoint",
                    params={"token_id": token_id},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return None
                    mid_data = await resp.json()

            best_bid = float(sell_data.get("price", 0))
            best_ask = float(buy_data.get("price", 0))
            midpoint = float(mid_data.get("mid", 0))

            if best_bid <= 0 and best_ask <= 0:
                return None

            result = {
                "best_bid": best_bid, "best_ask": best_ask,
                "midpoint": midpoint,
                "spread": round(best_ask - best_bid, 4) if best_ask > best_bid else 0,
                "time": time.time(),
            }
            self.cache[token_id] = result
            return result
        except Exception:
            return None


# ============================================================
# TRADE TRACKING
# ============================================================
@dataclass
class Trade:
    id: int
    direction: str
    token_id: str
    entry_token_price: float
    entry_fee: float
    entry_btc_coinbase: float
    entry_btc_chainlink: float
    gap: float
    shares: float
    cost_usd: float
    bet_size: float
    bankroll_at_entry: float
    opened_at: float
    order_id: Optional[str] = None
    closed_at: Optional[float] = None
    exit_token_price: Optional[float] = None
    exit_fee: Optional[float] = None
    pnl: Optional[float] = None
    exit_reason: Optional[str] = None
    is_paper: bool = True


# ============================================================
# THE BOT
# ============================================================
class EndgameProductionBot:
    STATE_FILE = "logs/endgame_5min_state.json"

    def __init__(self):
        self.market_finder = MarketFinder()
        self.book_reader = BookReader()
        self.clob_client = None

        # Price feeds
        self.cb_price: float = 0.0
        self.cb_time: float = 0.0
        self.cl_price: float = 0.0
        self.cl_time: float = 0.0
        self.cb_ticks: int = 0
        self.cl_ticks: int = 0

        # Bankroll (Kelly tracking)
        self.bankroll = STARTING_BANKROLL
        self.peak_bankroll = STARTING_BANKROLL
        self.max_drawdown_pct = 0.0
        self.max_drawdown_usd = 0.0

        # Trading state
        self.open_trade: Optional[Trade] = None
        self._pending_lock: bool = False
        self._selling_lock: bool = False
        self.closed_trades: list[Trade] = []
        self.trade_counter = 0
        self.last_trade_time = 0.0
        self.signals = 0

        # Staleness threshold — force-close if no price update for this many seconds
        self.STALE_SECONDS = 15.0

        # Streak tracking
        self.current_loss_streak = 0
        self.max_consec_losses = 0
        self.all_loss_streaks: list[int] = []

        # Gap history
        self.gap_history: deque = deque(maxlen=10000)

        self.start_time = time.time()

        # Logging
        os.makedirs("logs", exist_ok=True)
        self.log_file = open("logs/prod_endgame_5min_trades.jsonl", "a")
        self.bankroll_log = open("logs/prod_endgame_5min_bankroll.csv", "a")
        self.bankroll_log.write("timestamp,bankroll,peak,drawdown_pct\n")

        # Restore state from disk if exists (crash recovery)
        self._restore_state()

        # Telegram notifications
        self.notifier = TelegramNotifier() if TelegramNotifier else None
        self.BOT_NAME = "ENDGAME 5M"

        # CLOB client for live mode
        if not DRY_RUN:
            self._init_clob()

    # ------- STATE PERSISTENCE (crash recovery) -------

    def _save_state(self):
        """Save open trade and bankroll to disk after every trade open/close."""
        state = {
            "bankroll": self.bankroll,
            "peak_bankroll": self.peak_bankroll,
            "trade_counter": self.trade_counter,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_drawdown_usd": self.max_drawdown_usd,
            "current_loss_streak": self.current_loss_streak,
            "max_consec_losses": self.max_consec_losses,
            "open_trade": None,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.open_trade and self.open_trade.id > 0:
            t = self.open_trade
            state["open_trade"] = {
                "id": t.id, "direction": t.direction, "token_id": t.token_id,
                "entry_token_price": t.entry_token_price, "entry_fee": t.entry_fee,
                "entry_btc_coinbase": t.entry_btc_coinbase,
                "entry_btc_chainlink": t.entry_btc_chainlink,
                "gap": t.gap, "shares": t.shares, "cost_usd": t.cost_usd,
                "bet_size": t.bet_size, "bankroll_at_entry": t.bankroll_at_entry,
                "opened_at": t.opened_at, "order_id": t.order_id,
                "is_paper": t.is_paper,
            }
        try:
            with open(self.STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log_msg(f"[STATE] save error: {e}")

    def _restore_state(self):
        """Restore state from disk on startup (crash recovery)."""
        try:
            if not os.path.exists(self.STATE_FILE):
                return
            with open(self.STATE_FILE, "r") as f:
                state = json.load(f)

            self.bankroll = state.get("bankroll", STARTING_BANKROLL)
            self.peak_bankroll = state.get("peak_bankroll", STARTING_BANKROLL)
            self.trade_counter = state.get("trade_counter", 0)
            self.max_drawdown_pct = state.get("max_drawdown_pct", 0)
            self.max_drawdown_usd = state.get("max_drawdown_usd", 0)
            self.current_loss_streak = state.get("current_loss_streak", 0)
            self.max_consec_losses = state.get("max_consec_losses", 0)

            ot = state.get("open_trade")
            if ot:
                # Check if the trade is too old (> 15 minutes = market expired)
                age = time.time() - ot["opened_at"]
                if age < 300:  # less than 5 minutes old
                    self.open_trade = Trade(
                        id=ot["id"], direction=ot["direction"], token_id=ot["token_id"],
                        entry_token_price=ot["entry_token_price"], entry_fee=ot["entry_fee"],
                        entry_btc_coinbase=ot["entry_btc_coinbase"],
                        entry_btc_chainlink=ot["entry_btc_chainlink"],
                        gap=ot["gap"], shares=ot["shares"], cost_usd=ot["cost_usd"],
                        bet_size=ot["bet_size"], bankroll_at_entry=ot["bankroll_at_entry"],
                        opened_at=ot["opened_at"], order_id=ot.get("order_id"),
                        is_paper=ot.get("is_paper", True),
                    )
                    log_msg(f"[STATE] Restored open trade #{ot['id']} {ot['direction']}"
                            f" @ ${ot['entry_token_price']:.2f} (age: {age:.0f}s)")
                else:
                    # Trade too old — market likely resolved, count as loss
                    log_msg(f"[STATE] Expired trade #{ot['id']} (age: {age:.0f}s) — marking as loss")
                    self.bankroll += 0  # money is gone, was already deducted
                    self.current_loss_streak += 1
                    if self.current_loss_streak > self.max_consec_losses:
                        self.max_consec_losses = self.current_loss_streak

            log_msg(f"[STATE] Restored: bankroll=${self.bankroll:.2f}, "
                    f"trades={self.trade_counter}, peak=${self.peak_bankroll:.2f}")

        except Exception as e:
            log_msg(f"[STATE] restore error: {e} — starting fresh")

    def _init_clob(self):
        if not CLOB_AVAILABLE:
            log_msg("[ERROR] py-clob-client not installed")
            return
        if not PRIVATE_KEY:
            log_msg("[ERROR] POLYMARKET_PRIVATE_KEY not set")
            return
        try:
            self.clob_client = ClobClient(
                CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS,
            )
            self.clob_client.set_api_creds(
                self.clob_client.create_or_derive_api_creds()
            )
            log_msg("[CLOB] Authenticated")
        except Exception as e:
            log_msg(f"[ERROR] CLOB auth failed: {e}")

    def update_bankroll(self):
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll
        dd_usd = self.peak_bankroll - self.bankroll
        dd_pct = dd_usd / self.peak_bankroll if self.peak_bankroll > 0 else 0
        if dd_pct > self.max_drawdown_pct:
            self.max_drawdown_pct = dd_pct
            self.max_drawdown_usd = dd_usd

        ts_iso = datetime.now(timezone.utc).isoformat()
        self.bankroll_log.write(
            f"{ts_iso},{self.bankroll:.2f},{self.peak_bankroll:.2f},{dd_pct*100:.2f}\n"
        )
        self.bankroll_log.flush()

    # ------- PRICE HANDLERS -------

    def on_coinbase(self, price: float):
        self.cb_price = price
        self.cb_time = time.time()
        self.cb_ticks += 1

        if self.cl_price > 0:
            gap = price - self.cl_price
            self.gap_history.append(abs(gap))
            self.check_signal(gap)
            self.check_exit()

    def on_chainlink(self, price: float):
        self.cl_price = price
        self.cl_time = time.time()
        self.cl_ticks += 1
        self.check_exit()

    # ------- SIGNAL (v2 parameters) -------

    def check_signal(self, gap: float):
        if abs(gap) < BTC_MIN_GAP:
            return
        if self.open_trade is not None or self._pending_lock:
            return
        if time.time() - self.last_trade_time < COOLDOWN:
            return
        if self.cl_time > 0 and time.time() - self.cl_time < 0.5:
            return
        if self.bankroll < 5.0:
            return

        self.signals += 1
        direction = "UP" if gap > 0 else "DOWN"

        # Set lock IMMEDIATELY — use a simple string sentinel, not a Trade object
        # This prevents check_exit from running on a half-built trade
        self._pending_lock = True
        self.last_trade_time = time.time()
        asyncio.create_task(self._safe_open(direction, gap))

    async def _safe_open(self, direction: str, gap: float):
        """Wrapper that guarantees lock release on any failure."""
        try:
            await self.open_position(direction, gap)
        except Exception as e:
            log_msg(f"[ERROR] open_position failed: {e}")
        finally:
            self._pending_lock = False

    # ------- OPEN TRADE -------

    async def open_position(self, direction: str, gap: float):
        # Find market
        await self.market_finder.refresh()
        token_id = self.market_finder.get_token(direction)
        if not token_id:
            return

        # Read real unified order book
        book = await self.book_reader.get_prices(token_id)

        # ENDGAME with MAKER limit orders:
        # Only enter when the token is genuinely in ENDGAME territory (>= $0.70)
        # Use real unified book prices — NEVER a hardcoded default
        if not book or book.get("midpoint", 0) < 0.75:
            # Token isn't in ENDGAME zone — skip
            return

        if book["best_bid"] >= 0.78:
            entry_price = snap_price(book["best_bid"] + 0.01)
        elif book["midpoint"] >= 0.78:
            entry_price = snap_price(book["midpoint"])
        else:
            # Not quite ENDGAME yet, skip
            return

        # Safety check — target entry around $0.80
        if entry_price < 0.78 or entry_price > 0.98:
            return

        # Kelly sizing: 10% of bankroll, capped
        bet_size = min(self.bankroll * KELLY_FRACTION, MAX_BET_CAP)

        shares = bet_size / entry_price

        # Buy minimum 6 shares to ensure 5+ after Polymarket rounding/dust
        if shares < 6.0:
            shares = 6.0
            bet_size = shares * entry_price

        # Can we afford this minimum?
        if bet_size > self.bankroll:
            log_msg(f"[SKIP]  Bankroll ${self.bankroll:.2f} too low for min 6 shares @ ${entry_price:.2f}")
            return

        # MAKER: zero taker fees, earn rebates instead
        entry_fee = 0.0
        cost = shares * entry_price

        self.trade_counter += 1

        trade = Trade(
            id=self.trade_counter, direction=direction, token_id=token_id,
            entry_token_price=entry_price, entry_fee=entry_fee,
            entry_btc_coinbase=self.cb_price, entry_btc_chainlink=self.cl_price,
            gap=gap, shares=shares, cost_usd=cost, bet_size=bet_size,
            bankroll_at_entry=self.bankroll, opened_at=time.time(),
            is_paper=DRY_RUN,
        )

        if DRY_RUN:
            trade.order_id = f"PAPER-{trade.id}"
            book_str = f"bid=${book['best_bid']:.2f} ask=${book['best_ask']:.2f} mid=${book.get('midpoint',0):.2f}" if book else "no book"
            log_msg(f"[PAPER] OPEN  #{trade.id} {direction} @ ${entry_price:.2f}"
                    f" | bet=${bet_size:.2f} | gap=${gap:+.1f}"
                    f" | {book_str} | bank=${self.bankroll:.2f}")
        else:
            order_id = await self._submit_order(token_id, entry_price, shares)
            if order_id:
                trade.order_id = order_id
                log_msg(f"[LIVE]  OPEN  #{trade.id} {direction} @ ${entry_price:.2f}"
                        f" | bet=${bet_size:.2f} | order={order_id[:12]}...")
            else:
                log_msg(f"[LIVE]  ORDER FAILED #{trade.id}")
                return

        self.open_trade = trade
        self.last_trade_time = time.time()
        self.bankroll -= cost
        self.update_bankroll()
        self._log_trade(trade, "OPEN")
        self._save_state()

        # Telegram notification
        if self.notifier:
            asyncio.create_task(self.notifier.send_trade(
                "OPEN", trade.id, direction, entry_price,
                bankroll=self.bankroll, bot_name=self.BOT_NAME))

    async def _submit_order(self, token_id: str, price: float, size: float) -> Optional[str]:
        if not self.clob_client:
            return None

        for attempt in range(2):  # try twice
            try:
                order_args = OrderArgs(price=price, size=size, side=BUY, token_id=token_id)
                signed = self.clob_client.create_order(order_args)
                resp = self.clob_client.post_order(signed, OrderType.GTC)
                order_id = resp.get("orderID") or resp.get("order_id") if resp else None
                if order_id:
                    return order_id
                log_msg(f"[CLOB] No order ID in response (attempt {attempt+1}): {resp}")
            except Exception as e:
                log_msg(f"[CLOB] Order error (attempt {attempt+1}): {e}")

            if attempt == 0:
                await asyncio.sleep(1)  # wait 1 second before retry

        return None

    async def _submit_sell_order(self, token_id: str, price: float, size: float) -> Optional[str]:
        """Submit a SELL limit order to close a position. Returns order ID or None.
        Raises exception for 'does not exist' errors so caller can handle expired markets."""
        if not self.clob_client:
            return None

        for attempt in range(2):
            try:
                order_args = OrderArgs(price=price, size=size, side=SELL, token_id=token_id)
                signed = self.clob_client.create_order(order_args)
                resp = self.clob_client.post_order(signed, OrderType.GTC)
                order_id = resp.get("orderID") or resp.get("order_id") if resp else None
                if order_id:
                    return order_id
                log_msg(f"[CLOB] No sell order ID (attempt {attempt+1}): {resp}")
            except Exception as e:
                error_str = str(e)
                if "does not exist" in error_str:
                    # Market expired — re-raise so _execute_sell can handle it
                    raise
                log_msg(f"[CLOB] Sell error (attempt {attempt+1}): {e}")

            if attempt == 0:
                await asyncio.sleep(1)

        return None

    # ------- EXIT LOGIC -------

    def check_exit(self):
        trade = self.open_trade
        if trade is None or self._pending_lock:
            return
        # Skip if trade hasn't been fully initialized yet
        if trade.token_id == "pending" or trade.id == 0:
            return

        # STALENESS SAFETY: if no price updates for 15+ seconds, force-close
        now = time.time()
        cb_stale = (now - self.cb_time) > self.STALE_SECONDS if self.cb_time > 0 else False
        cl_stale = (now - self.cl_time) > self.STALE_SECONDS if self.cl_time > 0 else False

        if cb_stale and cl_stale and self.open_trade is not None:
            log_msg(f"[SAFETY] Feeds stale (CB: {now - self.cb_time:.0f}s, CL: {now - self.cl_time:.0f}s) — force closing")
            exit_price = snap_price(trade.entry_token_price)  # exit at entry (flat)

            if not DRY_RUN and trade.token_id and trade.token_id != "pending":
                asyncio.create_task(self._execute_sell(trade, "STALE_FEED"))
            else:
                proceeds = trade.shares * exit_price
                pnl = round(proceeds - trade.cost_usd, 2)
                self._finalize_exit(trade, exit_price, 0.0, pnl, "STALE_FEED")
            return

        if trade.direction == "UP":
            cl_move = self.cl_price - trade.entry_btc_chainlink
        else:
            cl_move = trade.entry_btc_chainlink - self.cl_price

        age = time.time() - trade.opened_at

        # Estimate current token price from Chainlink movement
        ticks = cl_move / PRICE_PER_TICK
        estimated_token = snap_price(trade.entry_token_price + ticks * 0.01)

        exit_price = trade.entry_token_price
        reason = None

        # STOP LOSS: if estimated token price drops to $0.44 or below, market sell immediately
        if estimated_token <= 0.44:
            exit_price = snap_price(0.44)
            reason = "STOP_LOSS"
        # WIN: strong confirmation, token near $1.00
        elif cl_move > PRICE_PER_TICK * 2:
            exit_price = snap_price(0.99)
            reason = "RESOLVED_WIN"
        # TIME EXIT
        elif age > TIME_EXIT:
            if ticks >= 1:
                exit_price = snap_price(min(0.95, trade.entry_token_price + ticks * 0.01))
            elif ticks <= -1:
                exit_price = snap_price(max(0.44, trade.entry_token_price + ticks * 0.01))
            else:
                exit_price = snap_price(trade.entry_token_price)
            reason = "TIME"

        if reason:
            # In LIVE mode, submit a real SELL order
            if not DRY_RUN and trade.token_id and trade.token_id != "pending":
                # Don't fire another sell if one is already in progress
                if hasattr(self, '_selling_lock') and self._selling_lock:
                    return
                asyncio.create_task(self._execute_sell(trade, reason))
                return  # async sell will handle the rest

            # PAPER mode: calculate P&L from estimated price
            exit_fee = 0.0
            rebate = round(trade.shares * exit_price * 0.0312 * 4 *
                          exit_price * (1 - exit_price) * 0.25, 4)
            proceeds = trade.shares * exit_price + rebate
            pnl = round(proceeds - trade.cost_usd, 2)

            self._finalize_exit(trade, exit_price, 0.0, pnl, reason)

    async def _execute_sell(self, trade, reason: str):
        """Submit a real SELL order to close the position."""
        if hasattr(self, '_selling_lock') and self._selling_lock:
            return
        self._selling_lock = True

        try:
            sell_shares = trade.shares

            if reason == "STOP_LOSS":
                # STOP LOSS: market order — sell at whatever price, get out NOW
                log_msg(f"[LIVE]  STOP LOSS #{trade.id} — market selling {sell_shares:.1f} shares...")
                sell_order_id = await self._submit_market_sell(trade.token_id, sell_shares)

                if sell_order_id:
                    book = await self.book_reader.get_prices(trade.token_id)
                    sell_price = book["best_bid"] if book and book["best_bid"] > 0 else 0.44
                    log_msg(f"[LIVE]  MARKET SELL #{trade.id} | order={sell_order_id[:12]}...")
                    proceeds = sell_shares * sell_price
                    pnl = round(proceeds - trade.cost_usd, 2)
                    self._finalize_exit(trade, sell_price, 0.0, pnl, reason)
                else:
                    log_msg(f"[LIVE]  MARKET SELL FAILED #{trade.id} — retry in 10s")
                    await asyncio.sleep(10)
            else:
                # TAKE PROFIT / TIME EXIT: limit order at book bid
                book = await self.book_reader.get_prices(trade.token_id)
                if book and book["best_bid"] > 0.05:
                    sell_price = snap_price(book["best_bid"])
                else:
                    if trade.direction == "UP":
                        cl_move = self.cl_price - trade.entry_btc_chainlink
                    else:
                        cl_move = trade.entry_btc_chainlink - self.cl_price
                    ticks = cl_move / PRICE_PER_TICK
                    sell_price = snap_price(trade.entry_token_price + ticks * 0.01)
                    if sell_price < 0.05:
                        sell_price = 0.05

                sell_order_id = await self._submit_sell_order(trade.token_id, sell_price, sell_shares)

                if not sell_order_id and sell_shares >= 5.0:
                    sell_shares = round(sell_shares - 0.1, 2)
                    log_msg(f"[LIVE]  Retrying sell with {sell_shares:.2f} shares...")
                    sell_order_id = await self._submit_sell_order(trade.token_id, sell_price, sell_shares)

                if sell_order_id:
                    log_msg(f"[LIVE]  SELL #{trade.id} @ ${sell_price:.2f}"
                            f" | {sell_shares:.1f} shares | order={sell_order_id[:12]}...")
                    proceeds = sell_shares * sell_price
                    pnl = round(proceeds - trade.cost_usd, 2)
                    self._finalize_exit(trade, sell_price, 0.0, pnl, reason)
                else:
                    log_msg(f"[LIVE]  SELL FAILED #{trade.id} — retry in 30s")
                    await asyncio.sleep(30)

        except Exception as e:
            error_str = str(e)
            if "does not exist" in error_str:
                log_msg(f"[LIVE]  Market expired for #{trade.id} — claim on polymarket.com")
                proceeds = trade.shares * 0.99
                pnl = round(proceeds - trade.cost_usd, 2)
                self._finalize_exit(trade, 0.99, 0.0, pnl, "EXPIRED_CLAIM")
                if self.notifier:
                    asyncio.create_task(self.notifier.send_error(
                        f"Market expired — claim #{trade.id} on polymarket.com",
                        bot_name=self.BOT_NAME))
            else:
                log_msg(f"[LIVE]  SELL ERROR #{trade.id}: {e}")
                await asyncio.sleep(30)
        finally:
            self._selling_lock = False

    async def _submit_market_sell(self, token_id: str, size: float) -> Optional[str]:
        """Submit a FOK market sell order — sells at whatever price is available."""
        if not self.clob_client:
            return None
        try:
            mo = MarketOrderArgs(token_id=token_id, amount=size, side=SELL)
            signed = self.clob_client.create_market_order(mo)
            resp = self.clob_client.post_order(signed, OrderType.FOK)
            return resp.get("orderID") or resp.get("order_id") if resp else None
        except Exception as e:
            log_msg(f"[CLOB] Market sell error: {e}")
            # If market doesn't exist, re-raise for _execute_sell to handle
            if "does not exist" in str(e):
                raise
            return None

    def _finalize_exit(self, trade, exit_price: float, exit_fee: float, pnl: float, reason: str):
        """Finalize a closed trade — update bankroll, streaks, logs, notifications."""
        trade.closed_at = time.time()
        trade.exit_token_price = exit_price
        trade.exit_fee = exit_fee
        trade.pnl = pnl
        trade.exit_reason = reason

        self.open_trade = None
        self.closed_trades.append(trade)

        # Update bankroll
        proceeds = trade.shares * exit_price
        self.bankroll += proceeds
        self.update_bankroll()

        # Streak tracking
        if pnl > 0:
            if self.current_loss_streak > 0:
                self.all_loss_streaks.append(self.current_loss_streak)
            self.current_loss_streak = 0
        else:
            self.current_loss_streak += 1
            if self.current_loss_streak > self.max_consec_losses:
                self.max_consec_losses = self.current_loss_streak

        mode = "PAPER" if DRY_RUN else "LIVE"
        log_msg(f"[{mode}] CLOSE #{trade.id} {trade.direction}"
                f" @ ${exit_price:.2f} | P&L=${pnl:+.2f} | {reason}"
                f" | bank=${self.bankroll:.2f}")
        self._log_trade(trade, "CLOSE")
        self._save_state()

        # Telegram notification
        if self.notifier:
            asyncio.create_task(self.notifier.send_trade(
                "CLOSE", trade.id, trade.direction, exit_price,
                pnl=pnl, reason=reason, bankroll=self.bankroll,
                bot_name=self.BOT_NAME))

    def _log_trade(self, trade: Trade, action: str):
        entry = {
            "action": action,
            "mode": "PAPER" if trade.is_paper else "LIVE",
            "id": trade.id, "direction": trade.direction,
            "token_id": trade.token_id[:20] + "..." if trade.token_id else None,
            "entry_price": trade.entry_token_price,
            "exit_price": trade.exit_token_price,
            "entry_fee": trade.entry_fee, "exit_fee": trade.exit_fee,
            "gap": round(trade.gap, 2),
            "bet_size": round(trade.bet_size, 2),
            "shares": round(trade.shares, 1),
            "pnl": trade.pnl, "reason": trade.exit_reason,
            "bankroll": round(self.bankroll, 2),
            "duration": round((trade.closed_at or time.time()) - trade.opened_at, 1),
            "time": datetime.now(timezone.utc).isoformat(),
        }
        self.log_file.write(json.dumps(entry) + "\n")
        self.log_file.flush()

    # ------- STATUS -------

    def print_status(self):
        elapsed = time.time() - self.start_time
        wins = sum(1 for t in self.closed_trades if t.pnl and t.pnl > 0)
        losses = len(self.closed_trades) - wins
        total_pnl = self.bankroll - STARTING_BANKROLL
        wr = (wins / len(self.closed_trades) * 100) if self.closed_trades else 0
        gaps = list(self.gap_history)
        avg_gap = sum(gaps) / len(gaps) if gaps else 0
        mode = "PAPER" if DRY_RUN else "LIVE"

        # Current bet size
        current_bet = min(self.bankroll * KELLY_FRACTION, MAX_BET_CAP)

        market_str = "found" if self.market_finder.active_market else "searching..."
        open_str = f"#{self.open_trade.id} {self.open_trade.direction}" if self.open_trade else "none"

        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {mode} | {elapsed/60:.0f}min | BTC ENDGAME 5-MIN (10% Kelly)")
        print(f"  Bankroll: ${self.bankroll:,.2f} (${total_pnl:+,.2f})"
              f" | Peak: ${self.peak_bankroll:,.2f}")
        print(f"  Bet size: ${current_bet:,.2f} | DD: {self.max_drawdown_pct*100:.1f}%"
              f" (${self.max_drawdown_usd:,.2f})")
        print(f"  Trades: {len(self.closed_trades)} ({wins}W/{losses}L)"
              f" | Win%: {wr:.0f}% | Consec losses: {self.max_consec_losses}")
        print(f"  Signals: {self.signals} | Avg gap: ${avg_gap:.2f}"
              f" | Market: {market_str} | Open: {open_str}")
        print(f"  Feeds: CB={self.cb_ticks:,} CL={self.cl_ticks:,}"
              f" | CB=${self.cb_price:,.2f} CL=${self.cl_price:,.2f}")
        print()

    def print_final(self):
        elapsed = time.time() - self.start_time
        total_pnl = self.bankroll - STARTING_BANKROLL
        wins = sum(1 for t in self.closed_trades if t.pnl and t.pnl > 0)
        losses = len(self.closed_trades) - wins
        wr = (wins / len(self.closed_trades) * 100) if self.closed_trades else 0
        mode = "PAPER" if DRY_RUN else "LIVE"

        print("\n" + "=" * 65)
        print(f"  ENDGAME BTC — FINAL RESULTS ({mode})")
        print("=" * 65)
        print(f"  Runtime:       {elapsed/3600:.1f}h ({elapsed/60:.0f}min)")
        print(f"  Starting:      ${STARTING_BANKROLL:,.2f}")
        print(f"  Final:         ${self.bankroll:,.2f}")
        print(f"  Profit:        ${total_pnl:+,.2f} ({(self.bankroll/STARTING_BANKROLL-1)*100:+.1f}%)")
        print(f"  Trades:        {len(self.closed_trades)} ({wins}W/{losses}L, {wr:.0f}%)")
        print(f"  Kelly:         {KELLY_FRACTION*100:.0f}% | Min gap: ${BTC_MIN_GAP}")
        print(f"  Max drawdown:  {self.max_drawdown_pct*100:.1f}% (${self.max_drawdown_usd:,.2f})")
        print(f"  Max consec L:  {self.max_consec_losses}")
        print(f"  Peak bankroll: ${self.peak_bankroll:,.2f}")

        if self.closed_trades:
            avg_pnl = total_pnl / len(self.closed_trades)
            avg_dur = sum((t.closed_at or t.opened_at) - t.opened_at
                         for t in self.closed_trades) / len(self.closed_trades)
            best = max(self.closed_trades, key=lambda t: t.pnl or 0)
            worst = min(self.closed_trades, key=lambda t: t.pnl or 0)
            print(f"  Avg P&L/trade: ${avg_pnl:+.2f}")
            print(f"  Avg duration:  {avg_dur:.0f}s")
            print(f"  Best trade:    #{best.id} ${best.pnl:+.2f} ({best.exit_reason})")
            print(f"  Worst trade:   #{worst.id} ${worst.pnl:+.2f} ({worst.exit_reason})")

            reasons = {}
            for t in self.closed_trades:
                r = t.exit_reason or "?"
                reasons[r] = reasons.get(r, 0) + 1
            print(f"  Exit reasons:  {', '.join(f'{k}={v}' for k,v in sorted(reasons.items()))}")

        if self.all_loss_streaks:
            print(f"  Loss streaks:  {sorted(self.all_loss_streaks, reverse=True)[:10]}")

        gaps = list(self.gap_history)
        if gaps:
            print(f"  Gap stats:     avg=${sum(gaps)/len(gaps):.2f} max=${max(gaps):.2f}")

        print(f"\n  Logs: logs/prod_endgame_trades.jsonl")
        print(f"        logs/prod_endgame_bankroll.csv")
        print("=" * 65)

        self.log_file.close()
        self.bankroll_log.close()


# ============================================================
# WEBSOCKET FEEDS
# ============================================================
async def run_coinbase(bot: EndgameProductionBot):
    while True:
        try:
            async with websockets.connect("wss://ws-feed.exchange.coinbase.com") as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "product_ids": ["BTC-USD"],
                    "channels": ["ticker"]
                }))
                log_msg("[FEED] Coinbase BTC connected")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if data.get("type") == "ticker":
                        bot.on_coinbase(float(data["price"]))
        except Exception as e:
            log_msg(f"[FEED] Coinbase reconnecting: {e}")
            await asyncio.sleep(2)


async def run_chainlink(bot: EndgameProductionBot):
    while True:
        try:
            async with websockets.connect(
                "wss://ws-live-data.polymarket.com", ping_interval=None
            ) as ws:
                await ws.send(json.dumps({
                    "action": "subscribe",
                    "subscriptions": [{
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        "filters": ""
                    }]
                }))
                log_msg("[FEED] Chainlink BTC connected")

                async def pinger():
                    while True:
                        await asyncio.sleep(5)
                        await ws.send("PING")
                asyncio.create_task(pinger())

                while True:
                    msg = await ws.recv()
                    if msg == "PONG" or not msg.startswith("{"):
                        continue
                    data = json.loads(msg)
                    if data.get("topic") == "crypto_prices_chainlink":
                        p = data.get("payload", {})
                        if "btc" in p.get("symbol", "").lower():
                            bot.on_chainlink(float(p["value"]))
        except Exception as e:
            log_msg(f"[FEED] Chainlink reconnecting: {e}")
            await asyncio.sleep(2)


async def run_status(bot: EndgameProductionBot):
    await bot.market_finder.refresh()
    status_count = 0
    while True:
        await asyncio.sleep(60)
        await bot.market_finder.refresh()
        bot.print_status()
        status_count += 1

        # Send Telegram status every 15 minutes
        if status_count % 15 == 0 and bot.notifier:
            total_pnl = bot.bankroll - STARTING_BANKROLL
            wins = sum(1 for t in bot.closed_trades if t.pnl and t.pnl > 0)
            wr = (wins / len(bot.closed_trades) * 100) if bot.closed_trades else 0
            elapsed = (time.time() - bot.start_time) / 60
            await bot.notifier.send_status(
                bot.bankroll, total_pnl, len(bot.closed_trades),
                wr, bot.max_drawdown_pct * 100, elapsed,
                bot_name=bot.BOT_NAME)


async def main():
    mode = "PAPER" if DRY_RUN else "*** LIVE ***"
    print("=" * 65)
    print(f"  ENDGAME BTC 5-MIN — {mode}")
    print("=" * 65)
    print()
    print(f"  Mode:        {mode}")
    print(f"  Asset:       BTC only")
    print(f"  Bankroll:    ${STARTING_BANKROLL:,.2f}")
    print(f"  Kelly:       {KELLY_FRACTION*100:.0f}% (bet=${STARTING_BANKROLL * KELLY_FRACTION:.2f} initial)")
    print(f"  Max bet:     ${MAX_BET_CAP:,.0f}")
    print(f"  Min gap:     ${BTC_MIN_GAP:.2f} (fixed)")
    print(f"  Price/tick:  ${PRICE_PER_TICK:.2f}")
    print(f"  Entry zone:  >= $0.70 (from real book prices)")
    print(f"  Fees:        MAKER (zero fees + rebates)")
    print(f"  Cooldown:    {COOLDOWN:.0f}s | Time exit: {TIME_EXIT:.0f}s")

    if not DRY_RUN and FUNDER_ADDRESS:
        print(f"  Wallet:      {FUNDER_ADDRESS[:10]}...{FUNDER_ADDRESS[-6:]}")

    print()
    print(f"  Press Ctrl+C to stop.")
    print()

    bot = EndgameProductionBot()

    # Telegram startup notification
    if bot.notifier:
        await bot.notifier.send_startup(bot_name=bot.BOT_NAME, mode=mode)

    tasks = [
        asyncio.create_task(run_coinbase(bot)),
        asyncio.create_task(run_chainlink(bot)),
        asyncio.create_task(run_status(bot)),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        bot.print_final()

        # Telegram shutdown notification
        if bot.notifier:
            total_pnl = bot.bankroll - STARTING_BANKROLL
            await bot.notifier.send_shutdown(
                bot_name=bot.BOT_NAME, bankroll=bot.bankroll,
                pnl=total_pnl, trades=len(bot.closed_trades))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
