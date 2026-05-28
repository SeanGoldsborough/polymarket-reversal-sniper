"""
S7 full backtest v3 — addresses live-vs-sim gap.

Differences from v2:
  * Engine: quote_evap_rate=0.19 + book_walk_excess_slippage=$0.012 to match
    the empirical drift distribution measured on 47 historical signals.
    Empirical reject rate: 34%; sim's existing stale-ask reject: 15%.
    The 19pp gap is bid evaporation (price-level liquidity vanishing during
    1500ms latency), which the engine modeled at 0 before.
  * Strategy: S7FadeStrategy(regime_filter_enabled=True), so the regime
    filter is enforced at signal-time inside the strategy (not post-hoc).

Compares against v2 baseline so we can see how much each fix moves WR.

Usage:
    python3 s7_full_backtest_v3.py [--max-files N] [--no-regime] [--no-evap]
"""
from __future__ import annotations
import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Dict, List, Tuple

from order_engine import PaperOrderEngine
from strategies import S7FadeStrategy, StrategyRunner
from cb_regime_tagger import classify_window_from_csv


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
            regime_filter: bool, rng_seed: int = 42) -> dict:
    engine = PaperOrderEngine(
        starting_usdc=10000,
        taker_latency_ms=latency_ms,
        use_trade_events=False,
        min_notional_usdc=1.0,
        quote_evap_rate=quote_evap_rate,
        book_walk_excess_slippage=book_walk_slip,
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
        "regime_skips": getattr(strat, "regime_skips", 0),
        "regime_insufficient": getattr(strat, "regime_insufficient", 0),
        "regime_fires_by_label": getattr(strat, "regime_fires_by_label", {}),
    }


def fmt(r: dict) -> str:
    lo, hi = r["wr_ci"]
    return (f"{r['name']:<32} attempts={r['attempts']:>3}  fills={r['n']:>3}  "
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
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    with open(args.resolutions) as f:
        resolutions = {int(k): v for k, v in json.load(f).items()}
    files = sorted(Path(args.ticks).glob("ticks_*.csv*"))
    if args.max_files:
        files = files[:args.max_files]
    days = len(files) * 5 / 60 / 24

    print(f"=== S7 full backtest v3 ===", flush=True)
    print(f"Windows: {len(files)} ({days:.2f} days)  latency={args.latency_ms}ms", flush=True)
    print(f"Bid-evap model: quote_evap={args.quote_evap_rate} "
          f"book_walk_slip=${args.book_walk_slip:.3f}", flush=True)
    print(flush=True)

    # 4-way comparison
    configs = [
        ("Baseline (v2, no fixes)",     0.0,                       0.0,                     False),
        ("With bid-evap only",          args.quote_evap_rate,      args.book_walk_slip,     False),
        ("With regime filter only",     0.0,                       0.0,                     True),
        ("With BOTH (v3 production)",   args.quote_evap_rate,      args.book_walk_slip,     True),
    ]
    results = []
    for name, qe, bw, rf in configs:
        print(f"Running: {name}...", flush=True)
        r = run_one(name, files, resolutions, days, args.latency_ms,
                    args.shares, qe, bw, rf)
        results.append(r)
        print(f"  → {fmt(r)}", flush=True)
        print(flush=True)

    print("=" * 100, flush=True)
    print("FINAL COMPARISON", flush=True)
    print("=" * 100, flush=True)
    for r in results:
        print(fmt(r), flush=True)
    print(flush=True)

    # Diagnostic on regime filter activity
    rf_result = next((r for r in results if "BOTH" in r["name"]), None)
    if rf_result:
        print(f"=== Regime filter diagnostic (v3) ===", flush=True)
        print(f"Regime-classified fires: {rf_result['regime_fires_by_label']}", flush=True)
        print(f"Regime skips (non-RANGING): {rf_result['regime_skips']}", flush=True)
        print(f"Insufficient-history (couldn't classify): {rf_result['regime_insufficient']}", flush=True)
    print(flush=True)

    # Compare to live (n=12, WR=25%) for context
    print(f"=== Live S7 (n=12): WR=25%, P&L approx -$6.71 over 1.5 days "
          f"= -${6.71/1.5:.2f}/day ===", flush=True)
    print(f"If v3 still over-predicts WR vs live, more refinement needed.", flush=True)


if __name__ == "__main__":
    main()
