"""
Strategy: Minute-mark market entry with 2:1 R:R TP/SL

Rules:
  - At each minute boundary inside a 5-min window (0s, 60s, 120s, 180s, 240s)
  - During the first 10 seconds of that minute, place ONE market BUY (FAK)
  - Size: 10 shares
  - TP: sell when bid >= entry + $0.10  (price-improvement reward)
  - SL: sell when bid <= entry - $0.05  (price-improvement risk)
  - If neither fires by window-end (300s): close at real Polymarket
    resolution ($1.00 or $0.00) if cached, else final-bid heuristic.

Latency: 1500ms on both entry and exit FAK (matches realistic live latency).

Tests both --side UP and --side DN to compare. Reports overall WR/$/day + by-minute breakdown.
"""
import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean
from collections import defaultdict

from replay_engine import ReplayEngine, BookState
from order_engine import PaperOrderEngine, Side, OrderType


import os as _os
SHARES = int(_os.getenv("SHARES", "10"))
TP_DELTA = float(_os.getenv("TP_DELTA", "0.10"))
SL_DELTA = float(_os.getenv("SL_DELTA", "-0.05"))
BE_TRIGGER = float(_os.getenv("BE_TRIGGER", "0"))  # If >0, when bid reaches entry+BE_TRIGGER, move SL up. 0 = disabled.
BE_SL_TARGET = float(_os.getenv("BE_SL_TARGET", "0"))  # Where to move SL when BE triggers. 0 = move to entry. Use 0.03 for true post-fee BE.
WINDOW_S = 300
LATENCY_MS = int(_os.getenv("LATENCY_MS", "1500"))
MINUTE_MARKS = [0, 60, 120, 180, 240]  # seconds into window
ENTRY_WINDOW_S = 10  # entry must happen within first 10s of each minute


def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def crypto_fee(price, sh):
    return 0.07 * price * (1 - price) * sh


def run_window(engine_cls, tick_file, side_label, resolutions):
    """Returns list of trade dicts for this window."""
    side_label = side_label.upper()
    engine = engine_cls(starting_usdc=10000, taker_latency_ms=LATENCY_MS,
                        min_notional_usdc=0)  # disable min-notional reject for this test
    open_positions = []  # list of {entry_ms, entry_price, shares, status, ...}
    closed_trades = []
    minute_entered = set()  # which minute boundaries we've already entered
    last_state = {"s": None}

    def cb(state: BookState):
        last_state["s"] = state
        engine.update_book(state)

        # Get current bid/ask for the chosen side
        if side_label == "UP":
            bid, ask = state.up_bid, state.up_ask
            token_id = f"{tick_file.stem}_UP"
        else:
            bid, ask = state.down_bid, state.down_ask
            token_id = f"{tick_file.stem}_DN"

        # 1) Check entries: each minute mark, if in first 10s, no entry yet
        sec = state.elapsed_ms / 1000.0
        for m in MINUTE_MARKS:
            if m in minute_entered:
                continue
            if m <= sec < m + ENTRY_WINDOW_S:
                # Place FAK BUY at the ask
                if ask <= 0 or ask >= 1.0:
                    continue
                try:
                    o = engine.place_order(
                        token_id=token_id, token_label=side_label,
                        side=Side.BUY, price=min(ask + 0.05, 0.98),
                        size=SHARES, order_type=OrderType.FAK,
                    )
                    minute_entered.add(m)
                    open_positions.append({
                        "entry_order": o,
                        "minute": m,
                        "entry_ms": state.elapsed_ms,
                        "entry_price": None,  # set when fill is confirmed
                        "tp": None,
                        "sl": None,
                        "exit_price": None,
                        "exit_ms": None,
                        "exit_reason": None,
                    })
                except Exception:
                    pass
                break  # only one entry per tick

        # 2) Refresh open positions — confirm fills, then check TP/SL
        for pos in open_positions:
            if pos["exit_ms"] is not None:
                continue  # already closed
            o = pos["entry_order"]
            # If entry FAK is filled, set TP/SL targets
            if pos["entry_price"] is None and o.filled_size > 0:
                pos["entry_price"] = o.fill_avg_price
                pos["tp"] = pos["entry_price"] + TP_DELTA
                pos["sl"] = pos["entry_price"] + SL_DELTA
                pos["be_promoted"] = False
            # Breakeven-SL promotion: if bid reaches entry + BE_TRIGGER, lock SL up to entry + BE_SL_TARGET
            if pos["entry_price"] is not None and BE_TRIGGER > 0 and not pos["be_promoted"]:
                if bid >= pos["entry_price"] + BE_TRIGGER:
                    pos["sl"] = pos["entry_price"] + BE_SL_TARGET
                    pos["be_promoted"] = True
            # Check TP/SL
            if pos["entry_price"] is not None:
                if bid >= pos["tp"]:
                    pos["exit_price"] = bid  # we'd sell at bid
                    pos["exit_ms"] = state.elapsed_ms
                    pos["exit_reason"] = "TP"
                elif bid <= pos["sl"]:
                    pos["exit_price"] = bid
                    pos["exit_ms"] = state.elapsed_ms
                    pos["exit_reason"] = "BE-SL" if pos["be_promoted"] else "SL"

    engine.replay = None  # safety
    rep = ReplayEngine()
    rep.replay(tick_file, cb)

    # Window-end resolution for any positions still open
    last = last_state["s"]
    window_start = int(tick_file.stem.split("_")[1])
    res = resolutions.get(window_start) if resolutions else None
    real_resolved = res and res.get("resolved")
    if real_resolved:
        up_won = res.get("up_winning")
        end_price = 1.00 if (
            (side_label == "UP" and up_won) or (side_label == "DN" and not up_won)
        ) else 0.00
    elif last is not None:
        final_bid = last.up_bid if side_label == "UP" else last.down_bid
        end_price = 1.00 if final_bid >= 0.95 else (0.00 if final_bid <= 0.05 else final_bid)
    else:
        end_price = 0.0

    for pos in open_positions:
        if pos["entry_price"] is None and pos["entry_order"].filled_size == 0:
            # Never filled
            pos["exit_reason"] = "NOFILL"
            continue
        if pos["entry_price"] is None and pos["entry_order"].filled_size > 0:
            # Caught in pending — set it
            pos["entry_price"] = pos["entry_order"].fill_avg_price
        if pos["exit_ms"] is None:
            pos["exit_price"] = end_price
            pos["exit_ms"] = WINDOW_S * 1000
            pos["exit_reason"] = "WINDOW-END"
        # Compute P&L
        sh = pos["entry_order"].filled_size
        entry = pos["entry_price"]
        exit_p = pos["exit_price"]
        entry_fee = crypto_fee(entry, sh)
        exit_fee = crypto_fee(exit_p, sh) if pos["exit_reason"] != "WINDOW-END" else 0
        # window-end = market resolves, no exit fee
        pnl = (exit_p - entry) * sh - entry_fee - exit_fee
        pos["pnl"] = pnl
        pos["shares"] = sh
        pos["entry_fee"] = entry_fee
        pos["exit_fee"] = exit_fee
        closed_trades.append(pos)

    return closed_trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--resolutions", default="/home/ubuntu/reports/resolutions.json")
    ap.add_argument("--side", default="BOTH", choices=["UP", "DN", "BOTH"])
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--csv", default=None, help="Dump per-trade rows to this CSV")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    csv_fh = None
    if args.csv:
        csv_fh = open(args.csv, "w")
        csv_fh.write("side,minute,entry_price,exit_reason,pnl,shares\n")

    try:
        with open(args.resolutions) as f:
            resolutions = {int(k): v for k, v in json.load(f).items()}
    except FileNotFoundError:
        resolutions = {}

    files = sorted(Path(args.ticks).glob("ticks_*.csv*"))
    if args.max_files:
        files = files[:args.max_files]
    days = len(files) * 5 / 60 / 24
    print(f"Minute-mark TP/SL strategy: {len(files)} windows ({days:.1f} days)", flush=True)
    print(f"Config: SHARES={SHARES}, TP=+${TP_DELTA}, SL=${SL_DELTA}, "
          f"latency={LATENCY_MS}ms, side={args.side}", flush=True)
    print(flush=True)

    sides = [args.side] if args.side != "BOTH" else ["UP", "DN"]
    for side in sides:
        all_trades = []
        for fi, f in enumerate(files):
            trades = run_window(PaperOrderEngine, f, side, resolutions)
            all_trades.extend(trades)
            if csv_fh:
                for t in trades:
                    if t.get("pnl") is None:
                        continue
                    csv_fh.write(f"{side},{t['minute']},{t['entry_price']:.4f},"
                                 f"{t['exit_reason']},{t['pnl']:.4f},{t.get('shares',0):.4f}\n")
                csv_fh.flush()
            if (fi + 1) % 50 == 0:
                print(f"  [{side}] {fi+1}/{len(files)} windows, "
                      f"{len(all_trades)} trades so far", flush=True)

        report(side, all_trades, days)

    if csv_fh:
        csv_fh.close()
        print(f"\nDumped trades to {args.csv}", flush=True)


def report(side, trades, days):
    print(flush=True)
    print(f"=== SIDE = {side} ===", flush=True)
    print(f"Total entries (incl. nofills): {len(trades)}", flush=True)
    filled = [t for t in trades if t.get("pnl") is not None]
    print(f"Filled trades: {len(filled)}", flush=True)
    if not filled:
        return

    # By exit reason
    by_reason = defaultdict(list)
    for t in filled:
        by_reason[t["exit_reason"]].append(t)
    print(flush=True)
    print(f"{'Exit Reason':<14} {'n':>5} {'pct':>5} {'WR':>5} {'avg PnL':>10} {'total PnL':>12}",
          flush=True)
    print("-" * 65, flush=True)
    for reason in ["TP", "SL", "BE-SL", "WINDOW-END"]:
        bucket = by_reason.get(reason, [])
        if not bucket:
            print(f"{reason:<14} {0:>5}", flush=True)
            continue
        pnls = [t["pnl"] for t in bucket]
        wins = sum(1 for p in pnls if p > 0.001)
        wr = 100 * wins / max(1, len(bucket))
        pct = 100 * len(bucket) / max(1, len(filled))
        print(f"{reason:<14} {len(bucket):>5} {pct:>4.0f}% {wr:>4.0f}% ${mean(pnls):>+8.3f} "
              f"${sum(pnls):>+10.2f}", flush=True)

    # Overall
    pnls = [t["pnl"] for t in filled]
    wins = sum(1 for p in pnls if p > 0.001)
    losses = sum(1 for p in pnls if p < -0.001)
    wlo, whi = wilson_ci(wins, wins + losses)
    total = sum(pnls)
    print("-" * 60, flush=True)
    print(f"OVERALL: n={len(filled)} W={wins} L={losses}  "
          f"WR={wins/max(1,wins+losses)*100:.0f}% "
          f"CI [{wlo*100:.0f}% — {whi*100:.0f}%]", flush=True)
    print(f"Total PnL: ${total:+.2f}  /day=${total/max(0.01,days):+.2f}",
          flush=True)

    # By minute mark
    print(flush=True)
    print(f"By-minute breakdown:", flush=True)
    print(f"{'Min':>5} {'n':>5} {'fills':>6} {'WR':>5} {'avg PnL':>10} {'total':>10}",
          flush=True)
    print("-" * 60, flush=True)
    by_min = defaultdict(list)
    for t in filled:
        by_min[t["minute"]].append(t)
    for m in sorted(by_min.keys()):
        bucket = by_min[m]
        pnls = [t["pnl"] for t in bucket]
        wins = sum(1 for p in pnls if p > 0.001)
        losses = sum(1 for p in pnls if p < -0.001)
        wr = 100 * wins / max(1, wins + losses)
        print(f"{m:>5}s {len(bucket):>5} {len(bucket):>6} "
              f"{wr:>4.0f}% ${mean(pnls):>+8.3f} ${sum(pnls):>+8.2f}", flush=True)


if __name__ == "__main__":
    main()
