"""
S7 P/L projections with the corrected fee formula (0.07 × p × (1-p)).

Three views:
  (1) Per-trade EV under sim WR + Option B dynamic shares
  (2) Daily $ projection across the CI range (54% — 83%)
  (3) Monte Carlo wallet trajectory over various horizons (bust probability)
"""
import random
import math
from statistics import mean

# ────────────────────────────────────────────────────────────────────────────
# Per-trade EV table — fade entry × WR
# ────────────────────────────────────────────────────────────────────────────

def crypto_fee(p, sh):
    return 0.07 * p * (1 - p) * sh


def dynamic_shares(limit, base=6, target=1.20):
    return max(base, math.ceil(target / limit))


def ev_per_trade(fade_entry, wr):
    """EV given a fade entry price and WR."""
    limit = min(fade_entry + 0.05, 0.98)
    shares = dynamic_shares(limit)
    fee = crypto_fee(limit, shares)
    win_payout = shares * 1.00 - shares * limit - fee
    loss_cost = -(shares * limit + fee)
    return wr * win_payout + (1 - wr) * loss_cost, shares, fee


def per_trade_table():
    print("=" * 80)
    print("(1) Per-trade EV by fade entry × WR (with correct fees + Option B sizing)")
    print("=" * 80)
    fades = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]
    wrs = [0.54, 0.68, 0.70, 0.76, 0.83]
    header = f"{'Fade $':>7} {'Sh':>4} {'Fee':>7} " + "".join(f"{f'@{int(w*100)}%':>9}" for w in wrs)
    print(header)
    print("-" * len(header))
    for fp in fades:
        _, sh, fee = ev_per_trade(fp, 0.70)
        row = f"{fp:>7.2f} {sh:>4} ${fee:>5.3f}"
        for wr in wrs:
            ev, _, _ = ev_per_trade(fp, wr)
            row += f" ${ev:>+7.2f}"
        print(row)


# ────────────────────────────────────────────────────────────────────────────
# Daily $ projection across CI
# ────────────────────────────────────────────────────────────────────────────

# From the 34-trade single-shot sim (corrected fees), entry distribution:
# bucket | n | avg_entry approx
ENTRY_DIST = [
    (0.125, 7),  # $0.05-$0.20
    (0.275, 1),  # $0.20-$0.35
    (0.425, 3),  # $0.35-$0.50
    (0.575, 11), # $0.50-$0.65
    (0.725, 4),  # $0.65-$0.80
    (0.875, 8),  # $0.80-$0.95
]
SAMPLE_DAYS = 1.25  # 365 windows / 288 windows-per-day


def sim_day_pnl(wr):
    """Expected $/day at a given uniform WR, weighted by entry distribution."""
    total = 0.0
    n_total = sum(c for _, c in ENTRY_DIST)
    for entry, count in ENTRY_DIST:
        ev, _, _ = ev_per_trade(entry, wr)
        total += ev * count
    trades_per_day = n_total / SAMPLE_DAYS
    return total / SAMPLE_DAYS, trades_per_day


def daily_projection():
    print()
    print("=" * 80)
    print(f"(2) Daily $ projection ({sum(c for _,c in ENTRY_DIST)/SAMPLE_DAYS:.1f} trades/day at historical rate)")
    print("=" * 80)
    print(f"{'Assumed WR':>12} {'$/day':>10} {'$/week':>10} {'$/month':>11}")
    print("-" * 50)
    for wr in (0.83, 0.76, 0.70, 0.68, 0.65, 0.60, 0.54, 0.50):
        daily, _ = sim_day_pnl(wr)
        label = f"{wr*100:.0f}%"
        if wr == 0.83:
            label += " (CI upper)"
        elif wr == 0.70:
            label += " (sim point)"
        elif wr == 0.68:
            label += " (real-res)"
        elif wr == 0.54:
            label += " (CI lower)"
        elif wr == 0.50:
            label += " (BE)"
        print(f"{label:>12} ${daily:>+8.2f} ${daily * 7:>+8.2f} ${daily * 30:>+9.2f}")


# ────────────────────────────────────────────────────────────────────────────
# Monte Carlo: bust prob + wallet trajectory
# ────────────────────────────────────────────────────────────────────────────

def sample_entry():
    """Sample a fade entry from the historical distribution."""
    weights = [c for _, c in ENTRY_DIST]
    entries = [e for e, _ in ENTRY_DIST]
    return random.choices(entries, weights=weights)[0] + random.uniform(-0.075, 0.075)


def sim_run(wr, wallet, n_trades, conditional_wr=False):
    """If conditional_wr=False: uniform WR. If True: use bucket-specific WR from sample."""
    for i in range(n_trades):
        entry = max(0.05, min(0.95, sample_entry()))
        limit = min(entry + 0.05, 0.98)
        sh = dynamic_shares(limit)
        cost = sh * limit
        fee = crypto_fee(limit, sh)
        if wallet < cost + fee:
            return True, wallet, i
        # If conditional, use per-bucket WR observed (29%/0%/33%/91%/75%/88%)
        if conditional_wr:
            bucket_wrs = {0.125: 0.29, 0.275: 0.0, 0.425: 0.33,
                          0.575: 0.91, 0.725: 0.75, 0.875: 0.88}
            # Find closest bucket center
            closest = min(bucket_wrs.keys(), key=lambda c: abs(c - entry))
            wr_eff = bucket_wrs[closest]
        else:
            wr_eff = wr
        if random.random() < wr_eff:
            wallet += sh * 1.00 - cost - fee
        else:
            wallet -= cost + fee
        if wallet < 0.01:
            return True, wallet, i + 1
    return False, wallet, n_trades


def trajectory_table(starting=14.72):
    print()
    print("=" * 80)
    print(f"(3) Monte Carlo trajectory (start=${starting}, 20k sims, 100 trades each)")
    print("=" * 80)
    print(f"{'WR':>20} {'P(bust)':>9} {'Mean final':>12} {'Median final':>14} "
          f"{'P10':>8} {'P90':>8}")
    print("-" * 80)
    scenarios = [
        ("UNIFORM 76% (signal)", 0.76, False),
        ("UNIFORM 70% (sim pt)", 0.70, False),
        ("UNIFORM 68% (real-res)", 0.68, False),
        ("UNIFORM 60%", 0.60, False),
        ("UNIFORM 54% (CI low)", 0.54, False),
        ("CONDITIONAL (per bucket)", None, True),
    ]
    for label, wr, cond in scenarios:
        busts = 0
        finals = []
        for _ in range(10000):
            b, f, _ = sim_run(wr or 0.7, starting, 100, conditional_wr=cond)
            if b:
                busts += 1
            finals.append(f)
        finals.sort()
        print(f"{label:>20} {100*busts/10000:>7.1f}% "
              f"${mean(finals):>10.2f} ${finals[len(finals)//2]:>12.2f} "
              f"${finals[len(finals)//10]:>6.2f} ${finals[len(finals)*9//10]:>6.2f}")


# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    per_trade_table()
    daily_projection()
    trajectory_table()
