"""
Replay Engine — streams historical tick data into strategies.

Replays the CSV files produced by tick_recorder.py at strategy-callback speed,
maintaining the current bilateral book + Coinbase BTC price as state.

Two consumers:
  1. SignalDetector — fires Signal events when CB moves $5+ between consecutive ticks
  2. Any strategy callback that wants the live book state at each tick

The engine is deterministic — same input file produces same callback sequence.
"""

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional


@dataclass
class BookState:
    """Snapshot of the bilateral market at one tick moment."""
    tick: int
    time_utc: str            # HH:MM:SS.fff format from recorder
    elapsed_ms: int          # ms since window start
    up_bid: float
    up_bid_size: float
    up_ask: float
    up_ask_size: float
    down_bid: float
    down_bid_size: float
    down_ask: float
    down_ask_size: float
    cb_price: float
    source: str              # POLY-SNAP | POLY | CB

    @property
    def up_spread(self) -> float:
        return self.up_ask - self.up_bid if self.up_ask > 0 and self.up_bid > 0 else 0.0

    @property
    def down_spread(self) -> float:
        return self.down_ask - self.down_bid if self.down_ask > 0 and self.down_bid > 0 else 0.0


@dataclass
class Signal:
    """A CB-move event detected by SignalDetector."""
    direction: str           # UP or DN
    cb_move: float           # signed (positive for UP, negative for DN)
    cb_price: float          # current CB price at signal time
    state: BookState         # market state at signal moment


class ReplayEngine:
    """Stream ticks from CSV files. Maintains current state. No strategy logic."""

    def __init__(self):
        self.state: BookState = BookState(
            tick=0, time_utc="", elapsed_ms=0,
            up_bid=0, up_bid_size=0, up_ask=0, up_ask_size=0,
            down_bid=0, down_bid_size=0, down_ask=0, down_ask_size=0,
            cb_price=0, source="",
        )
        self.current_file: Optional[Path] = None

    def iter_ticks(self, tick_file: Path) -> Iterator[BookState]:
        """Yield BookState objects for each row in the CSV.

        Tolerant of two recorder schemas:
        - Old (May 12): no *_size columns
        - New (May 13+): includes up_bid_size etc.
        """
        self.current_file = Path(tick_file)
        with open(tick_file) as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    self.state = BookState(
                        tick=int(row["tick"]),
                        time_utc=row["time_utc"],
                        elapsed_ms=int(row["elapsed_ms"]),
                        up_bid=float(row["up_bid"]),
                        up_bid_size=float(row.get("up_bid_size", 0) or 0),
                        up_ask=float(row["up_ask"]),
                        up_ask_size=float(row.get("up_ask_size", 0) or 0),
                        down_bid=float(row["down_bid"]),
                        down_bid_size=float(row.get("down_bid_size", 0) or 0),
                        down_ask=float(row["down_ask"]),
                        down_ask_size=float(row.get("down_ask_size", 0) or 0),
                        cb_price=float(row["cb_price"]),
                        source=row["source"],
                    )
                except (KeyError, ValueError):
                    # skip malformed rows
                    continue
                yield self.state

    def replay(self, tick_file: Path, callback: Callable[[BookState], None]) -> int:
        """Stream every tick to callback. Returns total tick count."""
        n = 0
        for tick in self.iter_ticks(tick_file):
            callback(tick)
            n += 1
        return n

    def replay_dir(self, dir_path: Path, callback: Callable[[BookState], None],
                   on_window_start: Optional[Callable[[Path], None]] = None,
                   on_window_end: Optional[Callable[[Path, int], None]] = None) -> int:
        """Replay all tick files in chronological order. Optional hooks for window boundaries."""
        files = sorted(Path(dir_path).glob("ticks_*.csv"))
        total = 0
        for f in files:
            if on_window_start:
                on_window_start(f)
            n = self.replay(f, callback)
            if on_window_end:
                on_window_end(f, n)
            total += n
        return total


class SignalDetector:
    """
    Detect CB spikes using the same logic as the live bot:
      if abs(new_cb_price - prev_cb_price) >= threshold AND consecutive CB ticks were <=max_gap_ms apart, fire.

    Maintains state across ticks. Pass each new tick via .update(state) and check return value.
    """

    def __init__(self, threshold: float = 5.0, max_gap_ms: int = 1500):
        self.threshold = threshold
        self.max_gap_ms = max_gap_ms
        self.prev_cb_price: float = 0.0
        self.prev_cb_ms: int = 0

    def update(self, state: BookState) -> Optional[Signal]:
        """Called on every tick. Returns Signal if a $threshold+ move just fired."""
        if state.cb_price <= 0:
            return None
        # Only act on CB updates (changes to cb_price are what we care about)
        if state.cb_price == self.prev_cb_price:
            return None

        signal: Optional[Signal] = None
        if (self.prev_cb_price > 0
                and (state.elapsed_ms - self.prev_cb_ms) <= self.max_gap_ms):
            move = state.cb_price - self.prev_cb_price
            if abs(move) >= self.threshold:
                signal = Signal(
                    direction="UP" if move > 0 else "DN",
                    cb_move=move,
                    cb_price=state.cb_price,
                    state=state,
                )

        self.prev_cb_price = state.cb_price
        self.prev_cb_ms = state.elapsed_ms
        return signal

    def reset(self):
        """Reset between windows (CB price tracking shouldn't cross window boundaries)."""
        self.prev_cb_price = 0.0
        self.prev_cb_ms = 0


# ── Quick smoke test if run directly ──
if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Replay engine smoke test")
    parser.add_argument("--dir", default="/home/ubuntu/reports/ticks",
                        help="Directory of tick CSV files")
    parser.add_argument("--limit", type=int, default=1, help="How many files to scan")
    parser.add_argument("--threshold", type=float, default=5.0, help="CB move threshold")
    args = parser.parse_args()

    engine = ReplayEngine()
    detector = SignalDetector(threshold=args.threshold)

    stats = {"total_ticks": 0, "total_signals": 0, "file_count": 0}
    files = sorted(Path(args.dir).glob("ticks_*.csv"))[: args.limit]

    for f in files:
        detector.reset()
        signals_in_file = []

        def cb(state: BookState):
            stats["total_ticks"] += 1
            sig = detector.update(state)
            if sig:
                signals_in_file.append(sig)

        n = engine.replay(f, cb)
        stats["total_signals"] += len(signals_in_file)
        stats["file_count"] += 1
        print(f"{f.name}: {n} ticks, {len(signals_in_file)} signals (>=${args.threshold})")
        for s in signals_in_file[:5]:
            print(f"   {s.state.time_utc} {s.direction} ${s.cb_move:+.2f} cb=${s.cb_price:.2f}")

    print()
    print(f"Total: {stats['file_count']} files, {stats['total_ticks']:,} ticks, {stats['total_signals']} signals")
