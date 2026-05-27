"""
Compare three scenarios on the full tick history:
  A: no min-notional check (historical baseline, optimistic)
  B: min-notional ON, dynamic-shares OFF (Option D — measures what live looks like without fix)
  C: min-notional ON, dynamic-shares ON (Option B — the fix)

Shows how many signals each option captures and the $/day under each.
"""
import argparse
from pathlib import Path
from statistics import mean

from order_engine import PaperOrderEngine
from strategies import S7FadeStrategy, StrategyRunner


def run(label, min_notional_usdc, min_notional_target, files):
    engine = PaperOrderEngine(starting_usdc=10000, taker_latency_ms=150,
                              use_trade_events=False,
                              min_notional_usdc=min_notional_usdc)
    strat = S7FadeStrategy(engine, shares=6,
                           min_notional_target=min_notional_target)
    runner = StrategyRunner(engine)
    runner.add_strategy(strat)
    runner.run_tick_files(files)

    trades = strat.trades
    fills = [t for t in trades if t.order.filled_size > 0]
    days = len(files) * 5 / 60 / 24
    if not fills:
        return label, 0, 0, 0, 0, 0, 0, 0
    pnls_ps = [t.pnl_per_share for t in fills]
    wins = sum(1 for p in pnls_ps if p > 0.001)
    losses = sum(1 for p in pnls_ps if p < -0.001)
    wr = 100 * wins / max(1, wins + losses)
    total = sum(t.pnl for t in fills)
    return (label, len(trades), len(fills),
            engine.min_notional_rejections, engine.taker_rejections,
            wr, total, total / max(0.01, days))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--max-files", type=int, default=None)
    args = ap.parse_args()
    files = sorted(Path(args.ticks).glob("ticks_*.csv*"))
    if args.max_files:
        files = files[:args.max_files]
    print(f"Comparing on {len(files)} windows")
    print()
    print(f"{'Scenario':<55} {'sig':>4} {'fld':>4} {'$<1 rej':>8} "
          f"{'stale rej':>10} {'WR':>5} {'Total':>8} {'$/day':>8}")
    print("-" * 110)
    for label, mn_usdc, mn_target in [
        ("A: no min-notional (historical baseline, optimistic)", 0.0, 0.0),
        ("B: min-notional ON, NO dynamic shares (what live did)", 1.0, 0.0),
        ("C: min-notional ON, DYNAMIC shares (Option B fix)", 1.0, 1.20),
    ]:
        lbl, sig, fld, nrej, srej, wr, total, perday = run(label, mn_usdc, mn_target, files)
        print(f"{lbl:<55} {sig:>4} {fld:>4} {nrej:>8} {srej:>10} "
              f"{wr:>4.0f}% ${total:>+6.2f} ${perday:>+6.2f}")


if __name__ == "__main__":
    main()
