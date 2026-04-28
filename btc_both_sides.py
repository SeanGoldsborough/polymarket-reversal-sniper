#!/usr/bin/env python3
"""
BTC-BOTH-SIDES — Buy Both Sides Below $0.50 on BTC 5-Min Up/Down
===================================================================
Strategy:
  - Monitor BTC Up/Down order book throughout the window
  - Place GTC maker bids on BOTH sides at target prices ($0.40-$0.45)
  - If both sides fill, guaranteed profit: $1.00 - total_cost
  - If only one side fills, hold to resolution (risky but often wins)
  - Cancel unfilled orders at T-30

Realistic paper simulation:
  - Reads REAL order book every 5 seconds
  - Only counts fills when ask depth exists at our bid price
  - Tracks exact fill prices and times
  - No simulated random fills

Entry: T+30 to T-30 (continuous monitoring)
Execution: GTC maker limit orders (0% fee)
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

try:
    from dotenv import load_dotenv
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-dotenv", "-q"])
    from dotenv import load_dotenv

load_dotenv()

CLOB_AVAILABLE = False
try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import OrderArgs, OrderType
    from py_clob_client_v2.order_builder.constants import BUY
    CLOB_AVAILABLE = True
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────
# Target buy price — only buy when ask dips to this
TARGET_PRICE = 0.47                        # Buy each side at $0.47
MAX_OTHER_SIDE_ASK = 0.60                  # Only buy if other side's ask is also < this (market undecided)
BET_PER_SIDE = 5.00                        # $5 per side
MAX_COMBINED_COST = 0.96                   # Max combined cost per share pair ($0.48 + $0.48)

SCAN_INTERVAL = 5                          # Check book every 5 seconds
ENTRY_START = 30                           # Start scanning at T+30
ENTRY_END = 30                             # Stop scanning at T-30

USDC_CONTRACT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PAPER_MODE = True  # Always paper for now

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.makedirs("logs", exist_ok=True)
BOT_NAME = "BTC-BOTH-SIDES-PAPER"
LOG_FILE = "logs/btc_both_sides_trades.jsonl"
SUMMARY_FILE = "logs/btc_both_sides_summary.json"


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def log_msg(msg):
    print(f"  [{ts()}] {msg}", flush=True)

def snap_price(price):
    return round(max(0.01, min(0.99, round(price * 100) / 100)), 2)

def atomic_write_json(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


async def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        async with aiohttp.ClientSession() as s:
            async with s.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                              data=data, headers={"Content-Type": "application/json"},
                              timeout=aiohttp.ClientTimeout(total=10)):
                pass
    except Exception:
        pass


class MarketFinder:
    def __init__(self):
        self.market = None

    async def refresh(self):
        try:
            now = int(time.time())
            window_start = (now // 300) * 300
            slug = f"btc-updown-5m-{window_start}"
            async with aiohttp.ClientSession() as s:
                async with s.get(f"https://gamma-api.polymarket.com/markets?slug={slug}",
                                 headers={"User-Agent": "Mozilla/5.0"},
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            m = data[0]
                            tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                            outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
                            up = tokens[0] if outcomes[0] == "Up" else tokens[1]
                            down = tokens[1] if outcomes[0] == "Up" else tokens[0]
                            self.market = {
                                "up": up, "down": down,
                                "question": m.get("question", ""),
                                "window_start": window_start,
                            }
        except Exception as e:
            log_msg(f"[MKT] BTC: {e}")


async def get_full_book(token_id):
    """Get full order book with all price levels."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://clob.polymarket.com/book?token_id={token_id}",
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    bids = [(float(b["price"]), float(b["size"])) for b in data.get("bids", [])]
                    asks = [(float(a["price"]), float(a["size"])) for a in data.get("asks", [])]
                    best_bid = max((p for p, s in bids), default=0)
                    best_ask = min((p for p, s in asks), default=0)
                    return {
                        "bid": best_bid, "ask": best_ask,
                        "bids": sorted(bids, reverse=True),
                        "asks": sorted(asks),
                    }
    except Exception:
        pass
    return None


class BTCBothSidesBot:
    def __init__(self):
        self.mf = MarketFinder()
        self.bankroll = 100.0
        self.starting_bankroll = 100.0
        self.peak = 100.0
        self.max_dd = 0
        self.wins = 0
        self.losses = 0
        self.both_filled = 0  # Trades where both sides filled
        self.one_filled = 0   # Trades where only one side filled
        self.trade_count = 0
        self.start_time = time.time()
        self.log_file = open(LOG_FILE, "a")

    async def run(self):
        while True:
            try:
                # Wait for next window
                now = time.time()
                nxt = (int(now) // 300 + 1) * 300
                wait = nxt - now
                if wait > 0:
                    await asyncio.sleep(wait)
                await asyncio.sleep(2)

                await self.mf.refresh()
                if not self.mf.market:
                    log_msg("[LOOP] No market found")
                    continue

                log_msg(f"[LOOP] Window start | Bank: ${self.bankroll:.2f} | {self.mf.market['question'][:40]}")

                if self.bankroll < 5:
                    log_msg(f"[RISK] Bankroll ${self.bankroll:.2f} too low")
                    continue

                await self._scan_and_trade()

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(5)

    async def _scan_and_trade(self):
        mkt = self.mf.market
        if not mkt:
            return

        window_start = (int(time.time()) // 300) * 300
        scan_start = window_start + ENTRY_START
        scan_end = window_start + 300 - ENTRY_END
        window_end = window_start + 300

        # Wait until T+30
        now = time.time()
        if now < scan_start:
            await asyncio.sleep(scan_start - now)

        self.trade_count += 1
        tid = self.trade_count

        # Track fills for each side
        up_fills = []   # List of (price, shares, timestamp)
        down_fills = []
        up_total_cost = 0
        down_total_cost = 0
        up_total_shares = 0
        down_total_shares = 0

        # Book snapshots for analysis
        book_history = []

        log_msg(f"[SCAN] #{tid} Monitoring both sides from T+{ENTRY_START} to T-{ENTRY_END}")

        # Scan the book continuously
        while time.time() < scan_end:
            up_book = await get_full_book(mkt["up"])
            down_book = await get_full_book(mkt["down"])

            if not up_book or not down_book:
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            now = time.time()
            elapsed = now - window_start
            remaining = window_end - now

            # Record book snapshot
            book_history.append({
                "t": round(elapsed, 1),
                "up_bid": up_book["bid"],
                "up_ask": up_book["ask"],
                "down_bid": down_book["bid"],
                "down_ask": down_book["ask"],
            })

            up_ask = up_book["ask"]
            down_ask = down_book["ask"]

            # Only buy UP if:
            # 1. UP ask <= target price
            # 2. DOWN ask is also reasonable (market still undecided)
            # 3. Haven't already filled UP
            if up_total_shares == 0 and up_ask <= TARGET_PRICE and up_ask > 0.01:
                if down_ask <= MAX_OTHER_SIDE_ASK:
                    available = sum(size for price, size in up_book["asks"] if price <= TARGET_PRICE)
                    if available >= 5:
                        shares = round(BET_PER_SIDE / TARGET_PRICE)
                        if shares < 5:
                            shares = 5
                        actual_shares = min(shares, int(available))
                        cost = actual_shares * up_ask
                        up_fills.append((up_ask, actual_shares, time.time()))
                        up_total_cost += cost
                        up_total_shares += actual_shares
                        log_msg(f"[FILL-UP] #{tid} {actual_shares}sh @ ${up_ask:.2f} (${cost:.2f}) | "
                                f"DOWN ask=${down_ask:.2f} (undecided) | T+{elapsed:.0f}s")

            # Only buy DOWN if:
            # 1. DOWN ask <= target price
            # 2. UP ask is also reasonable (market still undecided)
            # 3. Haven't already filled DOWN
            if down_total_shares == 0 and down_ask <= TARGET_PRICE and down_ask > 0.01:
                if up_ask <= MAX_OTHER_SIDE_ASK:
                    available = sum(size for price, size in down_book["asks"] if price <= TARGET_PRICE)
                    if available >= 5:
                        shares = round(BET_PER_SIDE / TARGET_PRICE)
                        if shares < 5:
                            shares = 5
                        actual_shares = min(shares, int(available))
                        cost = actual_shares * down_ask
                        down_fills.append((down_ask, actual_shares, time.time()))
                        down_total_cost += cost
                        down_total_shares += actual_shares
                        log_msg(f"[FILL-DN] #{tid} {actual_shares}sh @ ${down_ask:.2f} (${cost:.2f}) | "
                                f"UP ask=${up_ask:.2f} (undecided) | T+{elapsed:.0f}s")

            # If both filled, we're done — guaranteed profit
            if up_total_shares > 0 and down_total_shares > 0:
                avg_up = up_total_cost / up_total_shares
                avg_down = down_total_cost / down_total_shares
                combined = avg_up + avg_down
                log_msg(f"[BOTH] #{tid} UP ${avg_up:.3f} + DOWN ${avg_down:.3f} = ${combined:.3f} | "
                        f"Guaranteed profit: ${1.00 - combined:.3f}/pair")
                break

            # Log status every 30 seconds
            if int(elapsed) % 30 == 0 and int(elapsed) > 0:
                log_msg(f"[WATCH] #{tid} T+{elapsed:.0f}s | UP ask=${up_ask:.2f} DN ask=${down_ask:.2f} | "
                        f"fills: UP={up_total_shares} DN={down_total_shares}")

            await asyncio.sleep(SCAN_INTERVAL)

        # ── RESOLUTION ──
        total_cost = up_total_cost + down_total_cost
        total_shares_up = up_total_shares
        total_shares_down = down_total_shares

        if total_shares_up == 0 and total_shares_down == 0:
            log_msg(f"[SKIP] #{tid} No fills — neither side reached target prices")
            # Log book history for analysis
            if book_history:
                min_up_ask = min(s["up_ask"] for s in book_history if s["up_ask"] > 0)
                min_down_ask = min(s["down_ask"] for s in book_history if s["down_ask"] > 0)
                log_msg(f"[DATA] #{tid} Lowest asks seen: UP ${min_up_ask:.2f} DOWN ${min_down_ask:.2f}")
            return

        both_sides = total_shares_up > 0 and total_shares_down > 0

        if both_sides:
            self.both_filled += 1
            log_msg(f"[BOTH-FILLED] #{tid} UP: {total_shares_up}sh (${up_total_cost:.2f}) + "
                    f"DOWN: {total_shares_down}sh (${down_total_cost:.2f}) = ${total_cost:.2f}")
        else:
            self.one_filled += 1
            side = "UP" if total_shares_up > 0 else "DOWN"
            log_msg(f"[ONE-FILLED] #{tid} Only {side} filled — holding to resolution")

        # Wait for resolution
        for _ in range(90):
            up_book = await get_full_book(mkt["up"])
            down_book = await get_full_book(mkt["down"])
            if up_book and down_book:
                if up_book["bid"] >= 0.95:
                    # UP won
                    up_pnl = total_shares_up * (1.00) - up_total_cost if total_shares_up > 0 else 0
                    down_pnl = -down_total_cost  # DOWN resolves to $0
                    pnl = round(up_pnl + down_pnl, 4)
                    result = "WIN-BOTH" if both_sides else ("WIN" if total_shares_up > 0 else "LOSS")
                    self._record_trade(tid, pnl, result, total_cost, up_total_cost, down_total_cost,
                                       total_shares_up, total_shares_down, "UP", mkt["question"],
                                       both_sides, book_history)
                    return
                if down_book["bid"] >= 0.95:
                    # DOWN won
                    down_pnl = total_shares_down * (1.00) - down_total_cost if total_shares_down > 0 else 0
                    up_pnl = -up_total_cost  # UP resolves to $0
                    pnl = round(up_pnl + down_pnl, 4)
                    result = "WIN-BOTH" if both_sides else ("WIN" if total_shares_down > 0 else "LOSS")
                    self._record_trade(tid, pnl, result, total_cost, up_total_cost, down_total_cost,
                                       total_shares_up, total_shares_down, "DOWN", mkt["question"],
                                       both_sides, book_history)
                    return
            await asyncio.sleep(1)

        # Extended wait
        for _ in range(120):
            up_book = await get_full_book(mkt["up"])
            if up_book:
                if up_book["bid"] >= 0.95:
                    up_pnl = total_shares_up * 1.00 - up_total_cost if total_shares_up > 0 else 0
                    down_pnl = -down_total_cost
                    pnl = round(up_pnl + down_pnl, 4)
                    result = "WIN-BOTH" if both_sides else ("WIN" if total_shares_up > 0 else "LOSS")
                    self._record_trade(tid, pnl, result, total_cost, up_total_cost, down_total_cost,
                                       total_shares_up, total_shares_down, "UP-LATE", mkt["question"],
                                       both_sides, book_history)
                    return
                if up_book["bid"] <= 0.05:
                    down_pnl = total_shares_down * 1.00 - down_total_cost if total_shares_down > 0 else 0
                    up_pnl = -up_total_cost
                    pnl = round(up_pnl + down_pnl, 4)
                    result = "WIN-BOTH" if both_sides else ("WIN" if total_shares_down > 0 else "LOSS")
                    self._record_trade(tid, pnl, result, total_cost, up_total_cost, down_total_cost,
                                       total_shares_up, total_shares_down, "DOWN-LATE", mkt["question"],
                                       both_sides, book_history)
                    return
            await asyncio.sleep(1)

        # Timeout fallback
        log_msg(f"[TIMEOUT] #{tid} Ambiguous — recording as LOSS")
        pnl = round(-total_cost, 4)
        self._record_trade(tid, pnl, "LOSS-TIMEOUT", total_cost, up_total_cost, down_total_cost,
                           total_shares_up, total_shares_down, "?", mkt["question"],
                           both_sides, book_history)

    def _record_trade(self, tid, pnl, result, total_cost, up_cost, down_cost,
                       up_shares, down_shares, winner, question, both_sides, book_history):
        self.bankroll = round(self.bankroll + pnl, 4)
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1

        if self.bankroll > self.peak:
            self.peak = self.bankroll
        dd = (self.peak - self.bankroll) / self.peak * 100 if self.peak > 0 else 0
        if dd > self.max_dd:
            self.max_dd = dd

        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        ret_pct = round(pnl / total_cost * 100, 1) if total_cost > 0 else 0

        # Min asks seen during the window
        min_up = min((s["up_ask"] for s in book_history if s["up_ask"] > 0), default=0)
        min_down = min((s["down_ask"] for s in book_history if s["down_ask"] > 0), default=0)

        log_msg(f"[{result}] #{tid} | P&L ${pnl:+.2f} ({ret_pct:+.1f}%) | "
                f"UP: {up_shares}sh ${up_cost:.2f} | DOWN: {down_shares}sh ${down_cost:.2f} | "
                f"winner={winner} | both={both_sides} | bank ${self.bankroll:.2f} | "
                f"{self.wins}W/{self.losses}L ({wr:.0f}%)")

        try:
            self.log_file.write(json.dumps({
                "id": tid, "pnl": pnl, "return_pct": ret_pct, "result": result,
                "total_cost": round(total_cost, 4),
                "up_cost": round(up_cost, 4), "down_cost": round(down_cost, 4),
                "up_shares": up_shares, "down_shares": down_shares,
                "both_sides": both_sides, "winner": winner,
                "bankroll": self.bankroll,
                "min_up_ask": min_up, "min_down_ask": min_down,
                "book_snapshots": len(book_history),
                "question": question,
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except Exception:
            pass

        self._write_summary()

        icon = "🟢" if pnl > 0 else "🔴"
        both_tag = " ✅BOTH" if both_sides else " ⚠️ONE"
        asyncio.create_task(send_telegram(
            f"{icon}{both_tag} <b>{BOT_NAME} #{tid}</b>\n"
            f"{result} | P&L ${pnl:+.2f} ({ret_pct:+.0f}%)\n"
            f"UP: {up_shares}sh ${up_cost:.2f} | DOWN: {down_shares}sh ${down_cost:.2f}\n"
            f"Winner: {winner} | Min asks: UP ${min_up:.2f} DN ${min_down:.2f}\n"
            f"Bank: ${self.bankroll:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%)"))

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        summary = {
            "bot": BOT_NAME, "mode": "PAPER",
            "elapsed_minutes": round(elapsed, 1),
            "trades": total, "wins": self.wins, "losses": self.losses,
            "win_rate": round(wr, 1),
            "bankroll": round(self.bankroll, 2),
            "starting_bankroll": self.starting_bankroll,
            "pnl_total": round(self.bankroll - self.starting_bankroll, 2),
            "max_dd": round(self.max_dd, 1),
            "both_filled": self.both_filled,
            "one_filled": self.one_filled,
            "target_price": TARGET_PRICE,
            "max_other_side_ask": MAX_OTHER_SIDE_ASK,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(SUMMARY_FILE, summary)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        pnl = self.bankroll - self.starting_bankroll

        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {BOT_NAME} | {elapsed:.0f}min | PAPER")
        print(f"  Strategy: Buy BOTH sides at ${TARGET_PRICE} when market undecided")
        print(f"  Target: ${TARGET_PRICE} per side | Only if other side ask < ${MAX_OTHER_SIDE_ASK}")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f}")
        print(f"  Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}%")
        print(f"  Both-filled: {self.both_filled} | One-filled: {self.one_filled}")
        print()


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        bot.print_status()


async def main():
    print("=" * 65)
    print(f"  {BOT_NAME} — Buy Both Sides Below ${max(TARGET_PRICES)}")
    print("=" * 65)
    print(f"  Monitor book, buy UP and DOWN when asks dip to ${TARGET_PRICE}")
    print(f"  Only buy when OTHER side ask < ${MAX_OTHER_SIDE_ASK} (market undecided)")
    print(f"  ${BET_PER_SIDE}/side | Max combined: ${MAX_COMBINED_COST}")
    print(f"  Scan: T+{ENTRY_START} to T-{ENTRY_END} | Every {SCAN_INTERVAL}s")
    print(f"  REALISTIC: only fills when real ask depth exists")
    print()
    print(f"  Paper mode — collecting data")
    print()

    bot = BTCBothSidesBot()
    bot._write_summary()
    log_msg(f"[INIT] PAPER — Bank: ${bot.bankroll:.2f}")

    asyncio.create_task(send_telegram(
        f"🎯 <b>{BOT_NAME}</b> [PAPER]\n"
        f"Buy both sides below ${max(TARGET_PRICES)}\n"
        f"Targets: {', '.join(f'${p}' for p in TARGET_PRICES)}\n"
        f"Bank: ${bot.bankroll:.2f}"))

    now = time.time()
    nxt = (int(now) // 300 + 1) * 300
    wait = nxt - now
    log_msg(f"[SYNC] Waiting {wait:.0f}s for window boundary...")
    await asyncio.sleep(wait)
    log_msg(f"[SYNC] Aligned.")

    await asyncio.gather(bot.run(), run_status(bot))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
