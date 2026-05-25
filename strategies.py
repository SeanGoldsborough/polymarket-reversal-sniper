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

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from collections import deque

from replay_engine import BookState
from order_engine import OrderEngine, Side, OrderType, Order, OrderStatus


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

    def __init__(self, engine: OrderEngine, threshold: float = 15.0,
                 max_gap_ms: int = 1500, shares: int = 7,
                 entry_mode: str = "taker_ask"):
        super().__init__(engine, threshold=threshold, max_gap_ms=max_gap_ms,
                         shares=shares, entry_mode=entry_mode)
        self.threshold = threshold
        self.max_gap_ms = max_gap_ms
        self.shares = shares
        self.entry_mode = entry_mode

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
        try:
            order = self.engine.place_order(
                token_id=token_id, token_label=label,
                side=Side.BUY, price=price, size=self.shares,
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
        try:
            order = self.engine.place_order(
                token_id=token_id, token_label=label,
                side=Side.BUY, price=price, size=self.shares,
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
        try:
            order = self.engine.place_order(
                token_id=token_id, token_label=label,
                side=Side.BUY, price=price, size=self.shares,
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
        - shares: 7 (current live size)
    """
    name = "S7-FADE"

    def __init__(self, engine: OrderEngine, shares: int = 7,
                 threshold: float = 18.0, max_gap_ms: int = 1500):
        super().__init__(engine, threshold=threshold, max_gap_ms=max_gap_ms,
                         shares=shares, entry_mode="taker_market")


# ─────────────────────────────────────────────────────────────────────────────
# Composite: S2 + S6 (mirrors the live bot)
# ─────────────────────────────────────────────────────────────────────────────

class CombinedS2S6Strategy(Strategy):
    name = "S2+S6-COMBINED"

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
