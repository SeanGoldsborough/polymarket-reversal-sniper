"""
Filter S7 historical trades to fade entry <= a configurable cap, and report
strategy economics under the SURVIVING trade set. The cap is meant to drop
trades that would be EV-negative at the CI lower bound (54%).

Default cap = $0.45 (the break-even fade at 54% WR with corrected fees).
"""
import argparse
import json
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


def crypto_fee(p, sh):
    return 0.07 * p * (1 - p) * sh


def dynamic_shares(limit, base=6, target=1.20):
    return max(base, math.ceil(target / limit))


def ev_at(fade_entry, wr):
    limit = min(fade_entry + 0.05, 0.98)
    sh = dynamic_shares(limit)
    fee = crypto_fee(limit, sh)
    win = sh * 1.00 - sh * limit - fee
    loss = -(sh * limit + fee)
    return wr * win + (1 - wr) * loss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=float, default=0.45,
                    help="Drop trades with fade entry > cap")
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--resolutions", default="/home/ubuntu/reports/resolutions.json")
    args = ap.parse_args()

    with open(args.resolutions) as f:
        resolutions = {int(k): v for k, v in json.load(f).items()}

    files = sorted(Path(args.ticks).glob("ticks_*.csv"))
    days = len(files) * 5 / 60 / 24
    print(f"Capping fade entries to <= ${args.cap:.2f} over {len(files)} windows ({days:.1f} days)")

    engine = PaperOrderEngine(starting_usdc=10000, taker_latency_ms=150,
                              min_notional_usdc=1.0)
    strat = S7FadeStrategy(engine, shares=6, min_notional_target=1.20)
    runner = StrategyRunner(engine)
    runner.add_strategy(strat)
    runner.run_tick_files(files)
    trades = [t for t in strat.trades if t.order.filled_size >= 1]

    # Apply real resolutions + cap filter
    kept = []
    dropped = []
    for t in trades:
        try:
            window_ts = int(t.order.token_id.split("_")[1])
        except Exception:
            continue
        res = resolutions.get(window_ts)
        if not res or not res.get("resolved"):
            continue
        real_winner = "UP" if res.get("up_winning") else "DN"
        won = (t.direction == real_winner)
        fee_ps = (t.order.total_fees / t.order.filled_size) if t.order.filled_size > 0 else 0
        pnl_ps = (1.00 if won else 0.00) - t.order.fill_avg_price - fee_ps
        pnl_total = pnl_ps * t.order.filled_size
        trade_data = {
            "entry": t.order.fill_avg_price,
            "shares": t.order.filled_size,
            "won": won,
            "pnl_total": pnl_total,
        }
        # Cap is on the FADE entry — i.e., the recorded ask, which is fill_avg_price - $0.05
        # (because limit = ask + $0.05, and fill happens AT the limit when no improvement).
        # Actually the cleanest cap is on the FILL price (what we actually paid).
        # If we wanted "fade entry ≤ $0.45", that means ask ≤ $0.45 → fill ≤ $0.50.
        # I'll cap on FILL price to match how the bot would screen signals at decision time.
        if t.order.fill_avg_price <= args.cap + 0.05:
            kept.append(trade_data)
        else:
            dropped.append(trade_data)

    def report(label, traces):
        if not traces:
            print(f"\n{label}: 0 trades")
            return
        wins = sum(1 for t in traces if t["won"])
        losses = len(traces) - wins
        wr = wins / max(1, len(traces))
        wlo, whi = wilson_ci(wins, len(traces))
        total = sum(t["pnl_total"] for t in traces)
        avg_entry = mean(t["entry"] for t in traces)
        avg_sh = mean(t["shares"] for t in traces)
        per_day = total / max(0.01, days)
        # EV at the CI lower bound — does this set survive at 54% WR?
        ev_lo = sum(ev_at(t["entry"] - 0.05, 0.54) for t in traces)
        ev_lo_per_day = ev_lo / max(0.01, days)
        print(f"\n=== {label} (n={len(traces)}, {len(traces)/days:.1f}/day) ===")
        print(f"  W={wins} L={losses}  WR={wr*100:.0f}%  95% CI [{wlo*100:.0f}% — {whi*100:.0f}%]")
        print(f"  Avg entry: ${avg_entry:.2f}  Avg shares: {avg_sh:.1f}")
        print(f"  Total $: ${total:+.2f}  $/day: ${per_day:+.2f}")
        print(f"  EV @ 54% WR (CI lower): ${ev_lo_per_day:+.2f}/day  ← does it survive worst case?")

    print()
    report(f"NO CAP (baseline)", kept + dropped)
    report(f"WITH CAP <= ${args.cap:.2f} fade (≤ ${args.cap + 0.05:.2f} fill)", kept)
    report(f"DROPPED (fade > ${args.cap:.2f})", dropped)


if __name__ == "__main__":
    main()
