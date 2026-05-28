"""
S7 full backtest v4 — calibrated PM feed lag.

Differences from v3:
  * pm_feed_lag_ms=559 — calibrated from the live DRY_RUN comparison:
    live PM median lag = 520ms, backtest = 1079ms, diff = 559ms.
    Setting pm_feed_lag_ms=559 makes the engine match a live order against
    the PM book at (placed_at + taker_latency + 559)ms, modeling the
    fact that live PM has had 559ms more time to drift away from us than
    raw tick files suggest.

Without this calibration, sim overstates fillable WR because it gives
S7 fades a wider edge window than live actually offers.

Compares 4 ways:
  A. Baseline (no fixes) — replicates v3 baseline
  B. Bid-evap only (v3 default)
  C. Bid-evap + PM feed lag 559ms (NEW v4 production)
  D. All of the above + regime filter (control — we know it doesn't help,
     just confirming it doesn't help here either)

Usage:
    python3 s7_full_backtest_v4.py [--max-files N] [--pm-lag-ms 559]
"""
from __future__ import annotations
import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean
from typing import Dict, List, Tuple

from order_engine import PaperOrderEngine
from strategies import S7FadeStrategy, StrategyRunner


def wilson_ci(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def run_one(name: str, files: List[Path], resolutions: dict, days: float,
            latency_ms: int, shares: int,
            quote_evap_rate: float, book_walk_slip: float,
            pm_feed_lag_ms: int, regime_filter: bool,
            rng_seed: int = 42) -> dict:
    engine = PaperOrderEngine(
        starting_usdc=10000,
        taker_latency_ms=latency_ms,
        use_trade_events=False,
        min_notional_usdc=1.0,
        quote_evap_rate=quote_evap_rate,
        book_walk_excess_slippage=book_walk_slip,
        pm_feed_lag_ms=pm_feed_lag_ms,
        rng_seed=rng_seed,
    )
    strat = S7FadeStrategy(engine, shares=shares, min_notional_target=1.20,
                           regime_filter_enabled=regime_filter)
    runner = StrategyRunner(engine)
    runner.add_strategy(strat)
    runner.run_tick_files(files)
    trades = [t for t in strat.trades if t.order.filled_size >= 1]

    enriched = []
    for t in trades:
        try:
            window_ts = int(t.order.token_id.split("_")[1].split(".")[0])
        except Exception:
            continue
        res = resolutions.get(window_ts)
        if not res or not res.get("resolved"):
            continue
        real_winner = "UP" if res.get("up_winning") else "DN"
        won = (t.direction == real_winner)
        fee_ps = (t.order.total_fees / t.order.filled_size) if t.order.filled_size > 0 else 0
        pnl_ps = (1.00 if won else 0.00) - t.order.fill_avg_price - fee_ps
        enriched.append({"won": won, "pnl": pnl_ps * t.order.filled_size,
                         "entry": t.order.fill_avg_price})

    n = len(enriched)
    wins = sum(1 for t in enriched if t["won"])
    total = sum(t["pnl"] for t in enriched)
    lo, hi = wilson_ci(wins, n)
    return {
        "name": name,
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "wr": wins / n if n else 0,
        "wr_ci": (lo, hi),
        "total": total,
        "per_day": total / max(0.01, days),
        "avg_entry": mean(t["entry"] for t in enriched) if enriched else 0,
        "attempts": engine.taker_attempts,
        "stale_ask_rejects": engine.taker_rejections,
        "quote_evaporations": engine.quote_evaporations,
    }


def fmt(r: dict) -> str:
    lo, hi = r["wr_ci"]
    return (f"{r['name']:<40} attempts={r['attempts']:>3}  fills={r['n']:>3}  "
            f"W={r['wins']:>3} L={r['losses']:>3}  "
            f"WR={100*r['wr']:>3.0f}% [{100*lo:>2.0f}-{100*hi:>2.0f}%]  "
            f"total=${r['total']:>+7.2f}  /day=${r['per_day']:>+6.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--resolutions", default="/home/ubuntu/reports/resolutions.json")
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--latency-ms", type=int, default=1500)
    ap.add_argument("--shares", type=int, default=6)
    ap.add_argument("--quote-evap-rate", type=float, default=0.19)
    ap.add_argument("--book-walk-slip", type=float, default=0.012)
    ap.add_argument("--pm-feed-lag-ms", type=int, default=559)
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    with open(args.resolutions) as f:
        resolutions = {int(k): v for k, v in json.load(f).items()}
    files = sorted(Path(args.ticks).glob("ticks_*.csv*"))
    if args.max_files:
        files = files[:args.max_files]
    days = len(files) * 5 / 60 / 24

    print(f"=== S7 full backtest v4 ===", flush=True)
    print(f"Windows: {len(files)} ({days:.2f} days)", flush=True)
    print(f"NEW: pm_feed_lag_ms={args.pm_feed_lag_ms}ms (calibrated from live DRY_RUN)", flush=True)
    print(f"Existing: latency={args.latency_ms}ms, "
          f"quote_evap={args.quote_evap_rate}, "
          f"book_walk_slip=${args.book_walk_slip:.3f}", flush=True)
    print(flush=True)

    configs = [
        ("A. Baseline (no fixes)",                    0.0,                       0.0,                     0,                     False),
        ("B. v3 (evap only)",                         args.quote_evap_rate,      args.book_walk_slip,     0,                     False),
        ("C. v4 (evap + PM feed lag)",                args.quote_evap_rate,      args.book_walk_slip,     args.pm_feed_lag_ms,   False),
        ("D. v4 + regime filter (control)",           args.quote_evap_rate,      args.book_walk_slip,     args.pm_feed_lag_ms,   True),
    ]
    results = []
    for name, qe, bw, pml, rf in configs:
        print(f"Running: {name}...", flush=True)
        r = run_one(name, files, resolutions, days, args.latency_ms,
                    args.shares, qe, bw, pml, rf)
        results.append(r)
        print(f"  → {fmt(r)}", flush=True)
        print(flush=True)

    print("=" * 110, flush=True)
    print("FINAL COMPARISON", flush=True)
    print("=" * 110, flush=True)
    for r in results:
        print(fmt(r), flush=True)
    print(flush=True)

    # Live target
    print(f"=== Live target: WR=25% over n=12 (~-$4.47/day) ===", flush=True)
    v4 = results[2]
    diff_pp = 100 * (v4["wr"] - 0.25)
    print(f"v4 sim predicts WR={100*v4['wr']:.0f}% — gap to live: {diff_pp:+.0f}pp", flush=True)
    if v4["wr"] <= 0.40:
        print(f"  → v4 now MATCHES live within reasonable margin. PM feed lag was the missing piece.", flush=True)
    elif v4["wr"] <= 0.55:
        print(f"  → v4 closes most of the gap. Some residual unexplained.", flush=True)
    else:
        print(f"  → v4 still over-predicts. PM feed lag alone doesn't close the gap.", flush=True)


if __name__ == "__main__":
    main()
