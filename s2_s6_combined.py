"""
Combined S2 + S6 backtest.

Tests whether S2 (fade $15+ instant) and S6 (momentum $30+ cumulative) are
additive, conflicting, or independent across the same data.

Each strategy fires independently per its rules. Reports:
  - Windows fired by S2 only / S6 only / both / neither
  - Per-strategy P&L, joint P&L, "first-fires-wins" P&L
  - Whether they tend to be opposite direction (potential hedge effect)
"""

import argparse
from pathlib import Path
from statistics import mean

from replay_engine import ReplayEngine, SignalDetector, BookState
from order_engine import PaperOrderEngine, Side, OrderType, OrderStatus, Fill


def run(tick_dir: Path, max_files=None, shares_per_trade=10,
        s6_threshold=30.0, entry_mode="taker_ask"):
    engine = ReplayEngine()
    files = sorted(tick_dir.glob("ticks_*.csv"))
    if max_files:
        files = files[:max_files]

    # Per-window outcomes — track both strategies
    only_s2 = 0
    only_s6 = 0
    both = 0
    neither = 0
    both_same_direction = 0
    both_opposite_direction = 0

    # Per-strategy fills (for solo P&L)
    s2_pnls = []
    s6_pnls = []
    # Combined when both fire — what's the actual realized P&L?
    both_pnls = []
    # "First wins" — only execute whichever triggers first
    first_wins_pnls = []

    for fi, f in enumerate(files):
        cb_detector = SignalDetector(threshold=5.0)
        paper = PaperOrderEngine(starting_usdc=10000.0)

        sd = {
            "p0": None,
            "s2_fired": False,
            "s2_first": False,
            "s2_order": None,
            "s2_fade_dir": None,
            "s6_fired": False,
            "s6_first": False,
            "s6_order": None,
            "s6_direction": None,
            "last_state": None,
        }

        def make_buy(state, token_label, fade_side=False):
            """Place taker buy on given side."""
            if token_label == "UP":
                bid, ask = state.up_bid, state.up_ask
            else:
                bid, ask = state.down_bid, state.down_ask
            if entry_mode == "taker_ask":
                if ask <= 0 or ask >= 1.0:
                    return None
                price = ask
                otype = OrderType.FAK
            else:
                if bid <= 0 or bid >= 1.0:
                    return None
                price = bid
                otype = OrderType.GTC
            return paper.place_order(
                token_id=f"{f.stem}_{token_label}{'_fade' if fade_side else ''}",
                token_label=token_label,
                side=Side.BUY,
                price=price,
                size=shares_per_trade,
                order_type=otype,
            )

        def cb(state: BookState):
            sd["last_state"] = state
            paper.update_book(state)

            # Capture P0
            if sd["p0"] is None and state.cb_price > 0:
                sd["p0"] = state.cb_price

            # S2: CB instantaneous ±$15+
            sig = cb_detector.update(state)
            if sig and abs(sig.cb_move) >= 15 and not sd["s2_fired"]:
                fade_label = "DN" if sig.direction == "UP" else "UP"
                o = make_buy(state, fade_label, fade_side=True)
                if o is not None:
                    sd["s2_fired"] = True
                    sd["s2_order"] = o
                    sd["s2_fade_dir"] = fade_label
                    if not sd["s6_fired"]:
                        sd["s2_first"] = True

            # S6: cumulative ±$30
            if sd["p0"] is not None and not sd["s6_fired"]:
                move = state.cb_price - sd["p0"]
                if abs(move) >= s6_threshold:
                    dir_label = "UP" if move > 0 else "DN"
                    o = make_buy(state, dir_label)
                    if o is not None:
                        sd["s6_fired"] = True
                        sd["s6_order"] = o
                        sd["s6_direction"] = dir_label
                        if not sd["s2_fired"]:
                            sd["s6_first"] = True

        engine.replay(f, cb)

        # End-of-window resolution
        last = sd["last_state"]
        if last is None:
            continue

        def pnl_for(order, token_label):
            if order is None or order.filled_size < 1:
                return None
            final_bid = last.up_bid if token_label == "UP" else last.down_bid
            if final_bid >= 0.95: redemption = 1.00
            elif final_bid <= 0.05: redemption = 0.00
            else: redemption = final_bid
            fee_per_share = (order.total_fees / order.filled_size) if order.filled_size > 0 else 0
            return redemption - order.fill_avg_price - fee_per_share

        s2_pnl = pnl_for(sd["s2_order"], sd["s2_fade_dir"]) if sd["s2_fired"] else None
        s6_pnl = pnl_for(sd["s6_order"], sd["s6_direction"]) if sd["s6_fired"] else None

        # Categorize
        if sd["s2_fired"] and sd["s6_fired"]:
            both += 1
            # Direction analysis
            if sd["s2_fade_dir"] == sd["s6_direction"]:
                both_same_direction += 1
            else:
                both_opposite_direction += 1
            # Combined P&L (both positions held)
            combined_pnl = (s2_pnl or 0) + (s6_pnl or 0)
            both_pnls.append(combined_pnl)
            # First-wins P&L
            if sd["s2_first"]:
                first_wins_pnls.append(s2_pnl if s2_pnl is not None else 0)
            else:
                first_wins_pnls.append(s6_pnl if s6_pnl is not None else 0)
        elif sd["s2_fired"]:
            only_s2 += 1
            if s2_pnl is not None:
                first_wins_pnls.append(s2_pnl)
        elif sd["s6_fired"]:
            only_s6 += 1
            if s6_pnl is not None:
                first_wins_pnls.append(s6_pnl)
        else:
            neither += 1

        if s2_pnl is not None:
            s2_pnls.append(s2_pnl)
        if s6_pnl is not None:
            s6_pnls.append(s6_pnl)

        if (fi + 1) % 50 == 0:
            print(f"  Processed {fi+1}/{len(files)} files...")

    n_windows = len(files)
    days = n_windows * 5 / 60 / 24

    print()
    print("=" * 90)
    print(f"  S2 + S6 COMBINED — entry_mode={entry_mode}, S6_threshold=${s6_threshold}, {n_windows} windows")
    print("=" * 90)
    print()
    print(f"Window classification:")
    print(f"  S2 only fired:        {only_s2}  ({100*only_s2/n_windows:.0f}%)")
    print(f"  S6 only fired:        {only_s6}  ({100*only_s6/n_windows:.0f}%)")
    print(f"  Both fired:           {both}  ({100*both/n_windows:.0f}%)")
    print(f"  Neither fired:        {neither}  ({100*neither/n_windows:.0f}%)")
    print()
    if both > 0:
        print(f"When both fire:")
        print(f"  Same direction:      {both_same_direction}  ({100*both_same_direction/both:.0f}%)")
        print(f"  Opposite direction:  {both_opposite_direction}  ({100*both_opposite_direction/both:.0f}%)")
        print()
    if s2_pnls:
        avg = mean(s2_pnls); net = sum(s2_pnls) * 10
        print(f"S2 solo P&L (all S2 fires, n={len(s2_pnls)}): avg ${avg:+.4f}/share, total ${net:+.2f} at 10sh ({net/days:+.2f}/day)")
    if s6_pnls:
        avg = mean(s6_pnls); net = sum(s6_pnls) * 10
        print(f"S6 solo P&L (all S6 fires, n={len(s6_pnls)}): avg ${avg:+.4f}/share, total ${net:+.2f} at 10sh ({net/days:+.2f}/day)")
    print()
    if both_pnls:
        avg = mean(both_pnls); net = sum(both_pnls) * 10
        print(f"BOTH-FIRE combined P&L (n={len(both_pnls)}): avg ${avg:+.4f}/share, total ${net:+.2f} at 10sh")
    if first_wins_pnls:
        avg = mean(first_wins_pnls); net = sum(first_wins_pnls) * 10
        print(f"FIRST-WINS rule (n={len(first_wins_pnls)}): avg ${avg:+.4f}/share, total ${net:+.2f} at 10sh ({net/days:+.2f}/day)")
    print()
    # Naive sum
    if s2_pnls and s6_pnls:
        naive_sum = sum(s2_pnls) * 10 + sum(s6_pnls) * 10
        print(f"Naive sum (run both, no interaction): ${naive_sum:+.2f} at 10sh = ${naive_sum/days:+.2f}/day")
        print(f"  ↳ Compare to first-wins: ${sum(first_wins_pnls)*10/days:+.2f}/day")
        print(f"  ↳ The difference reveals whether strategies are additive or conflict")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="/home/ubuntu/reports/ticks")
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--shares", type=int, default=10)
    parser.add_argument("--s6-threshold", type=float, default=30.0)
    parser.add_argument("--entry-mode", default="taker_ask", choices=["at_bid", "taker_ask"])
    args = parser.parse_args()
    run(Path(args.dir), args.max_files, args.shares, args.s6_threshold, args.entry_mode)
