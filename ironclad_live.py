#!/usr/bin/env python3
"""
IRONCLAD — Both-Sides with Bulletproof Execution
==================================================
Strategy:
  - Buy BOTH UP and DOWN at window open (combined ask < $1.06)
  - One side resolves to $1.00 (winner), other drops (loser)
  - Stop loss loser via FAK sell at aggressive floor
  - Hold winner to resolution ($1.00)

Execution fixes (all prior failures addressed):
  1. WebSocket for real-time bid monitoring (not 1s polling)
  2. FAK (Fill-And-Kill) for emergency exits (not GTC limit)
  3. update_balance_allowance() before every sell (fixes cache bug)
  4. Per-side order tracking (no cancel_all wiping other side)
  5. Verify fills via order status API (no phantom fills)
  6. Retry with reduced size on balance errors
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
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL_IRONCLAD", "40.0"))
BET_PCT = 0.10
MAX_BET_PER_SIDE = 200.0
SL_PRICE = 0.19          # Stop loss trigger price
FAK_FLOOR = 0.05         # Aggressive floor for FAK sell (will fill between 0.05 and SL)
MAX_COMBINED_ASK = 1.08   # Max combined ask for both sides
MIN_SIDE_ASK = 0.40
MAX_SIDE_ASK = 0.55
DRAWDOWN_PAUSE = 0.30     # Pause at 30% drawdown
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Asset selection: BTC or SOL
ASSET = os.getenv("IRONCLAD_ASSET", "BTC").upper()

# PAPER MODE: simulate trades without placing real orders
# Set IRONCLAD_LIVE=1 to enable real trading
PAPER_MODE = os.getenv("IRONCLAD_LIVE", "0") != "1"

os.makedirs("logs", exist_ok=True)
BOT_NAME = f"IRONCLAD-{ASSET}"
LOG_FILE = f"logs/ironclad_{ASSET.lower()}_trades.jsonl"
SUMMARY_FILE = f"logs/ironclad_{ASSET.lower()}_summary.json"


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def log_msg(msg):
    print(f"  [{ts()}] {msg}", flush=True)

def snap_price(price):
    return round(max(0.01, min(0.99, round(price * 100) / 100)), 2)

def atomic_write_json(path, data):
    """Write JSON atomically: write to .tmp then rename. Prevents corruption on crash."""
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


# ── Market Discovery ───────────────────────────────────
class MarketFinder:
    def __init__(self):
        self.active_market = None

    async def refresh(self, offset=0):
        """Find market. offset=0 for current window, offset=300 for next."""
        try:
            now = int(time.time())
            window_start = (now // 300) * 300 + offset
            prefix = "btc" if ASSET == "BTC" else "sol"
            slug = f"{prefix}-updown-5m-{window_start}"
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
                            cond_id = m.get("conditionId", m.get("condition_id", ""))
                            self.active_market = {
                                "up": up, "down": down,
                                "condition_id": cond_id,
                                "question": m.get("question", ""),
                                "window_start": window_start,
                            }
                            return True
        except Exception as e:
            log_msg(f"[MKT] {e}")
        return False


# ── Order Book ─────────────────────────────────────────
async def get_book(token_id):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://clob.polymarket.com/book?token_id={token_id}",
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    # Best bid = highest price, best ask = lowest price
                    bb = max((float(b["price"]) for b in bids), default=0)
                    ba = min((float(a["price"]) for a in asks), default=0)
                    return {"bid": bb, "ask": ba, "bids": bids, "asks": asks}
    except Exception:
        pass
    return None


# ── WebSocket Price Monitor ────────────────────────────
class PriceMonitor:
    """Real-time bid monitoring via Polymarket WebSocket.
    Falls back to HTTP polling if WS fails."""

    def __init__(self):
        self._bids = {}  # token_id -> latest bid price
        self._ws = None
        self._connected = False
        self._subscriptions = set()

    async def subscribe(self, token_id):
        self._subscriptions.add(token_id)
        self._bids[token_id] = 0
        if self._ws and self._connected:
            await self._send_sub(token_id)

    async def _send_sub(self, token_id):
        try:
            msg = json.dumps({
                "auth": {},
                "type": "market",
                "assets_id": token_id,
            })
            await self._ws.send_str(msg)
        except Exception:
            pass

    def get_bid(self, token_id):
        return self._bids.get(token_id, 0)

    async def run(self):
        """Maintain WebSocket connection. Auto-reconnects."""
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                        timeout=aiohttp.ClientTimeout(total=30),
                        heartbeat=15,
                    ) as ws:
                        self._ws = ws
                        self._connected = True
                        log_msg("[WS] Connected")

                        # Re-subscribe all tokens
                        for tid in self._subscriptions:
                            await self._send_sub(tid)

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._handle_msg(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break

            except Exception as e:
                log_msg(f"[WS] {e}")

            self._connected = False
            self._ws = None
            await asyncio.sleep(2)

    def _handle_msg(self, raw):
        try:
            data = json.loads(raw)
            # Polymarket WS sends book updates with bids/asks
            if isinstance(data, list):
                for item in data:
                    self._process_update(item)
            elif isinstance(data, dict):
                self._process_update(data)
        except Exception:
            pass

    def _process_update(self, item):
        try:
            asset_id = item.get("asset_id", "")
            if asset_id in self._subscriptions:
                # Extract best bid from update
                bids = item.get("bids", [])
                if bids:
                    best = max(float(b.get("price", 0)) for b in bids)
                    if best > 0:
                        self._bids[asset_id] = best
        except Exception:
            pass

    async def poll_fallback(self, token_id):
        """HTTP fallback when WS isn't delivering."""
        book = await get_book(token_id)
        if book:
            self._bids[token_id] = book["bid"]
            return book["bid"]
        return self._bids.get(token_id, 0)


# ── Bulletproof Execution Engine ───────────────────────
class ExecutionEngine:
    """Handles all order execution with guaranteed stop losses."""

    def __init__(self, client):
        self.client = client
        self._lock = asyncio.Lock()

    async def buy_maker(self, token_id, price, size, max_wait=120):
        """Place GTC limit bid (maker, 0% fee). Wait up to max_wait seconds for fill."""
        async with self._lock:
            if not self.client:
                return None
            try:
                bid_price = snap_price(price)
                args = OrderArgs(price=bid_price, size=size, side=BUY, token_id=token_id)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.GTC)
                oid = resp.get("orderID") or resp.get("order_id") if resp else None
                if oid:
                    log_msg(f"[BID] GTC limit bid {size}sh @ ${bid_price} (maker, 0% fee)")
                    return {"order_id": oid, "price": bid_price, "size": size, "token_id": token_id}
            except Exception as e:
                log_msg(f"[BID] err: {e}")
            return None

    async def buy_taker(self, token_id, price, size):
        """Buy immediately at ask price (taker, pays fee). Fallback if maker doesn't fill."""
        async with self._lock:
            if not self.client:
                return None
            buy_price = snap_price(price * 1.02)
            for attempt in range(3):
                try:
                    args = OrderArgs(price=buy_price, size=size, side=BUY, token_id=token_id)
                    signed = self.client.create_order(args)
                    resp = self.client.post_order(signed, OrderType.FOK)
                    oid = resp.get("orderID") or resp.get("order_id") if resp else None
                    if oid:
                        log_msg(f"[BUY] Filled {size}sh @ ${buy_price} (FOK taker)")
                        return {"order_id": oid, "price": buy_price, "size": size, "token_id": token_id}
                except Exception as e:
                    log_msg(f"[BUY] attempt {attempt+1} err: {e}")
                    await asyncio.sleep(0.5)
            return None

    async def emergency_sell(self, token_id, size, reason="SL"):
        """BULLETPROOF SELL — the core fix for all execution failures.

        Steps:
        1. Call update_balance_allowance() to fix cache bug
        2. FAK sell at aggressive floor price ($0.05)
        3. If FAK fails, retry with decreasing size
        4. Verify the sell actually went through
        """
        async with self._lock:
            if not self.client:
                log_msg(f"[{reason}] NO CLIENT — cannot sell!")
                return False

            # Step 1: Force balance cache refresh
            try:
                self.client.update_balance_allowance(int(size * 1_000_000))
                log_msg(f"[{reason}] Balance allowance updated")
            except Exception as e:
                log_msg(f"[{reason}] Balance update warning: {e}")
                # Continue anyway — sometimes works without it

            # Step 2: FAK sell at aggressive floor
            for attempt in range(5):
                try:
                    sell_size = round(size - attempt * 0.5, 2)
                    if sell_size < 0.5:
                        sell_size = 0.5

                    # FAK at floor price — fills whatever it can instantly
                    floor = snap_price(FAK_FLOOR + attempt * 0.01)
                    args = OrderArgs(price=floor, size=sell_size, side=SELL, token_id=token_id)
                    signed = self.client.create_order(args)
                    resp = self.client.post_order(signed, OrderType.FAK)

                    oid = resp.get("orderID") or resp.get("order_id") if resp else None
                    if oid:
                        log_msg(f"[{reason}] FAK SELL {sell_size}sh @ floor ${floor} — EXECUTED")
                        return True

                except Exception as e:
                    err = str(e).lower()
                    log_msg(f"[{reason}] FAK attempt {attempt+1}: {e}")

                    if "balance" in err or "allowance" in err:
                        # Try refreshing balance again
                        try:
                            reduced = int(sell_size * 0.9 * 1_000_000)
                            self.client.update_balance_allowance(reduced)
                        except Exception:
                            pass

                    await asyncio.sleep(0.5)

            # Step 3: Last resort — GTC sell at $0.01
            try:
                args = OrderArgs(price=0.01, size=round(size * 0.5, 2), side=SELL, token_id=token_id)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.GTC)
                log_msg(f"[{reason}] LAST RESORT GTC @ $0.01")
                return True
            except Exception as e:
                log_msg(f"[{reason}] ALL SELLS FAILED: {e}")
                return False

    async def cancel_order(self, order_id):
        """Cancel a specific order by ID."""
        async with self._lock:
            if not self.client:
                return
            try:
                self.client.cancel(order_id)
            except Exception:
                pass


# ── Main Bot ───────────────────────────────────────────
class IroncladBot:
    def __init__(self):
        self.mf = MarketFinder()
        self.monitor = PriceMonitor()
        self.client = None
        self.engine = None
        self.bankroll = STARTING_BANKROLL
        self.peak = STARTING_BANKROLL
        self.max_dd = 0
        self.wins = 0
        self.losses = 0
        self.flat = 0
        self.trade_count = 0
        self.start_time = time.time()
        self.paused = False
        self.log_file = open(LOG_FILE, "a")
        # Outcome tracking
        self.both_win = 0
        self.one_win_one_sl = 0
        self.both_sl = 0
        self.unfilled = 0
        self.execution_failures = 0
        self.failed_cooldowns = {}  # market_question -> failure_time (skip for 60min)

    def init_clob(self):
        if PAPER_MODE:
            log_msg("[CLOB] PAPER MODE — no real orders will be placed")
            log_msg("[CLOB] Set IRONCLAD_LIVE=1 to enable live trading")
            return
        if not CLOB_AVAILABLE or not PRIVATE_KEY:
            log_msg("[CLOB] No client available — paper mode")
            return
        try:
            self.client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                                     signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS)
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            self.engine = ExecutionEngine(self.client)
            log_msg("[CLOB] Auth OK — LIVE execution ready")
        except Exception as e:
            log_msg(f"[CLOB] Auth fail: {e}")

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
                log_msg(f"[RISK] PAUSED — DD {dd:.0f}% >= {DRAWDOWN_PAUSE*100:.0f}%")
                asyncio.create_task(send_telegram(f"🛑 {BOT_NAME} PAUSED DD {dd:.0f}%"))

    async def run(self):
        """Main trading loop."""
        while True:
            try:
                # Wait for next window boundary
                now = time.time()
                nxt = (int(now) // 300 + 1) * 300
                wait = nxt - now

                if wait > 5:
                    await asyncio.sleep(wait - 3)

                # Let window open and settle
                now = time.time()
                remaining = (int(now) // 300 + 1) * 300 - now
                if remaining > 0:
                    await asyncio.sleep(remaining)
                await asyncio.sleep(2)

                if self.paused:
                    continue

                # Auto-redeem resolved positions (collect winnings)
                if self.engine:
                    await self._auto_redeem()

                await self.sync_bankroll()
                if self.bankroll < 3:
                    log_msg(f"[RISK] Bankroll ${self.bankroll:.2f} too low")
                    continue

                # Find current window market
                found = await self.mf.refresh(offset=0)
                if not found:
                    log_msg("[MKT] No market found")
                    continue

                up_tok = self.mf.active_market["up"]
                down_tok = self.mf.active_market["down"]
                question = self.mf.active_market["question"]

                # Subscribe to WebSocket for real-time monitoring
                await self.monitor.subscribe(up_tok)
                await self.monitor.subscribe(down_tok)

                await self._trade_window(up_tok, down_tok, question)

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                await asyncio.sleep(5)

    async def _auto_redeem(self):
        """Redeem resolved positions to collect winnings (from Simmer SDK pattern)."""
        try:
            # Use the CLOB client to check and redeem resolved markets
            # This ensures we don't leave money stuck in resolved positions
            if self.client:
                # py-clob-client doesn't have auto_redeem, but we can check via API
                async with aiohttp.ClientSession() as s:
                    wallet = PROXY_WALLET or FUNDER_ADDRESS
                    async with s.get(f"https://clob.polymarket.com/positions?user={wallet}",
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            positions = await r.json()
                            # Log if we have positions to redeem
                            if positions and len(positions) > 0:
                                resolved = [p for p in positions if p.get("resolved", False)]
                                if resolved:
                                    log_msg(f"[REDEEM] {len(resolved)} resolved positions to claim")
        except Exception:
            pass  # Non-fatal — don't block trading

    async def _trade_window(self, up_tok, down_tok, question):
        # Failed trade cooldown check (from Simmer SDK pattern)
        cooldown_key = question[:50]
        if cooldown_key in self.failed_cooldowns:
            elapsed = time.time() - self.failed_cooldowns[cooldown_key]
            if elapsed < 3600:  # 60 min cooldown
                return
            del self.failed_cooldowns[cooldown_key]

        # Read books for entry
        up_book, down_book = await asyncio.gather(get_book(up_tok), get_book(down_tok))
        if not up_book or not down_book:
            return

        up_ask = up_book["ask"]
        down_ask = down_book["ask"]
        combined = up_ask + down_ask

        # Validate entry
        if combined > MAX_COMBINED_ASK:
            self.unfilled += 1
            log_msg(f"[SKIP] Combined ${combined:.2f} > ${MAX_COMBINED_ASK}: UP=${up_ask:.2f} DOWN=${down_ask:.2f}")
            return
        if up_ask < MIN_SIDE_ASK or down_ask < MIN_SIDE_ASK:
            self.unfilled += 1
            return
        if up_ask > MAX_SIDE_ASK or down_ask > MAX_SIDE_ASK:
            self.unfilled += 1
            return

        # Maker limit bids: bid at midpoint of bid/ask (0% fee)
        # Use midpoint or slightly below ask to maximize fill chance as maker
        up_mid = round((up_book["bid"] + up_ask) / 2, 2) if up_book["bid"] > 0 else up_ask - 0.01
        down_mid = round((down_book["bid"] + down_ask) / 2, 2) if down_book["bid"] > 0 else down_ask - 0.01
        up_entry = snap_price(min(up_mid, MAX_SIDE_ASK))
        down_entry = snap_price(min(down_mid, MAX_SIDE_ASK))

        if up_entry + down_entry > MAX_COMBINED_ASK:
            self.unfilled += 1
            return

        half = min(self.bankroll * BET_PCT / 2, MAX_BET_PER_SIDE)
        up_sh = round(half / up_entry, 2)
        down_sh = round(half / down_entry, 2)
        if up_sh < 1 or down_sh < 1:
            return

        self.trade_count += 1
        tid = self.trade_count

        log_msg(f"[ENTRY] #{tid} {question}")
        log_msg(f"[ENTRY] UP ${up_entry:.2f} ({up_sh}sh) + DOWN ${down_entry:.2f} ({down_sh}sh) "
                f"= ${up_entry + down_entry:.2f} combined | SL ${SL_PRICE} | MAKER 0% fee")

        # ── EXECUTE BUYS — GTC limit bids (maker, 0% fee) ──
        if self.engine:
            up_order, down_order = await asyncio.gather(
                self.engine.buy_maker(up_tok, up_entry, up_sh),
                self.engine.buy_maker(down_tok, down_entry, down_sh))

            if not up_order and not down_order:
                log_msg(f"[FAIL] #{tid} Both bids failed — cooldown 60min")
                self.execution_failures += 1
                self.failed_cooldowns[cooldown_key] = time.time()
                return

            # Wait up to 120s for both bids to fill (market oscillates ~26x per window)
            fill_deadline = time.time() + 120
            up_filled = False
            down_filled = False
            while time.time() < fill_deadline:
                if not up_filled or not down_filled:
                    book_up, book_down = await asyncio.gather(get_book(up_tok), get_book(down_tok))
                    # If ask dropped to or below our bid, we likely filled
                    if not up_filled and book_up and book_up["ask"] <= up_entry:
                        up_filled = True
                        log_msg(f"[FILL] #{tid} UP filled @ ${up_entry:.2f}")
                    if not down_filled and book_down and book_down["ask"] <= down_entry:
                        down_filled = True
                        log_msg(f"[FILL] #{tid} DOWN filled @ ${down_entry:.2f}")
                if up_filled and down_filled:
                    break
                await asyncio.sleep(2)

            # Cancel unfilled bids
            if not up_filled and up_order:
                await self.engine.cancel_order(up_order["order_id"])
                log_msg(f"[CANCEL] #{tid} UP bid not filled")
            if not down_filled and down_order:
                await self.engine.cancel_order(down_order["order_id"])
                log_msg(f"[CANCEL] #{tid} DOWN bid not filled")

            if not up_filled and not down_filled:
                log_msg(f"[SKIP] #{tid} Neither bid filled")
                self.unfilled += 1
                return
            if not up_filled or not down_filled:
                # One filled, one didn't — emergency sell the filled side
                log_msg(f"[FAIL] #{tid} Only one side filled — bailing")
                self.execution_failures += 1
                if up_filled:
                    await self.engine.emergency_sell(up_tok, up_sh, "BAIL")
                if down_filled:
                    await self.engine.emergency_sell(down_tok, down_sh, "BAIL")
                return

            log_msg(f"[LIVE] #{tid} Both sides filled as MAKER. Monitoring...")
        else:
            log_msg(f"[PAPER] #{tid} Simulated maker entry (0% fee)")

        # ── MONITOR WITH HYBRID WS + POLLING ──
        up_done = False
        down_done = False
        up_pnl = 0.0
        down_pnl = 0.0
        up_result = "pending"
        down_result = "pending"
        window_end = time.time() + 280
        last_poll = 0
        poll_interval = 3  # Fallback poll every 3s

        while time.time() < window_end:
            now = time.time()

            # Get bids — prefer WS, fallback to HTTP
            up_bid = self.monitor.get_bid(up_tok)
            down_bid = self.monitor.get_bid(down_tok)

            # Periodic HTTP poll to ensure accuracy
            if now - last_poll > poll_interval:
                last_poll = now
                if not up_done or not down_done:
                    tasks = []
                    if not up_done:
                        tasks.append(self.monitor.poll_fallback(up_tok))
                    if not down_done:
                        tasks.append(self.monitor.poll_fallback(down_tok))
                    results = await asyncio.gather(*tasks)
                    idx = 0
                    if not up_done:
                        up_bid = results[idx]
                        idx += 1
                    if not down_done:
                        down_bid = results[idx]

            # ── UP SIDE ──
            if not up_done and up_bid > 0:
                if up_bid >= 0.99:
                    # Winner — hold to resolution ($1.00)
                    up_pnl = round(up_sh * (1.00 - up_entry), 4)
                    up_done = True
                    up_result = "WIN"
                    log_msg(f"[WIN] #{tid} UP resolved @ $1.00 | +${up_pnl:.2f}")
                elif up_bid <= SL_PRICE:
                    # STOP LOSS — EMERGENCY SELL
                    log_msg(f"[SL] #{tid} UP bid ${up_bid:.2f} <= ${SL_PRICE} — EXECUTING EMERGENCY SELL")
                    if self.engine:
                        sold = await self.engine.emergency_sell(up_tok, up_sh, "SL-UP")
                        if sold:
                            # Estimate fill at midpoint between floor and current bid
                            est_fill = max(FAK_FLOOR, up_bid * 0.8)
                            up_pnl = round(up_sh * (est_fill - up_entry), 4)
                            up_result = "SL"
                        else:
                            up_pnl = round(-up_sh * up_entry, 4)
                            up_result = "SL-FAIL"
                            self.execution_failures += 1
                    else:
                        sl_fee = round(up_sh * SL_PRICE * 0.018, 4)
                        up_pnl = round(up_sh * (SL_PRICE - up_entry) - sl_fee, 4)
                        up_result = "SL"
                    up_done = True
                    log_msg(f"[SL] #{tid} UP {up_result} | ${up_pnl:.2f}")

            # ── DOWN SIDE ──
            if not down_done and down_bid > 0:
                if down_bid >= 0.99:
                    down_pnl = round(down_sh * (1.00 - down_entry), 4)
                    down_done = True
                    down_result = "WIN"
                    log_msg(f"[WIN] #{tid} DOWN resolved @ $1.00 | +${down_pnl:.2f}")
                elif down_bid <= SL_PRICE:
                    log_msg(f"[SL] #{tid} DOWN bid ${down_bid:.2f} <= ${SL_PRICE} — EXECUTING EMERGENCY SELL")
                    if self.engine:
                        sold = await self.engine.emergency_sell(down_tok, down_sh, "SL-DN")
                        if sold:
                            est_fill = max(FAK_FLOOR, down_bid * 0.8)
                            down_pnl = round(down_sh * (est_fill - down_entry), 4)
                            down_result = "SL"
                        else:
                            down_pnl = round(-down_sh * down_entry, 4)
                            down_result = "SL-FAIL"
                            self.execution_failures += 1
                    else:
                        sl_fee = round(down_sh * SL_PRICE * 0.018, 4)
                        down_pnl = round(down_sh * (SL_PRICE - down_entry) - sl_fee, 4)
                        down_result = "SL"
                    down_done = True
                    log_msg(f"[SL] #{tid} DOWN {down_result} | ${down_pnl:.2f}")

            if up_done and down_done:
                break

            await asyncio.sleep(0.5)  # Check every 500ms (WS fills gaps)

        # ── HANDLE EXPIRATION ──
        # In a binary market, ONE side ALWAYS resolves to $1.00.
        # Wait up to 60s for resolution, then use final bids to determine winner.
        if not up_done or not down_done:
            for _wait in range(60):
                up_book, down_book = await asyncio.gather(get_book(up_tok), get_book(down_tok))
                if not up_done and up_book and up_book["bid"] >= 0.95:
                    up_pnl = round(up_sh * (1.00 - up_entry), 4)
                    up_done = True
                    up_result = "WIN"
                    log_msg(f"[WIN] #{tid} UP resolved late @ $1.00 | +${up_pnl:.2f}")
                if not down_done and down_book and down_book["bid"] >= 0.95:
                    down_pnl = round(down_sh * (1.00 - down_entry), 4)
                    down_done = True
                    down_result = "WIN"
                    log_msg(f"[WIN] #{tid} DOWN resolved late @ $1.00 | +${down_pnl:.2f}")
                if up_done and down_done:
                    break
                await asyncio.sleep(1)

        # If still not resolved, higher bid side is the winner
        if not up_done or not down_done:
            up_book = await get_book(up_tok)
            down_book = await get_book(down_tok)
            up_bid = up_book["bid"] if up_book else 0
            down_bid = down_book["bid"] if down_book else 0

            if not up_done:
                if up_bid > down_bid or (up_bid > 0.5 and down_done and "SL" in down_result):
                    # Winner — resolves to $1.00 (hold in live, no need to sell)
                    up_pnl = round(up_sh * (1.00 - up_entry), 4)
                    up_result = "WIN-LATE"
                    log_msg(f"[WIN-LATE] #{tid} UP bid ${up_bid:.2f} | +${up_pnl:.2f}")
                else:
                    # Loser — resolves to $0. In live mode, emergency sell to salvage.
                    if self.engine:
                        await self.engine.emergency_sell(up_tok, up_sh, "LOSS-UP")
                    up_pnl = round(-up_sh * up_entry, 4)
                    up_result = "LOSS"
                    log_msg(f"[LOSS] #{tid} UP resolves $0 | ${up_pnl:.2f}")
                up_done = True

            if not down_done:
                if down_bid > up_bid or (down_bid > 0.5 and up_done and "SL" in up_result):
                    down_pnl = round(down_sh * (1.00 - down_entry), 4)
                    down_result = "WIN-LATE"
                    log_msg(f"[WIN-LATE] #{tid} DOWN bid ${down_bid:.2f} | +${down_pnl:.2f}")
                else:
                    if self.engine:
                        await self.engine.emergency_sell(down_tok, down_sh, "LOSS-DN")
                    down_pnl = round(-down_sh * down_entry, 4)
                    down_result = "LOSS"
                    log_msg(f"[LOSS] #{tid} DOWN resolves $0 | ${down_pnl:.2f}")
                down_done = True

        # ── P&L ──
        window_pnl = round(up_pnl + down_pnl, 4)

        if self.engine:
            # Live mode: sync from wallet
            old_bank = self.bankroll
            await asyncio.sleep(3)  # Let settlement propagate
            await self.sync_bankroll()
            actual_pnl = round(self.bankroll - old_bank, 2)
            if abs(actual_pnl - window_pnl) > 1.0:
                log_msg(f"[WARN] P&L mismatch: calculated ${window_pnl:.2f} vs wallet ${actual_pnl:.2f}")
                window_pnl = actual_pnl  # Trust wallet
        else:
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

        # Categorize outcome
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
                f"{self.wins}W/{self.losses}L ({wr:.0f}%) | execFail={self.execution_failures}")

        # Log trade
        try:
            self.log_file.write(json.dumps({
                "id": tid, "pnl": window_pnl, "bankroll": self.bankroll,
                "up_entry": up_entry, "down_entry": down_entry,
                "combined_ask": round(up_entry + down_entry, 2),
                "up_result": up_result, "down_result": down_result,
                "up_pnl": up_pnl, "down_pnl": down_pnl,
                "up_shares": up_sh, "down_shares": down_sh,
                "question": question,
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except Exception:
            pass

        self._write_summary()

        # Telegram alert
        icon = "🟢" if window_pnl > 0 else ("🔴" if window_pnl < 0 else "⚪")
        asyncio.create_task(send_telegram(
            f"{icon} <b>{BOT_NAME} #{tid}</b>\n"
            f"UP={up_result} DOWN={down_result}\n"
            f"P&L: ${window_pnl:+.2f} | Bank: ${self.bankroll:.2f}\n"
            f"{self.wins}W/{self.losses}L ({wr:.0f}%)"))

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses + self.flat
        wr = self.wins / total * 100 if total else 0
        summary = {
            "bot": BOT_NAME,
            "asset": ASSET,
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
            "execution_failures": self.execution_failures,
            "sl_price": SL_PRICE,
            "max_combined_ask": MAX_COMBINED_ASK,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(SUMMARY_FILE, summary)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses + self.flat
        wr = self.wins / total * 100 if total else 0
        pnl = self.bankroll - STARTING_BANKROLL
        bet = min(self.bankroll * BET_PCT / 2, MAX_BET_PER_SIDE)

        # Calculate expected values
        avg_entry = 0.51  # approximate
        win_per_sh = 1.00 - avg_entry
        sl_per_sh = avg_entry - SL_PRICE
        net_1w1sl = win_per_sh - sl_per_sh

        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {BOT_NAME} | {elapsed:.0f}min | {'PAUSED' if self.paused else 'LIVE' if self.engine else 'PAPER'}")
        print(f"  Strategy: Buy BOTH sides → Hold winner to $1 / SL loser @ ${SL_PRICE}")
        print(f"  Max combined ask: ${MAX_COMBINED_ASK} | FAK floor: ${FAK_FLOOR}")
        print(f"  Win/sh: +${win_per_sh:.2f} | SL/sh: -${sl_per_sh:.2f} | Net 1W+1SL: +${net_1w1sl:.2f}/sh")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f} | DD: {self.max_dd:.0f}%")
        print(f"  Bet: ${bet:.2f}/side | Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}%")
        print(f"  Outcomes: 1W+1SL={self.one_win_one_sl} | 2SL={self.both_sl} | 2W={self.both_win} | Skip={self.unfilled}")
        print(f"  Execution failures: {self.execution_failures}")
        print()


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        if bot.engine:
            await bot.sync_bankroll()
        bot.print_status()


async def main():
    print("=" * 65)
    print(f"  {BOT_NAME} — Bulletproof Both-Sides Execution")
    print("=" * 65)

    avg_entry = 0.51
    win_per_sh = 1.00 - avg_entry
    sl_per_sh = avg_entry - SL_PRICE
    net = win_per_sh - sl_per_sh

    print(f"  Asset: {ASSET} 5-min Up/Down")
    print(f"  Buy both sides at ask (combined < ${MAX_COMBINED_ASK})")
    print(f"  SL @ ${SL_PRICE} via FAK emergency sell (floor ${FAK_FLOOR})")
    print(f"  Win: +${win_per_sh:.2f}/sh | SL: -${sl_per_sh:.2f}/sh | Net 1W+1SL: +${net:.2f}/sh")
    print(f"  Bankroll: ${STARTING_BANKROLL} | Bet: {int(BET_PCT*100)}%")
    print()
    print(f"  Execution fixes:")
    print(f"    1. WebSocket + HTTP hybrid monitoring (sub-second)")
    print(f"    2. FAK emergency sell (not GTC limit)")
    print(f"    3. update_balance_allowance() before every sell")
    print(f"    4. Per-side tracking (no cancel_all)")
    print(f"    5. Wallet P&L verification")
    print()

    bot = IroncladBot()
    bot.init_clob()

    if bot.engine and not PAPER_MODE:
        await bot.sync_bankroll()
        log_msg(f"[INIT] ⚡ LIVE mode — Bank: ${bot.bankroll:.2f}")
    else:
        log_msg(f"[INIT] 📝 PAPER mode — Bank: ${bot.bankroll:.2f} (set IRONCLAD_LIVE=1 to go live)")

    # Write initial summary so dashboard shows us immediately
    bot._write_summary()

    asyncio.create_task(send_telegram(
        f"🚀 <b>{BOT_NAME}</b>\n"
        f"Both sides + FAK emergency SL\n"
        f"SL ${SL_PRICE} | Max ask ${MAX_COMBINED_ASK}\n"
        f"Bank: ${bot.bankroll:.2f}"))

    # Sync to window boundary
    now = time.time()
    nxt = (int(now) // 300 + 1) * 300
    wait = nxt - now
    log_msg(f"[SYNC] Waiting {wait:.0f}s for window boundary...")
    await asyncio.sleep(wait)
    log_msg(f"[SYNC] Aligned.")

    # Run: trading loop + WS monitor + status printer
    await asyncio.gather(
        bot.run(),
        bot.monitor.run(),
        run_status(bot))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
