"""
S7 full backtest — combines all 4 regime-detection / safety tools.

Compared to s7_complete_backtest.py, this run additionally:
  1. Tags each window's regime via CB autocorrelation (cb_regime_tagger).
  2. Simulates the rolling-WR circuit breaker chronologically over the
     trade sequence.
  3. Measures PM bid-lag distribution from the tick files (CB tick → first
     same-direction PM bid change).
  4. Reports per-regime AND breaker-on/off AND regime-filter-on/off so we
     can see which guard rails actually move the needle.

Output sections:
  A. BASELINE — same as s7_complete_backtest (no filters, no breaker)
  B. BREAKER-ON — chronologically simulate the breaker on the trade sequence
  C. REGIME-FILTERED — only trades that fired in RANGING windows
  D. BOTH — regime filter + breaker
  E. PM LAG — distribution of CB → PM reaction latency, by regime
  F. PER-REGIME — same as backtest_split_by_regime (re-reported for reference)

Usage:
    python3 s7_full_backtest_v2.py [--max-files N]
"""
from __future__ import annotations
import argparse
import gzip
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional, Tuple

from order_engine import PaperOrderEngine
from strategies import S7FadeStrategy, StrategyRunner
from cb_regime_tagger import classify_window_from_csv
from circuit_breaker import RollingWRBreaker
import csv


def wilson_ci(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def fmt_block(label: str, trades: List[dict], days: float) -> str:
    if not trades:
        return f"{label:<28} n=0"
    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    pnl = sum(t["pnl"] for t in trades)
    lo, hi = wilson_ci(wins, n)
    return (f"{label:<28} n={n:>3}  W={wins:>3} L={n-wins:>3}  "
            f"WR={100*wins/n:>3.0f}%  CI [{100*lo:>2.0f}-{100*hi:>2.0f}%]  "
            f"total=${pnl:>+7.2f}  /day=${pnl/max(0.01,days):>+6.2f}")


def measure_pm_lag(tick_path: Path, cb_threshold: float = 1.0,
                   freshness_ms: int = 5000) -> List[dict]:
    """Walk a tick file and emit (cb_delta, lag_ms, aligned) records.
    For each CB move >= cb_threshold, find the first same-direction PM bid
    change within freshness_ms."""
    opener = gzip.open if str(tick_path).endswith(".gz") else open
    cb_events: List[Tuple[int, float, float]] = []  # (elapsed_ms, cb_old, cb_new)
    pm_events: List[Tuple[int, str, float, float]] = []  # (elapsed_ms, side, old_bid, new_bid)
    prev_cb = 0.0
    prev_up_bid = 0.0
    prev_dn_bid = 0.0
    with opener(tick_path, "rt") as f:
        for row in csv.DictReader(f):
            try:
                el = int(row["elapsed_ms"])
                cb = float(row["cb_price"])
                up_b = float(row["up_bid"])
                dn_b = float(row["down_bid"])
            except (KeyError, ValueError):
                continue
            if cb > 0 and prev_cb > 0 and abs(cb - prev_cb) >= cb_threshold:
                cb_events.append((el, prev_cb, cb))
            if up_b > 0 and prev_up_bid > 0 and abs(up_b - prev_up_bid) >= 0.005:
                pm_events.append((el, "UP", prev_up_bid, up_b))
            if dn_b > 0 and prev_dn_bid > 0 and abs(dn_b - prev_dn_bid) >= 0.005:
                pm_events.append((el, "DN", prev_dn_bid, dn_b))
            if cb > 0:
                prev_cb = cb
            if up_b > 0:
                prev_up_bid = up_b
            if dn_b > 0:
                prev_dn_bid = dn_b
    # Match cb events to first within-freshness same-direction PM move
    out: List[dict] = []
    consumed_pm: set = set()
    for cb_ts, cb_old, cb_new in cb_events:
        cb_up = cb_new > cb_old
        for j, (pm_ts, side, ob, nb) in enumerate(pm_events):
            if j in consumed_pm:
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
            })
            consumed_pm.add(j)
            break
    return out


def pct(xs: List[int], p: float) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    i = int(p * (len(s) - 1))
    return s[i]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--resolutions", default="/home/ubuntu/reports/resolutions.json")
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--latency-ms", type=int, default=1500)
    ap.add_argument("--shares", type=int, default=6)
    ap.add_argument("--cb-window-n", type=int, default=5)
    ap.add_argument("--cb-min-wr", type=float, default=0.40)
    ap.add_argument("--cb-pause-hours", type=float, default=4.0)
    ap.add_argument("--regime-lookback-s", type=int, default=60)
    ap.add_argument("--regime-at-elapsed-ms", type=int, default=60_000)
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    with open(args.resolutions) as f:
        resolutions: Dict[int, dict] = {int(k): v for k, v in json.load(f).items()}

    files = sorted(Path(args.ticks).glob("ticks_*.csv*"))
    if args.max_files:
        files = files[:args.max_files]
    days = len(files) * 5 / 60 / 24

    print(f"=== S7 full backtest (4-tool combined) ===", flush=True)
    print(f"Windows: {len(files)} ({days:.2f} days)", flush=True)
    print(f"Config: latency={args.latency_ms}ms shares={args.shares}base Option-B cap+floor "
          f"realFees realRes", flush=True)
    print(f"Breaker: window_n={args.cb_window_n} min_wr={args.cb_min_wr:.0%} "
          f"pause={args.cb_pause_hours}h", flush=True)
    print(f"Regime tagger: lookback={args.regime_lookback_s}s, "
          f"classify at elapsed_ms <= {args.regime_at_elapsed_ms}", flush=True)
    print(flush=True)

    # ── Pass 1: tag every window's regime + measure PM lag ──
    print("Pass 1: tagging regimes + measuring PM lag per window...", flush=True)
    regime_by_window: Dict[int, str] = {}
    regime_counts: Dict[str, int] = defaultdict(int)
    pm_lag_by_regime: Dict[str, List[dict]] = defaultdict(list)
    for i, f in enumerate(files):
        try:
            window_ts = int(f.stem.split("_")[1].split(".")[0])
        except (IndexError, ValueError):
            continue
        regime, _ = classify_window_from_csv(
            f, lookback_s=args.regime_lookback_s,
            at_elapsed_ms=args.regime_at_elapsed_ms,
        )
        regime_by_window[window_ts] = regime
        regime_counts[regime] += 1
        # PM lag (cheap: only on sampled windows to keep runtime down)
        if i % 5 == 0:  # 20% sample
            try:
                pm_lag_by_regime[regime].extend(measure_pm_lag(f))
            except Exception:
                pass
        if (i + 1) % 100 == 0:
            print(f"  pass1: {i+1}/{len(files)} done", flush=True)

    print(flush=True)
    print(f"Regime distribution:", flush=True)
    for regime in ["MOMENTUM", "EXHAUSTION", "RANGING", "INSUFFICIENT"]:
        n = regime_counts.get(regime, 0)
        if n:
            print(f"  {regime}: {n} ({100*n/len(files):.0f}%)", flush=True)
    print(flush=True)

    # ── Pass 2: run S7 ──
    print("Pass 2: running S7 strategy...", flush=True)
    engine = PaperOrderEngine(starting_usdc=10000, taker_latency_ms=args.latency_ms,
                              use_trade_events=False, min_notional_usdc=1.0)
    strat = S7FadeStrategy(engine, shares=args.shares, min_notional_target=1.20)
    runner = StrategyRunner(engine)
    runner.add_strategy(strat)
    runner.run_tick_files(files)
    trades = [t for t in strat.trades if t.order.filled_size >= 1]

    enriched: List[dict] = []
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
        enriched.append({
            "window_ts": window_ts,
            "entry": t.order.fill_avg_price,
            "shares": t.order.filled_size,
            "won": won,
            "pnl": pnl_ps * t.order.filled_size,
            "direction": t.direction,
            "regime": regime_by_window.get(window_ts, "INSUFFICIENT"),
        })
    enriched.sort(key=lambda t: t["window_ts"])
    print(f"Filled trades: {len(enriched)}", flush=True)
    print(flush=True)

    # ── Section A: baseline ──
    print(f"=== A. BASELINE (no filters) ===", flush=True)
    print(fmt_block("All trades", enriched, days), flush=True)
    print(flush=True)

    # ── Section B: breaker simulation ──
    print(f"=== B. CIRCUIT BREAKER SIMULATION ===", flush=True)
    breaker = RollingWRBreaker(
        window_n=args.cb_window_n,
        min_wr=args.cb_min_wr,
        pause_hours=args.cb_pause_hours,
    )
    breaker_kept: List[dict] = []
    breaker_skipped: List[dict] = []
    for t in enriched:
        ts_s = float(t["window_ts"])
        if breaker.should_skip(ts_s):
            breaker_skipped.append(t)
            continue
        breaker_kept.append(t)
        breaker.record_outcome(won=t["won"], ts_s=ts_s)
    print(fmt_block("Kept (breaker on)", breaker_kept, days), flush=True)
    print(fmt_block("Skipped by breaker", breaker_skipped, days), flush=True)
    print(f"Breaker engagements: {len(breaker.engagement_log)}", flush=True)
    print(flush=True)

    # ── Section C: regime filter ──
    print(f"=== C. REGIME FILTER (RANGING-only) ===", flush=True)
    ranging_only = [t for t in enriched if t["regime"] == "RANGING"]
    n_ranging_windows = regime_counts.get("RANGING", 1)
    ranging_days = n_ranging_windows * 5 / 60 / 24
    print(fmt_block("RANGING-only trades", ranging_only, ranging_days), flush=True)
    print(flush=True)

    # ── Section D: both ──
    print(f"=== D. BOTH (regime filter + breaker) ===", flush=True)
    breaker2 = RollingWRBreaker(
        window_n=args.cb_window_n,
        min_wr=args.cb_min_wr,
        pause_hours=args.cb_pause_hours,
    )
    both_kept: List[dict] = []
    both_skipped: List[dict] = []
    for t in enriched:
        if t["regime"] != "RANGING":
            both_skipped.append(t)
            continue
        ts_s = float(t["window_ts"])
        if breaker2.should_skip(ts_s):
            both_skipped.append(t)
            continue
        both_kept.append(t)
        breaker2.record_outcome(won=t["won"], ts_s=ts_s)
    print(fmt_block("Kept (regime + breaker)", both_kept, ranging_days), flush=True)
    print(fmt_block("Total skipped", both_skipped, days), flush=True)
    print(flush=True)

    # ── Section E: PM lag distribution ──
    print(f"=== E. PM LAG distribution by regime (CB move >= $1) ===", flush=True)
    print(f"  Per-regime: median lag (lower = faster PM reaction = less edge)", flush=True)
    for regime in ["MOMENTUM", "EXHAUSTION", "RANGING"]:
        rl = pm_lag_by_regime.get(regime, [])
        if not rl:
            continue
        lags = [r["lag_ms"] for r in rl]
        aligned_n = sum(1 for r in rl if r["aligned"])
        print(f"  {regime:<11} n={len(rl):>4}  "
              f"p10={pct(lags,0.10):>4}ms  p50={pct(lags,0.50):>4}ms  "
              f"p90={pct(lags,0.90):>4}ms  aligned={100*aligned_n/len(rl):.0f}%",
              flush=True)
    print(flush=True)

    # ── Section F: per-regime trade results ──
    print(f"=== F. PER-REGIME TRADE BREAKDOWN ===", flush=True)
    print(f"{'Regime':<14} {'n':>4} {'W':>3} {'L':>3} {'WR':>5} {'CI':>16} "
          f"{'total':>10} {'$/day':>10}", flush=True)
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
        n_w = regime_counts.get(regime, 1)
        regime_days = n_w * 5 / 60 / 24
        per_day_r = sum(rp) / max(0.01, regime_days)
        ci_str = f"[{100*rlo:.0f}-{100*rhi:.0f}%]"
        print(f"{regime:<14} {rn:>4} {rw:>3} {rn-rw:>3} {100*rw/rn:>4.0f}% "
              f"{ci_str:>16} ${sum(rp):>+8.2f}  ${per_day_r:>+8.2f}",
              flush=True)
    print(flush=True)

    # ── Summary table ──
    print(f"=== SUMMARY: which guard rail wins? ===", flush=True)
    print(f"{'Config':<30} {'n':>3} {'WR':>5} {'$/day':>10}", flush=True)

    def row(label, ts_, d):
        if not ts_:
            print(f"{label:<30} n=0", flush=True)
            return
        w = sum(1 for t in ts_ if t["won"])
        p = sum(t["pnl"] for t in ts_)
        print(f"{label:<30} {len(ts_):>3} {100*w/len(ts_):>4.0f}% "
              f"${p/max(0.01,d):>+8.2f}", flush=True)

    row("Baseline (all)",    enriched,     days)
    row("Breaker only",      breaker_kept, days)
    row("Regime only (RANGING)", ranging_only, ranging_days)
    row("Both (regime+breaker)", both_kept, ranging_days)
    print(flush=True)
    print(f"NOTE: $/day for regime-filtered rows uses the days SPENT in that "
          f"regime, so it's not strictly comparable to baseline $/day. The "
          f"WR comparison is the cleaner signal.", flush=True)


if __name__ == "__main__":
    main()
