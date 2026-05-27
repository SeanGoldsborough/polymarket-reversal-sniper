"""
Phase 3.5 validator — re-runs strategy backtests using the trade-event
simulator (use_trade_events=True + replay_with_trades).

Compares taker mode (FAK, latency-aware) vs maker mode (GTC, queue-advanced
by trade events) on the SAME windows so we can see honest maker fill rates.

Run with --mode taker or --mode maker.
"""
import argparse
import sys
from pathlib import Path
from statistics import mean

from replay_engine import ReplayEngine, SignalDetector, BookState, TradeEvent
from order_engine import PaperOrderEngine, Side, OrderType


def snap_price(p):
    return round(round(p * 100) / 100, 2)


def run_s2_threshold(tick_dir, trades_dir, threshold, mode, max_files=None, shares=10):
    engine = ReplayEngine()
    files = sorted(Path(tick_dir).glob("ticks_*.csv*"))
    # In maker mode we can only validate windows that have a matching trade CSV.
    # Filter FIRST so --max-files counts trade-eligible windows, not skipped ones.
    if mode == "maker":
        files = [f for f in files
                 if (Path(trades_dir) / f"trades_{int(f.stem.split('_')[1])}.csv").exists()]
    if max_files:
        files = files[:max_files]

    pnls = []
    wins = losses = filled = signals = 0
    entries = []
    windows_with_trades = 0

    for fi, f in enumerate(files):
        # Skip windows without matching trade CSV when mode=maker
        window_start = int(f.stem.split("_")[1])
        trade_file = Path(trades_dir) / f"trades_{window_start}.csv"
        if mode == "maker" and not trade_file.exists():
            continue
        if trade_file.exists():
            windows_with_trades += 1

        detector = SignalDetector(threshold=5.0)
        paper = PaperOrderEngine(
            starting_usdc=10000.0,
            taker_latency_ms=150,
            use_trade_events=(mode == "maker"),
        )
        trades = []
        last_state = {"s": None}

        def cb(state):
            last_state["s"] = state
            paper.update_book(state)
            sig = detector.update(state)
            if sig and abs(sig.cb_move) >= threshold:
                if sig.direction == "UP":
                    fade_lbl = "DN"
                    fade_bid = state.down_bid
                    fade_ask = state.down_ask
                    asset_id = f"{f.stem}_DN"
                else:
                    fade_lbl = "UP"
                    fade_bid = state.up_bid
                    fade_ask = state.up_ask
                    asset_id = f"{f.stem}_UP"
                if fade_bid <= 0 or fade_bid >= 1.0 or fade_ask <= 0:
                    return
                try:
                    if mode == "taker":
                        bid_price = snap_price(min(fade_ask + 0.05, 0.98))
                        o = paper.place_order(
                            token_id=asset_id, token_label=fade_lbl,
                            side=Side.BUY, price=bid_price, size=shares,
                            order_type=OrderType.FAK,
                        )
                    else:  # maker
                        # Join the bid at current best (queue_position=bid_size).
                        # This is the honest "passive maker" baseline — we
                        # measure how often a non-aggressive maker actually
                        # fills in the window.
                        bid_price = snap_price(fade_bid)
                        o = paper.place_order(
                            token_id=asset_id, token_label=fade_lbl,
                            side=Side.BUY, price=bid_price, size=shares,
                            order_type=OrderType.GTC,
                        )
                    trades.append({"order": o, "dir": fade_lbl, "label": fade_lbl})
                except Exception:
                    pass

        def on_trade_cb(trade):
            paper.on_trade(trade)

        if mode == "maker" and trade_file.exists():
            engine.replay_with_trades(f, cb, on_trade_cb, Path(trades_dir))
        else:
            engine.replay(f, cb)

        last = last_state["s"]
        if last is None:
            continue
        for t in trades:
            signals += 1
            o = t["order"]
            if o.filled_size < 1:
                continue
            filled += 1
            final_bid = last.up_bid if t["dir"] == "UP" else last.down_bid
            if final_bid >= 0.95:
                red = 1.00
            elif final_bid <= 0.05:
                red = 0.00
            else:
                red = final_bid
            fee_ps = (o.total_fees / o.filled_size) if o.filled_size > 0 else 0
            pnl = red - o.fill_avg_price - fee_ps
            pnls.append(pnl)
            entries.append(o.fill_avg_price)
            if pnl > 0.001:
                wins += 1
            elif pnl < -0.001:
                losses += 1

    wr = 100 * wins / max(1, wins + losses)
    avg_entry = mean(entries) if entries else 0
    be_wr = 100 * avg_entry
    margin = wr - be_wr
    fill_rate = 100 * filled / max(1, signals)
    days = len(files) * 5 / 60 / 24
    return {
        "threshold": threshold,
        "windows": len(files),
        "windows_with_trades": windows_with_trades,
        "signals": signals,
        "filled": filled,
        "fill_rate": fill_rate,
        "wr": wr,
        "avg_entry": avg_entry,
        "be_wr": be_wr,
        "margin": margin,
        "total_pnl": sum(pnls),
        "avg_pnl": mean(pnls) if pnls else 0,
        "per_day": sum(pnls) * shares / max(0.01, days),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["taker", "maker"], default="taker")
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--trades", default="/home/ubuntu/reports/trades")
    ap.add_argument("--thresholds", default="15,18,20,25")
    ap.add_argument("--max-files", type=int, default=None)
    args = ap.parse_args()

    print(f"S2 fade — {args.mode.upper()} mode — Phase 3.5 trade-event simulator")
    print()
    print(f"{'Thresh':<7} {'Wins':>9} {'Sig':>5} {'Fld':>5} {'Fill%':>6} "
          f"{'WR':>5} {'BE WR':>7} {'Margin':>8} {'$/sh':>9} {'$/day':>10}")
    print("-" * 88)
    thresholds = [int(t) for t in args.thresholds.split(",")]
    for thresh in thresholds:
        r = run_s2_threshold(args.ticks, args.trades, thresh, args.mode,
                             max_files=args.max_files)
        print(f"${thresh:<6} {r['windows_with_trades']:>9} {r['signals']:>5} "
              f"{r['filled']:>5} {r['fill_rate']:>5.1f}% "
              f"{r['wr']:>4.0f}% {r['be_wr']:>6.0f}%  {r['margin']:>+6.1f}pp "
              f"${r['avg_pnl']:>+7.4f} ${r['per_day']:>+8.2f}")
    print()
    if args.mode == "maker":
        print("Note: 'Wins' col = windows-with-trades (only these contribute to maker fills).")


if __name__ == "__main__":
    main()
