"""
Measure ask-drift after a CB-driven signal.

For each tick file, identify points where an S7-style signal would fire
($18+ CB move within 1.5s), then measure how the ask on the FADE side moves
over the next `latency_ms` window. The output is the empirical distribution
of (signal_ask, post_latency_ask) — exactly the slippage / evaporation that
PaperOrderEngine currently ignores.

Output:
    A. Summary: % rejected (ask moved >$0.05 above signal), % filled at
       same/better price, % filled with $0.01-$0.05 slippage.
    B. Histogram of ask drift (cents).
    C. By regime (using cb_regime_tagger).
    D. Empirical drift distribution saved to JSON for the engine to load.

Usage:
    python3 measure_ask_drift.py [--latency-ms 1500] [--threshold 18] [--max-files N]
"""
from __future__ import annotations
import argparse
import csv
import gzip
import json
import math
import sys
from collections import defaultdict, Counter
from pathlib import Path
from statistics import mean, median


def _open(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt")
    return open(path, "r")


def measure_one(path: Path, threshold: float, max_gap_ms: int, latency_ms: int):
    """For each S7-fire point in the window, return list of dicts with
    pre_ask, post_ask, post_bid, cb_delta."""
    rows = []
    with _open(path) as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "elapsed_ms": int(r["elapsed_ms"]),
                    "up_bid": float(r["up_bid"]),
                    "up_ask": float(r["up_ask"]),
                    "down_bid": float(r["down_bid"]),
                    "down_ask": float(r["down_ask"]),
                    "cb_price": float(r["cb_price"]),
                })
            except (KeyError, ValueError):
                continue
    if len(rows) < 10:
        return []
    fires = []
    prev_cb = 0.0
    prev_cb_ms = 0
    fired = False
    for i, r in enumerate(rows):
        if fired:
            continue
        if r["cb_price"] <= 0:
            continue
        if prev_cb > 0 and (r["elapsed_ms"] - prev_cb_ms) <= max_gap_ms:
            move = r["cb_price"] - prev_cb
            if abs(move) >= threshold:
                # Fade — opposite side
                if move > 0:
                    side, pre_ask = "DN", r["down_ask"]
                else:
                    side, pre_ask = "UP", r["up_ask"]
                if pre_ask <= 0 or pre_ask >= 1:
                    fired = True
                    continue
                # Look forward latency_ms
                target_ms = r["elapsed_ms"] + latency_ms
                post = None
                for r2 in rows[i:]:
                    if r2["elapsed_ms"] >= target_ms:
                        post = r2
                        break
                if post is None:
                    fired = True
                    continue
                post_ask = post["down_ask"] if side == "DN" else post["up_ask"]
                post_bid = post["down_bid"] if side == "DN" else post["up_bid"]
                fires.append({
                    "cb_delta": move,
                    "side": side,
                    "pre_ask": pre_ask,
                    "post_ask": post_ask,
                    "post_bid": post_bid,
                    "ask_drift": post_ask - pre_ask,  # signed
                })
                fired = True
        prev_cb = r["cb_price"]
        prev_cb_ms = r["elapsed_ms"]
    return fires


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--threshold", type=float, default=18.0)
    ap.add_argument("--max-gap-ms", type=int, default=1500)
    ap.add_argument("--latency-ms", type=int, default=1500)
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--out", default="/tmp/ask_drift_distribution.json")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    files = sorted(Path(args.ticks).glob("ticks_*.csv*"))
    if args.max_files:
        files = files[:args.max_files]

    print(f"=== Ask-drift measurement ===")
    print(f"Windows: {len(files)}  threshold=${args.threshold:.0f}  "
          f"latency={args.latency_ms}ms")
    print()

    all_fires = []
    for i, f in enumerate(files):
        all_fires.extend(measure_one(f, args.threshold, args.max_gap_ms, args.latency_ms))
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(files)} done", flush=True)

    print(flush=True)
    print(f"Total signals measured: {len(all_fires)}")
    print()

    # === A. Summary ===
    if not all_fires:
        print("No signals — nothing to summarize.")
        return
    drifts = [f["ask_drift"] for f in all_fires]
    cents = [int(round(d * 100)) for d in drifts]

    # FAK reject = post_ask > pre_ask + $0.05 (our markup) at S7 (cap $0.60)
    # We model this as ask drifted by >$0.05 (above our limit)
    reject_thresh = 0.05
    rejected = sum(1 for d in drifts if d > reject_thresh + 1e-9)
    filled = len(drifts) - rejected
    same_or_better = sum(1 for d in drifts if d <= 0)
    small_slip = sum(1 for d in drifts if 0 < d <= 0.02)
    med_slip = sum(1 for d in drifts if 0.02 < d <= reject_thresh)

    print(f"=== A. Outcome distribution ===")
    print(f"  Rejected (drift > +${reject_thresh:.2f}):     "
          f"{rejected:>4}/{len(drifts)}  ({100*rejected/len(drifts):.0f}%)")
    print(f"  Filled same-or-better (drift <= 0):  {same_or_better:>4}  "
          f"({100*same_or_better/len(drifts):.0f}%)")
    print(f"  Filled small slip ($0.01-$0.02):     {small_slip:>4}  "
          f"({100*small_slip/len(drifts):.0f}%)")
    print(f"  Filled medium slip ($0.03-$0.05):    {med_slip:>4}  "
          f"({100*med_slip/len(drifts):.0f}%)")
    print()
    print(f"Median drift: ${median(drifts):+.3f}  Mean: ${mean(drifts):+.3f}")
    print()

    # === B. Histogram (in cents) ===
    print(f"=== B. Drift histogram (cents) ===")
    counter = Counter(cents)
    keys = sorted(counter)
    for k in keys:
        n = counter[k]
        bar = "#" * min(60, max(1, int(60 * n / max(counter.values()))))
        print(f"  {k:>+4}¢  {n:>4} {bar}")
    print()

    # === C. By CB delta magnitude ===
    print(f"=== C. By CB delta size ===")
    buckets = [(18, 25, "$18-25"), (25, 50, "$25-50"), (50, 1e6, "$50+")]
    for lo, hi, label in buckets:
        b = [f for f in all_fires if lo <= abs(f["cb_delta"]) < hi]
        if not b:
            continue
        bd = [f["ask_drift"] for f in b]
        rej = sum(1 for d in bd if d > reject_thresh + 1e-9)
        print(f"  {label:<10} n={len(b):>3}  median=${median(bd):+.3f}  "
              f"reject%={100*rej/len(b):.0f}")
    print()

    # === D. Save distribution as JSON ===
    out = {
        "n_signals": len(all_fires),
        "latency_ms": args.latency_ms,
        "threshold": args.threshold,
        "reject_rate": rejected / len(drifts),
        "drift_buckets_cents": {str(k): v for k, v in counter.items()},
        "raw_drifts_cents": cents,
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"Wrote distribution → {args.out}")


if __name__ == "__main__":
    main()
