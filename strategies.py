"""
Strategy framework — modular strategies that plug into ReplayEngine + OrderEngine.

Each Strategy:
  - Receives BookState on every tick via on_tick()
  - Has a reference to an OrderEngine (paper or live)
  - Decides whether to place orders
  - Manages its own internal state (window-start price, fired flags, held positions)

A StrategyRunner can run multiple strategies in parallel against the same data stream.

Concrete strategies implemented:
  - S2FadeStrategy: CB ±$15+ instant → buy opposite side at ask (taker)
  - S6MomentumStrategy: BTC ±$30 cumulative from window start → buy aligned side
  - S1AlignedHoldStrategy: CB ±$5-9 → buy aligned side at bid (maker) or ask (taker)
  - CombinedS2S6Strategy: composes S2 + S6 (matches the live bot)
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from collections import deque

from replay_engine import BookState
from order_engine import OrderEngine, Side, OrderType, Order, OrderStatus


def dynamic_size(base_shares: int, limit_price: float, min_notional_target: float) -> int:
    """When min_notional_target > 0, scale shares up so size*limit_price >= target.
    Otherwise return base_shares. Used to bypass Polymarket's $1 min order rule on
    cheap-fade entries (e.g., a $0.10 fade with 6 shares = $0.60 < $1 → reject;
    scale to 12 shares = $1.20 → fills)."""
    if min_notional_target <= 0 or limit_price <= 0:
        return base_shares
    need = math.ceil(min_notional_target / limit_price)
    return max(base_shares, need)


@dataclass
class TradeRecord:
    """Strategy-level record of a trade lifecycle."""
    strategy_name: str
    direction: str           # "UP" or "DN"
    token_id: str
    order: Order
    entry_signal_ts_ms: int  # when our signal fired
    exit_price: float = 0.0  # set at end of window
    exit_reason: str = ""
    pnl: float = 0.0
    pnl_per_share: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Base Strategy
# ─────────────────────────────────────────────────────────────────────────────

class Strategy(ABC):
    """
    Abstract base for all strategies.

    Lifecycle:
      __init__(engine, config) — called once
      on_window_start(state, market_info) — called when a new 5-min window begins
      on_tick(state) — called for every tick during the window
      on_window_end(state) — called when window closes; should resolve held positions
    """

    name: str = "BaseStrategy"

    def __init__(self, engine: OrderEngine, **config):
        self.engine = engine
        self.config = config
        self.trades: List[TradeRecord] = []
        # Subclass should override these per-window
        self._reset_window_state()

    def _reset_window_state(self):
        """Reset per-window state. Override to add strategy-specific resets."""
        self._held: List[TradeRecord] = []

    def on_window_start(self, state: BookState, market: dict):
        """Called at the start of a new 5-min window. market dict has up/down token IDs."""
        self._reset_window_state()
        self._market = market

    @abstractmethod
    def on_tick(self, state: BookState) -> None:
        """Process a tick. Place orders via self.engine.place_order(...)."""
        ...

    def on_window_end(self, state: BookState) -> List[TradeRecord]:
        """Called when window ends. Resolve held positions and return finalized trades."""
        for tr in self._held:
            self._resolve_trade(tr, state)
        return self._held

    def _resolve_trade(self, tr: TradeRecord, state: BookState):
        """Use the engine state at window end to compute trade P&L."""
        # Position is held — resolution happens at window close
        if tr.direction == "UP":
            final_bid = state.up_bid
        else:
            final_bid = state.down_bid
        if final_bid >= 0.95:
            redemption = 1.00
        elif final_bid <= 0.05:
            redemption = 0.00
        else:
            redemption = final_bid
        tr.exit_price = redemption
        tr.exit_reason = f"HOLD-RESOLUTION (final_bid=${final_bid:.2f})"
        fee_per_share = (tr.order.total_fees / tr.order.filled_size) if tr.order.filled_size > 0 else 0
        tr.pnl_per_share = redemption - tr.order.fill_avg_price - fee_per_share
        tr.pnl = tr.pnl_per_share * tr.order.filled_size
        self.trades.append(tr)


# ─────────────────────────────────────────────────────────────────────────────
# S2: Fade large instant moves
# ─────────────────────────────────────────────────────────────────────────────

class S2FadeStrategy(Strategy):
    name = "S2-FADE"
    description = "Fade $15+ CB spike within 1.5s → taker ask → hold to resolution"

    def __init__(self, engine: OrderEngine, threshold: float = 15.0,
                 max_gap_ms: int = 1500, shares: int = 7,
                 entry_mode: str = "taker_ask",
                 min_notional_target: float = 0.0):
        super().__init__(engine, threshold=threshold, max_gap_ms=max_gap_ms,
                         shares=shares, entry_mode=entry_mode,
                         min_notional_target=min_notional_target)
        self.threshold = threshold
        self.max_gap_ms = max_gap_ms
        self.shares = shares
        self.entry_mode = entry_mode
        self.min_notional_target = min_notional_target

    def _reset_window_state(self):
        super()._reset_window_state()
        self.prev_cb_price = 0.0
        self.prev_cb_ms = 0
        self.fired = False

    def on_tick(self, state: BookState) -> None:
        if self.fired:
            return
        if state.cb_price <= 0:
            return
        # Check S2 trigger
        if (self.prev_cb_price > 0
                and (state.elapsed_ms - self.prev_cb_ms) <= self.max_gap_ms):
            move = state.cb_price - self.prev_cb_price
            if abs(move) >= self.threshold:
                # Fade — buy OPPOSITE direction
                if move > 0:
                    fade_label = "DN"
                    fade_token = self._market["down"]
                    fade_bid, fade_ask = state.down_bid, state.down_ask
                else:
                    fade_label = "UP"
                    fade_token = self._market["up"]
                    fade_bid, fade_ask = state.up_bid, state.up_ask
                if fade_bid > 0 and fade_ask > 0 and fade_ask < 1:
                    self._place_entry(state, fade_token, fade_label, fade_bid, fade_ask)
        self.prev_cb_price = state.cb_price
        self.prev_cb_ms = state.elapsed_ms

    def _place_entry(self, state: BookState, token_id: str, label: str,
                     bid: float, ask: float):
        # entry_mode:
        #   "taker_ask"    — FAK at exact ask (legacy; rejects when ask moves)
        #   "taker_market" — FAK at ask+$0.05 capped (S7 production)
        #   else (maker)   — GTC join at bid
        if self.entry_mode == "taker_ask":
            price = ask
            otype = OrderType.FAK
        elif self.entry_mode == "taker_market":
            price = round(min(ask + 0.05, 0.98) * 100) / 100
            otype = OrderType.FAK
        else:
            price = bid
            otype = OrderType.GTC
        size = dynamic_size(self.shares, price, getattr(self, "min_notional_target", 0.0))
        try:
            order = self.engine.place_order(
                token_id=token_id, token_label=label,
                side=Side.BUY, price=price, size=size,
                order_type=otype,
            )
            if order.filled_size > 0 or order.status == OrderStatus.OPEN:
                tr = TradeRecord(
                    strategy_name=self.name,
                    direction=label,
                    token_id=token_id,
                    order=order,
                    entry_signal_ts_ms=state.elapsed_ms,
                )
                self._held.append(tr)
                self.fired = True
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# S6: Momentum continuation
# ─────────────────────────────────────────────────────────────────────────────

class S6MomentumStrategy(Strategy):
    name = "S6-MOMENTUM"
    description = "Ride $30+ CB momentum (continuation, not fade) → taker ask → hold to resolution"

    def __init__(self, engine: OrderEngine, threshold: float = 30.0,
                 shares: int = 7, entry_mode: str = "taker_ask"):
        super().__init__(engine, threshold=threshold, shares=shares, entry_mode=entry_mode)
        self.threshold = threshold
        self.shares = shares
        self.entry_mode = entry_mode

    def _reset_window_state(self):
        super()._reset_window_state()
        self.p0 = None
        self.fired = False

    def on_tick(self, state: BookState) -> None:
        if self.fired:
            return
        if state.cb_price <= 0:
            return
        if self.p0 is None:
            self.p0 = state.cb_price
            return
        move = state.cb_price - self.p0
        if abs(move) < self.threshold:
            return
        # Momentum — buy ALIGNED direction
        if move > 0:
            label = "UP"
            token_id = self._market["up"]
            bid, ask = state.up_bid, state.up_ask
        else:
            label = "DN"
            token_id = self._market["down"]
            bid, ask = state.down_bid, state.down_ask
        if bid <= 0 or ask <= 0 or ask >= 1:
            return
        self._place_entry(state, token_id, label, bid, ask)

    def _place_entry(self, state: BookState, token_id: str, label: str,
                     bid: float, ask: float):
        # entry_mode:
        #   "taker_ask"    — FAK at exact ask (legacy; rejects when ask moves)
        #   "taker_market" — FAK at ask+$0.05 capped (S7 production)
        #   else (maker)   — GTC join at bid
        if self.entry_mode == "taker_ask":
            price = ask
            otype = OrderType.FAK
        elif self.entry_mode == "taker_market":
            price = round(min(ask + 0.05, 0.98) * 100) / 100
            otype = OrderType.FAK
        else:
            price = bid
            otype = OrderType.GTC
        size = dynamic_size(self.shares, price, getattr(self, "min_notional_target", 0.0))
        try:
            order = self.engine.place_order(
                token_id=token_id, token_label=label,
                side=Side.BUY, price=price, size=size,
                order_type=otype,
            )
            if order.filled_size > 0 or order.status == OrderStatus.OPEN:
                tr = TradeRecord(
                    strategy_name=self.name,
                    direction=label,
                    token_id=token_id,
                    order=order,
                    entry_signal_ts_ms=state.elapsed_ms,
                )
                self._held.append(tr)
                self.fired = True
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# S1: CB-aligned hold (for completeness)
# ─────────────────────────────────────────────────────────────────────────────

class S1AlignedHoldStrategy(Strategy):
    name = "S1-ALIGNED"
    description = "Aligned CB+book hold: $5-$10 CB move with tight book agreement → taker ask → hold to resolution"

    def __init__(self, engine: OrderEngine, min_move: float = 5.0, max_move: float = 9.99,
                 max_gap_ms: int = 1500, shares: int = 7, entry_mode: str = "taker_ask"):
        super().__init__(engine, min_move=min_move, max_move=max_move,
                         max_gap_ms=max_gap_ms, shares=shares, entry_mode=entry_mode)
        self.min_move = min_move
        self.max_move = max_move
        self.max_gap_ms = max_gap_ms
        self.shares = shares
        self.entry_mode = entry_mode

    def _reset_window_state(self):
        super()._reset_window_state()
        self.prev_cb_price = 0.0
        self.prev_cb_ms = 0

    def on_tick(self, state: BookState) -> None:
        if state.cb_price <= 0:
            return
        if self.prev_cb_price > 0 and (state.elapsed_ms - self.prev_cb_ms) <= self.max_gap_ms:
            move = state.cb_price - self.prev_cb_price
            if self.min_move <= abs(move) <= self.max_move:
                if move > 0:
                    label = "UP"
                    token_id = self._market["up"]
                    bid, ask = state.up_bid, state.up_ask
                else:
                    label = "DN"
                    token_id = self._market["down"]
                    bid, ask = state.down_bid, state.down_ask
                if bid > 0 and ask > 0 and ask < 1:
                    self._place_entry(state, token_id, label, bid, ask)
        self.prev_cb_price = state.cb_price
        self.prev_cb_ms = state.elapsed_ms

    def _place_entry(self, state: BookState, token_id: str, label: str,
                     bid: float, ask: float):
        # entry_mode:
        #   "taker_ask"    — FAK at exact ask (legacy; rejects when ask moves)
        #   "taker_market" — FAK at ask+$0.05 capped (S7 production)
        #   else (maker)   — GTC join at bid
        if self.entry_mode == "taker_ask":
            price = ask
            otype = OrderType.FAK
        elif self.entry_mode == "taker_market":
            price = round(min(ask + 0.05, 0.98) * 100) / 100
            otype = OrderType.FAK
        else:
            price = bid
            otype = OrderType.GTC
        size = dynamic_size(self.shares, price, getattr(self, "min_notional_target", 0.0))
        try:
            order = self.engine.place_order(
                token_id=token_id, token_label=label,
                side=Side.BUY, price=price, size=size,
                order_type=otype,
            )
            if order.filled_size > 0 or order.status == OrderStatus.OPEN:
                tr = TradeRecord(
                    strategy_name=self.name,
                    direction=label,
                    token_id=token_id,
                    order=order,
                    entry_signal_ts_ms=state.elapsed_ms,
                )
                self._held.append(tr)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# S7: Production S2 fade with $18 threshold + price-improvement market FAK
# ─────────────────────────────────────────────────────────────────────────────

class S7FadeStrategy(S2FadeStrategy):
    """S2 fade tuned to the validated S7 production config:
        - threshold: $18 CB move (vs S2 default $15) for stronger WR margin
        - entry_mode: taker_market (FAK at ask+$0.05 cap; price improvement)
        - shares: 6 (current live size)
        - min_notional_target: $1.20 (Option B dynamic shares to bypass $1 reject)

    Optional regime filter (off by default):
        - regime_filter_enabled: when True, classify regime at signal-time and
          skip if current regime is not in allowed_regimes.
        - regime_lookback_s: window of CB history used for autocorrelation.
        - allowed_regimes: tuple of regime labels to fire in.

    The regime classifier uses CB autocorrelation; default allows RANGING only
    per the 434-window backtest where RANGING was the only profitable regime.
    """
    name = "S7-FADE"
    description = "S2 tuned for live: fade $18+ CB spike → FAK ask+$0.05 cap, $1.20 min notional → hold"

    def __init__(self, engine: OrderEngine, shares: int = 6,
                 threshold: float = 18.0, max_gap_ms: int = 1500,
                 min_notional_target: float = 1.20,
                 min_fade_ask: float = 0.10,
                 regime_filter_enabled: bool = False,
                 regime_lookback_s: int = 60,
                 allowed_regimes: tuple = ("RANGING",),
                 min_history_for_regime_s: int = 60,
                 # v2: TA-driven composable filter chain + 5-substate classifier
                 filter_chain=None,
                 regime_classifier_v2=None,
                 v2_allowed_regimes: tuple = ("DISTRIBUTION",)):
        super().__init__(engine, threshold=threshold, max_gap_ms=max_gap_ms,
                         shares=shares, entry_mode="taker_market",
                         min_notional_target=min_notional_target)
        # min_fade_ask matches live btc_s7_fade.py MIN_FADE_ASK constant:
        # "Skip super-cheap fades — proven 0/5 death zone in history (CB moves
        # big enough to push fade <$0.10 don't revert)". Synced 2026-05-28
        # per feedback_sync_live_filters_to_backtest.
        self.min_fade_ask = min_fade_ask
        self.regime_filter_enabled = regime_filter_enabled
        self.regime_lookback_s = regime_lookback_s
        self.allowed_regimes = tuple(allowed_regimes)
        self.min_history_for_regime_s = min_history_for_regime_s
        # v2 — filter chain + classifier (both optional, default None = off)
        self.filter_chain = filter_chain
        self.regime_classifier_v2 = regime_classifier_v2
        self.v2_allowed_regimes = tuple(v2_allowed_regimes)
        # Stats
        self.death_zone_skips = 0
        self.regime_skips = 0
        self.regime_insufficient = 0
        self.regime_fires_by_label: dict = {}
        self.v2_regime_skips = 0
        self.v2_regime_fires_by_label: dict = {}
        self.v2_filter_skips = 0

    def _reset_window_state(self):
        super()._reset_window_state()
        # Rolling CB tick history for regime classification at signal time.
        # deque gives O(1) popleft instead of O(n) for list.pop(0).
        self._cb_hist: deque = deque()
        # Reset v2 filter chain and classifier state (per-window)
        # (we check getattr since this also runs from S2 __init__ pre-attrs)
        fc = getattr(self, "filter_chain", None)
        if fc is not None:
            fc.reset()
        cl = getattr(self, "regime_classifier_v2", None)
        if cl is not None:
            cl.reset()

    def on_tick(self, state: BookState) -> None:
        # Track CB history for the (v1) regime classifier
        if self.regime_filter_enabled and state.cb_price > 0:
            self._cb_hist.append((state.elapsed_ms, state.cb_price))
            cutoff = state.elapsed_ms - (self.regime_lookback_s + 5) * 1000
            while self._cb_hist and self._cb_hist[0][0] < cutoff:
                self._cb_hist.popleft()
        # Feed v2 filter chain + classifier on every tick
        if self.filter_chain is not None and state.cb_price > 0:
            self.filter_chain.observe(state.elapsed_ms, state.cb_price)
        if self.regime_classifier_v2 is not None and state.cb_price > 0:
            self.regime_classifier_v2.observe(state.elapsed_ms, state.cb_price)
        # Delegate the actual signal logic to S2
        super().on_tick(state)

    def _place_entry(self, state: BookState, token_id: str, label: str,
                     bid: float, ask: float):
        # Death-zone floor — matches live MIN_FADE_ASK skip.
        if self.min_fade_ask > 0 and ask < self.min_fade_ask:
            self.death_zone_skips += 1
            self.fired = True   # one-shot — don't re-check this window
            return
        if self.regime_filter_enabled:
            regime = self._classify_current_regime(state)
            self.regime_fires_by_label[regime] = self.regime_fires_by_label.get(regime, 0) + 1
            if regime not in self.allowed_regimes:
                self.regime_skips += 1
                # Mark fired so we don't re-check every tick this window
                self.fired = True
                return
        # v2 — composable filter chain (hard-close, climax, polarity, multi-TF)
        if self.filter_chain is not None:
            # label = direction of the FADE token; the CB SIGNAL direction is
            # opposite. Filter expects CB side, so flip.
            cb_side = "DN" if label == "UP" else "UP"
            ok, reason = self.filter_chain.approve(state.elapsed_ms, cb_side, state.cb_price)
            if not ok:
                self.v2_filter_skips += 1
                self.fired = True
                return
        # v2 — 5-substate regime classifier
        if self.regime_classifier_v2 is not None:
            rg, _info = self.regime_classifier_v2.classify(state.elapsed_ms)
            self.v2_regime_fires_by_label[rg] = self.v2_regime_fires_by_label.get(rg, 0) + 1
            if rg not in self.v2_allowed_regimes:
                self.v2_regime_skips += 1
                self.fired = True
                return
        super()._place_entry(state, token_id, label, bid, ask)

    def _classify_current_regime(self, state: BookState) -> str:
        """Classify regime from in-buffer CB history. Returns 'MOMENTUM',
        'EXHAUSTION', 'RANGING', or 'INSUFFICIENT'."""
        # Need at least min_history_for_regime_s of data
        if not self._cb_hist:
            self.regime_insufficient += 1
            return "INSUFFICIENT"
        span_ms = state.elapsed_ms - self._cb_hist[0][0]
        if span_ms < self.min_history_for_regime_s * 1000:
            self.regime_insufficient += 1
            return "INSUFFICIENT"
        # Bucket into 5s prices
        bucket_s = 5
        bucket_ms = bucket_s * 1000
        lookback_ms = self.regime_lookback_s * 1000
        latest_ts = self._cb_hist[-1][0]
        first_ts = latest_ts - lookback_ms
        i = 0
        prices: list = []
        bucket_boundary = first_ts
        while bucket_boundary <= latest_ts + 1:
            last_p = None
            while i < len(self._cb_hist) and self._cb_hist[i][0] <= bucket_boundary:
                last_p = self._cb_hist[i][1]
                i += 1
            if last_p is None and self._cb_hist:
                last_p = self._cb_hist[0][1]
            if last_p is not None:
                prices.append(last_p)
            bucket_boundary += bucket_ms
        if len(prices) < 4:
            self.regime_insufficient += 1
            return "INSUFFICIENT"
        # Log returns
        import math
        returns: list = []
        for k in range(1, len(prices)):
            if prices[k - 1] > 0 and prices[k] > 0:
                returns.append(math.log(prices[k] / prices[k - 1]))
        if len(returns) < 3:
            self.regime_insufficient += 1
            return "INSUFFICIENT"
        # Lag-1 autocorrelation
        n = len(returns)
        mean = sum(returns) / n
        num = sum((returns[k] - mean) * (returns[k - 1] - mean) for k in range(1, n))
        den = sum((r - mean) ** 2 for r in returns)
        ac = num / den if den > 0 else 0.0
        if ac > 0.25:
            return "MOMENTUM"
        if ac < -0.25:
            return "EXHAUSTION"
        return "RANGING"


# ─────────────────────────────────────────────────────────────────────────────
# Composite: S2 + S6 (mirrors the live bot)
# ─────────────────────────────────────────────────────────────────────────────

class CombinedS2S6Strategy(Strategy):
    name = "S2+S6-COMBINED"
    description = "Fire on EITHER S2-fade OR S6-momentum signal — whichever triggers first"

    def __init__(self, engine: OrderEngine, shares: int = 7, entry_mode: str = "taker_ask"):
        super().__init__(engine, shares=shares, entry_mode=entry_mode)
        self.s2 = S2FadeStrategy(engine, shares=shares, entry_mode=entry_mode)
        self.s6 = S6MomentumStrategy(engine, shares=shares, entry_mode=entry_mode)

    def _reset_window_state(self):
        super()._reset_window_state()
        # Reset both sub-strategies; their state is tracked separately
        # But trades accumulate to ours

    def on_window_start(self, state: BookState, market: dict):
        super().on_window_start(state, market)
        self.s2.on_window_start(state, market)
        self.s6.on_window_start(state, market)

    def on_tick(self, state: BookState) -> None:
        self.s2.on_tick(state)
        self.s6.on_tick(state)
        # Merge held trades up
        for tr in self.s2._held:
            if tr not in self._held:
                self._held.append(tr)
        for tr in self.s6._held:
            if tr not in self._held:
                self._held.append(tr)

    def on_window_end(self, state: BookState) -> List[TradeRecord]:
        # Resolve from this composite (the held lists are already merged)
        return super().on_window_end(state)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy runner for backtests
# ─────────────────────────────────────────────────────────────────────────────

class StrategyRunner:
    """
    Drives one or more Strategy instances against a ReplayEngine + OrderEngine.

    Usage:
      engine = PaperOrderEngine(...)
      runner = StrategyRunner(engine)
      runner.add_strategy(CombinedS2S6Strategy(engine, shares=7))
      runner.run_tick_files([...])
      runner.report()
    """

    def __init__(self, engine: OrderEngine):
        self.engine = engine
        self.strategies: List[Strategy] = []

    def add_strategy(self, strategy: Strategy):
        self.strategies.append(strategy)

    def run_tick_files(self, tick_files, market_lookup=None, trades_dir=None):
        """
        market_lookup: optional callable (window_start_ts) -> {"up": token_id, "down": token_id}.
        If None, uses placeholder labels (paper-only mode).
        trades_dir: optional Path to trades_<window>.csv files. When provided AND
                    engine.use_trade_events is True, uses replay_with_trades for
                    honest maker queue advancement.
        """
        from replay_engine import ReplayEngine
        from pathlib import Path
        replay = ReplayEngine()
        use_trades = (trades_dir is not None
                      and getattr(self.engine, "use_trade_events", False))
        for f in tick_files:
            # Window-start placeholder market — strategies need up/down token ids to differentiate
            window_ts = 0
            try:
                window_ts = int(str(f.stem).split("_")[1])
            except Exception:
                pass
            market = market_lookup(window_ts) if market_lookup else {
                "up": f"{f.stem}_UP",
                "down": f"{f.stem}_DN",
            }
            # Notify strategies of window start
            first_state = {"s": None, "last": None}

            def _on_tick(tick):
                if first_state["s"] is None:
                    first_state["s"] = tick
                    for s in self.strategies:
                        s.on_window_start(tick, market)
                first_state["last"] = tick
                if hasattr(self.engine, "update_book"):
                    self.engine.update_book(tick)
                for s in self.strategies:
                    s.on_tick(tick)

            def _on_trade(trade):
                if hasattr(self.engine, "on_trade"):
                    self.engine.on_trade(trade)

            if use_trades:
                replay.replay_with_trades(f, _on_tick, _on_trade, Path(trades_dir))
            else:
                for tick in replay.iter_ticks(f):
                    _on_tick(tick)

            # Window end
            if first_state["last"] is not None:
                for s in self.strategies:
                    s.on_window_end(first_state["last"])

    def report(self):
        from statistics import mean
        for s in self.strategies:
            n = len(s.trades)
            if n == 0:
                print(f"{s.name}: 0 trades")
                continue
            pnls = [t.pnl_per_share for t in s.trades]
            wins = sum(1 for p in pnls if p > 0.001)
            losses = sum(1 for p in pnls if p < -0.001)
            wr = 100 * wins / max(1, wins + losses)
            total = sum(t.pnl for t in s.trades)
            print(f"{s.name}: n={n} WR={wr:.0f}% total_pnl=${total:+.2f} "
                  f"avg_pnl/share=${mean(pnls):+.4f}")
