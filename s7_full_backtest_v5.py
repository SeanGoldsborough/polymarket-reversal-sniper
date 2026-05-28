"""
S7 full backtest v5 — TA-driven filter sweep.

Builds on v4 (calibrated PM feed lag, bid-evap) and adds the four course-
derived signal filters + v2 5-substate regime classifier, each isolated
so we can see which filters move the needle.

Configs tested:
  A. v4 baseline (no filters)
  B. + HardCloseFilter (no wicks)
  C. + VolumeClimaxFilter (only fade when tick density is fading)
  D. + PolarityFilter (only fade at recent S/R levels)
  E. + MultiTFFilter (only fade when 1-min CB structure agrees)
  F. + All 4 filters combined (FilterChain)
  G. + v2 RegimeClassifier (only DISTRIBUTION fires)
  H. + v2 + all filters (production candidate)

Per config we report n_signals, n_fills, WR, $/day, plus filter-skip
reasons so we can see WHY trades were filtered out.

Usage:
    python3 s7_full_backtest_v5.py [--max-files N]
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
from s7_signal_filters import (
    HardCloseFilter, VolumeClimaxFilter, PolarityFilter, MultiTFFilter,
    FilterChain,
)
from regime_classifier_v2 import RegimeClassifierV2


def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def make_filter_chain(name: str):
    """Return a FilterChain for the named config, or None for baseline."""
    if name == "A":
        return None, None
    if name == "B":
        return FilterChain(filters=[HardCloseFilter(hold_ms=2000, revert_threshold=5.0)]), None
    if name == "C":
        return FilterChain(filters=[VolumeClimaxFilter(mode="fade")]), None
    if name == "D":
        return FilterChain(filters=[PolarityFilter(lookback_ms=180_000, pivot_window=3, proximity=5.0)]), None
    if name == "E":
        return FilterChain(filters=[MultiTFFilter(lookback_ms=60_000, recent_ms=30_000)]), None
    if name == "F":
        return FilterChain(filters=[
            HardCloseFilter(hold_ms=2000, revert_threshold=5.0),
            VolumeClimaxFilter(mode="fade"),
            PolarityFilter(lookback_ms=180_000, pivot_window=3, proximity=5.0),
            MultiTFFilter(lookback_ms=60_000, recent_ms=30_000),
        ]), None
    if name == "G":
        return None, RegimeClassifierV2(min_history_s=60)
    if name == "H":
        return FilterChain(filters=[
            HardCloseFilter(hold_ms=2000, revert_threshold=5.0),
            VolumeClimaxFilter(mode="fade"),
            PolarityFilter(lookback_ms=180_000, pivot_window=3, proximity=5.0),
            MultiTFFilter(lookback_ms=60_000, recent_ms=30_000),
        ]), RegimeClassifierV2(min_history_s=60)
    raise ValueError(name)


def run_one(name, label, files, resolutions, days, latency_ms, shares,
            quote_evap_rate, book_walk_slip, pm_feed_lag_ms):
    fc, cl = make_filter_chain(name)
    engine = PaperOrderEngine(
        starting_usdc=10000, taker_latency_ms=latency_ms,
        use_trade_events=False, min_notional_usdc=1.0,
        quote_evap_rate=quote_evap_rate,
        book_walk_excess_slippage=book_walk_slip,
        pm_feed_lag_ms=pm_feed_lag_ms,
        rng_seed=42,
    )
    strat = S7FadeStrategy(
        engine, shares=shares, min_notional_target=1.20,
        filter_chain=fc,
        regime_classifier_v2=cl,
        v2_allowed_regimes=("DISTRIBUTION",),
    )
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
        enriched.append({"won": won, "pnl": pnl_ps * t.order.filled_size})

    n = len(enriched)
    wins = sum(1 for e in enriched if e["won"])
    total = sum(e["pnl"] for e in enriched)
    lo, hi = wilson_ci(wins, n)
    skips = {
        "death_zone": strat.death_zone_skips,
        "v2_filter": strat.v2_filter_skips,
        "v2_regime": strat.v2_regime_skips,
    }
    chain_rejects = fc.reject_counts if fc else {}
    return {
        "name": name, "label": label,
        "n": n, "wins": wins, "losses": n - wins,
        "wr": wins / n if n else 0,
        "wr_ci": (lo, hi),
        "total": total,
        "per_day": total / max(0.01, days),
        "attempts": engine.taker_attempts,
        "skips": skips,
        "v2_regime_fires": dict(strat.v2_regime_fires_by_label),
        "chain_rejects": dict(chain_rejects),
    }


def fmt(r):
    lo, hi = r["wr_ci"]
    return (f"{r['name']} {r['label']:<36} att={r['attempts']:>3} "
            f"fills={r['n']:>3}  W={r['wins']:>3}L={r['losses']:>3} "
            f"WR={100*r['wr']:>3.0f}% [{100*lo:>2.0f}-{100*hi:>2.0f}%] "
            f"$/day=${r['per_day']:>+6.2f}")


def main():
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

    print(f"=== S7 full backtest v5 — TA filter sweep ===", flush=True)
    print(f"Windows: {len(files)} ({days:.2f} days)  latency={args.latency_ms}ms", flush=True)
    print(f"Engine: quote_evap={args.quote_evap_rate} book_walk_slip=${args.book_walk_slip:.3f} "
          f"pm_feed_lag={args.pm_feed_lag_ms}ms", flush=True)
    print(flush=True)

    configs = [
        ("A", "v4 baseline (no filters)"),
        ("B", "+ HardCloseFilter"),
        ("C", "+ VolumeClimaxFilter"),
        ("D", "+ PolarityFilter"),
        ("E", "+ MultiTFFilter"),
        ("F", "+ ALL 4 filters"),
        ("G", "+ v2 RegimeClassifier (DIST only)"),
        ("H", "+ v2 + all filters (PROD)"),
    ]
    results = []
    for name, label in configs:
        print(f"Running {name}: {label}...", flush=True)
        r = run_one(name, label, files, resolutions, days, args.latency_ms,
                    args.shares, args.quote_evap_rate, args.book_walk_slip,
                    args.pm_feed_lag_ms)
        results.append(r)
        print(f"  {fmt(r)}", flush=True)
        if r["skips"]["v2_filter"] or r["skips"]["v2_regime"] or r["chain_rejects"]:
            print(f"    skips: {r['skips']}", flush=True)
            if r["chain_rejects"]:
                top3 = sorted(r["chain_rejects"].items(), key=lambda kv: -kv[1])[:3]
                print(f"    top filter rejects: {top3}", flush=True)
            if r["v2_regime_fires"]:
                print(f"    v2 fires by regime: {r['v2_regime_fires']}", flush=True)
        print(flush=True)

    print("=" * 100, flush=True)
    print("FINAL COMPARISON", flush=True)
    print("=" * 100, flush=True)
    for r in results:
        print(fmt(r), flush=True)
    print(flush=True)
    print(f"Live target: WR=25% n=12 ~-$4.47/day", flush=True)


if __name__ == "__main__":
    main()
