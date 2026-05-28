"""
CB autocorrelation regime tagger.

Classifies the current market into one of three regimes based on Coinbase
BTC price autocorrelation over a rolling lookback:

    MOMENTUM  — positive autocorrelation: returns continue in the same direction.
                Fade strategies (S7) lose. Aligned strategies (S18) win.
    EXHAUSTION — negative autocorrelation: returns mean-revert.
                Fade strategies (S7) win. Aligned strategies (S18) lose.
    RANGING   — autocorrelation near zero: no signal.

API (live):
    tagger = CBRegimeTagger(lookback_s=60, bucket_s=5)
    for each tick:
        tagger.update(ts_s, cb_price)
    regime, stats = tagger.classify()

API (backtest):
    regime, stats = classify_window_from_csv(path, at_elapsed_ms=None)
    # at_elapsed_ms=None uses the full window
    # at_elapsed_ms=60_000 uses only the first 60 seconds (live-realistic)

Thresholds (tunable):
    autocorr > +0.25  → MOMENTUM
    autocorr < -0.25  → EXHAUSTION
    else              → RANGING

Stats returned:
    n_returns: int — count of 5s log-returns sampled
    autocorr: float — lag-1 Pearson autocorrelation
    rv: float — realized vol (stdev of returns)
    last_price, first_price
"""
from __future__ import annotations
import csv
import gzip
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


# Regime thresholds. Validated in backtest split (task #4).
AUTOCORR_MOMENTUM_THRESHOLD = 0.25
AUTOCORR_EXHAUSTION_THRESHOLD = -0.25


@dataclass
class CBRegimeTagger:
    lookback_s: int = 60
    bucket_s: int = 5

    # Internal: rolling list of (ts_s, cb_price) ticks
    _history: List[Tuple[float, float]] = field(default_factory=list)

    def update(self, ts_s: float, cb_price: float) -> None:
        if cb_price <= 0:
            return
        self._history.append((ts_s, cb_price))
        # Trim to lookback + 1 bucket of buffer
        cutoff = ts_s - (self.lookback_s + self.bucket_s + 1)
        while self._history and self._history[0][0] < cutoff:
            self._history.pop(0)

    def _bucketed_prices(self) -> List[float]:
        """Return one CB price per `bucket_s` seconds (latest within bucket)."""
        if not self._history:
            return []
        latest_ts = self._history[-1][0]
        # Iterate from oldest, snap to bucket boundaries
        prices: List[float] = []
        i = 0
        bucket_boundary = latest_ts - self.lookback_s
        while bucket_boundary <= latest_ts + 0.001:
            # Find the latest tick at or before bucket_boundary
            last_price = None
            while i < len(self._history) and self._history[i][0] <= bucket_boundary:
                last_price = self._history[i][1]
                i += 1
            if last_price is None and self._history:
                # No tick before this boundary; take the first available
                last_price = self._history[0][1]
            if last_price is not None:
                prices.append(last_price)
            bucket_boundary += self.bucket_s
        return prices

    def classify(self) -> Tuple[str, dict]:
        """Return (regime_label, stats_dict). Regime is 'MOMENTUM', 'EXHAUSTION',
        'RANGING', or 'INSUFFICIENT' if not enough data."""
        prices = self._bucketed_prices()
        if len(prices) < 4:
            return "INSUFFICIENT", {
                "n_returns": 0, "autocorr": 0.0, "rv": 0.0,
                "first_price": 0.0, "last_price": 0.0,
            }
        # Log-returns
        returns: List[float] = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0 and prices[i] > 0:
                returns.append(math.log(prices[i] / prices[i - 1]))
        if len(returns) < 3:
            return "INSUFFICIENT", {
                "n_returns": len(returns), "autocorr": 0.0, "rv": 0.0,
                "first_price": prices[0], "last_price": prices[-1],
            }
        # Realized vol
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)
        rv = math.sqrt(var)
        # Lag-1 autocorrelation
        autocorr = _autocorr_lag1(returns)
        if autocorr > AUTOCORR_MOMENTUM_THRESHOLD:
            regime = "MOMENTUM"
        elif autocorr < AUTOCORR_EXHAUSTION_THRESHOLD:
            regime = "EXHAUSTION"
        else:
            regime = "RANGING"
        return regime, {
            "n_returns": len(returns),
            "autocorr": autocorr,
            "rv": rv,
            "first_price": prices[0],
            "last_price": prices[-1],
        }


def _autocorr_lag1(xs: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean = sum(xs) / n
    num = sum((xs[i] - mean) * (xs[i - 1] - mean) for i in range(1, n))
    den = sum((x - mean) ** 2 for x in xs)
    if den <= 0:
        return 0.0
    return num / den


def _open_tick_file(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt")
    return open(path, "r")


def classify_window_from_csv(path: str | Path, lookback_s: int = 60,
                              bucket_s: int = 5,
                              at_elapsed_ms: Optional[int] = None) -> Tuple[str, dict]:
    """Read a tick CSV (or .csv.gz) and classify its regime.

    at_elapsed_ms=None → uses the entire window's CB history.
    at_elapsed_ms=N → uses CB history only up through elapsed_ms <= N
                       (simulates live-bot view at time N).
    """
    p = Path(path)
    tagger = CBRegimeTagger(lookback_s=lookback_s, bucket_s=bucket_s)
    with _open_tick_file(p) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                elapsed_ms = int(row["elapsed_ms"])
            except (KeyError, ValueError):
                continue
            if at_elapsed_ms is not None and elapsed_ms > at_elapsed_ms:
                break
            try:
                cb_price = float(row["cb_price"])
            except (KeyError, ValueError):
                continue
            if cb_price <= 0:
                continue
            # Use elapsed_ms / 1000 as a synthetic ts_s; ordering is what matters
            tagger.update(elapsed_ms / 1000.0, cb_price)
    return tagger.classify()


if __name__ == "__main__":
    # Smoke test: classify a few historical windows
    import sys
    from collections import Counter
    tick_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/ubuntu/reports/ticks")
    files = sorted(tick_dir.glob("ticks_*.csv*"))[:50]
    counts = Counter()
    print(f"Sampling {len(files)} windows...")
    for f in files:
        regime, stats = classify_window_from_csv(f)
        counts[regime] += 1
        if files.index(f) < 5:
            print(f"  {f.name}: regime={regime} ac={stats['autocorr']:+.3f} "
                  f"rv={stats['rv']*100:.3f}% n_ret={stats['n_returns']}")
    print()
    print(f"Distribution over {len(files)} windows:")
    for regime, n in counts.most_common():
        print(f"  {regime}: {n} ({100*n/len(files):.0f}%)")
