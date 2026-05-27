"""
Conditional WR by fade entry price bucket — is the 70% WR uniform, or does it
vary by entry price? This determines whether S7 is truly profitable.
"""
import json
from pathlib import Path
from statistics import mean
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
    resolutions_path = "/home/ubuntu/reports/resolutions.json"
    with open(resolutions_path) as f:
        resolutions = {int(k): v for k, v in json.load(f).items()}
    files = sorted(tick_dir.glob("ticks_*.csv*"))
    print(f"WR by fade-entry bucket — {len(files)} windows")

    engine = PaperOrderEngine(starting_usdc=10000, taker_latency_ms=150,
                              min_notional_usdc=1.0)
    strat = S7FadeStrategy(engine, shares=6, min_notional_target=1.20)
    runner = StrategyRunner(engine)
    runner.add_strategy(strat)
    runner.run_tick_files(files)
    trades = [t for t in strat.trades if t.order.filled_size >= 1]

    # Apply real resolutions
    enriched = []
    for t in trades:
        try:
            stem = t.order.token_id.split("_")[1]
            window_ts = int(stem)
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
            "entry": t.order.fill_avg_price,
            "shares": t.order.filled_size,
            "won": won,
            "pnl_ps": pnl_ps,
            "pnl_total": pnl_ps * t.order.filled_size,
        })

    # Bucket by fade entry
    buckets = [
        ("$0.05-$0.20", 0.05, 0.20),
        ("$0.20-$0.35", 0.20, 0.35),
        ("$0.35-$0.50", 0.35, 0.50),
        ("$0.50-$0.65", 0.50, 0.65),
        ("$0.65-$0.80", 0.65, 0.80),
        ("$0.80-$0.95", 0.80, 0.95),
    ]
    print()
    print(f"{'Bucket':<14} {'n':>4} {'W':>3} {'L':>3} {'WR':>5} {'95% CI':>15} "
          f"{'avg sh':>7} {'$/trade':>9} {'theory@70%':>12}")
    print("-" * 95)
    overall_w = overall_n = 0
    overall_total = 0.0
    for label, lo, hi in buckets:
        in_bucket = [e for e in enriched if lo <= e["entry"] < hi]
        if not in_bucket:
            print(f"{label:<14} {0:>4} {'-':>3} {'-':>3} {'-':>5}")
            continue
        wins = sum(1 for e in in_bucket if e["won"])
        losses = len(in_bucket) - wins
        wr = wins / max(1, len(in_bucket))
        wlo, whi = wilson_ci(wins, len(in_bucket))
        avg_sh = mean(e["shares"] for e in in_bucket)
        avg_pnl = mean(e["pnl_total"] for e in in_bucket)
        # Theoretical EV at 70% WR with avg entry of bucket
        avg_entry = mean(e["entry"] for e in in_bucket)
        theory_ev = avg_sh * (0.65 - avg_entry)
        overall_w += wins
        overall_n += len(in_bucket)
        overall_total += sum(e["pnl_total"] for e in in_bucket)
        print(f"{label:<14} {len(in_bucket):>4} {wins:>3} {losses:>3} "
              f"{wr*100:>4.0f}% [{wlo*100:>3.0f}% — {whi*100:>3.0f}%]   "
              f"{avg_sh:>6.1f} ${avg_pnl:>+7.2f} ${theory_ev:>+10.2f}")
    print("-" * 95)
    if overall_n:
        owr = overall_w / overall_n
        olo, ohi = wilson_ci(overall_w, overall_n)
        print(f"{'OVERALL':<14} {overall_n:>4} {overall_w:>3} {overall_n - overall_w:>3} "
              f"{owr*100:>4.0f}% [{olo*100:>3.0f}% — {ohi*100:>3.0f}%]   "
              f"{'':>6} ${overall_total:>+7.2f}")


if __name__ == "__main__":
    main()
