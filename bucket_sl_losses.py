"""
Bucket the BE-SL strategy's exits by ENTRY PRICE.
Question: Are SL losses concentrated at certain entry prices?
If yes, those are "death zone" entries we should skip — or fade by buying
the OPPOSITE side.
"""
import argparse
import csv
import math
from collections import defaultdict
from statistics import mean


def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", help="trades CSV from minute_mark_tpsl.py --csv")
    ap.add_argument("--side", default="ALL", choices=["UP", "DN", "ALL"])
    args = ap.parse_args()

    rows = []
    with open(args.csv) as f:
        r = csv.DictReader(f)
        for row in r:
            if args.side != "ALL" and row["side"] != args.side:
                continue
            rows.append({
                "side": row["side"],
                "entry": float(row["entry_price"]),
                "exit_reason": row["exit_reason"],
                "pnl": float(row["pnl"]),
            })
    print(f"Loaded {len(rows)} trades from {args.csv} (side={args.side})\n")

    # Bucket by entry price (5-cent buckets)
    buckets = defaultdict(list)
    for r in rows:
        lo = math.floor(r["entry"] / 0.05) * 0.05
        buckets[(round(lo, 2), round(lo + 0.05, 2))].append(r)

    print(f"{'Bucket':<14} {'n':>5} {'TP%':>5} {'SL%':>5} {'BE-SL%':>7} {'W-E%':>5} "
          f"{'WR':>5} {'95% CI':>15} {'avg PnL':>10} {'total':>10}")
    print("-" * 110)
    for (lo, hi) in sorted(buckets.keys()):
        bucket = buckets[(lo, hi)]
        n = len(bucket)
        tp = sum(1 for t in bucket if t["exit_reason"] == "TP")
        sl = sum(1 for t in bucket if t["exit_reason"] == "SL")
        besl = sum(1 for t in bucket if t["exit_reason"] == "BE-SL")
        we = sum(1 for t in bucket if t["exit_reason"] == "WINDOW-END")
        pnls = [t["pnl"] for t in bucket]
        wins = sum(1 for p in pnls if p > 0.001)
        losses = sum(1 for p in pnls if p < -0.001)
        wlo, whi = wilson_ci(wins, wins + losses)
        wr = 100 * wins / max(1, wins + losses)
        marker = ""
        if sl / max(1, n) > 0.80 and n >= 10:
            marker = " ⚠️ DEATH-ZONE"
        elif sl / max(1, n) > 0.70 and n >= 10:
            marker = " ⚠️ heavy-sl"
        print(f"${lo:.2f}-${hi:.2f}  {n:>5} "
              f"{100*tp/max(1,n):>4.0f}% {100*sl/max(1,n):>4.0f}% "
              f"{100*besl/max(1,n):>6.0f}% {100*we/max(1,n):>4.0f}% "
              f"{wr:>4.0f}% [{wlo*100:>3.0f}% — {whi*100:>3.0f}%]  "
              f"${mean(pnls):>+8.3f} ${sum(pnls):>+8.2f}{marker}")


if __name__ == "__main__":
    main()
