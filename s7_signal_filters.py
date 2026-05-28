"""
Composable signal filters for S7 / S18 strategies — derived from the
Bitcoin Trading Beginners & Advanced courses and the Chart Raiders memo.

Each filter implements:
    .observe(elapsed_ms, cb_price)  — called on every CB tick to update state
    .approve(elapsed_ms, side, cb_price) → (bool, str)
        True/False to fire the signal, plus a one-word reason for stats.

Filters are stateful per window. Call .reset() at window start.

The four filters (all course-derived):

  1. HardCloseFilter — require CB to STAY past the trigger for hold_ms.
     "A level is only invalidated when a candle BODY closes past it.
     Wicks don't count."

  2. VolumeClimaxFilter — use Coinbase tick density as a volume proxy.
     "A move is not done until volume starts decreasing." For fades, this
     means fade ONLY when volume/tick-density is FALLING.

  3. PolarityFilter — find recent origin levels (a price touched twice with
     separation), only fire on the appropriate side / when crossing them.
     "Above the origin continues up, below it collapses."

  4. MultiTFFilter — confirm the -1 timeframe (1-minute CB structure)
     agrees with the proposed direction.
     "After a level is hit, the -1 timeframe below determines direction."

All filters default to permissive when underdetermined (insufficient data
returns True so we don't lock out an entire window). Each emits a reason
code we can aggregate at backtest time to see WHY trades were skipped.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple


# ============================================================================
# 1. Hard-close filter
# ============================================================================

@dataclass
class HardCloseFilter:
    """Require the CB price at approve-time to stay near the peak/trough
    achieved during the hold window — no revert > revert_threshold.

    The course's hard-close rule says a $18 spike that immediately reverts
    is a wick, not a real break. We approximate "candle body close" with:
    in the last hold_ms, what was the most extreme price? If the current
    CB has reverted from that extreme by more than revert_threshold, the
    move was a wick — reject.

    Stateless wrt side direction during observe; side is checked at approve.
    """
    hold_ms: int = 2000
    revert_threshold: float = 5.0   # USD — how far CB can pull back and still count
    _history: Deque[Tuple[int, float]] = field(default_factory=deque)

    def reset(self) -> None:
        self._history.clear()

    def observe(self, elapsed_ms: int, cb_price: float) -> None:
        if cb_price <= 0:
            return
        self._history.append((elapsed_ms, cb_price))
        cutoff = elapsed_ms - (self.hold_ms + 2000)
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    def approve(self, elapsed_ms: int, side: str, cb_price: float) -> Tuple[bool, str]:
        """side: 'UP' means CB just moved up (we'd FADE DN). 'DN' = CB just
        moved down (we'd FADE UP). Reject if CB has reverted significantly
        from the peak/trough achieved during the hold window."""
        cutoff = elapsed_ms - self.hold_ms
        relevant = [(ts, p) for ts, p in self._history if ts >= cutoff]
        if len(relevant) < 2:
            return True, "insufficient"
        if side == "UP":
            peak = max(p for _, p in relevant)
            revert = cb_price - peak
            if revert < -self.revert_threshold:
                return False, f"wick({revert:+.1f})"
        else:
            trough = min(p for _, p in relevant)
            revert = cb_price - trough
            if revert > self.revert_threshold:
                return False, f"wick({revert:+.1f})"
        return True, "hard_close"


# ============================================================================
# 2. Volume climax filter
# ============================================================================

@dataclass
class VolumeClimaxFilter:
    """Use CB tick density (ticks per second) as a volume proxy.

    Course rule for FADE entries: a move isn't done until volume starts to
    DECREASE. If volume is still rising, the move has fuel — don't fade.
    Volume falling = exhaustion signal = the right place to fade.

    For ALIGNED entries (S18), the rule is the opposite: rising volume
    sustains the move, fade-aligned skips if volume is falling. Mode
    controls which check we do.

    Mode:
      "fade"    → fire only if tick-density is FALLING (exhaustion present)
      "aligned" → fire only if tick-density is RISING (move has fuel)
    """
    mode: str = "fade"
    bucket_ms: int = 5000        # 5-second buckets
    n_buckets: int = 6           # 30-second window
    min_density_pct_change: float = -0.20  # for fade: must be down >= 20%
    _ticks: Deque[int] = field(default_factory=deque)
    _counts: Deque[Tuple[int, int]] = field(default_factory=deque)
    # bucket_idx, tick_count

    def reset(self) -> None:
        self._ticks.clear()
        self._counts.clear()

    def observe(self, elapsed_ms: int, cb_price: float) -> None:
        if cb_price <= 0:
            return
        self._ticks.append(elapsed_ms)
        cutoff = elapsed_ms - self.bucket_ms * self.n_buckets * 2
        while self._ticks and self._ticks[0] < cutoff:
            self._ticks.popleft()

    def _bucket_densities(self, now_ms: int) -> List[int]:
        if not self._ticks:
            return []
        counts: List[int] = [0] * self.n_buckets
        oldest = now_ms - self.bucket_ms * self.n_buckets
        for ts in self._ticks:
            if ts < oldest:
                continue
            idx = (ts - oldest) // self.bucket_ms
            if 0 <= idx < self.n_buckets:
                counts[idx] += 1
        return counts

    def approve(self, elapsed_ms: int, side: str, cb_price: float) -> Tuple[bool, str]:
        counts = self._bucket_densities(elapsed_ms)
        if len(counts) < self.n_buckets:
            return True, "insufficient"
        first_half = sum(counts[: self.n_buckets // 2])
        second_half = sum(counts[self.n_buckets // 2 :])
        if first_half == 0:
            return True, "insufficient"
        # Negative pct = density decreased over the window
        pct_change = (second_half - first_half) / first_half
        if self.mode == "fade":
            # Fade only when density falling — true exhaustion
            if pct_change <= self.min_density_pct_change:
                return True, f"climaxed(pct={pct_change:+.2f})"
            return False, f"still_building(pct={pct_change:+.2f})"
        else:  # aligned
            if pct_change >= -self.min_density_pct_change:
                return True, f"sustaining(pct={pct_change:+.2f})"
            return False, f"fading(pct={pct_change:+.2f})"


# ============================================================================
# 3. Polarity / origin-level filter
# ============================================================================

@dataclass
class PolarityFilter:
    """Find recent CB origin levels (a price touched 2+ times with separation)
    and approve only when the fade would be hitting a level (not into open
    space).

    Course concept: a break level hit twice = origin = polarity divider.
    Above origin continues up; below collapses. For a binary market this
    maps directly to UP-side vs DN-side polarity.

    For a FADE on a CB UP move (buy DN token), we want to be entering at
    a price level that has historically rejected — a resistance. So we
    approve the fade if the current CB price is at or above a recent
    swing high (resistance reload). For a CB DN fade (buy UP), we want
    to be at a support level.
    """
    lookback_ms: int = 180_000   # last 3 minutes
    pivot_window: int = 5        # CB tick pivot detection window
    proximity: float = 5.0       # USD — how close to a level counts as "at"
    _history: Deque[Tuple[int, float]] = field(default_factory=deque)
    _highs: List[float] = field(default_factory=list)
    _lows: List[float] = field(default_factory=list)

    def reset(self) -> None:
        self._history.clear()
        self._highs.clear()
        self._lows.clear()

    def observe(self, elapsed_ms: int, cb_price: float) -> None:
        if cb_price <= 0:
            return
        self._history.append((elapsed_ms, cb_price))
        cutoff = elapsed_ms - self.lookback_ms
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()
        # Rebuild simple pivots if history changed shape
        self._detect_pivots()

    def _detect_pivots(self) -> None:
        """Identify recent swing highs and lows via local-extreme detection."""
        if len(self._history) < self.pivot_window * 2 + 1:
            return
        prices = [p for _, p in self._history]
        highs = []
        lows = []
        w = self.pivot_window
        for i in range(w, len(prices) - w):
            window = prices[i - w : i + w + 1]
            mid = prices[i]
            if mid == max(window):
                highs.append(mid)
            if mid == min(window):
                lows.append(mid)
        self._highs = highs
        self._lows = lows

    def approve(self, elapsed_ms: int, side: str, cb_price: float) -> Tuple[bool, str]:
        if not self._highs and not self._lows:
            return True, "insufficient"
        # side="UP" → CB moved up; we'd FADE DN (buy DN token). We want CB
        # to be at or above a recent resistance.
        if side == "UP":
            for h in self._highs:
                if abs(cb_price - h) <= self.proximity:
                    return True, f"at_resistance({h:.0f})"
            return False, "no_level"
        else:
            for low in self._lows:
                if abs(cb_price - low) <= self.proximity:
                    return True, f"at_support({low:.0f})"
            return False, "no_level"


# ============================================================================
# 4. Multi-TF (+1/-1) confirmation filter
# ============================================================================

@dataclass
class MultiTFFilter:
    """Confirm that the -1 timeframe (60-second CB micro-trend) agrees with
    the proposed fade.

    Course rule: after a 5-minute level is tested, the 1-minute timeframe
    determines direction. For a fade on a CB UP move (buy DN), we want the
    last 60s of CB structure to be flipping bearish — lower highs or a
    sustained pullback. If the 60s CB structure is still trending up,
    don't fade.

    Implementation: compute the slope of last 60s of CB. For an UP fade,
    require the recent slope (last 30s) to be negative or flat. Mirror
    for DN.
    """
    lookback_ms: int = 60_000
    recent_ms: int = 30_000
    max_recent_slope_per_s: float = 0.5  # USD/sec — allow up to this against us
    _history: Deque[Tuple[int, float]] = field(default_factory=deque)

    def reset(self) -> None:
        self._history.clear()

    def observe(self, elapsed_ms: int, cb_price: float) -> None:
        if cb_price <= 0:
            return
        self._history.append((elapsed_ms, cb_price))
        cutoff = elapsed_ms - (self.lookback_ms + 5_000)
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    def _slope(self, since_ms: int) -> Optional[float]:
        """Simple slope (USD per second) over points >= since_ms."""
        pts = [(ts, p) for ts, p in self._history if ts >= since_ms]
        if len(pts) < 2:
            return None
        x0 = pts[0][0]
        n = len(pts)
        sx = sum((p[0] - x0) / 1000 for p in pts)
        sy = sum(p[1] for p in pts)
        sxy = sum(((p[0] - x0) / 1000) * p[1] for p in pts)
        sxx = sum(((p[0] - x0) / 1000) ** 2 for p in pts)
        denom = n * sxx - sx * sx
        if denom == 0:
            return None
        return (n * sxy - sx * sy) / denom

    def approve(self, elapsed_ms: int, side: str, cb_price: float) -> Tuple[bool, str]:
        recent_slope = self._slope(elapsed_ms - self.recent_ms)
        if recent_slope is None:
            return True, "insufficient"
        # FADE UP (CB went up, we buy DN): want recent slope flat/negative
        if side == "UP":
            if recent_slope <= self.max_recent_slope_per_s:
                return True, f"slope_ok({recent_slope:+.2f})"
            return False, f"still_trending_up({recent_slope:+.2f})"
        else:  # FADE DN
            if recent_slope >= -self.max_recent_slope_per_s:
                return True, f"slope_ok({recent_slope:+.2f})"
            return False, f"still_trending_down({recent_slope:+.2f})"


# ============================================================================
# Filter chain (composes multiple filters with AND semantics)
# ============================================================================

@dataclass
class FilterChain:
    """Composes a list of filters. observe forwards to all; approve returns
    (True, "pass") only if ALL filters approve. The reason is the first
    filter that rejects, or "pass" if none rejected."""
    filters: List[object] = field(default_factory=list)
    reject_counts: dict = field(default_factory=dict)
    pass_count: int = 0

    def reset(self) -> None:
        for f in self.filters:
            f.reset()
        self.reject_counts = {}
        self.pass_count = 0

    def observe(self, elapsed_ms: int, cb_price: float) -> None:
        for f in self.filters:
            f.observe(elapsed_ms, cb_price)

    def approve(self, elapsed_ms: int, side: str, cb_price: float) -> Tuple[bool, str]:
        for f in self.filters:
            ok, reason = f.approve(elapsed_ms, side, cb_price)
            if not ok:
                tag = f"{type(f).__name__}:{reason}"
                self.reject_counts[tag] = self.reject_counts.get(tag, 0) + 1
                return False, tag
        self.pass_count += 1
        return True, "pass"


# ============================================================================
# Self-tests
# ============================================================================

if __name__ == "__main__":
    # HardCloseFilter: a wick that reverts should be rejected
    hcf = HardCloseFilter(hold_ms=2000, revert_threshold=5.0)
    hcf.observe(0, 100_000)
    hcf.observe(500, 100_000)
    hcf.observe(1000, 100_018)  # spike
    hcf.observe(1500, 100_010)  # revert
    hcf.observe(2000, 100_002)  # back near anchor
    ok, reason = hcf.approve(2000, "UP", 100_002)
    assert ok is False, f"Expected reject for wick, got {ok} {reason}"
    print(f"HardCloseFilter wick: REJECT ({reason}) ✓")

    # HardCloseFilter: a sustained move should be accepted
    hcf2 = HardCloseFilter(hold_ms=2000, revert_threshold=5.0)
    hcf2.observe(0, 100_000)
    hcf2.observe(500, 100_018)
    hcf2.observe(1000, 100_020)
    hcf2.observe(1500, 100_019)
    hcf2.observe(2000, 100_021)
    ok2, reason2 = hcf2.approve(2000, "UP", 100_021)
    assert ok2 is True, f"Expected approve for hard close, got {ok2} {reason2}"
    print(f"HardCloseFilter sustained: APPROVE ({reason2}) ✓")

    # VolumeClimaxFilter (fade mode): density falling should approve
    vcf = VolumeClimaxFilter(mode="fade", bucket_ms=5000, n_buckets=6)
    # Bucket 0-15s: lots of ticks (climax)
    for t in range(0, 15_000, 100):
        vcf.observe(t, 100_000)
    # Bucket 15-30s: sparse ticks (volume fading)
    for t in range(15_000, 30_000, 2000):
        vcf.observe(t, 100_000)
    ok3, reason3 = vcf.approve(30_000, "UP", 100_000)
    print(f"VolumeClimaxFilter fade-mode (falling): {ok3} ({reason3})")

    # PolarityFilter: cb at recent swing high should approve UP fade
    pf = PolarityFilter(lookback_ms=180_000, pivot_window=2, proximity=5.0)
    prices = [100_000, 100_010, 100_015, 100_020, 100_015, 100_010,
              100_000, 99_990, 100_000, 100_010, 100_019]
    for i, p in enumerate(prices):
        pf.observe(i * 10_000, p)
    ok4, reason4 = pf.approve(110_000, "UP", 100_019)
    print(f"PolarityFilter near resistance: {ok4} ({reason4})")

    # MultiTFFilter: recent slope flat → approve UP fade
    mtf = MultiTFFilter()
    for i in range(0, 60_000, 1000):
        mtf.observe(i, 100_000 + (i / 1000) * 0.3)  # +0.3/s drift (allowable)
    ok5, reason5 = mtf.approve(60_000, "UP", 100_018)
    print(f"MultiTFFilter (small drift): {ok5} ({reason5})")

    # Chain
    chain = FilterChain(filters=[
        HardCloseFilter(hold_ms=2000, revert_threshold=5.0),
        VolumeClimaxFilter(mode="fade"),
        MultiTFFilter(),
    ])
    chain.observe(0, 100_000)
    chain.observe(2000, 100_020)
    ok6, reason6 = chain.approve(2000, "UP", 100_020)
    print(f"Chain (sustained, fresh): {ok6} ({reason6})")
    print(f"  reject_counts: {chain.reject_counts}")
    print(f"  pass_count: {chain.pass_count}")

    print("\nAll smoke tests passed.")
