"""
Validate that S7FadeStrategy (through the Strategy framework) reproduces the
direct-validator numbers from validate_phase35.py. This proves Phase 3 is wired
correctly end-to-end.
"""
import argparse
from pathlib import Path
from statistics import mean

from order_engine import PaperOrderEngine
from strategies import S7FadeStrategy, StrategyRunner


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--trades", default="/home/ubuntu/reports/trades")
    ap.add_argument("--max-files", type=int, default=76)
    ap.add_argument("--use-trade-events", action="store_true",
                    help="Drive maker fills via trade events (Phase 3.5)")
    args = ap.parse_args()

    tick_dir = Path(args.ticks)
    trade_dir = Path(args.trades)
    # Only validate windows that have BOTH tick + trade CSVs (matches phase35 set)
    files = sorted(tick_dir.glob("ticks_*.csv"))
    files = [f for f in files
             if (trade_dir / f"trades_{int(f.stem.split('_')[1])}.csv").exists()]
    files = files[:args.max_files]
    print(f"Validating S7FadeStrategy on {len(files)} windows (max-files={args.max_files})")

    engine = PaperOrderEngine(
        starting_usdc=10000,
        taker_latency_ms=150,
        use_trade_events=args.use_trade_events,
    )
    strat = S7FadeStrategy(engine, shares=7)
    runner = StrategyRunner(engine)
    runner.add_strategy(strat)

    runner.run_tick_files(files, trades_dir=trade_dir if args.use_trade_events else None)

    print()
    print(f"Engine taker attempts={engine.taker_attempts} rejections={engine.taker_rejections}")
    runner.report()

    trades = strat.trades
    if trades:
        pnls_ps = [t.pnl_per_share for t in trades]
        wins = sum(1 for p in pnls_ps if p > 0.001)
        losses = sum(1 for p in pnls_ps if p < -0.001)
        wr = 100 * wins / max(1, wins + losses)
        days = len(files) * 5 / 60 / 24
        total = sum(t.pnl for t in trades)
        print(f"Detail: n={len(trades)} W={wins} L={losses} WR={wr:.0f}% "
              f"avg/share=${mean(pnls_ps):+.4f} total=${total:+.2f} "
              f"/day=${total / max(0.01, days):+.2f}")


if __name__ == "__main__":
    main()
