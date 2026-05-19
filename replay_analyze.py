"""
Replay analysis — validates the bid-reaction hypothesis at tick-level resolution.

Runs every CB-spike signal through the recorded tick data and measures:
  - Did the bid (on the side we'd buy) reach +$0.04 within X seconds?
  - How fast (median + distribution)?
  - How often did the same window have multiple signals?
  - $10+ vs $5-9 split

This is the data validation that proves whether the strategy edge is real
*before* we build the order execution module and paper bot.
"""

import argparse
from pathlib import Path
from collections import Counter
from statistics import mean, median

from replay_engine import ReplayEngine, SignalDetector, BookState, Signal


def analyze_dir(tick_dir: Path, threshold: float = 5.0,
                tp_offset: float = 0.04,
                tp_window_sec: float = 60.0,
                max_files: int = None):
    engine = ReplayEngine()
    files = sorted(tick_dir.glob("ticks_*.csv"))
    if max_files:
        files = files[:max_files]

    # Aggregated stats
    total_signals = 0
    signals_by_strength = {"$5-6": 0, "$6-7": 0, "$7-8": 0, "$8-10": 0, "$10+": 0}
    tp_eligible_by_strength = {k: 0 for k in signals_by_strength}
    tp_reach_times_ms = []  # ms-to-tp-reach for trades that DID reach +$0.04
    windows_with_signals = 0
    signal_count_per_window = []

    # For each file (a window), run detector and track per-window state
    for fi, f in enumerate(files):
        detector = SignalDetector(threshold=threshold)
        # Open-position state per concurrent active signal:
        # When a signal fires we treat as an entry attempt at the side's current ask
        # then watch the same side's bid for reaching entry+$0.04
        active_trades = []  # list of {"direction","entry_bid","entry_ms","strength_bucket"}
        signals_this_window = 0

        def cb(state: BookState):
            nonlocal signals_this_window
            sig = detector.update(state)
            if sig:
                signals_this_window += 1
                # The "entry" approximates the ask on the chosen side
                if sig.direction == "UP":
                    entry = state.up_ask if state.up_ask > 0 else state.up_bid
                    target = entry + tp_offset
                else:
                    entry = state.down_ask if state.down_ask > 0 else state.down_bid
                    target = entry + tp_offset
                magnitude = abs(sig.cb_move)
                if magnitude < 6:
                    bucket = "$5-6"
                elif magnitude < 7:
                    bucket = "$6-7"
                elif magnitude < 8:
                    bucket = "$7-8"
                elif magnitude < 10:
                    bucket = "$8-10"
                else:
                    bucket = "$10+"
                active_trades.append({
                    "direction": sig.direction,
                    "entry_bid": entry,
                    "target": target,
                    "entry_ms": state.elapsed_ms,
                    "strength_bucket": bucket,
                    "tp_reached": False,
                })

            # For each active trade, check if bid (on same side) has reached target
            window_ms = int(tp_window_sec * 1000)
            for t in active_trades:
                if t["tp_reached"]:
                    continue
                if state.elapsed_ms - t["entry_ms"] > window_ms:
                    continue
                cur_bid = state.up_bid if t["direction"] == "UP" else state.down_bid
                if cur_bid >= t["target"]:
                    t["tp_reached"] = True
                    t["tp_time_ms"] = state.elapsed_ms - t["entry_ms"]

        engine.replay(f, cb)

        # End of window — collect stats from active_trades
        for t in active_trades:
            total_signals += 1
            signals_by_strength[t["strength_bucket"]] += 1
            if t["tp_reached"]:
                tp_eligible_by_strength[t["strength_bucket"]] += 1
                tp_reach_times_ms.append(t["tp_time_ms"])

        if signals_this_window > 0:
            windows_with_signals += 1
            signal_count_per_window.append(signals_this_window)

        if (fi + 1) % 25 == 0:
            print(f"  Processed {fi+1}/{len(files)} files...")

    # ──────── Print report ────────
    print()
    print("=" * 80)
    print(f"  REPLAY ANALYSIS — {len(files)} tick files, threshold ${threshold}")
    print("=" * 80)
    print()
    print(f"Total signals detected: {total_signals}")
    print(f"Windows with at least 1 signal: {windows_with_signals}/{len(files)} ({100*windows_with_signals/max(1,len(files)):.0f}%)")
    if signal_count_per_window:
        print(f"Avg signals per signal-window: {mean(signal_count_per_window):.2f}")
        print(f"Max signals in one window: {max(signal_count_per_window)}")
        dist = Counter(signal_count_per_window)
        print(f"Distribution: {dict(sorted(dist.items()))}")

    print()
    print(f"## TP-eligibility — bid reached entry+${tp_offset} within {tp_window_sec}s")
    print()
    print(f"{'Strength':<10} {'Signals':>8} {'TP-elig':>10} {'%':>6}")
    print("-" * 40)
    for bucket in ["$5-6", "$6-7", "$7-8", "$8-10", "$10+"]:
        n = signals_by_strength[bucket]
        tp = tp_eligible_by_strength[bucket]
        pct = 100*tp/n if n else 0
        print(f"{bucket:<10} {n:>8} {tp:>10} {pct:>5.0f}%")
    total_tp = sum(tp_eligible_by_strength.values())
    print(f"{'TOTAL':<10} {total_signals:>8} {total_tp:>10} {100*total_tp/max(1,total_signals):>5.0f}%")

    print()
    print(f"## Reaction speed — time to reach +${tp_offset} bid")
    if tp_reach_times_ms:
        times = sorted(tp_reach_times_ms)
        n = len(times)
        print(f"  n={n}")
        print(f"  Median: {times[n//2]/1000:.1f}s")
        print(f"  P25:    {times[n//4]/1000:.1f}s")
        print(f"  P75:    {times[3*n//4]/1000:.1f}s")
        print(f"  Min:    {times[0]/1000:.1f}s")
        print(f"  Max:    {times[-1]/1000:.1f}s")
        # Buckets
        b_lt5 = sum(1 for t in times if t < 5000)
        b_5_15 = sum(1 for t in times if 5000 <= t < 15000)
        b_15_30 = sum(1 for t in times if 15000 <= t < 30000)
        b_30p = sum(1 for t in times if t >= 30000)
        print(f"\n  <5s:    {b_lt5} ({100*b_lt5/n:.0f}%)")
        print(f"  5-15s:  {b_5_15} ({100*b_5_15/n:.0f}%)")
        print(f"  15-30s: {b_15_30} ({100*b_15_30/n:.0f}%)")
        print(f"  >=30s:  {b_30p} ({100*b_30p/n:.0f}%)")

    return {
        "total_signals": total_signals,
        "tp_eligible": total_tp,
        "tp_eligible_rate": total_tp / max(1, total_signals),
        "windows_signaling": windows_with_signals,
        "windows_total": len(files),
        "by_strength": signals_by_strength,
        "tp_by_strength": tp_eligible_by_strength,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="/home/ubuntu/reports/ticks")
    parser.add_argument("--threshold", type=float, default=5.0)
    parser.add_argument("--tp-offset", type=float, default=0.04)
    parser.add_argument("--tp-window-sec", type=float, default=60.0)
    parser.add_argument("--max-files", type=int, default=None)
    args = parser.parse_args()
    analyze_dir(Path(args.dir), args.threshold, args.tp_offset, args.tp_window_sec, args.max_files)
