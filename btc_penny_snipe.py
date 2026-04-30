#!/usr/bin/env python3
"""
BTC-PENNY-SNIPE — Buy deep OTM on both sides at $0.06, TP $0.09, SL $0.02
==========================================================================
Strategy:
  - Place GTC limit buy 50sh UP @ $0.06 and 50sh DOWN @ $0.06
  - When a side fills, place GTC limit sell @ $0.09 (TP) and monitor SL @ $0.02
  - Cancel unfilled orders at T-10
  - P&L tracked from actual fills and actual sell/redeem prices
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
BUY_PRICE = 0.09
SELL_PRICE = 0.15       # TP: +67% ($0.06/share profit)
SL_PRICE = 0.04         # SL: -56% ($0.05/share loss)
SHARES_PER_SIDE = 50    # 50 shares × $0.09 = $4.50 per side
CANCEL_BEFORE_END = 30  # Cancel unfilled at T-30 (skip last 30s)

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PAPER_MODE = os.getenv("BTC_PENNY_LIVE", "0") != "1"
POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.makedirs("logs", exist_ok=True)
BOT_NAME = "BTC-PENNY" if not PAPER_MODE else "BTC-PENNY-PAPER"
LOG_FILE = "logs/btc_penny_trades.jsonl"
SUMMARY_FILE = "logs/btc_penny_summary.json"


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

    async def refresh_next(self):
        """Get the NEXT window's market."""
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
                            log_msg(f"[MKT] Next: {m.get('question', '')[:50]}")
        except Exception as e:
            log_msg(f"[MKT] {e}")

    async def refresh_current(self):
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
            log_msg(f"[MKT] {e}")


class BTCPennyBot:
    def __init__(self):
        self.mf = MarketFinder()
        self.client = None
        self.bankroll = 100.0
        self.starting_bankroll = 100.0
        self.peak = 100.0
        self.max_dd = 0
        self.wins = 0
        self.losses = 0
        self.fills = 0
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
            try:
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
                now = time.time()
                nxt = (int(now) // 300 + 1) * 300
                wait = nxt - now
                if wait > 0:
                    await asyncio.sleep(wait)
                await asyncio.sleep(3)

                log_msg(f"[LOOP] Window start | Bank: ${self.bankroll:.2f}")

                if self.bankroll < 3:
                    log_msg("[RISK] Bankroll too low")
                    continue

                await self.mf.refresh_next()
                if not self.mf.market:
                    await self.mf.refresh_current()
                if not self.mf.market:
                    log_msg("[LOOP] No market found")
                    continue

                market_snapshot = dict(self.mf.market)
                asyncio.create_task(self._trade_window_safe(market_snapshot))

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(5)

    async def _trade_window_safe(self, market_snapshot):
        try:
            await self._trade_window(market_snapshot)
        except Exception as e:
            log_msg(f"[TRADE-ERR] {e}")
            import traceback
            traceback.print_exc()

    async def _trade_window(self, mkt):
        if not mkt:
            return

        self.trade_count += 1
        tid = self.trade_count
        target_window_start = mkt.get("window_start", (int(time.time()) // 300) * 300)
        window_end = target_window_start + 300
        cancel_time = window_end - CANCEL_BEFORE_END

        up_token = mkt["up"]
        down_token = mkt["down"]

        log_msg(f"[PLACE] #{tid} BUY 50sh UP + 50sh DOWN @ ${BUY_PRICE} | TP ${SELL_PRICE} | SL ${SL_PRICE} | {mkt['question'][:40]}")

        # ── PLACE GTC BIDS ON BOTH SIDES ──
        up_order_id = None
        down_order_id = None

        if self.client and not PAPER_MODE:
            try:
                args = OrderArgs(price=BUY_PRICE, size=SHARES_PER_SIDE, side=BUY, token_id=up_token)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.GTC)
                up_order_id = resp.get("orderID", "")
                log_msg(f"[BID-UP] GTC {SHARES_PER_SIDE}sh @ ${BUY_PRICE} order={up_order_id[:10]}...")
            except Exception as e:
                log_msg(f"[BID-UP] FAILED: {str(e)[:80]}")

            try:
                args = OrderArgs(price=BUY_PRICE, size=SHARES_PER_SIDE, side=BUY, token_id=down_token)
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.GTC)
                down_order_id = resp.get("orderID", "")
                log_msg(f"[BID-DN] GTC {SHARES_PER_SIDE}sh @ ${BUY_PRICE} order={down_order_id[:10]}...")
            except Exception as e:
                log_msg(f"[BID-DN] FAILED: {str(e)[:80]}")

        # ── STATE TRACKING ──
        up_filled = False
        down_filled = False
        up_fill_price = 0.0
        down_fill_price = 0.0
        up_shares = 0
        down_shares = 0

        up_tp_order_id = None   # GTC sell order at TP
        down_tp_order_id = None

        up_exited = False       # True when sold (TP, SL, or resolution)
        down_exited = False
        up_exit_price = 0.0     # Actual price we exited at
        down_exit_price = 0.0
        up_exit_reason = ""
        down_exit_reason = ""

        # ── MONITOR VIA WEBSOCKET ──
        try:
            async with websockets.connect(POLY_WS_URL, ping_interval=20, ping_timeout=10,
                                           close_timeout=5) as ws:
                sub_msg = json.dumps({"assets_ids": [up_token, down_token], "type": "market"})
                await ws.send(sub_msg)
                log_msg(f"[WS] #{tid} Connected — monitoring")

                last_log_time = 0
                up_bid = 0.0
                down_bid = 0.0

                async for raw in ws:
                    now = time.time()
                    if now >= cancel_time:
                        break

                    try:
                        msg = json.loads(raw)
                    except:
                        continue

                    items = []
                    if isinstance(msg, list):
                        items = [m for m in msg if isinstance(m, dict)]
                    elif isinstance(msg, dict) and ("asks" in msg or "bids" in msg):
                        items = [msg]

                    elapsed = now - target_window_start

                    # ── FILL DETECTION (live) ──
                    if not up_filled and self.client and not PAPER_MODE and up_order_id:
                        try:
                            order = self.client.get_order(up_order_id)
                            if order:
                                matched = float(order.get("size_matched", 0))
                                if matched > 0:
                                    up_filled = True
                                    up_fill_price = float(order.get("price", BUY_PRICE))
                                    up_shares = int(matched)
                                    log_msg(f"[FILL-UP] #{tid} {up_shares}sh @ ${up_fill_price:.2f} | T+{elapsed:.0f}s")
                                    # Pre-approve for selling
                                    try:
                                        params = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=up_token, signature_type=SIGNATURE_TYPE)
                                        self.client.update_balance_allowance(params)
                                    except:
                                        pass
                                    # Place GTC TP sell order
                                    try:
                                        args = OrderArgs(price=SELL_PRICE, size=up_shares, side=SELL, token_id=up_token)
                                        signed = self.client.create_order(args)
                                        resp = self.client.post_order(signed, OrderType.GTC)
                                        up_tp_order_id = resp.get("orderID", "")
                                        log_msg(f"[TP-UP] GTC sell {up_shares}sh @ ${SELL_PRICE} order={up_tp_order_id[:10]}...")
                                    except Exception as e:
                                        log_msg(f"[TP-UP] FAILED: {str(e)[:80]}")
                        except:
                            pass

                    if not down_filled and self.client and not PAPER_MODE and down_order_id:
                        try:
                            order = self.client.get_order(down_order_id)
                            if order:
                                matched = float(order.get("size_matched", 0))
                                if matched > 0:
                                    down_filled = True
                                    down_fill_price = float(order.get("price", BUY_PRICE))
                                    down_shares = int(matched)
                                    log_msg(f"[FILL-DN] #{tid} {down_shares}sh @ ${down_fill_price:.2f} | T+{elapsed:.0f}s")
                                    # Pre-approve for selling
                                    try:
                                        params = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=down_token, signature_type=SIGNATURE_TYPE)
                                        self.client.update_balance_allowance(params)
                                    except:
                                        pass
                                    # Place GTC TP sell order
                                    try:
                                        args = OrderArgs(price=SELL_PRICE, size=down_shares, side=SELL, token_id=down_token)
                                        signed = self.client.create_order(args)
                                        resp = self.client.post_order(signed, OrderType.GTC)
                                        down_tp_order_id = resp.get("orderID", "")
                                        log_msg(f"[TP-DN] GTC sell {down_shares}sh @ ${SELL_PRICE} order={down_tp_order_id[:10]}...")
                                    except Exception as e:
                                        log_msg(f"[TP-DN] FAILED: {str(e)[:80]}")
                        except:
                            pass

                    # ── CHECK TP FILL (GTC sell order matched?) ──
                    if up_filled and not up_exited and up_tp_order_id and self.client:
                        try:
                            order = self.client.get_order(up_tp_order_id)
                            if order:
                                matched = float(order.get("size_matched", 0))
                                if matched >= up_shares:
                                    up_exited = True
                                    up_exit_price = float(order.get("price", SELL_PRICE))
                                    up_exit_reason = "TP"
                                    log_msg(f"[TP-SOLD-UP] #{tid} {up_shares}sh @ ${up_exit_price:.2f}")
                        except:
                            pass

                    if down_filled and not down_exited and down_tp_order_id and self.client:
                        try:
                            order = self.client.get_order(down_tp_order_id)
                            if order:
                                matched = float(order.get("size_matched", 0))
                                if matched >= down_shares:
                                    down_exited = True
                                    down_exit_price = float(order.get("price", SELL_PRICE))
                                    down_exit_reason = "TP"
                                    log_msg(f"[TP-SOLD-DN] #{tid} {down_shares}sh @ ${down_exit_price:.2f}")
                        except:
                            pass

                    # ── SL CHECK via WS bids ──
                    for item in items:
                        aid = item.get("asset_id", "")
                        bids_data = item.get("bids", [])
                        if not bids_data:
                            continue
                        try:
                            best_bid = max(float(b["price"]) for b in bids_data if isinstance(b, dict) and "price" in b)
                        except (ValueError, KeyError):
                            continue

                        if aid == up_token:
                            up_bid = best_bid
                        elif aid == down_token:
                            down_bid = best_bid

                        # SL on UP
                        if aid == up_token and up_filled and not up_exited and best_bid <= SL_PRICE and best_bid > 0.005:
                            up_exited = True
                            up_exit_reason = "SL"
                            # Cancel TP order first
                            if up_tp_order_id and self.client:
                                try:
                                    self.client.cancel(up_tp_order_id)
                                except:
                                    pass
                            # FAK sell at market
                            if self.client and not PAPER_MODE:
                                for attempt in range(3):
                                    try:
                                        sell_p = snap_price(best_bid - 0.01)
                                        args = OrderArgs(price=sell_p, size=up_shares, side=SELL, token_id=up_token)
                                        signed = self.client.create_order(args)
                                        self.client.post_order(signed, OrderType.FAK)
                                        up_exit_price = sell_p
                                        log_msg(f"[SL-SOLD-UP] #{tid} FAK sell {up_shares}sh @ ${sell_p}")
                                        break
                                    except Exception as e:
                                        log_msg(f"[SL-UP] #{tid} Attempt {attempt+1}: {str(e)[:60]}")
                            else:
                                up_exit_price = best_bid
                                log_msg(f"[SL-UP] #{tid} PAPER SL @ ${best_bid:.2f}")

                        # SL on DOWN
                        if aid == down_token and down_filled and not down_exited and best_bid <= SL_PRICE and best_bid > 0.005:
                            down_exited = True
                            down_exit_reason = "SL"
                            if down_tp_order_id and self.client:
                                try:
                                    self.client.cancel(down_tp_order_id)
                                except:
                                    pass
                            if self.client and not PAPER_MODE:
                                for attempt in range(3):
                                    try:
                                        sell_p = snap_price(best_bid - 0.01)
                                        args = OrderArgs(price=sell_p, size=down_shares, side=SELL, token_id=down_token)
                                        signed = self.client.create_order(args)
                                        self.client.post_order(signed, OrderType.FAK)
                                        down_exit_price = sell_p
                                        log_msg(f"[SL-SOLD-DN] #{tid} FAK sell {down_shares}sh @ ${sell_p}")
                                        break
                                    except Exception as e:
                                        log_msg(f"[SL-DN] #{tid} Attempt {attempt+1}: {str(e)[:60]}")
                            else:
                                down_exit_price = best_bid
                                log_msg(f"[SL-DN] #{tid} PAPER SL @ ${best_bid:.2f}")

                    # ── LOG every 15s ──
                    if now - last_log_time >= 15 and elapsed > 5:
                        last_log_time = now
                        fills = []
                        if up_filled:
                            status = "EXITED" if up_exited else f"bid ${up_bid:.2f}"
                            fills.append(f"UP {up_shares}sh {status}")
                        if down_filled:
                            status = "EXITED" if down_exited else f"bid ${down_bid:.2f}"
                            fills.append(f"DN {down_shares}sh {status}")
                        fill_str = " | ".join(fills) if fills else "waiting for fills"
                        log_msg(f"[WATCH] #{tid} T+{elapsed:.0f}s | {fill_str}")

                    # Early exit if both sides exited
                    if up_exited and down_exited:
                        break
                    if (up_filled or down_filled) and up_exited and not down_filled:
                        break
                    if (up_filled or down_filled) and down_exited and not up_filled:
                        break

        except Exception as e:
            log_msg(f"[WS] #{tid} Error: {e}")

        # ── CANCEL UNFILLED ORDERS ──
        if self.client and not PAPER_MODE:
            if not up_filled and up_order_id:
                try:
                    self.client.cancel(up_order_id)
                    log_msg(f"[CANCEL-UP] #{tid} Unfilled buy cancelled")
                except:
                    pass
            if not down_filled and down_order_id:
                try:
                    self.client.cancel(down_order_id)
                    log_msg(f"[CANCEL-DN] #{tid} Unfilled buy cancelled")
                except:
                    pass
            # Cancel any remaining TP sell orders
            if up_filled and not up_exited and up_tp_order_id:
                try:
                    self.client.cancel(up_tp_order_id)
                except:
                    pass
            if down_filled and not down_exited and down_tp_order_id:
                try:
                    self.client.cancel(down_tp_order_id)
                except:
                    pass

        # ── NO FILLS ──
        if not up_filled and not down_filled:
            self.no_fills += 1
            log_msg(f"[SKIP] #{tid} No fills")
            return

        self.fills += 1

        # ── WAIT FOR RESOLUTION on any un-exited positions ──
        if (up_filled and not up_exited) or (down_filled and not down_exited):
            log_msg(f"[RESOLVE] #{tid} Waiting for resolution on open positions...")
            for _ in range(180):  # 90 seconds
                try:
                    async with aiohttp.ClientSession() as s:
                        # Check UP book
                        if up_filled and not up_exited:
                            async with s.get(f"https://clob.polymarket.com/book?token_id={up_token}",
                                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                                if r.status == 200:
                                    data = await r.json()
                                    bids = data.get("bids", [])
                                    bb = max((float(b["price"]) for b in bids), default=0)
                                    if bb >= 0.95:
                                        up_exited = True
                                        up_exit_price = 1.00  # Resolves to $1
                                        up_exit_reason = "WIN-RES"
                                        log_msg(f"[RES-UP] #{tid} UP resolved $1.00")
                                    elif bb <= 0.05:
                                        up_exited = True
                                        up_exit_price = 0.00
                                        up_exit_reason = "LOSS-RES"
                                        log_msg(f"[RES-UP] #{tid} UP resolved $0.00")

                        # Check DOWN book
                        if down_filled and not down_exited:
                            async with s.get(f"https://clob.polymarket.com/book?token_id={down_token}",
                                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                                if r.status == 200:
                                    data = await r.json()
                                    bids = data.get("bids", [])
                                    bb = max((float(b["price"]) for b in bids), default=0)
                                    if bb >= 0.95:
                                        down_exited = True
                                        down_exit_price = 1.00
                                        down_exit_reason = "WIN-RES"
                                        log_msg(f"[RES-DN] #{tid} DOWN resolved $1.00")
                                    elif bb <= 0.05:
                                        down_exited = True
                                        down_exit_price = 0.00
                                        down_exit_reason = "LOSS-RES"
                                        log_msg(f"[RES-DN] #{tid} DOWN resolved $0.00")
                except:
                    pass

                if (not up_filled or up_exited) and (not down_filled or down_exited):
                    break
                await asyncio.sleep(0.5)

        # ── CALCULATE P&L FROM ACTUAL FILLS AND EXITS ──
        up_cost = up_shares * up_fill_price if up_filled else 0
        down_cost = down_shares * down_fill_price if down_filled else 0
        total_cost = up_cost + down_cost

        up_revenue = up_shares * up_exit_price if up_filled and up_exited else 0
        down_revenue = down_shares * down_exit_price if down_filled and down_exited else 0

        # If still not exited after resolution wait, assume $0 (worst case)
        if up_filled and not up_exited:
            up_exit_reason = "TIMEOUT"
            log_msg(f"[TIMEOUT-UP] #{tid} Could not determine resolution")
        if down_filled and not down_exited:
            down_exit_reason = "TIMEOUT"
            log_msg(f"[TIMEOUT-DN] #{tid} Could not determine resolution")

        total_revenue = up_revenue + down_revenue
        pnl = round(total_revenue - total_cost, 4)

        self._record_trade(tid, pnl, total_cost, total_revenue, mkt,
                           up_filled, up_shares, up_fill_price, up_exit_price, up_exit_reason,
                           down_filled, down_shares, down_fill_price, down_exit_price, down_exit_reason)

    def _record_trade(self, tid, pnl, total_cost, total_revenue, mkt,
                       up_filled, up_shares, up_fill_price, up_exit_price, up_exit_reason,
                       down_filled, down_shares, down_fill_price, down_exit_price, down_exit_reason):
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

        # Build result string
        parts = []
        if up_filled:
            parts.append(f"UP:{up_exit_reason}")
        if down_filled:
            parts.append(f"DN:{down_exit_reason}")
        result = " ".join(parts)

        log_msg(f"[{'WIN' if pnl > 0 else 'LOSS'}] #{tid} P&L ${pnl:+.2f} ({ret_pct:+.1f}%) | {result} | "
                f"Cost ${total_cost:.2f} → Rev ${total_revenue:.2f} | "
                f"bank ${self.bankroll:.2f} | {self.wins}W/{self.losses}L ({wr:.0f}%)")

        # Detailed breakdown
        if up_filled:
            up_pnl = up_shares * up_exit_price - up_shares * up_fill_price
            log_msg(f"  UP: {up_shares}sh buy ${up_fill_price:.2f} → exit ${up_exit_price:.2f} ({up_exit_reason}) P&L ${up_pnl:+.2f}")
        if down_filled:
            dn_pnl = down_shares * down_exit_price - down_shares * down_fill_price
            log_msg(f"  DN: {down_shares}sh buy ${down_fill_price:.2f} → exit ${down_exit_price:.2f} ({down_exit_reason}) P&L ${dn_pnl:+.2f}")

        try:
            self.log_file.write(json.dumps({
                "id": tid, "pnl": pnl, "return_pct": ret_pct, "result": result,
                "total_cost": round(total_cost, 4), "total_revenue": round(total_revenue, 4),
                "up_filled": up_filled, "up_shares": up_shares,
                "up_fill_price": up_fill_price, "up_exit_price": up_exit_price, "up_exit_reason": up_exit_reason,
                "down_filled": down_filled, "down_shares": down_shares,
                "down_fill_price": down_fill_price, "down_exit_price": down_exit_price, "down_exit_reason": down_exit_reason,
                "bankroll": self.bankroll,
                "question": mkt["question"],
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except Exception:
            pass

        self._write_summary()

        icon = "\U0001F7E2" if pnl > 0 else "\U0001F534"
        asyncio.create_task(send_telegram(
            f"{icon} <b>{BOT_NAME} #{tid}</b>\n"
            f"P&L ${pnl:+.2f} ({ret_pct:+.0f}%) | {result}\n"
            f"Cost ${total_cost:.2f} -> Rev ${total_revenue:.2f}\n"
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
            "fills": self.fills, "no_fills": self.no_fills,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(SUMMARY_FILE, summary)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        pnl = self.bankroll - self.starting_bankroll
        mode = "LIVE" if not PAPER_MODE else "PAPER"

        print(f"\n  [{ts()}] {'='*60}")
        print(f"  {BOT_NAME} | {elapsed:.0f}min | {mode}")
        print(f"  Buy ${BUY_PRICE} both sides | TP ${SELL_PRICE} | SL ${SL_PRICE} | {SHARES_PER_SIDE}sh/side")
        print(f"  Bank: ${self.bankroll:.2f} (${pnl:+.2f}) | Peak: ${self.peak:.2f} | DD: {self.max_dd:.0f}%")
        print(f"  Trades: {total} ({self.wins}W/{self.losses}L) WR: {wr:.0f}%")
        print(f"  Fills: {self.fills} | No fill: {self.no_fills}")
        print()


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        bot.print_status()


async def main():
    mode = "LIVE" if not PAPER_MODE else "PAPER"
    print("=" * 65)
    print(f"  {BOT_NAME} — Buy ${BUY_PRICE} Both Sides | TP ${SELL_PRICE} | SL ${SL_PRICE}")
    print("=" * 65)
    print(f"  {SHARES_PER_SIDE}sh per side (${SHARES_PER_SIDE * BUY_PRICE:.2f}/side)")
    print(f"  TP: sell @ ${SELL_PRICE} (+{(SELL_PRICE/BUY_PRICE - 1)*100:.0f}%)")
    print(f"  SL: sell @ ${SL_PRICE} (-{(1 - SL_PRICE/BUY_PRICE)*100:.0f}%)")
    print(f"  Mode: {mode}")
    print()

    bot = BTCPennyBot()
    bot.init_clob()
    bot._write_summary()
    log_msg(f"[INIT] {mode} — Bank: ${bot.bankroll:.2f}")

    asyncio.create_task(send_telegram(
        f"\U0001F3AF <b>{BOT_NAME}</b> [{mode}]\n"
        f"Buy ${BUY_PRICE} both sides | TP ${SELL_PRICE} | SL ${SL_PRICE}\n"
        f"{SHARES_PER_SIDE}sh/side (${SHARES_PER_SIDE * BUY_PRICE:.2f}/side)\n"
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
