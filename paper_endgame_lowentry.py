#!/usr/bin/env python3
"""
PAPER ENDGAME — Low Entry Simulator
=====================================
True simulation that reads real Polymarket book prices and simulates trading.
Tests two strategies in parallel on every signal:

Strategy A: Buy limit at $0.60 → TP $0.75 / SL $0.44
Strategy B: Buy limit at $0.60 → Tiered exit:
            50% @ $0.75
            30% @ $0.85
            20% hold to $1.00 (or stop loss)

Entry: Limit buy at $0.60. If best ask is $0.60 or below within 10 seconds, fills.
       Otherwise the order expires unfilled.

Logs to: logs/paper_endgame_lowentry.jsonl
Run alongside the live bot — uses real market data, no real orders placed.
"""

import asyncio
import json
import time
import os
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, List

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

# ── Config ─────────────────────────────────────────────
ENTRY_LIMIT = 0.67        # max limit buy price
ENTRY_MIN = 0.65          # min ask price — wait for momentum (ask must be climbing into the zone)
ENTRY_TIMEOUT = 240       # seconds to wait for ask to climb into entry zone
SHARES = 10               # paper trade size

# Strategy A
A_TP = 0.82
A_SL = 0.40

# Strategy B (tiered)
B_TIER1_PCT = 0.50
B_TIER1_PRICE = 0.82
B_TIER2_PCT = 0.30
B_TIER2_PRICE = 0.92
B_TIER3_PCT = 0.20
B_TIER3_PRICE = 1.00      # capped at $1 (token max)
B_SL = 0.40               # stop loss for unfilled tiers

# Strategy C — late entry on the leading side
C_TRIGGER_REMAINING = 60  # last N seconds of the window
C_ENTRY_MIN = 0.87        # min ask — wait for momentum
C_ENTRY_LIMIT = 0.89      # max ask price
C_TP = 0.99               # take profit
C_SL = 0.74               # stop loss

# Signal detection (matches live bot)
MIN_GAP = 15.0
MAX_GAP = 18.0

LOG_FILE = "logs/paper_endgame_lowentry.jsonl"
SUMMARY_FILE = "logs/paper_endgame_lowentry_summary.json"
os.makedirs("logs", exist_ok=True)


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log_msg(msg):
    print(f"  [{ts()}] {msg}", flush=True)


def write_log(entry):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ── Market finder ──────────────────────────────────────
class MarketFinder:
    def __init__(self):
        self.active_market = None
        self.last_refresh = 0.0

    async def refresh(self):
        if time.time() - self.last_refresh < 30:
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
                            log_msg(f"[MARKET] {m.get('question', '')[:60]}")
        except Exception as e:
            log_msg(f"[MARKET] refresh error: {e}")

    def get_token(self, direction):
        if not self.active_market:
            return None
        return self.active_market["up_token"] if direction == "UP" else self.active_market["down_token"]


# ── Book reader ────────────────────────────────────────
async def get_book(token_id):
    """Fetch current order book for a token."""
    try:
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    # Polymarket returns sorted: best bid is last, best ask is last
                    best_bid = float(bids[-1]["price"]) if bids else 0.0
                    best_ask = float(asks[-1]["price"]) if asks else 0.0
                    midpoint = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0.0
                    return {
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "midpoint": midpoint,
                    }
    except Exception:
        pass
    return None


# ── Trade tracking ─────────────────────────────────────
@dataclass
class PaperTrade:
    id: int
    direction: str
    token_id: str
    entry_price: float
    shares: float
    gap: float
    opened_at: float
    cb_at_entry: float
    cl_at_entry: float
    bid_at_entry: float
    ask_at_entry: float

    # Strategy A
    a_status: str = "open"
    a_exit_price: float = 0.0
    a_pnl: float = 0.0

    # Strategy B
    b_tier1_filled: bool = False
    b_tier2_filled: bool = False
    b_tier3_filled: bool = False
    b_proceeds: float = 0.0
    b_status: str = "open"
    b_pnl: float = 0.0


# ── Paper bot ──────────────────────────────────────────
class PaperBot:
    def __init__(self):
        self.market_finder = MarketFinder()
        self.cb_price = 0.0
        self.cb_time = 0.0
        self.cl_price = 0.0
        self.cl_time = 0.0
        self.cb_ticks = 0
        self.cl_ticks = 0
        self.open_trades: List[PaperTrade] = []
        self.trade_counter = 0
        self.last_trade_window = -1
        self.signals = 0
        self.unfilled_signals = 0  # signals where limit didn't fill
        self.start_time = time.time()
        # Stats per strategy
        self.a_wins = 0
        self.a_losses = 0
        self.a_pnl_total = 0.0
        self.b_wins = 0
        self.b_losses = 0
        self.b_pnl_total = 0.0
        # Strategy C tracking (independent of A/B)
        self.c_signals = 0
        self.c_unfilled = 0
        self.c_trades = 0
        self.c_wins = 0
        self.c_losses = 0
        self.c_pnl_total = 0.0
        self.c_trade_counter = 0

    def on_coinbase(self, price):
        self.cb_price = price
        self.cb_time = time.time()
        self.cb_ticks += 1
        if self.cl_price > 0:
            gap = price - self.cl_price
            self.check_signal(gap)

    def on_chainlink(self, price):
        self.cl_price = price
        self.cl_time = time.time()
        self.cl_ticks += 1

    def check_signal(self, gap):
        if not (MIN_GAP <= abs(gap) <= MAX_GAP):
            return
        # 1 trade per 5-min window
        current_window = int(time.time()) // 300
        if current_window == self.last_trade_window:
            return
        self.signals += 1
        direction = "UP" if gap > 0 else "DOWN"
        self.last_trade_window = current_window
        asyncio.create_task(self._open_gap_trade(direction, gap))

    async def _open_gap_trade(self, direction, gap):
        """Gap signal fired — place a $0.60 limit on the gap side, wait 10s for fill."""
        await self.market_finder.refresh()
        token_id = self.market_finder.get_token(direction)
        if not token_id:
            log_msg(f"[SIG]   {direction} gap=${gap:+.1f} but no market token")
            return

        log_msg(f"[SIG]   {direction} gap=${gap:+.1f} — waiting for ask in "
                f"${ENTRY_MIN:.2f}-${ENTRY_LIMIT:.2f} zone")
        start = time.time()
        bid_at_entry = 0.0
        ask_at_entry = 0.0
        while time.time() - start < ENTRY_TIMEOUT:
            book = await get_book(token_id)
            if book:
                ask_at_entry = book["best_ask"]
                bid_at_entry = book["best_bid"]
                # Require ask to be in the entry zone (price momentum confirmed)
                if ENTRY_MIN <= ask_at_entry <= ENTRY_LIMIT:
                    self.trade_counter += 1
                    trade = PaperTrade(
                        id=self.trade_counter,
                        direction=direction,
                        token_id=token_id,
                        entry_price=ask_at_entry,
                        shares=SHARES,
                        gap=gap,
                        opened_at=time.time(),
                        cb_at_entry=self.cb_price,
                        cl_at_entry=self.cl_price,
                        bid_at_entry=bid_at_entry,
                        ask_at_entry=ask_at_entry,
                    )
                    self.open_trades.append(trade)
                    log_msg(f"[FILL]  #{trade.id} {direction} @ ${ask_at_entry:.2f} "
                            f"({SHARES} shares) | bid=${bid_at_entry:.2f}")
                    asyncio.create_task(self._monitor(trade))
                    return
                # Ask jumped above limit — missed it
                if ask_at_entry > ENTRY_LIMIT:
                    self.unfilled_signals += 1
                    log_msg(f"[NOFILL] {direction} ask=${ask_at_entry:.2f} skipped past "
                            f"${ENTRY_LIMIT:.2f}")
                    return
            await asyncio.sleep(0.5)

        self.unfilled_signals += 1
        log_msg(f"[NOFILL] {direction} gap signal — ask was ${ask_at_entry:.2f}, "
                f"never reached ${ENTRY_MIN:.2f}-${ENTRY_LIMIT:.2f} zone")

    async def watch_window(self):
        """At start of each 5-min window, watch both UP and DOWN tokens.
        Whichever side's ask drops to $0.60 first, buy it."""
        while True:
            try:
                # Wait until next window starts
                now = time.time()
                next_window = (int(now) // 300 + 1) * 300
                wait = next_window - now
                if wait > 0:
                    await asyncio.sleep(wait)

                # Force refresh for the new window (bypass cache)
                self.market_finder.last_refresh = 0
                await asyncio.sleep(2)  # let Polymarket index the new window
                await self.market_finder.refresh()
                if not self.market_finder.active_market:
                    log_msg("[WIN]   No market found for new window")
                    continue

                # Verify we got the current window
                current_window_start = (int(time.time()) // 300) * 300
                market_window = self.market_finder.active_market["window_start"]
                if market_window != current_window_start:
                    log_msg(f"[WIN]   Stale market (got {market_window}, want {current_window_start})")
                    # Force another refresh
                    self.market_finder.last_refresh = 0
                    await asyncio.sleep(3)
                    await self.market_finder.refresh()
                    market_window = self.market_finder.active_market.get("window_start", 0)
                    if market_window != current_window_start:
                        log_msg(f"[WIN]   Still stale, skipping window")
                        continue

                up_token = self.market_finder.get_token("UP")
                down_token = self.market_finder.get_token("DOWN")
                log_msg(f"[WIN]   New window started — watching for ${ENTRY_LIMIT:.2f} entry")

                # Watch both tokens for up to 4 minutes (leave 1 min for resolution)
                deadline = current_window_start + 240
                filled = False
                while time.time() < deadline and not filled:
                    # Check both books in parallel
                    up_book, down_book = await asyncio.gather(
                        get_book(up_token), get_book(down_token)
                    )

                    # Check UP first — must be in entry zone (price momentum)
                    if up_book and ENTRY_MIN <= up_book["best_ask"] <= ENTRY_LIMIT:
                        await self._fill_trade("UP", up_token, up_book)
                        filled = True
                        break

                    # Then DOWN — must be in entry zone
                    if down_book and ENTRY_MIN <= down_book["best_ask"] <= ENTRY_LIMIT:
                        await self._fill_trade("DOWN", down_token, down_book)
                        filled = True
                        break

                    await asyncio.sleep(0.5)

                if not filled:
                    self.unfilled_signals += 1
                    log_msg(f"[NOFILL] Neither side hit ${ENTRY_LIMIT:.2f} in window")
            except Exception as e:
                log_msg(f"[WIN]   error: {e}")
                await asyncio.sleep(5)

    async def watch_late_entry(self):
        """Strategy C: at last 60s of each window, watch for ask hitting $0.89."""
        while True:
            try:
                # Wait until we're at T-60 in the current window
                now = time.time()
                window_start = (int(now) // 300) * 300
                trigger_time = window_start + (300 - C_TRIGGER_REMAINING)

                if now >= trigger_time:
                    # Already past trigger for this window — wait for next
                    next_window = window_start + 300
                    trigger_time = next_window + (300 - C_TRIGGER_REMAINING)

                wait = trigger_time - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)

                # Now in the last 60 seconds of a window
                self.market_finder.last_refresh = 0
                await self.market_finder.refresh()
                if not self.market_finder.active_market:
                    log_msg("[C]     No market for late entry")
                    continue

                up_token = self.market_finder.get_token("UP")
                down_token = self.market_finder.get_token("DOWN")
                current_window_start = (int(time.time()) // 300) * 300
                deadline = current_window_start + 295  # leave 5s buffer

                self.c_signals += 1
                log_msg(f"[C]     Late entry watch — looking for ask at ${C_ENTRY_LIMIT:.2f}")

                filled = False
                while time.time() < deadline and not filled:
                    up_book, down_book = await asyncio.gather(
                        get_book(up_token), get_book(down_token)
                    )

                    if up_book and C_ENTRY_MIN <= up_book["best_ask"] <= C_ENTRY_LIMIT:
                        await self._fill_c_trade("UP", up_token, up_book)
                        filled = True
                        break
                    if down_book and C_ENTRY_MIN <= down_book["best_ask"] <= C_ENTRY_LIMIT:
                        await self._fill_c_trade("DOWN", down_token, down_book)
                        filled = True
                        break

                    await asyncio.sleep(0.5)

                if not filled:
                    self.c_unfilled += 1
                    log_msg(f"[C]     NOFILL — neither side hit ${C_ENTRY_LIMIT:.2f}")
            except Exception as e:
                log_msg(f"[C]     error: {e}")
                await asyncio.sleep(5)

    async def _fill_c_trade(self, direction, token_id, book):
        """Open a Strategy C paper trade."""
        self.c_trade_counter += 1
        self.c_trades += 1
        bid = book["best_bid"]
        ask = book["best_ask"]
        opened_at = time.time()
        cost = SHARES * ask
        log_msg(f"[C:OPEN] #{self.c_trade_counter} {direction} @ ${ask:.2f} "
                f"({SHARES} shares) | bid=${bid:.2f}")
        asyncio.create_task(self._monitor_c(direction, token_id, ask, opened_at,
                                            self.c_trade_counter, cost))

    async def _monitor_c(self, direction, token_id, entry_price, opened_at, tid, cost):
        """Monitor a Strategy C trade for TP or SL."""
        try:
            await asyncio.sleep(1)
            while time.time() - opened_at < 295:
                book = await get_book(token_id)
                if not book:
                    await asyncio.sleep(1)
                    continue
                bid = book["best_bid"]
                if bid >= C_TP:
                    pnl = round((C_TP - entry_price) * SHARES, 4)
                    self.c_wins += 1
                    self.c_pnl_total += pnl
                    log_msg(f"[C:WIN]  #{tid} sold @ ${C_TP:.2f} | P&L=${pnl:+.2f}")
                    self._write_c_log(tid, direction, entry_price, "won", C_TP, pnl)
                    return
                if bid <= C_SL:
                    pnl = round((C_SL - entry_price) * SHARES, 4)
                    self.c_losses += 1
                    self.c_pnl_total += pnl
                    log_msg(f"[C:SL]   #{tid} sold @ ${C_SL:.2f} | P&L=${pnl:+.2f}")
                    self._write_c_log(tid, direction, entry_price, "stopped", C_SL, pnl)
                    return
                await asyncio.sleep(1)

            # Window expired — assume $0 wipeout
            pnl = round(-entry_price * SHARES, 4)
            self.c_losses += 1
            self.c_pnl_total += pnl
            log_msg(f"[C:EXP]  #{tid} expired @ $0 | P&L=${pnl:+.2f}")
            self._write_c_log(tid, direction, entry_price, "expired", 0.0, pnl)
        except Exception as e:
            log_msg(f"[C:MON]  error #{tid}: {e}")

    def _write_c_log(self, tid, direction, entry_price, status, exit_price, pnl):
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps({
                    "strategy": "C",
                    "trade_id": tid,
                    "direction": direction,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "status": status,
                    "pnl": pnl,
                    "shares": SHARES,
                    "time": datetime.now(timezone.utc).isoformat(),
                }) + "\n")
        except Exception:
            pass
        self._write_summary()

    async def _fill_trade(self, direction, token_id, book):
        """Open a paper trade with the limit fill."""
        self.signals += 1
        self.trade_counter += 1
        bid_at_entry = book["best_bid"]
        ask_at_entry = book["best_ask"]
        trade = PaperTrade(
            id=self.trade_counter,
            direction=direction,
            token_id=token_id,
            entry_price=ask_at_entry,
            shares=SHARES,
            gap=0.0,  # not gap-based anymore
            opened_at=time.time(),
            cb_at_entry=self.cb_price,
            cl_at_entry=self.cl_price,
            bid_at_entry=bid_at_entry,
            ask_at_entry=ask_at_entry,
        )
        self.open_trades.append(trade)
        log_msg(f"[FILL]  #{trade.id} {direction} @ ${ENTRY_LIMIT:.2f} "
                f"({SHARES} shares) | book bid=${bid_at_entry:.2f} ask=${ask_at_entry:.2f}")
        asyncio.create_task(self._monitor(trade))

    async def _monitor(self, trade):
        """Watch the bid price every 1s. Trigger exits for both strategies."""
        cost = trade.shares * trade.entry_price
        try:
            await asyncio.sleep(1)
            while time.time() - trade.opened_at < 295:
                book = await get_book(trade.token_id)
                if not book:
                    await asyncio.sleep(1)
                    continue
                bid = book["best_bid"]

                # Strategy A: simple TP/SL
                if trade.a_status == "open":
                    if bid >= A_TP:
                        trade.a_status = "won"
                        trade.a_exit_price = A_TP
                        trade.a_pnl = round((A_TP - trade.entry_price) * trade.shares, 4)
                        self.a_wins += 1
                        self.a_pnl_total += trade.a_pnl
                        log_msg(f"[A:WIN] #{trade.id} sold @ ${A_TP:.2f} | P&L=${trade.a_pnl:+.2f}")
                    elif bid <= A_SL:
                        trade.a_status = "stopped"
                        trade.a_exit_price = A_SL
                        trade.a_pnl = round((A_SL - trade.entry_price) * trade.shares, 4)
                        self.a_losses += 1
                        self.a_pnl_total += trade.a_pnl
                        log_msg(f"[A:SL]  #{trade.id} sold @ ${A_SL:.2f} | P&L=${trade.a_pnl:+.2f}")

                # Strategy B: tiered exits
                if trade.b_status == "open":
                    # Check stop loss FIRST — only triggers if no tiers filled yet
                    if bid <= B_SL and not (trade.b_tier1_filled or trade.b_tier2_filled or trade.b_tier3_filled):
                        sold_shares = trade.shares
                        trade.b_proceeds += sold_shares * B_SL
                        trade.b_status = "stopped"
                        trade.b_pnl = round(trade.b_proceeds - cost, 4)
                        if trade.b_pnl > 0:
                            self.b_wins += 1
                        else:
                            self.b_losses += 1
                        self.b_pnl_total += trade.b_pnl
                        log_msg(f"[B:SL]  #{trade.id} stopped @ ${B_SL:.2f} | P&L=${trade.b_pnl:+.2f}")
                    else:
                        # Check tier fills
                        if not trade.b_tier1_filled and bid >= B_TIER1_PRICE:
                            sold = trade.shares * B_TIER1_PCT
                            trade.b_proceeds += sold * B_TIER1_PRICE
                            trade.b_tier1_filled = True
                            log_msg(f"[B:T1]  #{trade.id} sold {sold:.1f} @ ${B_TIER1_PRICE:.2f}")
                        if not trade.b_tier2_filled and bid >= B_TIER2_PRICE:
                            sold = trade.shares * B_TIER2_PCT
                            trade.b_proceeds += sold * B_TIER2_PRICE
                            trade.b_tier2_filled = True
                            log_msg(f"[B:T2]  #{trade.id} sold {sold:.1f} @ ${B_TIER2_PRICE:.2f}")
                        if not trade.b_tier3_filled and bid >= B_TIER3_PRICE:
                            sold = trade.shares * B_TIER3_PCT
                            trade.b_proceeds += sold * B_TIER3_PRICE
                            trade.b_tier3_filled = True
                            trade.b_status = "won"
                            trade.b_pnl = round(trade.b_proceeds - cost, 4)
                            self.b_wins += 1
                            self.b_pnl_total += trade.b_pnl
                            log_msg(f"[B:T3]  #{trade.id} sold {sold:.1f} @ ${B_TIER3_PRICE:.2f} "
                                    f"| total P&L=${trade.b_pnl:+.2f}")

                # Both done?
                a_done = trade.a_status != "open"
                b_done = trade.b_status != "open"
                if a_done and b_done:
                    self._finalize(trade)
                    return

                await asyncio.sleep(1)

            # Window expired
            self._finalize_expired(trade, cost)
        except Exception as e:
            log_msg(f"[MON]   error #{trade.id}: {e}")

    def _finalize_expired(self, trade, cost):
        """Trade window expired — calculate final state for any unclosed strategy."""
        if trade.a_status == "open":
            # Token resolves to $0 if losing side, $1 if winning
            # Use the last bid we saw to estimate — but conservatively assume $0 wipeout
            trade.a_status = "expired"
            trade.a_exit_price = 0.0
            trade.a_pnl = round(-trade.entry_price * trade.shares, 4)
            self.a_losses += 1
            self.a_pnl_total += trade.a_pnl
            log_msg(f"[A:EXP] #{trade.id} expired @ $0 | P&L=${trade.a_pnl:+.2f}")

        if trade.b_status == "open":
            # Any unfilled tiers expire at $0
            trade.b_status = "expired"
            trade.b_pnl = round(trade.b_proceeds - cost, 4)
            if trade.b_pnl > 0:
                self.b_wins += 1
            else:
                self.b_losses += 1
            self.b_pnl_total += trade.b_pnl
            log_msg(f"[B:EXP] #{trade.id} expired | proceeds=${trade.b_proceeds:.2f} | P&L=${trade.b_pnl:+.2f}")

        self._finalize(trade)

    def _finalize(self, trade):
        try:
            self.open_trades.remove(trade)
        except ValueError:
            pass
        write_log({
            "trade_id": trade.id,
            "direction": trade.direction,
            "entry_price": trade.entry_price,
            "gap": trade.gap,
            "shares": trade.shares,
            "duration": round(time.time() - trade.opened_at, 1),
            "bid_at_entry": trade.bid_at_entry,
            "ask_at_entry": trade.ask_at_entry,
            "a_status": trade.a_status,
            "a_exit_price": trade.a_exit_price,
            "a_pnl": trade.a_pnl,
            "b_status": trade.b_status,
            "b_proceeds": round(trade.b_proceeds, 4),
            "b_pnl": trade.b_pnl,
            "b_tier1": trade.b_tier1_filled,
            "b_tier2": trade.b_tier2_filled,
            "b_tier3": trade.b_tier3_filled,
            "time": datetime.now(timezone.utc).isoformat(),
        })
        self._write_summary()

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        a_total = self.a_wins + self.a_losses
        b_total = self.b_wins + self.b_losses
        summary = {
            "elapsed_minutes": round(elapsed, 1),
            "signals_total": self.signals,
            "signals_unfilled": self.unfilled_signals,
            "trades_filled": self.trade_counter,
            "fill_rate": round((self.trade_counter / self.signals * 100) if self.signals else 0, 1),
            "strategy_a": {
                "wins": self.a_wins,
                "losses": self.a_losses,
                "win_rate": round((self.a_wins / a_total * 100) if a_total else 0, 1),
                "pnl_total": round(self.a_pnl_total, 2),
            },
            "strategy_b": {
                "wins": self.b_wins,
                "losses": self.b_losses,
                "win_rate": round((self.b_wins / b_total * 100) if b_total else 0, 1),
                "pnl_total": round(self.b_pnl_total, 2),
            },
            "strategy_c": {
                "signals": self.c_signals,
                "unfilled": self.c_unfilled,
                "trades": self.c_trades,
                "wins": self.c_wins,
                "losses": self.c_losses,
                "win_rate": round((self.c_wins / (self.c_wins + self.c_losses) * 100)
                                  if (self.c_wins + self.c_losses) else 0, 1),
                "pnl_total": round(self.c_pnl_total, 2),
            },
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        with open(SUMMARY_FILE, "w") as f:
            json.dump(summary, f, indent=2)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        a_total = self.a_wins + self.a_losses
        b_total = self.b_wins + self.b_losses
        a_wr = (self.a_wins / a_total * 100) if a_total else 0
        b_wr = (self.b_wins / b_total * 100) if b_total else 0
        fill_rate = (self.trade_counter / self.signals * 100) if self.signals else 0
        print()
        print(f"  PAPER ENDGAME LOW ENTRY | {elapsed:.0f}min")
        print(f"  Signals: {self.signals} | Filled: {self.trade_counter} ({fill_rate:.0f}%)"
              f" | Unfilled: {self.unfilled_signals}")
        print(f"  Strategy A (TP $0.75 / SL $0.44): {self.a_wins}W/{self.a_losses}L"
              f" ({a_wr:.0f}%) | P&L=${self.a_pnl_total:+.2f}")
        print(f"  Strategy B (50/30/20 tiered):    {self.b_wins}W/{self.b_losses}L"
              f" ({b_wr:.0f}%) | P&L=${self.b_pnl_total:+.2f}")
        c_total = self.c_wins + self.c_losses
        c_wr = (self.c_wins / c_total * 100) if c_total else 0
        print(f"  Strategy C (late $0.89/$0.99/$0.74): {self.c_wins}W/{self.c_losses}L"
              f" ({c_wr:.0f}%) | P&L=${self.c_pnl_total:+.2f}"
              f" | signals={self.c_signals} unfilled={self.c_unfilled}")
        print(f"  Open: {len(self.open_trades)} | Feeds: CB=${self.cb_price:.2f} CL=${self.cl_price:.2f}")
        print()


# ── Feeds ──────────────────────────────────────────────
async def run_coinbase(bot):
    while True:
        try:
            async with websockets.connect("wss://ws-feed.exchange.coinbase.com") as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}]
                }))
                log_msg("[FEED] Coinbase BTC connected")
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("type") == "ticker":
                        bot.on_coinbase(float(data["price"]))
        except Exception as e:
            log_msg(f"[FEED] CB reconnecting: {e}")
            await asyncio.sleep(2)


async def run_chainlink(bot):
    while True:
        try:
            async with websockets.connect("wss://ws-live-data.polymarket.com",
                                          ping_interval=None) as ws:
                await ws.send(json.dumps({
                    "action": "subscribe",
                    "subscriptions": [{"topic": "crypto_prices_chainlink",
                                       "type": "*", "filters": ""}]
                }))
                log_msg("[FEED] Chainlink BTC connected")

                async def pinger():
                    try:
                        while True:
                            await asyncio.sleep(5)
                            await ws.send("PING")
                    except Exception:
                        pass
                ping_task = asyncio.create_task(pinger())

                try:
                    async for msg in ws:
                        if msg == "PONG" or not msg.startswith("{"):
                            continue
                        data = json.loads(msg)
                        if data.get("topic") == "crypto_prices_chainlink":
                            p = data.get("payload", {})
                            if "btc" in p.get("symbol", "").lower():
                                bot.on_chainlink(float(p["value"]))
                finally:
                    ping_task.cancel()
        except Exception as e:
            log_msg(f"[FEED] CL reconnecting: {e}")
            await asyncio.sleep(2)


async def run_status(bot):
    await bot.market_finder.refresh()
    while True:
        await asyncio.sleep(60)
        await bot.market_finder.refresh()
        bot.print_status()


async def main():
    print("=" * 65)
    print("  PAPER ENDGAME — LOW ENTRY SIMULATOR")
    print("=" * 65)
    print(f"  Entry:    At each window open, watch UP & DOWN tokens.")
    print(f"            Buy whichever side's ask hits ${ENTRY_LIMIT:.2f} first.")
    print(f"            (limit order, fills as the side moves up)")
    print(f"  Shares:   {SHARES} per trade")
    print()
    print(f"  Strategy A: TP ${A_TP} / SL ${A_SL}")
    print(f"  Strategy B: Tiered exit:")
    print(f"              {int(B_TIER1_PCT*100)}% @ ${B_TIER1_PRICE}")
    print(f"              {int(B_TIER2_PCT*100)}% @ ${B_TIER2_PRICE}")
    print(f"              {int(B_TIER3_PCT*100)}% @ ${B_TIER3_PRICE}")
    print(f"              SL ${B_SL} (only if no tiers filled)")
    print(f"  Strategy C: Late entry (last {C_TRIGGER_REMAINING}s of window)")
    print(f"              Limit buy @ ${C_ENTRY_LIMIT}, TP ${C_TP}, SL ${C_SL}")
    print()
    print(f"  Logs: {LOG_FILE}")
    print(f"  Summary: {SUMMARY_FILE}")
    print()

    # Wait for the next 5-min window boundary so we start in sync with markets
    now = time.time()
    next_window = (int(now) // 300 + 1) * 300
    wait = next_window - now
    log_msg(f"[SYNC]  Waiting {wait:.0f}s for next window boundary "
            f"({datetime.fromtimestamp(next_window, timezone.utc).strftime('%H:%M:%S')} UTC)...")
    await asyncio.sleep(wait)
    log_msg(f"[SYNC]  Aligned with window boundary — starting bot")

    bot = PaperBot()
    await asyncio.gather(
        run_coinbase(bot),
        run_chainlink(bot),
        run_status(bot),
        bot.watch_window(),
        bot.watch_late_entry(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
