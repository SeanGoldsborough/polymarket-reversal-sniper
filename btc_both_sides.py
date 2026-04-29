#!/usr/bin/env python3
"""
BTC-BOTH-SIDES — Buy Both Sides at $0.47 on BTC 5-Min Up/Down
================================================================
Strategy:
  - At window open, place GTC maker bids at $0.47 on BOTH Up and Down
  - Monitor via WebSocket for fills
  - If one fills, check if market has decided:
    - Other side ask < $0.60 → keep both bids (still undecided)
    - Other side ask > $0.60 → cancel unfilled bid (market decided)
  - If both fill: guaranteed profit ($1.00 - $0.94 = $0.06/pair)
  - If one fills: hold to resolution
  - Cancel all unfilled at T-30

Entry: Window open (T+10)
Execution: GTC maker limit bids at $0.47 (0% fee)
Monitor: Polymarket WebSocket for real-time book updates
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
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import OrderArgs, OrderType, BalanceAllowanceParams
    from py_clob_client_v2.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────
BID_LOW = 0.40                             # Only buy if ask >= this
BID_HIGH = 0.60                            # Only buy if ask <= this
SHARES_PER_SIDE = 5                        # Fixed 5 shares per side (minimum)
MAX_COMBINED_COST = 1.05                   # Max combined per-share cost
SL_PRICE = 0.20                            # Stop loss — sell if bid drops to $0.20
ENTRY_DELAY = 10                           # Place bids T+10 seconds into window
CANCEL_BEFORE_END = 30                     # Cancel unfilled at T-30

USDC_CONTRACT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PAPER_MODE = os.getenv("BTC_BOTH_SIDES_LIVE", "0") != "1"
POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.makedirs("logs", exist_ok=True)
BOT_NAME = "BTC-BOTH-SIDES" if not PAPER_MODE else "BTC-BOTH-SIDES-PAPER"
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
            self._current_window_start = window_start
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

    async def refresh_next(self):
        """Get the NEXT window's market (1 window ahead)."""
        try:
            now = int(time.time())
            current_window = (now // 300) * 300
            next_window = current_window + 300
            slug = f"btc-updown-5m-{next_window}"
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
                                "window_start": next_window,
                            }
                            log_msg(f"[MKT] Next window: {m.get('question', '')[:50]}")
                        else:
                            log_msg(f"[MKT] Next window not available yet")
        except Exception as e:
            log_msg(f"[MKT] Next: {e}")


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
                    ask_depth_at_target = sum(float(a["size"]) for a in asks if float(a["price"]) <= BID_HIGH)
                    return {"bid": bb, "ask": ba, "depth_at_target": ask_depth_at_target}
    except Exception:
        pass
    return None


class BTCBothSidesBot:
    def __init__(self):
        self.mf = MarketFinder()
        self.client = None
        self.bankroll = 100.0
        self.starting_bankroll = 100.0
        self.peak = 100.0
        self.max_dd = 0
        self.wins = 0
        self.losses = 0
        self.both_filled = 0
        self.one_filled = 0
        self.no_fills = 0
        self.trade_count = 0
        self.start_time = time.time()
        self.log_file = open(LOG_FILE, "a")

    def init_clob(self):
        if PAPER_MODE:
            log_msg("[CLOB] PAPER MODE")
            return
        if not CLOB_AVAILABLE or not PRIVATE_KEY:
            log_msg("[CLOB] No client — paper mode")
            return
        try:
            self.client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                                     signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS)
            self.client.set_api_creds(self.client.create_or_derive_api_key())
            log_msg("[CLOB] Auth OK — LIVE execution ready")
            # Read wallet balance
            try:
                from py_clob_client_v2.clob_types import BalanceAllowanceParams
                params = BalanceAllowanceParams(asset_type="COLLATERAL", token_id="", signature_type=2)
                result = self.client.get_balance_allowance(params)
                bal = int(result.get("balance", "0")) / 1_000_000
                if bal > 0:
                    self.bankroll = bal
                    self.peak = bal
                    self.starting_bankroll = bal
                    log_msg(f"[WALLET] Balance: ${bal:.2f}")
            except:
                pass
        except Exception as e:
            log_msg(f"[CLOB] Auth fail: {e}")

    async def run(self):
        while True:
            try:
                # At start of current window, get NEXT window's market and place orders
                now = time.time()
                nxt = (int(now) // 300 + 1) * 300
                wait = nxt - now
                if wait > 0:
                    await asyncio.sleep(wait)
                await asyncio.sleep(5)  # Brief delay for market to be available

                log_msg(f"[LOOP] Window start | Bank: ${self.bankroll:.2f}")

                if self.bankroll < 5:
                    log_msg(f"[RISK] Bankroll too low")
                    continue

                # Get the NEXT window's market (prices near $0.50)
                await self.mf.refresh_next()
                if not self.mf.market:
                    # Fallback to current window
                    await self.mf.refresh()
                if not self.mf.market:
                    log_msg("[LOOP] No market found")
                    continue

                await self._trade_window()

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(5)

    async def _trade_window(self):
        mkt = self.mf.market
        if not mkt:
            return

        self.trade_count += 1
        tid = self.trade_count
        # Use the market's window_start (could be next window)
        target_window_start = mkt.get("window_start", (int(time.time()) // 300) * 300)
        window_end = target_window_start + 300
        cancel_time = window_end - CANCEL_BEFORE_END

        up_token = mkt["up"]
        down_token = mkt["down"]
        shares_per_side = SHARES_PER_SIDE

        # ── PLACE BIDS ON BOTH SIDES ──
        log_msg(f"[PLACE] #{tid} Buying both sides ${BID_LOW}-${BID_HIGH} | SL ${SL_PRICE} | {mkt['question'][:40]}")

        up_order_id = None
        down_order_id = None

        up_order_id = None
        down_order_id = None

        if self.client and not PAPER_MODE:
            # Get current asks to set bid price
            up_book_init = await get_book(up_token)
            down_book_init = await get_book(down_token)
            up_bid_price = min(up_book_init["ask"], BID_HIGH) if up_book_init and BID_LOW <= up_book_init["ask"] <= BID_HIGH else 0.50
            down_bid_price = min(down_book_init["ask"], BID_HIGH) if down_book_init and BID_LOW <= down_book_init["ask"] <= BID_HIGH else 0.50

            try:
                args = OrderArgs(price=up_bid_price, size=shares_per_side, side=BUY, token_id=up_token)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.GTC)
                up_order_id = resp.get("orderID", "")
                log_msg(f"[BID-UP] GTC {shares_per_side}sh @ ${up_bid_price:.2f} order={up_order_id[:10]}...")
            except Exception as e:
                log_msg(f"[BID-UP] FAILED: {str(e)[:60]}")

            try:
                args = OrderArgs(price=down_bid_price, size=shares_per_side, side=BUY, token_id=down_token)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.GTC)
                down_order_id = resp.get("orderID", "")
                log_msg(f"[BID-DN] GTC {shares_per_side}sh @ ${down_bid_price:.2f} order={down_order_id[:10]}...")
            except Exception as e:
                log_msg(f"[BID-DN] FAILED: {str(e)[:60]}")
        else:
            log_msg(f"[PAPER] Watching for asks in ${BID_LOW}-${BID_HIGH} range")

        # ── MONITOR VIA WEBSOCKET ──
        up_filled = False
        down_filled = False
        up_fill_price = 0
        down_fill_price = 0
        up_cancelled = False
        down_cancelled = False
        book_history = []

        up_ask = 0
        down_ask = 0
        up_shares = shares_per_side
        down_shares = shares_per_side
        up_sl_hit = False
        down_sl_hit = False
        both_logged = False
        up_sl_price = 0
        down_sl_price = 0
        last_log_time = 0

        try:
            async with websockets.connect(POLY_WS_URL, ping_interval=20, ping_timeout=10,
                                           close_timeout=5) as ws:
                sub_msg = json.dumps({"assets_ids": [up_token, down_token], "type": "market"})
                await ws.send(sub_msg)
                log_msg(f"[WS] #{tid} Connected — monitoring both sides")

                async for raw in ws:
                    now = time.time()
                    if now >= cancel_time:
                        break

                    try:
                        msg = json.loads(raw)
                    except:
                        continue

                    # Parse message — can be a list (initial snapshot) or dict (update)
                    items = []
                    if isinstance(msg, list):
                        items = [m for m in msg if isinstance(m, dict)]
                    elif isinstance(msg, dict) and ("asks" in msg or "bids" in msg):
                        items = [msg]

                    for item in items:
                        aid = item.get("asset_id", "")
                        asks = item.get("asks", [])

                        if asks and isinstance(asks, list):
                            try:
                                best = min(float(a["price"]) for a in asks if isinstance(a, dict) and "price" in a)
                                if aid == up_token:
                                    up_ask = best
                                elif aid == down_token:
                                    down_ask = best
                            except (ValueError, KeyError):
                                pass

                    # Fill detection: buy when ask is in range $0.45-$0.55
                    elapsed = now - window_start

                    if not up_filled and BID_LOW <= up_ask <= BID_HIGH:
                        up_filled = True
                        up_fill_price = up_ask
                        up_shares = round(SHARES_PER_SIDE / up_ask)
                        if up_shares < 5:
                            up_shares = 5
                        log_msg(f"[FILL-UP] #{tid} {up_shares}sh @ ${up_fill_price:.2f} | "
                                f"DOWN ask=${down_ask:.2f} | T+{elapsed:.0f}s")

                    if not down_filled and BID_LOW <= down_ask <= BID_HIGH:
                        down_filled = True
                        down_fill_price = down_ask
                        down_shares = round(SHARES_PER_SIDE / down_ask)
                        if down_shares < 5:
                            down_shares = 5
                        log_msg(f"[FILL-DN] #{tid} {down_shares}sh @ ${down_fill_price:.2f} | "
                                f"UP ask=${up_ask:.2f} | T+{elapsed:.0f}s")

                    if up_filled and down_filled and not both_logged:
                        both_logged = True
                        combined = up_fill_price + down_fill_price
                        log_msg(f"[BOTH] #{tid} Both filled! UP ${up_fill_price:.2f} + DOWN ${down_fill_price:.2f} = "
                                f"${combined:.2f} | SL ${SL_PRICE} on both sides — monitoring for SL")
                        # Pre-approve both tokens for selling (so SL executes instantly)
                        if self.client and not PAPER_MODE:
                            try:
                                params_up = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=up_token, signature_type=SIGNATURE_TYPE)
                                self.client.update_balance_allowance(params_up)
                                params_dn = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=down_token, signature_type=SIGNATURE_TYPE)
                                self.client.update_balance_allowance(params_dn)
                                log_msg(f"[APPROVED] #{tid} Both tokens pre-approved for SL sell")
                            except Exception as e:
                                log_msg(f"[APPROVE] #{tid} Failed: {str(e)[:60]}")

                    # SL check on BOTH filled sides (or one-fill)
                    for item in items:
                        aid = item.get("asset_id", "")
                        bids_data = item.get("bids", [])
                        if not bids_data:
                            continue
                        try:
                            best_bid = max(float(b["price"]) for b in bids_data if isinstance(b, dict) and "price" in b)
                        except (ValueError, KeyError):
                            continue

                        if aid == up_token and up_filled and not up_sl_hit and best_bid <= SL_PRICE and best_bid > 0.01:
                            up_sl_hit = True
                            up_sl_price = best_bid
                            log_msg(f"[SL-UP] #{tid} UP bid ${best_bid:.2f} <= SL ${SL_PRICE} — selling")
                            if self.client and not PAPER_MODE:
                                for attempt in range(3):
                                    try:
                                        sell_price = snap_price(max(best_bid - 0.02, 0.01))
                                        args = OrderArgs(price=sell_price, size=up_shares, side=SELL, token_id=up_token)
                                        signed = self.client.create_order(args)
                                        self.client.post_order(signed, OrderType.FAK)
                                        log_msg(f"[SL-SOLD-UP] #{tid} FAK sell {up_shares}sh @ ${sell_price}")
                                        break
                                    except Exception as e:
                                        log_msg(f"[SL-SELL-UP] #{tid} Attempt {attempt+1}: {str(e)[:60]}")

                        if aid == down_token and down_filled and not down_sl_hit and best_bid <= SL_PRICE and best_bid > 0.01:
                            down_sl_hit = True
                            down_sl_price = best_bid
                            log_msg(f"[SL-DN] #{tid} DOWN bid ${best_bid:.2f} <= SL ${SL_PRICE} — selling")
                            if self.client and not PAPER_MODE:
                                for attempt in range(3):
                                    try:
                                        sell_price = snap_price(max(best_bid - 0.02, 0.01))
                                        args = OrderArgs(price=sell_price, size=down_shares, side=SELL, token_id=down_token)
                                        signed = self.client.create_order(args)
                                        self.client.post_order(signed, OrderType.FAK)
                                        log_msg(f"[SL-SOLD-DN] #{tid} FAK sell {down_shares}sh @ ${sell_price}")
                                        break
                                    except Exception as e:
                                        log_msg(f"[SL-SELL-DN] #{tid} Attempt {attempt+1}: {str(e)[:60]}")

                    # Record snapshot every 10s
                    if int(elapsed) % 10 == 0 and up_ask > 0 and down_ask > 0:
                        book_history.append({"t": round(elapsed), "up_ask": up_ask, "down_ask": down_ask})

                    # Log every 30s
                    if now - last_log_time >= 30 and elapsed > 5:
                        last_log_time = now
                        log_msg(f"[WATCH] #{tid} T+{elapsed:.0f}s | UP ask=${up_ask:.2f} DN ask=${down_ask:.2f} | "
                                f"fills: UP={'Y' if up_filled else 'N'} DN={'Y' if down_filled else 'N'}")

        except Exception as e:
            log_msg(f"[WS] #{tid} Error: {e}")
            # Fallback: one HTTP check
            up_book = await get_book(up_token)
            down_book = await get_book(down_token)
            if up_book:
                up_ask = up_book["ask"]
            if down_book:
                down_ask = down_book["ask"]

        # ── CANCEL UNFILLED AT T-30 ──
        if not up_filled:
            log_msg(f"[CANCEL-UP] #{tid} Unfilled — cancelled at T-30")
        if not down_filled:
            log_msg(f"[CANCEL-DN] #{tid} Unfilled — cancelled at T-30")

        # ── DETERMINE RESULT ──
        both_sides = up_filled and down_filled
        any_fill = up_filled or down_filled

        if not any_fill:
            self.no_fills += 1
            min_up = min((s["up_ask"] for s in book_history), default=0)
            min_down = min((s["down_ask"] for s in book_history), default=0)
            log_msg(f"[SKIP] #{tid} No fills | Lowest asks: UP ${min_up:.2f} DOWN ${min_down:.2f}")
            return

        up_cost = up_shares * up_fill_price if up_filled else 0
        down_cost = down_shares * down_fill_price if down_filled else 0
        total_cost = up_cost + down_cost

        if both_sides:
            self.both_filled += 1
        else:
            self.one_filled += 1

        if not both_sides and up_filled:
            log_msg(f"[WATCH-RES] #{tid} One fill (UP) — holding to resolution, SL ${SL_PRICE}")
        elif not both_sides and down_filled:
            log_msg(f"[WATCH-RES] #{tid} One fill (DOWN) — holding to resolution, SL ${SL_PRICE}")
        elif both_sides:
            log_msg(f"[WATCH-RES] #{tid} Both filled — holding to resolution, SL ${SL_PRICE} on both")

        # ── WAIT FOR RESOLUTION ──
        for _ in range(90):
            up_book = await get_book(up_token)
            if up_book:
                # Check SL on filled sides (HTTP fallback for WS SL)
                if up_filled and not up_sl_hit and up_book["bid"] <= SL_PRICE and up_book["bid"] > 0.01:
                    up_sl_hit = True
                    up_sl_price = up_book["bid"]
                    log_msg(f"[SL-UP] #{tid} UP bid ${up_sl_price:.2f} <= SL ${SL_PRICE} — selling")
                    if self.client and not PAPER_MODE:
                        for attempt in range(3):
                            try:
                                sell_price = snap_price(max(up_sl_price - 0.02, 0.01))
                                args = OrderArgs(price=sell_price, size=up_shares, side=SELL, token_id=up_token)
                                signed = self.client.create_order(args)
                                self.client.post_order(signed, OrderType.FAK)
                                log_msg(f"[SL-SOLD-UP] #{tid} FAK sell {up_shares}sh @ ${sell_price}")
                                break
                            except Exception as e:
                                log_msg(f"[SL-SELL-UP] #{tid} Attempt {attempt+1}: {str(e)[:60]}")

                down_book_check = await get_book(down_token)
                if down_book_check and down_filled and not down_sl_hit and down_book_check["bid"] <= SL_PRICE and down_book_check["bid"] > 0.01:
                    down_sl_hit = True
                    down_sl_price = down_book_check["bid"]
                    log_msg(f"[SL-DN] #{tid} DOWN bid ${down_sl_price:.2f} <= SL ${SL_PRICE} — selling")
                    if self.client and not PAPER_MODE:
                        for attempt in range(3):
                            try:
                                sell_price = snap_price(max(down_sl_price - 0.02, 0.01))
                                args = OrderArgs(price=sell_price, size=down_shares, side=SELL, token_id=down_token)
                                signed = self.client.create_order(args)
                                self.client.post_order(signed, OrderType.FAK)
                                log_msg(f"[SL-SOLD-DN] #{tid} FAK sell {down_shares}sh @ ${sell_price}")
                                break
                            except Exception as e:
                                log_msg(f"[SL-SELL-DN] #{tid} Attempt {attempt+1}: {str(e)[:60]}")

                if up_book["bid"] >= 0.95:
                    winner = "UP"
                    up_pnl = (up_shares * 1.00 - up_cost) if up_filled else 0
                    # DOWN: SL recovery or full loss
                    if down_sl_hit and down_filled:
                        down_pnl = down_shares * down_sl_price - down_cost
                    elif down_filled:
                        down_pnl = -down_cost
                    else:
                        down_pnl = 0
                    pnl = round(up_pnl + down_pnl, 4)
                    result = "WIN-BOTH+SL" if both_sides and down_sl_hit else ("WIN-BOTH" if both_sides else ("WIN" if up_filled else "LOSS"))
                    self._record_trade(tid, pnl, result, total_cost, up_cost, down_cost,
                                       up_shares if up_filled else 0,
                                       down_shares if down_filled else 0,
                                       winner, mkt["question"], both_sides, book_history)
                    return
                if up_book["bid"] <= 0.05:
                    winner = "DOWN"
                    down_pnl = (down_shares * 1.00 - down_cost) if down_filled else 0
                    # UP: SL recovery or full loss
                    if up_sl_hit and up_filled:
                        up_pnl = up_shares * up_sl_price - up_cost
                    elif up_filled:
                        up_pnl = -up_cost
                    else:
                        up_pnl = 0
                    pnl = round(up_pnl + down_pnl, 4)
                    result = "WIN-BOTH+SL" if both_sides and up_sl_hit else ("WIN-BOTH" if both_sides else ("WIN" if down_filled else "LOSS"))
                    self._record_trade(tid, pnl, result, total_cost, up_cost, down_cost,
                                       up_shares if up_filled else 0,
                                       down_shares if down_filled else 0,
                                       winner, mkt["question"], both_sides, book_history)
                    return

            await asyncio.sleep(0.5)

        # Extended wait
        for _ in range(120):
            up_book = await get_book(up_token)
            if up_book:
                if up_book["bid"] >= 0.95:
                    up_pnl = (up_shares * 1.00 - up_cost) if up_filled else 0
                    down_pnl = -down_cost if down_filled else 0
                    pnl = round(up_pnl + down_pnl, 4)
                    self._record_trade(tid, pnl, "WIN-BOTH" if both_sides else "WIN-LATE", total_cost,
                                       up_cost, down_cost, up_shares if up_filled else 0,
                                       down_shares if down_filled else 0,
                                       "UP", mkt["question"], both_sides, book_history)
                    return
                if up_book["bid"] <= 0.05:
                    down_pnl = (down_shares * 1.00 - down_cost) if down_filled else 0
                    up_pnl = -up_cost if up_filled else 0
                    pnl = round(up_pnl + down_pnl, 4)
                    self._record_trade(tid, pnl, "WIN-BOTH" if both_sides else "WIN-LATE", total_cost,
                                       up_cost, down_cost, up_shares if up_filled else 0,
                                       down_shares if down_filled else 0,
                                       "DOWN", mkt["question"], both_sides, book_history)
                    return
            await asyncio.sleep(1)

        # Timeout
        log_msg(f"[TIMEOUT] #{tid} Ambiguous — recording as LOSS")
        pnl = round(-total_cost, 4)
        self._record_trade(tid, pnl, "LOSS-TIMEOUT", total_cost, up_cost, down_cost,
                           up_shares if up_filled else 0,
                           down_shares if down_filled else 0,
                           "?", mkt["question"], both_sides, book_history)

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

        min_up = min((s["up_ask"] for s in book_history if s["up_ask"] > 0), default=0)
        min_down = min((s["down_ask"] for s in book_history if s["down_ask"] > 0), default=0)

        log_msg(f"[{result}] #{tid} P&L ${pnl:+.2f} ({ret_pct:+.1f}%) | "
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
        both_tag = " ✅BOTH" if both_sides else " ⚠️ONE" if (up_shares > 0 or down_shares > 0) else ""
        asyncio.create_task(send_telegram(
            f"{icon}{both_tag} <b>{BOT_NAME} #{tid}</b>\n"
            f"{result} | P&L ${pnl:+.2f} ({ret_pct:+.0f}%)\n"
            f"UP: {up_shares}sh @ ${up_cost/up_shares:.2f} (${up_cost:.2f}) | DOWN: {down_shares}sh @ ${down_cost/down_shares:.2f} (${down_cost:.2f})\n"
            f"Winner: {winner} | SL: ${SL_PRICE}\n"
            f"Bank: ${self.bankroll:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%)"))

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        summary = {
            "bot": BOT_NAME, "mode": "LIVE" if not PAPER_MODE else "PAPER",
            "elapsed_minutes": round(elapsed, 1),
            "trades": total, "wins": self.wins, "losses": self.losses,
            "win_rate": round(wr, 1),
            "bankroll": round(self.bankroll, 2),
            "starting_bankroll": self.starting_bankroll,
            "pnl_total": round(self.bankroll - self.starting_bankroll, 2),
            "max_dd": round(self.max_dd, 1),
            "both_filled": self.both_filled,
            "one_filled": self.one_filled,
            "no_fills": self.no_fills,
            "bid_price": BID_HIGH,
            "cancel_threshold": BID_HIGH,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(SUMMARY_FILE, summary)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        pnl = self.bankroll - self.starting_bankroll

        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {BOT_NAME} | {elapsed:.0f}min | {"LIVE" if not PAPER_MODE else "PAPER"}")
        print(f"  Strategy: GTC ${BID_HIGH} on BOTH sides + WS monitor")
        print(f"  Cancel unfilled when other side ask > ${BID_HIGH}")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f}")
        print(f"  Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}%")
        print(f"  Both: {self.both_filled} | One: {self.one_filled} | No fill: {self.no_fills}")
        print()


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        bot.print_status()


async def main():
    print("=" * 65)
    print(f"  {BOT_NAME} — GTC ${BID_HIGH} Both Sides + WS Monitor")
    print("=" * 65)
    print(f"  Place GTC bids at ${BID_HIGH} on BOTH Up and Down at window open")
    print(f"  Monitor via WebSocket for fills")
    print(f"  Cancel unfilled when other side ask > ${BID_HIGH}")
    print(f"  ${SHARES_PER_SIDE}/side | Cancel at T-{CANCEL_BEFORE_END}")
    print(f"  Paper mode")
    print()

    bot = BTCBothSidesBot()
    bot.init_clob()
    bot._write_summary()
    mode = "LIVE" if not PAPER_MODE else "PAPER"
    log_msg(f"[INIT] {mode} — Bank: ${bot.bankroll:.2f}")

    asyncio.create_task(send_telegram(
        f"🎯 <b>{BOT_NAME}</b> [PAPER]\n"
        f"GTC ${BID_HIGH} both sides + WS monitor\n"
        f"Cancel when other ask > ${BID_HIGH}\n"
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
