import random
import math
from statistics import mean


def sim(wr, wallet, n=100):
    for i in range(n):
        fade = max(0.01, min(0.95, random.triangular(0.05, 0.95, 0.51)))
        limit = min(fade + 0.05, 0.98)
        sh = max(6, math.ceil(1.20 / limit))
        cost = sh * limit
        if wallet < cost:
            return True, wallet, i
        fee = 0.022 * min(limit, 1 - limit) * sh
        if random.random() < wr:
            wallet += sh * 1.00 - fee - cost
        else:
            wallet -= cost + fee
        if wallet < 0.01:
            return True, wallet, i + 1
    return False, wallet, n


W = "WR"
P = "P(bust)"
F = "Mean final"
print(f"{W:>5} {P:>9} {F:>12}")
print("-" * 32)
for wr in (0.84, 0.80, 0.76, 0.70, 0.67, 0.60, 0.56):
    bust = 0
    finals = []
    for _ in range(20000):
        b, f, _ = sim(wr, 16.81)
        if b:
            bust += 1
        finals.append(f)
    print(f"{wr * 100:>4.0f}% {100 * bust / 20000:>7.1f}% ${mean(finals):>10.2f}")
