"""
For cheap fades — is there a "death zone" entry price below which the fade
NEVER recovers (always resolves to $0)? Or is there always some chance of
reversion?

Uses fine-grained bucketing (every $0.01) to find the floor.
"""
import json
from pathlib import Path
from statistics import mean
from collections import defaultdict
import math

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


def main():
    tick_dir = Path("/home/ubuntu/reports/ticks")
    with open("/home/ubuntu/reports/resolutions.json") as f:
        resolutions = {int(k): v for k, v in json.load(f).items()}
    files = sorted(tick_dir.glob("ticks_*.csv"))
    print(f"Cheap-fade floor analysis on {len(files)} windows")

    # Run a permissive backtest (no cap) so we capture every cheap fade
    engine = PaperOrderEngine(starting_usdc=100000, taker_latency_ms=150,
                              min_notional_usdc=0)  # no min notional — see ALL cheap fades
    strat = S7FadeStrategy(engine, shares=6, min_notional_target=0)  # no scaling — see raw signals
    runner = StrategyRunner(engine)
    runner.add_strategy(strat)
    runner.run_tick_files(files)

    # Collect every trade with real resolution
    trades_data = []
    for t in strat.trades:
        if t.order.filled_size < 1:
            continue
        try:
            window_ts = int(t.order.token_id.split("_")[1])
        except Exception:
            continue
        res = resolutions.get(window_ts)
        if not res or not res.get("resolved"):
            continue
        real_winner = "UP" if res.get("up_winning") else "DN"
        won = (t.direction == real_winner)
        trades_data.append({
            "entry": t.order.fill_avg_price,
            "won": won,
        })

    print(f"Total trades captured: {len(trades_data)}")
    print()

    # Bucket by entry price ($0.05 increments — finer for cheap entries)
    buckets = defaultdict(list)
    for t in trades_data:
        # Use $0.05 buckets for cheap, $0.10 for higher
        if t["entry"] <= 0.30:
            b_lo = math.floor(t["entry"] / 0.05) * 0.05
            b_hi = b_lo + 0.05
        else:
            b_lo = math.floor(t["entry"] / 0.10) * 0.10
            b_hi = b_lo + 0.10
        buckets[(round(b_lo, 2), round(b_hi, 2))].append(t)

    print(f"{'Bucket':<14} {'n':>4} {'W':>3} {'L':>3} {'WR':>5} {'95% CI':>17}")
    print("-" * 50)
    for (lo, hi) in sorted(buckets.keys()):
        bucket = buckets[(lo, hi)]
        wins = sum(1 for t in bucket if t["won"])
        n = len(bucket)
        wlo, whi = wilson_ci(wins, n)
        wr = wins / max(1, n) * 100
        marker = " ⚠️ death zone?" if wr < 25 and n >= 3 else ""
        print(f"${lo:.2f}-${hi:.2f}  {n:>4} {wins:>3} {n-wins:>3} "
              f"{wr:>4.0f}% [{wlo*100:>3.0f}% — {whi*100:>3.0f}%]{marker}")


if __name__ == "__main__":
    main()
