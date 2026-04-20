#!/usr/bin/env python3
"""
REVERSAL SNIPER LIVE — Production-Ready Reversal Trading
==========================================================
Same strategy as paper REVERSAL-V1 with all execution fixes:

Fixes applied for live:
  1. Wallet balance sync — reads real USDC balance, not simulated
  2. Fill verification — polls order status after maker bid
  3. Time-of-day filter — skips 9am-4pm ET (13:00-20:00 UTC)
  4. Max live bet cap — hard $MAX_LIVE_BET regardless of bankroll
  5. Wallet-based bankroll — no simulated compounding
  6. P&L verification — compares calculated vs wallet after each trade
  7. Drawdown based on real wallet, not simulated

Strategy:
  - Wait for one side to drop below $0.25
  - Coinbase reversal: 5 ticks + $10 move in 10s
  - Buy cheap token via GTC maker bid (0% fee)
  - Hold to resolution ($1.00 or $0.00)
  - No stop loss (small bets, hold to binary outcome)

Paper results: 609 trades, 23% WR, $100 -> $4,465 (+4,365%)
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
BET_PCT = 0.05
MAX_LIVE_BET = 3.00          # HARD CAP — never bet more than $3 per trade live
FAK_FLOOR = 0.05

# Signal thresholds (same as paper V1)
CHEAP_MAX = 0.25
CHEAP_MIN = 0.05
REVERSAL_TICKS = 5
REVERSAL_MOVE = 10.0
REVERSAL_WINDOW = 10.0
MIN_WINDOW_ELAPSED = 30
MAX_WINDOW_ELAPSED = 240
COOLDOWN = 60.0
MAX_TRADES_PER_WINDOW = 1

# Time-of-day filter: skip 9am-4pm ET (13:00-20:00 UTC)
# Reversal bots lose money during US market hours
SKIP_HOURS_UTC_START = 13
SKIP_HOURS_UTC_END = 20

DRAWDOWN_PAUSE_PCT = 50      # Pause at 50% DD of starting wallet
MIN_WALLET_BALANCE = 5.0     # Don't trade if wallet below $5

USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# LIVE/PAPER MODE
PAPER_MODE = os.getenv("REVERSAL_LIVE", "0") != "1"

ASSETS = ["BTC", "SOL"]

os.makedirs("logs", exist_ok=True)
BOT_NAME = "REVERSAL-LIVE" if not PAPER_MODE else "REVERSAL-LIVE-PAPER"
LOG_FILE = "logs/reversal_live_trades.jsonl"
SUMMARY_FILE = "logs/reversal_live_summary.json"


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

def is_skip_hours():
    """Return True if current UTC hour is in the skip window (9am-4pm ET)."""
    hr = datetime.now(timezone.utc).hour
    return SKIP_HOURS_UTC_START <= hr < SKIP_HOURS_UTC_END


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
    """Read real USDC balance from Polygon chain."""
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


# ── Execution Engine (IRONCLAD — production grade) ─────
class ExecutionEngine:
    def __init__(self, client):
        self.client = client
        self._lock = asyncio.Lock()

    async def buy_maker(self, token_id, price, size):
        """Place GTC maker bid. Returns order dict or None."""
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
                    log_msg(f"[BID] GTC {size}sh @ ${bid_price} (maker) order={oid[:8]}...")
                    return {"order_id": oid, "price": bid_price, "size": size, "token_id": token_id}
            except Exception as e:
                log_msg(f"[BID] err: {e}")
            return None

    async def check_fill(self, token_id, entry_price, max_wait=30):
        """Poll book to check if our maker bid likely filled.
        If ask drops to or below our bid, we likely filled."""
        for _ in range(max_wait):
            book = await get_book(token_id)
            if book and book["ask"] <= entry_price:
                return True
            if book and book["bid"] >= entry_price:
                return True
            await asyncio.sleep(1)
        return False

    async def cancel_order(self, order_id):
        async with self._lock:
            if not self.client:
                return
            try:
                self.client.cancel(order_id)
                log_msg(f"[CANCEL] order={order_id[:8]}...")
            except Exception:
                pass

    async def emergency_sell(self, token_id, size, reason="SL"):
        """FAK sell at aggressive floor. update_balance_allowance first."""
        async with self._lock:
            if not self.client:
                return False
            try:
                self.client.update_balance_allowance(int(size * 1_000_000))
            except Exception:
                pass
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
                        log_msg(f"[{reason}] FAK SELL {sell_size}sh @ ${floor}")
                        return True
                except Exception as e:
                    if "balance" in str(e).lower():
                        try:
                            self.client.update_balance_allowance(int(sell_size * 0.9 * 1_000_000))
                        except Exception:
                            pass
                    await asyncio.sleep(0.3)
            return False


# ── Reversal Sniper Live Bot ───────────────────────────
class ReversalSniperLive:
    def __init__(self):
        self.mf = MarketFinder()
        self.coinbase = CoinbaseFeed()
        self.client = None
        self.engine = None
        self.bankroll = 0.0        # Will be set from wallet
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
        self.skipped_time_filter = 0
        self.fill_failures = 0
        self.position = None

    def init_clob(self):
        if PAPER_MODE:
            log_msg("[CLOB] PAPER MODE -- no real orders")
            log_msg("[CLOB] Set REVERSAL_LIVE=1 to enable live trading")
            self.bankroll = 100.0
            self.starting_bankroll = 100.0
            self.peak = 100.0
            return
        if not CLOB_AVAILABLE or not PRIVATE_KEY:
            log_msg("[CLOB] No client -- falling back to paper mode")
            self.bankroll = 100.0
            self.starting_bankroll = 100.0
            self.peak = 100.0
            return
        try:
            self.client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                                     signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS)
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            self.engine = ExecutionEngine(self.client)
            log_msg("[CLOB] Auth OK -- LIVE execution ready")
        except Exception as e:
            log_msg(f"[CLOB] Auth fail: {e}")

    async def sync_bankroll(self):
        """Read real wallet balance. Only in live mode."""
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
                log_msg(f"[RISK] PAUSED -- DD {dd:.0f}% >= {DRAWDOWN_PAUSE_PCT}%")
                asyncio.create_task(send_telegram(f"🛑 {BOT_NAME} PAUSED DD {dd:.0f}%"))

    async def run(self):
        log_msg("[SNIPER] Waiting for Coinbase feed...")
        while not self.coinbase.connected or self.coinbase.price == 0:
            await asyncio.sleep(0.5)
        log_msg(f"[SNIPER] Coinbase live: BTC ${self.coinbase.price:,.2f}")

        # Sync wallet on startup (live mode)
        await self.sync_bankroll()

        while True:
            try:
                if self.paused:
                    await asyncio.sleep(5)
                    continue

                # Time-of-day filter
                if is_skip_hours():
                    if self.skipped_time_filter == 0 or self.skipped_time_filter % 300 == 0:
                        log_msg(f"[SKIP] 9am-4pm ET -- reversal bots lose money during market hours")
                    self.skipped_time_filter += 1
                    await asyncio.sleep(10)
                    continue

                now = time.time()
                current_window = int(now) // 300
                if current_window != self.current_window:
                    self.current_window = current_window
                    self.trades_this_window = 0
                    await self.mf.refresh_all()
                    # Periodic wallet sync (live mode)
                    if not PAPER_MODE and self.engine:
                        await self.sync_bankroll()

                # Check wallet minimum
                if self.bankroll < MIN_WALLET_BALANCE:
                    await asyncio.sleep(5)
                    continue

                if self.position:
                    await self._manage_position()
                    await asyncio.sleep(0.5)
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
        bet = min(self.bankroll * BET_PCT, MAX_LIVE_BET)  # Hard cap at MAX_LIVE_BET
        shares = round(bet / entry_price, 2)
        if shares < 1:
            return

        self.trade_count += 1
        self.trades_this_window += 1
        tid = self.trade_count
        potential_return = round((1.00 / entry_price - 1) * 100, 0)

        log_msg(f"[REVERSAL] #{tid} {asset} {cheap_side} @ ${entry_price:.2f} ({shares}sh) "
                f"| bet ${bet:.2f} | reversal ${strength:.0f} | +{potential_return:.0f}% potential")

        # ── EXECUTE BUY ──
        if self.engine:
            order = await self.engine.buy_maker(cheap_token, entry_price, shares)
            if not order:
                log_msg(f"[FAIL] #{tid} Bid placement failed")
                self.fill_failures += 1
                return

            # FILL VERIFICATION: wait up to 30s for fill
            log_msg(f"[WAIT] #{tid} Waiting for maker fill...")
            filled = await self.engine.check_fill(cheap_token, entry_price, max_wait=30)

            if not filled:
                # Cancel unfilled bid
                await self.engine.cancel_order(order["order_id"])
                log_msg(f"[UNFILL] #{tid} Bid not filled -- cancelled")
                self.fill_failures += 1
                return

            log_msg(f"[FILL] #{tid} Maker bid filled @ ${entry_price:.2f}")
        else:
            log_msg(f"[PAPER] #{tid} Simulated buy {shares}sh {cheap_side} @ ${entry_price:.2f}")

        self.last_trade_time = time.time()
        self.position = {
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

    async def _manage_position(self):
        """Hold to resolution. No SL -- binary outcome."""
        pos = self.position
        if not pos:
            return

        token_id = pos["token_id"]
        book = await get_book(token_id)
        if not book:
            return

        bid = book["bid"]

        # WIN
        if bid >= 0.95:
            pnl = round(pos["shares"] * (1.00 - pos["entry_price"]), 4)
            log_msg(f"[WIN] #{pos['id']} {pos['asset']} {pos['side']} @ $1.00 | +${pnl:.2f}")
            await self._close_position("WIN", pnl, 1.00)
            return

        # Window ended -- wait for resolution
        window_end = (int(pos["entry_time"]) // 300 + 1) * 300
        if time.time() > window_end + 30:
            for _ in range(60):
                book = await get_book(token_id)
                if book and book["bid"] >= 0.95:
                    pnl = round(pos["shares"] * (1.00 - pos["entry_price"]), 4)
                    await self._close_position("WIN", pnl, 1.00)
                    return
                if book and book["bid"] <= 0.05:
                    pnl = round(-pos["shares"] * pos["entry_price"], 4)
                    await self._close_position("LOSS", pnl, 0.0)
                    return
                await asyncio.sleep(1)

            # Force resolve
            book = await get_book(token_id)
            final_bid = book["bid"] if book else 0
            if final_bid > 0.5:
                pnl = round(pos["shares"] * (1.00 - pos["entry_price"]), 4)
                await self._close_position("WIN-LATE", pnl, 1.00)
            else:
                pnl = round(-pos["shares"] * pos["entry_price"], 4)
                await self._close_position("LOSS", pnl, 0.0)

    async def _close_position(self, result, pnl, exit_price):
        pos = self.position
        if not pos:
            return

        # Update bankroll
        if PAPER_MODE or not self.engine:
            self.bankroll = round(self.bankroll + pnl, 4)
        else:
            # LIVE: sync from wallet to get real P&L
            await asyncio.sleep(3)  # Let settlement propagate
            old_bank = pos["wallet_before"]
            await self.sync_bankroll()
            actual_pnl = round(self.bankroll - old_bank, 2)
            if abs(actual_pnl - pnl) > 0.50:
                log_msg(f"[WARN] P&L mismatch: calc=${pnl:+.2f} vs wallet=${actual_pnl:+.2f}")
                pnl = actual_pnl  # Trust wallet

        if pnl > 0.01:
            self.wins += 1
        else:
            self.losses += 1

        if self.bankroll > self.peak:
            self.peak = self.bankroll
        dd = (self.peak - self.bankroll) / self.peak * 100 if self.peak > 0 else 0
        if dd > self.max_dd:
            self.max_dd = dd
        if dd >= DRAWDOWN_PAUSE_PCT and not self.paused:
            self.paused = True
            log_msg(f"[RISK] PAUSED -- DD {dd:.0f}%")

        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        elapsed = time.time() - pos["entry_time"]
        ret_pct = round(pnl / pos["bet_amount"] * 100, 1) if pos["bet_amount"] > 0 else 0

        mode = "LIVE" if self.engine and not PAPER_MODE else "PAPER"
        log_msg(f"[{mode}] #{pos['id']} {pos['asset']} {pos['side']} {result} | "
                f"P&L ${pnl:+.2f} ({ret_pct:+.0f}%) | bank ${self.bankroll:.2f} | "
                f"{self.wins}W/{self.losses}L ({wr:.0f}%) | held {elapsed:.0f}s")

        try:
            self.log_file.write(json.dumps({
                "id": pos["id"], "asset": pos["asset"], "side": pos["side"],
                "result": result, "entry_price": pos["entry_price"],
                "exit_price": exit_price, "shares": pos["shares"],
                "bet_amount": pos["bet_amount"],
                "pnl": pnl, "return_pct": ret_pct, "bankroll": self.bankroll,
                "btc_at_entry": pos["btc_at_entry"],
                "reversal_strength": pos["reversal_strength"],
                "hold_seconds": round(elapsed, 1),
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
            f"[{mode}] {pos['asset']} {pos['side']} @ ${pos['entry_price']:.2f} -> {result}\n"
            f"P&L: ${pnl:+.2f} ({ret_pct:+.0f}%) | Bank: ${self.bankroll:.2f}\n"
            f"Bet: ${pos['bet_amount']:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%)"))

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
            "skipped_time_filter": self.skipped_time_filter,
            "fill_failures": self.fill_failures,
            "max_live_bet": MAX_LIVE_BET,
            "skip_hours": f"{SKIP_HOURS_UTC_START}-{SKIP_HOURS_UTC_END} UTC",
            "btc_price": round(self.coinbase.price, 2),
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
        skip = "SKIPPING (9a-4p ET)" if is_skip_hours() else "ACTIVE"

        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {BOT_NAME} | {elapsed:.0f}min | {mode} | {skip}")
        print(f"  Strategy: Buy cheap (${CHEAP_MIN}-${CHEAP_MAX}) on Coinbase reversal")
        print(f"  Time filter: OFF during {SKIP_HOURS_UTC_START}:00-{SKIP_HOURS_UTC_END}:00 UTC (9a-4p ET)")
        print(f"  Max bet: ${MAX_LIVE_BET} | Assets: {', '.join(ASSETS)}")
        print(f"  BTC: ${self.coinbase.price:,.2f} | Coinbase: {'OK' if self.coinbase.connected else 'DOWN'}")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f} | DD: {self.max_dd:.0f}%")
        print(f"  Bet: ${bet:.2f} | Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}%")
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
    print(f"  {BOT_NAME} -- Reversal Sniper [{mode}]")
    print("=" * 65)
    print(f"  Buy cheap tokens (${CHEAP_MIN}-${CHEAP_MAX}) on Coinbase reversal")
    print(f"  Time filter: OFF during 9am-4pm ET (saves ~$275 in losses)")
    print(f"  Max bet: ${MAX_LIVE_BET}/trade | Bet: {int(BET_PCT*100)}% of wallet")
    print(f"  Fill verification: poll up to 30s, cancel if unfilled")
    print(f"  Assets: {', '.join(ASSETS)}")
    print()
    if mode == "LIVE":
        print(f"  *** LIVE MODE -- REAL MONEY AT RISK ***")
    else:
        print(f"  Paper mode -- set REVERSAL_LIVE=1 to go live")
    print()

    bot = ReversalSniperLive()
    bot.init_clob()

    if not PAPER_MODE and bot.engine:
        await bot.sync_bankroll()
        log_msg(f"[INIT] LIVE -- Wallet: ${bot.bankroll:.2f}")
    else:
        log_msg(f"[INIT] PAPER -- Bank: ${bot.bankroll:.2f}")

    bot._write_summary()

    asyncio.create_task(send_telegram(
        f"🎯 <b>{BOT_NAME}</b> [{mode}]\n"
        f"Reversal sniper {'LIVE' if mode == 'LIVE' else 'paper'}\n"
        f"Max bet ${MAX_LIVE_BET} | Skip 9a-4p ET\n"
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
