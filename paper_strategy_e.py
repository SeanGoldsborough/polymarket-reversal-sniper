#!/usr/bin/env python3
"""
PAPER STRATEGY E — Late-window timing tests
=============================================
Tests 4 variants in parallel on every window:

  E1: T-60 entry, TP entry+$0.15, SL entry-$0.15
  E2: T-60 entry, hold to $1.00 (or expiry)
  E3: T-30 entry, TP entry+$0.15, SL entry-$0.15
  E4: T-30 entry, hold to $1.00 (or expiry)

Entry: At T-60 or T-30, identify the leading side (higher midpoint) and buy
       at the current ask. Skip if no clear leader (mids equal).

Bet size: 10% of bankroll per trade (each variant has its own bankroll).
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
STARTING_BANKROLL = 100.0
BET_PCT = 0.10
TP_DELTA = 0.15
SL_DELTA = 0.15
HOLD_TARGET = 1.00
T_60 = 60   # seconds remaining when E1/E2 enter
T_30 = 30   # seconds remaining when E3/E4 enter

LOG_FILE = "logs/paper_strategy_e.jsonl"
SUMMARY_FILE = "logs/paper_strategy_e_summary.json"
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


# ── Variant ────────────────────────────────────────────
@dataclass
class Variant:
    name: str
    trigger_remaining: int   # seconds before window end to enter
    use_tp_sl: bool          # True = $0.15 TP/SL, False = hold to $1
    sl_offset: float = 0.0   # if > 0, adds an SL at (entry - sl_offset). Only for hold-to-$1.
    bankroll: float = STARTING_BANKROLL
    peak: float = STARTING_BANKROLL
    wins: int = 0
    losses: int = 0
    pnl_total: float = 0.0
    trade_count: int = 0


# ── Bot ────────────────────────────────────────────────
class PaperBotE:
    def __init__(self):
        self.market_finder = MarketFinder()
        self.cb_price = 0.0
        self.cl_price = 0.0
        self.cb_ticks = 0
        self.cl_ticks = 0
        self.start_time = time.time()
        self.variants = {
            "E1": Variant("E1: T-60 + $0.15 TP/SL", T_60, True),
            "E2": Variant("E2: T-60 + hold to $1.00", T_60, False),
            "E3": Variant("E3: T-30 + $0.15 TP/SL", T_30, True),
            "E4": Variant("E4: T-30 + hold to $1.00", T_30, False),
            "E5": Variant("E5: T-30 + hold to $1.00 + $0.10 SL", T_30, False, sl_offset=0.10),
            "E5F": Variant("E5+Fix: T-30 + hold + pre-placed SL + min $0.70 + fees", T_30, False, sl_offset=0.10),
        }
        self.last_window_processed = -1

    def on_coinbase(self, price):
        self.cb_price = price
        self.cb_ticks += 1

    def on_chainlink(self, price):
        self.cl_price = price
        self.cl_ticks += 1

    async def watch_windows(self):
        """At each window, fire all variant trades at their trigger times."""
        while True:
            try:
                # Wait for the next T-60 trigger
                now = time.time()
                window_start = (int(now) // 300) * 300
                t60_trigger = window_start + (300 - T_60)

                if now >= t60_trigger:
                    next_window = window_start + 300
                    t60_trigger = next_window + (300 - T_60)

                wait = t60_trigger - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)

                # Time filter (ET): skip 9-11am and 2-3pm
                et_hour = (datetime.now(timezone.utc).hour - 4) % 24
                if et_hour in (9, 10, 11, 14, 15):
                    log_msg(f"[WIN] Skipping window — outside trading hours (ET {et_hour}:00)")
                    await asyncio.sleep(60)
                    continue

                # We're at T-60 of the current window
                self.market_finder.last_refresh = 0
                await self.market_finder.refresh()
                if not self.market_finder.active_market:
                    log_msg("[WIN] No market for window")
                    continue

                # Don't double-process the same window
                current_window = (int(time.time()) // 300) * 300
                if current_window == self.last_window_processed:
                    continue
                self.last_window_processed = current_window

                up_token = self.market_finder.active_market["up_token"]
                down_token = self.market_finder.active_market["down_token"]

                # Fire E1 and E2 at T-60
                await self._fire_entries(up_token, down_token, "T-60")

                # Wait until T-30
                await asyncio.sleep(30)

                # Fire E3 and E4 at T-30
                await self._fire_entries(up_token, down_token, "T-30")
            except Exception as e:
                log_msg(f"[WATCH] error: {e}")
                await asyncio.sleep(5)

    async def _fire_entries(self, up_token, down_token, label):
        """Read both books, identify the leading side, and fire variant trades."""
        up_book, down_book = await asyncio.gather(get_book(up_token), get_book(down_token))
        if not up_book or not down_book:
            log_msg(f"[{label}] book read failed")
            return
        if up_book["midpoint"] == down_book["midpoint"]:
            log_msg(f"[{label}] no clear leader (mids equal)")
            return

        if up_book["midpoint"] > down_book["midpoint"]:
            direction = "UP"
            book = up_book
            token_id = up_token
        else:
            direction = "DOWN"
            book = down_book
            token_id = down_token

        ask = book["best_ask"]
        if ask <= 0 or ask >= 1.0:
            log_msg(f"[{label}] invalid ask ${ask:.2f}")
            return

        # Fire the two variants at this trigger
        if label == "T-60":
            asyncio.create_task(self._open_variant("E1", direction, token_id, ask))
            asyncio.create_task(self._open_variant("E2", direction, token_id, ask))
        else:
            asyncio.create_task(self._open_variant("E3", direction, token_id, ask))
            asyncio.create_task(self._open_variant("E4", direction, token_id, ask))
            asyncio.create_task(self._open_variant("E5", direction, token_id, ask))
            # E5F: only enter if midpoint >= $0.70
            if book["midpoint"] >= 0.70:
                asyncio.create_task(self._open_variant("E5F", direction, token_id, ask))
            else:
                pass  # skip — below min entry

    async def _open_variant(self, key, direction, token_id, entry_price):
        v = self.variants[key]

        # E5F: simulate taker fee (~2%) on entry
        if key == "E5F":
            fee_rate = 0.02
            effective_entry = round(entry_price * (1 + fee_rate), 4)
        else:
            effective_entry = entry_price

        shares = round((v.bankroll * BET_PCT) / effective_entry, 2)
        cost = round(shares * effective_entry, 4)
        v.trade_count += 1
        tid = v.trade_count
        opened_at = time.time()

        log_msg(f"[{key}] OPEN #{tid} {direction} @ ${effective_entry:.2f} "
                f"({shares} sh, ${cost:.2f}) | bank=${v.bankroll:.2f}")

        try:
            # E5F: simulate balance verification delay (3-5s)
            if key == "E5F":
                await asyncio.sleep(4)
            else:
                await asyncio.sleep(1)

            while time.time() - opened_at < 295:
                book = await get_book(token_id)
                if not book:
                    await asyncio.sleep(1)
                    continue
                bid = book["best_bid"]

                if v.use_tp_sl:
                    # E1/E3: $0.15 TP and SL
                    tp = round(entry_price + TP_DELTA, 2)
                    sl = round(entry_price - SL_DELTA, 2)
                    if bid >= tp:
                        self._close_variant(v, key, tid, shares, cost, tp, "won")
                        return
                    if bid <= sl:
                        self._close_variant(v, key, tid, shares, cost, sl, "stopped")
                        return
                else:
                    # E2/E4/E5: hold to $1.00 (only exit if bid hits $0.99+)
                    if bid >= 0.99:
                        self._close_variant(v, key, tid, shares, cost, HOLD_TARGET, "won")
                        return
                    # Optional SL for hold-to-$1 variants (E5, E5F)
                    if v.sl_offset > 0:
                        sl_price = round(effective_entry - v.sl_offset, 2)
                        if bid <= sl_price:
                            if key == "E5F":
                                # Simulate pre-placed SL: fills at SL price if bid >= SL
                                # But if bid crashed PAST the SL (gap down), fill at actual bid
                                # with $0.02 slippage
                                if bid < sl_price - 0.05:
                                    # SL was on the book but price gapped through it
                                    fill_price = round(bid + 0.02, 2)  # slight improvement from book order
                                else:
                                    fill_price = sl_price  # pre-placed order filled at SL price
                                self._close_variant(v, key, tid, shares, cost, fill_price, "stopped")
                            else:
                                self._close_variant(v, key, tid, shares, cost, sl_price, "stopped")
                            return

                await asyncio.sleep(1)

            # Window expired
            # For TP/SL variants, expired = $0 wipeout
            # For hold-to-$1 variants, expired = $0 wipeout (didn't reach $0.99)
            self._close_variant(v, key, tid, shares, cost, 0.0, "expired")
        except Exception as e:
            log_msg(f"[{key}] err #{tid}: {e}")

    def _close_variant(self, v, key, tid, shares, cost, exit_price, status):
        proceeds = round(shares * exit_price, 4)
        pnl = round(proceeds - cost, 4)
        v.bankroll = round(v.bankroll + pnl, 4)
        if v.bankroll > v.peak:
            v.peak = v.bankroll
        v.pnl_total += pnl
        if pnl > 0:
            v.wins += 1
        else:
            v.losses += 1
        log_msg(f"[{key}] {status.upper()} #{tid} @ ${exit_price:.2f} | P&L=${pnl:+.2f} "
                f"| bank=${v.bankroll:.2f}")
        self._write_log(key, tid, exit_price, status, pnl, v.bankroll)
        self._write_summary()

    def _write_log(self, key, tid, exit_price, status, pnl, bankroll):
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps({
                    "variant": key,
                    "trade_id": tid,
                    "exit_price": exit_price,
                    "status": status,
                    "pnl": pnl,
                    "bankroll": bankroll,
                    "time": datetime.now(timezone.utc).isoformat(),
                }) + "\n")
        except Exception:
            pass

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        summary = {
            "elapsed_minutes": round(elapsed, 1),
            "variants": {},
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        for k, v in self.variants.items():
            total = v.wins + v.losses
            wr = (v.wins / total * 100) if total else 0
            summary["variants"][k] = {
                "name": v.name,
                "trades": v.trade_count,
                "wins": v.wins,
                "losses": v.losses,
                "win_rate": round(wr, 1),
                "bankroll": round(v.bankroll, 2),
                "pnl_total": round(v.pnl_total, 2),
                "roi_pct": round((v.bankroll / STARTING_BANKROLL - 1) * 100, 2),
            }
        with open(SUMMARY_FILE, "w") as f:
            json.dump(summary, f, indent=2)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        print()
        print(f"  PAPER E | {elapsed:.0f}min | Late-window timing tests")
        print(f"  Feeds: CB={self.cb_ticks:,} CL={self.cl_ticks:,} | CB=${self.cb_price:,.2f} CL=${self.cl_price:,.2f}")
        print()
        for k, v in self.variants.items():
            total = v.wins + v.losses
            wr = (v.wins / total * 100) if total else 0
            roi = (v.bankroll / STARTING_BANKROLL - 1) * 100
            print(f"  {v.name}")
            print(f"    Bankroll: ${v.bankroll:.2f} (${v.pnl_total:+.2f}, ROI {roi:+.1f}%)")
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
    print("  PAPER STRATEGY E — Late-window timing tests")
    print("=" * 65)
    print(f"  E1: T-60 entry + $0.15 TP/SL")
    print(f"  E2: T-60 entry + hold to $1.00")
    print(f"  E3: T-30 entry + $0.15 TP/SL")
    print(f"  E4: T-30 entry + hold to $1.00")
    print(f"  E5: T-30 entry + hold to $1.00 + $0.10 SL")
    print(f"  E5F: E5+Fix (pre-placed SL, min $0.70, 2% fee sim, balance delay)")
    print(f"  Each variant: ${STARTING_BANKROLL} bankroll, {int(BET_PCT*100)}% per trade")
    print()

    now = time.time()
    next_window = (int(now) // 300 + 1) * 300
    wait = next_window - now
    log_msg(f"[SYNC] Waiting {wait:.0f}s for window boundary...")
    await asyncio.sleep(wait)
    log_msg(f"[SYNC] Aligned. Starting bot.")

    bot = PaperBotE()
    await asyncio.gather(
        run_coinbase(bot),
        run_chainlink(bot),
        run_status(bot),
        bot.watch_windows(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
