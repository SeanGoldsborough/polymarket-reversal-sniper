#!/usr/bin/env python3
"""
PAPER SOL OMEGA — Solana 5-min Both Sides
==========================================
Strategy:
  - Target the UPCOMING window (place orders before it opens)
  - Limit buy BOTH sides at $0.52
  - Stop loss BOTH sides at $0.19
  - One side stops, other wins to $1.00

Math per window:
  Winner: $1.00 - $0.52 = +$0.48
  Loser SL: $0.19 - $0.52 = -$0.33
  Net: +$0.15 per window (if 1 win + 1 SL)
  Both SL: -$0.66 (if oscillation stops both)

Bet sizing: 10% of bankroll split 50/50.
"""

import asyncio
import json
import time
import os
import sys
from datetime import datetime, timezone

try:
    import aiohttp
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "aiohttp", "-q"])
    import aiohttp

STARTING_BANKROLL = 100.0
BET_PCT = 0.10
LIMIT_ENTRY = 0.52
SL_PRICE = 0.19
MAX_COMBINED_ASK = 1.08
MIN_SIDE_ASK = 0.40
MAX_SIDE_ASK = 0.55

LOG_FILE = "logs/paper_sol_omega.jsonl"
SUMMARY_FILE = "logs/paper_sol_omega_summary.json"
os.makedirs("logs", exist_ok=True)


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def log_msg(msg):
    print(f"  [{ts()}] {msg}", flush=True)


class MarketFinder:
    def __init__(self):
        self.active_market = None
        self.last_refresh = 0.0

    async def refresh(self, offset=0):
        """Find the market. offset=0 for current, offset=300 for next window."""
        self.last_refresh = time.time()
        try:
            now = int(time.time())
            window_start = (now // 300) * 300 + offset
            slug = f"sol-updown-5m-{window_start}"
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
                            self.active_market = {
                                "up": up, "down": down,
                                "question": m.get("question", ""),
                                "window_start": window_start,
                            }
                            return True
        except Exception as e:
            log_msg(f"[MKT] {e}")
        return False


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
                    return {"bid": bb, "ask": ba}
    except Exception:
        pass
    return None


class PaperSolOmega:
    def __init__(self):
        self.mf = MarketFinder()
        self.bankroll = STARTING_BANKROLL
        self.peak = STARTING_BANKROLL
        self.max_dd = 0
        self.wins = 0
        self.losses = 0
        self.flat = 0
        self.trade_count = 0
        self.start_time = time.time()
        self.log_file = open(LOG_FILE, "a")
        # Track per-window details
        self.both_win = 0
        self.one_win_one_sl = 0
        self.both_sl = 0
        self.unfilled = 0

    async def run(self):
        while True:
            try:
                # Wait for next window boundary
                now = time.time()
                nxt = (int(now) // 300 + 1) * 300
                wait = nxt - now

                # Try to find the UPCOMING window's market 30s before it opens
                if wait > 35:
                    await asyncio.sleep(wait - 30)

                # Try to get the upcoming window
                found = await self.mf.refresh(offset=300)
                if not found:
                    # Try current window instead
                    found = await self.mf.refresh(offset=0)
                if not found:
                    log_msg(f"[MKT] No SOL market found — waiting")
                    await asyncio.sleep(30)
                    continue

                # Wait for window to actually open
                now = time.time()
                nxt = (int(now) // 300 + 1) * 300
                wait = nxt - now
                if wait > 0 and wait < 35:
                    await asyncio.sleep(wait)
                await asyncio.sleep(2)  # let market settle

                # Re-find current window market
                await self.mf.refresh(offset=0)
                if not self.mf.active_market:
                    continue

                up_tok = self.mf.active_market["up"]
                down_tok = self.mf.active_market["down"]
                question = self.mf.active_market["question"]

                await self._trade_window(up_tok, down_tok, question)

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                await asyncio.sleep(5)

    async def _trade_window(self, up_tok, down_tok, question):
        # Read books
        up_book, down_book = await asyncio.gather(get_book(up_tok), get_book(down_tok))
        if not up_book or not down_book:
            return

        up_ask = up_book["ask"]
        down_ask = down_book["ask"]

        # Buy both at their ask price if combined <= $1.04
        # SOL markets aren't always balanced — one side can be above $0.52
        combined = up_ask + down_ask
        if combined > MAX_COMBINED_ASK:
            self.unfilled += 1
            log_msg(f"[SKIP] Combined ${combined:.2f} > ${MAX_COMBINED_ASK}: UP=${up_ask:.2f} DOWN=${down_ask:.2f}")
            return

        if up_ask <= 0 or down_ask <= 0:
            return

        # Maker limit bids: bid at midpoint (0% fee)
        up_mid = round((up_book["bid"] + up_ask) / 2, 2) if up_book["bid"] > 0 else up_ask - 0.01
        down_mid = round((down_book["bid"] + down_ask) / 2, 2) if down_book["bid"] > 0 else down_ask - 0.01
        up_entry = min(up_mid, MAX_SIDE_ASK)
        down_entry = min(down_mid, MAX_SIDE_ASK)

        # Skip if combined too expensive
        if up_entry + down_entry > MAX_COMBINED_ASK:
            return

        half = min(self.bankroll * BET_PCT / 2, 200)
        up_sh = round(half / up_entry, 2)
        down_sh = round(half / down_entry, 2)
        if up_sh < 1 or down_sh < 1:
            return

        # MAKER entry: 0% fee (limit bids, not market buys)
        total_cost = round(up_sh * up_entry + down_sh * down_entry, 4)
        self.trade_count += 1
        tid = self.trade_count

        log_msg(f"[BID] #{tid} SOL | UP ${up_entry:.2f} ({up_sh}sh) DOWN ${down_entry:.2f} ({down_sh}sh) "
                f"| SL ${SL_PRICE} | cost ${total_cost:.2f} | MAKER 0% fee")

        # Monitor every second for SL or TP
        up_done = False
        down_done = False
        up_pnl = 0
        down_pnl = 0
        up_result = "pending"
        down_result = "pending"
        window_end = time.time() + 280

        while time.time() < window_end:
            up_book, down_book = await asyncio.gather(get_book(up_tok), get_book(down_tok))

            # UP side
            if not up_done and up_book:
                bid = up_book["bid"]
                if bid >= 0.99:
                    up_pnl = round(up_sh * (1.00 - up_entry), 4)
                    up_done = True
                    up_result = "WIN"
                    log_msg(f"[WIN] #{tid} UP resolved @ $1.00 | +${up_pnl:.2f}")
                elif bid <= SL_PRICE:
                    sl_fee = round(up_sh * SL_PRICE * 0.018, 4)
                    up_pnl = round(up_sh * (SL_PRICE - up_entry) - sl_fee, 4)
                    up_done = True
                    up_result = "SL"
                    log_msg(f"[SL] #{tid} UP stopped @ ${SL_PRICE} | ${up_pnl:.2f} (fee ${sl_fee:.2f})")

            # DOWN side
            if not down_done and down_book:
                bid = down_book["bid"]
                if bid >= 0.99:
                    down_pnl = round(down_sh * (1.00 - down_entry), 4)
                    down_done = True
                    down_result = "WIN"
                    log_msg(f"[WIN] #{tid} DOWN resolved @ $1.00 | +${down_pnl:.2f}")
                elif bid <= SL_PRICE:
                    sl_fee = round(down_sh * SL_PRICE * 0.018, 4)
                    down_pnl = round(down_sh * (SL_PRICE - down_entry) - sl_fee, 4)
                    down_done = True
                    down_result = "SL"
                    log_msg(f"[SL] #{tid} DOWN stopped @ ${SL_PRICE} | ${down_pnl:.2f} (fee ${sl_fee:.2f})")

            if up_done and down_done:
                break

            await asyncio.sleep(1)

        # Handle any unresolved sides at window end.
        # In a binary market, ONE side ALWAYS resolves to .00.
        # Wait up to 60s for resolution, then use final bids to determine winner.
        if not up_done or not down_done:
            for _wait in range(60):
                up_book, down_book = await asyncio.gather(get_book(up_tok), get_book(down_tok))
                if not up_done and up_book and up_book["bid"] >= 0.95:
                    up_pnl = round(up_sh * (1.00 - up_entry), 4)
                    up_done = True
                    up_result = "WIN"
                    log_msg(f"[WIN] #{tid} UP resolved late @ .00 | +${up_pnl:.2f}")
                if not down_done and down_book and down_book["bid"] >= 0.95:
                    down_pnl = round(down_sh * (1.00 - down_entry), 4)
                    down_done = True
                    down_result = "WIN"
                    log_msg(f"[WIN] #{tid} DOWN resolved late @ .00 | +${down_pnl:.2f}")
                if up_done and down_done:
                    break
                await asyncio.sleep(1)

        # If still not resolved, the higher bid side is the winner
        if not up_done or not down_done:
            up_book = await get_book(up_tok)
            down_book = await get_book(down_tok)
            up_bid = up_book["bid"] if up_book else 0
            down_bid = down_book["bid"] if down_book else 0

            if not up_done:
                if up_bid > down_bid or (up_bid > 0.5 and down_done and "SL" in down_result):
                    up_pnl = round(up_sh * (1.00 - up_entry), 4)
                    up_result = "WIN-LATE"
                    log_msg(f"[WIN-LATE] #{tid} UP bid ${up_bid:.2f} > DOWN ${down_bid:.2f} | +${up_pnl:.2f}")
                else:
                    # Loser side — resolves to /bin/zsh
                    up_pnl = round(-up_sh * up_entry, 4)
                    up_result = "LOSS"
                    log_msg(f"[LOSS] #{tid} UP resolves /bin/zsh | ${up_pnl:.2f}")
                up_done = True

            if not down_done:
                if down_bid > up_bid or (down_bid > 0.5 and up_done and "SL" in up_result):
                    down_pnl = round(down_sh * (1.00 - down_entry), 4)
                    down_result = "WIN-LATE"
                    log_msg(f"[WIN-LATE] #{tid} DOWN bid ${down_bid:.2f} > UP ${up_bid:.2f} | +${down_pnl:.2f}")
                else:
                    down_pnl = round(-down_sh * down_entry, 4)
                    down_result = "LOSS"
                    log_msg(f"[LOSS] #{tid} DOWN resolves /bin/zsh | ${down_pnl:.2f}")
                down_done = True

        # P&L
        window_pnl = round(up_pnl + down_pnl, 4)
        self.bankroll = round(self.bankroll + window_pnl, 4)
        if self.bankroll > self.peak:
            self.peak = self.bankroll
        dd = (self.peak - self.bankroll) / self.peak * 100 if self.peak > 0 else 0
        if dd > self.max_dd:
            self.max_dd = dd

        if window_pnl > 0.01:
            self.wins += 1
        elif window_pnl < -0.01:
            self.losses += 1
        else:
            self.flat += 1

        # Categorize
        if "WIN" in up_result and "WIN" in down_result:
            self.both_win += 1
        elif ("WIN" in up_result and "SL" in down_result) or ("SL" in up_result and "WIN" in down_result):
            self.one_win_one_sl += 1
        elif "SL" in up_result and "SL" in down_result:
            self.both_sl += 1

        total = self.wins + self.losses + self.flat
        wr = self.wins / total * 100 if total else 0

        log_msg(f"[DONE] #{tid} UP={up_result} DOWN={down_result} | "
                f"P&L ${window_pnl:+.2f} | bank ${self.bankroll:.2f} | "
                f"{self.wins}W/{self.losses}L ({wr:.0f}%)")

        # Log
        try:
            self.log_file.write(json.dumps({
                "id": tid, "pnl": window_pnl, "bankroll": self.bankroll,
                "up_entry": up_entry, "down_entry": down_entry,
                "up_result": up_result, "down_result": down_result,
                "up_pnl": up_pnl, "down_pnl": down_pnl,
                "question": question,
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except Exception:
            pass
        self._write_summary()

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses + self.flat
        wr = self.wins / total * 100 if total else 0
        summary = {
            "elapsed_minutes": round(elapsed, 1),
            "trades": total, "wins": self.wins, "losses": self.losses,
            "win_rate": round(wr, 1),
            "bankroll": round(self.bankroll, 2),
            "peak": round(self.peak, 2),
            "pnl_total": round(self.bankroll - STARTING_BANKROLL, 2),
            "max_dd": round(self.max_dd, 1),
            "both_win": self.both_win,
            "one_win_one_sl": self.one_win_one_sl,
            "both_sl": self.both_sl,
            "unfilled": self.unfilled,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        with open(SUMMARY_FILE, "w") as f:
            json.dump(summary, f, indent=2)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses + self.flat
        wr = self.wins / total * 100 if total else 0
        pnl = self.bankroll - STARTING_BANKROLL
        bet = min(self.bankroll * BET_PCT / 2, 200)
        print(f"\n  [{ts()}] {'='*60}")
        print(f"  PAPER SOL OMEGA | {elapsed:.0f}min")
        print(f"  Entry: limit buy both @ ${LIMIT_ENTRY} | SL @ ${SL_PRICE}")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f} | DD: {self.max_dd:.0f}%")
        print(f"  Bet: ${bet:.2f}/side | Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}%")
        print(f"  Outcomes: 1W+1SL={self.one_win_one_sl} | 2SL={self.both_sl} | 2W={self.both_win} | Unfilled={self.unfilled}")
        print()


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        bot.print_status()


async def main():
    print("=" * 65)
    print("  PAPER SOL OMEGA — Solana 5-min Both Sides")
    print("=" * 65)
    print(f"  Limit buy both sides @ ${LIMIT_ENTRY}")
    print(f"  Stop loss @ ${SL_PRICE}")
    print(f"  Win: +${1.00 - LIMIT_ENTRY:.2f}/sh | SL loss: -${LIMIT_ENTRY - SL_PRICE:.2f}/sh")
    print(f"  Net per 1W+1SL window: +${(1.00 - LIMIT_ENTRY) - (LIMIT_ENTRY - SL_PRICE):.2f}/sh")
    print(f"  Bankroll: ${STARTING_BANKROLL} | Bet: {int(BET_PCT*100)}%")
    print()

    now = time.time()
    nxt = (int(now) // 300 + 1) * 300
    wait = nxt - now
    bot = PaperSolOmega()
    bot._write_summary()  # Write initial summary so dashboard shows us immediately
    log_msg(f"[SYNC] Waiting {wait:.0f}s for window boundary...")
    await asyncio.sleep(wait)
    log_msg(f"[SYNC] Aligned.")

    await asyncio.gather(bot.run(), run_status(bot))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
