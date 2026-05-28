"""
S7 + S18 regime-split backtest.

For each historical window, classify the regime BEFORE running the strategy,
then bucket trades by regime to see whether the strategy's edge holds /
inverts / disappears per regime.

This is the validation step for tasks #2 (CB autocorrelation tagger) and #4.

Methodology:
    1. Use the FIRST 60 seconds of each window's CB ticks (matches what the
       live bot would see at signal-time) to classify regime.
    2. Run the chosen strategy across all 434 windows with our standard
       backtest config (latency=1500ms, Option B sizing, corrected fees,
       real resolutions, cap+floor).
    3. Tag each filled trade with its window's regime label.
    4. Report WR / P&L / $/day per regime.

Expected outcome:
    S7 (fade)    — wins in EXHAUSTION, loses in MOMENTUM, neutral in RANGING.
    S18 (aligned)— wins in MOMENTUM, loses in EXHAUSTION, neutral in RANGING.
    If a strategy shows the EXPECTED inversion across regimes, regime
    filtering can lift its overall WR materially.

Usage:
    python3 backtest_split_by_regime.py [--strategy s7|s18] [--max-files N]
"""
from __future__ import annotations
import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional, Tuple

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


def get_strategy(name: str, engine, shares: int):
    if name == "s7":
        return S7FadeStrategy(engine, shares=shares, min_notional_target=1.20)
    if name == "s18":
        try:
            from s18_aligned_delta_5s10 import S18AlignedDelta5s10Strategy
        except ImportError:
            from s18_aligned_5s10 import S18AlignedDelta5s10Strategy
        return S18AlignedDelta5s10Strategy(engine, shares=shares,
                                            min_notional_target=1.20)
    raise ValueError(f"unknown strategy: {name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=["s7", "s18"], default="s7")
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--resolutions", default="/home/ubuntu/reports/resolutions.json")
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--latency-ms", type=int, default=1500)
    ap.add_argument("--shares", type=int, default=6)
    ap.add_argument("--regime-lookback-s", type=int, default=60)
    ap.add_argument("--regime-at-elapsed-ms", type=int, default=60_000,
                    help="Use only CB ticks up to this elapsed ms to "
                         "classify regime (None = use entire window)")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    with open(args.resolutions) as f:
        resolutions: Dict[int, dict] = {int(k): v for k, v in json.load(f).items()}

    files = sorted(Path(args.ticks).glob("ticks_*.csv*"))
    if args.max_files:
        files = files[:args.max_files]
    days = len(files) * 5 / 60 / 24

    print(f"=== Regime-split backtest: {args.strategy.upper()} ===", flush=True)
    print(f"Windows: {len(files)} ({days:.2f} days)", flush=True)
    print(f"Regime tagger: lookback={args.regime_lookback_s}s, "
          f"classify at elapsed_ms <= {args.regime_at_elapsed_ms}", flush=True)
    print(flush=True)

    # Pass 1: tag every window's regime
    print("Pass 1: tagging windows...", flush=True)
    regime_by_window: Dict[int, str] = {}
    regime_stats_by_window: Dict[int, dict] = {}
    regime_counts: Dict[str, int] = defaultdict(int)
    for i, f in enumerate(files):
        try:
            window_ts = int(f.stem.split("_")[1].split(".")[0])
        except (IndexError, ValueError):
            continue
        regime, stats = classify_window_from_csv(
            f, lookback_s=args.regime_lookback_s,
            at_elapsed_ms=args.regime_at_elapsed_ms,
        )
        regime_by_window[window_ts] = regime
        regime_stats_by_window[window_ts] = stats
        regime_counts[regime] += 1
        if (i + 1) % 100 == 0:
            print(f"  tagged {i+1}/{len(files)}...", flush=True)

    print(flush=True)
    print(f"Regime distribution:", flush=True)
    for regime in ["MOMENTUM", "EXHAUSTION", "RANGING", "INSUFFICIENT"]:
        n = regime_counts.get(regime, 0)
        if n:
            print(f"  {regime}: {n} ({100*n/len(files):.0f}%)", flush=True)
    print(flush=True)

    # Pass 2: run the strategy
    print(f"Pass 2: running {args.strategy.upper()} backtest...", flush=True)
    engine = PaperOrderEngine(starting_usdc=10000, taker_latency_ms=args.latency_ms,
                              use_trade_events=False, min_notional_usdc=1.0)
    strat = get_strategy(args.strategy, engine, args.shares)
    runner = StrategyRunner(engine)
    runner.add_strategy(strat)
    runner.run_tick_files(files)
    trades = [t for t in strat.trades if t.order.filled_size >= 1]

    # Enrich with real resolutions + regime tag
    enriched: List[dict] = []
    no_resolution = 0
    for t in trades:
        try:
            window_ts = int(t.order.token_id.split("_")[1].split(".")[0])
        except Exception:
            continue
        res = resolutions.get(window_ts)
        if not res or not res.get("resolved"):
            no_resolution += 1
            continue
        real_winner = "UP" if res.get("up_winning") else "DN"
        won = (t.direction == real_winner)
        fee_ps = (t.order.total_fees / t.order.filled_size) if t.order.filled_size > 0 else 0
        pnl_ps = (1.00 if won else 0.00) - t.order.fill_avg_price - fee_ps
        enriched.append({
            "entry": t.order.fill_avg_price,
            "shares": t.order.filled_size,
            "won": won,
            "pnl": pnl_ps * t.order.filled_size,
            "direction": t.direction,
            "regime": regime_by_window.get(window_ts, "INSUFFICIENT"),
            "window_ts": window_ts,
        })

    print(flush=True)
    print(f"Filled trades: {len(enriched)} (no-resolution: {no_resolution})", flush=True)
    print(flush=True)

    if not enriched:
        print("No resolved trades!", flush=True)
        return

    # Overall result
    n = len(enriched)
    wins = sum(1 for t in enriched if t["won"])
    total = sum(t["pnl"] for t in enriched)
    wlo, whi = wilson_ci(wins, n)
    per_day = total / max(0.01, days)
    print(f"=== OVERALL ===", flush=True)
    print(f"n={n}  W={wins}  L={n-wins}", flush=True)
    print(f"WR={100*wins/n:.0f}%  95% CI [{wlo*100:.0f}% — {whi*100:.0f}%]", flush=True)
    print(f"Avg entry: ${mean(t['entry'] for t in enriched):.3f}", flush=True)
    print(f"Avg PnL/trade: ${mean(t['pnl'] for t in enriched):+.3f}", flush=True)
    print(f"Total: ${total:+.2f}  /day: ${per_day:+.2f}", flush=True)
    print(flush=True)

    # Per-regime
    print(f"=== BY REGIME ===", flush=True)
    header = f"{'Regime':<14} {'n':>4} {'W':>3} {'L':>3} {'WR':>5} {'CI':>16} {'avgPnL':>9} {'total':>9} {'$/day':>9}"
    print(header, flush=True)
    print("-" * len(header), flush=True)
    by_regime: Dict[str, List[dict]] = defaultdict(list)
    for t in enriched:
        by_regime[t["regime"]].append(t)
    for regime in ["MOMENTUM", "EXHAUSTION", "RANGING", "INSUFFICIENT"]:
        ts_ = by_regime.get(regime, [])
        if not ts_:
            continue
        rn = len(ts_)
        rw = sum(1 for t in ts_ if t["won"])
        rp = [t["pnl"] for t in ts_]
        rlo, rhi = wilson_ci(rw, rn)
        # Per-day requires per-regime window count
        n_windows = regime_counts.get(regime, 1)
        regime_days = n_windows * 5 / 60 / 24
        per_day_r = sum(rp) / max(0.01, regime_days)
        ci_str = f"[{100*rlo:.0f}-{100*rhi:.0f}%]"
        print(f"{regime:<14} {rn:>4} {rw:>3} {rn-rw:>3} {100*rw/rn:>4.0f}% "
              f"{ci_str:>16} {mean(rp):>+8.3f} {sum(rp):>+8.2f} {per_day_r:>+8.2f}",
              flush=True)
    print(flush=True)

    # Diagnostic: what regime were the live-bot trades in?
    # (For S7 only; map known live trades.)
    if args.strategy == "s7":
        # Live trade window timestamps (from stdout RESOLVE)
        # We don't have the exact window_ts here, so just report
        # MOMENTUM proportion: if live trades happen disproportionately
        # in MOMENTUM windows that's the smoking gun.
        pass

    # Persist json for downstream tooling
    out_path = Path(f"/tmp/regime_split_{args.strategy}.json")
    with out_path.open("w") as f:
        json.dump({
            "strategy": args.strategy,
            "n_windows": len(files),
            "n_trades": n,
            "overall_wr": wins / n,
            "by_regime": {
                regime: {
                    "n_trades": len(by_regime.get(regime, [])),
                    "wins": sum(1 for t in by_regime.get(regime, []) if t["won"]),
                    "n_windows": regime_counts.get(regime, 0),
                    "total_pnl": sum(t["pnl"] for t in by_regime.get(regime, [])),
                }
                for regime in by_regime
            },
            "regime_distribution": dict(regime_counts),
        }, f, indent=2)
    print(f"Wrote summary → {out_path}", flush=True)


if __name__ == "__main__":
    main()
