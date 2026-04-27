#!/usr/bin/env python3
"""
BTC LADDER — Directional Maker Ladder on BTC 5-Min Up/Down
============================================================
Strategy:
  - Track BTC price via Coinbase WebSocket
  - Record "price to beat" at window open (BTC price at start of 5-min window)
  - If Coinbase BTC > price_to_beat + $100: BUY UP with 5 laddered maker bids
  - If Coinbase BTC < price_to_beat - $100: BUY DOWN with 5 laddered maker bids
  - Ladder: 5 equal bids at +$0.01, +$0.02, +$0.03, +$0.04, +$0.05 above current price
  - Each bid = (20% of bankroll) / 5
  - Hold to resolution ($1.00 or $0.00)
  - Maker orders = 0% fee + rebate eligibility

Execution: GTC maker limit orders (0% fee)
"""

import asyncio
import json
import time
import os
import sys
from datetime import datetime, timezone
from collections import deque

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
    from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams
    from py_clob_client.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────
BET_PCT = 0.20                   # 20% of bankroll per signal
LADDER_STEPS = 5                 # Number of ladder orders
LADDER_SPREAD = 0.01             # $0.01 between each step
SIGNAL_THRESHOLD = 100.0         # BTC must be $100 above/below price to beat

MIN_WALLET_BALANCE = 5.0
COOLDOWN = 60.0                  # Don't re-enter same window
MAX_TRADES_PER_WINDOW = 1

USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PAPER_MODE = os.getenv("BTC_LADDER_LIVE", "0") != "1"

# Ensure we're in the right directory for log files
os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.makedirs("logs", exist_ok=True)
BOT_NAME = "BTC-LADDER" if not PAPER_MODE else "BTC-LADDER-PAPER"
LOG_FILE = "logs/btc_ladder_trades.jsonl"
SUMMARY_FILE = "logs/btc_ladder_summary.json"


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


# ── Coinbase Price Feed ─────────────────────────────────
class CoinbaseFeed:
    def __init__(self):
        self.price = 0.0
        self.last_update = 0.0
        self.connected = False
        self.window_open_price = 0.0
        self.current_window = 0

    def _check_window(self):
        window = int(time.time()) // 300
        if window != self.current_window:
            self.current_window = window
            self.window_open_price = self.price
            if self.price > 0:
                log_msg(f"[CB] New window — price to beat: ${self.price:,.2f}")

    async def run(self):
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        "wss://ws-feed.exchange.coinbase.com",
                        timeout=aiohttp.ClientTimeout(total=30),
                        heartbeat=15,
                    ) as ws:
                        sub = json.dumps({
                            "type": "subscribe",
                            "product_ids": ["BTC-USD"],
                            "channels": ["ticker"]
                        })
                        await ws.send_str(sub)
                        self.connected = True
                        log_msg("[COINBASE] Connected to BTC-USD stream")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                    if data.get("type") == "ticker":
                                        p = float(data.get("price", 0))
                                        if p > 0:
                                            self.price = p
                                            self.last_update = time.time()
                                            self._check_window()
                                except Exception:
                                    pass
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except Exception as e:
                log_msg(f"[COINBASE] {e}")
            self.connected = False
            await asyncio.sleep(2)


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


# ── Execution Engine (Maker Ladder) ────────────────────
class LadderEngine:
    def __init__(self, client):
        self.client = client
        self._lock = asyncio.Lock()

    async def place_ladder(self, token_id, base_price, total_amount):
        """Place 5 GTC maker limit bids at base_price+0.01 through base_price+0.05.
        Each order gets total_amount / 5."""
        async with self._lock:
            if not self.client:
                return []

            # Calculate how many steps fit under $0.99
            valid_steps = []
            for i in range(1, LADDER_STEPS + 1):
                p = snap_price(base_price + i * LADDER_SPREAD)
                if p <= 0.99:
                    valid_steps.append((i, p))

            if not valid_steps:
                log_msg(f"[LADDER] No valid steps — base ${base_price:.2f} too high")
                return []

            per_order = round(total_amount / len(valid_steps), 2)
            orders = []

            for i, bid_price in valid_steps:
                shares = round(per_order / bid_price)
                if shares < 5:
                    shares = 5

                try:
                    args = OrderArgs(price=bid_price, size=shares, side=BUY, token_id=token_id)
                    signed = self.client.create_order(args)
                    resp = self.client.post_order(signed, OrderType.GTC)
                    oid = resp.get("orderID") or resp.get("order_id") if resp else None
                    if oid:
                        log_msg(f"[BID] #{i} GTC {shares}sh @ ${bid_price:.2f} (${per_order:.2f}) order={oid[:8]}...")
                        orders.append({
                            "order_id": oid, "price": bid_price, "shares": shares,
                            "amount": per_order, "step": i, "token_id": token_id
                        })
                except Exception as e:
                    log_msg(f"[BID] #{i} err @ ${bid_price:.2f}: {e}")

            return orders

    async def check_order_status(self, order_id):
        try:
            result = self.client.get_order(order_id)
            return result
        except Exception:
            return None

    async def cancel_order(self, order_id):
        async with self._lock:
            if not self.client:
                return
            try:
                self.client.cancel(order_id)
            except Exception:
                pass

    async def cancel_all(self):
        async with self._lock:
            if not self.client:
                return
            try:
                self.client.cancel_all()
                log_msg("[CANCEL] All orders cancelled")
            except Exception as e:
                log_msg(f"[CANCEL] err: {e}")


# ── BTC Ladder Bot ────────────────────────────────────
class BTCLadderBot:
    def __init__(self):
        self.mf = MarketFinder()
        self.coinbase = CoinbaseFeed()
        self.client = None
        self.engine = None
        self.bankroll = 0.0
        self.starting_bankroll = 0.0
        self.peak = 0.0
        self.max_dd = 0
        self.wins = 0
        self.losses = 0
        self.trade_count = 0
        self.start_time = time.time()
        self.log_file = open(LOG_FILE, "a")
        self.current_window = 0
        self.traded_this_window = False
        self.position = None          # Active position
        self.placing_order = False
        self.signals_detected = 0
        self.signals_up = 0
        self.signals_down = 0

    def init_clob(self):
        if PAPER_MODE:
            log_msg("[CLOB] PAPER MODE — no real orders")
            log_msg("[CLOB] Set BTC_LADDER_LIVE=1 to enable live trading")
            self.bankroll = 100.0
            self.starting_bankroll = 100.0
            self.peak = 100.0
            return
        if not CLOB_AVAILABLE or not PRIVATE_KEY:
            log_msg("[CLOB] No client — falling back to paper mode")
            self.bankroll = 100.0
            self.starting_bankroll = 100.0
            self.peak = 100.0
            return
        try:
            self.client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                                     signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS)
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            self.engine = LadderEngine(self.client)
            log_msg("[CLOB] Auth OK — LIVE execution ready")
        except Exception as e:
            log_msg(f"[CLOB] Auth fail: {e}")

    async def sync_bankroll(self):
        if PAPER_MODE or not self.engine:
            return
        b = await fetch_wallet_balance()
        if b is not None:
            self.bankroll = b
            if self.starting_bankroll == 0:
                self.starting_bankroll = b
                self.peak = b
                log_msg(f"[WALLET] Initial balance: ${b:.2f}")
            if b > self.peak:
                self.peak = b
            dd = (self.peak - b) / self.peak * 100 if self.peak > 0 else 0
            if dd > self.max_dd:
                self.max_dd = dd

    async def run(self):
        log_msg("[LADDER] Waiting for Coinbase feed...")
        while not self.coinbase.connected or self.coinbase.price == 0:
            await asyncio.sleep(0.5)
        log_msg(f"[LADDER] Coinbase live: BTC ${self.coinbase.price:,.2f}")

        await self.sync_bankroll()

        while True:
            try:
                now = time.time()
                current_window = int(now) // 300

                # New window
                if current_window != self.current_window:
                    self.current_window = current_window
                    self.traded_this_window = False
                    await self.mf.refresh()

                    # Cancel any unfilled orders from previous window
                    if self.engine and not PAPER_MODE:
                        await self.engine.cancel_all()

                    # Check if previous position resolved
                    if self.position:
                        await self._check_resolution()

                    if not PAPER_MODE and self.engine:
                        await self.sync_bankroll()

                    log_msg(f"[LOOP] Window start | BTC: ${self.coinbase.price:,.2f} | "
                            f"Price to beat: ${self.coinbase.window_open_price:,.2f} | "
                            f"Bank: ${self.bankroll:.2f}")

                # Don't trade if we already traded this window or have open position
                if self.traded_this_window or self.position:
                    await asyncio.sleep(1)
                    continue

                if self.bankroll < MIN_WALLET_BALANCE:
                    await asyncio.sleep(5)
                    continue

                if not self.mf.market:
                    await asyncio.sleep(2)
                    continue

                # Wait at least 30s into window for price to stabilize
                window_start = (int(now) // 300) * 300
                window_elapsed = now - window_start
                if window_elapsed < 30:
                    await asyncio.sleep(1)
                    continue

                # Don't enter in last 30s
                if window_elapsed > 270:
                    await asyncio.sleep(1)
                    continue

                # ── CHECK SIGNAL ──
                price_to_beat = self.coinbase.window_open_price
                current_btc = self.coinbase.price

                if price_to_beat <= 0:
                    await asyncio.sleep(1)
                    continue

                diff = current_btc - price_to_beat
                direction = None

                if diff >= SIGNAL_THRESHOLD:
                    direction = "UP"
                    self.signals_up += 1
                elif diff <= -SIGNAL_THRESHOLD:
                    direction = "DOWN"
                    self.signals_down += 1

                if not direction:
                    await asyncio.sleep(0.5)
                    continue

                self.signals_detected += 1

                # ── GET BOOK PRICE ──
                mkt = self.mf.market
                if direction == "UP":
                    token_id = mkt["up"]
                    side_label = "UP"
                else:
                    token_id = mkt["down"]
                    side_label = "DOWN"

                book = await get_book(token_id)
                if not book or book["bid"] <= 0:
                    await asyncio.sleep(1)
                    continue

                current_price = book["bid"]

                # Don't buy if price already too high (near resolution)
                if current_price >= 0.95:
                    log_msg(f"[SKIP] {side_label} already at ${current_price:.2f} — too expensive")
                    await asyncio.sleep(5)
                    continue

                # ── PLACE LADDER ──
                total_bet = round(self.bankroll * BET_PCT, 2)
                per_order = round(total_bet / LADDER_STEPS, 2)

                log_msg(f"[SIGNAL] BTC ${current_btc:,.2f} vs beat ${price_to_beat:,.2f} = "
                        f"${diff:+,.2f} → {side_label}")
                log_msg(f"[LADDER] Placing {LADDER_STEPS} bids on {side_label} | "
                        f"${total_bet:.2f} total (${per_order:.2f} each) | "
                        f"base ${current_price:.2f}")

                self.placing_order = True
                try:
                    if self.engine and not PAPER_MODE:
                        orders = await self.engine.place_ladder(token_id, current_price, total_bet)
                    else:
                        # Paper mode — simulate fills (cap at $0.99)
                        orders = []
                        valid_paper_steps = []
                        for i in range(1, LADDER_STEPS + 1):
                            p = snap_price(current_price + i * LADDER_SPREAD)
                            if p <= 0.99:
                                valid_paper_steps.append((i, p))
                        if valid_paper_steps:
                            paper_per = round(total_bet / len(valid_paper_steps), 2)
                            for i, bid_price in valid_paper_steps:
                                shares = round(paper_per / bid_price)
                                if shares < 5:
                                    shares = 5
                                log_msg(f"[PAPER] #{i} BID {shares}sh @ ${bid_price:.2f}")
                                orders.append({
                                    "order_id": f"paper-{i}", "price": bid_price,
                                    "shares": shares, "amount": paper_per, "step": i,
                                    "token_id": token_id
                                })
                finally:
                    self.placing_order = False

                if not orders:
                    log_msg(f"[FAIL] No orders placed")
                    continue

                self.traded_this_window = True
                self.trade_count += 1

                self.position = {
                    "id": self.trade_count,
                    "direction": side_label,
                    "token_id": token_id,
                    "orders": orders,
                    "total_bet": total_bet,
                    "entry_time": time.time(),
                    "btc_at_entry": current_btc,
                    "price_to_beat": price_to_beat,
                    "diff": diff,
                    "base_price": current_price,
                    "question": mkt["question"],
                    "wallet_before": self.bankroll,
                    "filled_shares": 0,
                    "filled_cost": 0,
                    "avg_fill": 0,
                }

                # Start monitoring fills
                asyncio.create_task(self._monitor_fills())

                await asyncio.sleep(2)

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                await asyncio.sleep(2)

    async def _monitor_fills(self):
        """Monitor maker order fills until window ends."""
        pos = self.position
        if not pos:
            return

        window_end = (int(pos["entry_time"]) // 300 + 1) * 300

        while time.time() < window_end - 5:
            if not self.position or self.position["id"] != pos["id"]:
                return

            if self.engine and not PAPER_MODE:
                total_filled = 0
                total_cost = 0
                for order in pos["orders"]:
                    status = await self.engine.check_order_status(order["order_id"])
                    if status:
                        matched = float(status.get("size_matched", 0))
                        if matched > 0:
                            total_filled += matched
                            total_cost += matched * order["price"]

                if total_filled > pos["filled_shares"]:
                    pos["filled_shares"] = total_filled
                    pos["filled_cost"] = total_cost
                    pos["avg_fill"] = total_cost / total_filled if total_filled > 0 else 0
                    log_msg(f"[FILL] #{pos['id']} {total_filled:.1f}sh filled @ avg ${pos['avg_fill']:.3f}")
            else:
                # Paper mode: assume all orders fill at their bid price
                total_filled = sum(o["shares"] for o in pos["orders"])
                total_cost = sum(o["shares"] * o["price"] for o in pos["orders"])
                pos["filled_shares"] = total_filled
                pos["filled_cost"] = total_cost
                pos["avg_fill"] = total_cost / total_filled if total_filled > 0 else 0

            await asyncio.sleep(5)

        # Cancel unfilled orders at window end
        if self.engine and not PAPER_MODE:
            for order in pos["orders"]:
                await self.engine.cancel_order(order["order_id"])

        log_msg(f"[WINDOW-END] #{pos['id']} Total filled: {pos['filled_shares']:.1f}sh @ avg ${pos['avg_fill']:.3f}")

    async def _check_resolution(self):
        """Check if the position resolved."""
        pos = self.position
        if not pos:
            return

        if pos["filled_shares"] <= 0:
            log_msg(f"[UNFILL] #{pos['id']} No fills — clearing")
            self.position = None
            return

        token_id = pos["token_id"]

        # Wait for resolution
        for _ in range(90):
            book = await get_book(token_id)
            if book:
                if book["bid"] >= 0.95:
                    pnl = round(pos["filled_shares"] * 1.00 - pos["filled_cost"], 4)
                    await self._close_position("WIN", pnl, 1.00)
                    return
                if book["bid"] <= 0.05:
                    pnl = round(-pos["filled_cost"], 4)
                    await self._close_position("LOSS", pnl, 0.00)
                    return
            await asyncio.sleep(1)

        # Force resolve
        book = await get_book(token_id)
        final_bid = book["bid"] if book else 0
        if final_bid > 0.5:
            pnl = round(pos["filled_shares"] * 1.00 - pos["filled_cost"], 4)
            await self._close_position("WIN", pnl, 1.00)
        else:
            pnl = round(-pos["filled_cost"], 4)
            await self._close_position("LOSS", pnl, 0.00)

    async def _close_position(self, result, pnl, exit_price):
        pos = self.position
        if not pos:
            return

        if PAPER_MODE or not self.engine:
            self.bankroll = round(self.bankroll + pnl, 4)
        else:
            await asyncio.sleep(3)
            await self.sync_bankroll()
            actual_pnl = round(self.bankroll - pos["wallet_before"], 2)
            if abs(actual_pnl - pnl) > 0.50:
                log_msg(f"[WARN] P&L mismatch: calc=${pnl:+.2f} vs wallet=${actual_pnl:+.2f}")
                pnl = actual_pnl

        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1

        if self.bankroll > self.peak:
            self.peak = self.bankroll

        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        elapsed = time.time() - pos["entry_time"]
        ret_pct = round(pnl / pos["filled_cost"] * 100, 1) if pos["filled_cost"] > 0 else 0

        mode = "LIVE" if self.engine and not PAPER_MODE else "PAPER"
        log_msg(f"[{mode}] #{pos['id']} BTC {pos['direction']} {result} | "
                f"P&L ${pnl:+.2f} ({ret_pct:+.0f}%) | "
                f"filled {pos['filled_shares']:.0f}sh @ ${pos['avg_fill']:.3f} | "
                f"bank ${self.bankroll:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%) | "
                f"BTC ${pos['btc_at_entry']:,.2f} vs beat ${pos['price_to_beat']:,.2f} (${pos['diff']:+,.2f})")

        try:
            self.log_file.write(json.dumps({
                "id": pos["id"], "direction": pos["direction"],
                "result": result, "pnl": pnl, "return_pct": ret_pct,
                "filled_shares": pos["filled_shares"],
                "filled_cost": round(pos["filled_cost"], 4),
                "avg_fill": round(pos["avg_fill"], 4),
                "base_price": pos["base_price"],
                "total_bet": pos["total_bet"],
                "btc_at_entry": pos["btc_at_entry"],
                "price_to_beat": pos["price_to_beat"],
                "diff": round(pos["diff"], 2),
                "bankroll": self.bankroll,
                "hold_seconds": round(elapsed, 1),
                "orders_placed": len(pos["orders"]),
                "mode": mode,
                "question": pos["question"],
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except Exception:
            pass

        self._write_summary()
        self.position = None

        icon = "🟢" if pnl > 0 else "🔴"
        asyncio.create_task(send_telegram(
            f"{icon} <b>{BOT_NAME} #{pos['id']}</b>\n"
            f"[{mode}] BTC {pos['direction']} @ avg ${pos['avg_fill']:.3f} → {result}\n"
            f"P&L: ${pnl:+.2f} ({ret_pct:+.0f}%) | {pos['filled_shares']:.0f}sh filled\n"
            f"BTC: ${pos['btc_at_entry']:,.2f} vs beat ${pos['price_to_beat']:,.2f} (${pos['diff']:+,.2f})\n"
            f"Bank: ${self.bankroll:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%)"))

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        sb = self.starting_bankroll if self.starting_bankroll > 0 else 100
        summary = {
            "bot": BOT_NAME,
            "mode": "LIVE" if self.engine and not PAPER_MODE else "PAPER",
            "elapsed_minutes": round(elapsed, 1),
            "trades": total, "wins": self.wins, "losses": self.losses,
            "win_rate": round(wr, 1),
            "bankroll": round(self.bankroll, 2),
            "starting_bankroll": round(sb, 2),
            "pnl_total": round(self.bankroll - sb, 2),
            "max_dd": round(self.max_dd, 1),
            "signals_detected": self.signals_detected,
            "signals_up": self.signals_up,
            "signals_down": self.signals_down,
            "signal_threshold": SIGNAL_THRESHOLD,
            "bet_pct": BET_PCT,
            "ladder_steps": LADDER_STEPS,
            "btc_price": round(self.coinbase.price, 2),
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(SUMMARY_FILE, summary)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        sb = self.starting_bankroll if self.starting_bankroll > 0 else 100
        pnl = self.bankroll - sb
        bet = round(self.bankroll * BET_PCT, 2)
        mode = "LIVE" if self.engine and not PAPER_MODE else "PAPER"

        ptb = self.coinbase.window_open_price
        diff = self.coinbase.price - ptb if ptb > 0 else 0
        signal = "UP" if diff >= SIGNAL_THRESHOLD else "DOWN" if diff <= -SIGNAL_THRESHOLD else "NONE"

        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {BOT_NAME} | {elapsed:.0f}min | {mode}")
        print(f"  Strategy: BTC $100 gap → ladder 5 maker bids on leading side")
        print(f"  BTC: ${self.coinbase.price:,.2f} | Beat: ${ptb:,.2f} | Diff: ${diff:+,.2f} | Signal: {signal}")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f} | DD: {self.max_dd:.0f}%")
        print(f"  Bet: ${bet:.2f} ({int(BET_PCT*100)}%) / {LADDER_STEPS} = ${bet/LADDER_STEPS:.2f} each")
        print(f"  Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}%")
        print(f"  Signals: {self.signals_detected} (UP:{self.signals_up} DOWN:{self.signals_down})")
        if self.position:
            pos = self.position
            held = time.time() - pos["entry_time"]
            print(f"  OPEN: #{pos['id']} {pos['direction']} | {pos['filled_shares']:.0f}sh @ ${pos['avg_fill']:.3f} | {held:.0f}s")
        print()


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        if not PAPER_MODE and bot.engine:
            await bot.sync_bankroll()
        bot.print_status()


async def main():
    mode = "LIVE" if not PAPER_MODE else "PAPER"
    print("=" * 65)
    print(f"  {BOT_NAME} — BTC Directional Ladder [{mode}]")
    print("=" * 65)
    print(f"  Signal: Coinbase BTC ±${SIGNAL_THRESHOLD:.0f} from window open price")
    print(f"  Action: 5 GTC maker bids on leading side (0% fee)")
    print(f"  Ladder: +$0.01 to +$0.05 above current price")
    print(f"  Bet: {int(BET_PCT*100)}% of wallet / {LADDER_STEPS} orders")
    print(f"  Hold to resolution ($1.00 or $0.00)")
    print()
    if mode == "LIVE":
        print(f"  *** LIVE MODE — REAL MONEY AT RISK ***")
    else:
        print(f"  Paper mode — set BTC_LADDER_LIVE=1 to go live")
    print()

    bot = BTCLadderBot()
    bot.init_clob()

    if not PAPER_MODE and bot.engine:
        await bot.sync_bankroll()
        log_msg(f"[INIT] LIVE — Wallet: ${bot.bankroll:.2f}")
    else:
        log_msg(f"[INIT] PAPER — Bank: ${bot.bankroll:.2f}")

    bot._write_summary()

    asyncio.create_task(send_telegram(
        f"🎯 <b>{BOT_NAME}</b> [{mode}]\n"
        f"BTC ±${SIGNAL_THRESHOLD:.0f} gap → 5 maker ladder\n"
        f"Bet: {int(BET_PCT*100)}% / {LADDER_STEPS} orders\n"
        f"Bank: ${bot.bankroll:.2f}"))

    await asyncio.gather(
        bot.coinbase.run(),
        bot.run(),
        run_status(bot))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
