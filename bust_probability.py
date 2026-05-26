"""
S7 bust probability given current wallet + Option B dynamic sizing.
Monte Carlo across the WR confidence interval.
"""
import random
import math
from statistics import mean


def simulate_one_run(wr, starting_wallet, n_trades_horizon, avg_fade_price=0.51,
                     fak_markup=0.05, min_notional=1.20, base_shares=6):
    """Simulate N trades; return (busted?, final wallet, trades_done)."""
    wallet = starting_wallet
    for i in range(n_trades_horizon):
        # Sample a fade price from a rough distribution that matches historical avg $0.51
        # Use a triangular distribution: most fades around $0.20-$0.70, some extremes
        fade_price = max(0.01, min(0.95,
                                    random.triangular(0.05, 0.95, avg_fade_price)))
        limit_price = min(fade_price + fak_markup, 0.98)
        # Dynamic sizing
        shares = max(base_shares, math.ceil(min_notional / limit_price))
        entry_cost = shares * limit_price
        if wallet < entry_cost:
            return True, wallet, i  # busted (can't afford trade)
        # Outcome
        if random.random() < wr:
            # WIN: token resolves to $1.00
            payout = shares * 1.00 - 0.022 * min(limit_price, 1 - limit_price) * shares
            pnl = payout - entry_cost
        else:
            # LOSS: token resolves to $0.00
            pnl = -entry_cost - 0.022 * min(limit_price, 1 - limit_price) * shares
        wallet += pnl
        if wallet < 0.01:
            return True, wallet, i + 1
    return False, wallet, n_trades_horizon


def run_mc(wr, starting_wallet, n_trades, n_sims=10000):
    bust_count = 0
    final_wallets = []
    bust_at = []
    for _ in range(n_sims):
        busted, final, trades = simulate_one_run(wr, starting_wallet, n_trades)
        if busted:
            bust_count += 1
            bust_at.append(trades)
        final_wallets.append(final)
    return {
        "wr": wr,
        "p_bust": bust_count / n_sims,
        "mean_final": mean(final_wallets),
        "median_final": sorted(final_wallets)[n_sims // 2],
        "median_bust_at": sorted(bust_at)[len(bust_at) // 2] if bust_at else None,
    }


def main():
    starting = 16.81
    horizon = 100  # trades to simulate
    n_sims = 20000
    print(f"S7 bust probability — starting wallet=${starting}, "
          f"horizon={horizon} trades, n_sims={n_sims}")
    print(f"(Option B dynamic sizing: shares = max(6, ceil($1.20 / limit_price)))")
    print()
    print(f"{'WR':>5} {'P(bust)':>9} {'Mean final $':>14} {'Median final $':>15} {'Median bust@trade':>20}")
    print("-" * 80)
    for wr in (0.83, 0.80, 0.75, 0.70, 0.65, 0.60, 0.54, 0.50):
        r = run_mc(wr, starting, horizon, n_sims)
        bust_at_str = str(r['median_bust_at']) if r['median_bust_at'] is not None else "—"
        print(f"{wr*100:>4.0f}% {r['p_bust']*100:>7.1f}% "
              f"${r['mean_final']:>12.2f} ${r['median_final']:>13.2f} "
              f"{bust_at_str:>20}")
    print()
    print("Interpretation:")
    print("  - 70%  WR is sim's point estimate")
    print("  - 83%  is upper 95% CI (recent regime is favorable)")
    print("  - 54%  is lower 95% CI — if live falls here, we're at break-even")
    print("  - 50%  is below CI — implies edge is gone")


if __name__ == "__main__":
    main()
