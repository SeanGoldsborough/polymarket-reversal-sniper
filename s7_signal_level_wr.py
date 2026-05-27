"""
Signal-level WR for S7: every $18+ CB move in every window gets counted
(not just one per window). This extracts the maximum signal from our 343
windows of historical data to tighten the CI on S7's win rate.

Key difference from s7_full_validation.py:
  - That uses S7FadeStrategy with single-shot-per-window (matches live bot)
  - This counts EVERY signal as an independent observation of the underlying edge

The strategy-level WR (what the live bot achieves) and signal-level WR (what
the edge actually is) should be CLOSE but not identical — multi-shot windows
are correlated (if you trigger twice in the same window, both resolutions
depend on the same window-end).
"""
import argparse
import math
from pathlib import Path
from statistics import mean

from replay_engine import ReplayEngine, SignalDetector
from order_engine import PaperOrderEngine, Side, OrderType


def wilson_ci(wins: int, n: int, z: float = 1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def snap_price(p):
    return round(round(p * 100) / 100, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--threshold", type=float, default=18.0)
    ap.add_argument("--max-files", type=int, default=None)
    args = ap.parse_args()

    files = sorted(Path(args.ticks).glob("ticks_*.csv*"))
    if args.max_files:
        files = files[:args.max_files]
    print(f"Signal-level WR: threshold=${args.threshold:.0f}, {len(files)} windows")
    print(f"(Multi-shot: every $18+ CB move counts as an independent observation)")

    engine = ReplayEngine()
    all_pnls_ps = []
    wins = losses = 0
    total_signals = 0
    fills = 0
    rejected_min_notional = 0
    rejected_stale_ask = 0
    sizes = []
    notionals = []
    entries = []

    for f in files:
        detector = SignalDetector(threshold=5.0)
        paper = PaperOrderEngine(starting_usdc=100000, taker_latency_ms=150,
                                 min_notional_usdc=1.0)
        # Track per-window signal-level trades
        trades_this_window = []
        last_state = [None]

        def cb(state):
            last_state[0] = state
            paper.update_book(state)
            sig = detector.update(state)
            if not sig or abs(sig.cb_move) < args.threshold:
                return
            # Build a fade order (every signal, no fired flag)
            if sig.direction == "UP":
                lbl = "DN"
                fade_bid, fade_ask = state.down_bid, state.down_ask
                token = f"{f.stem}_DN"
            else:
                lbl = "UP"
                fade_bid, fade_ask = state.up_bid, state.up_ask
                token = f"{f.stem}_UP"
            if fade_bid <= 0 or fade_bid >= 1.0 or fade_ask <= 0:
                return
            bid_price = snap_price(min(fade_ask + 0.05, 0.98))
            # Option B dynamic sizing
            shares = max(6, math.ceil(1.20 / bid_price)) if bid_price > 0 else 6
            try:
                o = paper.place_order(
                    token_id=token, token_label=lbl,
                    side=Side.BUY, price=bid_price, size=shares,
                    order_type=OrderType.FAK,
                )
                trades_this_window.append({"o": o, "dir": lbl})
            except Exception:
                pass

        engine.replay(f, cb)

        rejected_min_notional += paper.min_notional_rejections
        rejected_stale_ask += paper.taker_rejections

        last = last_state[0]
        if last is None:
            continue
        for t in trades_this_window:
            total_signals += 1
            o = t["o"]
            if o.filled_size < 1:
                continue
            fills += 1
            sizes.append(o.filled_size)
            notionals.append(o.filled_size * o.fill_avg_price)
            entries.append(o.fill_avg_price)
            final_bid = last.up_bid if t["dir"] == "UP" else last.down_bid
            if final_bid >= 0.95:
                red = 1.00
            elif final_bid <= 0.05:
                red = 0.00
            else:
                red = final_bid
            fee_ps = (o.total_fees / o.filled_size) if o.filled_size > 0 else 0
            pnl_ps = red - o.fill_avg_price - fee_ps
            all_pnls_ps.append(pnl_ps)
            pnl_total = pnl_ps * o.filled_size
            if pnl_ps > 0.001:
                wins += 1
            elif pnl_ps < -0.001:
                losses += 1

    resolved = wins + losses
    wr = wins / max(1, resolved)
    wlo, whi = wilson_ci(wins, resolved)
    avg_entry = mean(entries) if entries else 0
    be_wr = avg_entry
    margin = wr - be_wr
    days = len(files) * 5 / 60 / 24
    total_pnl_dollars = sum(p * s for p, s in zip(all_pnls_ps, sizes))

    print()
    print(f"=== SIGNAL-LEVEL S7 RESULTS ({len(files)} windows = {days:.1f} days) ===")
    print(f"Signals: {total_signals}  Fills: {fills}  "
          f"$<1 rejects: {rejected_min_notional}  stale-ask rejects: {rejected_stale_ask}")
    print(f"Resolved: W={wins} L={losses}  n={resolved}")
    print(f"WR={wr * 100:.0f}%   95% Wilson CI [{wlo * 100:.0f}% — {whi * 100:.0f}%]   "
          f"half-width = {(whi - wlo) * 50:.1f}pp")
    print(f"BE WR ≈ {be_wr * 100:.0f}%   margin = {(wr - be_wr) * 100:+.1f}pp")
    print(f"Avg PnL/share: ${mean(all_pnls_ps):+.4f}")
    print(f"Total PnL (real share sizes): ${total_pnl_dollars:+.2f}")
    print(f"Per-day: ${total_pnl_dollars / max(0.01, days):+.2f}")
    print(f"Share size: min={min(sizes):.0f} max={max(sizes):.0f} mean={mean(sizes):.1f}")
    print()
    if wlo > be_wr:
        print(f"EDGE CONFIRMED (lower CI {wlo * 100:.0f}% > BE {be_wr * 100:.0f}%)")
    else:
        print(f"EDGE NOT YET CONFIRMED at 95% (lower CI {wlo * 100:.0f}% <= BE {be_wr * 100:.0f}%)")


if __name__ == "__main__":
    main()
