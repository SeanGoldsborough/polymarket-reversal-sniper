"""Fine-grained EV per fade entry price (every $0.05) — clarifies the cutover."""
import math


def crypto_fee(p, sh):
    return 0.07 * p * (1 - p) * sh


def dyn_sh(limit, base=6, target=1.20):
    return max(base, math.ceil(target / limit))


def ev(fade_entry, wr):
    limit = min(fade_entry + 0.05, 0.98)
    sh = dyn_sh(limit)
    fee = crypto_fee(limit, sh)
    win = sh * 1.00 - sh * limit - fee
    loss = -(sh * limit + fee)
    return wr * win + (1 - wr) * loss, sh


print(f"{'Fade $':>7} {'Sh':>4} {'@54%':>9} {'@68%':>9} {'@70%':>9} {'@76%':>9} {'@83%':>9}")
print("-" * 60)
for fp_int in range(5, 96, 5):
    fp = fp_int / 100
    row = f"${fp:>5.2f} "
    _, sh = ev(fp, 0.70)
    row += f"{sh:>4} "
    for wr in (0.54, 0.68, 0.70, 0.76, 0.83):
        e, _ = ev(fp, wr)
        marker = " " if e >= 0 else "X"
        row += f" ${e:>+6.2f}{marker}"
    print(row)
print()
print("X = EV-negative at that WR. 'Profitable' means EV > $0 at the assumed WR.")
print("At 70% WR: profitable up to ~$0.65 fade; losing above.")
print("At 54% WR: profitable only up to ~$0.45 fade.")
