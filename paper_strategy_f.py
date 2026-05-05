#!/usr/bin/env python3
"""
PAPER STRATEGY F — $0.90 entry / $0.85 SL / hold to $1.00
============================================================
At T-30 of each window, watch both UP and DOWN tokens.
Buy whichever side's ask is in the $0.88-$0.91 zone first
(targeting $0.90 entry).

Stop loss: $0.85
Take profit: hold to $0.99/$1.00 (resolution)

Bet sizing: 10% of bankroll per trade.
"""

import asyncio
import json
import time
import os
import sys
from datetime import datetime, timezone
from typing import Optional

try:
    import websockets
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

try:
    import aiohttp
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "aiohttp", "-q"])
    import aiohttp

# ── Config ─────────────────────────────────────────────
STARTING_BANKROLL = 100.0
BET_PCT = 0.10
TP_PRICE = 0.99           # bid hits 0.99, treated as resolution to $1.00
RESOLUTION_PRICE = 1.00

# Variants — each has trigger time, entry zone, and SL config
# entry_min/max = price range to fill in
# sl_fixed = absolute SL price (use 0 to disable)
# sl_offset = entry - offset SL (use 0 to disable)
VARIANTS = {
    "F1": {
        "trigger": 30,
        "entry_min": 0.88,
        "entry_max": 0.91,
        "sl_fixed": 0.85,
        "sl_offset": 0.0,
        "label": "T-30 / $0.88-$0.91 / SL $0.85",
    },
    "F2": {
        "trigger": 45,
        "entry_min": 0.01,    # any price (winningest side)
        "entry_max": 0.99,
        "sl_fixed": 0.0,
        "sl_offset": 0.10,    # SL = entry - $0.10
        "label": "T-45 / any price / SL entry-$0.10",
    },
}

# F3 — re-entry strategy with its own logic (handled separately)
F3_CONFIG = {
    "start_after_seconds": 60,   # don't enter until 60s into window
    "entry_min": 0.69,
    "entry_max": 0.71,
    "tp_price": 0.80,
    "sl_price": 0.63,
    "label": "F3: T+60 / $0.69-$0.71 / TP $0.80 / SL $0.63 / re-entry",
}

LOG_FILE = "logs/paper_strategy_f.jsonl"
SUMMARY_FILE = "logs/paper_strategy_f_summary.json"
TRAJECTORY_FILE = "logs/paper_strategy_f_trajectories.jsonl"
os.makedirs("logs", exist_ok=True)


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log_msg(msg):
    print(f"  [{ts()}] {msg}", flush=True)


# ── Market finder ──────────────────────────────────────
class MarketFinder:
    def __init__(self):
        self.active_market = None
        self.last_refresh = 0.0

    async def refresh(self):
        if time.time() - self.last_refresh < 30:
            return
        self.last_refresh = time.time()
        try:
            now = int(time.time())
            window_start = (now // 300) * 300
            slug = f"btc-updown-5m-{window_start}"
            url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            m = data[0]
                            tokens_raw = m.get("clobTokenIds", "[]")
                            outcomes_raw = m.get("outcomes", "[]")
                            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
                            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                            up_token = tokens[0] if outcomes[0] == "Up" else tokens[1]
                            down_token = tokens[1] if outcomes[0] == "Up" else tokens[0]
                            self.active_market = {
                                "up_token": up_token,
                                "down_token": down_token,
                                "question": m.get("question", ""),
                                "window_start": window_start,
                            }
        except Exception as e:
            log_msg(f"[MARKET] error: {e}")


async def get_book(token_id):
    try:
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    best_bid = float(bids[-1]["price"]) if bids else 0.0
                    best_ask = float(asks[-1]["price"]) if asks else 0.0
                    midpoint = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0.0
                    return {"best_bid": best_bid, "best_ask": best_ask, "midpoint": midpoint}
    except Exception:
        pass
    return None


# ── Variant state ──────────────────────────────────────
from dataclasses import dataclass, field

@dataclass
class VariantState:
    name: str
    trigger_remaining: int
    entry_min: float
    entry_max: float
    sl_fixed: float
    sl_offset: float
    label: str
    bankroll: float = STARTING_BANKROLL
    peak_bankroll: float = STARTING_BANKROLL
    max_dd_pct: float = 0.0
    trade_counter: int = 0
    wins: int = 0
    losses: int = 0
    consec_losses: int = 0
    max_consec_losses: int = 0
    signals: int = 0
    unfilled: int = 0
    last_window: int = -1


# ── Bot ────────────────────────────────────────────────
class PaperBotF:
    def __init__(self):
        self.market_finder = MarketFinder()
        self.cb_price = 0.0
        self.cl_price = 0.0
        self.cb_ticks = 0
        self.cl_ticks = 0
        self.start_time = time.time()
        self.variants = {
            key: VariantState(
                name=key,
                trigger_remaining=cfg["trigger"],
                entry_min=cfg["entry_min"],
                entry_max=cfg["entry_max"],
                sl_fixed=cfg["sl_fixed"],
                sl_offset=cfg["sl_offset"],
                label=cfg["label"],
            )
            for key, cfg in VARIANTS.items()
        }
        # F3 has its own state
        self.f3 = VariantState(
            name="F3",
            trigger_remaining=240,  # not used; F3 uses start_after_seconds
            entry_min=F3_CONFIG["entry_min"],
            entry_max=F3_CONFIG["entry_max"],
            sl_fixed=F3_CONFIG["sl_price"],
            sl_offset=0.0,
            label=F3_CONFIG["label"],
        )

    def on_coinbase(self, price):
        self.cb_price = price
        self.cb_ticks += 1

    def on_chainlink(self, price):
        self.cl_price = price
        self.cl_ticks += 1

    def _update_dd(self, v: VariantState):
        if v.bankroll > v.peak_bankroll:
            v.peak_bankroll = v.bankroll
        dd_usd = v.peak_bankroll - v.bankroll
        dd_pct = (dd_usd / v.peak_bankroll * 100) if v.peak_bankroll > 0 else 0
        if dd_pct > v.max_dd_pct:
            v.max_dd_pct = dd_pct

    async def trajectory_observer(self):
        """Observe both UP and DOWN token bids for the entire window. Logs every 1s."""
        while True:
            try:
                # Wait for next window boundary
                now = time.time()
                next_window = (int(now) // 300 + 1) * 300
                wait = next_window - now
                if wait > 0:
                    await asyncio.sleep(wait)

                # Wait briefly for Polymarket to index the new market
                await asyncio.sleep(2)
                self.market_finder.last_refresh = 0
                await self.market_finder.refresh()
                if not self.market_finder.active_market:
                    continue

                up_token = self.market_finder.active_market["up_token"]
                down_token = self.market_finder.active_market["down_token"]
                window_start = self.market_finder.active_market["window_start"]
                question = self.market_finder.active_market.get("question", "")
                end_time = window_start + 295  # leave 5s buffer

                up_history = []
                down_history = []
                while time.time() < end_time:
                    t = round(time.time() - window_start, 1)
                    up_book, down_book = await asyncio.gather(
                        get_book(up_token), get_book(down_token)
                    )
                    if up_book:
                        up_history.append({
                            "t": t,
                            "bid": up_book["best_bid"],
                            "ask": up_book["best_ask"],
                        })
                    if down_book:
                        down_history.append({
                            "t": t,
                            "bid": down_book["best_bid"],
                            "ask": down_book["best_ask"],
                        })
                    await asyncio.sleep(1)

                # Determine winning side from final bids
                up_final = up_history[-1]["bid"] if up_history else 0
                down_final = down_history[-1]["bid"] if down_history else 0
                winner = "UP" if up_final > down_final else "DOWN"

                # Write trajectory record
                try:
                    with open(TRAJECTORY_FILE, "a") as f:
                        f.write(json.dumps({
                            "window_start": window_start,
                            "question": question,
                            "winner": winner,
                            "up_final_bid": up_final,
                            "down_final_bid": down_final,
                            "up_max_bid": max((p["bid"] for p in up_history), default=0),
                            "down_max_bid": max((p["bid"] for p in down_history), default=0),
                            "up_min_bid": min((p["bid"] for p in up_history), default=0),
                            "down_min_bid": min((p["bid"] for p in down_history), default=0),
                            "up_history": up_history[::2],   # every 2s to keep file size manageable
                            "down_history": down_history[::2],
                        }) + "\n")
                except Exception as e:
                    log_msg(f"[TRAJ] write error: {e}")
            except Exception as e:
                log_msg(f"[TRAJ] error: {e}")
                await asyncio.sleep(5)

    async def watch_f3(self):
        """F3: re-entry strategy. Enters at $0.70 starting 60s into window.
        Multiple trades per window if price oscillates."""
        while True:
            try:
                # Wait for next window start + 60s
                now = time.time()
                window_start = (int(now) // 300) * 300
                f3_start = window_start + F3_CONFIG["start_after_seconds"]
                if now >= f3_start:
                    next_window = window_start + 300
                    f3_start = next_window + F3_CONFIG["start_after_seconds"]
                wait = f3_start - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)

                self.market_finder.last_refresh = 0
                await self.market_finder.refresh()
                if not self.market_finder.active_market:
                    continue

                up_token = self.market_finder.active_market["up_token"]
                down_token = self.market_finder.active_market["down_token"]
                window_end = (int(time.time()) // 300) * 300 + 295
                self.f3.signals += 1
                log_msg(f"[F3:WATCH] Looking for ask in ${F3_CONFIG['entry_min']:.2f}-${F3_CONFIG['entry_max']:.2f}")

                # State machine for re-entry:
                #   "looking" -> waiting for ask in entry zone
                #   "in_trade" -> a trade is open, waiting for it to close
                #   "armed_for_reentry" -> after a winning exit, wait for ask < 0.70
                #   "looking" again after price drops below 0.70
                state = "looking"
                trades_this_window = 0

                while time.time() < window_end:
                    up_book, down_book = await asyncio.gather(
                        get_book(up_token), get_book(down_token)
                    )
                    if not up_book or not down_book:
                        await asyncio.sleep(0.5)
                        continue

                    if state == "looking":
                        # Find a side with ask in entry zone
                        side = None
                        token = None
                        book = None
                        if F3_CONFIG["entry_min"] <= up_book["best_ask"] <= F3_CONFIG["entry_max"]:
                            side, token, book = "UP", up_token, up_book
                        elif F3_CONFIG["entry_min"] <= down_book["best_ask"] <= F3_CONFIG["entry_max"]:
                            side, token, book = "DOWN", down_token, down_book

                        if side:
                            # Open trade and monitor synchronously (so we can do re-entry)
                            outcome = await self._f3_trade(side, token, book, window_end)
                            trades_this_window += 1
                            if outcome == "won":
                                state = "armed_for_reentry"
                            elif outcome == "stopped":
                                state = "done"  # don't re-enter after a loss
                                break
                            else:
                                state = "done"  # expired
                                break
                            continue

                    elif state == "armed_for_reentry":
                        # Wait for ask of either side to drop below 0.70
                        if (up_book["best_ask"] > 0 and up_book["best_ask"] < 0.70) or \
                           (down_book["best_ask"] > 0 and down_book["best_ask"] < 0.70):
                            state = "looking"
                            log_msg(f"[F3] Re-entry armed: ask dropped below $0.70, watching for $0.70 again")

                    await asyncio.sleep(0.5)

                if trades_this_window == 0:
                    self.f3.unfilled += 1
                    log_msg(f"[F3:NOFILL] No fills this window")
            except Exception as e:
                log_msg(f"[F3] error: {e}")
                await asyncio.sleep(5)

    async def _f3_trade(self, direction, token_id, book, window_end):
        """Execute one F3 trade synchronously. Returns 'won', 'stopped', or 'expired'."""
        v = self.f3
        v.trade_counter += 1
        tid = v.trade_counter
        entry_price = book["best_ask"]
        bid = book["best_bid"]
        shares = round((v.bankroll * BET_PCT) / entry_price, 2)
        cost = round(shares * entry_price, 4)
        opened_at = time.time()
        tp_price = F3_CONFIG["tp_price"]
        sl_price = F3_CONFIG["sl_price"]

        log_msg(f"[F3:OPEN] #{tid} {direction} @ ${entry_price:.2f} "
                f"({shares} sh, ${cost:.2f}) | TP ${tp_price} SL ${sl_price} | bank=${v.bankroll:.2f}")

        while time.time() < window_end:
            book2 = await get_book(token_id)
            if not book2:
                await asyncio.sleep(1)
                continue
            bid2 = book2["best_bid"]

            if bid2 >= tp_price:
                proceeds = round(shares * tp_price, 4)
                pnl = round(proceeds - cost, 4)
                self._close_f3(tid, direction, entry_price, tp_price, "won", pnl)
                return "won"

            if bid2 <= sl_price:
                proceeds = round(shares * sl_price, 4)
                pnl = round(proceeds - cost, 4)
                self._close_f3(tid, direction, entry_price, sl_price, "stopped", pnl)
                return "stopped"

            await asyncio.sleep(1)

        # Window expired before TP/SL
        pnl = round(-cost, 4)
        self._close_f3(tid, direction, entry_price, 0.0, "expired", pnl)
        return "expired"

    def _close_f3(self, tid, direction, entry_price, exit_price, status, pnl):
        v = self.f3
        v.bankroll = round(v.bankroll + pnl, 4)
        if pnl > 0:
            v.wins += 1
            v.consec_losses = 0
        else:
            v.losses += 1
            v.consec_losses += 1
            if v.consec_losses > v.max_consec_losses:
                v.max_consec_losses = v.consec_losses
        if v.bankroll > v.peak_bankroll:
            v.peak_bankroll = v.bankroll
        dd_usd = v.peak_bankroll - v.bankroll
        dd_pct = (dd_usd / v.peak_bankroll * 100) if v.peak_bankroll > 0 else 0
        if dd_pct > v.max_dd_pct:
            v.max_dd_pct = dd_pct
        log_msg(f"[F3:{status.upper()}] #{tid} sold @ ${exit_price:.2f} | P&L=${pnl:+.2f} "
                f"| bank=${v.bankroll:.2f}")
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps({
                    "variant": "F3",
                    "trade_id": tid,
                    "direction": direction,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "status": status,
                    "pnl": pnl,
                    "bankroll": v.bankroll,
                    "time": datetime.now(timezone.utc).isoformat(),
                }) + "\n")
        except Exception:
            pass
        self._write_summary()

    async def watch_windows(self, variant_key: str):
        """For one variant, watch each window and place a limit buy at its trigger time."""
        v = self.variants[variant_key]
        while True:
            try:
                now = time.time()
                window_start = (int(now) // 300) * 300
                trigger = window_start + (300 - v.trigger_remaining)

                if now >= trigger:
                    next_window = window_start + 300
                    trigger = next_window + (300 - v.trigger_remaining)

                wait = trigger - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)

                current_window = int(time.time()) // 300
                if current_window == v.last_window:
                    continue
                v.last_window = current_window

                self.market_finder.last_refresh = 0
                await self.market_finder.refresh()
                if not self.market_finder.active_market:
                    continue

                up_token = self.market_finder.active_market["up_token"]
                down_token = self.market_finder.active_market["down_token"]
                deadline = (int(time.time()) // 300) * 300 + 295

                v.signals += 1
                log_msg(f"[{variant_key}:WATCH] {v.label}")

                # F2-style: any-price means buy the leader immediately at trigger
                if v.entry_min <= 0.05 and v.entry_max >= 0.95:
                    up_book, down_book = await asyncio.gather(
                        get_book(up_token), get_book(down_token)
                    )
                    if not up_book or not down_book:
                        v.unfilled += 1
                        log_msg(f"[{variant_key}:NOFILL] book read failed")
                        continue
                    if up_book["midpoint"] > down_book["midpoint"]:
                        await self._fill_trade(variant_key, "UP", up_token, up_book)
                    elif down_book["midpoint"] > up_book["midpoint"]:
                        await self._fill_trade(variant_key, "DOWN", down_token, down_book)
                    else:
                        v.unfilled += 1
                        log_msg(f"[{variant_key}:NOFILL] no clear leader")
                    continue

                # F1-style: zone-based entry
                filled = False
                up_max, up_min = 0.0, 1.0
                down_max, down_min = 0.0, 1.0
                while time.time() < deadline and not filled:
                    up_book, down_book = await asyncio.gather(
                        get_book(up_token), get_book(down_token)
                    )
                    if up_book:
                        a = up_book["best_ask"]
                        if a > up_max: up_max = a
                        if a > 0 and a < up_min: up_min = a
                    if down_book:
                        a = down_book["best_ask"]
                        if a > down_max: down_max = a
                        if a > 0 and a < down_min: down_min = a

                    if up_book and v.entry_min <= up_book["best_ask"] <= v.entry_max:
                        await self._fill_trade(variant_key, "UP", up_token, up_book)
                        filled = True
                        break
                    if down_book and v.entry_min <= down_book["best_ask"] <= v.entry_max:
                        await self._fill_trade(variant_key, "DOWN", down_token, down_book)
                        filled = True
                        break
                    await asyncio.sleep(0.5)

                if not filled:
                    v.unfilled += 1
                    log_msg(f"[{variant_key}:NOFILL] UP ${up_min:.2f}-${up_max:.2f}, "
                            f"DOWN ${down_min:.2f}-${down_max:.2f}")
            except Exception as e:
                log_msg(f"[F:WATCH] error: {e}")
                await asyncio.sleep(5)

    async def _fill_trade(self, variant_key, direction, token_id, book):
        v = self.variants[variant_key]
        v.trade_counter += 1
        tid = v.trade_counter
        entry_price = book["best_ask"]
        bid = book["best_bid"]
        shares = round((v.bankroll * BET_PCT) / entry_price, 2)
        cost = round(shares * entry_price, 4)
        opened_at = time.time()

        log_msg(f"[{variant_key}:OPEN] #{tid} {direction} @ ${entry_price:.2f} "
                f"({shares} sh, ${cost:.2f}) | bid=${bid:.2f} | bank=${v.bankroll:.2f}")

        asyncio.create_task(self._monitor(variant_key, tid, direction, token_id,
                                          entry_price, shares, cost, opened_at))

    async def _monitor(self, variant_key, tid, direction, token_id,
                       entry_price, shares, cost, opened_at):
        v = self.variants[variant_key]
        # Compute the SL price for this trade
        if v.sl_fixed > 0:
            sl_price = v.sl_fixed
        elif v.sl_offset > 0:
            sl_price = round(entry_price - v.sl_offset, 2)
        else:
            sl_price = 0.0  # no SL
        try:
            await asyncio.sleep(1)
            while time.time() - opened_at < 295:
                book = await get_book(token_id)
                if not book:
                    await asyncio.sleep(1)
                    continue
                bid = book["best_bid"]

                if bid >= TP_PRICE:
                    proceeds = round(shares * RESOLUTION_PRICE, 4)
                    pnl = round(proceeds - cost, 4)
                    self._close(variant_key, tid, direction, entry_price,
                                RESOLUTION_PRICE, "won", pnl)
                    return

                if sl_price > 0 and bid <= sl_price:
                    proceeds = round(shares * sl_price, 4)
                    pnl = round(proceeds - cost, 4)
                    self._close(variant_key, tid, direction, entry_price,
                                sl_price, "stopped", pnl)
                    return

                await asyncio.sleep(1)

            pnl = round(-cost, 4)
            self._close(variant_key, tid, direction, entry_price, 0.0, "expired", pnl)
        except Exception as e:
            log_msg(f"[{variant_key}:MON] err #{tid}: {e}")

    def _close(self, variant_key, tid, direction, entry_price, exit_price, status, pnl):
        v = self.variants[variant_key]
        v.bankroll = round(v.bankroll + pnl, 4)
        if pnl > 0:
            v.wins += 1
            v.consec_losses = 0
        else:
            v.losses += 1
            v.consec_losses += 1
            if v.consec_losses > v.max_consec_losses:
                v.max_consec_losses = v.consec_losses
        self._update_dd(v)
        log_msg(f"[{variant_key}:{status.upper()}] #{tid} sold @ ${exit_price:.2f} | "
                f"P&L=${pnl:+.2f} | bank=${v.bankroll:.2f}")
        self._write_log(variant_key, tid, direction, entry_price, exit_price, status, pnl)
        self._write_summary()

    def _write_log(self, variant_key, tid, direction, entry_price, exit_price, status, pnl):
        try:
            v = self.variants[variant_key]
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps({
                    "variant": variant_key,
                    "trade_id": tid,
                    "direction": direction,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "status": status,
                    "pnl": pnl,
                    "bankroll": v.bankroll,
                    "time": datetime.now(timezone.utc).isoformat(),
                }) + "\n")
        except Exception:
            pass

    def _write_summary(self):
        elapsed = (time.time() - self.start_time) / 60
        summary = {
            "elapsed_minutes": round(elapsed, 1),
            "variants": {},
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        all_variants = list(self.variants.items()) + [("F3", self.f3)]
        for key, v in all_variants:
            total = v.wins + v.losses
            wr = (v.wins / total * 100) if total else 0
            summary["variants"][key] = {
                "label": v.label,
                "trades": v.trade_counter,
                "wins": v.wins,
                "losses": v.losses,
                "win_rate": round(wr, 1),
                "bankroll": round(v.bankroll, 2),
                "peak": round(v.peak_bankroll, 2),
                "pnl_total": round(v.bankroll - STARTING_BANKROLL, 2),
                "roi_pct": round((v.bankroll / STARTING_BANKROLL - 1) * 100, 2),
                "max_dd_pct": round(v.max_dd_pct, 1),
                "signals": v.signals,
                "unfilled": v.unfilled,
            }
        with open(SUMMARY_FILE, "w") as f:
            json.dump(summary, f, indent=2)

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        print()
        print(f"  PAPER F | {elapsed:.0f}min")
        print(f"  Feeds: CB={self.cb_ticks:,} CL={self.cl_ticks:,} | CB=${self.cb_price:,.2f} CL=${self.cl_price:,.2f}")
        print()
        all_variants = list(self.variants.items()) + [("F3", self.f3)]
        for key, v in all_variants:
            total = v.wins + v.losses
            wr = (v.wins / total * 100) if total else 0
            pnl_total = v.bankroll - STARTING_BANKROLL
            bet_size = v.bankroll * BET_PCT
            realistic_low = STARTING_BANKROLL + (pnl_total * 0.60)
            realistic_high = STARTING_BANKROLL + (pnl_total * 0.75)
            print(f"  {key} ({v.label}):")
            print(f"    Bankroll: ${v.bankroll:.2f} (${pnl_total:+.2f}) | Peak: ${v.peak_bankroll:.2f}")
            print(f"    Realistic Est. (60-75%): ${realistic_low:.2f}-${realistic_high:.2f}")
            print(f"    Bet size: ${bet_size:.2f} | DD: {v.max_dd_pct:.1f}%")
            print(f"    Trades: {total} ({v.wins}W/{v.losses}L) | Win%: {wr:.0f}% | Consec losses: {v.consec_losses}")
            print(f"    Signals: {v.signals} | Unfilled: {v.unfilled}")
        print()


# ── Feeds ──────────────────────────────────────────────
async def run_coinbase(bot):
    while True:
        try:
            async with websockets.connect("wss://ws-feed.exchange.coinbase.com") as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}]
                }))
                log_msg("[FEED] Coinbase BTC connected")
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("type") == "ticker":
                        bot.on_coinbase(float(data["price"]))
        except Exception as e:
            log_msg(f"[FEED] CB reconnecting: {e}")
            await asyncio.sleep(2)


async def run_chainlink(bot):
    while True:
        try:
            async with websockets.connect("wss://ws-live-data.polymarket.com",
                                          ping_interval=None) as ws:
                await ws.send(json.dumps({
                    "action": "subscribe",
                    "subscriptions": [{"topic": "crypto_prices_chainlink",
                                       "type": "*", "filters": ""}]
                }))
                log_msg("[FEED] Chainlink BTC connected")

                async def pinger():
                    try:
                        while True:
                            await asyncio.sleep(5)
                            await ws.send("PING")
                    except Exception:
                        pass
                ping_task = asyncio.create_task(pinger())
                try:
                    async for msg in ws:
                        if msg == "PONG" or not msg.startswith("{"):
                            continue
                        data = json.loads(msg)
                        if data.get("topic") == "crypto_prices_chainlink":
                            p = data.get("payload", {})
                            if "btc" in p.get("symbol", "").lower():
                                bot.on_chainlink(float(p["value"]))
                finally:
                    ping_task.cancel()
        except Exception as e:
            log_msg(f"[FEED] CL reconnecting: {e}")
            await asyncio.sleep(2)


async def run_status(bot):
    while True:
        await asyncio.sleep(60)
        bot.print_status()


async def main():
    print("=" * 65)
    print("  PAPER STRATEGY F")
    print("=" * 65)
    print(f"  Variants:")
    for key, cfg in VARIANTS.items():
        print(f"    {key}: {cfg['label']}")
    print(f"    F3: {F3_CONFIG['label']}")
    print(f"  Take profit: hold to ${RESOLUTION_PRICE} (exits when bid >= ${TP_PRICE})")
    print(f"  Bet sizing: {int(BET_PCT*100)}% of bankroll per trade")
    print(f"  Starting bankroll: ${STARTING_BANKROLL}")
    print()

    now = time.time()
    next_window = (int(now) // 300 + 1) * 300
    wait = next_window - now
    log_msg(f"[SYNC] Waiting {wait:.0f}s for window boundary...")
    await asyncio.sleep(wait)
    log_msg(f"[SYNC] Aligned. Starting bot.")

    bot = PaperBotF()
    tasks = [
        run_coinbase(bot),
        run_chainlink(bot),
        run_status(bot),
        bot.trajectory_observer(),
        bot.watch_f3(),
    ]
    for key in VARIANTS:
        tasks.append(bot.watch_windows(key))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
