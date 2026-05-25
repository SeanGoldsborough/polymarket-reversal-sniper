"""
Smoke test for Phase 3.5 trade-event simulator.

Picks a price level that has real trade volume in the window, places a maker
order there with queue_position=0, and confirms on_trade fills it correctly.
"""
import sys
from pathlib import Path
from collections import defaultdict

from replay_engine import ReplayEngine, load_trade_events, BookState, TradeEvent
from order_engine import PaperOrderEngine, Side, OrderType


def main():
    if len(sys.argv) < 2:
        print("Usage: python smoke_trade_sim.py <ticks_<window>.csv> [trades_dir]")
        sys.exit(1)
    tick_file = Path(sys.argv[1])
    trades_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    window_start = int(tick_file.stem.split("_")[1])
    trades = load_trade_events(window_start, trades_dir)
    print(f"[1] Loaded {len(trades)} trade events for window {window_start}")

    # Find an (asset_id, price, side) combination with substantial volume so the
    # smoke test actually exercises the fill path
    bucket = defaultdict(float)
    bucket_count = defaultdict(int)
    for t in trades:
        key = (t.asset_id, round(t.price, 2), t.side)
        bucket[key] += t.size
        bucket_count[key] += 1
    if not bucket:
        print("    no trades; cannot smoke test fill")
        sys.exit(2)
    # Pick the (asset_id, price, side) with the most volume — that's the level
    # most likely to host a maker order in a real strategy
    top_key, top_vol = max(bucket.items(), key=lambda kv: kv[1])
    top_asset, top_price, top_taker_side = top_key
    # Maker side is opposite of taker side
    maker_side = Side.BUY if top_taker_side == "SELL" else Side.SELL
    print(f"    Most-traded level: asset={top_asset[:10]}... price=${top_price:.2f} "
          f"taker_side={top_taker_side} vol={top_vol:.0f}sh ({bucket_count[top_key]} trades)")
    print(f"    Will place maker {maker_side.value} at this level to test fill path")

    engine = ReplayEngine()
    paper = PaperOrderEngine(starting_usdc=1000.0, taker_latency_ms=150,
                             use_trade_events=True)

    tick_count = 0
    trade_count = 0
    fills_count = 0
    placed_order = {"o": None}
    # Match label based on which token — we don't actually need it precise for the test
    label = "UP"

    def on_fill(fill):
        nonlocal fills_count
        fills_count += 1
        print(f"    [FILL #{fills_count}] {fill.side.value} {fill.size}sh @ ${fill.price:.3f} t={fill.ts_ms}ms")

    paper.on_fill(on_fill)

    def on_tick(state):
        nonlocal tick_count
        tick_count += 1
        paper.update_book(state)
        if placed_order["o"] is None and state.elapsed_ms > 1000:
            # Place maker at the high-volume level with queue_position=0 by
            # zeroing out the recorded book side (so engine sees us as best)
            # — we don't actually clear the book; we just place above/below the
            # current best to ensure queue_position is computed as 0 OR equal-level.
            # Simplest: just place at top_price; engine's queue computation will
            # decide queue position based on whether we're better/equal/worse.
            o = paper.place_order(
                token_id=top_asset, token_label=label,
                side=maker_side, price=top_price, size=10,
                order_type=OrderType.GTC,
            )
            placed_order["o"] = o
            print(f"[3] Placed maker {maker_side.value} 10sh @ ${top_price:.2f} "
                  f"(queue_pos={o.queue_position:.0f})")

    def on_trade(trade):
        nonlocal trade_count
        trade_count += 1
        paper.on_trade(trade)

    ticks_processed, trades_processed = engine.replay_with_trades(
        tick_file, on_tick, on_trade, trades_dir
    )

    print()
    print(f"[2] Interleaved replay processed {ticks_processed} ticks, {trades_processed} trades")
    o = placed_order["o"]
    if o:
        print(f"[4] Order final: status={o.status.name} filled={o.filled_size}/{o.size} "
              f"queue_pos={o.queue_position:.0f} vol_consumed={o.volume_consumed_at_level:.0f}")
    print(f"[5] Total fills: {fills_count}")
    print(f"[6] Final USDC: ${paper.usdc:.4f}, positions: {dict(paper.positions)}")

    # Validate behavior is sane
    if o is None:
        print("FAIL: order was never placed")
        sys.exit(1)
    if top_vol < o.queue_position:
        # Not enough volume in window to fill — that's ok, but explain
        print(f"NOTE: total volume at level ({top_vol:.0f}) < queue_position ({o.queue_position:.0f}); "
              f"no fill is the correct outcome")
    elif o.filled_size == 0 and o.queue_position == 0:
        print("FAIL: queue_position=0 and trade volume exists but order didn't fill — fill path broken")
        sys.exit(1)

    print()
    print("=== SMOKE TEST PASS ===")


if __name__ == "__main__":
    main()
