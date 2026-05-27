"""
Full S7 validation across ALL available tick windows (not just trade-CSV subset).
Runs S7FadeStrategy through StrategyRunner with use_trade_events=False (so it
works on every window — trade-event maker queue isn't relevant for S7's taker
execution). Reports binomial Wilson CI on the win rate.
"""
import argparse
import math
from pathlib import Path
from statistics import mean

from order_engine import PaperOrderEngine
from strategies import S7FadeStrategy, StrategyRunner


def wilson_ci(wins: int, n: int, z: float = 1.96):
    """95% Wilson score interval (more accurate than normal-approx for small n)."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--max-files", type=int, default=None)
    args = ap.parse_args()

    files = sorted(Path(args.ticks).glob("ticks_*.csv*"))
    if args.max_files:
        files = files[:args.max_files]
    print(f"S7 full validation: {len(files)} tick windows")

    engine = PaperOrderEngine(starting_usdc=10000, taker_latency_ms=150,
                              use_trade_events=False)
    strat = S7FadeStrategy(engine, shares=7)
    runner = StrategyRunner(engine)
    runner.add_strategy(strat)
    runner.run_tick_files(files)

    trades = strat.trades
    if not trades:
        print("No trades. S7 fired zero signals.")
        return

    pnls_ps = [t.pnl_per_share for t in trades]
    wins = sum(1 for p in pnls_ps if p > 0.001)
    losses = sum(1 for p in pnls_ps if p < -0.001)
    n_resolved = wins + losses
    wr = wins / max(1, n_resolved)
    wlo, whi = wilson_ci(wins, n_resolved)
    avg_entry = mean(t.order.fill_avg_price for t in trades)
    be_wr = avg_entry  # rough — break-even = entry price (ignores fee)
    margin = wr - be_wr
    days = len(files) * 5 / 60 / 24
    total = sum(t.pnl for t in trades)

    sizes = [t.order.filled_size for t in trades]
    notionals = [t.order.filled_size * t.order.fill_avg_price for t in trades]
    print()
    print(f"=== S7 RESULTS ({len(files)} windows = {days:.1f} days) ===")
    print(f"Engine taker: attempts={engine.taker_attempts} "
          f"stale-ask-rejects={engine.taker_rejections} "
          f"min-notional-rejects={getattr(engine, 'min_notional_rejections', 0)}")
    print(f"Trades: n={len(trades)} W={wins} L={losses}")
    print(f"WR={wr * 100:.0f}%   95% CI [{wlo * 100:.0f}% - {whi * 100:.0f}%]")
    print(f"BE WR={be_wr * 100:.0f}%   margin={margin * 100:+.1f}pp")
    print(f"Avg entry: ${avg_entry:.3f}")
    print(f"Share size: min={min(sizes):.0f} max={max(sizes):.0f} mean={mean(sizes):.1f}")
    print(f"Notional/trade: min=${min(notionals):.2f} max=${max(notionals):.2f} mean=${mean(notionals):.2f}")
    print(f"Avg PnL/share: ${mean(pnls_ps):+.4f}")
    print(f"Total PnL: ${total:+.2f}")
    print(f"Per-day: ${total / max(0.01, days):+.2f}")

    # Edge over break-even: is the 95% CI lower bound > BE WR?
    if wlo > be_wr:
        print()
        print(f"EDGE CONFIRMED: lower 95% CI bound ({wlo * 100:.0f}%) > BE WR ({be_wr * 100:.0f}%)")
    else:
        print()
        print(f"WARNING: lower 95% CI bound ({wlo * 100:.0f}%) <= BE WR ({be_wr * 100:.0f}%) — "
              f"more data needed to be confident in edge")


if __name__ == "__main__":
    main()
