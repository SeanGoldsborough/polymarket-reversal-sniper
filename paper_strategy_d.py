#!/usr/bin/env python3
"""
PAPER STRATEGY D — Combined A+C
================================
Phase 1 (first 4 min of window): Strategy A
  - Gap signal $15-$18
  - Entry zone $0.68-$0.70
  - TP = entry + $0.15
  - SL = entry - $0.15

Phase 2 (last 60s of window): Strategy C
  - Watch both UP and DOWN tokens
  - Buy whichever side's ask is in $0.87-$0.89 first
  - TP at $0.99 / SL at $0.74

Bet sizing: 10% of bankroll per trade
Stops after: 300 trades
Output: CSV in Polymarket-History format
"""

import asyncio
import json
import time
import os
import sys
import csv
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
STARTING_BANKROLL = 100.0
BET_PCT = 0.10            # 10% of bankroll per trade
MAX_TRADES = 999999  # no limit — run continuously

# Price movement tracking
MAX_SPREAD = 0.05         # skip trades if (ask - bid) > $0.05
VELOCITY_WINDOW = 10      # seconds for velocity calc
VEL_STRONG = 0.020        # +$0.02/sec or better = strong momentum
VEL_MILD = 0.005          # +$0.005/sec to $0.02/sec = mild
# below mild = flat / fading
BET_PCT_STRONG = 0.15
BET_PCT_MILD = 0.10
BET_PCT_FLAT = 0.05

# Phase 1 (Strategy A)
A_PHASE_END = 240         # first 4 minutes
A_ENTRY_MIN = 0.68
A_ENTRY_MAX = 0.70
A_PROFIT_DELTA = 0.15
A_LOSS_DELTA = 0.15
A_GAP_MIN = 15.0
A_GAP_MAX = 18.0
A_TIMEOUT = 240           # wait up to 4 min for fill after gap

# Phase 2 (Strategy C)
C_PHASE_START = 240       # last 60 seconds
C_ENTRY_MIN = 0.87
C_ENTRY_MAX = 0.89
C_TP = 0.99
C_SL = 0.74

LOG_FILE = "logs/paper_strategy_d.jsonl"
CSV_FILE = "logs/paper_strategy_d_history.csv"
SUMMARY_FILE = "logs/paper_strategy_d_summary.json"
os.makedirs("logs", exist_ok=True)


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log_msg(msg):
    print(f"  [{ts()}] {msg}", flush=True)


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
        except Exception as e:
            log_msg(f"[MARKET] error: {e}")

    def get_token(self, direction):
        if not self.active_market:
            return None
        return self.active_market["up_token"] if direction == "UP" else self.active_market["down_token"]

    def market_name(self):
        if not self.active_market:
            return ""
        return self.active_market.get("question", "")


# ── Book reader ────────────────────────────────────────
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
                    midpoint = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0.0
                    return {"best_bid": best_bid, "best_ask": best_ask, "midpoint": midpoint}
    except Exception:
        pass
    return None


# ── Bot ────────────────────────────────────────────────
class PaperBotD:
    def __init__(self):
        self.market_finder = MarketFinder()
        self.cb_price = 0.0
        self.cl_price = 0.0
        self.cb_ticks = 0
        self.cl_ticks = 0
        self.bankroll = STARTING_BANKROLL
        self.peak_bankroll = STARTING_BANKROLL
        self.max_drawdown_pct = 0.0
        self.max_drawdown_usd = 0.0
        self.trade_counter = 0
        self.signals = 0
        self.unfilled = 0
        self.wins = 0
        self.losses = 0
        self.consec_losses = 0
        self.max_consec_losses = 0
        # Per-phase stats
        self.a_wins = 0
        self.a_losses = 0
        self.a_pnl = 0.0
        self.c_wins = 0
        self.c_losses = 0
        self.c_pnl = 0.0
        # Gap tracking
        self.gap_sum = 0.0
        self.gap_count = 0
        self.last_a_window = -1
        self.last_c_window = -1
        self.start_time = time.time()
        # Init CSV with header
        if not os.path.exists(CSV_FILE):
            with open(CSV_FILE, "w") as f:
                f.write('"marketName","action","usdcAmount","tokenAmount","tokenName","timestamp","hash"\n')

    def on_coinbase(self, price):
        self.cb_price = price
        self.cb_ticks += 1
        if self.cl_price > 0:
            gap = price - self.cl_price
            self.gap_sum += abs(gap)
            self.gap_count += 1
            self.check_a_signal(gap)

    def on_chainlink(self, price):
        self.cl_price = price
        self.cl_ticks += 1

    def _update_peak_dd(self):
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll
        dd_usd = self.peak_bankroll - self.bankroll
        dd_pct = (dd_usd / self.peak_bankroll * 100) if self.peak_bankroll > 0 else 0
        if dd_pct > self.max_drawdown_pct:
            self.max_drawdown_pct = dd_pct
            self.max_drawdown_usd = dd_usd

    def calc_shares(self, entry_price, velocity=None):
        # Fixed sizing — passive velocity measurement only
        bet_usd = self.bankroll * BET_PCT
        return round(bet_usd / entry_price, 2), BET_PCT

    async def _measure_velocity_async(self, tid, phase, token_id):
        """Measure velocity over 10s after fill, log it, but don't affect trade."""
        try:
            readings = []
            for _ in range(VELOCITY_WINDOW):
                book = await get_book(token_id)
                if book:
                    readings.append((time.time(), book["best_bid"]))
                await asyncio.sleep(1)
            if len(readings) >= 3:
                first_t, first_bid = readings[0]
                last_t, last_bid = readings[-1]
                elapsed = last_t - first_t
                if elapsed > 0:
                    velocity = (last_bid - first_bid) / elapsed
                    if velocity >= VEL_STRONG:
                        tag = f"STRONG (+{velocity*1000:.1f}m$/s)"
                    elif velocity >= VEL_MILD:
                        tag = f"mild (+{velocity*1000:.1f}m$/s)"
                    elif velocity > -VEL_MILD:
                        tag = f"flat ({velocity*1000:+.1f}m$/s)"
                    else:
                        tag = f"FADING ({velocity*1000:+.1f}m$/s)"
                    log_msg(f"[{phase}:VEL] #{tid} 10s velocity: {tag}")
        except Exception as e:
            log_msg(f"[{phase}:VEL] #{tid} measurement error: {e}")

    async def measure_velocity(self, token_id, samples=10):
        """Measure bid velocity ($/sec) over the last `samples` seconds.
        Returns (velocity, latest_book) or (None, None) on error."""
        readings = []
        for _ in range(samples):
            book = await get_book(token_id)
            if book:
                readings.append((time.time(), book["best_bid"], book))
            await asyncio.sleep(1)
        if len(readings) < 3:
            return None, None
        first_t, first_bid, _ = readings[0]
        last_t, last_bid, last_book = readings[-1]
        elapsed = last_t - first_t
        if elapsed <= 0:
            return None, last_book
        velocity = (last_bid - first_bid) / elapsed
        return velocity, last_book

    def check_a_signal(self, gap):
        if self.trade_counter >= MAX_TRADES:
            return
        if not (A_GAP_MIN <= abs(gap) <= A_GAP_MAX):
            return
        # Only in first 4 min of window
        now = time.time()
        window_start = (int(now) // 300) * 300
        elapsed = now - window_start
        if elapsed > A_PHASE_END:
            return
        # 1 trade per window per strategy
        current_window = int(now) // 300
        if current_window == self.last_a_window:
            return
        self.last_a_window = current_window
        self.signals += 1
        direction = "UP" if gap > 0 else "DOWN"
        asyncio.create_task(self._open_a(direction, gap))

    async def _open_a(self, direction, gap):
        await self.market_finder.refresh()
        token_id = self.market_finder.get_token(direction)
        if not token_id:
            return

        log_msg(f"[A:SIG] {direction} gap=${gap:+.1f} — waiting for ask in "
                f"${A_ENTRY_MIN:.2f}-${A_ENTRY_MAX:.2f}")

        start = time.time()
        while time.time() - start < A_TIMEOUT and self.trade_counter < MAX_TRADES:
            window_start = (int(time.time()) // 300) * 300
            if time.time() - window_start > A_PHASE_END:
                break

            book = await get_book(token_id)
            if book:
                ask = book["best_ask"]
                bid = book["best_bid"]
                if A_ENTRY_MIN <= ask <= A_ENTRY_MAX:
                    # Passive measurement: log spread, no filter
                    spread = ask - bid
                    await self._fill_trade("A", direction, token_id, ask, bid, gap, 0.0, spread)
                    return
                if ask > A_ENTRY_MAX:
                    self.unfilled += 1
                    log_msg(f"[A:SKIP] {direction} ask=${ask:.2f} skipped past entry zone")
                    return
            await asyncio.sleep(0.5)

        self.unfilled += 1
        log_msg(f"[A:NOFILL] {direction} timeout / phase 2 reached")

    async def watch_phase_c(self):
        """Strategy C: at T-60s of each window, watch for ask in $0.87-$0.89."""
        while True:
            try:
                if self.trade_counter >= MAX_TRADES:
                    await asyncio.sleep(10)
                    continue

                now = time.time()
                window_start = (int(now) // 300) * 300
                trigger = window_start + C_PHASE_START

                if now >= trigger:
                    next_window = window_start + 300
                    trigger = next_window + C_PHASE_START

                wait = trigger - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)

                if self.trade_counter >= MAX_TRADES:
                    continue

                # Check we haven't already taken a C trade in this window
                current_window = int(time.time()) // 300
                if current_window == self.last_c_window:
                    continue

                self.market_finder.last_refresh = 0
                await self.market_finder.refresh()
                if not self.market_finder.active_market:
                    continue

                up_token = self.market_finder.get_token("UP")
                down_token = self.market_finder.get_token("DOWN")
                deadline = (int(time.time()) // 300) * 300 + 295

                self.last_c_window = current_window
                log_msg(f"[C:WATCH] Looking for ask in ${C_ENTRY_MIN:.2f}-${C_ENTRY_MAX:.2f}")

                filled = False
                while time.time() < deadline and not filled:
                    if self.trade_counter >= MAX_TRADES:
                        break
                    up_book, down_book = await asyncio.gather(
                        get_book(up_token), get_book(down_token)
                    )
                    if up_book and C_ENTRY_MIN <= up_book["best_ask"] <= C_ENTRY_MAX:
                        spread = up_book["best_ask"] - up_book["best_bid"]
                        await self._fill_trade("C", "UP", up_token,
                                               up_book["best_ask"], up_book["best_bid"], 0, 0.0, spread)
                        filled = True
                        break
                    if down_book and C_ENTRY_MIN <= down_book["best_ask"] <= C_ENTRY_MAX:
                        spread = down_book["best_ask"] - down_book["best_bid"]
                        await self._fill_trade("C", "DOWN", down_token,
                                               down_book["best_ask"], down_book["best_bid"], 0, 0.0, spread)
                        filled = True
                        break
                    await asyncio.sleep(0.5)

                if not filled:
                    self.unfilled += 1
                    log_msg(f"[C:NOFILL] Neither side hit zone")
            except Exception as e:
                log_msg(f"[C] error: {e}")
                await asyncio.sleep(5)

    async def _fill_trade(self, phase, direction, token_id, entry_price, bid, gap,
                          velocity=0.0, spread=0.0):
        """Open a paper trade. phase = 'A' or 'C'."""
        if self.trade_counter >= MAX_TRADES:
            return

        self.trade_counter += 1
        tid = self.trade_counter
        shares, sizing_pct = self.calc_shares(entry_price, velocity)
        cost = round(shares * entry_price, 4)
        opened_at = time.time()
        market_name = self.market_finder.market_name()

        # Schedule async passive velocity measurement (doesn't block trade)
        asyncio.create_task(self._measure_velocity_async(tid, phase, token_id))

        if phase == "A":
            tp = round(entry_price + A_PROFIT_DELTA, 2)
            sl = round(entry_price - A_LOSS_DELTA, 2)
        else:
            tp = C_TP
            sl = C_SL

        log_msg(f"[{phase}:OPEN] #{tid} {direction} @ ${entry_price:.2f} "
                f"({shares} sh, ${cost:.2f}) | TP=${tp:.2f} SL=${sl:.2f} | "
                f"spread=${spread:.2f} | bank=${self.bankroll:.2f}")

        # Write Buy to CSV
        self._write_csv(market_name, "Buy", cost, shares, direction.title(), opened_at)

        asyncio.create_task(self._monitor_trade(tid, phase, direction, token_id,
                                                entry_price, shares, cost, tp, sl,
                                                opened_at, market_name))

    async def _monitor_trade(self, tid, phase, direction, token_id,
                             entry_price, shares, cost, tp, sl, opened_at, market_name):
        try:
            await asyncio.sleep(1)
            while time.time() - opened_at < 295:
                book = await get_book(token_id)
                if not book:
                    await asyncio.sleep(1)
                    continue
                bid = book["best_bid"]

                if bid >= tp:
                    proceeds = round(shares * tp, 4)
                    pnl = round(proceeds - cost, 4)
                    self.bankroll = round(self.bankroll + pnl, 4)
                    self.wins += 1
                    self.consec_losses = 0
                    if phase == "A":
                        self.a_wins += 1
                        self.a_pnl += pnl
                    else:
                        self.c_wins += 1
                        self.c_pnl += pnl
                    self._update_peak_dd()
                    log_msg(f"[{phase}:WIN] #{tid} sold @ ${tp:.2f} | P&L=${pnl:+.2f} "
                            f"| bank=${self.bankroll:.2f}")
                    self._write_csv(market_name, "Sell", proceeds, shares,
                                    direction.title(), time.time())
                    self._write_log(tid, phase, direction, entry_price, tp, "won", pnl)
                    self._check_done()
                    return

                if bid <= sl:
                    proceeds = round(shares * sl, 4)
                    pnl = round(proceeds - cost, 4)
                    self.bankroll = round(self.bankroll + pnl, 4)
                    self.losses += 1
                    self.consec_losses += 1
                    if self.consec_losses > self.max_consec_losses:
                        self.max_consec_losses = self.consec_losses
                    if phase == "A":
                        self.a_losses += 1
                        self.a_pnl += pnl
                    else:
                        self.c_losses += 1
                        self.c_pnl += pnl
                    self._update_peak_dd()
                    log_msg(f"[{phase}:SL] #{tid} sold @ ${sl:.2f} | P&L=${pnl:+.2f} "
                            f"| bank=${self.bankroll:.2f}")
                    self._write_csv(market_name, "Sell", proceeds, shares,
                                    direction.title(), time.time())
                    self._write_log(tid, phase, direction, entry_price, sl, "stopped", pnl)
                    self._check_done()
                    return

                await asyncio.sleep(1)

            # Window expired — token resolves to $0
            pnl = round(-cost, 4)
            self.bankroll = round(self.bankroll + pnl, 4)
            self.losses += 1
            self.consec_losses += 1
            if self.consec_losses > self.max_consec_losses:
                self.max_consec_losses = self.consec_losses
            if phase == "A":
                self.a_losses += 1
                self.a_pnl += pnl
            else:
                self.c_losses += 1
                self.c_pnl += pnl
            self._update_peak_dd()
            log_msg(f"[{phase}:EXP] #{tid} expired @ $0 | P&L=${pnl:+.2f} "
                    f"| bank=${self.bankroll:.2f}")
            self._write_csv(market_name, "Redeem", 0.0, 0.0, "", time.time())
            self._write_log(tid, phase, direction, entry_price, 0.0, "expired", pnl)
            self._check_done()
        except Exception as e:
            log_msg(f"[{phase}:MON] err #{tid}: {e}")

    def _write_csv(self, market_name, action, usdc, tokens, token_name, ts_unix):
        try:
            fake_hash = "0x" + os.urandom(32).hex()
            with open(CSV_FILE, "a") as f:
                f.write(f'"{market_name}","{action}","{usdc}","{tokens}","{token_name}",'
                        f'"{int(ts_unix)}","{fake_hash}"\n')
        except Exception as e:
            log_msg(f"[CSV] error: {e}")

    def _write_log(self, tid, phase, direction, entry_price, exit_price, status, pnl):
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps({
                    "trade_id": tid,
                    "phase": phase,
                    "direction": direction,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "status": status,
                    "pnl": pnl,
                    "bankroll_after": self.bankroll,
                    "time": datetime.now(timezone.utc).isoformat(),
                }) + "\n")
        except Exception:
            pass
        self._write_summary()

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = (self.wins / total * 100) if total else 0
        summary = {
            "elapsed_minutes": round(elapsed, 1),
            "trades_completed": total,
            "trades_target": MAX_TRADES,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": round(wr, 1),
            "starting_bankroll": STARTING_BANKROLL,
            "current_bankroll": round(self.bankroll, 2),
            "pnl_total": round(self.bankroll - STARTING_BANKROLL, 2),
            "roi_pct": round((self.bankroll / STARTING_BANKROLL - 1) * 100, 2),
            "signals_total": self.signals,
            "unfilled": self.unfilled,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        with open(SUMMARY_FILE, "w") as f:
            json.dump(summary, f, indent=2)

    def _check_done(self):
        if self.trade_counter >= MAX_TRADES and (self.wins + self.losses) >= MAX_TRADES:
            log_msg(f"[DONE]  Reached {MAX_TRADES} trades. Final bankroll: ${self.bankroll:.2f}")
            log_msg(f"[DONE]  CSV at: {CSV_FILE}")
            self._write_summary()

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = (self.wins / total * 100) if total else 0
        pnl_total = self.bankroll - STARTING_BANKROLL
        bet_size = self.bankroll * BET_PCT
        avg_gap = (self.gap_sum / self.gap_count) if self.gap_count else 0
        a_total = self.a_wins + self.a_losses
        c_total = self.c_wins + self.c_losses
        a_wr = (self.a_wins / a_total * 100) if a_total else 0
        c_wr = (self.c_wins / c_total * 100) if c_total else 0
        market_status = "found" if self.market_finder.active_market else "none"
        print()
        print(f"  PAPER D | {elapsed:.0f}min | Combined A+C (A=$0.68-0.70/±$0.15, C=$0.87-0.89/$0.99/$0.74)")
        # Realistic estimate: discount paper P&L by 25-40% for live execution friction
        realistic_low = STARTING_BANKROLL + (pnl_total * 0.60)
        realistic_high = STARTING_BANKROLL + (pnl_total * 0.75)
        print(f"  Bankroll: ${self.bankroll:.2f} (${pnl_total:+.2f}) | Peak: ${self.peak_bankroll:.2f}")
        print(f"  Realistic Est. (60-75% of paper P&L): ${realistic_low:.2f}-${realistic_high:.2f} "
              f"(${pnl_total*0.60:+.2f} to ${pnl_total*0.75:+.2f})")
        print(f"  Bet size: ${bet_size:.2f} | DD: {self.max_drawdown_pct:.1f}% (${self.max_drawdown_usd:.2f})")
        print(f"  Trades: {total} ({self.wins}W/{self.losses}L) | Win%: {wr:.0f}% | Consec losses: {self.consec_losses}")
        print(f"  Phase A (first 4min): P&L=${self.a_pnl:+.2f}")
        print(f"    Trades: {a_total} ({self.a_wins}W/{self.a_losses}L) | Win%: {a_wr:.0f}%")
        print(f"  Phase C (last 60s):   P&L=${self.c_pnl:+.2f}")
        print(f"    Trades: {c_total} ({self.c_wins}W/{self.c_losses}L) | Win%: {c_wr:.0f}%")
        print(f"  Signals: {self.signals} | Avg gap: ${avg_gap:.2f} | Market: {market_status} | Unfilled: {self.unfilled}")
        print(f"  Feeds: CB={self.cb_ticks:,} CL={self.cl_ticks:,} | CB=${self.cb_price:,.2f} CL=${self.cl_price:,.2f}")
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
    while True:
        await asyncio.sleep(60)
        bot.print_status()


async def main():
    print("=" * 65)
    print("  PAPER STRATEGY D — Combined A+C")
    print("=" * 65)
    print(f"  Phase 1 (first 4min): Strategy A")
    print(f"    Gap signal: ${A_GAP_MIN}-${A_GAP_MAX}")
    print(f"    Entry zone: ${A_ENTRY_MIN}-${A_ENTRY_MAX}")
    print(f"    TP: entry +${A_PROFIT_DELTA} | SL: entry -${A_LOSS_DELTA}")
    print()
    print(f"  Phase 2 (last 60s): Strategy C")
    print(f"    Entry zone: ${C_ENTRY_MIN}-${C_ENTRY_MAX}")
    print(f"    TP ${C_TP} | SL ${C_SL}")
    print()
    print(f"  Bet sizing: {int(BET_PCT*100)}% of bankroll per trade")
    print(f"  Starting bankroll: ${STARTING_BANKROLL}")
    print(f"  Stop after: {MAX_TRADES} trades")
    print(f"  CSV output: {CSV_FILE}")
    print()

    # Sync to next 5-min boundary
    now = time.time()
    next_window = (int(now) // 300 + 1) * 300
    wait = next_window - now
    log_msg(f"[SYNC]  Waiting {wait:.0f}s for window boundary...")
    await asyncio.sleep(wait)
    log_msg(f"[SYNC]  Aligned. Starting bot.")

    bot = PaperBotD()
    await asyncio.gather(
        run_coinbase(bot),
        run_chainlink(bot),
        run_status(bot),
        bot.watch_phase_c(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
