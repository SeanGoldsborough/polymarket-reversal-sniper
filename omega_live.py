#!/usr/bin/env python3
"""
OMEGA — Pure Reactive Execution
=================================
Buy BOTH sides at window open.
NO pre-placed orders. Monitor every second.
When bid hits TP or SL → sell IMMEDIATELY.
No share locking. No cancellation delays. No gaps.
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
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL_OMEGA", "40.0"))
BET_PCT = 0.10
MAX_BET_PER_SIDE = 200.0
TP_OFFSET = 0.15
SL_OFFSET = 0.15
MAX_COMBINED_ASK = 1.03
MIN_SIDE_ASK = 0.10
MAX_SIDE_ASK = 0.90
DRAWDOWN_PAUSE = 0.30
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

os.makedirs("logs", exist_ok=True)
BOT_NAME = "OMEGA"


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def log_msg(msg):
    print(f"  [{ts()}] {msg}", flush=True)

def snap_price(price):
    return round(max(0.01, min(0.99, round(price * 100) / 100)), 2)


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


async def fetch_wallet_balance():
    wallet = PROXY_WALLET or FUNDER_ADDRESS
    if not wallet:
        return None
    padded = wallet.lower().replace("0x", "").zfill(64)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
               "params": [{"to": USDC_CONTRACT, "data": f"0x70a08231{padded}"}, "latest"]}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post("https://polygon-bor-rpc.publicnode.com", json=payload,
                              headers={"Content-Type": "application/json"},
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                return int(data.get("result", "0x0"), 16) / 1_000_000
    except Exception:
        return None


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
                                                  "question": m.get("question", ""), "window_start": window_start}
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
                    return {"bid": bb, "ask": ba}
    except Exception:
        pass
    return None


class OmegaBot:
    def __init__(self):
        self.mf = MarketFinder()
        self.client = None
        self._lock = asyncio.Lock()
        self.bankroll = STARTING_BANKROLL
        self.peak = STARTING_BANKROLL
        self.max_dd = 0
        self.wins = 0
        self.losses = 0
        self.trade_count = 0
        self.start_time = time.time()
        self.paused = False
        self.log_file = open("logs/omega_trades.jsonl", "a")

    def init_clob(self):
        if not CLOB_AVAILABLE or not PRIVATE_KEY:
            return
        try:
            self.client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                                     signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS)
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            log_msg("[CLOB] Auth OK")
        except Exception as e:
            log_msg(f"[CLOB] Auth fail: {e}")

    async def _buy(self, token, price, size):
        async with self._lock:
            if not self.client:
                return None
            try:
                args = OrderArgs(price=price, size=size, side=BUY, token_id=token)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.GTC)
                return resp.get("orderID") or resp.get("order_id") if resp else None
            except Exception as e:
                log_msg(f"[ORD] buy err: {e}")
                return None

    async def _sell(self, token, price, size):
        """Sell with retries at decreasing prices. NO shares are locked beforehand."""
        async with self._lock:
            if not self.client:
                return None
            for attempt in range(3):
                try:
                    sell_price = snap_price(price - attempt * 0.02)
                    args = OrderArgs(price=sell_price, size=size, side=SELL, token_id=token)
                    signed = self.client.create_order(args)
                    resp = self.client.post_order(signed, OrderType.GTC)
                    oid = resp.get("orderID") or resp.get("order_id") if resp else None
                    if oid:
                        log_msg(f"[SELL] OK @ ${sell_price} ({size} sh)")
                        return oid
                except Exception as e:
                    err = str(e)
                    if "balance" in err.lower() and attempt < 2:
                        size = round(size - 1, 2)
                        if size < 1:
                            return None
                        continue
                    log_msg(f"[SELL] err attempt {attempt+1}: {e}")
            return None

    async def _cancel_all(self):
        async with self._lock:
            if not self.client:
                return
            try:
                self.client.cancel_all()
            except Exception:
                pass

    async def sync_bankroll(self):
        b = await fetch_wallet_balance()
        if b is not None:
            self.bankroll = b
            if b > self.peak:
                self.peak = b
            dd = (self.peak - b) / self.peak * 100 if self.peak > 0 else 0
            if dd > self.max_dd:
                self.max_dd = dd
            if dd >= DRAWDOWN_PAUSE * 100 and not self.paused:
                self.paused = True
                log_msg(f"[RISK] PAUSED DD {dd:.0f}%")
                asyncio.create_task(send_telegram(f"🛑 {BOT_NAME} PAUSED DD {dd:.0f}%"))

    async def run(self):
        while True:
            try:
                now = time.time()
                nxt = (int(now) // 300 + 1) * 300
                wait = nxt - now
                if wait > 0:
                    await asyncio.sleep(wait)
                await asyncio.sleep(2)

                if self.paused:
                    continue

                await self.sync_bankroll()
                if self.bankroll < 3:
                    continue

                self.mf.last_refresh = 0
                await self.mf.refresh()
                if not self.mf.active_market:
                    continue

                # Cancel ANY leftover orders from previous windows
                await self._cancel_all()
                await asyncio.sleep(1)

                await self._trade_window(self.mf.active_market["up"], self.mf.active_market["down"])

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                await self._cancel_all()
                await asyncio.sleep(5)

    async def _trade_window(self, up_tok, down_tok):
        # Read books
        up_book, down_book = await asyncio.gather(get_book(up_tok), get_book(down_tok))
        if not up_book or not down_book:
            return

        up_ask = up_book["ask"]
        down_ask = down_book["ask"]

        if up_ask + down_ask > MAX_COMBINED_ASK:
            return
        if up_ask < MIN_SIDE_ASK or down_ask < MIN_SIDE_ASK:
            return
        if up_ask > MAX_SIDE_ASK or down_ask > MAX_SIDE_ASK:
            return

        half = min(self.bankroll * BET_PCT / 2, MAX_BET_PER_SIDE)
        up_sh = round(half / up_ask, 2)
        down_sh = round(half / down_ask, 2)
        if up_sh < 1 or down_sh < 1:
            return

        up_entry = snap_price(up_ask)
        down_entry = snap_price(down_ask)
        up_tp = up_entry + TP_OFFSET
        up_sl = up_entry - SL_OFFSET
        down_tp = down_entry + TP_OFFSET
        down_sl = down_entry - SL_OFFSET

        self.trade_count += 1
        tid = self.trade_count

        log_msg(f"[BUY] #{tid} UP ${up_entry} ({up_sh}sh) DOWN ${down_entry} ({down_sh}sh)")

        # Buy both — NO SL orders placed. Shares are FREE to sell anytime.
        up_buy_id, down_buy_id = await asyncio.gather(
            self._buy(up_tok, up_entry, up_sh),
            self._buy(down_tok, down_entry, down_sh))

        if not up_buy_id and not down_buy_id:
            log_msg(f"[FAIL] Both buys failed")
            return
        if not up_buy_id or not down_buy_id:
            log_msg(f"[FAIL] One buy failed — cancelling")
            await self._cancel_all()
            return

        log_msg(f"[BUY] ✅ Both filled. Monitoring for TP ${TP_OFFSET:+.2f} / SL ${SL_OFFSET:+.2f}")

        # Wait for buys to settle
        await asyncio.sleep(2)

        # PURE REACTIVE MONITORING — check every second, sell immediately
        up_done = False
        down_done = False
        window_end = time.time() + 280

        while time.time() < window_end:
            up_book, down_book = await asyncio.gather(get_book(up_tok), get_book(down_tok))

            # UP side
            if not up_done and up_book:
                bid = up_book["bid"]
                if bid >= up_tp:
                    log_msg(f"[TP] UP ${bid:.2f} >= ${up_tp:.2f}")
                    await self._sell(up_tok, bid, up_sh)
                    up_done = True
                elif bid <= up_sl:
                    log_msg(f"[SL] UP ${bid:.2f} <= ${up_sl:.2f}")
                    await self._sell(up_tok, bid, up_sh)
                    up_done = True

            # DOWN side
            if not down_done and down_book:
                bid = down_book["bid"]
                if bid >= down_tp:
                    log_msg(f"[TP] DOWN ${bid:.2f} >= ${down_tp:.2f}")
                    await self._sell(down_tok, bid, down_sh)
                    down_done = True
                elif bid <= down_sl:
                    log_msg(f"[SL] DOWN ${bid:.2f} <= ${down_sl:.2f}")
                    await self._sell(down_tok, bid, down_sh)
                    down_done = True

            if up_done and down_done:
                break

            await asyncio.sleep(1)

        # Window ending — sell anything still held
        if not up_done:
            log_msg(f"[EXP] UP not exited — selling at market")
            up_book = await get_book(up_tok)
            if up_book and up_book["bid"] > 0.01:
                await self._sell(up_tok, up_book["bid"], up_sh)
            up_done = True
        if not down_done:
            log_msg(f"[EXP] DOWN not exited — selling at market")
            down_book = await get_book(down_tok)
            if down_book and down_book["bid"] > 0.01:
                await self._sell(down_tok, down_book["bid"], down_sh)
            down_done = True

        # Cancel any remaining orders
        await self._cancel_all()
        await asyncio.sleep(3)

        # P&L from wallet
        old_bank = self.bankroll
        await self.sync_bankroll()
        pnl = round(self.bankroll - old_bank, 2)

        if pnl > 0.01:
            self.wins += 1
        elif pnl < -0.01:
            self.losses += 1

        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0

        log_msg(f"[DONE] #{tid} P&L ${pnl:+.2f} | bank ${self.bankroll:.2f} | "
                f"{self.wins}W/{self.losses}L ({wr:.0f}%)")

        try:
            self.log_file.write(json.dumps({
                "id": tid, "pnl": pnl, "bankroll": self.bankroll,
                "up_entry": up_entry, "down_entry": down_entry,
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except Exception:
            pass

        icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
        asyncio.create_task(send_telegram(
            f"{icon} <b>{BOT_NAME} #{tid}</b>\n"
            f"P&L: ${pnl:+.2f} | Bank: ${self.bankroll:.2f}\n"
            f"{self.wins}W/{self.losses}L ({wr:.0f}%)"))

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        pnl = self.bankroll - STARTING_BANKROLL
        bet = min(self.bankroll * BET_PCT / 2, MAX_BET_PER_SIDE)
        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {BOT_NAME} | {elapsed:.0f}min | {'PAUSED' if self.paused else 'ACTIVE'}")
        print(f"  Strategy: Buy both → REACTIVE TP/SL (no pre-placed orders)")
        print(f"  TP +${TP_OFFSET} / SL -${SL_OFFSET} | Monitor every 1s | Sell instantly")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f} | DD: {self.max_dd:.0f}%")
        print(f"  Bet: ${bet:.2f}/side | Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}%")
        print()


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        await bot.sync_bankroll()
        bot.print_status()


async def main():
    print("=" * 65)
    print(f"  {BOT_NAME} — Pure Reactive Execution")
    print("=" * 65)
    print(f"  Buy BOTH sides at window open")
    print(f"  NO pre-placed SL orders. Shares are never locked.")
    print(f"  Monitor every 1 second. Sell INSTANTLY when TP/SL hit.")
    print(f"  TP +${TP_OFFSET} / SL -${SL_OFFSET}")
    print(f"  Bankroll: ${STARTING_BANKROLL}")
    print()

    bot = OmegaBot()
    bot.init_clob()
    await bot.sync_bankroll()
    log_msg(f"[INIT] Bank: ${bot.bankroll:.2f}")
    await send_telegram(f"🚀 <b>{BOT_NAME} v2</b>\nReactive execution\nTP +${TP_OFFSET} SL -${SL_OFFSET}\nBank: ${bot.bankroll:.2f}")
    await asyncio.gather(bot.run(), run_status(bot))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
