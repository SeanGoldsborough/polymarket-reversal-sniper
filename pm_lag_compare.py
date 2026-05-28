"""
Compare PM-lag distribution: LIVE bot vs BACKTEST tick files.

Inputs:
    --live PATH     Path to the live bot's PM lag JSONL log (from PMLagTracker).
                    Default: /home/ubuntu/polymarket-bot/logs/btc_s7_pm_lag.jsonl
    --backtest PATH Path to backtest tick directory. We extract PM lag stats
                    from each window's tick file using the same algorithm.
                    Default: /home/ubuntu/reports/ticks
    --max-files N   Limit backtest files (default: all).

Computes per-source:
    - n events
    - lag p10/p50/p90 (ms)
    - aligned% (PM moved direction-consistent with CB)
    - by CB delta bucket

Outputs:
    A. Side-by-side distributions
    B. Per-bucket comparison
    C. Recommended PaperOrderEngine knobs to match live (latency, evap rate)

Usage:
    python3 pm_lag_compare.py [--live PATH] [--backtest DIR]
"""
from __future__ import annotations
import argparse
import csv
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Dict, List, Tuple


def _open(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt")
    return open(path, "r")


def measure_lag_from_tick(path: Path, cb_threshold: float = 1.0,
                          freshness_ms: int = 5000) -> List[dict]:
    """Measure CB→PM reactions in a single tick CSV."""
    cb_events = []
    pm_events = []
    prev_cb = 0.0
    prev_up_bid = 0.0
    prev_dn_bid = 0.0
    with _open(path) as f:
        for r in csv.DictReader(f):
            try:
                el = int(r["elapsed_ms"])
                cb = float(r["cb_price"])
                ub = float(r["up_bid"])
                db = float(r["down_bid"])
            except (KeyError, ValueError):
                continue
            if cb > 0 and prev_cb > 0 and abs(cb - prev_cb) >= cb_threshold:
                cb_events.append((el, prev_cb, cb))
            if ub > 0 and prev_up_bid > 0 and abs(ub - prev_up_bid) >= 0.005:
                pm_events.append((el, "UP", prev_up_bid, ub))
            if db > 0 and prev_dn_bid > 0 and abs(db - prev_dn_bid) >= 0.005:
                pm_events.append((el, "DN", prev_dn_bid, db))
            if cb > 0:
                prev_cb = cb
            if ub > 0:
                prev_up_bid = ub
            if db > 0:
                prev_dn_bid = db
    out = []
    consumed = set()
    for cb_ts, cb_old, cb_new in cb_events:
        cb_up = cb_new > cb_old
        for j, (pm_ts, side, ob, nb) in enumerate(pm_events):
            if j in consumed:
                continue
            lag = pm_ts - cb_ts
            if lag < 0:
                continue
            if lag > freshness_ms:
                break
            pm_up = nb > ob
            aligned = (
                (cb_up and ((side == "UP" and pm_up) or (side == "DN" and not pm_up)))
                or (not cb_up and ((side == "UP" and not pm_up) or (side == "DN" and pm_up)))
            )
            out.append({
                "cb_abs": abs(cb_new - cb_old),
                "lag_ms": lag,
                "aligned": aligned,
                "source": "backtest",
            })
            consumed.add(j)
            break
    return out


def load_live_lag(path: Path) -> List[dict]:
    rows: List[dict] = []
    if not path.exists():
        return rows
    with path.open() as f:
        for line in f:
            try:
                r = json.loads(line)
                rows.append({
                    "cb_abs": r.get("cb_abs", abs(r.get("cb_delta", 0))),
                    "lag_ms": r["lag_ms"],
                    "aligned": bool(r.get("aligned", False)),
                    "source": "live",
                })
            except (json.JSONDecodeError, KeyError):
                continue
    return rows


def pct(xs: List[int], p: float) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    i = int(p * (len(s) - 1))
    return s[i]


def summary(events: List[dict], label: str) -> dict:
    n = len(events)
    if n == 0:
        return {"label": label, "n": 0}
    lags = [e["lag_ms"] for e in events]
    al = sum(1 for e in events if e["aligned"])
    return {
        "label": label,
        "n": n,
        "p10": pct(lags, 0.10),
        "p50": pct(lags, 0.50),
        "p90": pct(lags, 0.90),
        "max": max(lags),
        "aligned_pct": 100 * al / n,
    }


def fmt_summary(s: dict) -> str:
    if s["n"] == 0:
        return f"{s['label']:<14} n=0"
    return (f"{s['label']:<14} n={s['n']:>5}  "
            f"p10={s['p10']:>4}ms  p50={s['p50']:>4}ms  "
            f"p90={s['p90']:>4}ms  max={s['max']:>5}ms  "
            f"aligned={s['aligned_pct']:>2.0f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", default="/home/ubuntu/polymarket-bot/logs/btc_s7_pm_lag.jsonl")
    ap.add_argument("--backtest", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--cb-threshold", type=float, default=1.0)
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    live = load_live_lag(Path(args.live))
    print(f"Live PM lag events: {len(live)}", flush=True)

    bt_files = sorted(Path(args.backtest).glob("ticks_*.csv*"))
    if args.max_files:
        bt_files = bt_files[:args.max_files]
    backtest = []
    print(f"Backtest tick files: {len(bt_files)} — measuring lag...", flush=True)
    for i, f in enumerate(bt_files):
        backtest.extend(measure_lag_from_tick(f, args.cb_threshold))
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(bt_files)} done", flush=True)
    print(f"Backtest PM lag events: {len(backtest)}", flush=True)
    print(flush=True)

    print("=== A. Side-by-side distribution ===", flush=True)
    print(fmt_summary(summary(live, "LIVE")), flush=True)
    print(fmt_summary(summary(backtest, "BACKTEST")), flush=True)
    print(flush=True)

    # === B. By CB delta bucket ===
    print("=== B. By CB delta size (LIVE vs BACKTEST) ===", flush=True)
    buckets = [(1, 3, "$1-3"), (3, 5, "$3-5"), (5, 10, "$5-10"), (10, 1e9, "$10+")]
    for lo, hi, label in buckets:
        bl = [e for e in live if lo <= e["cb_abs"] < hi]
        bb = [e for e in backtest if lo <= e["cb_abs"] < hi]
        print(f"  {label:<8}", flush=True)
        print(f"    {fmt_summary(summary(bl, 'LIVE'))}", flush=True)
        print(f"    {fmt_summary(summary(bb, 'BACKTEST'))}", flush=True)
    print(flush=True)

    # === C. Recommended adjustments ===
    if len(live) > 0 and len(backtest) > 0:
        live_p50 = pct([e["lag_ms"] for e in live], 0.5)
        bt_p50 = pct([e["lag_ms"] for e in backtest], 0.5)
        live_aligned = 100 * sum(1 for e in live if e["aligned"]) / len(live)
        bt_aligned = 100 * sum(1 for e in backtest if e["aligned"]) / len(backtest)

        print("=== C. Recommended PaperOrderEngine knobs ===", flush=True)
        if live_p50 > bt_p50:
            print(f"  Live median lag ({live_p50}ms) > backtest ({bt_p50}ms)", flush=True)
            print(f"  → Consider raising taker_latency_ms by "
                  f"~{live_p50 - bt_p50}ms to match", flush=True)
        else:
            print(f"  Live median lag ({live_p50}ms) <= backtest ({bt_p50}ms): OK", flush=True)

        if live_aligned < bt_aligned - 5:
            print(f"  Live aligned% ({live_aligned:.0f}) < backtest ({bt_aligned:.0f})", flush=True)
            print(f"  → Live sees more random PM moves; consider raising "
                  f"quote_evap_rate to penalize fills more aggressively", flush=True)
        else:
            print(f"  Live aligned% ({live_aligned:.0f}) ≈ backtest "
                  f"({bt_aligned:.0f}): OK", flush=True)


if __name__ == "__main__":
    main()
