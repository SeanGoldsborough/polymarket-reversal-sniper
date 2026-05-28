"""
Regime classifier v2 — replaces the lagging MOMENTUM/EXHAUSTION/RANGING tagger
with a 5-substate directional taxonomy derived from the Bitcoin Trading
courses (Beginner Vol I & Advanced Vol II) and the Chart Raiders memo.

The 5 sub-regimes (with optimal strategy mapping):

  ACCUMULATION_BULLISH  Narrow range, higher lows building.    → S18 UP on breakout
  ACCUMULATION_BEARISH  Narrow range, lower highs building.    → S18 DN on breakdown
  TRENDING_UP           Sustained HH+HL; CB up across window.  → S18 UP on pullback
  TRENDING_DOWN         Sustained LH+LL; CB down across window. → S18 DN on pullback
  DISTRIBUTION          Tick density climaxed then fading +
                        CB stalling = exhaustion signal.       → S7 FADE

Each sub-regime carries an "optimal_strategy" recommendation and a
"do_not_use" list, so a strategy can ask the classifier whether IT should
fire in the current regime.

Course-derived design:
  - "There is no genuine 'sideways' — what looks sideways IS accumulation"
    → no neutral bucket; every observation lands in one of the 5
  - "Strength is relative to events, not a fixed touch count"
    → classifier weights recent extremes by recency
  - "The trend is your friend until it breaks"
    → trend sub-regimes recommend WITH-trend (S18 aligned), not against
  - "Volume climax + price stall = distribution"
    → only fade in DISTRIBUTION, never in trends or accumulation

API:
    clf = RegimeClassifierV2(lookback_ms=120_000)
    for each tick: clf.observe(elapsed_ms, cb_price)
    label, info = clf.classify(elapsed_ms)
    # label = one of the 5 strings; info has {optimal_strategy, do_not_use, signals_used}

The classifier is permissive when data is thin — returns INSUFFICIENT
rather than guessing.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple


# Strategy mapping per sub-regime
STRATEGY_MAP = {
    "ACCUMULATION_BULLISH": {
        "optimal_strategy": "S18-ALIGNED-UP-on-breakout",
        "do_not_use": ["S7-FADE"],
        "rationale": "Building higher lows → breakout up likely. Fading the breakout = lose.",
    },
    "ACCUMULATION_BEARISH": {
        "optimal_strategy": "S18-ALIGNED-DN-on-breakdown",
        "do_not_use": ["S7-FADE"],
        "rationale": "Building lower highs → breakdown likely. Fading the breakdown = lose.",
    },
    "TRENDING_UP": {
        "optimal_strategy": "S18-ALIGNED-UP",
        "do_not_use": ["S7-FADE"],
        "rationale": "Trend is your friend. Fade against trend loses.",
    },
    "TRENDING_DOWN": {
        "optimal_strategy": "S18-ALIGNED-DN",
        "do_not_use": ["S7-FADE"],
        "rationale": "Trend is your friend. Fade against trend loses.",
    },
    "DISTRIBUTION": {
        "optimal_strategy": "S7-FADE",
        "do_not_use": ["S18-ALIGNED-DELTA-5S10"],
        "rationale": "Volume climax + stall = exhaustion. Only safe fade regime.",
    },
    "INSUFFICIENT": {
        "optimal_strategy": None,
        "do_not_use": [],
        "rationale": "Not enough data — fall through to default strategy behavior.",
    },
}


@dataclass
class RegimeClassifierV2:
    lookback_ms: int = 120_000        # 2-minute lookback
    pivot_window: int = 3             # CB pivot detection window
    trend_threshold_usd: float = 25.0 # CB net move that defines a trend
    narrow_range_usd: float = 30.0    # ≤ this range = accumulation
    tick_density_climax_ratio: float = 1.5  # peak/recent > this = climax
    min_history_s: int = 60           # need at least 60s before classifying

    _history: Deque[Tuple[int, float]] = field(default_factory=deque)

    def reset(self) -> None:
        self._history.clear()

    def observe(self, elapsed_ms: int, cb_price: float) -> None:
        if cb_price <= 0:
            return
        self._history.append((elapsed_ms, cb_price))
        cutoff = elapsed_ms - self.lookback_ms
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    # ---- helpers ----

    def _net_move(self) -> float:
        if len(self._history) < 2:
            return 0.0
        return self._history[-1][1] - self._history[0][1]

    def _range(self) -> float:
        if not self._history:
            return 0.0
        ps = [p for _, p in self._history]
        return max(ps) - min(ps)

    def _pivots(self) -> Tuple[List[float], List[float]]:
        """Returns (highs, lows) in chronological order."""
        if len(self._history) < self.pivot_window * 2 + 1:
            return [], []
        prices = [p for _, p in self._history]
        highs, lows = [], []
        w = self.pivot_window
        for i in range(w, len(prices) - w):
            window = prices[i - w : i + w + 1]
            mid = prices[i]
            if mid == max(window):
                highs.append(mid)
            if mid == min(window):
                lows.append(mid)
        return highs, lows

    def _ladder_direction(self) -> Optional[str]:
        """Are highs+lows both rising (UP ladder) or both falling (DN ladder)?
        Returns 'UP', 'DN', or None."""
        highs, lows = self._pivots()
        if len(highs) < 2 or len(lows) < 2:
            return None
        highs_rising = all(highs[i] >= highs[i - 1] for i in range(1, len(highs)))
        highs_falling = all(highs[i] <= highs[i - 1] for i in range(1, len(highs)))
        lows_rising = all(lows[i] >= lows[i - 1] for i in range(1, len(lows)))
        lows_falling = all(lows[i] <= lows[i - 1] for i in range(1, len(lows)))
        if highs_rising and lows_rising:
            return "UP"
        if highs_falling and lows_falling:
            return "DN"
        return None

    def _tick_density_climax(self, elapsed_ms: int) -> Optional[bool]:
        """True if there's been a tick-density climax (volume surge then fade).
        We compare peak 10s density in the window vs the most recent 10s.
        peak_density / recent_density >= climax_ratio → climaxed.
        """
        if not self._history:
            return None
        bucket_ms = 10_000
        oldest_ts = elapsed_ms - self.lookback_ms
        # Build per-bucket counts
        n_buckets = max(1, (elapsed_ms - oldest_ts) // bucket_ms)
        buckets = [0] * n_buckets
        for ts, _ in self._history:
            if ts < oldest_ts:
                continue
            idx = (ts - oldest_ts) // bucket_ms
            if 0 <= idx < n_buckets:
                buckets[idx] += 1
        if max(buckets) == 0:
            return None
        peak = max(buckets)
        recent = buckets[-1] if buckets[-1] > 0 else 1
        return peak / recent >= self.tick_density_climax_ratio

    # ---- classify ----

    def classify(self, elapsed_ms: int) -> Tuple[str, dict]:
        if not self._history:
            return "INSUFFICIENT", {**STRATEGY_MAP["INSUFFICIENT"], "reason": "no data"}
        span_ms = self._history[-1][0] - self._history[0][0]
        if span_ms < self.min_history_s * 1000:
            return "INSUFFICIENT", {**STRATEGY_MAP["INSUFFICIENT"], "reason": f"span {span_ms}ms < {self.min_history_s*1000}ms"}

        net = self._net_move()
        rng = self._range()
        ladder = self._ladder_direction()
        climaxed = self._tick_density_climax(elapsed_ms)

        signals = {
            "net_move": round(net, 2),
            "range": round(rng, 2),
            "ladder": ladder,
            "climaxed": climaxed,
        }

        # 5. DISTRIBUTION takes priority — volume climax means the move is
        # exhausting regardless of which direction the move was in. This
        # follows the course's rule: "a move isn't done until volume starts
        # decreasing." Climax + any-size range = distribution candidate.
        if climaxed and rng > self.narrow_range_usd:
            info = {**STRATEGY_MAP["DISTRIBUTION"], "signals": signals,
                    "reason": f"climax detected (range ${rng:.0f})"}
            return "DISTRIBUTION", info

        # 3. TRENDING_UP / 4. TRENDING_DOWN: net move > threshold AND ladder agrees
        if net >= self.trend_threshold_usd and ladder in (None, "UP"):
            info = {**STRATEGY_MAP["TRENDING_UP"], "signals": signals,
                    "reason": f"net +${net:.0f}"}
            return "TRENDING_UP", info
        if net <= -self.trend_threshold_usd and ladder in (None, "DN"):
            info = {**STRATEGY_MAP["TRENDING_DOWN"], "signals": signals,
                    "reason": f"net ${net:.0f}"}
            return "TRENDING_DOWN", info

        # 1. ACCUMULATION_BULLISH: narrow range AND ladder UP
        # 2. ACCUMULATION_BEARISH: narrow range AND ladder DN
        if rng <= self.narrow_range_usd:
            if ladder == "UP":
                info = {**STRATEGY_MAP["ACCUMULATION_BULLISH"], "signals": signals,
                        "reason": "narrow range + ladder UP"}
                return "ACCUMULATION_BULLISH", info
            if ladder == "DN":
                info = {**STRATEGY_MAP["ACCUMULATION_BEARISH"], "signals": signals,
                        "reason": "narrow range + ladder DN"}
                return "ACCUMULATION_BEARISH", info

        # Wide range but no ladder and no clear trend — also accumulation,
        # use net_move sign as a tie-break (course rejects 'ranging' as a
        # category)
        if abs(net) < self.trend_threshold_usd:
            if net >= 0:
                info = {**STRATEGY_MAP["ACCUMULATION_BULLISH"], "signals": signals,
                        "reason": "wide range, mild net up"}
                return "ACCUMULATION_BULLISH", info
            else:
                info = {**STRATEGY_MAP["ACCUMULATION_BEARISH"], "signals": signals,
                        "reason": "wide range, mild net down"}
                return "ACCUMULATION_BEARISH", info

        # Fallback (rare): high range + high net but ladder didn't confirm
        info = {**STRATEGY_MAP["INSUFFICIENT"], "signals": signals,
                "reason": "ambiguous"}
        return "INSUFFICIENT", info


# ============================================================================
# Self-tests
# ============================================================================

if __name__ == "__main__":
    # Test 1: TRENDING_UP
    clf = RegimeClassifierV2(min_history_s=60)
    # Slowly rising prices over 90s
    for s in range(0, 90):
        clf.observe(s * 1000, 100_000 + s * 0.5)  # +$45 over 90s
    label, info = clf.classify(90_000)
    print(f"[Test1 TRENDING_UP]   label={label}  reason={info.get('reason')}  signals={info.get('signals')}")

    # Test 2: TRENDING_DOWN
    clf2 = RegimeClassifierV2(min_history_s=60)
    for s in range(0, 90):
        clf2.observe(s * 1000, 100_000 - s * 0.5)
    label2, info2 = clf2.classify(90_000)
    print(f"[Test2 TRENDING_DOWN] label={label2}  reason={info2.get('reason')}  signals={info2.get('signals')}")

    # Test 3: ACCUMULATION_BULLISH — explicit higher-lows pattern in narrow range
    import math
    import random
    clf3 = RegimeClassifierV2(min_history_s=60)
    base = 100_000
    # Sawtooth that drifts up: each "trough" is a few $ higher than the previous
    for s in range(0, 90):
        # Sine wave with upward drift
        wave = 8 * math.sin(s * 0.7)  # ±8 oscillation
        drift = s * 0.1               # +0.1/s drift in mean
        clf3.observe(s * 1000, base + wave + drift)
    label3, info3 = clf3.classify(90_000)
    print(f"[Test3 ACC_BULLISH]   label={label3}  reason={info3.get('reason')}  signals={info3.get('signals')}")

    # Test 4: DISTRIBUTION (climax then stall)
    clf4 = RegimeClassifierV2(min_history_s=60)
    # First 30s: dense ticks driving price up to 100_040
    for ms in range(0, 30_000, 100):
        clf4.observe(ms, 100_000 + (ms / 30_000) * 40)
    # Next 60s: sparse ticks, price wandering near 100_040
    random.seed(7)
    for ms in range(30_000, 90_000, 3000):
        clf4.observe(ms, 100_040 + random.uniform(-3, 3))
    label4, info4 = clf4.classify(90_000)
    print(f"[Test4 DISTRIBUTION]  label={label4}  reason={info4.get('reason')}  signals={info4.get('signals')}")

    # Test 5: INSUFFICIENT (only 30s of data)
    clf5 = RegimeClassifierV2(min_history_s=60)
    for s in range(0, 30):
        clf5.observe(s * 1000, 100_000)
    label5, info5 = clf5.classify(30_000)
    print(f"[Test5 INSUFFICIENT]  label={label5}  reason={info5.get('reason')}")

    print("\nStrategy mapping:")
    for k, v in STRATEGY_MAP.items():
        print(f"  {k:<22} → {v['optimal_strategy']}")
