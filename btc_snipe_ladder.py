#!/usr/bin/env python3
"""
BTC-SNIPE-LADDER — T-70 Maker Ladder on BTC 5-Min Up/Down
=============================================================
Strategy:
  - BTC only
  - At T-70 (70 seconds before window close), check which side is winning
  - Place 6 GTC maker limit bids at $0.87, $0.88, $0.89, $0.90, $0.91, $0.92
  - 20% of bankroll split across the 6 orders
  - Hold to resolution ($1.00 or $0.00)
  - Maker orders = 0% fee + rebate eligibility

Entry: T-70 (230 seconds into window)
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
BET_PCT = 0.00                   # Not used — fixed $1/order instead
BET_PER_ORDER = 2.00             # $2 per ladder order, $12 total per trade
LADDER_PRICES = [0.87, 0.88, 0.89, 0.90, 0.91, 0.92]
ENTRY_SECONDS_BEFORE = 70       # Enter at T-70

MIN_LEADING_BID = 0.70          # Only trade if leading side bid >= this
MAX_LEADING_BID = 0.95          # Don't trade if leading side already too expensive

STARTING_BANKROLL = 100.0

USDC_CONTRACT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PAPER_MODE = os.getenv("BTC_SNIPE_LADDER_LIVE", "0") != "1"

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.makedirs("logs", exist_ok=True)
BOT_NAME = "BTC-SNIPE-LADDER" if not PAPER_MODE else "BTC-SNIPE-LADDER-PAPER"
LOG_FILE = "logs/btc_snipe_ladder_trades.jsonl"
SUMMARY_FILE = "logs/btc_snipe_ladder_summary.json"


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


# ── Market Discovery ───────────────────────────────────
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


async def get_book(token_id):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://clob.polymarket.com/book?token_id={token_id}",
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    bb = max((float(b["price"]) for b in bids), default=0)
                    ba = min((float(a["price"]) for a in asks), default=0)
                    return {"bid": bb, "ask": ba}
    except Exception:
        pass
    return None


# ── Bot ───────────────────────────────────────────────
class BTCSnipeLadderBot:
    def __init__(self):
        self.mf = MarketFinder()
        self.client = None
        self.engine = None
        self.bankroll = STARTING_BANKROLL
        self.peak = STARTING_BANKROLL
        self.max_dd = 0
        self.wins = 0
        self.losses = 0
        self.trade_count = 0
        self.start_time = time.time()
        self.log_file = open(LOG_FILE, "a")
        self.unfilled = 0
        self._pending_trade = None

    async def _read_wallet_balance(self):
        """Read pUSD balance from Polygon via aiohttp."""
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

    def init_clob(self):
        if PAPER_MODE:
            log_msg("[CLOB] PAPER MODE — no real orders")
            return
        if not CLOB_AVAILABLE or not PRIVATE_KEY:
            log_msg("[CLOB] No client — paper mode")
            return
        try:
            self.client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                                     signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS)
            self.client.set_api_creds(self.client.create_or_derive_api_key())
            log_msg("[CLOB] Auth OK — LIVE execution ready")
        except Exception as e:
            log_msg(f"[CLOB] Auth fail: {e}")

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

                log_msg(f"[LOOP] Window start | Bank: ${self.bankroll:.2f}")

                if self.bankroll < 3:
                    log_msg(f"[RISK] Bankroll ${self.bankroll:.2f} too low")
                    continue

                # Scan continuously from T-70 to T-30
                window_start = (int(time.time()) // 300) * 300
                entry_start = window_start + 300 - ENTRY_SECONDS_BEFORE  # T-70
                entry_end = window_start + 300 - 30                       # T-30

                now = time.time()
                if now < entry_start:
                    await asyncio.sleep(entry_start - now)

                # Place ladder once conditions qualify, then monitor fills
                ladder_placed = False
                while time.time() < entry_end:
                    if not ladder_placed:
                        placed = await self._try_place_ladder()
                        if placed:
                            ladder_placed = True
                    await asyncio.sleep(3)  # Check every 3 seconds

                # Cancel unfilled orders at T-30
                if ladder_placed and self.client and not PAPER_MODE:
                    try:
                        self.client.cancel_all()
                        log_msg("[CANCEL] Unfilled orders cancelled at T-30")
                    except:
                        pass

                # If ladder was placed, wait for resolution
                if ladder_placed:
                    await self._wait_resolution()

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                await asyncio.sleep(5)

    async def _try_place_ladder(self):
        """Check book and place ladder if conditions qualify. Returns True if placed."""
        mkt = self.mf.market
        if not mkt:
            return False

        up_book, down_book = await asyncio.gather(
            get_book(mkt["up"]), get_book(mkt["down"]))
        if not up_book or not down_book:
            return False

        up_bid = up_book["bid"]
        down_bid = down_book["bid"]

        if up_bid > down_bid:
            target_side = "UP"
            target_token = mkt["up"]
            current_bid = up_bid
        elif down_bid > up_bid:
            target_side = "DOWN"
            target_token = mkt["down"]
            current_bid = down_bid
        else:
            return False

        if current_bid < MIN_LEADING_BID:
            return False
        if current_bid > MAX_LEADING_BID:
            return False

        valid_prices = [p for p in LADDER_PRICES if p <= current_bid + 0.02]
        if not valid_prices:
            return False

        self.trade_count += 1
        tid = self.trade_count

        log_msg(f"[SIGNAL] #{tid} BTC {target_side} @ ${current_bid:.2f} | "
                f"Ladder {len(valid_prices)} bids ${valid_prices[0]:.2f}-${valid_prices[-1]:.2f} | "
                f"${BET_PER_ORDER:.2f}/order | {mkt['question']}")

        orders_placed = 0
        for bid_price in valid_prices:
            shares = round(BET_PER_ORDER / bid_price)
            if shares < 2:
                shares = 2

            if self.client and not PAPER_MODE:
                try:
                    args = OrderArgs(price=bid_price, size=shares, side=BUY, token_id=target_token)
                    signed = self.client.create_order(args)
                    resp = self.client.post_order(signed, OrderType.GTC)
                    oid = resp.get("orderID") or resp.get("order_id") if resp else None
                    if oid:
                        log_msg(f"[BID] GTC {shares}sh @ ${bid_price:.2f} order={oid[:8]}...")
                        orders_placed += 1
                except Exception as e:
                    log_msg(f"[BID] err @ ${bid_price:.2f}: {e}")
            else:
                log_msg(f"[PAPER] BID {shares}sh @ ${bid_price:.2f}")
                orders_placed += 1

        if orders_placed == 0:
            log_msg(f"[FAIL] #{tid} No orders placed")
            return False

        # Store trade info for resolution tracking
        self._pending_trade = {
            "tid": tid, "target_side": target_side, "target_token": target_token,
            "current_bid": current_bid, "orders_placed": orders_placed,
            "valid_prices": valid_prices, "question": mkt["question"],
        }
        return True

    async def _wait_resolution(self):
        """Wait for resolution of the pending trade."""
        trade = self._pending_trade
        if not trade:
            return

        tid = trade["tid"]
        target_token = trade["target_token"]
        target_side = trade["target_side"]
        current_bid = trade["current_bid"]
        orders_placed = trade["orders_placed"]

        # Check fills
        filled_shares = 0
        filled_cost = 0

        if self.client and not PAPER_MODE:
            # Read fill status from order book or check positions
            # For now, estimate based on what we placed
            # TODO: check actual order status via API
            await asyncio.sleep(5)
            # Simple approach: check if we have tokens by reading the book spread
            # If our bids were at $0.87-$0.92 and the bid is now higher, they may have filled
            book = await get_book(target_token)
            if book and book["bid"] >= 0.90:
                # Likely some fills — estimate conservatively
                for p in trade["valid_prices"]:
                    if p <= book["bid"]:
                        shares = round(BET_PER_ORDER / p)
                        if shares < 2:
                            shares = 2
                        filled_shares += shares
                        filled_cost += shares * p
        else:
            # Paper: simulate fills for orders at/below current bid
            for p in trade["valid_prices"]:
                shares = round(BET_PER_ORDER / p)
                if shares < 2:
                    shares = 2
                if p <= current_bid:
                    filled_shares += shares
                    filled_cost += shares * p
                else:
                    import random
                    if random.random() < 0.3:
                        filled_shares += shares
                        filled_cost += shares * p

        if filled_shares <= 0:
            log_msg(f"[UNFILL] #{tid} No fills — skipping")
            self.unfilled += 1
            self._pending_trade = None
            return

        avg_fill = filled_cost / filled_shares if filled_shares > 0 else 0
        log_msg(f"[FILLED] #{tid} {filled_shares:.0f}sh @ avg ${avg_fill:.3f} (${filled_cost:.2f})")

        # Wait for definitive resolution
        for _ in range(90):
            book = await get_book(target_token)
            if book:
                if book["bid"] >= 0.95:
                    pnl = round(filled_shares * (1.00 - avg_fill), 4)
                    self._record_trade(tid, target_side, avg_fill, 1.00, filled_shares, filled_cost,
                                       pnl, "WIN", trade["question"], current_bid, orders_placed)
                    self._pending_trade = None
                    return
                if book["bid"] <= 0.05:
                    pnl = round(-filled_cost, 4)
                    self._record_trade(tid, target_side, avg_fill, 0.00, filled_shares, filled_cost,
                                       pnl, "LOSS", trade["question"], current_bid, orders_placed)
                    self._pending_trade = None
                    return
            await asyncio.sleep(1)

        # Extended wait
        for _ in range(120):
            book = await get_book(target_token)
            if book:
                if book["bid"] >= 0.95:
                    pnl = round(filled_shares * (1.00 - avg_fill), 4)
                    self._record_trade(tid, target_side, avg_fill, 1.00, filled_shares, filled_cost,
                                       pnl, "WIN-LATE", trade["question"], current_bid, orders_placed)
                    self._pending_trade = None
                    return
                if book["bid"] <= 0.05:
                    pnl = round(-filled_cost, 4)
                    self._record_trade(tid, target_side, avg_fill, 0.00, filled_shares, filled_cost,
                                       pnl, "LOSS", trade["question"], current_bid, orders_placed)
                    self._pending_trade = None
                    return
            await asyncio.sleep(1)

        # Final fallback
        book = await get_book(target_token)
        final_bid = book["bid"] if book else 0
        log_msg(f"[TIMEOUT] #{tid} Final bid=${final_bid:.2f} after extended wait")
        if final_bid >= 0.90:
            pnl = round(filled_shares * (1.00 - avg_fill), 4)
            self._record_trade(tid, target_side, avg_fill, 1.00, filled_shares, filled_cost,
                               pnl, "WIN-LATE", trade["question"], current_bid, orders_placed)
        elif final_bid <= 0.10:
            pnl = round(-filled_cost, 4)
            self._record_trade(tid, target_side, avg_fill, 0.00, filled_shares, filled_cost,
                               pnl, "LOSS", trade["question"], current_bid, orders_placed)
        else:
            log_msg(f"[WARN] #{tid} Ambiguous bid=${final_bid:.2f} — recording as LOSS")
            pnl = round(-filled_cost, 4)
            self._record_trade(tid, target_side, avg_fill, final_bid, filled_shares, filled_cost,
                               pnl, "LOSS-AMBIGUOUS", trade["question"], current_bid, orders_placed)
        self._pending_trade = None

    def _record_trade(self, tid, side, avg_fill, exit_price, shares, cost, pnl, result, question, entry_bid, orders):
        self.bankroll = round(self.bankroll + pnl, 4)

        if pnl > 0.01:
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
        ret_pct = round(pnl / cost * 100, 1) if cost > 0 else 0

        log_msg(f"[{result}] #{tid} BTC {side} | avg ${avg_fill:.3f} → ${exit_price:.2f} | "
                f"{shares:.0f}sh ${cost:.2f} cost | P&L ${pnl:+.2f} ({ret_pct:+.1f}%) | "
                f"bank ${self.bankroll:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%)")

        try:
            self.log_file.write(json.dumps({
                "id": tid, "side": side, "avg_fill": round(avg_fill, 4),
                "exit": exit_price, "shares": round(shares, 2),
                "cost": round(cost, 4), "pnl": pnl, "return_pct": ret_pct,
                "result": result, "bankroll": self.bankroll,
                "entry_bid": entry_bid, "orders_placed": orders,
                "ladder": [p for p in LADDER_PRICES],
                "question": question,
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except Exception:
            pass

        self._write_summary()

        icon = "🟢" if pnl > 0 else "🔴"
        asyncio.create_task(send_telegram(
            f"{icon} <b>{BOT_NAME} #{tid}</b>\n"
            f"BTC {side} | avg ${avg_fill:.3f} → {result}\n"
            f"P&L: ${pnl:+.2f} ({ret_pct:+.0f}%) | {shares:.0f}sh\n"
            f"Ladder: ${LADDER_PRICES[0]:.2f}-${LADDER_PRICES[-1]:.2f} | {orders} orders\n"
            f"Bank: ${self.bankroll:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%)"))

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        summary = {
            "bot": BOT_NAME,
            "mode": "LIVE" if self.client and not PAPER_MODE else "PAPER",
            "elapsed_minutes": round(elapsed, 1),
            "trades": total, "wins": self.wins, "losses": self.losses,
            "win_rate": round(wr, 1),
            "bankroll": round(self.bankroll, 2),
            "starting_bankroll": STARTING_BANKROLL,
            "pnl_total": round(self.bankroll - STARTING_BANKROLL, 2),
            "max_dd": round(self.max_dd, 1),
            "unfilled": self.unfilled,
            "ladder": LADDER_PRICES,
            "entry_t_minus": ENTRY_SECONDS_BEFORE,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(SUMMARY_FILE, summary)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        pnl = self.bankroll - STARTING_BANKROLL
        bet = round(self.bankroll * BET_PCT, 2)
        mode = "LIVE" if self.client and not PAPER_MODE else "PAPER"

        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {BOT_NAME} | {elapsed:.0f}min | {mode}")
        print(f"  Strategy: T-{ENTRY_SECONDS_BEFORE} → ladder maker bids ${LADDER_PRICES[0]:.2f}-${LADDER_PRICES[-1]:.2f}")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f} | DD: {self.max_dd:.0f}%")
        print(f"  Bet: ${bet:.2f} ({int(BET_PCT*100)}%) / {len(LADDER_PRICES)} = ${bet/len(LADDER_PRICES):.2f} each")
        print(f"  Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}% | Unfilled: {self.unfilled}")
        print()


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        bot.print_status()


async def main():
    mode = "LIVE" if not PAPER_MODE else "PAPER"
    print("=" * 65)
    print(f"  {BOT_NAME} — T-{ENTRY_SECONDS_BEFORE} Maker Ladder [{mode}]")
    print("=" * 65)
    print(f"  At T-{ENTRY_SECONDS_BEFORE}, buy winning side with maker ladder")
    print(f"  Ladder: ${LADDER_PRICES[0]:.2f} to ${LADDER_PRICES[-1]:.2f} ({len(LADDER_PRICES)} orders)")
    print(f"  Bet: {int(BET_PCT*100)}% of bankroll / {len(LADDER_PRICES)} orders")
    print(f"  Leading bid range: ${MIN_LEADING_BID}-${MAX_LEADING_BID}")
    print(f"  Hold to resolution | 0% maker fee")
    print()
    if mode == "LIVE":
        print(f"  *** LIVE MODE — REAL MONEY AT RISK ***")
    else:
        print(f"  Paper mode — set BTC_SNIPE_LADDER_LIVE=1 to go live")
    print()

    bot = BTCSnipeLadderBot()
    bot.init_clob()

    # Sync wallet balance in live mode
    if not PAPER_MODE and bot.client:
        bal = await bot._read_wallet_balance()
        if bal and bal > 0:
            bot.bankroll = bal
            bot.peak = bal
            log_msg(f"[WALLET] Balance: ${bal:.2f}")

    bot._write_summary()
    log_msg(f"[INIT] {mode} mode — Bank: ${bot.bankroll:.2f}")

    asyncio.create_task(send_telegram(
        f"🎯 <b>{BOT_NAME}</b> [{mode}]\n"
        f"T-{ENTRY_SECONDS_BEFORE} maker ladder ${LADDER_PRICES[0]:.2f}-${LADDER_PRICES[-1]:.2f}\n"
        f"Bet: {int(BET_PCT*100)}% / {len(LADDER_PRICES)} orders\n"
        f"Bank: ${bot.bankroll:.2f}"))

    # Align to window
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
