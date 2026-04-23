#!/usr/bin/env python3
"""
REVERSAL-MID — BTC Reversal with Take Profit at $0.35
========================================================
Strategy:
  - BTC only
  - Buy cheap token ($0.05-$0.25) on Coinbase reversal signal
  - WebSocket real-time bid monitoring for TP detection
  - Take profit at $0.35 via emergency_sell (FAK)
  - Hold to resolution if TP not hit
  - 2% bankroll per trade, no time filter

Execution: IRONCLAD (FOK+FAK taker buy, emergency_sell, fill verification)
Monitor:   WebSocket (wss://ws-subscriptions-clob.polymarket.com/ws/market)
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
    import websockets
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

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
BET_PCT = 0.02                   # 2% of bankroll per trade
MAX_LIVE_BET = 5.00              # Hard cap
FAK_FLOOR = 0.05
TP_PRICE = 0.35                  # Take profit target

# Signal thresholds
CHEAP_MAX = 0.25
CHEAP_MIN = 0.05
REVERSAL_TICKS = 5
REVERSAL_MOVE = 10.0
REVERSAL_WINDOW = 10.0
MIN_WINDOW_ELAPSED = 30
MAX_WINDOW_ELAPSED = 240
COOLDOWN = 60.0
MAX_TRADES_PER_WINDOW = 1

DRAWDOWN_PAUSE_PCT = 95          # High threshold — shared wallet
MIN_WALLET_BALANCE = 5.0

USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PAPER_MODE = os.getenv("REVERSAL_MID_LIVE", "0") != "1"

ASSETS = ["BTC"]

os.makedirs("logs", exist_ok=True)
BOT_NAME = "REVERSAL-MID" if not PAPER_MODE else "REVERSAL-MID-PAPER"
LOG_FILE = "logs/reversal_mid_trades.jsonl"
SUMMARY_FILE = "logs/reversal_mid_summary.json"

# WebSocket URL for Polymarket CLOB book updates
POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


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
        self._history = deque(maxlen=1000)
        self._tick_dirs = deque(maxlen=20)
        self.connected = False
        self.tick_count = 0
        self.window_open_price = 0.0
        self.current_window = 0
        self._prev_price = 0.0

    def get_reversal_signal(self, current_trend_dir):
        if len(self._history) < 10:
            return False, 0
        now = time.time()
        cutoff = now - REVERSAL_WINDOW
        old_price = None
        for t, p in self._history:
            if t >= cutoff:
                old_price = p
                break
        if old_price is None or old_price == 0:
            return False, 0
        move = self.price - old_price
        reversal_dir = "down" if current_trend_dir == "up" else "up"
        consecutive = 0
        for d in reversed(self._tick_dirs):
            if d == reversal_dir:
                consecutive += 1
            else:
                break
        if reversal_dir == "down" and move < -REVERSAL_MOVE and consecutive >= REVERSAL_TICKS:
            return True, abs(move)
        if reversal_dir == "up" and move > REVERSAL_MOVE and consecutive >= REVERSAL_TICKS:
            return True, abs(move)
        return False, 0

    def _check_window(self):
        window = int(time.time()) // 300
        if window != self.current_window:
            self.current_window = window
            self.window_open_price = self.price
            if self.price > 0:
                log_msg(f"[CB] New window -- open ${self.price:,.2f}")

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
                                            if self._prev_price > 0:
                                                if p > self._prev_price:
                                                    self._tick_dirs.append("up")
                                                elif p < self._prev_price:
                                                    self._tick_dirs.append("down")
                                            self._prev_price = p
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
        self.markets = {}

    async def refresh_all(self):
        for asset in ASSETS:
            try:
                now = int(time.time())
                window_start = (now // 300) * 300
                prefix = asset.lower()
                slug = f"{prefix}-updown-5m-{window_start}"
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
                                self.markets[asset] = {
                                    "up": up, "down": down,
                                    "question": m.get("question", ""),
                                    "window_start": window_start,
                                }
            except Exception as e:
                log_msg(f"[MKT] {asset}: {e}")


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


# ── Execution Engine (IRONCLAD — FOK+FAK taker) ────────
class ExecutionEngine:
    def __init__(self, client):
        self.client = client
        self._lock = asyncio.Lock()

    async def buy_taker(self, token_id, price, size):
        """FOK then FAK fallback. Integer shares, price buffer."""
        async with self._lock:
            if not self.client:
                return None
            shares = round(size)  # Integer shares to avoid decimal precision errors
            if shares < 5:
                shares = 5        # Polymarket minimum
            buy_price = snap_price(min(price + 0.01, 0.99))  # Buffer for taker fill

            # Try FOK first
            try:
                args = OrderArgs(price=buy_price, size=shares, side=BUY, token_id=token_id)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.FOK)
                oid = resp.get("orderID") or resp.get("order_id") if resp else None
                if oid:
                    log_msg(f"[BUY] FOK {shares}sh @ ${buy_price} order={oid[:8]}...")
                    # Verify fill
                    status = await self.check_order_status(oid)
                    if status and status.get("status") == "MATCHED":
                        return {"order_id": oid, "price": price, "size": shares, "token_id": token_id}
                    log_msg(f"[BUY] FOK not matched, trying FAK...")
            except Exception as e:
                log_msg(f"[BUY] FOK: {e}")

            # FAK fallback
            try:
                args = OrderArgs(price=buy_price, size=shares, side=BUY, token_id=token_id)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.FAK)
                oid = resp.get("orderID") or resp.get("order_id") if resp else None
                if oid:
                    log_msg(f"[BUY] FAK {shares}sh @ ${buy_price} order={oid[:8]}...")
                    # Wait briefly for fill
                    await asyncio.sleep(1)
                    status = await self.check_order_status(oid)
                    if status:
                        matched = float(status.get("size_matched", 0))
                        if matched >= 0.5:
                            log_msg(f"[FILL] Confirmed {matched}sh filled")
                            return {"order_id": oid, "price": price, "size": matched, "token_id": token_id}
            except Exception as e:
                log_msg(f"[BUY] FAK: {e}")

            return None

    async def check_order_status(self, order_id):
        """Check order fill status via CLOB API."""
        try:
            result = self.client.get_order(order_id)
            return result
        except Exception:
            return None

    async def emergency_sell(self, token_id, size, reason="SL"):
        """FAK sell at aggressive floor. update_balance_allowance first."""
        async with self._lock:
            if not self.client:
                return False
            # Update balance allowance for conditional token
            try:
                params = BalanceAllowanceParams(
                    asset_type="CONDITIONAL",
                    token_id=token_id,
                    signature_type=SIGNATURE_TYPE
                )
                self.client.update_balance_allowance(params)
            except Exception:
                pass
            for attempt in range(5):
                try:
                    sell_size = max(round(size - attempt), 1)
                    floor = snap_price(FAK_FLOOR + attempt * 0.01)
                    args = OrderArgs(price=floor, size=sell_size, side=SELL, token_id=token_id)
                    signed = self.client.create_order(args)
                    resp = self.client.post_order(signed, OrderType.FAK)
                    oid = resp.get("orderID") or resp.get("order_id") if resp else None
                    if oid:
                        log_msg(f"[{reason}] FAK SELL {sell_size}sh @ ${floor}")
                        return True
                except Exception as e:
                    if "balance" in str(e).lower():
                        try:
                            params = BalanceAllowanceParams(
                                asset_type="CONDITIONAL",
                                token_id=token_id,
                                signature_type=SIGNATURE_TYPE
                            )
                            self.client.update_balance_allowance(params)
                        except Exception:
                            pass
                    await asyncio.sleep(0.3)
            return False


# ── WebSocket Position Monitor ─────────────────────────
async def ws_monitor_position(bot, pos):
    """
    Monitor position via Polymarket WebSocket for real-time TP detection.
    Falls back to HTTP polling if WS connection fails.
    """
    token_id = pos["token_id"]
    peak_bid = pos["entry_price"]
    bid_history = []
    ws_connected = False

    try:
        async with websockets.connect(POLY_WS_URL, ping_interval=20, ping_timeout=10,
                                       close_timeout=5) as ws:
            # Subscribe to this token's book
            sub_msg = json.dumps({
                "assets_ids": [token_id],
                "type": "market"
            })
            await ws.send(sub_msg)
            ws_connected = True
            log_msg(f"[WS] #{pos['id']} Subscribed to book for {pos['asset']} {pos['side']}")

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                # Parse book update — extract best bid
                # Polymarket WS sends different message formats
                bids = None
                asset_id = msg.get("asset_id", "")

                if "market" in msg.get("event_type", ""):
                    bids = msg.get("bids", [])
                elif "book" in msg.get("event_type", ""):
                    bids = msg.get("bids", [])
                elif isinstance(msg.get("bids"), list):
                    bids = msg["bids"]

                if not bids or (asset_id and asset_id != token_id):
                    continue

                bid = max((float(b.get("price", b.get("p", 0))) for b in bids), default=0)
                if bid <= 0:
                    continue

                now = time.time()
                bid_history.append((now, bid))

                # Track peak
                if bid > peak_bid:
                    peak_bid = bid
                    log_msg(f"[PEAK] #{pos['id']} New peak bid ${bid:.2f}")

                # ── TP CHECK ──
                if bid >= TP_PRICE:
                    log_msg(f"[TP] #{pos['id']} bid ${bid:.2f} >= ${TP_PRICE} — SELLING NOW")
                    if bot.engine and not PAPER_MODE:
                        sold = await bot.engine.emergency_sell(
                            token_id, round(pos["shares"]), f"TP-{pos['id']}"
                        )
                        if sold:
                            log_msg(f"[TP] #{pos['id']} SOLD via emergency_sell")
                            return {"exit": "TP", "peak": peak_bid, "exit_price": bid,
                                    "bid_history": bid_history[-50:]}
                    else:
                        # Paper mode TP
                        return {"exit": "TP", "peak": peak_bid, "exit_price": TP_PRICE,
                                "bid_history": bid_history[-50:]}

                # ── RESOLUTION CHECK ──
                if bid >= 0.95:
                    log_msg(f"[WIN] #{pos['id']} resolved @ ${bid:.2f}")
                    return {"exit": "WIN", "peak": peak_bid, "exit_price": 1.00,
                            "bid_history": bid_history[-50:]}
                if bid <= 0.02:
                    log_msg(f"[LOSS] #{pos['id']} resolved @ ${bid:.2f}")
                    return {"exit": "LOSS", "peak": peak_bid, "exit_price": 0.00,
                            "bid_history": bid_history[-50:]}

                # ── TIMEOUT: window expired + 60s grace ──
                window_end = (int(pos["entry_time"]) // 300 + 1) * 300
                if now > window_end + 60:
                    log_msg(f"[EXPIRE] #{pos['id']} Window ended, bid=${bid:.2f}")
                    if bid > 0.5:
                        return {"exit": "WIN-LATE", "peak": peak_bid, "exit_price": 1.00,
                                "bid_history": bid_history[-50:]}
                    else:
                        return {"exit": "LOSS", "peak": peak_bid, "exit_price": 0.00,
                                "bid_history": bid_history[-50:]}

    except (websockets.exceptions.ConnectionClosed, ConnectionError, OSError) as e:
        log_msg(f"[WS] #{pos['id']} Connection lost: {e}")
    except Exception as e:
        log_msg(f"[WS] #{pos['id']} Error: {e}")

    # ── FALLBACK: HTTP polling ──
    if not ws_connected:
        log_msg(f"[WS] #{pos['id']} WS failed — falling back to HTTP polling")
    else:
        log_msg(f"[WS] #{pos['id']} WS dropped — switching to HTTP polling")

    return await http_monitor_position(bot, pos, peak_bid, bid_history)


async def http_monitor_position(bot, pos, peak_bid=None, bid_history=None):
    """HTTP polling fallback for position monitoring."""
    token_id = pos["token_id"]
    if peak_bid is None:
        peak_bid = pos["entry_price"]
    if bid_history is None:
        bid_history = []

    while True:
        book = await get_book(token_id)
        if not book:
            await asyncio.sleep(0.3)
            continue

        bid = book["bid"]
        now = time.time()
        bid_history.append((now, bid))

        if bid > peak_bid:
            peak_bid = bid
            log_msg(f"[PEAK-HTTP] #{pos['id']} New peak bid ${bid:.2f}")

        # TP check
        if bid >= TP_PRICE:
            log_msg(f"[TP] #{pos['id']} bid ${bid:.2f} >= ${TP_PRICE} — SELLING NOW (HTTP)")
            if bot.engine and not PAPER_MODE:
                sold = await bot.engine.emergency_sell(
                    token_id, round(pos["shares"]), f"TP-{pos['id']}"
                )
                if sold:
                    return {"exit": "TP", "peak": peak_bid, "exit_price": bid,
                            "bid_history": bid_history[-50:]}
            else:
                return {"exit": "TP", "peak": peak_bid, "exit_price": TP_PRICE,
                        "bid_history": bid_history[-50:]}

        # Resolution
        if bid >= 0.95:
            return {"exit": "WIN", "peak": peak_bid, "exit_price": 1.00,
                    "bid_history": bid_history[-50:]}
        if bid <= 0.02:
            return {"exit": "LOSS", "peak": peak_bid, "exit_price": 0.00,
                    "bid_history": bid_history[-50:]}

        # Timeout
        window_end = (int(pos["entry_time"]) // 300 + 1) * 300
        if now > window_end + 60:
            if bid > 0.5:
                return {"exit": "WIN-LATE", "peak": peak_bid, "exit_price": 1.00,
                        "bid_history": bid_history[-50:]}
            else:
                return {"exit": "LOSS", "peak": peak_bid, "exit_price": 0.00,
                        "bid_history": bid_history[-50:]}

        await asyncio.sleep(0.3)


# ── Reversal MID Bot ──────────────────────────────────
class ReversalMidBot:
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
        self.paused = False
        self.log_file = open(LOG_FILE, "a")
        self.last_trade_time = 0
        self.trades_this_window = 0
        self.current_window = 0
        self.signals_detected = 0
        self.reversals_detected = 0
        self.fill_failures = 0
        self.position = None
        self.placing_order = False  # Lock to prevent duplicate orders

    def init_clob(self):
        if PAPER_MODE:
            log_msg("[CLOB] PAPER MODE — no real orders")
            log_msg("[CLOB] Set REVERSAL_MID_LIVE=1 to enable live trading")
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
            self.engine = ExecutionEngine(self.client)
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
            if dd >= DRAWDOWN_PAUSE_PCT and not self.paused:
                self.paused = True
                log_msg(f"[RISK] PAUSED — DD {dd:.0f}% >= {DRAWDOWN_PAUSE_PCT}%")
                asyncio.create_task(send_telegram(f"🛑 {BOT_NAME} PAUSED DD {dd:.0f}%"))

    async def run(self):
        log_msg("[MID] Waiting for Coinbase feed...")
        while not self.coinbase.connected or self.coinbase.price == 0:
            await asyncio.sleep(0.5)
        log_msg(f"[MID] Coinbase live: BTC ${self.coinbase.price:,.2f}")

        await self.sync_bankroll()

        while True:
            try:
                if self.paused:
                    await asyncio.sleep(5)
                    continue

                now = time.time()
                current_window = int(now) // 300
                if current_window != self.current_window:
                    self.current_window = current_window
                    self.trades_this_window = 0
                    await self.mf.refresh_all()
                    if not PAPER_MODE and self.engine:
                        await self.sync_bankroll()

                if self.bankroll < MIN_WALLET_BALANCE:
                    await asyncio.sleep(5)
                    continue

                # If we have a position, it's being monitored by ws_monitor_position
                # Just wait
                if self.position:
                    await asyncio.sleep(1)
                    continue

                window_start = (int(now) // 300) * 300
                window_elapsed = now - window_start
                if window_elapsed < MIN_WINDOW_ELAPSED or window_elapsed > MAX_WINDOW_ELAPSED:
                    await asyncio.sleep(0.5)
                    continue

                if now - self.last_trade_time < COOLDOWN:
                    await asyncio.sleep(0.5)
                    continue

                if self.trades_this_window >= MAX_TRADES_PER_WINDOW:
                    await asyncio.sleep(1)
                    continue

                if self.placing_order:
                    await asyncio.sleep(0.5)
                    continue

                for asset in ASSETS:
                    if asset not in self.mf.markets:
                        continue
                    await self._scan_for_reversal(asset, self.mf.markets[asset])
                    if self.position:
                        break

                await asyncio.sleep(0.3)

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                await asyncio.sleep(2)

    async def _scan_for_reversal(self, asset, mkt):
        up_book, down_book = await asyncio.gather(
            get_book(mkt["up"]), get_book(mkt["down"]))
        if not up_book or not down_book:
            return

        up_ask = up_book["ask"]
        down_ask = down_book["ask"]

        cheap_side = None
        cheap_token = None
        cheap_ask = 0
        trend_dir = None

        if CHEAP_MIN <= up_ask <= CHEAP_MAX:
            cheap_side = "UP"
            cheap_token = mkt["up"]
            cheap_ask = up_ask
            trend_dir = "down"
        elif CHEAP_MIN <= down_ask <= CHEAP_MAX:
            cheap_side = "DOWN"
            cheap_token = mkt["down"]
            cheap_ask = down_ask
            trend_dir = "up"

        if not cheap_side:
            return

        self.signals_detected += 1

        is_reversing, strength = self.coinbase.get_reversal_signal(trend_dir)
        if not is_reversing:
            return

        self.reversals_detected += 1

        # ── SIZE THE BET ──
        entry_price = snap_price(cheap_ask)
        bet = min(self.bankroll * BET_PCT, MAX_LIVE_BET)
        shares = round(bet / entry_price)  # Integer shares
        if shares < 5:
            shares = 5

        self.trade_count += 1
        self.trades_this_window += 1
        tid = self.trade_count
        potential_return = round((1.00 / entry_price - 1) * 100, 0)

        log_msg(f"[REVERSAL] #{tid} {asset} {cheap_side} @ ${entry_price:.2f} ({shares}sh) "
                f"| bet ${bet:.2f} | reversal ${strength:.0f} | +{potential_return:.0f}% potential")

        # ── EXECUTE BUY (FOK+FAK) ──
        self.placing_order = True
        try:
            if self.engine and not PAPER_MODE:
                order = await self.engine.buy_taker(cheap_token, entry_price, shares)
                if not order:
                    log_msg(f"[FAIL] #{tid} Bid placement failed")
                    self.fill_failures += 1
                    return

                actual_shares = order.get("size", shares)
                log_msg(f"[FILL] #{tid} CONFIRMED {actual_shares}sh @ ${entry_price:.2f}")
                shares = actual_shares
            else:
                log_msg(f"[PAPER] #{tid} Simulated buy {shares}sh {cheap_side} @ ${entry_price:.2f}")
        finally:
            self.placing_order = False

        self.last_trade_time = time.time()
        pos = {
            "id": tid,
            "asset": asset,
            "side": cheap_side,
            "token_id": cheap_token,
            "entry_price": entry_price,
            "shares": shares,
            "bet_amount": bet,
            "entry_time": time.time(),
            "btc_at_entry": self.coinbase.price,
            "reversal_strength": strength,
            "question": mkt["question"],
            "wallet_before": self.bankroll,
        }
        self.position = pos

        # Launch WebSocket monitor as a task
        asyncio.create_task(self._monitor_and_close(pos))

    async def _monitor_and_close(self, pos):
        """Run WS monitor, then close position based on result."""
        try:
            result = await ws_monitor_position(self, pos)
            if result:
                await self._close_position(pos, result)
            else:
                log_msg(f"[ERR] #{pos['id']} Monitor returned no result")
                # Force HTTP fallback resolution
                result = await http_monitor_position(self, pos)
                if result:
                    await self._close_position(pos, result)
        except Exception as e:
            log_msg(f"[ERR] #{pos['id']} Monitor failed: {e}")
        finally:
            self.position = None

    async def _close_position(self, pos, result):
        exit_type = result["exit"]
        peak_bid = result.get("peak", pos["entry_price"])
        exit_price = result.get("exit_price", 0)

        if exit_type in ("WIN", "WIN-LATE", "TP"):
            if exit_type == "TP":
                pnl = round(pos["shares"] * (TP_PRICE - pos["entry_price"]), 4)
            else:
                pnl = round(pos["shares"] * (1.00 - pos["entry_price"]), 4)
            self.wins += 1
        else:
            pnl = round(-pos["shares"] * pos["entry_price"], 4)
            self.losses += 1

        # Update bankroll
        if PAPER_MODE or not self.engine:
            self.bankroll = round(self.bankroll + pnl, 4)
        else:
            await asyncio.sleep(3)
            old_bank = pos["wallet_before"]
            await self.sync_bankroll()
            actual_pnl = round(self.bankroll - old_bank, 2)
            if abs(actual_pnl - pnl) > 0.50:
                log_msg(f"[WARN] P&L mismatch: calc=${pnl:+.2f} vs wallet=${actual_pnl:+.2f}")
                pnl = actual_pnl

        if self.bankroll > self.peak:
            self.peak = self.bankroll
        dd = (self.peak - self.bankroll) / self.peak * 100 if self.peak > 0 else 0
        if dd > self.max_dd:
            self.max_dd = dd

        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        elapsed = time.time() - pos["entry_time"]
        ret_pct = round(pnl / pos["bet_amount"] * 100, 1) if pos["bet_amount"] > 0 else 0

        mode = "LIVE" if self.engine and not PAPER_MODE else "PAPER"
        log_msg(f"[{mode}] #{pos['id']} {pos['asset']} {pos['side']} {exit_type} | "
                f"P&L ${pnl:+.2f} ({ret_pct:+.0f}%) | peak ${peak_bid:.2f} | bank ${self.bankroll:.2f} | "
                f"{self.wins}W/{self.losses}L ({wr:.0f}%) | held {elapsed:.0f}s")

        # Log bid history
        bid_hist = result.get("bid_history", [])
        bid_prices = [f"${b[1]:.2f}" for b in bid_hist[-10:]]
        if bid_prices:
            log_msg(f"[BIDS] #{pos['id']} Last 10: {', '.join(bid_prices)}")

        try:
            self.log_file.write(json.dumps({
                "id": pos["id"], "asset": pos["asset"], "side": pos["side"],
                "result": exit_type, "entry_price": pos["entry_price"],
                "exit_price": exit_price, "shares": pos["shares"],
                "bet_amount": pos["bet_amount"],
                "pnl": pnl, "return_pct": ret_pct, "bankroll": self.bankroll,
                "btc_at_entry": pos["btc_at_entry"],
                "reversal_strength": pos["reversal_strength"],
                "peak_bid": peak_bid,
                "hold_seconds": round(elapsed, 1),
                "tp_price": TP_PRICE,
                "bid_count": len(bid_hist),
                "mode": mode,
                "question": pos["question"],
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except Exception:
            pass

        self._write_summary()

        icon = "🟢" if pnl > 0 else "🔴"
        asyncio.create_task(send_telegram(
            f"{icon} <b>{BOT_NAME} #{pos['id']}</b>\n"
            f"[{mode}] {pos['asset']} {pos['side']} @ ${pos['entry_price']:.2f} -> {exit_type}\n"
            f"P&L: ${pnl:+.2f} ({ret_pct:+.0f}%) | Peak: ${peak_bid:.2f}\n"
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
            "peak": round(self.peak, 2),
            "pnl_total": round(self.bankroll - sb, 2),
            "max_dd": round(self.max_dd, 1),
            "signals_detected": self.signals_detected,
            "reversals_detected": self.reversals_detected,
            "fill_failures": self.fill_failures,
            "tp_price": TP_PRICE,
            "monitor": "WebSocket + HTTP fallback",
            "assets": ASSETS,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(SUMMARY_FILE, summary)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        sb = self.starting_bankroll if self.starting_bankroll > 0 else 100
        pnl = self.bankroll - sb
        bet = min(self.bankroll * BET_PCT, MAX_LIVE_BET)
        mode = "LIVE" if self.engine and not PAPER_MODE else "PAPER"

        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {BOT_NAME} | {elapsed:.0f}min | {mode}")
        print(f"  Strategy: Reversal + TP @ ${TP_PRICE} (WebSocket monitor)")
        print(f"  Assets: {', '.join(ASSETS)} | No time filter")
        print(f"  BTC: ${self.coinbase.price:,.2f} | Coinbase: {'OK' if self.coinbase.connected else 'DOWN'}")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f} | DD: {self.max_dd:.0f}%")
        print(f"  Bet: ${bet:.2f} ({int(BET_PCT*100)}%) | Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}%")
        print(f"  Signals: {self.signals_detected} | Reversals: {self.reversals_detected} | Fills failed: {self.fill_failures}")
        if self.position:
            pos = self.position
            held = time.time() - pos["entry_time"]
            print(f"  OPEN: #{pos['id']} {pos['asset']} {pos['side']} @ ${pos['entry_price']:.2f} bet=${pos['bet_amount']:.2f} ({held:.0f}s)")
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
    print(f"  {BOT_NAME} — Reversal + TP @ ${TP_PRICE} [{mode}]")
    print("=" * 65)
    print(f"  Buy cheap BTC tokens on Coinbase reversal")
    print(f"  TP: ${TP_PRICE} via WebSocket real-time monitor")
    print(f"  Fallback: HTTP polling if WS disconnects")
    print(f"  Bet: {int(BET_PCT*100)}% of wallet (max ${MAX_LIVE_BET})")
    print(f"  Assets: {', '.join(ASSETS)}")
    print()
    if mode == "LIVE":
        print(f"  *** LIVE MODE — REAL MONEY AT RISK ***")
    else:
        print(f"  Paper mode — set REVERSAL_MID_LIVE=1 to go live")
    print()

    bot = ReversalMidBot()
    bot.init_clob()

    if not PAPER_MODE and bot.engine:
        await bot.sync_bankroll()
        log_msg(f"[INIT] LIVE — Wallet: ${bot.bankroll:.2f}")
    else:
        log_msg(f"[INIT] PAPER — Bank: ${bot.bankroll:.2f}")

    bot._write_summary()

    asyncio.create_task(send_telegram(
        f"🎯 <b>{BOT_NAME}</b> [{mode}]\n"
        f"Reversal + TP @ ${TP_PRICE} (WebSocket monitor)\n"
        f"BTC only | {int(BET_PCT*100)}% bet\n"
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
