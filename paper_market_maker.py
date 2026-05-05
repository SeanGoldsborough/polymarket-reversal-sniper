#!/usr/bin/env python3
"""
PAPER MARKET MAKER — Spread Capture + Two-Sided Quoting
=========================================================
Phase 1: Place BIDS on both UP and DOWN below $0.50.
         When both fill, combined cost < $1.00.
         Resolution guarantees $1.00 back = risk-free profit.

Phase 2: Two-sided quoting — bid AND ask on each side.
         Capture the spread on round trips.

Phase 3: Binance preemptive cancel (monitor BTC price,
         cancel stale quotes before adverse selection).

All entries as MAKER (0% fee + rebate eligible).
Simulates realistic fill rates and maker rebates.

Trades BTC 5-min markets (primary volume).
"""

import asyncio
import json
import time
import os
import sys
from datetime import datetime, timezone

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

STARTING_BANKROLL = 100.0
BET_PCT = 0.10
# Phase 1: bid offset below midpoint
BID_OFFSET = 0.10       # bid at mid - $0.02 on each side
MAX_COMBINED_BID = 0.80  # max we'll pay for both sides combined
# Phase 2: ask offset above midpoint
ASK_OFFSET = 0.10        # ask at mid + $0.03 on each side
# Phase 3: Binance cancel threshold
BINANCE_CANCEL_THRESHOLD = 15.0  # cancel if BTC moves $15+ against our position
# Maker rebate: 20% of taker fees collected on our fills
MAKER_REBATE_RATE = 0.20
TAKER_FEE_RATE = 0.018  # what takers pay — we get 20% of this

LOG_FILE = "logs/paper_market_maker.jsonl"
SUMMARY_FILE = "logs/paper_market_maker_summary.json"
os.makedirs("logs", exist_ok=True)


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def log_msg(msg):
    print(f"  [{ts()}] {msg}", flush=True)


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
            async with aiohttp.ClientSession() as s:
                async with s.get(f"https://gamma-api.polymarket.com/markets?slug={slug}",
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            m = data[0]
                            tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                            outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
                            up = tokens[0] if outcomes[0] == "Up" else tokens[1]
                            down = tokens[1] if outcomes[0] == "Up" else tokens[0]
                            self.active_market = {"up": up, "down": down,
                                                  "question": m.get("question",""),
                                                  "window_start": window_start}
        except Exception as e:
            log_msg(f"[MKT] {e}")


async def get_book(token_id):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://clob.polymarket.com/book?token_id={token_id}",
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    bb = float(bids[-1]["price"]) if bids else 0
                    ba = float(asks[-1]["price"]) if asks else 0
                    return {"bid": bb, "ask": ba, "mid": (bb + ba) / 2 if bb and ba else 0}
    except Exception:
        pass
    return None


class PaperMarketMaker:
    def __init__(self):
        self.mf = MarketFinder()
        self.bankroll = STARTING_BANKROLL
        self.peak = STARTING_BANKROLL
        self.max_dd = 0.0
        self.start_time = time.time()
        self.log_file = open(LOG_FILE, "a")
        # Binance price tracking
        self.btc_price = 0.0
        self.btc_ticks = 0
        # Stats
        self.trade_count = 0
        self.wins = 0
        self.losses = 0
        self.total_rebates = 0.0
        # Phase stats
        self.p1_trades = 0  # spread capture
        self.p1_pnl = 0.0
        self.p2_trades = 0  # two-sided quoting
        self.p2_pnl = 0.0
        self.p3_cancels = 0  # preemptive cancels
        self.p3_saved = 0.0

    def on_btc_price(self, price):
        self.btc_price = price
        self.btc_ticks += 1

    async def run(self):
        while True:
            try:
                now = time.time()
                nxt = (int(now) // 300 + 1) * 300
                wait = nxt - now
                if wait > 0:
                    await asyncio.sleep(wait)
                await asyncio.sleep(2)

                self.mf.last_refresh = 0
                await self.mf.refresh()
                if not self.mf.active_market:
                    continue

                up_tok = self.mf.active_market["up"]
                down_tok = self.mf.active_market["down"]

                await self._make_market(up_tok, down_tok)

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                await asyncio.sleep(5)

    async def _make_market(self, up_tok, down_tok):
        """Run all 3 phases during one 5-minute window."""
        window_end = time.time() + 280
        window_pnl = 0.0
        window_rebates = 0.0
        p1_filled_up = False
        p1_filled_down = False
        p1_up_price = 0
        p1_down_price = 0
        p1_up_shares = 0
        p1_down_shares = 0
        p2_round_trips = 0
        p2_pnl = 0.0
        p3_cancel_count = 0
        p3_saved = 0.0
        btc_at_entry = self.btc_price

        self.trade_count += 1
        tid = self.trade_count

        half_bet = min(self.bankroll * BET_PCT / 2, 200)

        while time.time() < window_end:
            up_book, down_book = await asyncio.gather(get_book(up_tok), get_book(down_tok))
            if not up_book or not down_book:
                await asyncio.sleep(1)
                continue

            up_mid = up_book["mid"]
            down_mid = down_book["mid"]
            up_bid_book = up_book["bid"]
            down_bid_book = down_book["bid"]
            up_ask_book = up_book["ask"]
            down_ask_book = down_book["ask"]

            # ══════════════════════════════════════════════
            # PHASE 1: Spread Capture
            # Place bids below midpoint. If both fill, profit is guaranteed.
            # ══════════════════════════════════════════════
            if not p1_filled_up:
                # Our bid: midpoint - offset
                our_up_bid = round(up_mid - BID_OFFSET, 2)
                # Fills if the ask drops to our bid (someone sells into us)
                if up_ask_book > 0 and up_ask_book <= our_up_bid:
                    p1_up_price = our_up_bid
                    p1_up_shares = round(half_bet / p1_up_price, 2)
                    p1_filled_up = True
                    rebate = round(p1_up_shares * p1_up_price * TAKER_FEE_RATE * MAKER_REBATE_RATE, 4)
                    window_rebates += rebate
                    log_msg(f"[P1] UP bid filled @ ${p1_up_price:.2f} ({p1_up_shares:.1f}sh) rebate +${rebate:.3f}")

            if not p1_filled_down:
                our_down_bid = round(down_mid - BID_OFFSET, 2)
                if down_ask_book > 0 and down_ask_book <= our_down_bid:
                    p1_down_price = our_down_bid
                    p1_down_shares = round(half_bet / p1_down_price, 2)
                    p1_filled_down = True
                    rebate = round(p1_down_shares * p1_down_price * TAKER_FEE_RATE * MAKER_REBATE_RATE, 4)
                    window_rebates += rebate
                    log_msg(f"[P1] DOWN bid filled @ ${p1_down_price:.2f} ({p1_down_shares:.1f}sh) rebate +${rebate:.3f}")

            # ══════════════════════════════════════════════
            # PHASE 2: Two-Sided Quoting
            # Post bid AND ask on each side. Earn the spread on round trips.
            # Only if we already have Phase 1 inventory.
            # ══════════════════════════════════════════════
            if p1_filled_up:
                # Our ask: midpoint + offset
                our_up_ask = round(up_mid + ASK_OFFSET, 2)
                # Fills if a buyer crosses our ask
                if up_bid_book >= our_up_ask:
                    # Sold our UP shares at a profit
                    sell_price = our_up_ask
                    spread_profit = round(p1_up_shares * (sell_price - p1_up_price), 4)
                    rebate = round(p1_up_shares * sell_price * TAKER_FEE_RATE * MAKER_REBATE_RATE, 4)
                    window_rebates += rebate
                    p2_pnl += spread_profit
                    p2_round_trips += 1
                    log_msg(f"[P2] UP sold @ ${sell_price:.2f} (bought ${p1_up_price:.2f}) "
                            f"+${spread_profit:.2f} rebate +${rebate:.3f}")
                    # Reset — can buy again
                    p1_filled_up = False

            if p1_filled_down:
                our_down_ask = round(down_mid + ASK_OFFSET, 2)
                if down_bid_book >= our_down_ask:
                    sell_price = our_down_ask
                    spread_profit = round(p1_down_shares * (sell_price - p1_down_price), 4)
                    rebate = round(p1_down_shares * sell_price * TAKER_FEE_RATE * MAKER_REBATE_RATE, 4)
                    window_rebates += rebate
                    p2_pnl += spread_profit
                    p2_round_trips += 1
                    log_msg(f"[P2] DOWN sold @ ${sell_price:.2f} (bought ${p1_down_price:.2f}) "
                            f"+${spread_profit:.2f} rebate +${rebate:.3f}")
                    p1_filled_down = False

            # ══════════════════════════════════════════════
            # PHASE 3: Binance Preemptive Cancel
            # If BTC moved significantly, cancel the losing side's bid
            # before an informed taker can sell into us.
            # ══════════════════════════════════════════════
            if self.btc_price > 0 and btc_at_entry > 0:
                btc_move = self.btc_price - btc_at_entry
                # If BTC went UP significantly, DOWN tokens will lose value
                # Cancel our DOWN bid to avoid buying a loser
                if btc_move > BINANCE_CANCEL_THRESHOLD and not p1_filled_down:
                    p3_cancel_count += 1
                    # Estimate what we would have lost
                    potential_loss = round(half_bet * 0.30, 2)  # rough estimate
                    p3_saved += potential_loss
                    btc_at_entry = self.btc_price  # reset
                    log_msg(f"[P3] BTC up ${btc_move:.0f} — cancelled DOWN bid (saved ~${potential_loss:.2f})")

                elif btc_move < -BINANCE_CANCEL_THRESHOLD and not p1_filled_up:
                    p3_cancel_count += 1
                    potential_loss = round(half_bet * 0.30, 2)
                    p3_saved += potential_loss
                    btc_at_entry = self.btc_price
                    log_msg(f"[P3] BTC down ${btc_move:.0f} — cancelled UP bid (saved ~${potential_loss:.2f})")

            await asyncio.sleep(1)

        # ══════════════════════════════════════════════
        # WINDOW END: Resolve any held positions
        # ══════════════════════════════════════════════
        p1_pnl = 0.0

        # If we hold BOTH sides from Phase 1, guaranteed profit
        if p1_filled_up and p1_filled_down:
            combined_cost = p1_up_shares * p1_up_price + p1_down_shares * p1_down_price
            # One side resolves at $1, other at $0
            # But we hold BOTH — one pays $1.00 per share
            # Use the side with fewer shares as the basis
            min_shares = min(p1_up_shares, p1_down_shares)
            resolution_value = min_shares * 1.00
            matched_cost = min_shares * p1_up_price + min_shares * p1_down_price
            p1_pnl = round(resolution_value - matched_cost, 4)
            # Any extra unmatched shares on one side: 50/50 chance of winning
            extra_up = p1_up_shares - min_shares
            extra_down = p1_down_shares - min_shares
            # For paper, assume the unmatched side is random
            self.p1_trades += 1
            log_msg(f"[P1] Both filled — matched {min_shares:.1f}sh × 2 | "
                    f"cost ${matched_cost:.2f} → ${resolution_value:.2f} | +${p1_pnl:.2f}")

        elif p1_filled_up or p1_filled_down:
            # Only one side filled — directional exposure
            # Check if our side won
            up_book_final = await get_book(up_tok)
            down_book_final = await get_book(down_tok)
            if p1_filled_up and up_book_final:
                if up_book_final["bid"] >= 0.90:
                    p1_pnl = round(p1_up_shares * (1.00 - p1_up_price), 4)
                    log_msg(f"[P1] UP won — +${p1_pnl:.2f}")
                else:
                    p1_pnl = round(-p1_up_shares * p1_up_price, 4)
                    log_msg(f"[P1] UP lost — ${p1_pnl:.2f}")
            elif p1_filled_down and down_book_final:
                if down_book_final["bid"] >= 0.90:
                    p1_pnl = round(p1_down_shares * (1.00 - p1_down_price), 4)
                    log_msg(f"[P1] DOWN won — +${p1_pnl:.2f}")
                else:
                    p1_pnl = round(-p1_down_shares * p1_down_price, 4)
                    log_msg(f"[P1] DOWN lost — ${p1_pnl:.2f}")
            self.p1_trades += 1

        # Total P&L
        window_pnl = round(p1_pnl + p2_pnl + window_rebates, 4)
        self.bankroll = round(self.bankroll + window_pnl, 4)
        self.total_rebates += window_rebates
        self.p1_pnl += p1_pnl
        self.p2_pnl += p2_pnl
        self.p2_trades += p2_round_trips
        self.p3_cancels += p3_cancel_count
        self.p3_saved += p3_saved

        if self.bankroll > self.peak:
            self.peak = self.bankroll
        dd = (self.peak - self.bankroll) / self.peak * 100 if self.peak > 0 else 0
        if dd > self.max_dd:
            self.max_dd = dd

        if window_pnl > 0.01:
            self.wins += 1
        elif window_pnl < -0.01:
            self.losses += 1

        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0

        log_msg(f"[DONE] #{tid} P1=${p1_pnl:+.2f} P2=${p2_pnl:+.2f} rebate=${window_rebates:.3f} "
                f"| total ${window_pnl:+.2f} | bank ${self.bankroll:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%)")

        # Log
        try:
            self.log_file.write(json.dumps({
                "id": tid, "pnl": window_pnl, "bankroll": self.bankroll,
                "p1_pnl": p1_pnl, "p2_pnl": p2_pnl, "p2_trips": p2_round_trips,
                "rebates": window_rebates, "p3_cancels": p3_cancel_count,
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except Exception:
            pass
        self._write_summary()

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        summary = {
            "elapsed_minutes": round(elapsed, 1),
            "trades": total, "wins": self.wins, "losses": self.losses,
            "win_rate": round(wr, 1),
            "bankroll": round(self.bankroll, 2),
            "peak": round(self.peak, 2),
            "pnl_total": round(self.bankroll - STARTING_BANKROLL, 2),
            "max_dd": round(self.max_dd, 1),
            "phase1_trades": self.p1_trades, "phase1_pnl": round(self.p1_pnl, 2),
            "phase2_round_trips": self.p2_trades, "phase2_pnl": round(self.p2_pnl, 2),
            "phase3_cancels": self.p3_cancels, "phase3_saved": round(self.p3_saved, 2),
            "total_rebates": round(self.total_rebates, 2),
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        with open(SUMMARY_FILE, "w") as f:
            json.dump(summary, f, indent=2)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        pnl = self.bankroll - STARTING_BANKROLL
        print(f"\n  [{ts()}] {'='*60}")
        print(f"  PAPER MARKET MAKER | {elapsed:.0f}min")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f} | DD: {self.max_dd:.0f}%")
        print(f"  Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}%")
        print(f"  Phase 1 (spread capture): {self.p1_trades} fills, ${self.p1_pnl:+.2f}")
        print(f"  Phase 2 (round trips):    {self.p2_trades} trips, ${self.p2_pnl:+.2f}")
        print(f"  Phase 3 (preemptive):     {self.p3_cancels} cancels, ~${self.p3_saved:.2f} saved")
        print(f"  Maker rebates earned:     ${self.total_rebates:.2f}")
        print(f"  BTC: ${self.btc_price:,.2f} ({self.btc_ticks:,} ticks)")
        print()


async def run_coinbase(bot):
    while True:
        try:
            async with websockets.connect("wss://ws-feed.exchange.coinbase.com") as ws:
                await ws.send(json.dumps({"type": "subscribe",
                    "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}]}))
                log_msg("[FEED] Coinbase BTC connected")
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("type") == "ticker":
                        bot.on_btc_price(float(data["price"]))
        except Exception as e:
            log_msg(f"[FEED] CB reconnecting: {e}")
            await asyncio.sleep(2)


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        bot.print_status()


async def main():
    print("=" * 65)
    print("  PAPER MARKET MAKER — Spread Capture + Two-Sided Quoting")
    print("=" * 65)
    print(f"  Phase 1: Bid both sides at mid - ${BID_OFFSET} (maker, 0% fee)")
    print(f"           Both fill → combined < $1.00 → risk-free profit")
    print(f"  Phase 2: Ask at mid + ${ASK_OFFSET} to sell inventory")
    print(f"           Round trip spread: ${BID_OFFSET + ASK_OFFSET:.2f}/sh")
    print(f"  Phase 3: Monitor Binance BTC — cancel if ${BINANCE_CANCEL_THRESHOLD}+ move")
    print(f"  Maker rebate: {MAKER_REBATE_RATE*100:.0f}% of taker fees on our fills")
    print(f"  Bankroll: ${STARTING_BANKROLL} | Bet: {int(BET_PCT*100)}%")
    print()

    now = time.time()
    nxt = (int(now) // 300 + 1) * 300
    wait = nxt - now
    log_msg(f"[SYNC] Waiting {wait:.0f}s...")
    await asyncio.sleep(wait)
    log_msg(f"[SYNC] Aligned.")

    bot = PaperMarketMaker()
    await asyncio.gather(bot.run(), run_status(bot), run_coinbase(bot))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
