"""
Search for strategies with strong margin (>=10pp WR above break-even).

Tests several variants using the realistic engine:
  - S2 with different thresholds (15, 18, 20, 25, 30, 35)
  - S6 with different thresholds (already tested)
  - S2 + selective filters
"""

import argparse
from pathlib import Path
from statistics import mean

from replay_engine import ReplayEngine, SignalDetector, BookState
from order_engine import PaperOrderEngine, Side, OrderType


def run_s2_at_threshold(tick_dir, threshold, max_files=None, shares=10):
    engine = ReplayEngine()
    files = sorted(tick_dir.glob("ticks_*.csv"))
    if max_files:
        files = files[:max_files]

    pnls = []
    wins = losses = filled = signals = 0
    entries = []

    for fi, f in enumerate(files):
        detector = SignalDetector(threshold=5.0)
        paper = PaperOrderEngine(starting_usdc=10000.0, taker_latency_ms=150)
        trades = []
        last_state = {"s": None}

        def cb(state):
            last_state["s"] = state
            paper.update_book(state)
            sig = detector.update(state)
            if sig and abs(sig.cb_move) >= threshold:
                if sig.direction == "UP":
                    fade_lbl, fade_bid, fade_ask = "DN", state.down_bid, state.down_ask
                else:
                    fade_lbl, fade_bid, fade_ask = "UP", state.up_bid, state.up_ask
                if fade_bid <= 0 or fade_bid >= 1.0 or fade_ask <= 0:
                    return
                bid_price = min(fade_ask + 0.05, 0.98)
                try:
                    o = paper.place_order(
                        token_id=f"{f.stem}_{fade_lbl}", token_label=fade_lbl,
                        side=Side.BUY, price=bid_price, size=shares, order_type=OrderType.FAK,
                    )
                    trades.append({"order": o, "dir": fade_lbl})
                except Exception:
                    pass

        engine.replay(f, cb)

        last = last_state["s"]
        if last is None: continue
        for t in trades:
            signals += 1
            o = t["order"]
            if o.filled_size < 1: continue
            filled += 1
            final_bid = last.up_bid if t["dir"] == "UP" else last.down_bid
            if final_bid >= 0.95: red = 1.00
            elif final_bid <= 0.05: red = 0.00
            else: red = final_bid
            fee_ps = (o.total_fees / o.filled_size) if o.filled_size > 0 else 0
            pnl = red - o.fill_avg_price - fee_ps
            pnls.append(pnl)
            entries.append(o.fill_avg_price)
            if pnl > 0.001: wins += 1
            elif pnl < -0.001: losses += 1

    wr = 100 * wins / max(1, wins + losses)
    avg_entry = mean(entries) if entries else 0
    be_wr = 100 * avg_entry  # break-even WR = entry / 1.00 (rough — ignores fee)
    margin = wr - be_wr
    return {
        "threshold": threshold, "signals": signals, "filled": filled,
        "wr": wr, "avg_entry": avg_entry, "be_wr": be_wr, "margin": margin,
        "total_pnl": sum(pnls), "avg_pnl": mean(pnls) if pnls else 0,
        "per_day": sum(pnls) * 10 / max(0.01, len(files) * 5 / 60 / 24),
    }


def main():
    tick_dir = Path("/home/ubuntu/reports/ticks")
    print("S2 fade threshold sweep — realistic engine")
    print()
    print(f"{'Thresh':<7} {'Signals':>8} {'Filled':>7} {'WR':>5} {'Avg Entry':>10} {'BE WR':>7} {'Margin':>8} {'$/sh':>8} {'$/day @ 10sh':>14}")
    print("-" * 95)
    for thresh in [15, 18, 20, 25, 30, 35]:
        r = run_s2_at_threshold(tick_dir, thresh)
        print(f"{f'${thresh}':<7} {r['signals']:>8} {r['filled']:>7} {r['wr']:>4.0f}% ${r['avg_entry']:>9.3f} {r['be_wr']:>6.0f}%  {r['margin']:>+6.1f}pp ${r['avg_pnl']:>+7.4f} ${r['per_day']:>+12.2f}")


if __name__ == "__main__":
    main()
