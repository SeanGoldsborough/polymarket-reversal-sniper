"""
Order Execution Module — Phase 2

A unified order interface with two backends:
  - PaperOrderEngine: simulates fills against tick data fed by ReplayEngine
  - LiveOrderEngine: wraps Polymarket CLOB API (not implemented yet — interface defined)

Design philosophy:
  - Strategies call engine.place_order(...) regardless of mode
  - Paper mode does CONSERVATIVE fill simulation (won't over-state profitability)
  - Tracks: open orders, positions, USDC balance, fills
  - Fill callbacks let strategies react to events
  - All fills include timing for later latency analysis

What this DOES simulate (paper mode):
  - Maker GTC fills when bid/ask crosses our price level (conservative)
  - Queue position estimate from bid/ask size at our level when we placed
  - Partial fills based on available size
  - Settlement delay (1-3s for buys before token is sellable)
  - Taker fees per Polymarket fee curve
  - Maker fees: $0 (Polymarket has no maker fee)

What this does NOT simulate yet (TODO):
  - Adverse selection beyond what the conservative fill rule captures
  - Per-tick trade events (we only see book updates, not trades)
  - Cancel race conditions (if order is filling as we cancel)
"""

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional, Dict
from abc import ABC, abstractmethod

from replay_engine import BookState


class OrderType(Enum):
    GTC = "GTC"   # Good Till Cancelled — maker order, rests on book
    FOK = "FOK"   # Fill Or Kill — taker, fills entire size at limit or cancels
    FAK = "FAK"   # Fill And Kill — taker, fills what's available at limit, cancels rest


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"  # partially filled, still has open size
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass
class Order:
    id: str
    token_id: str            # the token (UP or DN) we're trading
    token_label: str         # "UP" or "DN" for readability
    side: Side
    price: float
    size: float              # original requested size
    order_type: OrderType
    placed_at_ms: int        # when placed (window-elapsed-ms)
    status: OrderStatus = OrderStatus.OPEN
    filled_size: float = 0.0
    fill_avg_price: float = 0.0
    total_fees: float = 0.0
    # Paper-only metadata:
    queue_position: float = 0.0  # estimated size ahead of us when placed
    last_seen_size_at_price: float = 0.0  # bid/ask size at our price, latest observation
    mid_at_placement: float = 0.0       # midpoint of our side at order placement
    mid_at_fill: float = 0.0            # midpoint at first fill (for adverse-selection)
    fill_first_at_ms: int = 0           # first fill timestamp


@dataclass
class Fill:
    order_id: str
    token_id: str
    token_label: str
    side: Side
    size: float
    price: float
    fee: float
    ts_ms: int
    mid_at_fill: float = 0.0      # for adverse-selection measurement
    # adverse_cost at fill = (our fill price - mid_at_fill) for BUY, (mid_at_fill - fill price) for SELL
    # Positive = we paid worse than fair value
    # Computed externally; not stored here directly.


def taker_fee(price: float, size: float) -> float:
    """Polymarket taker fee: 2.2% of min(price, 1-price) per share."""
    return 0.022 * min(price, 1 - price) * size


# ─────────────────────────────────────────────────────────────────────────────
# Abstract interface
# ─────────────────────────────────────────────────────────────────────────────

class OrderEngine(ABC):
    @abstractmethod
    def place_order(self, token_id: str, token_label: str, side: Side,
                    price: float, size: float, order_type: OrderType) -> Order:
        """Place an order. Returns the Order (which may already be filled/rejected)."""

    @abstractmethod
    def cancel(self, order_id: str) -> bool:
        """Attempt to cancel. Returns True if successfully cancelled (False if already filled/expired)."""

    @abstractmethod
    def cancel_all(self) -> int:
        """Cancel all open orders. Returns number successfully cancelled."""

    @abstractmethod
    def get_open_orders(self) -> List[Order]:
        ...

    @abstractmethod
    def get_position(self, token_id: str) -> float:
        """Shares held of that token."""

    @abstractmethod
    def get_usdc_balance(self) -> float:
        ...

    def on_fill(self, callback: Callable[[Fill], None]) -> None:
        if not hasattr(self, "_fill_callbacks"):
            self._fill_callbacks = []
        self._fill_callbacks.append(callback)

    def _emit_fill(self, fill: Fill) -> None:
        for cb in getattr(self, "_fill_callbacks", []):
            cb(fill)


# ─────────────────────────────────────────────────────────────────────────────
# Paper implementation
# ─────────────────────────────────────────────────────────────────────────────

# Settlement delay for buys: how long until the bought token is available for selling
SETTLEMENT_DELAY_MS = 1500


class PaperOrderEngine(OrderEngine):
    """
    Simulates fills against a replayed book.

    Call .update_book(state) on every tick from the replay engine. The engine
    will check all open orders for fills using a conservative rule:
      - Maker BUY at $X fills when current_bid drops to $X or below
        (i.e. the bid book has moved through our level — sellers crossed it)
      - Maker SELL at $X fills when current_ask rises to $X or above
        (i.e. the ask book moved through our level — buyers crossed it)
      - Partial fills based on size at our level at the moment of fill
      - Taker FOK: fills entire size at current ask (BUY) or bid (SELL) if size available
      - Taker FAK: fills what's available, cancels remainder
    """

    def __init__(self, starting_usdc: float = 100.0, verbose: bool = False):
        self.usdc = starting_usdc
        self.positions: Dict[str, float] = {}  # token_id -> shares held
        self.pending_settlement: List[Fill] = []  # buys waiting to settle
        self.open_orders: Dict[str, Order] = {}
        self._fill_callbacks: List[Callable[[Fill], None]] = []
        self._last_state: Optional[BookState] = None
        self.verbose = verbose
        self.realized_pnl = 0.0  # sum of all fill cash flows (entries are negative, exits positive)
        self.total_fills = 0
        self.total_fees = 0.0

    # ── Public API ─────────────────────────────────────────────────────────

    def place_order(self, token_id: str, token_label: str, side: Side,
                    price: float, size: float, order_type: OrderType) -> Order:
        if self._last_state is None:
            raise RuntimeError("place_order called before update_book — engine has no market state")

        s = self._last_state
        # Compute mid at placement for adverse-selection later
        if token_label == "UP":
            bid, ask = s.up_bid, s.up_ask
        else:
            bid, ask = s.down_bid, s.down_ask
        mid_at_placement = (bid + ask) / 2 if (bid > 0 and ask > 0) else 0.0

        order = Order(
            id=str(uuid.uuid4())[:8],
            token_id=token_id,
            token_label=token_label,
            side=side,
            price=price,
            size=size,
            order_type=order_type,
            placed_at_ms=s.elapsed_ms,
            mid_at_placement=mid_at_placement,
        )

        if order_type in (OrderType.FOK, OrderType.FAK):
            # Taker — try to fill immediately
            self._try_taker_fill(order)
            if order.status == OrderStatus.OPEN:
                # FOK that couldn't fully fill is rejected
                order.status = OrderStatus.REJECTED
            return order

        # GTC: store and check for fill on next book update
        self._record_queue_position(order)
        # Check fill against CURRENT state in case our price already crosses
        if self._would_fill_immediately(order):
            self._fill_gtc_to_size(order, order.size, self._last_state)
            if order.filled_size == order.size:
                order.status = OrderStatus.FILLED
                return order
        self.open_orders[order.id] = order
        return order

    def cancel(self, order_id: str) -> bool:
        order = self.open_orders.get(order_id)
        if not order:
            return False
        order.status = OrderStatus.CANCELLED
        del self.open_orders[order_id]
        return True

    def cancel_all(self) -> int:
        n = len(self.open_orders)
        for o in list(self.open_orders.values()):
            o.status = OrderStatus.CANCELLED
        self.open_orders.clear()
        return n

    def get_open_orders(self) -> List[Order]:
        return list(self.open_orders.values())

    def get_position(self, token_id: str) -> float:
        # Apply any settled buys
        self._settle_pending()
        return self.positions.get(token_id, 0.0)

    def get_usdc_balance(self) -> float:
        return self.usdc

    # ── Book update — drives fill simulation ─────────────────────────────

    def update_book(self, state: BookState):
        """Called on every tick from the replay engine."""
        self._last_state = state
        self._settle_pending()
        # Check each open order for fills against this state
        for order_id in list(self.open_orders.keys()):
            order = self.open_orders.get(order_id)
            if not order:
                continue
            self._check_gtc_for_fill(order, state)

    # ── Internal fill mechanics ─────────────────────────────────────────

    def _settle_pending(self):
        if self._last_state is None:
            return
        # Buys that have settled (placed_at + delay <= now) become available shares
        now_ms = self._last_state.elapsed_ms
        still_pending = []
        for fill in self.pending_settlement:
            if now_ms >= fill.ts_ms + SETTLEMENT_DELAY_MS:
                self.positions[fill.token_id] = self.positions.get(fill.token_id, 0.0) + fill.size
            else:
                still_pending.append(fill)
        self.pending_settlement = still_pending

    def _record_queue_position(self, order: Order):
        """Estimate queue position based on size at our price level."""
        s = self._last_state
        if order.side == Side.BUY:
            # Buying — we're on the bid side
            bid = s.up_bid if order.token_label == "UP" else s.down_bid
            bid_size = s.up_bid_size if order.token_label == "UP" else s.down_bid_size
            # If we're at or above current bid, we become best bid → queue = 0
            if order.price > bid:
                order.queue_position = 0.0
            elif order.price == bid:
                # Same level, behind existing size
                order.queue_position = bid_size
            else:
                # Below current bid — there's everything above us
                order.queue_position = bid_size  # rough; we won't fill until bid drops
        else:  # SELL
            ask = s.up_ask if order.token_label == "UP" else s.down_ask
            ask_size = s.up_ask_size if order.token_label == "UP" else s.down_ask_size
            if order.price < ask:
                order.queue_position = 0.0
            elif order.price == ask:
                order.queue_position = ask_size
            else:
                order.queue_position = ask_size

    def _would_fill_immediately(self, order: Order) -> bool:
        """If our limit order crosses the spread, it would fill as taker on placement."""
        s = self._last_state
        if order.side == Side.BUY:
            ask = s.up_ask if order.token_label == "UP" else s.down_ask
            return ask > 0 and ask <= order.price
        else:
            bid = s.up_bid if order.token_label == "UP" else s.down_bid
            return bid > 0 and bid >= order.price

    def _check_gtc_for_fill(self, order: Order, state: BookState):
        """
        Conservative GTC fill rule:
          - Maker BUY at $X fills when the bid drops STRICTLY BELOW $X (book moved through us).
            We don't fill just because bid == our price — that means we joined the queue at
            that level. We fill only when the book has clearly moved past our level, indicating
            our queue position has been consumed.
          - Maker SELL at $X fills when the ask rises STRICTLY ABOVE $X.
        Partial fill if available size is smaller than our remaining size.
        """
        if order.side == Side.BUY:
            cur_bid = state.up_bid if order.token_label == "UP" else state.down_bid
            # Skip until bid has moved STRICTLY below our level (book moved through us)
            if cur_bid > 0 and cur_bid < order.price:
                # Fill triggered
                remaining = order.size - order.filled_size
                # Conservative: assume we can fill our remaining size (less queue position)
                fill_size = max(0.0, remaining - order.queue_position * 0.5)  # half of queue assumed taken
                if fill_size <= 0.01:
                    return
                fill_size = min(fill_size, remaining)
                self._fill_gtc_to_size(order, fill_size, state)
                if order.filled_size >= order.size - 0.01:
                    order.status = OrderStatus.FILLED
                    del self.open_orders[order.id]
                else:
                    order.status = OrderStatus.PARTIAL
        else:  # SELL
            cur_ask = state.up_ask if order.token_label == "UP" else state.down_ask
            # Strictly above — book must have moved through our sell level
            if cur_ask > 0 and cur_ask > order.price:
                remaining = order.size - order.filled_size
                fill_size = max(0.0, remaining - order.queue_position * 0.5)
                if fill_size <= 0.01:
                    return
                fill_size = min(fill_size, remaining)
                self._fill_gtc_to_size(order, fill_size, state)
                if order.filled_size >= order.size - 0.01:
                    order.status = OrderStatus.FILLED
                    del self.open_orders[order.id]
                else:
                    order.status = OrderStatus.PARTIAL

    def _fill_gtc_to_size(self, order: Order, fill_size: float, state: BookState):
        """Apply a fill of `fill_size` to a maker order at order.price (no fee)."""
        # Compute mid at fill for adverse-selection measurement
        if order.token_label == "UP":
            bid, ask = state.up_bid, state.up_ask
        else:
            bid, ask = state.down_bid, state.down_ask
        mid_at_fill = (bid + ask) / 2 if (bid > 0 and ask > 0) else 0.0

        fill = Fill(
            order_id=order.id,
            token_id=order.token_id,
            token_label=order.token_label,
            side=order.side,
            size=fill_size,
            price=order.price,
            fee=0.0,  # maker = no fee
            ts_ms=state.elapsed_ms,
            mid_at_fill=mid_at_fill,
        )
        if order.filled_size == 0:
            # First fill — capture for adverse selection tracking
            order.mid_at_fill = mid_at_fill
            order.fill_first_at_ms = state.elapsed_ms
        order.filled_size += fill_size
        order.fill_avg_price = order.price  # all fills at our limit
        if order.side == Side.BUY:
            self.usdc -= fill_size * order.price
            self.pending_settlement.append(fill)  # not available until settlement
        else:
            self.usdc += fill_size * order.price
            self.positions[order.token_id] = self.positions.get(order.token_id, 0.0) - fill_size
        self.total_fills += 1
        self._emit_fill(fill)
        if self.verbose:
            print(f"  [FILL] {order.side.value} {fill_size}sh {order.token_label} @ ${order.price:.3f} (order {order.id})")

    def _try_taker_fill(self, order: Order):
        """For FOK/FAK orders, try to fill immediately against current book."""
        s = self._last_state
        if order.side == Side.BUY:
            ask = s.up_ask if order.token_label == "UP" else s.down_ask
            ask_size = s.up_ask_size if order.token_label == "UP" else s.down_ask_size
            if ask <= 0 or ask > order.price:
                return  # no fill possible at our limit
            available = ask_size
            if order.order_type == OrderType.FOK and available < order.size:
                return  # FOK = all or nothing
            fill_size = min(available, order.size)
            if fill_size <= 0:
                return
            fee = taker_fee(ask, fill_size)
            fill = Fill(
                order_id=order.id, token_id=order.token_id, token_label=order.token_label,
                side=order.side, size=fill_size, price=ask, fee=fee, ts_ms=s.elapsed_ms,
            )
            order.filled_size = fill_size
            order.fill_avg_price = ask
            order.total_fees = fee
            order.status = OrderStatus.FILLED if fill_size >= order.size - 0.01 else OrderStatus.PARTIAL
            self.usdc -= fill_size * ask + fee
            self.pending_settlement.append(fill)
            self.total_fills += 1
            self.total_fees += fee
            self._emit_fill(fill)
        else:  # SELL
            bid = s.up_bid if order.token_label == "UP" else s.down_bid
            bid_size = s.up_bid_size if order.token_label == "UP" else s.down_bid_size
            if bid <= 0 or bid < order.price:
                return
            available = bid_size
            if order.order_type == OrderType.FOK and available < order.size:
                return
            # Need to have the shares — check position (settled only)
            self._settle_pending()
            held = self.positions.get(order.token_id, 0.0)
            if held < order.size and order.order_type == OrderType.FOK:
                return  # can't sell more than we have for FOK
            fill_size = min(available, order.size, held)
            if fill_size <= 0:
                return
            fee = taker_fee(bid, fill_size)
            fill = Fill(
                order_id=order.id, token_id=order.token_id, token_label=order.token_label,
                side=order.side, size=fill_size, price=bid, fee=fee, ts_ms=s.elapsed_ms,
            )
            order.filled_size = fill_size
            order.fill_avg_price = bid
            order.total_fees = fee
            order.status = OrderStatus.FILLED if fill_size >= order.size - 0.01 else OrderStatus.PARTIAL
            self.usdc += fill_size * bid - fee
            self.positions[order.token_id] = held - fill_size
            self.total_fills += 1
            self.total_fees += fee
            self._emit_fill(fill)


# ─────────────────────────────────────────────────────────────────────────────
# Live implementation (wraps py_clob_client_v2)
# ─────────────────────────────────────────────────────────────────────────────

class LiveOrderEngine(OrderEngine):
    """
    Wraps py_clob_client_v2.ClobClient to expose the same interface as PaperOrderEngine.

    Strategy code can switch between paper and live by swapping which engine class
    is instantiated — no other code changes needed.

    Fill detection: polls get_order(order_id) after placement. For taker FOK/FAK,
    fills are reported immediately. For maker GTC, the engine returns OPEN and
    the strategy or a poll loop should check periodically (or use WS user channel
    in a future enhancement).
    """

    def __init__(self, clob_client, signature_type: int = 2, default_token_label_map=None):
        """
        clob_client: an authenticated ClobClient instance
        signature_type: Polymarket signature type for balance/approval calls (typically 2)
        default_token_label_map: optional dict {token_id: "UP" or "DN"} for fill enrichment
        """
        self.client = clob_client
        self.signature_type = signature_type
        self.token_label_map = default_token_label_map or {}
        self._fill_callbacks: List[Callable[[Fill], None]] = []
        # Local cache of orders we placed (for cancel + status tracking)
        self._our_orders: Dict[str, Order] = {}

    def place_order(self, token_id: str, token_label: str, side: Side,
                    price: float, size: float, order_type: OrderType) -> Order:
        # Lazy import to avoid hard dependency at module level
        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType as ClobOrderType, BalanceAllowanceParams
            from py_clob_client_v2.order_builder.constants import BUY, SELL
        except ImportError as e:
            raise RuntimeError(f"py_clob_client_v2 not available: {e}")

        # Map our enums to Polymarket SDK
        clob_side = BUY if side == Side.BUY else SELL
        type_map = {
            OrderType.GTC: ClobOrderType.GTC,
            OrderType.FOK: ClobOrderType.FOK,
            OrderType.FAK: ClobOrderType.FAK,
        }
        clob_type = type_map.get(order_type, ClobOrderType.GTC)

        # Snap price to 2 decimals (Polymarket tick size)
        snapped = round(round(price * 100) / 100, 2)

        order = Order(
            id="",
            token_id=token_id,
            token_label=token_label,
            side=side,
            price=snapped,
            size=size,
            order_type=order_type,
            placed_at_ms=int(__import__("time").time() * 1000),
        )

        try:
            args = OrderArgs(price=snapped, size=int(size), side=clob_side, token_id=token_id)
            signed = self.client.create_order(args)
            resp = self.client.post_order(signed, clob_type)
            order_id = resp.get("orderID", "")
            order.id = order_id
            # For takers, check immediate fill
            if order_type in (OrderType.FOK, OrderType.FAK):
                # Brief settle, then check
                import time as _t
                _t.sleep(0.25)
                self._refresh_from_api(order)
                if order.filled_size >= order.size - 0.01:
                    order.status = OrderStatus.FILLED
                elif order.filled_size > 0:
                    order.status = OrderStatus.PARTIAL
                else:
                    # Polymarket may report null status if filled+settled fast
                    order.status = OrderStatus.REJECTED if order_type == OrderType.FOK else OrderStatus.CANCELLED
            else:
                order.status = OrderStatus.OPEN
                self._our_orders[order_id] = order
        except Exception as e:
            order.status = OrderStatus.REJECTED
            order.fill_avg_price = 0
            # propagate-ish: caller can inspect status
            raise

        return order

    def _refresh_from_api(self, order: Order) -> None:
        """Query the API for current status of `order` and update fields in place."""
        if not order.id:
            return
        try:
            api_order = self.client.get_order(order.id)
        except Exception:
            return
        if not api_order:
            return
        matched = float(api_order.get("size_matched", 0))
        if matched > order.filled_size:
            # New fills since last check — emit event
            new_size = matched - order.filled_size
            fill = Fill(
                order_id=order.id,
                token_id=order.token_id,
                token_label=order.token_label,
                side=order.side,
                size=new_size,
                price=float(api_order.get("price", order.price)),
                fee=0.0,  # Polymarket reports fees separately; not always available
                ts_ms=int(__import__("time").time() * 1000),
            )
            order.filled_size = matched
            order.fill_avg_price = float(api_order.get("price", order.price))
            if order.filled_size == 0:
                pass
            elif order.filled_size >= order.size - 0.01:
                order.status = OrderStatus.FILLED
            else:
                order.status = OrderStatus.PARTIAL
            self._emit_fill(fill)

    def cancel(self, order_id: str) -> bool:
        try:
            self.client.cancel_orders([order_id])
            if order_id in self._our_orders:
                o = self._our_orders[order_id]
                # Re-check status — cancel might have raced with a fill
                self._refresh_from_api(o)
                if o.status != OrderStatus.FILLED:
                    o.status = OrderStatus.CANCELLED
                del self._our_orders[order_id]
            return True
        except Exception:
            return False

    def cancel_all(self) -> int:
        n = 0
        for oid in list(self._our_orders.keys()):
            if self.cancel(oid):
                n += 1
        return n

    def get_open_orders(self) -> List[Order]:
        # Refresh local cache from API
        try:
            from py_clob_client_v2.clob_types import OpenOrderParams
            api_orders = self.client.get_open_orders(OpenOrderParams()) or []
        except Exception:
            return list(self._our_orders.values())
        # Update local cache from API truth
        api_ids = {o.get("id", ""): o for o in api_orders}
        # Drop locally-tracked orders that no longer exist remotely
        for oid in list(self._our_orders.keys()):
            if oid not in api_ids:
                self._refresh_from_api(self._our_orders[oid])
                del self._our_orders[oid]
        return list(self._our_orders.values())

    def get_position(self, token_id: str) -> float:
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams
            params = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=token_id,
                                            signature_type=self.signature_type)
            self.client.update_balance_allowance(params)
            result = self.client.get_balance_allowance(params)
            return int(result.get("balance", "0")) / 1_000_000
        except Exception:
            return 0.0

    def get_usdc_balance(self) -> float:
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams
            params = BalanceAllowanceParams(asset_type="COLLATERAL", token_id="",
                                            signature_type=self.signature_type)
            self.client.update_balance_allowance(params)
            result = self.client.get_balance_allowance(params)
            return int(result.get("balance", "0")) / 1_000_000
        except Exception:
            return 0.0

    def poll_fills(self) -> int:
        """
        Iterate all our open orders, refresh from API. Fires fill callbacks for any new fills.
        Returns the number of newly-detected fill events.
        Call this periodically (e.g., every 100-500ms) for makers awaiting fills.
        """
        before = sum(o.filled_size for o in self._our_orders.values())
        for o in list(self._our_orders.values()):
            self._refresh_from_api(o)
            if o.status == OrderStatus.FILLED:
                del self._our_orders[o.id]
        after = sum(o.filled_size for o in self._our_orders.values())
        return 1 if after != before else 0


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path
    from replay_engine import ReplayEngine

    if len(sys.argv) < 2:
        print("Usage: python order_engine.py <tick_file.csv>")
        sys.exit(1)

    engine = ReplayEngine()
    paper = PaperOrderEngine(starting_usdc=100.0, verbose=True)

    state_dict = {"fills_count": 0, "placed": False, "placed_buy_id": None}

    def on_fill(fill: Fill):
        state_dict["fills_count"] += 1

    paper.on_fill(on_fill)

    def cb(state: BookState):
        paper.update_book(state)
        if not state_dict["placed"] and state.up_bid > 0 and state.up_ask > 0:
            target_price = round(state.up_bid - 0.01, 2)
            if target_price > 0:
                order = paper.place_order(
                    token_id="up_token", token_label="UP",
                    side=Side.BUY, price=target_price, size=10,
                    order_type=OrderType.GTC,
                )
                state_dict["placed_buy_id"] = order.id
                state_dict["placed"] = True
                print(f"[TEST] Placed buy 10sh UP @ ${target_price:.2f} "
                      f"(initial bid=${state.up_bid:.2f}/ask=${state.up_ask:.2f}, "
                      f"queue={order.queue_position:.0f})")

    engine.replay(Path(sys.argv[1]), cb)

    print()
    print(f"=== SMOKE TEST RESULTS ===")
    print(f"Total fills: {paper.total_fills}")
    print(f"Open orders remaining: {len(paper.open_orders)}")
    print(f"Final USDC: ${paper.usdc:.4f}")
    print(f"Final positions: {dict(paper.positions)}")
    print(f"Pending settlement: {len(paper.pending_settlement)}")
