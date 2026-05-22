"""
Momentum strategy validator.

For each 5-min window:
  - Record BTC price at window start (P0)
  - Track BTC price during window
  - When BTC reaches P0 + threshold (e.g., +$50): buy UP side
    OR when BTC reaches P0 - threshold: buy DOWN side
  - Only one trade per window (whichever direction triggers first)
  - Hold to window resolution
  - Compute WR + P&L

Tests both maker (at bid) and taker (at ask) entry.
"""

import argparse
from pathlib import Path
from statistics import mean

from replay_engine import ReplayEngine, BookState
from order_engine import PaperOrderEngine, Side, OrderType, OrderStatus, Fill


def run(tick_dir: Path, max_files=None, shares_per_trade=10,
        threshold=50.0, entry_mode="taker_ask"):
    assert entry_mode in ("at_bid", "taker_ask")
    engine = ReplayEngine()
    files = sorted(tick_dir.glob("ticks_*.csv"))
    if max_files:
        files = files[:max_files]

    signals = 0
    filled = 0
    pnls = []
    wins = 0
    losses = 0
    flats = 0
    direction_counts = {"UP": 0, "DN": 0}
    direction_wins = {"UP": 0, "DN": 0}

    for fi, f in enumerate(files):
        paper = PaperOrderEngine(starting_usdc=10000.0)

        state_dict = {
            "p0": None,                 # window-start BTC price
            "triggered": False,         # have we placed our trade yet?
            "trade_direction": None,
            "order": None,
            "last_state": None,
        }

        def cb(state: BookState):
            state_dict["last_state"] = state
            paper.update_book(state)

            # Capture P0 from first non-zero cb_price
            if state_dict["p0"] is None and state.cb_price > 0:
                state_dict["p0"] = state.cb_price
                return

            if state_dict["p0"] is None or state_dict["triggered"]:
                return

            move = state.cb_price - state_dict["p0"]
            if abs(move) < threshold:
                return

            # Trigger! Buy the winning side
            direction = "UP" if move > 0 else "DN"
            if direction == "UP":
                token_label = "UP"
                bid, ask = state.up_bid, state.up_ask
            else:
                token_label = "DN"
                bid, ask = state.down_bid, state.down_ask

            if bid <= 0 or bid >= 1.0:
                return
            if entry_mode == "taker_ask":
                if ask <= 0 or ask >= 1.0:
                    return
                entry_price = ask
                otype = OrderType.FAK
            else:
                entry_price = bid
                otype = OrderType.GTC

            try:
                order = paper.place_order(
                    token_id=f"{f.stem}_{token_label}",
                    token_label=token_label,
                    side=Side.BUY,
                    price=entry_price,
                    size=shares_per_trade,
                    order_type=otype,
                )
                state_dict["triggered"] = True
                state_dict["trade_direction"] = direction
                state_dict["order"] = order
            except Exception:
                pass

        engine.replay(f, cb)

        if not state_dict["triggered"]:
            continue  # no trigger this window — skip

        signals += 1
        order = state_dict["order"]
        direction_counts[state_dict["trade_direction"]] += 1

        if order.filled_size < 1:
            continue  # didn't fill

        filled += 1
        last = state_dict["last_state"]
        final_bid = last.up_bid if state_dict["trade_direction"] == "UP" else last.down_bid

        if final_bid >= 0.95:
            redemption = 1.00
            direction_wins[state_dict["trade_direction"]] += 1
        elif final_bid <= 0.05:
            redemption = 0.00
        else:
            redemption = final_bid

        fee_per_share = (order.total_fees / order.filled_size) if order.filled_size > 0 else 0
        pnl = redemption - order.fill_avg_price - fee_per_share
        pnls.append(pnl)
        if pnl > 0.001: wins += 1
        elif pnl < -0.001: losses += 1
        else: flats += 1

        if (fi + 1) % 50 == 0:
            print(f"  Processed {fi+1}/{len(files)} files...")

    print()
    print("=" * 90)
    print(f"  MOMENTUM VALIDATION — buy winning side when BTC moves ±${threshold} from window start")
    print(f"  Entry mode: {entry_mode} | {shares_per_trade}sh per trade | {len(files)} windows")
    print("=" * 90)
    print()
    print(f"Windows triggered:  {signals}/{len(files)} ({100*signals/max(1,len(files)):.0f}%)")
    print(f"Filled:             {filled}/{signals} ({100*filled/max(1,signals):.0f}%)")
    print(f"  UP trades: {direction_counts['UP']} ({direction_wins['UP']} resolved as wins)")
    print(f"  DN trades: {direction_counts['DN']} ({direction_wins['DN']} resolved as wins)")
    if pnls:
        wr = 100 * wins / max(1, wins + losses)
        avg = mean(pnls)
        total = sum(pnls)
        net_10sh = total * 10
        days = len(files) * 5 / 60 / 24
        per_day = net_10sh / max(0.01, days)
        print(f"WR (P&L > 0):       {wr:.0f}%  ({wins}W/{losses}L/{flats}F)")
        print(f"Avg P&L per trade:  ${avg:+.4f}/share")
        print(f"Total P&L:          ${total:+.4f}/share = ${net_10sh:+.2f} at 10sh")
        print(f"Per-day at 10sh:    ${per_day:+.2f}")
        print(f"Per-day at 100sh:   ${per_day*10:+.2f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="/home/ubuntu/reports/ticks")
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--shares", type=int, default=10)
    parser.add_argument("--threshold", type=float, default=50.0)
    parser.add_argument("--entry-mode", default="taker_ask", choices=["at_bid", "taker_ask"])
    args = parser.parse_args()
    print(f"Threshold: ${args.threshold} | Entry mode: {args.entry_mode}")
    run(Path(args.dir), args.max_files, args.shares, args.threshold, args.entry_mode)
