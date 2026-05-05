#!/usr/bin/env python3
"""
LATENCY ARB — Coinbase-to-Polymarket Latency Arbitrage
======================================================
Strategy:
  - Watch Coinbase BTC/USD websocket for real-time price
  - When BTC moves $X+ in Y seconds, Polymarket book lags 2-5s
  - Immediately buy the winning side before book adjusts
  - Exit: sell when book reprices OR hold to $1.00 resolution

Edge:
  - Coinbase price leads Polymarket book by 2-55 seconds
  - We react in <500ms, buy before book adjusts
  - Even 2nd/3rd in queue gets filled if there's liquidity

Uses IRONCLAD execution engine:
  - FAK orders for instant fills
  - update_balance_allowance() before sells
  - Per-order tracking, no cancel_all
  - Emergency sell with aggressive floor
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
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────
STARTING_BANKROLL = float(os.getenv("STARTING_BANKROLL_ARB", "40.0"))
BET_PCT = 0.10
MAX_BET = 200.0
FAK_FLOOR = 0.05

# Signal thresholds
MOVE_THRESHOLD = 50.0     # BTC must move $50+ to trigger (strong directional signal)
MOVE_WINDOW = 5.0         # ... within 5 seconds
COOLDOWN = 30.0           # seconds between trades
MAX_ASK = 0.60            # don't buy if ask > $0.60 (book already adjusted, no edge)
MIN_ASK = 0.35            # don't buy if ask < $0.35 (wrong side)
SL_PRICE = 0.20           # stop loss trigger
TP_PRICE = 0.85           # take profit (sell early before resolution)
SKIP_LOG_COOLDOWN = 10.0  # only log skips every 10s to reduce spam

DRAWDOWN_PAUSE = 0.30
MAX_TRADES_PER_WINDOW = 3  # max trades per 5-min window

USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PAPER_MODE = os.getenv("ARB_LIVE", "0") != "1"

os.makedirs("logs", exist_ok=True)
BOT_NAME = "LATENCY-ARB"
LOG_FILE = "logs/latency_arb_trades.jsonl"
SUMMARY_FILE = "logs/latency_arb_summary.json"


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
    """Real-time BTC/USD price from Coinbase websocket.
    Coinbase servers are US/EU — much lower latency from Ireland EC2 than Binance (Asia)."""

    def __init__(self):
        self.price = 0.0
        self.last_update = 0.0
        self._history = deque(maxlen=500)  # (timestamp, price) for momentum calc
        self.connected = False
        self.tick_count = 0
        self.window_open_price = 0.0  # BTC price at start of current 5-min window
        self.current_window = 0

    def get_move(self, seconds):
        """Get price move over last N seconds. Returns (move_dollars, direction)."""
        now = time.time()
        cutoff = now - seconds
        old_price = None
        for t, p in self._history:
            if t >= cutoff:
                old_price = p
                break
        if old_price is None or old_price == 0:
            return 0.0, "flat"
        move = self.price - old_price
        direction = "up" if move > 0 else "down" if move < 0 else "flat"
        return move, direction

    def get_window_move(self):
        """Get BTC % move from window open. This is the key signal."""
        if self.window_open_price == 0:
            return 0.0, 0.0, "flat"
        move = self.price - self.window_open_price
        pct = (move / self.window_open_price) * 100
        direction = "up" if move > 0 else "down" if move < 0 else "flat"
        return move, pct, direction

    def _check_window(self):
        """Track window open price for mispricing detection."""
        window = int(time.time()) // 300
        if window != self.current_window:
            self.current_window = window
            self.window_open_price = self.price
            if self.price > 0:
                log_msg(f"[CB] New window — open price ${self.price:,.2f}")

    async def run(self):
        """Connect to Coinbase Advanced Trade websocket."""
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        "wss://ws-feed.exchange.coinbase.com",
                        timeout=aiohttp.ClientTimeout(total=30),
                        heartbeat=15,
                    ) as ws:
                        # Subscribe to BTC-USD ticker
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
                                            self._history.append((time.time(), p))
                                            self.tick_count += 1
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
        self.active_market = None
        self.last_refresh = 0.0

    async def refresh(self, offset=0):
        try:
            now = int(time.time())
            window_start = (now // 300) * 300 + offset
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
                            self.active_market = {
                                "up": up, "down": down,
                                "question": m.get("question", ""),
                                "window_start": window_start,
                            }
                            self.last_refresh = time.time()
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
                    bb = max((float(b["price"]) for b in bids), default=0)
                    ba = min((float(a["price"]) for a in asks), default=0)
                    return {"bid": bb, "ask": ba}
    except Exception:
        pass
    return None


# ── Execution Engine (from IRONCLAD) ───────────────────
class ExecutionEngine:
    """Bulletproof order execution with FAK + balance cache fix."""

    def __init__(self, client):
        self.client = client
        self._lock = asyncio.Lock()

    async def buy_taker(self, token_id, price, size):
        """Buy immediately. Price includes 2% buffer for guaranteed fill."""
        async with self._lock:
            if not self.client:
                return None
            buy_price = snap_price(price * 1.02)  # 2% buffer
            for attempt in range(3):
                try:
                    args = OrderArgs(price=buy_price, size=size, side=BUY, token_id=token_id)
                    signed = self.client.create_order(args)
                    resp = self.client.post_order(signed, OrderType.FOK)
                    oid = resp.get("orderID") or resp.get("order_id") if resp else None
                    if oid:
                        log_msg(f"[BUY] Filled {size}sh @ ${buy_price} (FOK)")
                        return {"order_id": oid, "price": buy_price, "size": size, "token_id": token_id}
                    # FOK didn't fill — try FAK
                    args = OrderArgs(price=buy_price, size=size, side=BUY, token_id=token_id)
                    signed = self.client.create_order(args)
                    resp = self.client.post_order(signed, OrderType.FAK)
                    oid = resp.get("orderID") or resp.get("order_id") if resp else None
                    if oid:
                        log_msg(f"[BUY] Filled {size}sh @ ${buy_price} (FAK)")
                        return {"order_id": oid, "price": buy_price, "size": size, "token_id": token_id}
                except Exception as e:
                    log_msg(f"[BUY] attempt {attempt+1}: {e}")
                    await asyncio.sleep(0.3)
            return None

    async def emergency_sell(self, token_id, size, reason="SL"):
        """BULLETPROOF SELL: update_balance_allowance → FAK at aggressive floor."""
        async with self._lock:
            if not self.client:
                return False

            # Step 1: Force balance cache refresh
            try:
                self.client.update_balance_allowance(int(size * 1_000_000))
            except Exception as e:
                log_msg(f"[{reason}] Balance update warn: {e}")

            # Step 2: FAK sell at aggressive floor
            for attempt in range(5):
                try:
                    sell_size = round(size - attempt * 0.5, 2)
                    if sell_size < 0.5:
                        sell_size = 0.5
                    floor = snap_price(FAK_FLOOR + attempt * 0.01)
                    args = OrderArgs(price=floor, size=sell_size, side=SELL, token_id=token_id)
                    signed = self.client.create_order(args)
                    resp = self.client.post_order(signed, OrderType.FAK)
                    oid = resp.get("orderID") or resp.get("order_id") if resp else None
                    if oid:
                        log_msg(f"[{reason}] FAK SELL {sell_size}sh @ ${floor} — OK")
                        return True
                except Exception as e:
                    log_msg(f"[{reason}] FAK attempt {attempt+1}: {e}")
                    if "balance" in str(e).lower():
                        try:
                            self.client.update_balance_allowance(int(sell_size * 0.9 * 1_000_000))
                        except Exception:
                            pass
                    await asyncio.sleep(0.3)

            # Last resort
            try:
                args = OrderArgs(price=0.01, size=round(size * 0.5, 2), side=SELL, token_id=token_id)
                signed = self.client.create_order(args)
                self.client.post_order(signed, OrderType.GTC)
                log_msg(f"[{reason}] LAST RESORT GTC @ $0.01")
                return True
            except Exception as e:
                log_msg(f"[{reason}] ALL SELLS FAILED: {e}")
                return False

    async def sell_at_bid(self, token_id, size, min_price=0.01):
        """Sell at current bid price. For taking profit when book reprices."""
        async with self._lock:
            if not self.client:
                return False
            try:
                self.client.update_balance_allowance(int(size * 1_000_000))
            except Exception:
                pass
            try:
                sell_price = snap_price(max(min_price, 0.01))
                args = OrderArgs(price=sell_price, size=size, side=SELL, token_id=token_id)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.FAK)
                oid = resp.get("orderID") or resp.get("order_id") if resp else None
                if oid:
                    log_msg(f"[SELL] FAK {size}sh @ ${sell_price} — OK")
                    return True
            except Exception as e:
                log_msg(f"[SELL] {e}")
            return False


# ── Latency Arb Bot ────────────────────────────────────
class LatencyArbBot:
    def __init__(self):
        self.mf = MarketFinder()
        self.coinbase = CoinbaseFeed()
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
        self.last_trade_time = 0
        self.trades_this_window = 0
        self.current_window = 0
        self.signals_detected = 0
        self.signals_skipped = 0
        self.execution_failures = 0
        # Track open position
        self.position = None  # {side, token_id, entry_price, shares, entry_time}

    def init_clob(self):
        if PAPER_MODE:
            log_msg("[CLOB] PAPER MODE — no real orders")
            log_msg("[CLOB] Set ARB_LIVE=1 to enable live trading")
            return
        if not CLOB_AVAILABLE or not PRIVATE_KEY:
            log_msg("[CLOB] No client — paper mode")
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
                log_msg(f"[RISK] PAUSED — DD {dd:.0f}%")
                asyncio.create_task(send_telegram(f"🛑 {BOT_NAME} PAUSED DD {dd:.0f}%"))

    async def run(self):
        """Main loop: scan for signals every 100ms."""
        log_msg("[ARB] Waiting for Coinbase feed...")
        while not self.coinbase.connected or self.coinbase.price == 0:
            await asyncio.sleep(0.5)
        log_msg(f"[ARB] Coinbase live: BTC ${self.coinbase.price:,.2f}")

        while True:
            try:
                if self.paused:
                    await asyncio.sleep(5)
                    continue

                # Refresh market at window boundaries
                now = time.time()
                current_window = int(now) // 300
                if current_window != self.current_window:
                    self.current_window = current_window
                    self.trades_this_window = 0
                    await self.mf.refresh(offset=0)
                    if not self.mf.active_market:
                        await asyncio.sleep(5)
                        continue

                if not self.mf.active_market:
                    await self.mf.refresh(offset=0)
                    if not self.mf.active_market:
                        await asyncio.sleep(5)
                        continue

                # Check if we have an open position to manage
                if self.position:
                    await self._manage_position()
                    await asyncio.sleep(0.3)
                    continue

                # Check for signal — TWO methods:
                # 1. Window mispricing: BTC moved X% from open but token still cheap
                # 2. Rapid momentum: BTC moved $50+ in 5 seconds

                window_move, window_pct, window_dir = self.coinbase.get_window_move()
                rapid_move, rapid_dir = self.coinbase.get_move(MOVE_WINDOW)

                signal = False
                signal_dir = "flat"
                signal_move = 0
                signal_type = ""

                # Method 1: Window mispricing (primary — from oracle lag research)
                # If BTC is 0.07%+ from open, one side should be >= $0.62
                # If token is still < MAX_ASK, it's mispriced
                if abs(window_pct) >= 0.07 and self.coinbase.window_open_price > 0:
                    signal = True
                    signal_dir = window_dir
                    signal_move = window_move
                    signal_type = f"MISPRICE {window_pct:+.3f}%"

                # Method 2: Rapid momentum (secondary — original strategy)
                elif abs(rapid_move) >= MOVE_THRESHOLD:
                    signal = True
                    signal_dir = rapid_dir
                    signal_move = rapid_move
                    signal_type = f"MOMENTUM ${rapid_move:+.0f}/{MOVE_WINDOW}s"

                if signal:
                    self.signals_detected += 1

                    # Cooldown check
                    if now - self.last_trade_time < COOLDOWN:
                        await asyncio.sleep(0.1)
                        continue

                    # Max trades per window
                    if self.trades_this_window >= MAX_TRADES_PER_WINDOW:
                        await asyncio.sleep(0.1)
                        continue

                    # Don't trade in last 30s of window (too close to resolution)
                    window_elapsed = now - (int(now) // 300) * 300
                    if window_elapsed > 270:
                        await asyncio.sleep(0.1)
                        continue

                    await self._execute_signal(signal_dir, signal_move, signal_type)

                await asyncio.sleep(0.1)  # 100ms scan interval

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                await asyncio.sleep(2)

    async def _execute_signal(self, direction, move, signal_type=""):
        """BTC moved — buy the corresponding side on Polymarket."""
        mkt = self.mf.active_market
        if not mkt:
            return

        # BTC up → buy UP token, BTC down → buy DOWN token
        if direction == "up":
            token_id = mkt["up"]
            side = "UP"
        else:
            token_id = mkt["down"]
            side = "DOWN"

        # Check the ask price — if it's already adjusted, no edge
        book = await get_book(token_id)
        if not book or book["ask"] <= 0:
            return

        ask = book["ask"]
        if ask > MAX_ASK:
            self.signals_skipped += 1
            now = time.time()
            if not hasattr(self, '_last_skip_log') or now - self._last_skip_log > SKIP_LOG_COOLDOWN:
                log_msg(f"[SKIP] {side} ask ${ask:.2f} > ${MAX_ASK} — book already moved ({self.signals_skipped} total skips)")
                self._last_skip_log = now
            return
        if ask < MIN_ASK:
            self.signals_skipped += 1
            return

        # Size the trade
        bet = min(self.bankroll * BET_PCT, MAX_BET)
        shares = round(bet / ask, 2)
        if shares < 1:
            return

        self.trade_count += 1
        self.trades_this_window += 1
        tid = self.trade_count

        log_msg(f"[SIGNAL] #{tid} {signal_type} → buy {side} @ ${ask:.2f} ({shares}sh)")

        entry_time = time.time()

        # Execute buy
        if self.engine:
            order = await self.engine.buy_taker(token_id, ask, shares)
            if not order:
                self.execution_failures += 1
                log_msg(f"[FAIL] #{tid} Buy failed")
                return
            actual_price = order["price"]
        else:
            # Paper mode: simulate fill at ask
            actual_price = ask
            log_msg(f"[PAPER] #{tid} Simulated buy {shares}sh {side} @ ${ask:.2f}")

        self.last_trade_time = time.time()
        self.position = {
            "id": tid,
            "side": side,
            "token_id": token_id,
            "entry_price": actual_price,
            "shares": shares,
            "entry_time": entry_time,
            "btc_at_entry": self.coinbase.price,
            "move_at_signal": move,
            "question": mkt["question"],
        }

    async def _manage_position(self):
        """Monitor open position for TP, SL, or resolution."""
        pos = self.position
        if not pos:
            return

        token_id = pos["token_id"]
        book = await get_book(token_id)
        if not book:
            return

        bid = book["bid"]
        tid = pos["id"]
        entry = pos["entry_price"]
        shares = pos["shares"]
        elapsed = time.time() - pos["entry_time"]

        # WIN: bid near $1.00 — resolved in our favor
        if bid >= 0.95:
            pnl = round(shares * (1.00 - entry), 4)
            if self.engine:
                # Hold to resolution for $1.00
                pass
            log_msg(f"[WIN] #{tid} {pos['side']} resolved @ $1.00 | +${pnl:.2f}")
            self._close_position("WIN", pnl, 1.00)
            return

        # TAKE PROFIT: bid crossed TP — sell to lock in profit
        if bid >= TP_PRICE and entry < TP_PRICE:
            pnl = round(shares * (bid - entry), 4)
            if self.engine:
                # Sell with small buffer below bid
                await self.engine.sell_at_bid(token_id, shares, snap_price(bid - 0.02))
            log_msg(f"[TP] #{tid} {pos['side']} bid ${bid:.2f} >= ${TP_PRICE} | +${pnl:.2f}")
            self._close_position("TP", pnl, bid)
            return

        # STOP LOSS: bid dropped to SL
        if bid <= SL_PRICE:
            pnl = round(shares * (SL_PRICE - entry), 4)
            if self.engine:
                await self.engine.emergency_sell(token_id, shares, f"SL-{pos['side']}")
            log_msg(f"[SL] #{tid} {pos['side']} bid ${bid:.2f} <= ${SL_PRICE} | ${pnl:.2f}")
            self._close_position("SL", pnl, SL_PRICE)
            return

        # EXPIRY: window ending — check resolution
        window_end = (int(pos["entry_time"]) // 300 + 1) * 300
        if time.time() > window_end + 30:
            # Wait for resolution
            for _ in range(60):
                book = await get_book(token_id)
                if book and book["bid"] >= 0.95:
                    pnl = round(shares * (1.00 - entry), 4)
                    log_msg(f"[WIN-LATE] #{tid} {pos['side']} resolved | +${pnl:.2f}")
                    self._close_position("WIN", pnl, 1.00)
                    return
                if book and book["bid"] <= 0.05:
                    pnl = round(-shares * entry, 4)
                    log_msg(f"[LOSS] #{tid} {pos['side']} resolved $0 | ${pnl:.2f}")
                    self._close_position("LOSS", pnl, 0.0)
                    return
                await asyncio.sleep(1)

            # Force close — use final bid to determine
            book = await get_book(token_id)
            final_bid = book["bid"] if book else 0
            if final_bid > 0.5:
                pnl = round(shares * (1.00 - entry), 4)
                self._close_position("WIN-LATE", pnl, 1.00)
            else:
                pnl = round(-shares * entry, 4)
                if self.engine:
                    await self.engine.emergency_sell(token_id, shares, "EXP")
                self._close_position("LOSS", pnl, 0.0)
            return

    def _close_position(self, result, pnl, exit_price):
        """Close position and record trade."""
        pos = self.position
        if not pos:
            return

        if not self.engine:
            # Paper mode: update bankroll directly
            self.bankroll = round(self.bankroll + pnl, 4)

        if pnl > 0.01:
            self.wins += 1
        elif pnl < -0.01:
            self.losses += 1
        else:
            self.flat += 1

        if self.bankroll > self.peak:
            self.peak = self.bankroll
        dd = (self.peak - self.bankroll) / self.peak * 100 if self.peak > 0 else 0
        if dd > self.max_dd:
            self.max_dd = dd

        total = self.wins + self.losses + self.flat
        wr = self.wins / total * 100 if total else 0
        elapsed = time.time() - pos["entry_time"]

        log_msg(f"[DONE] #{pos['id']} {pos['side']} {result} | P&L ${pnl:+.2f} | "
                f"bank ${self.bankroll:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%) | "
                f"held {elapsed:.0f}s")

        # Log trade
        try:
            self.log_file.write(json.dumps({
                "id": pos["id"],
                "side": pos["side"],
                "result": result,
                "entry_price": pos["entry_price"],
                "exit_price": exit_price,
                "shares": pos["shares"],
                "pnl": pnl,
                "bankroll": self.bankroll,
                "btc_at_entry": pos["btc_at_entry"],
                "btc_move": pos["move_at_signal"],
                "hold_seconds": round(elapsed, 1),
                "question": pos["question"],
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except Exception:
            pass

        self._write_summary()
        self.position = None

        # Telegram
        icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
        asyncio.create_task(send_telegram(
            f"{icon} <b>{BOT_NAME} #{pos['id']}</b>\n"
            f"BTC {pos['move_at_signal']:+.0f} → {pos['side']} {result}\n"
            f"P&L: ${pnl:+.2f} | Bank: ${self.bankroll:.2f}\n"
            f"Held {elapsed:.0f}s | {self.wins}W/{self.losses}L ({wr:.0f}%)"))

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses + self.flat
        wr = self.wins / total * 100 if total else 0
        summary = {
            "bot": BOT_NAME,
            "elapsed_minutes": round(elapsed, 1),
            "trades": total, "wins": self.wins, "losses": self.losses,
            "win_rate": round(wr, 1),
            "bankroll": round(self.bankroll, 2),
            "peak": round(self.peak, 2),
            "pnl_total": round(self.bankroll - STARTING_BANKROLL, 2),
            "max_dd": round(self.max_dd, 1),
            "signals_detected": self.signals_detected,
            "signals_skipped": self.signals_skipped,
            "execution_failures": self.execution_failures,
            "move_threshold": MOVE_THRESHOLD,
            "sl_price": SL_PRICE,
            "tp_price": TP_PRICE,
            "btc_price": round(self.coinbase.price, 2),
            "coinbase_ticks": self.coinbase.tick_count,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(SUMMARY_FILE, summary)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses + self.flat
        wr = self.wins / total * 100 if total else 0
        pnl = self.bankroll - STARTING_BANKROLL
        bet = min(self.bankroll * BET_PCT, MAX_BET)

        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {BOT_NAME} | {elapsed:.0f}min | {'PAUSED' if self.paused else 'LIVE' if self.engine else 'PAPER'}")
        print(f"  Strategy: Coinbase BTC leads Polymarket book by 2-55s")
        print(f"  Signal: BTC moves ${MOVE_THRESHOLD}+ in {MOVE_WINDOW}s → buy winning side")
        print(f"  TP: ${TP_PRICE} | SL: ${SL_PRICE} | Cooldown: {COOLDOWN}s")
        print(f"  BTC: ${self.coinbase.price:,.2f} ({self.coinbase.tick_count:,} ticks) | Coinbase: {'OK' if self.coinbase.connected else 'DOWN'}")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f} | DD: {self.max_dd:.0f}%")
        print(f"  Bet: ${bet:.2f} | Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}%")
        print(f"  Signals: {self.signals_detected} detected, {self.signals_skipped} skipped | execFail: {self.execution_failures}")
        if self.position:
            pos = self.position
            held = time.time() - pos["entry_time"]
            print(f"  OPEN: #{pos['id']} {pos['side']} @ ${pos['entry_price']:.2f} ({held:.0f}s)")
        print()


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        if bot.engine:
            await bot.sync_bankroll()
        bot.print_status()


async def main():
    print("=" * 65)
    print(f"  {BOT_NAME} — Coinbase-to-Polymarket Latency Arbitrage")
    print("=" * 65)
    print(f"  Coinbase BTC/USD → mispricing at 0.07%+ from open OR ${MOVE_THRESHOLD}+ momentum")
    print(f"  Buy winning side on Polymarket before book adjusts")
    print(f"  TP: bid >= ${TP_PRICE} | SL: bid <= ${SL_PRICE} | Hold to resolution")
    print(f"  Cooldown: {COOLDOWN}s | Max {MAX_TRADES_PER_WINDOW}/window")
    print(f"  Bankroll: ${STARTING_BANKROLL} | Bet: {int(BET_PCT*100)}%")
    print()
    print(f"  Execution: IRONCLAD engine (FAK + balance fix + per-order tracking)")
    print()

    bot = LatencyArbBot()
    bot.init_clob()
    bot._write_summary()  # Write initial summary for dashboard

    if bot.engine and not PAPER_MODE:
        await bot.sync_bankroll()
        log_msg(f"[INIT] LIVE mode — Bank: ${bot.bankroll:.2f}")
    else:
        log_msg(f"[INIT] PAPER mode — Bank: ${bot.bankroll:.2f}")

    asyncio.create_task(send_telegram(
        f"🚀 <b>{BOT_NAME}</b>\n"
        f"Coinbase latency arb\n"
        f"Signal: BTC ${MOVE_THRESHOLD}+ in {MOVE_WINDOW}s\n"
        f"TP ${TP_PRICE} SL ${SL_PRICE}\n"
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
