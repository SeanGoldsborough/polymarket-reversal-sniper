#!/usr/bin/env python3
"""
F2LiveMarket — BTC 5-Minute Markets
=====================================
T-45 entry on the leading side (market order).
One trade per 5-minute window maximum.
Stop loss at $0.44 via market order.
Min bet = 5 shares, Kelly 10% kicks in when bankroll > $60.

Run:  python3 prod_endgame_5min_ALT.py
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
# CONFIGURATION — F2LiveMarket
# ============================================================
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Bet sizing — 10% of bankroll, min 6 shares
BET_PCT = 0.10
KELLY_FRACTION = BET_PCT  # alias for compatibility
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL_F2", "80.0"))
MAX_BET_CAP = float(os.getenv("MAX_BET_CAP_F2", "2000.0"))
KELLY_MIN_BANKROLL = 60.0

# F2 strategy: T-45 entry, hold to $0.99, dynamic SL
TRIGGER_REMAINING = 45     # enter with 45 seconds left in window
EXIT_PRICE = 0.99          # TP: bid hits $0.99 → treated as $1.00 resolution
SL_OFFSET = 0.10           # SL: entry_price - $0.10
ENTRY_PRICE = 0.50         # placeholder for compatibility (actual entry = market price)
STOP_LOSS_PRICE = 0.40     # placeholder (actual SL is dynamic per trade)

# Not used — kept for compatibility with shared code
BTC_MIN_GAP = 15.0
BTC_MAX_GAP = 18.0
PRICE_PER_TICK = 10.0
COOLDOWN = 5.0
TIME_EXIT = 30.0

# CLOB
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon


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
    tp_order_id: Optional[str] = None
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
    STATE_FILE = "logs/f2_live_market_state.json"

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

        # Bankroll — fetched live from wallet, fallback to STARTING_BANKROLL
        self.bankroll = STARTING_BANKROLL  # overwritten by async init
        self.peak_bankroll = STARTING_BANKROLL
        self.max_drawdown_pct = 0.0
        self.max_drawdown_usd = 0.0
        self._last_balance_fetch = 0.0
        self._balance_fetch_interval = 30.0  # re-fetch every 30s

        # Trading state
        self.open_trade: Optional[Trade] = None
        self._pending_lock: bool = False
        self._selling_lock: bool = False
        self.closed_trades: list[Trade] = []
        self.trade_counter = 0
        self.last_trade_time = 0.0
        self.signals = 0

        # ALT: 1 trade per 5-min window
        self.last_trade_window = 0  # timestamp of the window we last traded in

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
        self.log_file = open("logs/f2_live_market_trades.jsonl", "a")
        self.bankroll_log = open("logs/f2_live_market_bankroll.csv", "a")
        self.bankroll_log.write("timestamp,bankroll,peak,drawdown_pct\n")
        self.trajectory_log = open("logs/f2_live_market_trajectories.jsonl", "a")

        # Restore state from disk if exists (crash recovery)
        self._restore_state()

        # Telegram notifications
        self.notifier = TelegramNotifier() if TelegramNotifier else None
        self.BOT_NAME = "F2 LIVE MARKET"

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

            # bankroll synced live from wallet; peak still restored from state
            self.peak_bankroll = state.get("peak_bankroll", STARTING_BANKROLL)
            self.trade_counter = state.get("trade_counter", 0)
            self.max_drawdown_pct = state.get("max_drawdown_pct", 0)
            self.max_drawdown_usd = state.get("max_drawdown_usd", 0)
            self.current_loss_streak = state.get("current_loss_streak", 0)
            self.max_consec_losses = state.get("max_consec_losses", 0)

            ot = state.get("open_trade")
            if ot:
                age = time.time() - ot["opened_at"]
                if age > 300:
                    # Trade older than 5 min window — market resolved, discard
                    log_msg(f"[STATE] Trade #{ot['id']} expired (age: {age:.0f}s) — clearing")
                else:
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

    async def fetch_wallet_balance(self) -> Optional[float]:
        """Fetch live USDC balance by querying the USDC contract on Polygon."""
        wallet = PROXY_WALLET or FUNDER_ADDRESS
        if not wallet:
            return None
        # USDC.e on Polygon — balanceOf(address) selector = 0x70a08231
        padded_addr = wallet.lower().replace("0x", "").zfill(64)
        call_data = f"0x70a08231{padded_addr}"
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": USDC_CONTRACT, "data": call_data}, "latest"]
        }
        rpc = os.getenv("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(rpc, json=payload, headers={"Content-Type": "application/json"},
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result", "0x0")
                        raw = int(result, 16)
                        # USDC has 6 decimals
                        return raw / 1_000_000
        except Exception as e:
            log_msg(f"[WALLET] Balance fetch error: {e}")
        return None

    async def sync_bankroll(self):
        """Sync bankroll from live wallet balance. Called periodically."""
        now = time.time()
        if now - self._last_balance_fetch < self._balance_fetch_interval:
            return
        self._last_balance_fetch = now
        balance = await self.fetch_wallet_balance()
        if balance is not None:
            old = self.bankroll
            self.bankroll = balance
            if abs(old - balance) > 0.01:
                log_msg(f"[WALLET] Bankroll synced: ${old:.2f} -> ${balance:.2f}")
            self.update_bankroll()

    # ------- PRICE HANDLERS -------

    def on_coinbase(self, price: float):
        self.cb_price = price
        self.cb_time = time.time()
        self.cb_ticks += 1
        if self.cl_price > 0:
            gap = price - self.cl_price
            self.gap_history.append(abs(gap))
        self.check_exit()

    def on_chainlink(self, price: float):
        self.cl_price = price
        self.cl_time = time.time()
        self.cl_ticks += 1
        self.check_exit()

    # ------- SIGNAL (F2: T-45 window watcher, no gap needed) -------

    def check_signal(self, gap: float):
        """Not used in F2 — signals come from the window watcher."""
        pass

        self._pending_lock = True
        self.last_trade_time = time.time()
        asyncio.create_task(self._safe_open(direction, gap))

    async def _safe_open(self, direction: str, gap: float):
        """Wrapper that guarantees lock release on any failure."""
        try:
            await self.sync_bankroll()
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

        # Read order book
        book = await self.book_reader.get_prices(token_id)
        if not book or book.get("best_ask", 0) <= 0:
            return

        # F2: buy at the ask price (market order style)
        ask = book.get("best_ask", 0)
        entry_price = snap_price(ask)

        # 10% of bankroll sizing, min 6 shares
        if self.bankroll >= KELLY_MIN_BANKROLL:
            bet_size = min(self.bankroll * BET_PCT, MAX_BET_CAP)
        else:
            bet_size = 6.0 * entry_price

        shares = round(bet_size / entry_price, 2)
        if shares < 6.0:
            shares = 6.0
            bet_size = shares * entry_price

        if bet_size > self.bankroll:
            log_msg(f"[SKIP]  Bankroll ${self.bankroll:.2f} too low for min 6 shares @ ${entry_price:.2f}")
            return

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
                    f" | bet=${bet_size:.2f} | {shares:.1f} shares | gap=${gap:+.1f}"
                    f" | {book_str} | bank=${self.bankroll:.2f}")
        else:
            # F2: market buy at ask price (GTC limit at ask for instant fill)
            order_id = await self._submit_order(token_id, entry_price, shares)
            if order_id:
                trade.order_id = order_id
                log_msg(f"[LIVE]  BUY #{trade.id} {direction} @ ${entry_price:.2f}"
                        f" | {shares:.1f} shares | order={order_id[:12]}...")
            else:
                log_msg(f"[LIVE]  ORDER FAILED #{trade.id}")
                return

        self.open_trade = trade
        # bankroll synced from wallet — no manual deduction needed
        self._log_trade(trade, "OPEN")
        self._save_state()

        # Mark this window as traded
        self.last_trade_window = int(time.time()) // 300

        # Telegram notification
        if self.notifier:
            asyncio.create_task(self.notifier.send_trade(
                "OPEN", trade.id, direction, entry_price,
                bankroll=self.bankroll, bot_name=self.BOT_NAME))

        # LIVE: place SL order on the book immediately, then monitor for TP
        if not DRY_RUN and trade.order_id:
            asyncio.create_task(self._place_sl_and_monitor(trade))

    async def _place_take_profit(self, trade):
        """Place a GTC limit sell at $0.94 after confirming buy has filled."""
        try:
            # Wait for buy to fill — the buy should fill instantly since we buy at ask
            await asyncio.sleep(3)

            # Check actual token balance instead of assuming full fill
            actual_balance = await self._get_token_balance(trade.token_id)
            if actual_balance > 0 and actual_balance < trade.shares:
                log_msg(f"[LIVE]  Partial fill detected: got {actual_balance:.2f} of {trade.shares:.2f} shares")
                trade.shares = actual_balance
                trade.cost_usd = actual_balance * trade.entry_token_price

            # Sell all shares — no dust left behind
            sell_shares = round(trade.shares, 2)
            if sell_shares < 1.0:
                log_msg(f"[LIVE]  Too few shares to place TP ({trade.shares:.2f}) — skipping TP order")
                return

            sell_order_id = await self._submit_sell_order(trade.token_id, EXIT_PRICE, sell_shares, quiet=True)
            if sell_order_id:
                trade.tp_order_id = sell_order_id
                log_msg(f"[LIVE]  TP ORDER #{trade.id} @ ${EXIT_PRICE:.2f}"
                        f" | {sell_shares:.1f} shares | order={sell_order_id[:12]}...")
                return

            # First attempt failed — wait and retry a few times quietly
            for i in range(5):
                await asyncio.sleep(5)
                sell_order_id = await self._submit_sell_order(trade.token_id, EXIT_PRICE, sell_shares, quiet=True)
                if sell_order_id:
                    trade.tp_order_id = sell_order_id
                    log_msg(f"[LIVE]  TP ORDER #{trade.id} @ ${EXIT_PRICE:.2f}"
                            f" | {sell_shares:.1f} shares | order={sell_order_id[:12]}...")
                    return

            # Still failed after 28s — check if we actually hold the position
            actual_balance = await self._get_token_balance(trade.token_id)
            if actual_balance >= 0.5:
                # We DO hold shares — don't orphan the trade. Keep it open
                # so the stop loss monitor can protect it. TP will be retried
                # by the stop loss monitor when bid >= EXIT_PRICE.
                log_msg(f"[LIVE]  TP failed but holding {actual_balance:.2f} shares — "
                        f"keeping #{trade.id} open for SL/TP monitor")
                trade.shares = actual_balance
                return

            # No shares held — safe to cancel and clear
            log_msg(f"[LIVE]  TP failed and no balance — cancelling buy #{trade.id}")
            if trade.order_id:
                try:
                    self.clob_client.cancel(trade.order_id)
                except Exception:
                    pass
            self.open_trade = None
            self._last_balance_fetch = 0.0  # force wallet sync
            self._save_state()
        except Exception as e:
            if "does not exist" in str(e):
                log_msg(f"[LIVE]  Market expired before TP placed #{trade.id}")
                if trade.order_id:
                    try:
                        self.clob_client.cancel(trade.order_id)
                    except Exception:
                        pass
                self.open_trade = None
                self._last_balance_fetch = 0.0  # force wallet sync
                self._save_state()
            else:
                log_msg(f"[LIVE]  TP ERROR #{trade.id}: {e}")

    async def _place_sl_and_monitor(self, trade):
        """Place SL sell order on the book immediately, then monitor for TP.
        The SL order sits on the book and fills automatically if bid drops.
        When bid hits $0.99, cancel the SL and sell at TP."""
        sl_price = round(trade.entry_token_price - SL_OFFSET, 2)
        try:
            # Wait briefly for buy to settle, then place SL immediately
            # Don't rely on balance check — CLOB balance API doesn't reflect
            # internal ledger. Just place the SL; if buy didn't fill, SL will fail harmlessly.
            await asyncio.sleep(3)

            # Place GTC limit sell at (entry - $0.10) — this IS the stop loss
            # Use the shares from the buy order (trade.shares)
            log_msg(f"[SL]    Placing SL order #{trade.id} @ ${sl_price:.2f} "
                    f"({trade.shares:.1f} shares)")
            sl_order_id = await self._submit_sell_order(
                trade.token_id, sl_price, trade.shares, quiet=True)

            if sl_order_id:
                log_msg(f"[SL]    SL ON BOOK #{trade.id} @ ${sl_price:.2f} | order={sl_order_id[:12]}...")
            else:
                log_msg(f"[SL]    SL order FAILED #{trade.id} — falling back to monitor")
                asyncio.create_task(self._check_stop_loss(trade))
                return

            # Monitor: check every second for TP or SL fill
            while self.open_trade and self.open_trade.id == trade.id:
                age = time.time() - trade.opened_at
                if age > 300:
                    # Window expired — cancel SL, check what we hold
                    try:
                        self.clob_client.cancel_all()
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                    remaining = await self._get_token_balance(trade.token_id)
                    if remaining < 0.5:
                        # SL filled during the window
                        proceeds = trade.shares * sl_price
                        pnl = round(proceeds - trade.cost_usd, 2)
                        log_msg(f"[SL]    #{trade.id} SL filled during window | P&L=${pnl:+.2f}")
                        self._finalize_exit(trade, sl_price, 0.0, pnl, "STOP_LOSS")
                    else:
                        # Still holding — will be redeemed by cron
                        log_msg(f"[SL]    #{trade.id} expired with {remaining:.1f} shares — "
                                f"clearing for redeem cron")
                        self.open_trade = None
                        self._last_balance_fetch = 0.0
                        self._save_state()
                    return

                # Check book for TP
                book = await self.book_reader.get_prices(trade.token_id)
                if book and book.get("best_bid", 0) >= EXIT_PRICE:
                    # TP hit! Cancel the SL order and sell at TP
                    log_msg(f"[TP]    #{trade.id} bid=${book['best_bid']:.2f} >= ${EXIT_PRICE} — "
                            f"cancelling SL and selling")
                    try:
                        self.clob_client.cancel_all()
                        await asyncio.sleep(2)
                    except Exception:
                        pass

                    # Sell at TP using trade.shares (don't rely on balance API)
                    sell_id = await self._submit_sell_order(
                        trade.token_id, EXIT_PRICE, trade.shares)
                    if sell_id:
                        log_msg(f"[TP]    SELL ORDER #{trade.id} @ ${EXIT_PRICE:.2f} | {trade.shares:.1f} shares")
                        await asyncio.sleep(3)
                    else:
                        # Limit sell failed, try market sell
                        log_msg(f"[TP]    Limit sell failed — trying market sell")
                        try:
                            self.clob_client.cancel_all()
                            await asyncio.sleep(1)
                        except Exception:
                            pass
                        await self._submit_market_sell(trade.token_id, trade.shares)
                        await asyncio.sleep(2)
                    # Either sold or SL had already filled
                    proceeds = trade.shares * 1.00
                    pnl = round(proceeds - trade.cost_usd, 2)
                    self._finalize_exit(trade, 1.00, 0.0, pnl, "TAKE_PROFIT")
                    return

                # Detect SL fill: if bid dropped below our SL price,
                # the pre-placed SL order should have filled
                if book and sl_order_id:
                    bid = book.get("best_bid", 1.0)
                    if bid <= sl_price:
                        # Verify by checking if our SL order is still open
                        try:
                            order_info = self.clob_client.get_order(sl_order_id)
                            order_status = order_info.get("status", "") if order_info else ""
                        except Exception:
                            order_status = "unknown"

                        if order_status in ("MATCHED", "FILLED", "unknown", ""):
                            proceeds = trade.shares * sl_price
                            pnl = round(proceeds - trade.cost_usd, 2)
                            log_msg(f"[SL]    #{trade.id} SL filled (bid=${bid:.2f} <= ${sl_price:.2f}) | "
                                    f"P&L=${pnl:+.2f}")
                            self._finalize_exit(trade, sl_price, 0.0, pnl, "STOP_LOSS")
                            return
                        elif order_status == "LIVE":
                            # SL order still on book but bid dropped past it?
                            # This shouldn't happen — the order should have filled
                            log_msg(f"[SL]    #{trade.id} WARNING: bid=${bid:.2f} < SL ${sl_price:.2f} "
                                    f"but order still LIVE")

                await asyncio.sleep(1)

        except Exception as e:
            log_msg(f"[SL]    #{trade.id} error: {e}")
            # Fall back to old monitor
            asyncio.create_task(self._check_stop_loss(trade))

    async def _check_stop_loss(self, trade):
        """Monitor real book price. Dynamic SL at entry - $0.10.
        Also detects TP when bid >= $0.99."""
        STOP_LOSS_TRIGGER = round(trade.entry_token_price - SL_OFFSET, 2)
        STOP_LOSS_SELL = round(STOP_LOSS_TRIGGER - 0.02, 2)  # sell slightly below trigger
        try:
            await asyncio.sleep(5)  # let the TP order settle first
            lowest_bid = 1.0
            highest_bid = 0.0
            bid_log = []  # (age_seconds, bid, ask, midpoint)

            # Calculate how far into the 5-min window we entered
            window_start = int(trade.opened_at) // 300 * 300
            entry_offset = trade.opened_at - window_start

            while self.open_trade and self.open_trade.id == trade.id:
                age = time.time() - trade.opened_at

                # Check trade age — if > 5 min, market resolved, stop monitoring
                if age > 300:
                    outcome = "EXPIRED"
                    self._write_trajectory(trade, bid_log, outcome, lowest_bid,
                                           highest_bid, entry_offset)
                    if bid_log:
                        bids_only = [b[1] for b in bid_log[-10:]]
                        log_msg(f"[STOP]  #{trade.id} expired — bid history: "
                                f"{', '.join(f'${b:.2f}' for b in bids_only)} "
                                f"| low=${lowest_bid:.2f}")
                    return

                # Read real book price
                book = await self.book_reader.get_prices(trade.token_id)
                if book:
                    bid = book.get("best_bid", 1.0)
                    ask = book.get("best_ask", 0.0)
                    mid = book.get("midpoint", 0.0)
                    bid_log.append((round(age, 1), bid, ask, mid))
                    if bid < lowest_bid:
                        lowest_bid = bid
                    if bid > highest_bid:
                        highest_bid = bid
                    # Log every 30s so we can see the trajectory
                    if len(bid_log) % 30 == 0:
                        log_msg(f"[STOP]  #{trade.id} bid=${bid:.2f} low=${lowest_bid:.2f} "
                                f"high=${highest_bid:.2f} age={age:.0f}s")

                # TP detection: if bid >= $0.99, sell now (market resolved)
                if book and book.get("best_bid", 0.0) >= EXIT_PRICE:
                    log_msg(f"[STOP]  #{trade.id} TP triggered (bid=${book['best_bid']:.2f} >= ${EXIT_PRICE})")
                    # Cancel any open orders and sell
                    if self.clob_client:
                        try:
                            self.clob_client.cancel_all()
                            await asyncio.sleep(2)
                        except Exception:
                            pass
                    actual_balance = await self._get_token_balance(trade.token_id)
                    if actual_balance >= 0.5:
                        sell_id = await self._submit_sell_order(trade.token_id, EXIT_PRICE, actual_balance)
                        if sell_id:
                            log_msg(f"[STOP]  SOLD #{trade.id} @ ${EXIT_PRICE:.2f} | {actual_balance:.1f} shares")
                    proceeds = trade.shares * 1.00  # treat as $1.00 resolution
                    pnl = round(proceeds - trade.cost_usd, 2)
                    self._finalize_exit(trade, 1.00, 0.0, pnl, "TAKE_PROFIT")
                    self._write_trajectory(trade, bid_log, "TAKE_PROFIT", lowest_bid,
                                           highest_bid, entry_offset)
                    return

                if book and book.get("best_bid", 1.0) <= STOP_LOSS_TRIGGER:
                    bid = book["best_bid"]
                    log_msg(f"[STOP]  Book bid ${bid:.2f} <= ${STOP_LOSS_TRIGGER:.2f} "
                            f"for #{trade.id} — triggering stop loss")

                    # Cancel all open orders (including TP)
                    if self.clob_client:
                        try:
                            self.clob_client.cancel_all()
                            log_msg(f"[STOP]  Cancelled all orders for #{trade.id}")
                            await asyncio.sleep(3)
                        except Exception:
                            pass

                    # Check actual balance
                    actual_balance = await self._get_token_balance(trade.token_id)
                    sell_shares = actual_balance if actual_balance > 0 else trade.shares

                    if sell_shares < 0.01:
                        log_msg(f"[STOP]  No shares to sell — position already closed")
                        self.open_trade = None
                        self._last_balance_fetch = 0.0
                        self._save_state()
                        return

                    # MARKET SELL immediately — no limit order, prioritize execution speed
                    log_msg(f"[STOP]  MARKET SELLING {sell_shares:.2f} shares (bid=${bid:.2f})")
                    sell_order_id = await self._submit_market_sell(
                        trade.token_id, sell_shares)
                    if sell_order_id:
                        await asyncio.sleep(3)
                        still_holding = await self._get_token_balance(trade.token_id)
                        if still_holding < sell_shares * 0.1:
                            book_now = await self.book_reader.get_prices(trade.token_id)
                            actual_price = book_now["best_bid"] if book_now else bid
                            proceeds = sell_shares * actual_price
                            pnl = round(proceeds - trade.cost_usd, 2)
                            log_msg(f"[STOP]  MARKET SELL FILLED #{trade.id} | P&L=${pnl:+.2f}")
                            self._finalize_exit(trade, actual_price, 0.0, pnl, "STOP_LOSS_MARKET")
                        else:
                            log_msg(f"[STOP]  MARKET SELL NOT FILLED #{trade.id} — keeping trade open")
                            # DON'T finalize — keep monitoring so we try again
                            continue
                    else:
                        log_msg(f"[STOP]  Market sell order failed #{trade.id} — keeping trade open")
                        # DON'T finalize — keep monitoring
                        continue
                    self._write_trajectory(trade, bid_log, "STOP_LOSS", lowest_bid,
                                           highest_bid, entry_offset)
                    return

                # Check every second for fast reaction
                await asyncio.sleep(1)

            # Trade closed by TP or other means while we were monitoring
            if bid_log:
                self._write_trajectory(trade, bid_log, "TP_OR_CLOSED", lowest_bid,
                                       highest_bid, entry_offset)
        except Exception as e:
            log_msg(f"[STOP]  Monitor error #{trade.id}: {e}")

    async def _check_30s_confirm(self, trade):
        """30-second confirmation: if bid <= $0.64 at 30s, exit immediately.
        Place limit sell at $0.64; if filled great, otherwise market sell."""
        CONFIRM_THRESHOLD = 0.64
        try:
            await asyncio.sleep(30)

            # Check if trade still open
            if not self.open_trade or self.open_trade.id != trade.id:
                return

            # Read book
            book = await self.book_reader.get_prices(trade.token_id)
            if not book:
                return

            bid = book.get("best_bid", 1.0)
            log_msg(f"[30S]   #{trade.id} 30-sec check: bid=${bid:.2f}")

            if bid > CONFIRM_THRESHOLD:
                # Trade is healthy, let TP/SL handle it
                return

            log_msg(f"[30S]   #{trade.id} bid ${bid:.2f} <= ${CONFIRM_THRESHOLD:.2f} — exiting")

            # Cancel TP order to free shares
            if self.clob_client:
                try:
                    self.clob_client.cancel_all()
                    log_msg(f"[30S]   Cancelled all orders for #{trade.id}")
                    await asyncio.sleep(3)
                except Exception:
                    pass

            # Check actual balance
            actual_balance = await self._get_token_balance(trade.token_id)
            sell_shares = actual_balance if actual_balance > 0 else trade.shares

            if sell_shares < 0.01:
                log_msg(f"[30S]   No shares to sell — already closed")
                self.open_trade = None
                self._last_balance_fetch = 0.0
                self._save_state()
                return

            # Try limit sell at $0.64 first
            log_msg(f"[30S]   Placing limit sell: {sell_shares:.2f} shares @ ${CONFIRM_THRESHOLD:.2f}")
            sell_order_id = await self._submit_sell_order(
                trade.token_id, CONFIRM_THRESHOLD, sell_shares)

            if sell_order_id:
                log_msg(f"[30S]   LIMIT SELL #{trade.id} @ ${CONFIRM_THRESHOLD:.2f}"
                        f" | {sell_shares:.1f} shares | order={sell_order_id[:12]}...")
                # Wait briefly to see if it fills, then check
                await asyncio.sleep(5)
                # Check if balance dropped (filled)
                new_balance = await self._get_token_balance(trade.token_id)
                if new_balance < sell_shares * 0.5:
                    # Filled
                    proceeds = sell_shares * CONFIRM_THRESHOLD
                    pnl = round(proceeds - trade.cost_usd, 2)
                    self._finalize_exit(trade, CONFIRM_THRESHOLD, 0.0, pnl, "30S_LIMIT")
                    return
                # Not filled — check if price dropped further, market sell
                book2 = await self.book_reader.get_prices(trade.token_id)
                if book2 and book2.get("best_bid", 1.0) < CONFIRM_THRESHOLD:
                    log_msg(f"[30S]   Price dropped below ${CONFIRM_THRESHOLD:.2f}, market selling")
                    try:
                        self.clob_client.cancel_all()
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                    market_id = await self._submit_market_sell(trade.token_id, sell_shares)
                    if market_id:
                        new_bid = book2["best_bid"]
                        proceeds = sell_shares * new_bid
                        pnl = round(proceeds - trade.cost_usd, 2)
                        self._finalize_exit(trade, new_bid, 0.0, pnl, "30S_MARKET")
            else:
                # Limit failed, fall back to market sell
                log_msg(f"[30S]   Limit failed, market selling")
                market_id = await self._submit_market_sell(trade.token_id, sell_shares)
                if market_id:
                    proceeds = sell_shares * bid
                    pnl = round(proceeds - trade.cost_usd, 2)
                    self._finalize_exit(trade, bid, 0.0, pnl, "30S_MARKET")
        except Exception as e:
            log_msg(f"[30S]   Monitor error #{trade.id}: {e}")

    def _write_trajectory(self, trade, bid_log, outcome, lowest_bid, highest_bid, entry_offset):
        """Write full price trajectory for a trade to the trajectory log."""
        try:
            entry = {
                "trade_id": trade.id,
                "direction": trade.direction,
                "entry_price": trade.entry_token_price,
                "gap": trade.gap,
                "shares": trade.shares,
                "cost_usd": trade.cost_usd,
                "entry_offset_in_window": round(entry_offset, 1),
                "outcome": outcome,
                "lowest_bid": lowest_bid,
                "highest_bid": highest_bid,
                "duration": round(time.time() - trade.opened_at, 1),
                "cb_price_at_entry": trade.entry_btc_coinbase,
                "cl_price_at_entry": trade.entry_btc_chainlink,
                "time": datetime.now(timezone.utc).isoformat(),
                # Sample trajectory: every 5th reading to keep file size manageable
                "trajectory": [
                    {"t": t, "bid": b, "ask": a, "mid": m}
                    for t, b, a, m in bid_log[::5]
                ],
                # Key moments
                "bid_at_10s": next((b for t, b, a, m in bid_log if t >= 10), None),
                "bid_at_30s": next((b for t, b, a, m in bid_log if t >= 30), None),
                "bid_at_60s": next((b for t, b, a, m in bid_log if t >= 60), None),
                "bid_at_120s": next((b for t, b, a, m in bid_log if t >= 120), None),
                "bid_at_180s": next((b for t, b, a, m in bid_log if t >= 180), None),
                "bid_at_240s": next((b for t, b, a, m in bid_log if t >= 240), None),
            }
            self.trajectory_log.write(json.dumps(entry) + "\n")
            self.trajectory_log.flush()
        except Exception as e:
            log_msg(f"[TRAJ] Write error: {e}")

    async def _get_token_balance(self, token_id: str) -> float:
        """Query actual token balance from CLOB API."""
        if not self.clob_client:
            return 0.0
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=SIGNATURE_TYPE,
            )
            resp = self.clob_client.get_balance_allowance(params)
            if resp and "balance" in resp:
                # Balance is in raw units (6 decimals)
                raw = float(resp["balance"])
                return raw / 1_000_000
        except Exception as e:
            log_msg(f"[CLOB] Balance check error: {e}")
            # Fallback: try the positions endpoint
            try:
                wallet = PROXY_WALLET or FUNDER_ADDRESS
                url = f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=0&limit=100"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                           timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            positions = await resp.json()
                            for p in positions:
                                if p.get("asset") == token_id:
                                    return float(p.get("size", 0))
            except Exception:
                pass
        return 0.0

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

    async def _submit_sell_order(self, token_id: str, price: float, size: float, quiet: bool = False) -> Optional[str]:
        """Submit a SELL limit order. quiet=True suppresses error logs (for TP retries)."""
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
                if not quiet:
                    log_msg(f"[CLOB] No sell order ID (attempt {attempt+1}): {resp}")
            except Exception as e:
                error_str = str(e)
                if "does not exist" in error_str:
                    raise
                if not quiet:
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

        # Calculate time remaining in 5-min window
        window_start = int(trade.opened_at) // 300 * 300
        window_end = window_start + 300
        time_remaining = window_end - time.time()

        # STOP LOSS: checked via real book price in _check_stop_loss() async loop
        # (estimated_token from ticks is unreliable for SL in 5-min markets)

        # TAKE PROFIT: the GTC sell at $0.94 handles this automatically
        # But check if it filled by seeing if token price is >= EXIT_PRICE
        if estimated_token >= EXIT_PRICE:
            exit_price = EXIT_PRICE
            reason = "TAKE_PROFIT"
        # TIME EXIT: disabled — redeem cron job handles expired positions
        # elif time_remaining <= 10:
        #     exit_price = snap_price(estimated_token)
        #     reason = "TIME"

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
        """Submit a real SELL order to close the position.
        Always cancels the TP order first to free up shares."""
        if hasattr(self, '_selling_lock') and self._selling_lock:
            return
        self._selling_lock = True

        try:
            # STEP 1: Cancel ALL open orders to free up locked shares
            if self.clob_client:
                try:
                    self.clob_client.cancel_all()
                    log_msg(f"[LIVE]  Cancelled all open orders for #{trade.id}")
                    await asyncio.sleep(3)  # wait for cancellation to settle
                except Exception as e:
                    # Fallback: try cancelling just the TP order
                    tp_id = getattr(trade, 'tp_order_id', None)
                    if tp_id:
                        try:
                            self.clob_client.cancel(tp_id)
                            log_msg(f"[LIVE]  Cancelled TP order for #{trade.id}")
                            await asyncio.sleep(3)
                        except Exception:
                            pass

            # Check actual token balance — may differ from trade.shares due to partial fills
            actual_balance = await self._get_token_balance(trade.token_id)
            if actual_balance > 0:
                if actual_balance < trade.shares:
                    log_msg(f"[LIVE]  Actual balance: {actual_balance:.2f} (expected {trade.shares:.2f})")
                sell_shares = actual_balance
            else:
                sell_shares = trade.shares

            if sell_shares < 0.01:
                log_msg(f"[LIVE]  No shares to sell #{trade.id} — position already resolved or redeemed")
                self.open_trade = None
                self._last_balance_fetch = 0.0  # force wallet sync
                self._save_state()
                return

            if reason == "STOP_LOSS":
                # STOP LOSS: market order — sell at whatever price, get out NOW
                log_msg(f"[LIVE]  STOP LOSS #{trade.id} — market selling {sell_shares:.1f} shares...")
                sell_order_id = await self._submit_market_sell(trade.token_id, sell_shares)

                if not sell_order_id:
                    # Try with fewer shares
                    sell_shares = round(sell_shares - 0.1, 2)
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

            elif reason == "TAKE_PROFIT":
                # TP order already filled on the book — just finalize
                log_msg(f"[LIVE]  TP FILLED #{trade.id} @ ${EXIT_PRICE:.2f}")
                proceeds = sell_shares * EXIT_PRICE
                pnl = round(proceeds - trade.cost_usd, 2)
                self._finalize_exit(trade, EXIT_PRICE, 0.0, pnl, reason)

            else:
                # TIME EXIT: sell at book bid — dump everything before window closes
                book = await self.book_reader.get_prices(trade.token_id)
                if book and book["best_bid"] > 0.05:
                    sell_price = snap_price(book["best_bid"])
                else:
                    sell_price = snap_price(trade.entry_token_price)

                # Try selling all shares
                sell_order_id = await self._submit_sell_order(trade.token_id, sell_price, sell_shares)

                if not sell_order_id:
                    # Balance error — try cancelling all orders again and retry with 1 less share
                    try:
                        self.clob_client.cancel_all()
                        await asyncio.sleep(3)
                    except Exception:
                        pass
                    sell_shares = round(sell_shares - 1.0, 2)
                    if sell_shares >= 1.0:
                        log_msg(f"[LIVE]  Retrying sell with {sell_shares:.2f} shares...")
                        sell_order_id = await self._submit_sell_order(trade.token_id, sell_price, sell_shares)

                if sell_order_id:
                    log_msg(f"[LIVE]  SELL #{trade.id} @ ${sell_price:.2f}"
                            f" | {sell_shares:.1f} shares | order={sell_order_id[:12]}...")
                    proceeds = sell_shares * sell_price
                    pnl = round(proceeds - trade.cost_usd, 2)
                    self._finalize_exit(trade, sell_price, 0.0, pnl, reason)
                else:
                    log_msg(f"[LIVE]  SELL FAILED #{trade.id} — retry in 10s")
                    await asyncio.sleep(10)

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
                await asyncio.sleep(10)
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

        # Bankroll will be synced from wallet on next cycle
        # Force an immediate sync on next check
        self._last_balance_fetch = 0.0

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
        print(f"  {mode} | {elapsed/60:.0f}min | BTC F2 LIVE MARKET (T-{TRIGGER_REMAINING} / SL entry-${SL_OFFSET} / TP ${EXIT_PRICE})")
        print(f"  Bankroll: ${self.bankroll:,.2f} (${total_pnl:+,.2f})"
              f" | Peak: ${self.peak_bankroll:,.2f}")
        print(f"  Bet size: ${current_bet:,.2f} | DD: {self.max_drawdown_pct*100:.1f}%"
              f" (${self.max_drawdown_usd:,.2f})")
        print(f"  Trades: {len(self.closed_trades)} ({wins}W/{losses}L)"
              f" | Win%: {wr:.0f}% | Consec losses: {self.max_consec_losses}")
        print(f"  Signals: {self.signals} | Market: {market_str} | Open: {open_str}")
        print(f"  Feeds: CB={self.cb_ticks:,} CL={self.cl_ticks:,}"
              f" | CB=${self.cb_price:,.2f} CL=${self.cl_price:,.2f}")
        print(f"  Trading hours: 12a-9a / 12p-2p / 4p-12a ET | Time filter: OFF")
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

        print(f"\n  Logs: logs/f2_live_market_trades.jsonl")
        print(f"        logs/f2_live_market_bankroll.csv")
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
                    try:
                        while True:
                            await asyncio.sleep(5)
                            await ws.send("PING")
                    except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
                        pass
                ping_task = asyncio.create_task(pinger())

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
            try:
                ping_task.cancel()
            except (NameError, AttributeError):
                pass
            log_msg(f"[FEED] Chainlink reconnecting: {e}")
            await asyncio.sleep(2)


async def run_f2_watcher(bot: EndgameProductionBot):
    """T-45 window watcher: at T-45 of each window, buy the leading side."""
    while True:
        try:
            now = time.time()
            window_start = (int(now) // 300) * 300
            trigger = window_start + (300 - TRIGGER_REMAINING)
            if now >= trigger:
                next_window = window_start + 300
                trigger = next_window + (300 - TRIGGER_REMAINING)
            wait = trigger - time.time()
            if wait > 0:
                await asyncio.sleep(wait)

            # Time filter (ET): DISABLED for now
            # et_hour = (datetime.now(timezone.utc).hour - 4) % 24
            # if et_hour in (9, 10, 11, 14, 15):
            #     await asyncio.sleep(60)
            #     continue

            # Auto-clear stale trades
            if bot.open_trade and time.time() - bot.open_trade.opened_at > 300:
                log_msg(f"[STALE] Clearing expired trade #{bot.open_trade.id}")
                bot.open_trade = None
                bot._last_balance_fetch = 0.0
                bot._save_state()

            # Don't open if already in a trade
            if bot.open_trade is not None or bot._pending_lock:
                continue

            # 1 trade per window
            current_window = int(time.time()) // 300
            if current_window == bot.last_trade_window:
                continue

            # Refresh market
            bot.market_finder.last_refresh = 0
            await bot.market_finder.refresh()
            if not bot.market_finder.active_market:
                continue

            up_token = bot.market_finder.get_token("UP")
            down_token = bot.market_finder.get_token("DOWN")

            # Read both books, pick the leading side
            up_book = await bot.book_reader.get_prices(up_token) if up_token else None
            down_book = await bot.book_reader.get_prices(down_token) if down_token else None

            if not up_book or not down_book:
                continue

            up_mid = up_book.get("midpoint", 0)
            down_mid = down_book.get("midpoint", 0)
            if up_mid > down_mid and up_mid >= 0.70:
                direction = "UP"
            elif down_mid > up_mid and down_mid >= 0.70:
                direction = "DOWN"
            else:
                continue  # no clear leader or leading side below $0.70

            bot.signals += 1
            bot._pending_lock = True
            bot.last_trade_window = current_window
            log_msg(f"[F2:SIG] T-{TRIGGER_REMAINING} | {direction} leading | "
                    f"UP mid=${up_book.get('midpoint',0):.2f} DOWN mid=${down_book.get('midpoint',0):.2f}")
            asyncio.create_task(bot._safe_open(direction, 0.0))

        except Exception as e:
            log_msg(f"[F2:WATCH] error: {e}")
            await asyncio.sleep(5)


async def run_status(bot: EndgameProductionBot):
    await bot.market_finder.refresh()
    await bot.sync_bankroll()  # initial sync on startup
    status_count = 0
    while True:
        await asyncio.sleep(60)
        await bot.market_finder.refresh()
        await bot.sync_bankroll()

        # Detect stale open trades — if trade is older than 5 minutes,
        # check if we still hold the tokens. If not, the market resolved.
        if bot.open_trade and not bot._selling_lock:
            trade_age = time.time() - bot.open_trade.opened_at
            if trade_age > 300:  # older than 5 minutes = market window expired
                balance = await bot._get_token_balance(bot.open_trade.token_id)
                if balance < 0.01:
                    log_msg(f"[STALE] Trade #{bot.open_trade.id} has no token balance "
                            f"after {trade_age:.0f}s — position resolved, clearing")
                    bot.open_trade = None
                    bot._last_balance_fetch = 0.0
                    bot._save_state()
                else:
                    log_msg(f"[STALE] Trade #{bot.open_trade.id} still holds "
                            f"{balance:.2f} shares after {trade_age:.0f}s")

        bot.print_status()
        status_count += 1

        # Send Telegram status every 15 minutes
        if status_count % 15 == 0 and bot.notifier:
            total_pnl = bot.bankroll - STARTING_BANKROLL
            wins = sum(1 for t in bot.closed_trades if t.pnl and t.pnl > 0)
            losses = len(bot.closed_trades) - wins
            wr = (wins / len(bot.closed_trades) * 100) if bot.closed_trades else 0
            elapsed = (time.time() - bot.start_time) / 60
            current_bet = min(bot.bankroll * KELLY_FRACTION, MAX_BET_CAP)
            gaps = list(bot.gap_history)
            avg_gap = sum(gaps) / len(gaps) if gaps else 0
            market_str = "found" if bot.market_finder.active_market else "searching"
            open_str = (f"#{bot.open_trade.id} {bot.open_trade.direction}"
                        if bot.open_trade else "none")
            et_hour_now = (datetime.now(timezone.utc).hour - 4) % 24
            trading_status = "ACTIVE" if et_hour_now not in (9, 10, 11, 14, 15) else "PAUSED"
            msg = (
                f"📊 <b>{bot.BOT_NAME} Status</b>\n"
                f"<i>{elapsed:.0f}min | entry=$0.81 exit=$0.94</i>\n"
                f"\n"
                f"💰 Bankroll: ${bot.bankroll:,.2f} (${total_pnl:+,.2f})\n"
                f"📈 Peak: ${bot.peak_bankroll:,.2f}\n"
                f"🎯 Bet size: ${current_bet:,.2f}\n"
                f"📉 DD: {bot.max_drawdown_pct*100:.1f}% (${bot.max_drawdown_usd:,.2f})\n"
                f"\n"
                f"📊 Trades: {len(bot.closed_trades)} ({wins}W/{losses}L)\n"
                f"🏆 Win%: {wr:.0f}% | Consec losses: {bot.max_consec_losses}\n"
                f"📡 Signals: {bot.signals} | Avg gap: ${avg_gap:.2f}\n"
                f"🛒 Market: {market_str} | Open: {open_str}\n"
                f"\n"
                f"📡 Feeds: CB={bot.cb_ticks:,} CL={bot.cl_ticks:,}\n"
                f"💵 CB=${bot.cb_price:,.2f} CL=${bot.cl_price:,.2f}\n"
                f"\n"
                f"🕐 Trading: 12a-9a / 12p-2p / 4p-12a ET\n"
                f"⚡ Status: <b>{trading_status}</b>"
            )
            await bot.notifier.send(msg, silent=True)


async def main():
    mode = "PAPER" if DRY_RUN else "*** LIVE ***"
    print("=" * 65)
    print(f"  F2 LIVE MARKET — {mode}")
    print("=" * 65)
    print()
    print(f"  Mode:        {mode}")
    print(f"  Strategy:    F2 — T-{TRIGGER_REMAINING} market buy on leading side")
    print(f"  Asset:       BTC 5-min Up/Down")
    print(f"  Bankroll:    LIVE from wallet (fallback ${STARTING_BANKROLL:,.2f})")
    print(f"  Bet sizing:  {int(BET_PCT*100)}% of bankroll (min 6 shares)")
    print(f"  Max bet:     ${MAX_BET_CAP:,.0f}")
    print(f"  Take Profit: hold to ${EXIT_PRICE} (resolution to $1.00)")
    print(f"  Stop Loss:   entry - ${SL_OFFSET:.2f} (dynamic)")
    print(f"  Allowed ET:  12am-8:59am, 12pm-1:59pm, 4pm-11:59pm (skip 9-11am, 2-3pm)")

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
        asyncio.create_task(run_f2_watcher(bot)),
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
