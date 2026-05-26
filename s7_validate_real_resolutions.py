"""
S7 validation using REAL Polymarket market resolutions (from cache) instead of
the final-bid heuristic. Strategy is single-shot per window (matches the live bot).

This produces the most honest strategy-level WR we can extract from our data.
Reports both:
  - Resolution agreement: did our final-bid heuristic match the real outcome?
  - Updated WR using real outcomes (reclassifies any mis-classified trades)
"""
import argparse
import json
import math
from pathlib import Path
from statistics import mean

from order_engine import PaperOrderEngine
from strategies import S7FadeStrategy, StrategyRunner


def wilson_ci(wins: int, n: int, z: float = 1.96):
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
    ap.add_argument("--resolutions", default="/home/ubuntu/reports/resolutions.json")
    ap.add_argument("--max-files", type=int, default=None)
    args = ap.parse_args()

    with open(args.resolutions) as f:
        resolutions = {int(k): v for k, v in json.load(f).items()}

    files = sorted(Path(args.ticks).glob("ticks_*.csv"))
    if args.max_files:
        files = files[:args.max_files]
    print(f"S7 validation w/ REAL Polymarket resolutions on {len(files)} windows")

    engine = PaperOrderEngine(starting_usdc=10000, taker_latency_ms=150,
                              use_trade_events=False, min_notional_usdc=1.0)
    strat = S7FadeStrategy(engine, shares=6, min_notional_target=1.20)
    runner = StrategyRunner(engine)
    runner.add_strategy(strat)
    runner.run_tick_files(files)
    trades = strat.trades

    # Recompute outcomes using REAL resolutions
    heur_wins = heur_losses = 0
    real_wins = real_losses = 0
    skipped_unresolved = 0
    flips_heur_to_real = []  # cases where heuristic was wrong

    pnls_real = []
    pnls_heur = []
    for t in trades:
        if t.order.filled_size < 1:
            continue
        # Heuristic outcome (what s7_full_validation reported)
        heur_pnl = t.pnl_per_share
        pnls_heur.append(heur_pnl)
        if heur_pnl > 0.001:
            heur_wins += 1
        elif heur_pnl < -0.001:
            heur_losses += 1

        # Real outcome — look up the window
        window_ts = None
        try:
            # token_id was set as `{f.stem}_UP|DN` in the runner's default market_lookup
            stem = t.order.token_id.split("_")[1]  # ticks_<ts>_UP → <ts>
            window_ts = int(stem)
        except Exception:
            skipped_unresolved += 1
            pnls_real.append(heur_pnl)
            continue
        res = resolutions.get(window_ts)
        if not res or not res.get("resolved"):
            skipped_unresolved += 1
            pnls_real.append(heur_pnl)
            continue
        # Did we win? We bought t.direction (UP/DN). Real winner is UP if up_winning else DN.
        real_winner = "UP" if res.get("up_winning") else "DN"
        if t.direction == real_winner:
            redemption = 1.00
        else:
            redemption = 0.00
        fee_ps = (t.order.total_fees / t.order.filled_size) if t.order.filled_size > 0 else 0
        real_pnl_ps = redemption - t.order.fill_avg_price - fee_ps
        pnls_real.append(real_pnl_ps)
        if real_pnl_ps > 0.001:
            real_wins += 1
        elif real_pnl_ps < -0.001:
            real_losses += 1
        # Flag flips
        heur_won = heur_pnl > 0.001
        real_won = real_pnl_ps > 0.001
        if heur_won != real_won:
            flips_heur_to_real.append({
                "tid": t.token_id, "dir": t.direction,
                "heur_pnl_ps": heur_pnl, "real_pnl_ps": real_pnl_ps,
                "real_winner": real_winner,
            })

    n = len(pnls_real)
    heur_n = heur_wins + heur_losses
    real_n = real_wins + real_losses

    heur_wr = heur_wins / max(1, heur_n)
    real_wr = real_wins / max(1, real_n)
    hlo, hhi = wilson_ci(heur_wins, heur_n)
    rlo, rhi = wilson_ci(real_wins, real_n)

    days = len(files) * 5 / 60 / 24
    total_real_dollars = sum(p * t.order.filled_size
                             for p, t in zip(pnls_real,
                                              [t for t in trades if t.order.filled_size >= 1]))
    total_heur_dollars = sum(p * t.order.filled_size
                             for p, t in zip(pnls_heur,
                                              [t for t in trades if t.order.filled_size >= 1]))

    print()
    print(f"=== S7 STRATEGY-LEVEL (single-shot) — n={n} fills ===")
    print(f"Skipped (no resolution): {skipped_unresolved}")
    print()
    print(f"{'':<10} {'W':>4} {'L':>4} {'WR':>5} {'95% CI':>15} {'Total $':>10} {'$/day':>9}")
    print("-" * 70)
    print(f"{'Heuristic':<10} {heur_wins:>4} {heur_losses:>4} {heur_wr*100:>4.0f}% "
          f"[{hlo*100:>3.0f}% — {hhi*100:>3.0f}%]   "
          f"${total_heur_dollars:>+8.2f}  ${total_heur_dollars/max(0.01,days):>+7.2f}")
    print(f"{'Real':<10} {real_wins:>4} {real_losses:>4} {real_wr*100:>4.0f}% "
          f"[{rlo*100:>3.0f}% — {rhi*100:>3.0f}%]   "
          f"${total_real_dollars:>+8.2f}  ${total_real_dollars/max(0.01,days):>+7.2f}")
    print()
    print(f"Heuristic was WRONG on {len(flips_heur_to_real)} of {n} trades "
          f"({100*len(flips_heur_to_real)/max(1,n):.0f}%)")
    if flips_heur_to_real:
        print("Sample flips (heuristic→real outcome differs):")
        for fl in flips_heur_to_real[:10]:
            print(f"  {fl['dir']} → real winner {fl['real_winner']}: "
                  f"heur_pnl_ps=${fl['heur_pnl_ps']:+.3f} real=${fl['real_pnl_ps']:+.3f}")


if __name__ == "__main__":
    main()
