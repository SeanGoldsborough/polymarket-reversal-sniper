#!/usr/bin/env python3
"""
PAPER SNIPE — Last-15s High Confidence Entry
==============================================
Strategy:
  - Wait until T-15 seconds before window close
  - Buy the side priced $0.96-$0.98 (direction is ~96% locked in)
  - Hold to resolution ($1.00)
  - SL at $0.49 (only if direction completely flips — rare at T-15)
  - Maker bid (0% fee), small profit per trade but very high WR

Math per trade:
  Win: $1.00 - $0.97 = +$0.03/sh (3% return, 0% fee as maker)
  Loss (SL): $0.49 - $0.97 = -$0.48/sh (rare at T-15)
  Breakeven WR: ~94% (need 16 wins per 1 loss)

Runs on both BTC and SOL.
Uses IRONCLAD execution engine.
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
STARTING_BANKROLL = 149.32
BET_PCT = 0.34
MAX_BET = 5000.0
FAK_FLOOR = 0.05

# Entry thresholds
MIN_ENTRY = 0.97            # Only buy if price >= this
MAX_ENTRY = 0.99            # Don't buy above this (too expensive, no edge)
ENTRY_SECONDS_BEFORE = 15   # Enter at T-15 seconds

# Stop loss
SL_PRICE = 0.49             # Only triggers if direction completely reverses

# Reactive hedge: if BTC crosses back toward threshold, buy the OTHER side
HEDGE_THRESHOLD_PCT = 0.00019  # Hedge when asset price is within ~0.019% of threshold
HEDGE_PCT = 0.10            # Hedge with 10% of bankroll on the other side
HEDGE_MAX_PRICE = 0.30      # Don't hedge if cheap side already above $0.30

# DRAWDOWN_PAUSE removed — compounding strategy handles losses via bet sizing

USDC_CONTRACT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PAPER_MODE = os.getenv("SNIPE_ALT_LIVE", "0") != "1"
ASSETS = ["ETH", "DOGE"]

# ── Coinbase Price Feeds (for hedge trigger) ───────────
# Maps asset name to Coinbase product ID
COINBASE_PRODUCTS = {
    "ETH": "ETH-USD",
    "DOGE": "DOGE-USD",
    "XRP": "XRP-USD",
    "BTC": "BTC-USD",
    "SOL": "SOL-USD",
}

class CoinbaseMultiFeed:
    """Single WebSocket tracking multiple assets."""
    def __init__(self, assets):
        self.assets = assets
        self.prices = {a: 0.0 for a in assets}
        self.window_open_prices = {a: 0.0 for a in assets}
        self.current_window = 0
        self.connected = False

    def get_price(self, asset):
        return self.prices.get(asset, 0.0)

    def get_window_open(self, asset):
        return self.window_open_prices.get(asset, 0.0)

    def _check_window(self):
        window = int(time.time()) // 300
        if window != self.current_window:
            self.current_window = window
            for a in self.assets:
                if self.prices[a] > 0:
                    self.window_open_prices[a] = self.prices[a]

    async def run(self):
        product_ids = [COINBASE_PRODUCTS[a] for a in self.assets if a in COINBASE_PRODUCTS]
        # Reverse lookup: product_id → asset
        prod_to_asset = {COINBASE_PRODUCTS[a]: a for a in self.assets if a in COINBASE_PRODUCTS}

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
                            "product_ids": product_ids,
                            "channels": ["ticker"]
                        })
                        await ws.send_str(sub)
                        self.connected = True
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                    if data.get("type") == "ticker":
                                        prod = data.get("product_id", "")
                                        p = float(data.get("price", 0))
                                        asset = prod_to_asset.get(prod)
                                        if asset and p > 0:
                                            self.prices[asset] = p
                                            self._check_window()
                                except Exception:
                                    pass
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except Exception:
                pass
            self.connected = False
            await asyncio.sleep(2)


os.makedirs("logs", exist_ok=True)
BOT_NAME = "SNIPE-ALT-LIVE" if not PAPER_MODE else "SNIPE-ALT-PAPER"
LOG_FILE = "logs/snipe_alt_live_trades.jsonl"
SUMMARY_FILE = "logs/snipe_alt_live_summary.json"


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


def get_clob_balance(client):
    """Read balance from CLOB API using correct signature_type."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams
        params = BalanceAllowanceParams(asset_type="COLLATERAL", token_id="", signature_type=2)
        result = client.get_balance_allowance(params)
        balance = int(result.get("balance", "0")) / 1_000_000
        return balance
    except Exception as e:
        print(f"  [WALLET] CLOB balance err: {e}", flush=True)
        return None


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


# ── Execution Engine (from IRONCLAD) ───────────────────
class ExecutionEngine:
    def __init__(self, client):
        self.client = client
        self._lock = asyncio.Lock()

    async def buy_taker(self, token_id, price, size):
        """FAK first (partial fill), FOK fallback (full fill)."""
        async with self._lock:
            if not self.client:
                return None
            buy_price = snap_price(price * 1.02)
            size = round(size)  # Integer shares
            # FAK first — gets partial fills immediately
            try:
                args = OrderArgs(price=buy_price, size=size, side=BUY, token_id=token_id)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.FAK)
                oid = resp.get("orderID") or resp.get("order_id") if resp else None
                if oid:
                    log_msg(f"[BUY] FAK {size}sh @ ${buy_price} order={oid[:8]}...")
                    return {"order_id": oid, "price": buy_price, "size": size, "token_id": token_id}
            except Exception as e:
                log_msg(f"[BUY] FAK: {e}")
            # FOK fallback — try full fill
            try:
                args = OrderArgs(price=buy_price, size=size, side=BUY, token_id=token_id)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.FOK)
                oid = resp.get("orderID") or resp.get("order_id") if resp else None
                if oid:
                    log_msg(f"[BUY] FOK {size}sh @ ${buy_price} order={oid[:8]}...")
                    return {"order_id": oid, "price": buy_price, "size": size, "token_id": token_id}
            except Exception as e:
                log_msg(f"[BUY] FOK: {e}")
            return None

    def check_order_status(self, order_id):
        """Verify fill via CLOB API."""
        try:
            order = self.client.get_order(order_id)
            if order:
                status = order.get("status", "")
                size_matched = float(order.get("size_matched", "0") or "0")
                if status == "MATCHED" or size_matched > 0:
                    return True, size_matched
            return False, 0
        except:
            return False, 0

    async def emergency_sell(self, token_id, size, reason="SL"):
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
                        return True
                except Exception as e:
                    if "balance" in str(e).lower():
                        try:
                            self.client.update_balance_allowance(int(sell_size * 0.9 * 1_000_000))
                        except Exception:
                            pass
                    await asyncio.sleep(0.3)
            return False

    async def cancel_order(self, order_id):
        async with self._lock:
            if not self.client:
                return
            try:
                self.client.cancel(order_id)
            except Exception:
                pass


# ── Snipe Bot ──────────────────────────────────────────
class SnipeBot:
    def __init__(self, coinbase):
        self.mf = MarketFinder()
        self.coinbase = coinbase
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
        self.unfilled = 0
        self.sl_hits = 0
        # Track per-asset stats
        self.asset_stats = {a: {"wins": 0, "losses": 0, "pnl": 0} for a in ASSETS}

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
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            self.engine = ExecutionEngine(self.client)
            log_msg("[CLOB] Auth OK — LIVE execution ready")
            # Sync bankroll from wallet
            bal = get_clob_balance(self.client)
            if bal and bal > 0:
                self.bankroll = bal
                self.peak = bal
                log_msg(f"[WALLET] Balance: ${bal:.2f}")
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
                log_msg(f"[LOOP] Window cycle start, bankroll=${self.bankroll:.2f}")

                if self.bankroll < 3:
                    log_msg(f"[RISK] Bankroll ${self.bankroll:.2f} too low")
                    continue

                await self.mf.refresh_all()
                # Sync bankroll from wallet every window
                if self.engine:
                    bal = get_clob_balance(self.client)
                    if bal and bal > 0:
                        self.bankroll = bal
                log_msg(f"[SCAN] Markets: {list(self.mf.markets.keys())} bank=${self.bankroll:.2f}")

                # Wait until T-15 for each asset
                window_start = (int(time.time()) // 300) * 300
                entry_time = window_start + 300 - ENTRY_SECONDS_BEFORE

                now = time.time()
                if now < entry_time:
                    await asyncio.sleep(entry_time - now)

                # Scan all assets for snipe opportunities
                for asset in ASSETS:
                    if asset not in self.mf.markets:
                        continue
                    await self._snipe(asset, self.mf.markets[asset])

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                await asyncio.sleep(5)

    async def _snipe(self, asset, mkt):
        """At T-15, buy the high-confidence side ($0.96-$0.98)."""
        up_book, down_book = await asyncio.gather(
            get_book(mkt["up"]), get_book(mkt["down"]))
        if not up_book or not down_book:
            return

        up_bid = up_book["bid"]
        down_bid = down_book["bid"]
        up_ask = up_book["ask"]
        down_ask = down_book["ask"]

        # Find the high-confidence side
        target_side = None
        target_token = None
        target_book = None

        if MIN_ENTRY <= up_bid <= MAX_ENTRY:
            target_side = "UP"
            target_token = mkt["up"]
            target_book = up_book
        elif MIN_ENTRY <= down_bid <= MAX_ENTRY:
            target_side = "DOWN"
            target_token = mkt["down"]
            target_book = down_book

        if not target_side:
            self.unfilled += 1
            return

        # Maker bid at the bid price (we're joining the bid, 0% fee)
        entry_price = snap_price(target_book["bid"])
        if entry_price < MIN_ENTRY or entry_price > MAX_ENTRY:
            self.unfilled += 1
            return

        bet = min(self.bankroll * BET_PCT, MAX_BET)
        shares = round(bet / entry_price, 2)
        shares = round(max(shares, 5.0))  # Integer shares
        if shares * entry_price > self.bankroll * 0.5:  # safety: never bet more than 50% of wallet
            return

        self.trade_count += 1
        tid = self.trade_count
        profit_per_sh = round(1.00 - entry_price, 2)

        log_msg(f"[SNIPE] #{tid} {asset} {target_side} @ ${entry_price:.2f} ({shares}sh) "
                f"| +${profit_per_sh}/sh if win | {mkt['question']}")

        # Execute buy
        if self.engine:
            order = await self.engine.buy_taker(target_token, entry_price, shares)
            if not order:
                log_msg(f"[SNIPE] #{tid} FOK failed")
                self.unfilled += 1
                return
            # Verify fill
            await asyncio.sleep(1)
            filled, filled_size = self.engine.check_order_status(order["order_id"])
            if filled and filled_size > 0:
                shares = filled_size
                log_msg(f"[SNIPE] #{tid} CONFIRMED {shares}sh @ ${entry_price:.2f}")
            else:
                log_msg(f"[SNIPE] #{tid} Fill not confirmed - proceeding with {shares}sh")
        else:
            log_msg(f"[PAPER] #{tid} Simulated buy {shares}sh @ ${entry_price:.2f}")

        # Determine the OTHER side token for potential hedge
        if target_side == "UP":
            hedge_token = mkt["down"]
            hedge_side = "DOWN"
        else:
            hedge_token = mkt["up"]
            hedge_side = "UP"

        # Monitor for SL or resolution (only ~15 seconds left + 60s resolution wait)
        window_end = (int(time.time()) // 300 + 1) * 300
        monitor_end = window_end + 60
        hedge_placed = False
        hedge_shares = 0
        hedge_cost = 0
        hedge_entry = 0

        while time.time() < monitor_end:
            book = await get_book(target_token)
            if not book:
                await asyncio.sleep(0.5)
                continue

            bid = book["bid"]

            # WIN: resolved to $1.00
            if bid >= 0.95:
                pnl = round(shares * (1.00 - entry_price), 4)
                # If we hedged, the hedge loses
                if hedge_placed:
                    hedge_loss = round(-hedge_cost, 4)
                    pnl += hedge_loss
                    log_msg(f"[HEDGE] #{tid} WIN — hedge lost ${hedge_loss:.2f} (net P&L ${pnl:+.2f})")
                self._record_trade(tid, asset, target_side, entry_price, 1.00, shares, pnl, "WIN", mkt["question"],
                                   hedge_placed=hedge_placed, hedge_cost=hedge_cost, hedge_shares=hedge_shares, hedge_entry=hedge_entry)
                return

            # ── REACTIVE HEDGE: if asset price crosses back toward threshold, buy OTHER side ──
            asset_price = self.coinbase.get_price(asset)
            price_to_beat = self.coinbase.get_window_open(asset)

            if not hedge_placed and time.time() < window_end and asset_price > 0 and price_to_beat > 0:
                threshold_abs = price_to_beat * HEDGE_THRESHOLD_PCT

                should_hedge = False
                if target_side == "UP" and price_to_beat > 0:
                    # We need asset > price_to_beat. Hedge if asset drops near/below it.
                    if asset_price <= price_to_beat + threshold_abs:
                        should_hedge = True
                elif target_side == "DOWN" and price_to_beat > 0:
                    # We need asset < price_to_beat. Hedge if asset rises near/above it.
                    if asset_price >= price_to_beat - threshold_abs:
                        should_hedge = True

                if should_hedge:
                    hedge_book = await get_book(hedge_token)
                    if hedge_book and hedge_book["ask"] <= HEDGE_MAX_PRICE and hedge_book["ask"] > 0.01:
                        hedge_bet = round(self.bankroll * HEDGE_PCT, 2)
                        hedge_price = snap_price(min(hedge_book["ask"] + 0.01, 0.99))
                        hedge_shares = round(max(hedge_bet / hedge_price, 5))
                        hedge_entry = hedge_price
                        hedge_cost = round(hedge_shares * hedge_price, 4)

                        diff_from_beat = asset_price - price_to_beat
                        log_msg(f"[HEDGE] #{tid} {asset} ${asset_price:,.4f} near threshold ${price_to_beat:,.4f} (${diff_from_beat:+,.4f})")
                        log_msg(f"[HEDGE] #{tid} Buying {hedge_shares}sh {hedge_side} @ ${hedge_price:.2f} (${hedge_cost:.2f})")

                        if self.engine:
                            hedge_order = await self.engine.buy_taker(hedge_token, hedge_price, hedge_shares)
                            if hedge_order:
                                hedge_placed = True
                                hedge_shares = hedge_order.get("size", hedge_shares)
                                log_msg(f"[HEDGE] #{tid} FILLED {hedge_shares}sh {hedge_side} @ ${hedge_price:.2f}")
                            else:
                                log_msg(f"[HEDGE] #{tid} Fill FAILED")
                        else:
                            hedge_placed = True
                            log_msg(f"[HEDGE-PAPER] #{tid} Simulated {hedge_shares}sh {hedge_side} @ ${hedge_price:.2f}")

            # SL: direction completely reversed
            if bid <= SL_PRICE and time.time() < window_end:
                sl_fee = round(shares * bid * 0.018, 4)
                pnl = round(shares * (bid - entry_price) - sl_fee, 4)
                if self.engine:
                    await self.engine.emergency_sell(target_token, shares, "SL")
                self.sl_hits += 1
                # Hedge will resolve separately — if hedge is on the winning side, it profits
                if hedge_placed:
                    # Hedge side is winning since our side lost
                    hedge_pnl = round(hedge_shares * (1.00 - hedge_entry), 4)
                    pnl += hedge_pnl
                    log_msg(f"[HEDGE] #{tid} SL — hedge wins +${hedge_pnl:.2f} (net P&L ${pnl:+.2f})")
                self._record_trade(tid, asset, target_side, entry_price, bid, shares, pnl, "SL+HEDGE" if hedge_placed else "SL", mkt["question"],
                                   hedge_placed=hedge_placed, hedge_cost=hedge_cost, hedge_shares=hedge_shares, hedge_entry=hedge_entry)
                return

            # LOSS: resolved to $0
            if bid <= 0.05 and time.time() > window_end:
                pnl = round(-shares * entry_price, 4)
                if self.engine:
                    await self.engine.emergency_sell(target_token, shares, "LOSS")
                # Hedge wins!
                if hedge_placed:
                    hedge_pnl = round(hedge_shares * (1.00 - hedge_entry), 4)
                    pnl += hedge_pnl
                    log_msg(f"[HEDGE] #{tid} LOSS → hedge wins +${hedge_pnl:.2f} (net P&L ${pnl:+.2f})")
                self._record_trade(tid, asset, target_side, entry_price, 0, shares, pnl, "LOSS+HEDGE" if hedge_placed else "LOSS", mkt["question"],
                                   hedge_placed=hedge_placed, hedge_cost=hedge_cost, hedge_shares=hedge_shares, hedge_entry=hedge_entry)
                return

            await asyncio.sleep(0.5)  # Poll faster for hedge detection

        # Timeout — determine by final bid
        book = await get_book(target_token)
        final_bid = book["bid"] if book else 0
        if final_bid > 0.5:
            pnl = round(shares * (1.00 - entry_price), 4)
            if hedge_placed:
                hedge_loss = round(-hedge_cost, 4)
                pnl += hedge_loss
            self._record_trade(tid, asset, target_side, entry_price, 1.00, shares, pnl, "WIN-LATE", mkt["question"],
                               hedge_placed=hedge_placed, hedge_cost=hedge_cost, hedge_shares=hedge_shares, hedge_entry=hedge_entry)
        else:
            pnl = round(-shares * entry_price, 4)
            if self.engine:
                await self.engine.emergency_sell(target_token, shares, "EXP")
            self._record_trade(tid, asset, target_side, entry_price, 0, shares, pnl, "LOSS", mkt["question"],
                               hedge_placed=hedge_placed, hedge_cost=hedge_cost, hedge_shares=hedge_shares, hedge_entry=hedge_entry)

    def _record_trade(self, tid, asset, side, entry, exit_price, shares, pnl, result, question,
                       hedge_placed=False, hedge_cost=0, hedge_shares=0, hedge_entry=0):
        # Always use calculated P&L (wallet sync causes false readings with shared wallet)
        self.bankroll = round(self.bankroll + pnl, 4)

        if pnl > 0.01:
            self.wins += 1
            self.asset_stats[asset]["wins"] += 1
        elif pnl < -0.01:
            self.losses += 1
            self.asset_stats[asset]["losses"] += 1
        else:
            self.flat += 1

        self.asset_stats[asset]["pnl"] = round(self.asset_stats[asset]["pnl"] + pnl, 4)

        if self.bankroll > self.peak:
            self.peak = self.bankroll
        dd = (self.peak - self.bankroll) / self.peak * 100 if self.peak > 0 else 0
        if dd > self.max_dd:
            self.max_dd = dd

        total = self.wins + self.losses + self.flat
        wr = self.wins / total * 100 if total else 0
        ret_pct = round(pnl / (shares * entry) * 100, 1) if entry > 0 else 0

        hedge_str = ""
        if hedge_placed:
            hedge_str = f" | HEDGE: {hedge_shares}sh @ ${hedge_entry:.2f} (${hedge_cost:.2f})"

        log_msg(f"[{result}] #{tid} {asset} {side} @ ${entry:.2f}→${exit_price:.2f} | "
                f"P&L ${pnl:+.2f} ({ret_pct:+.1f}%) | bank ${self.bankroll:.2f} | "
                f"{self.wins}W/{self.losses}L ({wr:.0f}%){hedge_str}")

        try:
            self.log_file.write(json.dumps({
                "id": tid, "asset": asset, "side": side,
                "entry": entry, "exit": exit_price, "shares": shares,
                "pnl": pnl, "return_pct": ret_pct, "result": result,
                "bankroll": self.bankroll, "question": question,
                "hedge_placed": hedge_placed, "hedge_cost": round(hedge_cost, 4),
                "hedge_shares": hedge_shares, "hedge_entry": round(hedge_entry, 4),
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except Exception:
            pass

        self._write_summary()

        icon = "🟢" if pnl > 0 else "🔴"
        asyncio.create_task(send_telegram(
            f"{icon} <b>{BOT_NAME} #{tid}</b>\n"
            f"{asset} {side} @ ${entry:.2f} → {result}\n"
            f"P&L: ${pnl:+.2f} ({ret_pct:+.1f}%) | Bank: ${self.bankroll:.2f}\n"
            f"{self.wins}W/{self.losses}L ({wr:.0f}%)"))

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
            "unfilled": self.unfilled,
            "sl_hits": self.sl_hits,
            "entry_range": f"${MIN_ENTRY}-${MAX_ENTRY}",
            "sl_price": SL_PRICE,
            "assets": ASSETS,
            "asset_stats": self.asset_stats,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(SUMMARY_FILE, summary)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses + self.flat
        wr = self.wins / total * 100 if total else 0
        pnl = self.bankroll - STARTING_BANKROLL
        bet = min(self.bankroll * BET_PCT, MAX_BET)
        be_wr = round(0.48 / (0.48 + 0.03) * 100, 0)

        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {BOT_NAME} | {elapsed:.0f}min | {'PAUSED' if self.paused else 'LIVE' if self.engine else 'PAPER'}")
        print(f"  Strategy: Buy ${MIN_ENTRY}-${MAX_ENTRY} at T-{ENTRY_SECONDS_BEFORE}s | Hold to $1.00 | SL ${SL_PRICE}")
        print(f"  Win: +$0.02-$0.04/sh | Loss (SL): -$0.48/sh | Breakeven WR: ~{be_wr}%")
        print(f"  Assets: {', '.join(ASSETS)} | Maker 0% fee")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f} | DD: {self.max_dd:.0f}%")
        print(f"  Bet: ${bet:.2f} | Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}% | SL: {self.sl_hits}")
        for a in ASSETS:
            s = self.asset_stats[a]
            at = s["wins"] + s["losses"]
            awr = s["wins"] / at * 100 if at else 0
            print(f"    {a}: {at}t {s['wins']}W/{s['losses']}L ({awr:.0f}%) P&L ${s['pnl']:+.2f}")
        print()


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        bot.print_status()


async def main():
    print("=" * 65)
    print(f"  {BOT_NAME} — Last-15s High Confidence Snipe")
    print("=" * 65)
    print(f"  Buy at ${MIN_ENTRY}-${MAX_ENTRY} with T-{ENTRY_SECONDS_BEFORE}s remaining")
    print(f"  Hold to resolution ($1.00) | SL @ ${SL_PRICE}")
    print(f"  Win: +$0.02-$0.04/sh (0% maker fee) | Loss: -$0.48/sh")
    print(f"  Assets: {', '.join(ASSETS)} | Bankroll: ${STARTING_BANKROLL} | Bet: {int(BET_PCT*100)}%")
    print()

    coinbase = CoinbaseMultiFeed(ASSETS)
    bot = SnipeBot(coinbase)
    bot.init_clob()
    bot._write_summary()

    log_msg(f"[INIT] {'LIVE' if bot.engine else 'PAPER'} mode — Bank: ${bot.bankroll:.2f}")
    log_msg(f"[INIT] Hedge: asset within ~{HEDGE_THRESHOLD_PCT*100:.3f}% of threshold → buy other side ({int(HEDGE_PCT*100)}%)")

    # Wait for Coinbase to connect
    log_msg(f"[COINBASE] Connecting to {', '.join(ASSETS)}...")
    asyncio.create_task(coinbase.run())
    for _ in range(30):
        if coinbase.connected and all(coinbase.get_price(a) > 0 for a in ASSETS):
            break
        await asyncio.sleep(0.5)
    if coinbase.connected:
        prices_str = " | ".join(f"{a} ${coinbase.get_price(a):,.4f}" for a in ASSETS)
        log_msg(f"[COINBASE] Connected — {prices_str}")
    else:
        log_msg("[COINBASE] Not fully connected — hedge will be disabled until connected")

    asyncio.create_task(send_telegram(
        f"🎯 <b>{BOT_NAME}</b>\n"
        f"Last-15s snipe @ ${MIN_ENTRY}-${MAX_ENTRY}\n"
        f"Hedge: ±{HEDGE_THRESHOLD_PCT*100:.3f}% of threshold → buy other side\n"
        f"SL ${SL_PRICE} | {', '.join(ASSETS)}\n"
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
