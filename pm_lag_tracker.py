"""
PM bid-lag tracker.

Measures the latency between a Coinbase BTC price tick and the Polymarket
order book reacting to it. The hypothesis: when PM bid-lag is short, the
market is "fast" and our edge windows shrink (alpha already priced in by the
time we can act). When PM bid-lag is long, our edge is real.

Usage in bot:
    tracker = PMLagTracker(log_path="logs/btc_s7_pm_lag.jsonl",
                           cb_move_threshold=1.0, freshness_ms=5000)
    # In CB websocket handler:
    tracker.on_cb_tick(ts_ms, cb_old_price, cb_new_price)
    # In PM order-book handler (each bid update):
    tracker.on_pm_bid_change(ts_ms, side, old_bid, new_bid, up_token, down_token)

Each significant CB move ($1+) is buffered. When a PM bid changes within
`freshness_ms` of a CB move, we log a {cb_move, pm_change, lag_ms} record.

Analysis (offline):
    python3 pm_lag_tracker.py --analyze logs/btc_s7_pm_lag.jsonl
    Prints:
        - Distribution of lag (ms): p10/p50/p90/max
        - Reactive vs unreactive: % of CB moves that triggered a same-direction PM move
        - Bucketed by CB move size: $1, $3, $5, $10+
        - Per hour-of-day lag: identifies fast vs slow regimes
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import deque, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median
from typing import Deque, Optional, List


@dataclass
class CBEvent:
    ts_ms: int
    cb_old: float
    cb_new: float
    delta: float                       # cb_new - cb_old (signed)
    consumed: bool = False             # set when first PM reaction logged


@dataclass
class PMLagTracker:
    log_path: str
    cb_move_threshold: float = 1.0     # only log CB moves of this magnitude+
    freshness_ms: int = 5000           # max lag we'll attribute as "reaction"

    _cb_events: Deque[CBEvent] = field(default_factory=lambda: deque(maxlen=200))
    _log_file: Optional[object] = None
    _last_up_bid: float = 0.0
    _last_down_bid: float = 0.0

    def _ensure_open(self) -> None:
        if self._log_file is None:
            Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(self.log_path, "a")

    def on_cb_tick(self, ts_ms: int, cb_old: float, cb_new: float) -> None:
        if cb_old <= 0 or cb_new <= 0:
            return
        delta = cb_new - cb_old
        if abs(delta) < self.cb_move_threshold:
            return
        self._cb_events.append(CBEvent(ts_ms=ts_ms, cb_old=cb_old, cb_new=cb_new, delta=delta))

    def on_pm_bid_change(self, ts_ms: int, side: str, old_bid: float, new_bid: float) -> None:
        """side: 'UP' or 'DN'. Logs a reaction record for the most recent unmatched CB event."""
        if old_bid <= 0 or new_bid <= 0:
            return
        if abs(new_bid - old_bid) < 0.005:
            return
        for ev in reversed(self._cb_events):
            if ev.consumed:
                continue
            lag = ts_ms - ev.ts_ms
            if lag < 0:
                continue
            if lag > self.freshness_ms:
                break
            # Was the PM move direction-aligned with the CB move?
            # CB UP → expect UP bid to rise, DN bid to fall.
            cb_up = ev.delta > 0
            pm_up = new_bid > old_bid
            aligned = (
                (cb_up and ((side == "UP" and pm_up) or (side == "DN" and not pm_up)))
                or (not cb_up and ((side == "UP" and not pm_up) or (side == "DN" and pm_up)))
            )
            self._write_record(ev, ts_ms, lag, side, old_bid, new_bid, aligned)
            ev.consumed = True
            return

    def _write_record(self, ev: CBEvent, pm_ts_ms: int, lag_ms: int,
                      side: str, old_bid: float, new_bid: float, aligned: bool) -> None:
        self._ensure_open()
        rec = {
            "cb_ts_ms": ev.ts_ms,
            "pm_ts_ms": pm_ts_ms,
            "lag_ms": lag_ms,
            "cb_delta": ev.delta,
            "cb_abs": abs(ev.delta),
            "cb_dir": "UP" if ev.delta > 0 else "DN",
            "side": side,
            "old_bid": old_bid,
            "new_bid": new_bid,
            "bid_delta": new_bid - old_bid,
            "aligned": aligned,
        }
        self._log_file.write(json.dumps(rec) + "\n")
        self._log_file.flush()


def analyze(log_path: str | Path) -> None:
    """Read a pm-lag log and print distribution statistics."""
    p = Path(log_path)
    if not p.exists():
        print(f"Log not found: {log_path}")
        return
    rows: List[dict] = []
    with p.open() as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not rows:
        print("Log is empty.")
        return

    print(f"=== PM bid-lag analysis: {len(rows)} reaction events ===")
    lags = [r["lag_ms"] for r in rows]
    lags_sorted = sorted(lags)

    def pct(xs: List[int], p: float) -> int:
        if not xs:
            return 0
        i = int(p * (len(xs) - 1))
        return xs[i]

    aligned = sum(1 for r in rows if r["aligned"])
    print()
    print(f"Lag distribution (ms): "
          f"p10={pct(lags_sorted, 0.10)} p50={pct(lags_sorted, 0.50)} "
          f"p90={pct(lags_sorted, 0.90)} max={max(lags)}")
    print(f"Aligned-direction reactions: {aligned}/{len(rows)} ({100*aligned/len(rows):.0f}%)")
    print(f"  (high alignment % means PM tracks CB closely → less edge)")
    print()

    # By CB move size bucket
    print(f"=== By CB move size ===")
    buckets = [(1, 3, "$1-3"), (3, 5, "$3-5"), (5, 10, "$5-10"), (10, 1e9, "$10+")]
    for lo, hi, label in buckets:
        b = [r for r in rows if lo <= r["cb_abs"] < hi]
        if not b:
            continue
        bl = sorted(r["lag_ms"] for r in b)
        ba = sum(1 for r in b if r["aligned"])
        print(f"  {label:<8} n={len(b):>4} | median_lag={pct(bl, 0.5):>4}ms "
              f"p90={pct(bl, 0.9):>4}ms | aligned={100*ba/len(b):.0f}%")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyze", type=str, help="Path to pm-lag JSONL log to analyze")
    args = ap.parse_args()
    if args.analyze:
        analyze(args.analyze)
    else:
        # Quick self-test
        import os, tempfile
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
            log_path = tf.name
        tracker = PMLagTracker(log_path=log_path, cb_move_threshold=1.0, freshness_ms=5000)
        # Simulate a $3 CB UP move at t=0, then a PM UP bid rise at t=1200ms
        tracker.on_cb_tick(ts_ms=0, cb_old=100_000, cb_new=100_003)
        tracker.on_pm_bid_change(ts_ms=1200, side="UP", old_bid=0.50, new_bid=0.55)
        # Simulate a $5 CB DN move at t=10000, then a PM DN bid rise (aligned) at t=10800
        tracker.on_cb_tick(ts_ms=10_000, cb_old=100_003, cb_new=99_998)
        tracker.on_pm_bid_change(ts_ms=10_800, side="DN", old_bid=0.40, new_bid=0.48)
        # Close the log
        if tracker._log_file:
            tracker._log_file.close()
        analyze(log_path)
        os.unlink(log_path)


if __name__ == "__main__":
    main()
