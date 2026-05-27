"""
S7 latency sensitivity test: run the same full validation at multiple
taker_latency_ms values to see how WR / fills / $/day change.

Why: live observations show latency varies from ~460ms (typical) to ~5000ms
(slow), with one 73s outlier. The simulator uses 150ms by default. If results
are insensitive to latency, the model is fine; if highly sensitive, we need
better modeling.
"""
import argparse
import math
from pathlib import Path
from statistics import mean

from order_engine import PaperOrderEngine
from strategies import S7FadeStrategy, StrategyRunner


def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def run_at_latency(files, latency_ms):
    engine = PaperOrderEngine(starting_usdc=10000, taker_latency_ms=latency_ms,
                              use_trade_events=False, min_notional_usdc=1.0)
    strat = S7FadeStrategy(engine, shares=6, min_notional_target=1.20)
    runner = StrategyRunner(engine)
    runner.add_strategy(strat)
    runner.run_tick_files(files)
    trades = [t for t in strat.trades if t.order.filled_size >= 1]
    if not trades:
        return None
    pnls_ps = [t.pnl_per_share for t in trades]
    wins = sum(1 for p in pnls_ps if p > 0.001)
    losses = sum(1 for p in pnls_ps if p < -0.001)
    n = wins + losses
    wlo, whi = wilson_ci(wins, n)
    days = len(files) * 5 / 60 / 24
    total = sum(t.pnl for t in trades)
    return {
        "attempts": engine.taker_attempts,
        "fills": len(trades),
        "stale_ask_rejects": engine.taker_rejections,
        "wins": wins,
        "losses": losses,
        "wr": wins / max(1, n) * 100,
        "ci_lo": wlo * 100,
        "ci_hi": whi * 100,
        "total": total,
        "per_day": total / max(0.01, days),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--latencies", default="150,500,1000,2500,5000")
    args = ap.parse_args()

    import sys
    sys.stdout.reconfigure(line_buffering=True)
    files = sorted(Path(args.ticks).glob("ticks_*.csv*"))
    days = len(files) * 5 / 60 / 24
    print(f"S7 latency sensitivity sweep over {len(files)} windows ({days:.1f} days)", flush=True)
    print(flush=True)
    latencies = [int(x) for x in args.latencies.split(",")]
    print(f"{'Latency':>9} {'attempts':>9} {'fills':>6} {'rejects':>8} "
          f"{'WR':>5} {'95% CI':>15} {'Total':>9} {'$/day':>9}", flush=True)
    print("-" * 85, flush=True)
    for ms in latencies:
        print(f"  running latency={ms}ms ...", flush=True)
        r = run_at_latency(files, ms)
        if r is None:
            print(f"{ms:>7}ms  no trades", flush=True)
            continue
        print(f"{ms:>7}ms {r['attempts']:>9} {r['fills']:>6} "
              f"{r['stale_ask_rejects']:>8} "
              f"{r['wr']:>4.0f}% [{r['ci_lo']:>3.0f}% — {r['ci_hi']:>3.0f}%]   "
              f"${r['total']:>+7.2f} ${r['per_day']:>+7.2f}", flush=True)


if __name__ == "__main__":
    main()
