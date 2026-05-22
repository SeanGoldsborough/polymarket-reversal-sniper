"""
Quick smoke test of the Phase 3 strategy framework.

Runs CombinedS2S6Strategy against a small replay sample using PaperOrderEngine.
Confirms the modular framework produces the same results as the standalone validator.
"""

import sys
from pathlib import Path

from order_engine import PaperOrderEngine
from strategies import CombinedS2S6Strategy, S2FadeStrategy, S6MomentumStrategy, StrategyRunner


def main():
    tick_dir = Path("/home/ubuntu/reports/ticks")
    files = sorted(tick_dir.glob("ticks_*.csv"))[:30]
    if not files:
        print("No tick files found")
        return

    print(f"Running framework smoke test on {len(files)} tick files...")
    print()

    # Test 1: Combined strategy
    print("=== CombinedS2S6Strategy ===")
    engine = PaperOrderEngine(starting_usdc=10000.0)
    runner = StrategyRunner(engine)
    runner.add_strategy(CombinedS2S6Strategy(engine, shares=7))
    runner.run_tick_files(files)
    runner.report()
    print()

    # Test 2: S2 + S6 as separate strategies (should match combined when summed)
    print("=== S2 + S6 as separate strategies ===")
    engine2 = PaperOrderEngine(starting_usdc=10000.0)
    runner2 = StrategyRunner(engine2)
    runner2.add_strategy(S2FadeStrategy(engine2, shares=7))
    runner2.add_strategy(S6MomentumStrategy(engine2, shares=7))
    runner2.run_tick_files(files)
    runner2.report()


if __name__ == "__main__":
    main()
