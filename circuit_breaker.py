"""
Rolling-window WR circuit breaker.

Use:
    breaker = RollingWRBreaker(window_n=5, min_wr=0.40, pause_hours=4)
    breaker.load_from_stdout("logs/btc_s7_stdout.log")   # or load_from_jsonl
    if breaker.should_skip(now_ts_s):
        log("skipping — circuit breaker engaged")
    else:
        # ... fire trade ...
        breaker.record_outcome(won=bool, ts_s=now_ts_s)

Spec:
    - Track the last `window_n` filled-trade outcomes (W/L).
    - If win-rate over that window < `min_wr`, ENGAGE: skip all signals for
      `pause_hours` hours of wall-clock time.
    - After the pause expires, CLEAR history. Next `window_n` trades must
      finish before the breaker re-evaluates. This prevents flicker.

Tested against live S7 trades 1-12 (3W/9L overall):
    - Outcome sequence: W L W L L W L L L L L L
    - Breaker (n=5, min_wr=0.40) would fire after trade #8 (LLWLL = 1/5 = 20%)
    - Prevents trades #9-12, saves ~$9.65 in losses (5/26-5/28 window).
"""
from __future__ import annotations
import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, List, Optional, Tuple


# Stdout format:  "[15:25:30] [RESOLVE] #13 DN | entry $0.61 → exit $0.00 | P&L $-3.89"
# We only need the P&L sign to decide W/L.
_RESOLVE_RE = re.compile(
    r"\[RESOLVE\][^|]*\|[^|]*\|\s*P&L\s*\$([-+]?\d+\.\d+)"
)


@dataclass
class TradeOutcome:
    won: bool
    ts_s: float  # epoch seconds


@dataclass
class RollingWRBreaker:
    window_n: int = 5
    min_wr: float = 0.40
    pause_hours: float = 4.0

    history: Deque[TradeOutcome] = field(default_factory=deque)
    engaged_until_s: Optional[float] = None   # epoch seconds when pause ends
    engagement_log: List[Tuple[float, float]] = field(default_factory=list)  # (engaged_at, until)

    def _trim(self) -> None:
        while len(self.history) > self.window_n:
            self.history.popleft()

    def load_from_jsonl(self, path: str | Path) -> int:
        """Load trade outcomes from a btc_*_trades.jsonl.
        Returns count of records loaded. Only counts filled trades with non-zero
        expected_pnl (skips NOFILL / rejected / exception)."""
        p = Path(path)
        if not p.exists():
            return 0
        rows: List[TradeOutcome] = []
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("outcome") != "filled":
                    continue
                pnl = r.get("expected_pnl", 0)
                if pnl == 0:
                    continue
                # ISO time → epoch
                ts_str = r.get("time", "")
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    ts_s = dt.timestamp()
                except Exception:
                    ts_s = 0.0
                rows.append(TradeOutcome(won=(pnl > 0), ts_s=ts_s))
        # Keep only the most recent window_n
        for r in rows[-self.window_n:]:
            self.history.append(r)
        return len(rows)

    def load_from_stdout(self, path: str | Path) -> int:
        """Load trade outcomes by parsing RESOLVE lines from the bot's stdout log.
        Useful when the JSONL is stale (record_trade bug). Returns count."""
        p = Path(path)
        if not p.exists():
            return 0
        rows: List[TradeOutcome] = []
        with p.open() as f:
            for line in f:
                m = _RESOLVE_RE.search(line)
                if not m:
                    continue
                pnl = float(m.group(1))
                rows.append(TradeOutcome(won=(pnl > 0), ts_s=0.0))
        # Keep only the most recent window_n
        for r in rows[-self.window_n:]:
            self.history.append(r)
        return len(rows)

    def record_outcome(self, won: bool, ts_s: float) -> None:
        """Append a new trade outcome and re-evaluate the breaker."""
        self.history.append(TradeOutcome(won=won, ts_s=ts_s))
        self._trim()
        # Re-evaluate only if breaker is not currently engaged
        if self.engaged_until_s is not None and ts_s < self.engaged_until_s:
            return
        # Auto-clear if pause has expired
        if self.engaged_until_s is not None and ts_s >= self.engaged_until_s:
            self.engaged_until_s = None
            self.history.clear()
            self.history.append(TradeOutcome(won=won, ts_s=ts_s))
        if len(self.history) >= self.window_n:
            wins = sum(1 for o in self.history if o.won)
            wr = wins / len(self.history)
            if wr < self.min_wr:
                self.engaged_until_s = ts_s + self.pause_hours * 3600.0
                self.engagement_log.append((ts_s, self.engaged_until_s))

    def should_skip(self, now_s: float) -> bool:
        """Return True if the bot should skip this signal."""
        if self.engaged_until_s is None:
            return False
        if now_s >= self.engaged_until_s:
            # Pause expired — clear and re-arm
            self.engaged_until_s = None
            self.history.clear()
            return False
        return True

    def status(self, now_s: float) -> dict:
        wins = sum(1 for o in self.history if o.won)
        n = len(self.history)
        wr = wins / n if n else 0.0
        return {
            "n_recent": n,
            "wins": wins,
            "wr": wr,
            "engaged": self.engaged_until_s is not None,
            "engaged_until_s": self.engaged_until_s,
            "secs_until_resume": (self.engaged_until_s - now_s) if self.engaged_until_s else 0,
            "total_engagements": len(self.engagement_log),
        }


if __name__ == "__main__":
    # Replay historical S7 trades to verify breaker fires at the right time.
    outcomes = [
        ("#1", "W"), ("#2", "L"), ("#3", "W"), ("#4", "L"),
        ("#5", "L"), ("#6", "W"), ("#7", "L"), ("#8", "L"),
        ("#9", "L"), ("#10", "L"), ("#11", "L"), ("#12", "L"),
    ]
    pnls = [+0.31, -2.09, +0.55, -7.67, -0.23, +5.79, -2.26, -2.20, -1.48, -1.65, -2.63, -3.89]
    breaker = RollingWRBreaker(window_n=5, min_wr=0.40, pause_hours=4.0)
    ts = 1_779_500_000.0
    saved = 0.0
    skipped_pnl = 0.0
    print(f"{'#':<4} {'W/L':<3} {'rolling-WR':<12} {'engaged?':<10} {'pnl':<8} {'cum':<8}")
    cum = 0.0
    for (label, wl), pnl in zip(outcomes, pnls):
        skip = breaker.should_skip(ts)
        if skip:
            saved -= pnl  # we would have skipped this trade
            skipped_pnl += pnl
            row = f"{label:<4} SKIP                                    {pnl:+.2f}"
            print(row)
        else:
            cum += pnl
            won = (wl == "W")
            breaker.record_outcome(won=won, ts_s=ts)
            status = breaker.status(ts)
            wr_str = f"{status['wins']}/{status['n_recent']} {100*status['wr']:.0f}%"
            print(f"{label:<4} {wl:<3} {wr_str:<12} {'YES' if status['engaged'] else 'no':<10} "
                  f"{pnl:+.2f}   {cum:+.2f}")
        ts += 30 * 60  # 30 min between trades
    print()
    print(f"Cumulative without breaker: ${sum(pnls):+.2f}")
    print(f"Cumulative with breaker:    ${cum:+.2f}  (saved {saved:+.2f} by skipping)")
