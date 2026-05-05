#!/usr/bin/env python3
"""
PAPER STRATEGY G — Both Sides Hedge
=====================================
At the start of each 5-min window, buy BOTH UP and DOWN tokens at ~$0.50.
One will go to $1.00, the other to $0.00.

The edge: sell the losing side via SL at $0.30-$0.40 before it hits $0,
while holding the winner to $1.00.

Variants tested in parallel:
  G1: SL at entry - $0.10 (sell loser at ~$0.40)
  G2: SL at entry - $0.15 (sell loser at ~$0.35)
  G3: SL at entry - $0.20 (sell loser at ~$0.30)
  G4: No SL on loser (baseline — break even minus fees)

Bet sizing: 10% of bankroll split equally between UP and DOWN (5% each side).
"""

import asyncio
import json
import time
import os
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field
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
BET_PCT = 0.10            # total per window (split 50/50)
ENTRY_DELAY = 0           # enter immediately at window open
TP_PRICE = 0.99           # winner exits here
SL_OFFSETS = {
    "G1": 0.10,
    "G2": 0.15,
    "G3": 0.20,
    "G4": 0.00,  # no SL
    "G5": 0.10,  # same SL as G1 but uses maker bids instead of taker asks
    "G6": 0.05,  # delayed SL — only placed on confirmed loser after one side hits $0.65
    "G7": 0.05,  # buy both, NO SL until T+210, then SL loser at bid-$0.05
    "G8": 0.10,  # single-side: buy ONLY the leader at T+210, SL at entry-$0.10, hold to $1
}

LOG_FILE = "logs/paper_strategy_g.jsonl"
SUMMARY_FILE = "logs/paper_strategy_g_summary.json"
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


# ── Variant state ──────────────────────────────────────
@dataclass
class Variant:
    name: str
    sl_offset: float
    use_maker: bool = False    # True = bid at mid-$0.02 instead of buy at ask
    delayed_sl: bool = False   # True = wait for one side to hit $0.65 before placing SL on loser
    wait_for_210: bool = False # True = no SL until T+210, then SL loser
    single_side: bool = False  # True = only buy the leader at T+210 (don't buy both)
    bankroll: float = STARTING_BANKROLL
    peak: float = STARTING_BANKROLL
    wins: int = 0          # windows where net P&L > 0
    losses: int = 0        # windows where net P&L <= 0
    pnl_total: float = 0.0
    trade_count: int = 0   # number of windows traded
    unfilled: int = 0      # windows where maker bids didn't fill


# ── Bot ────────────────────────────────────────────────
class PaperBotG:
    def __init__(self):
        self.market_finder = MarketFinder()
        self.cb_price = 0.0
        self.cl_price = 0.0
        self.cb_ticks = 0
        self.cl_ticks = 0
        self.start_time = time.time()
        self.variants = {}
        for key, offset in SL_OFFSETS.items():
            self.variants[key] = Variant(
                name=key, sl_offset=offset,
                use_maker=(key == "G5"),
                delayed_sl=(key == "G6"),
                wait_for_210=(key == "G7"),
                single_side=(key == "G8"),
            )

    def on_coinbase(self, price):
        self.cb_price = price
        self.cb_ticks += 1

    def on_chainlink(self, price):
        self.cl_price = price
        self.cl_ticks += 1

    async def watch_windows(self):
        """At the start of each window (+ delay), buy both sides."""
        while True:
            try:
                # Wait for next window + entry delay
                now = time.time()
                next_window = (int(now) // 300 + 1) * 300
                entry_time = next_window + ENTRY_DELAY
                wait = entry_time - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)

                self.market_finder.last_refresh = 0
                await self.market_finder.refresh()
                if not self.market_finder.active_market:
                    continue

                up_token = self.market_finder.active_market["up_token"]
                down_token = self.market_finder.active_market["down_token"]

                # Read both books
                up_book, down_book = await asyncio.gather(
                    get_book(up_token), get_book(down_token)
                )
                if not up_book or not down_book:
                    continue

                up_ask = up_book["best_ask"]
                down_ask = down_book["best_ask"]

                # Skip if combined cost is > $1.05 (too expensive, fees would kill us)
                if up_ask + down_ask > 1.05:
                    log_msg(f"[G:SKIP] Combined ask ${up_ask + down_ask:.2f} > $1.05")
                    continue

                # Skip if either side is < $0.10 or > $0.90 (already decided)
                if up_ask < 0.10 or up_ask > 0.90 or down_ask < 0.10 or down_ask > 0.90:
                    log_msg(f"[G:SKIP] Not balanced enough: UP=${up_ask:.2f} DOWN=${down_ask:.2f}")
                    continue

                log_msg(f"[G:OPEN] Buying both sides: UP @ ${up_ask:.2f} + DOWN @ ${down_ask:.2f} "
                        f"= ${up_ask + down_ask:.2f}")

                # Run all variants in parallel for this window
                up_mid = up_book["midpoint"]
                down_mid = down_book["midpoint"]
                tasks = []
                for key, v in self.variants.items():
                    if v.single_side:
                        continue  # G8 has its own watcher
                    tasks.append(self._run_window(key, v, up_token, down_token,
                                                  up_ask, down_ask, up_mid, down_mid))
                await asyncio.gather(*tasks)

            except Exception as e:
                log_msg(f"[G] error: {e}")
                await asyncio.sleep(5)

    async def _run_window(self, key, v, up_token, down_token, up_entry, down_entry,
                          up_mid=0, down_mid=0):
        """Simulate one window for one variant: buy both, SL the loser, TP the winner."""

        # G5: maker entry — bid at mid - $0.02 and wait for fill
        if v.use_maker:
            up_bid_price = round(up_mid - 0.02, 2)
            down_bid_price = round(down_mid - 0.02, 2)
            if up_bid_price < 0.05 or down_bid_price < 0.05:
                v.unfilled += 1
                return

            # Wait up to 30s for both sides to fill (ask drops to our bid)
            up_filled = False
            down_filled = False
            start = time.time()
            while time.time() - start < 30:
                up_book, down_book = await asyncio.gather(
                    get_book(up_token), get_book(down_token)
                )
                if not up_filled and up_book and up_book["best_ask"] <= up_bid_price:
                    up_filled = True
                if not down_filled and down_book and down_book["best_ask"] <= down_bid_price:
                    down_filled = True
                if up_filled and down_filled:
                    break
                await asyncio.sleep(0.5)

            if not up_filled or not down_filled:
                v.unfilled += 1
                log_msg(f"[{key}] Maker bids not filled (UP={'Y' if up_filled else 'N'} "
                        f"DOWN={'Y' if down_filled else 'N'})")
                return

            up_entry = up_bid_price
            down_entry = down_bid_price
            log_msg(f"[{key}] MAKER FILLED both @ UP=${up_entry:.2f} DOWN=${down_entry:.2f} "
                    f"= ${up_entry + down_entry:.2f}")

        v.trade_count += 1
        tid = v.trade_count

        # 10% of bankroll split 50/50
        total_bet = v.bankroll * BET_PCT
        half_bet = total_bet / 2
        up_shares = round(half_bet / up_entry, 2)
        down_shares = round(half_bet / down_entry, 2)
        up_cost = round(up_shares * up_entry, 4)
        down_cost = round(down_shares * down_entry, 4)
        total_cost = up_cost + down_cost

        sl_offset = v.sl_offset

        # G6/G7: don't set SL prices until conditions met
        if v.delayed_sl or v.wait_for_210:
            up_sl = 0
            down_sl = 0
        else:
            up_sl = round(up_entry - sl_offset, 2) if sl_offset > 0 else 0
            down_sl = round(down_entry - sl_offset, 2) if sl_offset > 0 else 0

        # Track which side exited
        up_exited = False
        down_exited = False
        up_proceeds = 0.0
        down_proceeds = 0.0
        confirmed_winner = None  # "UP" or "DOWN" once one side hits $0.65

        opened_at = time.time()

        try:
            await asyncio.sleep(1)
            while time.time() - opened_at < 290:
                up_book, down_book = await asyncio.gather(
                    get_book(up_token), get_book(down_token)
                )

                # G6: check if a winner has been confirmed ($0.65+)
                if v.delayed_sl and confirmed_winner is None:
                    if up_book and up_book["best_bid"] >= 0.65:
                        confirmed_winner = "UP"
                        down_sl = round(down_book["best_bid"] - sl_offset, 2) if down_book else 0
                        if down_sl < 0.01:
                            down_sl = 0.01
                        log_msg(f"[{key}] #{tid} UP confirmed winner @ ${up_book['best_bid']:.2f} "
                                f"— SL on DOWN @ ${down_sl:.2f}")
                    elif down_book and down_book["best_bid"] >= 0.65:
                        confirmed_winner = "DOWN"
                        up_sl = round(up_book["best_bid"] - sl_offset, 2) if up_book else 0
                        if up_sl < 0.01:
                            up_sl = 0.01
                        log_msg(f"[{key}] #{tid} DOWN confirmed winner @ ${down_book['best_bid']:.2f} "
                                f"— SL on UP @ ${up_sl:.2f}")

                # G7: wait until T+210 then SL the loser
                age = time.time() - opened_at
                if v.wait_for_210 and confirmed_winner is None and age >= 210:
                    if up_book and down_book:
                        if up_book["best_bid"] > down_book["best_bid"]:
                            confirmed_winner = "UP"
                            down_sl = round(down_book["best_bid"] - sl_offset, 2)
                            if down_sl < 0.01:
                                down_sl = 0.01
                            log_msg(f"[{key}] #{tid} T+210: UP leading ${up_book['best_bid']:.2f} "
                                    f"— SL on DOWN @ ${down_sl:.2f}")
                        else:
                            confirmed_winner = "DOWN"
                            up_sl = round(up_book["best_bid"] - sl_offset, 2)
                            if up_sl < 0.01:
                                up_sl = 0.01
                            log_msg(f"[{key}] #{tid} T+210: DOWN leading ${down_book['best_bid']:.2f} "
                                    f"— SL on UP @ ${up_sl:.2f}")

                # UP side
                if not up_exited and up_book:
                    up_bid = up_book["best_bid"]
                    # TP
                    if up_bid >= TP_PRICE:
                        up_proceeds = round(up_shares * 1.00, 4)
                        up_exited = True
                    # SL (only if SL is set — for G6, only after winner confirmed)
                    elif up_sl > 0 and up_bid <= up_sl:
                        up_proceeds = round(up_shares * up_sl, 4)
                        up_exited = True

                # DOWN side
                if not down_exited and down_book:
                    down_bid = down_book["best_bid"]
                    # TP
                    if down_bid >= TP_PRICE:
                        down_proceeds = round(down_shares * 1.00, 4)
                        down_exited = True
                    # SL (only if SL is set)
                    elif down_sl > 0 and down_bid <= down_sl:
                        down_proceeds = round(down_shares * down_sl, 4)
                        down_exited = True

                # Both exited?
                if up_exited and down_exited:
                    break

                await asyncio.sleep(1)

            # Window ended — handle any un-exited sides
            if not up_exited:
                # Expired — assume resolution. Check last bid.
                if up_book and up_book["best_bid"] >= 0.90:
                    up_proceeds = round(up_shares * 1.00, 4)  # won
                else:
                    up_proceeds = 0.0  # lost
            if not down_exited:
                if down_book and down_book["best_bid"] >= 0.90:
                    down_proceeds = round(down_shares * 1.00, 4)
                else:
                    down_proceeds = 0.0

            total_proceeds = up_proceeds + down_proceeds
            pnl = round(total_proceeds - total_cost, 4)
            v.bankroll = round(v.bankroll + pnl, 4)
            if v.bankroll > v.peak:
                v.peak = v.bankroll
            v.pnl_total += pnl
            if pnl > 0:
                v.wins += 1
            else:
                v.losses += 1

            log_msg(f"[{key}] #{tid} UP=${up_proceeds:.2f} DOWN=${down_proceeds:.2f} "
                    f"cost=${total_cost:.2f} P&L=${pnl:+.2f} bank=${v.bankroll:.2f}")

            # Log
            try:
                with open(LOG_FILE, "a") as f:
                    f.write(json.dumps({
                        "variant": key,
                        "trade_id": tid,
                        "up_entry": up_entry,
                        "down_entry": down_entry,
                        "up_proceeds": up_proceeds,
                        "down_proceeds": down_proceeds,
                        "total_cost": total_cost,
                        "pnl": pnl,
                        "bankroll": v.bankroll,
                        "time": datetime.now(timezone.utc).isoformat(),
                    }) + "\n")
            except Exception:
                pass
            self._write_summary()

        except Exception as e:
            log_msg(f"[{key}] #{tid} error: {e}")

    async def watch_g8(self):
        """G8: Single-side strategy. At T+210, buy ONLY the leading side. SL at entry-$0.10."""
        v = self.variants["G8"]
        while True:
            try:
                # Wait for next window + 210s
                now = time.time()
                next_window = (int(now) // 300 + 1) * 300
                trigger = next_window + 210
                wait = trigger - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)

                self.market_finder.last_refresh = 0
                await self.market_finder.refresh()
                if not self.market_finder.active_market:
                    continue

                up_token = self.market_finder.active_market["up_token"]
                down_token = self.market_finder.active_market["down_token"]

                up_book, down_book = await asyncio.gather(
                    get_book(up_token), get_book(down_token)
                )
                if not up_book or not down_book:
                    continue

                # Pick the leader
                up_bid = up_book["best_bid"]
                down_bid = down_book["best_bid"]
                if up_bid > down_bid:
                    direction = "UP"
                    token = up_token
                    entry_price = up_book["best_ask"]
                    book = up_book
                elif down_bid > up_bid:
                    direction = "DOWN"
                    token = down_token
                    entry_price = down_book["best_ask"]
                    book = down_book
                else:
                    v.unfilled += 1
                    continue

                if entry_price <= 0 or entry_price >= 1.0:
                    v.unfilled += 1
                    continue

                v.trade_count += 1
                tid = v.trade_count
                total_bet = v.bankroll * BET_PCT
                shares = round(total_bet / entry_price, 2)
                cost = round(shares * entry_price, 4)
                sl_price = round(entry_price - v.sl_offset, 2)

                log_msg(f"[G8] #{tid} T+210 BUY {direction} @ ${entry_price:.2f} "
                        f"({shares} sh, ${cost:.2f}) | SL ${sl_price:.2f}")

                # Monitor for TP or SL
                opened_at = time.time()
                window_end = (int(time.time()) // 300) * 300 + 295
                proceeds = 0.0
                status = "expired"

                while time.time() < window_end:
                    b = await get_book(token)
                    if not b:
                        await asyncio.sleep(1)
                        continue
                    bid = b["best_bid"]

                    if bid >= TP_PRICE:
                        proceeds = round(shares * 1.00, 4)
                        status = "won"
                        break
                    if bid <= sl_price:
                        proceeds = round(shares * sl_price, 4)
                        status = "stopped"
                        break
                    await asyncio.sleep(1)

                if status == "expired":
                    # Check last bid to estimate
                    if b and b["best_bid"] >= 0.90:
                        proceeds = round(shares * 1.00, 4)
                        status = "won"
                    else:
                        proceeds = 0.0

                pnl = round(proceeds - cost, 4)
                v.bankroll = round(v.bankroll + pnl, 4)
                if v.bankroll > v.peak:
                    v.peak = v.bankroll
                v.pnl_total += pnl
                if pnl > 0:
                    v.wins += 1
                else:
                    v.losses += 1

                log_msg(f"[G8] #{tid} {status.upper()} {direction} @ "
                        f"${proceeds/shares if shares > 0 else 0:.2f} | "
                        f"P&L ${pnl:+.2f} | bank=${v.bankroll:.2f}")

                try:
                    with open(LOG_FILE, "a") as f:
                        f.write(json.dumps({
                            "variant": "G8",
                            "trade_id": tid,
                            "direction": direction,
                            "entry_price": entry_price,
                            "sl_price": sl_price,
                            "proceeds": proceeds,
                            "pnl": pnl,
                            "status": status,
                            "bankroll": v.bankroll,
                            "time": datetime.now(timezone.utc).isoformat(),
                        }) + "\n")
                except Exception:
                    pass
                self._write_summary()

            except Exception as e:
                log_msg(f"[G8] error: {e}")
                await asyncio.sleep(5)

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        summary = {
            "elapsed_minutes": round(elapsed, 1),
            "variants": {},
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        for key, v in self.variants.items():
            total = v.wins + v.losses
            wr = (v.wins / total * 100) if total else 0
            summary["variants"][key] = {
                "sl_offset": v.sl_offset,
                "trades": v.trade_count,
                "wins": v.wins,
                "losses": v.losses,
                "win_rate": round(wr, 1),
                "bankroll": round(v.bankroll, 2),
                "peak": round(v.peak, 2),
                "pnl_total": round(v.pnl_total, 2),
                "roi_pct": round((v.bankroll / STARTING_BANKROLL - 1) * 100, 2),
            }
        with open(SUMMARY_FILE, "w") as f:
            json.dump(summary, f, indent=2)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        print()
        print(f"  PAPER G — BOTH SIDES HEDGE | {elapsed:.0f}min")
        print(f"  Feeds: CB={self.cb_ticks:,} CL={self.cl_ticks:,} | "
              f"CB=${self.cb_price:,.2f} CL=${self.cl_price:,.2f}")
        print()
        for key, v in self.variants.items():
            total = v.wins + v.losses
            wr = (v.wins / total * 100) if total else 0
            pnl = v.bankroll - STARTING_BANKROLL
            sl_label = f"SL entry-${v.sl_offset:.2f}" if v.sl_offset > 0 else "No SL"
            print(f"  {key} ({sl_label}):")
            print(f"    Bankroll: ${v.bankroll:.2f} (${pnl:+.2f}) | Peak: ${v.peak:.2f}")
            unfilled = f" | Unfilled: {v.unfilled}" if v.unfilled else ""
            print(f"    Trades: {total} ({v.wins}W/{v.losses}L) | Win%: {wr:.0f}%{unfilled}")
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
    print("  PAPER STRATEGY G — BOTH SIDES HEDGE")
    print("=" * 65)
    print(f"  Buy BOTH UP and DOWN at window start (+{ENTRY_DELAY}s)")
    print(f"  Hold winner to ${TP_PRICE}, SL the loser")
    print(f"  Bet sizing: {int(BET_PCT*100)}% of bankroll (split 50/50)")
    print()
    print(f"  Variants:")
    for key, offset in SL_OFFSETS.items():
        if key == "G5":
            print(f"    {key}: MAKER bids (mid-$0.02) + SL entry-${offset:.2f}")
        elif key == "G6":
            print(f"    {key}: DELAYED SL — wait for $0.65 winner, SL loser at bid-${offset:.2f}")
        elif key == "G7":
            print(f"    {key}: BUY BOTH, no SL until T+210, then SL loser at bid-${offset:.2f}")
        elif key == "G8":
            print(f"    {key}: SINGLE-SIDE — buy leader at T+210, SL entry-${offset:.2f}, hold to $1")
        elif offset > 0:
            print(f"    {key}: SL at entry - ${offset:.2f}")
        else:
            print(f"    {key}: No SL (hold both to resolution)")
    print()

    now = time.time()
    next_window = (int(now) // 300 + 1) * 300
    wait = next_window - now
    log_msg(f"[SYNC] Waiting {wait:.0f}s for window boundary...")
    await asyncio.sleep(wait)
    log_msg(f"[SYNC] Aligned. Starting bot.")

    bot = PaperBotG()
    await asyncio.gather(
        run_coinbase(bot),
        run_chainlink(bot),
        run_status(bot),
        bot.watch_windows(),
        bot.watch_g8(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
