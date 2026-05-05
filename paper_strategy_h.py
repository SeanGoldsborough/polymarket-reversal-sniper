#!/usr/bin/env python3
"""
PAPER STRATEGY H — Gap Signal + Oscillation Exploitation
==========================================================
Uses Coinbase/Chainlink gap to know direction, then exploits
bot-driven oscillations for optimal entry.

H1: Dip entry + hold to $1.00 (buy once on $0.05 dip, hold to resolution)
H2: Scalp oscillations (buy every $0.05 dip, sell every $0.05 rise on gap side)
H3: Dip entry + hold + $0.08 SL (same as H1 but cap wrong-side losses)

Bet sizing: 10% of bankroll per trade.
"""

import asyncio
import json
import time
import os
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

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
BET_PCT = 0.10
GAP_MIN = 15.0
GAP_MAX = 18.0
DIP_THRESHOLD = 0.05    # buy when price drops this much from recent high
SCALP_RISE = 0.05       # sell when price rises this much from buy (H2 only)
SCALP_SL = 0.08         # stop loss for scalp trades (H2)
HOLD_SL = 0.08          # stop loss for H3
TP_PRICE = 0.99

LOG_FILE = "logs/paper_strategy_h.jsonl"
SUMMARY_FILE = "logs/paper_strategy_h_summary.json"
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


# ── Variant state ──────────────────────────────────────
@dataclass
class Variant:
    name: str
    bankroll: float = STARTING_BANKROLL
    peak: float = STARTING_BANKROLL
    wins: int = 0
    losses: int = 0
    pnl_total: float = 0.0
    trade_count: int = 0
    signals: int = 0


# ── Bot ────────────────────────────────────────────────
class PaperBotH:
    def __init__(self):
        self.market_finder = MarketFinder()
        self.cb_price = 0.0
        self.cl_price = 0.0
        self.cb_ticks = 0
        self.cl_ticks = 0
        self.start_time = time.time()
        self.last_trade_window = -1
        self.gap_signals = 0
        self.variants = {
            "H1": Variant("H1: Dip + hold to $1.00"),
            "H2": Variant("H2: Scalp oscillations"),
            "H3": Variant("H3: Dip + hold + $0.08 SL"),
        }

    def on_coinbase(self, price):
        self.cb_price = price
        self.cb_ticks += 1
        if self.cl_price > 0:
            gap = price - self.cl_price
            self.check_gap(gap)

    def on_chainlink(self, price):
        self.cl_price = price
        self.cl_ticks += 1

    def check_gap(self, gap):
        if not (GAP_MIN <= abs(gap) <= GAP_MAX):
            return
        current_window = int(time.time()) // 300
        if current_window == self.last_trade_window:
            return
        self.last_trade_window = current_window
        self.gap_signals += 1
        direction = "UP" if gap > 0 else "DOWN"
        asyncio.create_task(self._on_gap_signal(direction, gap))

    async def _on_gap_signal(self, direction, gap):
        await self.market_finder.refresh()
        token_id = self.market_finder.get_token(direction)
        if not token_id:
            return

        log_msg(f"[GAP] {direction} gap=${gap:+.1f} — watching for ${DIP_THRESHOLD:.2f} dip")

        # Run all 3 variants in parallel on this signal
        await asyncio.gather(
            self._run_h1(direction, token_id, gap),
            self._run_h2(direction, token_id, gap),
            self._run_h3(direction, token_id, gap),
        )

    async def _run_h1(self, direction, token_id, gap):
        """H1: Wait for $0.05 dip on gap side, buy once, hold to $1.00."""
        v = self.variants["H1"]
        v.signals += 1
        opened_at = time.time()
        window_end = (int(time.time()) // 300) * 300 + 295

        # Phase 1: wait for dip
        recent_high = 0.0
        entry_price = None
        while time.time() < window_end:
            book = await get_book(token_id)
            if not book:
                await asyncio.sleep(1)
                continue
            bid = book["best_bid"]
            if bid > recent_high:
                recent_high = bid
            if recent_high - bid >= DIP_THRESHOLD and entry_price is None:
                entry_price = book["best_ask"]
                break
            await asyncio.sleep(0.5)

        if entry_price is None:
            return  # no dip happened

        shares = round((v.bankroll * BET_PCT) / entry_price, 2)
        cost = round(shares * entry_price, 4)
        v.trade_count += 1
        tid = v.trade_count
        log_msg(f"[H1] #{tid} BUY {direction} @ ${entry_price:.2f} ({shares} sh) — dip from ${recent_high:.2f}")

        # Phase 2: hold to resolution
        while time.time() < window_end:
            book = await get_book(token_id)
            if book and book["best_bid"] >= TP_PRICE:
                proceeds = round(shares * 1.00, 4)
                pnl = round(proceeds - cost, 4)
                self._close("H1", v, tid, direction, entry_price, 1.00, "won", pnl)
                return
            await asyncio.sleep(1)

        # Expired — check last bid
        if book and book["best_bid"] >= 0.90:
            proceeds = round(shares * 1.00, 4)
            pnl = round(proceeds - cost, 4)
            self._close("H1", v, tid, direction, entry_price, 1.00, "won", pnl)
        else:
            pnl = round(-cost, 4)
            self._close("H1", v, tid, direction, entry_price, 0.0, "expired", pnl)

    async def _run_h2(self, direction, token_id, gap):
        """H2: Scalp oscillations on gap side. Buy dips, sell rises, repeat."""
        v = self.variants["H2"]
        v.signals += 1
        opened_at = time.time()
        window_end = (int(time.time()) // 300) * 300 + 270  # stop scalping at T-30

        recent_high = 0.0
        holding = False
        buy_price = 0.0
        window_pnl = 0.0
        trades_this_window = 0
        last_buy_shares = 0.0
        last_buy_cost = 0.0

        while time.time() < window_end:
            book = await get_book(token_id)
            if not book:
                await asyncio.sleep(1)
                continue
            bid = book["best_bid"]
            if bid > recent_high:
                recent_high = bid

            if not holding:
                # Buy on dip
                if recent_high - bid >= DIP_THRESHOLD:
                    buy_price = book["best_ask"]
                    last_buy_shares = round((v.bankroll * BET_PCT) / buy_price, 2)
                    last_buy_cost = round(last_buy_shares * buy_price, 4)
                    holding = True
                    recent_high = bid  # reset
                    trades_this_window += 1
            else:
                # Take profit
                if bid - buy_price >= SCALP_RISE:
                    proceeds = round(last_buy_shares * bid, 4)
                    trade_pnl = round(proceeds - last_buy_cost, 4)
                    window_pnl += trade_pnl
                    holding = False
                    recent_high = bid
                # Stop loss
                elif buy_price - bid >= SCALP_SL:
                    proceeds = round(last_buy_shares * bid, 4)
                    trade_pnl = round(proceeds - last_buy_cost, 4)
                    window_pnl += trade_pnl
                    holding = False
                    recent_high = bid

            await asyncio.sleep(0.5)

        # If still holding at T-30, hold to resolution
        if holding:
            # Check final outcome
            final_end = (int(time.time()) // 300) * 300 + 295
            while time.time() < final_end:
                book = await get_book(token_id)
                if book and book["best_bid"] >= TP_PRICE:
                    proceeds = round(last_buy_shares * 1.00, 4)
                    window_pnl += round(proceeds - last_buy_cost, 4)
                    holding = False
                    break
                await asyncio.sleep(1)
            if holding:
                if book and book["best_bid"] >= 0.90:
                    proceeds = round(last_buy_shares * 1.00, 4)
                    window_pnl += round(proceeds - last_buy_cost, 4)
                else:
                    window_pnl += round(-last_buy_cost, 4)

        if trades_this_window > 0:
            v.trade_count += trades_this_window
            status = "won" if window_pnl > 0 else "lost"
            self._close("H2", v, v.trade_count, direction, 0, 0,
                         status, window_pnl, trades_this_window)

    async def _run_h3(self, direction, token_id, gap):
        """H3: Wait for $0.05 dip, buy once, hold to $1.00, SL at -$0.08."""
        v = self.variants["H3"]
        v.signals += 1
        opened_at = time.time()
        window_end = (int(time.time()) // 300) * 300 + 295

        # Phase 1: wait for dip
        recent_high = 0.0
        entry_price = None
        while time.time() < window_end:
            book = await get_book(token_id)
            if not book:
                await asyncio.sleep(1)
                continue
            bid = book["best_bid"]
            if bid > recent_high:
                recent_high = bid
            if recent_high - bid >= DIP_THRESHOLD and entry_price is None:
                entry_price = book["best_ask"]
                break
            await asyncio.sleep(0.5)

        if entry_price is None:
            return

        shares = round((v.bankroll * BET_PCT) / entry_price, 2)
        cost = round(shares * entry_price, 4)
        sl_price = round(entry_price - HOLD_SL, 2)
        v.trade_count += 1
        tid = v.trade_count
        log_msg(f"[H3] #{tid} BUY {direction} @ ${entry_price:.2f} ({shares} sh) SL ${sl_price:.2f}")

        # Phase 2: hold to resolution with SL
        while time.time() < window_end:
            book = await get_book(token_id)
            if not book:
                await asyncio.sleep(1)
                continue
            bid = book["best_bid"]

            if bid >= TP_PRICE:
                proceeds = round(shares * 1.00, 4)
                pnl = round(proceeds - cost, 4)
                self._close("H3", v, tid, direction, entry_price, 1.00, "won", pnl)
                return
            if bid <= sl_price:
                proceeds = round(shares * sl_price, 4)
                pnl = round(proceeds - cost, 4)
                self._close("H3", v, tid, direction, entry_price, sl_price, "stopped", pnl)
                return
            await asyncio.sleep(1)

        # Expired
        if book and book["best_bid"] >= 0.90:
            proceeds = round(shares * 1.00, 4)
            pnl = round(proceeds - cost, 4)
            self._close("H3", v, tid, direction, entry_price, 1.00, "won", pnl)
        else:
            pnl = round(-cost, 4)
            self._close("H3", v, tid, direction, entry_price, 0.0, "expired", pnl)

    def _close(self, key, v, tid, direction, entry, exit_p, status, pnl, trade_count=1):
        v.bankroll = round(v.bankroll + pnl, 4)
        if v.bankroll > v.peak:
            v.peak = v.bankroll
        v.pnl_total += pnl
        if pnl > 0:
            v.wins += 1
        else:
            v.losses += 1

        log_msg(f"[{key}] #{tid} {status.upper()} {direction} | P&L ${pnl:+.2f} | bank=${v.bankroll:.2f}"
                + (f" ({trade_count} scalps)" if trade_count > 1 else ""))

        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps({
                    "variant": key,
                    "trade_id": tid,
                    "direction": direction,
                    "entry_price": entry,
                    "exit_price": exit_p,
                    "status": status,
                    "pnl": pnl,
                    "bankroll": v.bankroll,
                    "trade_count": trade_count,
                    "time": datetime.now(timezone.utc).isoformat(),
                }) + "\n")
        except Exception:
            pass
        self._write_summary()

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        summary = {
            "elapsed_minutes": round(elapsed, 1),
            "gap_signals": self.gap_signals,
            "variants": {},
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        for key, v in self.variants.items():
            total = v.wins + v.losses
            wr = (v.wins / total * 100) if total else 0
            summary["variants"][key] = {
                "name": v.name,
                "trades": v.trade_count,
                "wins": v.wins,
                "losses": v.losses,
                "win_rate": round(wr, 1),
                "bankroll": round(v.bankroll, 2),
                "peak": round(v.peak, 2),
                "pnl_total": round(v.pnl_total, 2),
                "roi_pct": round((v.bankroll / STARTING_BANKROLL - 1) * 100, 2),
                "signals": v.signals,
            }
        with open(SUMMARY_FILE, "w") as f:
            json.dump(summary, f, indent=2)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        print()
        print(f"  PAPER H — GAP + OSCILLATION | {elapsed:.0f}min")
        print(f"  Gap signals: {self.gap_signals} | "
              f"Feeds: CB={self.cb_ticks:,} CL={self.cl_ticks:,} | "
              f"CB=${self.cb_price:,.2f} CL=${self.cl_price:,.2f}")
        print()
        for key, v in self.variants.items():
            total = v.wins + v.losses
            wr = (v.wins / total * 100) if total else 0
            pnl = v.bankroll - STARTING_BANKROLL
            realistic_low = STARTING_BANKROLL + (pnl * 0.60)
            realistic_high = STARTING_BANKROLL + (pnl * 0.75)
            print(f"  {key} ({v.name}):")
            print(f"    Bankroll: ${v.bankroll:.2f} (${pnl:+.2f}) | Peak: ${v.peak:.2f}")
            print(f"    Realistic Est.: ${realistic_low:.2f}-${realistic_high:.2f}")
            print(f"    Trades: {v.trade_count} ({v.wins}W/{v.losses}L) | Win%: {wr:.0f}%")
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
    print("  PAPER STRATEGY H — GAP SIGNAL + OSCILLATION EXPLOITATION")
    print("=" * 65)
    print(f"  Gap signal: ${GAP_MIN}-${GAP_MAX} (Coinbase vs Chainlink)")
    print(f"  Dip threshold: ${DIP_THRESHOLD:.2f}")
    print()
    print(f"  H1: Buy dip + hold to $1.00 (no SL)")
    print(f"  H2: Scalp (buy ${DIP_THRESHOLD} dips, sell ${SCALP_RISE} rises, SL ${SCALP_SL})")
    print(f"  H3: Buy dip + hold to $1.00 + ${HOLD_SL} SL")
    print()
    print(f"  Bet sizing: {int(BET_PCT*100)}% of bankroll per trade")
    print(f"  Starting bankroll: ${STARTING_BANKROLL}")
    print()

    now = time.time()
    next_window = (int(now) // 300 + 1) * 300
    wait = next_window - now
    log_msg(f"[SYNC] Waiting {wait:.0f}s for window boundary...")
    await asyncio.sleep(wait)
    log_msg(f"[SYNC] Aligned. Starting bot.")

    bot = PaperBotH()
    await asyncio.gather(
        run_coinbase(bot),
        run_chainlink(bot),
        run_status(bot),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
