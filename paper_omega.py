#!/usr/bin/env python3
"""
PAPER OMEGA — Blind-validated strategies
==========================================
Runs the two strategies proven profitable in blind simulation:

O1: F2 (T-45, buy leader, SL entry-$0.10, hold to $1.00)
O2: E5 (T-30, buy leader, SL entry-$0.10, hold to $1.00)

Both simulate realistic execution:
- 2% taker fee on entry
- SL fills at exact price (pre-placed on book)
- 4-second balance settlement delay
- If SL triggers but price gapped past, fill at bid+$0.02

These are the ONLY strategies proven profitable on 1000+ blind trades.
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
STARTING_BANKROLL = 54.0
BET_PCT = 0.10
SL_OFFSET = 0.10
TP_PRICE = 0.99
FEE_RATE = 0.02  # 2% taker fee on entry
SETTLE_DELAY = 4  # seconds for order settlement

LOG_FILE = "logs/paper_omega.jsonl"
SUMMARY_FILE = "logs/paper_omega_summary.json"
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
                                "up_token": up_token, "down_token": down_token,
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
                    mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0.0
                    return {"best_bid": best_bid, "best_ask": best_ask, "midpoint": mid}
    except Exception:
        pass
    return None


@dataclass
class Variant:
    name: str
    trigger_remaining: int  # seconds before window end
    bankroll: float = STARTING_BANKROLL
    peak: float = STARTING_BANKROLL
    max_dd: float = 0.0
    wins: int = 0
    losses: int = 0
    consec_losses: int = 0
    max_consec: int = 0
    trade_count: int = 0
    signals: int = 0


class PaperOmega:
    def __init__(self):
        self.market_finder = MarketFinder()
        self.cb_price = 0.0
        self.cl_price = 0.0
        self.cb_ticks = 0
        self.cl_ticks = 0
        self.start_time = time.time()
        self.variants = {
            "O1": Variant("O1: F2 (T-45)", 45),
            "O2": Variant("O2: E5 (T-30)", 30),
        }

    def on_coinbase(self, price):
        self.cb_price = price
        self.cb_ticks += 1

    def on_chainlink(self, price):
        self.cl_price = price
        self.cl_ticks += 1

    async def watch_windows(self, key):
        v = self.variants[key]
        while True:
            try:
                now = time.time()
                window_start = (int(now) // 300) * 300
                trigger = window_start + (300 - v.trigger_remaining)
                if now >= trigger:
                    trigger = (window_start + 300) + (300 - v.trigger_remaining)
                wait = trigger - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)

                current_window = int(time.time()) // 300
                self.market_finder.last_refresh = 0
                await self.market_finder.refresh()
                if not self.market_finder.active_market:
                    continue

                up_token = self.market_finder.active_market["up_token"]
                down_token = self.market_finder.active_market["down_token"]

                up_book, down_book = await asyncio.gather(
                    get_book(up_token), get_book(down_token))
                if not up_book or not down_book:
                    continue

                v.signals += 1

                # Pick leader
                if up_book["midpoint"] > down_book["midpoint"]:
                    direction = "UP"
                    token = up_token
                    entry_ask = up_book["best_ask"]
                elif down_book["midpoint"] > up_book["midpoint"]:
                    direction = "DOWN"
                    token = down_token
                    entry_ask = down_book["best_ask"]
                else:
                    continue

                if entry_ask <= 0 or entry_ask >= 1.0:
                    continue

                # Simulate fee
                effective_entry = round(entry_ask * (1 + FEE_RATE), 4)
                shares = round((v.bankroll * BET_PCT) / effective_entry, 2)
                cost = round(shares * effective_entry, 4)
                sl_price = round(effective_entry - SL_OFFSET, 2)

                v.trade_count += 1
                tid = v.trade_count

                log_msg(f"[{key}] #{tid} {direction} @ ${effective_entry:.2f} "
                        f"({shares:.1f}sh ${cost:.2f}) SL ${sl_price:.2f}")

                # Simulate settlement delay
                await asyncio.sleep(SETTLE_DELAY)

                # Monitor
                deadline = (int(time.time()) // 300) * 300 + 295
                while time.time() < deadline:
                    book = await get_book(token)
                    if not book:
                        await asyncio.sleep(1)
                        continue
                    bid = book["best_bid"]

                    # TP
                    if bid >= TP_PRICE:
                        proceeds = round(shares * 1.00, 4)
                        pnl = round(proceeds - cost, 4)
                        self._close(key, v, tid, direction, effective_entry, 1.00, "won", pnl)
                        break

                    # SL (simulate realistic fill)
                    if bid <= sl_price:
                        if bid < sl_price - 0.05:
                            fill = round(bid + 0.02, 2)  # gapped — fill near bid
                        else:
                            fill = sl_price  # pre-placed order fills at SL
                        proceeds = round(shares * fill, 4)
                        pnl = round(proceeds - cost, 4)
                        self._close(key, v, tid, direction, effective_entry, fill, "stopped", pnl)
                        break

                    await asyncio.sleep(1)
                else:
                    # Expired — assume worst case ($0)
                    pnl = round(-cost, 4)
                    self._close(key, v, tid, direction, effective_entry, 0.0, "expired", pnl)

            except Exception as e:
                log_msg(f"[{key}] error: {e}")
                await asyncio.sleep(5)

    def _close(self, key, v, tid, direction, entry, exit_p, status, pnl):
        v.bankroll = round(v.bankroll + pnl, 4)
        if v.bankroll > v.peak:
            v.peak = v.bankroll
        dd = (v.peak - v.bankroll) / v.peak * 100 if v.peak > 0 else 0
        if dd > v.max_dd:
            v.max_dd = dd
        if pnl > 0:
            v.wins += 1
            v.consec_losses = 0
        else:
            v.losses += 1
            v.consec_losses += 1
            if v.consec_losses > v.max_consec:
                v.max_consec = v.consec_losses
        log_msg(f"[{key}] {status.upper()} #{tid} {direction} @ ${exit_p:.2f} | "
                f"P&L ${pnl:+.2f} | bank=${v.bankroll:.2f}")
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps({
                    "variant": key, "trade_id": tid, "direction": direction,
                    "entry": entry, "exit": exit_p, "status": status,
                    "pnl": pnl, "bankroll": v.bankroll,
                    "time": datetime.now(timezone.utc).isoformat(),
                }) + "\n")
        except Exception:
            pass
        self._write_summary()

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        summary = {"elapsed_minutes": round(elapsed, 1), "variants": {},
                   "updated": datetime.now(timezone.utc).isoformat()}
        for key, v in self.variants.items():
            total = v.wins + v.losses
            wr = (v.wins / total * 100) if total else 0
            summary["variants"][key] = {
                "name": v.name, "trades": v.trade_count, "wins": v.wins,
                "losses": v.losses, "win_rate": round(wr, 1),
                "bankroll": round(v.bankroll, 2), "peak": round(v.peak, 2),
                "pnl_total": round(v.bankroll - STARTING_BANKROLL, 2),
                "roi_pct": round((v.bankroll / STARTING_BANKROLL - 1) * 100, 2),
                "max_dd": round(v.max_dd, 1), "signals": v.signals,
            }
        with open(SUMMARY_FILE, "w") as f:
            json.dump(summary, f, indent=2)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        print()
        print(f"  PAPER OMEGA | {elapsed:.0f}min | Blind-validated strategies")
        print(f"  Feeds: CB={self.cb_ticks:,} CL={self.cl_ticks:,}")
        print()
        for key, v in self.variants.items():
            total = v.wins + v.losses
            wr = (v.wins / total * 100) if total else 0
            pnl = v.bankroll - STARTING_BANKROLL
            real_low = STARTING_BANKROLL + pnl * 0.60
            real_high = STARTING_BANKROLL + pnl * 0.75
            print(f"  {key} ({v.name}):")
            print(f"    Bank: ${v.bankroll:.2f} (${pnl:+.2f}) | Peak: ${v.peak:.2f} | DD: {v.max_dd:.1f}%")
            print(f"    Trades: {total} ({v.wins}W/{v.losses}L) | WR: {wr:.0f}% | Consec: {v.consec_losses}")
            print(f"    Realistic Est: ${real_low:.2f}-${real_high:.2f}")
        print()


async def run_coinbase(bot):
    while True:
        try:
            async with websockets.connect("wss://ws-feed.exchange.coinbase.com") as ws:
                await ws.send(json.dumps({"type": "subscribe",
                    "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}]}))
                log_msg("[FEED] Coinbase connected")
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
                await ws.send(json.dumps({"action": "subscribe",
                    "subscriptions": [{"topic": "crypto_prices_chainlink",
                                       "type": "*", "filters": ""}]}))
                log_msg("[FEED] Chainlink connected")
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
    print("  PAPER OMEGA — Blind-validated strategies")
    print("=" * 65)
    print(f"  O1: F2 (T-45, buy leader, SL entry-$0.10, hold to $1)")
    print(f"  O2: E5 (T-30, buy leader, SL entry-$0.10, hold to $1)")
    print(f"  Includes: 2% fee, 4s settlement delay, realistic SL fill")
    print(f"  Starting: ${STARTING_BANKROLL}")
    print()

    now = time.time()
    next_window = (int(now) // 300 + 1) * 300
    wait = next_window - now
    log_msg(f"[SYNC] Waiting {wait:.0f}s for window boundary...")
    await asyncio.sleep(wait)
    log_msg(f"[SYNC] Aligned.")

    bot = PaperOmega()
    await asyncio.gather(
        run_coinbase(bot),
        run_chainlink(bot),
        run_status(bot),
        bot.watch_windows("O1"),
        bot.watch_windows("O2"),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
