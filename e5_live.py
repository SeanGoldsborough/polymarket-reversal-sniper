#!/usr/bin/env python3
"""
PAPER F2 + E5 — Directional Gap Signal Strategies
====================================================
F2: Enter at T-45, any price, SL at entry-$0.10, hold to $1.00
E5: Enter at T-30, hold to $1.00, SL at $0.10 below entry

Both use gap signal: buy the side where bid > 0.50 (indicates directional bias)
Entry via GTC limit bids (MAKER, 0% fee)
SL via FAK sell (taker, guaranteed execution)
TP: hold to resolution ($1.00)

Uses IRONCLAD execution module patterns.
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
STARTING_BANKROLL = 100.0
BET_PCT = 0.10
MAX_BET = 200.0
FAK_FLOOR = 0.05
DRAWDOWN_PAUSE = 0.30
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET", FUNDER_ADDRESS)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PAPER_MODE = os.getenv("E5_LIVE", "0") != "1"


# Time filter: E5 only trades 9am-4pm ET (13:00-20:00 UTC)
ACTIVE_HOURS_UTC_START = 13
ACTIVE_HOURS_UTC_END = 20

def is_active_hours():
    from datetime import datetime, timezone
    hr = datetime.now(timezone.utc).hour
    return ACTIVE_HOURS_UTC_START <= hr < ACTIVE_HOURS_UTC_END

os.makedirs("logs", exist_ok=True)
LOG_FILE = "logs/e5_live_trades.jsonl"
SUMMARY_FILE = "logs/e5_live_summary.json"

# Variant configs
VARIANTS = {
    "F2": {
        "name": "F2: T-45 / any price / SL entry-$0.10",
        "entry_offset": 45,   # seconds before window end
        "sl_offset": 0.10,    # SL = entry - this
        "min_entry": 0.01,
        "max_entry": 0.99,
    },
    "E5": {
        "name": "E5: T-30 / hold to $1.00 / SL $0.10",
        "entry_offset": 30,
        "sl_offset": 0.10,
        "min_entry": 0.01,
        "max_entry": 0.99,
    },
}


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


def get_clob_balance(client):
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams
        params = BalanceAllowanceParams(asset_type="COLLATERAL", token_id="", signature_type=2)
        result = client.get_balance_allowance(params)
        return int(result.get("balance", "0")) / 1_000_000
    except Exception as e:
        print(f"  [WALLET] CLOB balance err: {e}", flush=True)
        return None


# ── Market Discovery ───────────────────────────────────
class MarketFinder:
    def __init__(self):
        self.active_market = None

    async def refresh(self, offset=0):
        try:
            now = int(time.time())
            window_start = (now // 300) * 300 + offset
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
                            self.active_market = {
                                "up": up, "down": down,
                                "question": m.get("question", ""),
                                "window_start": window_start,
                            }
                            return True
        except Exception as e:
            log_msg(f"[MKT] {e}")
        return False


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
    """Bulletproof execution: maker bids for entry, FAK for SL."""

    def __init__(self, client):
        self.client = client
        self._lock = asyncio.Lock()

    async def buy_maker(self, token_id, price, size):
        """GTC limit bid (maker, 0% fee)."""
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
                    log_msg(f"[BID] GTC {size}sh @ ${bid_price} (maker)")
                    return {"order_id": oid, "price": bid_price, "size": size, "token_id": token_id}
            except Exception as e:
                log_msg(f"[BID] err: {e}")
            return None

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
                        log_msg(f"[{reason}] FAK SELL {sell_size}sh @ ${floor} — OK")
                        return True
                except Exception as e:
                    log_msg(f"[{reason}] attempt {attempt+1}: {e}")
                    if "balance" in str(e).lower():
                        try:
                            self.client.update_balance_allowance(int(sell_size * 0.9 * 1_000_000))
                        except Exception:
                            pass
                    await asyncio.sleep(0.3)
            return False

    def check_order_status(self, order_id):
        """Check if order filled via CLOB API. Returns (is_filled, size_matched)."""
        try:
            order = self.client.get_order(order_id)
            if order:
                status = order.get("status", "")
                size_matched = float(order.get("size_matched", "0") or "0")
                original_size = float(order.get("original_size", "0") or order.get("size", "0") or "0")
                log_msg(f"[ORDER] {order_id[:8]}... status={status} filled={size_matched}/{original_size}")
                if status == "MATCHED" or size_matched >= original_size * 0.95:
                    return True, size_matched
                if size_matched > 0:
                    return True, size_matched  # partial fill counts
            return False, 0
        except Exception as e:
            log_msg(f"[ORDER] check err: {e}")
            return False, 0

    async def wait_for_fill(self, order_id, max_wait=25):
        """Poll order status until filled or timeout."""
        for i in range(max_wait):
            filled, size = self.check_order_status(order_id)
            if filled:
                return True, size
            await asyncio.sleep(1)
        return False, 0

    async def cancel_order(self, order_id):
        async with self._lock:
            if not self.client:
                return
            try:
                self.client.cancel(order_id)
                log_msg(f"[CANCEL] order={order_id[:8]}...")
            except Exception:
                pass


# ── Variant Tracker ────────────────────────────────────
class VariantState:
    def __init__(self, key, config):
        self.key = key
        self.config = config
        self.bankroll = STARTING_BANKROLL
        self.peak = STARTING_BANKROLL
        self.max_dd = 0
        self.wins = 0
        self.losses = 0
        self.trades = 0
        self.signals = 0
        self.unfilled = 0

    def to_dict(self):
        total = self.wins + self.losses
        wr = self.wins / total * 100 if total else 0
        return {
            "name": self.config["name"],
            "trades": total,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(wr, 1),
            "bankroll": round(self.bankroll, 2),
            "peak": round(self.peak, 2),
            "pnl_total": round(self.bankroll - STARTING_BANKROLL, 2),
            "roi_pct": round((self.bankroll - STARTING_BANKROLL) / STARTING_BANKROLL * 100, 2),
            "max_dd": round(self.max_dd, 1),
            "signals": self.signals,
            "unfilled": self.unfilled,
        }


# ── Main Bot ───────────────────────────────────────────
class PaperF2E5:
    def __init__(self):
        self.mf = MarketFinder()
        self.client = None
        self.engine = None
        self.start_time = time.time()
        self.log_file = open(LOG_FILE, "a")
        self.variants = {k: VariantState(k, v) for k, v in VARIANTS.items()}

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
            # Sync wallet balance
            bal = get_clob_balance(self.client)
            if bal and bal > 0:
                for v in self.variants.values():
                    v.bankroll = bal
                    v.peak = bal
                log_msg(f"[WALLET] Balance: ${bal:.2f}")
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

                # Time filter: only trade 9am-4pm ET
                if not is_active_hours():
                    continue

                log_msg("[SCAN] Refreshing market...")
                await self.mf.refresh(offset=0)
                if not self.mf.active_market:
                    log_msg("[SCAN] No market found")
                    continue

                up_tok = self.mf.active_market["up"]
                down_tok = self.mf.active_market["down"]
                question = self.mf.active_market["question"]
                window_start = self.mf.active_market["window_start"]
                window_end = window_start + 300

                log_msg(f"[SCAN] Market found: {question[:50]}")
                await self._run_variant("E5", up_tok, down_tok, question, window_end)

            except Exception as e:
                log_msg(f"[MAIN] {e}")
                await asyncio.sleep(5)

    async def _run_variant(self, key, up_tok, down_tok, question, window_end):
        v = self.variants[key]
        cfg = v.config
        entry_time = window_end - cfg["entry_offset"]

        # Wait for entry time
        now = time.time()
        if now < entry_time:
            await asyncio.sleep(entry_time - now)

        v.signals += 1
        log_msg(f"[{key}] Signal #{v.signals} -- reading books...")

        # Read books to determine direction
        up_book, down_book = await asyncio.gather(get_book(up_tok), get_book(down_tok))
        if not up_book or not down_book:
            log_msg(f"[{key}] Books failed: up={up_book is not None} down={down_book is not None}")
            v.unfilled += 1
            return

        # Gap signal: buy the side with higher bid (indicates market direction)
        up_bid = up_book["bid"]
        down_bid = down_book["bid"]

        if up_bid > down_bid and up_bid > 0.50:
            side = "UP"
            token_id = up_tok
            book = up_book
        elif down_bid > up_bid and down_bid > 0.50:
            side = "DOWN"
            token_id = down_tok
            book = down_book
        else:
            log_msg(f"[{key}] No gap signal: UP bid=${up_bid:.2f} DOWN bid=${down_bid:.2f}")
            v.unfilled += 1
            return

        ask = book["ask"]
        bid = book["bid"]
        if ask <= 0 or ask < cfg["min_entry"] or ask > cfg["max_entry"]:
            log_msg(f"[{key}] Ask invalid: ${ask:.2f}")
            v.unfilled += 1
            return

        # Maker bid at midpoint (0% fee)
        mid = round((bid + ask) / 2, 2)
        entry_price = snap_price(mid)
        sl_price = snap_price(entry_price - cfg["sl_offset"])

        bet = min(v.bankroll * BET_PCT, MAX_BET)
        shares = round(bet / entry_price, 2)
        shares = max(round(shares, 2), 5.0)  # Polymarket minimum
        if shares * entry_price > v.bankroll * 0.5:  # safety: never bet more than 50% of wallet
            log_msg(f"[{key}] Shares too small: {shares} (bet=${bet:.2f} entry=${entry_price:.2f})")
            v.unfilled += 1
            return

        v.trades += 1
        tid = v.trades

        log_msg(f"[{key}] #{tid} {side} @ ${entry_price:.2f} ({shares}sh) SL ${sl_price:.2f} | {question}")

        # Execute buy
        if self.engine:
            order = await self.engine.buy_maker(token_id, entry_price, shares)
            if not order:
                log_msg(f"[{key}] #{tid} Bid placement failed")
                v.unfilled += 1
                v.trades -= 1  # undo trade count increment
                return
            # Fill verification: check actual order status via CLOB API
            log_msg(f"[{key}] #{tid} Waiting for fill (order {order['order_id'][:8]}...)...")
            filled, filled_size = await self.engine.wait_for_fill(order["order_id"], max_wait=25)
            if not filled:
                await self.engine.cancel_order(order["order_id"])
                # Double check it didn't fill during cancel
                filled2, filled_size2 = self.engine.check_order_status(order["order_id"])
                if filled2:
                    log_msg(f"[{key}] #{tid} Filled during cancel! size={filled_size2}")
                    shares = filled_size2
                else:
                    log_msg(f"[{key}] #{tid} Not filled -- cancelled")
                    v.unfilled += 1
                    v.trades -= 1
                    return
            else:
                shares = filled_size if filled_size > 0 else shares
            log_msg(f"[{key}] #{tid} CONFIRMED FILL {shares}sh @ ${entry_price:.2f}")
        else:
            log_msg(f"[{key}] #{tid} Paper fill @ ${entry_price:.2f}")

        # Monitor for SL or resolution
        monitor_end = window_end + 60  # wait up to 60s past window for resolution
        while time.time() < monitor_end:
            book = await get_book(token_id)
            if not book:
                await asyncio.sleep(1)
                continue

            current_bid = book["bid"]

            # WIN: resolved to $1.00
            if current_bid >= 0.95:
                pnl = round(shares * (1.00 - entry_price), 4)
                v.wins += 1
                v.bankroll = round(v.bankroll + pnl, 4)
                if not PAPER_MODE and self.client:
                    bal = get_clob_balance(self.client)
                    if bal and bal > 0:
                        v.bankroll = bal
                log_msg(f"[{key}] #{tid} WIN {side} @ $1.00 | +${pnl:.2f} | bank ${v.bankroll:.2f}")
                self._log_trade(key, tid, side, entry_price, 1.00, shares, pnl, "WIN", question)
                self._update_peak_dd(v)
                self._write_summary()
                return

            # SL: bid dropped below stop loss
            if current_bid <= sl_price and time.time() < window_end:
                sl_fee = round(shares * sl_price * 0.018, 4)
                pnl = round(shares * (sl_price - entry_price) - sl_fee, 4)
                if self.engine:
                    await self.engine.emergency_sell(token_id, shares, f"SL-{key}")
                v.losses += 1
                v.bankroll = round(v.bankroll + pnl, 4)
                if not PAPER_MODE and self.client:
                    await asyncio.sleep(2)
                    bal = get_clob_balance(self.client)
                    if bal and bal > 0:
                        v.bankroll = bal
                log_msg(f"[{key}] #{tid} SL {side} @ ${sl_price:.2f} | ${pnl:.2f} | bank ${v.bankroll:.2f}")
                self._log_trade(key, tid, side, entry_price, sl_price, shares, pnl, "SL", question)
                self._update_peak_dd(v)
                self._write_summary()
                return

            # LOSS: resolved to $0 (bid near 0 after window)
            if current_bid <= 0.05 and time.time() > window_end:
                pnl = round(-shares * entry_price, 4)
                if self.engine:
                    await self.engine.emergency_sell(token_id, shares, f"LOSS-{key}")
                v.losses += 1
                v.bankroll = round(v.bankroll + pnl, 4)
                if not PAPER_MODE and self.client:
                    await asyncio.sleep(2)
                    bal = get_clob_balance(self.client)
                    if bal and bal > 0:
                        v.bankroll = bal
                log_msg(f"[{key}] #{tid} LOSS {side} resolved $0 | ${pnl:.2f} | bank ${v.bankroll:.2f}")
                self._log_trade(key, tid, side, entry_price, 0, shares, pnl, "LOSS", question)
                self._update_peak_dd(v)
                self._write_summary()
                return

            await asyncio.sleep(1)

        # Timeout — use final bid to determine
        book = await get_book(token_id)
        final_bid = book["bid"] if book else 0
        if final_bid > 0.5:
            pnl = round(shares * (1.00 - entry_price), 4)
            v.wins += 1
            result = "WIN-LATE"
        else:
            pnl = round(-shares * entry_price, 4)
            if self.engine:
                await self.engine.emergency_sell(token_id, shares, f"EXP-{key}")
            v.losses += 1
            result = "LOSS"
        v.bankroll = round(v.bankroll + pnl, 4)
        # Live mode: sync from wallet
        if not PAPER_MODE and self.client:
            bal = get_clob_balance(self.client)
            if bal and bal > 0:
                v.bankroll = bal
        log_msg(f"[{key}] #{tid} {result} {side} | ${pnl:.2f} | bank ${v.bankroll:.2f}")
        self._log_trade(key, tid, side, entry_price, final_bid, shares, pnl, result, question)
        self._update_peak_dd(v)
        self._write_summary()

    def _update_peak_dd(self, v):
        if v.bankroll > v.peak:
            v.peak = v.bankroll
        dd = (v.peak - v.bankroll) / v.peak * 100 if v.peak > 0 else 0
        if dd > v.max_dd:
            v.max_dd = dd

    def _log_trade(self, key, tid, side, entry, exit_price, shares, pnl, result, question):
        try:
            self.log_file.write(json.dumps({
                "variant": key, "id": tid, "side": side,
                "entry": entry, "exit": exit_price, "shares": shares,
                "pnl": pnl, "result": result,
                "bankroll": self.variants[key].bankroll,
                "question": question,
                "time": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            self.log_file.flush()
        except Exception:
            pass

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        summary = {
            "elapsed_minutes": round(elapsed, 1),
            "variants": {k: v.to_dict() for k, v in self.variants.items()},
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(SUMMARY_FILE, summary)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        print(f"\n  [{ts()}] {'='*60}")
        print(f"  PAPER F2 + E5 | {elapsed:.0f}min | {'LIVE' if self.engine else 'PAPER'}")
        for k, v in self.variants.items():
            total = v.wins + v.losses
            wr = v.wins / total * 100 if total else 0
            pnl = v.bankroll - STARTING_BANKROLL
            print(f"  {v.config['name']}")
            print(f"    Bank: ${v.bankroll:.2f} (${pnl:+.2f}) | {total}t ({v.wins}W/{v.losses}L) {wr:.0f}% | DD: {v.max_dd:.0f}%")
        print()


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        bot.print_status()


async def main():
    print("=" * 65)
    print("  PAPER F2 + E5 — Directional Gap Signal Strategies")
    print("=" * 65)
    for k, v in VARIANTS.items():
        print(f"  {v['name']}")
        print(f"    Entry: MAKER bid (0% fee) at T-{v['entry_offset']}s | SL: FAK at entry-${v['sl_offset']}")
    print(f"  TP: hold to resolution ($1.00)")
    print(f"  Bankroll: ${STARTING_BANKROLL} each | Bet: {int(BET_PCT*100)}%")
    print()

    bot = PaperF2E5()
    bot.init_clob()
    bot._write_summary()

    log_msg(f"[INIT] {'LIVE' if bot.engine else 'PAPER'} mode")

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
