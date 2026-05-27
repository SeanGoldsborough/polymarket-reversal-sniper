"""
Stochastic RSI K/D crossover strategy on 1-min CB closes.

Settings (per user spec):
  - RSI Length = 2 (source = Close)
  - Stochastic Length = 2
  - K Smoothing = 2 (SMA)
  - D Smoothing = 2 (SMA)

Rules:
  - At each 1-min boundary inside a 5-min window (60s, 120s, 180s, 240s)
  - Compute K and D for the just-closed minute bar (using rolling history
    that persists ACROSS windows, so warmup is satisfied)
  - If K crosses ABOVE D in that bar → buy UP (FAK at ask)
  - If K crosses BELOW D in that bar → buy DOWN (FAK at ask)
  - Hold to window resolution ($1.00 if won, $0.00 if lost)
  - Multiple positions held simultaneously OK; resolve at end of each window
"""
import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean
from collections import defaultdict, deque

from replay_engine import ReplayEngine, BookState
from order_engine import PaperOrderEngine, Side, OrderType


SHARES = 10
LATENCY_MS = 1500
RSI_LEN = 2
STOCH_LEN = 2
K_LEN = 2
D_LEN = 2
MINUTE_MARKS = [60, 120, 180, 240]  # signals at end of bars 1-4 (bar 5 ends at window end)


def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def crypto_fee(price, sh):
    return 0.07 * price * (1 - price) * sh


class StochRSI:
    """Computes K and D one bar at a time. Returns (K, D) or (None, None) if warmup."""
    def __init__(self, rsi_len=2, stoch_len=2, k_len=2, d_len=2):
        self.rsi_len = rsi_len
        self.stoch_len = stoch_len
        self.k_len = k_len
        self.d_len = d_len
        self.closes = deque(maxlen=rsi_len + 1)  # need prev + last for diff
        self.gains = deque(maxlen=rsi_len)
        self.losses = deque(maxlen=rsi_len)
        self.rsi_vals = deque(maxlen=stoch_len)
        self.stoch_vals = deque(maxlen=k_len)
        self.k_vals = deque(maxlen=d_len)

    def update(self, close):
        if self.closes:
            diff = close - self.closes[-1]
            self.gains.append(max(diff, 0))
            self.losses.append(max(-diff, 0))
        self.closes.append(close)

        # RSI
        if len(self.gains) < self.rsi_len:
            return None, None
        avg_gain = mean(self.gains)
        avg_loss = mean(self.losses)
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - 100 / (1 + rs)
        self.rsi_vals.append(rsi)

        # Stoch of RSI
        if len(self.rsi_vals) < self.stoch_len:
            return None, None
        lo = min(self.rsi_vals)
        hi = max(self.rsi_vals)
        if hi == lo:
            stoch = 50.0  # flat — neutral
        else:
            stoch = (rsi - lo) / (hi - lo) * 100
        self.stoch_vals.append(stoch)

        # K = SMA(stoch, K_len)
        if len(self.stoch_vals) < self.k_len:
            return None, None
        k = mean(self.stoch_vals)
        self.k_vals.append(k)

        # D = SMA(K, D_len)
        if len(self.k_vals) < self.d_len:
            return k, None
        d = mean(self.k_vals)
        return k, d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default="/home/ubuntu/reports/ticks")
    ap.add_argument("--resolutions", default="/home/ubuntu/reports/resolutions.json")
    ap.add_argument("--max-files", type=int, default=None)
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    try:
        with open(args.resolutions) as f:
            resolutions = {int(k): v for k, v in json.load(f).items()}
    except FileNotFoundError:
        resolutions = {}

    files = sorted(Path(args.ticks).glob("ticks_*.csv*"))
    if args.max_files:
        files = files[:args.max_files]
    days = len(files) * 5 / 60 / 24
    print(f"Stoch RSI K/D crossover: {len(files)} windows ({days:.1f} days)", flush=True)
    print(f"Config: RSI={RSI_LEN}, Stoch={STOCH_LEN}, K={K_LEN}, D={D_LEN}, "
          f"SHARES={SHARES}, latency={LATENCY_MS}ms", flush=True)
    print(flush=True)

    # Indicator persists across windows
    stoch_rsi = StochRSI(RSI_LEN, STOCH_LEN, K_LEN, D_LEN)
    prev_k = None
    prev_d = None

    all_trades = []  # global list across windows

    for fi, f in enumerate(files):
        window_start = int(f.stem.split("_")[1])
        engine = PaperOrderEngine(starting_usdc=10000, taker_latency_ms=LATENCY_MS,
                                  min_notional_usdc=0)
        open_positions = []
        minute_signal_done = set()
        last_state = {"s": None, "minute_close": {}}

        # Sample CB price at end of each minute bar for this window
        def cb(state: BookState):
            nonlocal prev_k, prev_d
            last_state["s"] = state
            engine.update_book(state)
            sec = state.elapsed_ms / 1000.0

            # Track latest CB price per minute (so we have the "close" at minute end)
            current_minute = int(sec // 60)  # 0-4 for minutes within window
            if state.cb_price > 0:
                last_state["minute_close"][current_minute] = state.cb_price

            # At each minute boundary (after the bar closes), compute K/D + signal
            for m_end in MINUTE_MARKS:
                if m_end in minute_signal_done:
                    continue
                if sec < m_end:
                    continue
                # Bar (m_end/60 - 1) just closed
                bar_idx = m_end // 60 - 1  # 0=minute 0-60s, 1=60-120s, etc.
                if bar_idx not in last_state["minute_close"]:
                    minute_signal_done.add(m_end)
                    continue
                close = last_state["minute_close"][bar_idx]
                k, d = stoch_rsi.update(close)
                minute_signal_done.add(m_end)
                if k is None or d is None:
                    continue
                # Crossover detection
                if prev_k is not None and prev_d is not None:
                    if prev_k <= prev_d and k > d:
                        # Bullish: buy UP
                        side_label = "UP"
                        ask = state.up_ask
                        token = f"{f.stem}_UP"
                    elif prev_k >= prev_d and k < d:
                        # Bearish: buy DOWN
                        side_label = "DN"
                        ask = state.down_ask
                        token = f"{f.stem}_DN"
                    else:
                        prev_k, prev_d = k, d
                        continue
                    if 0 < ask < 1:
                        try:
                            o = engine.place_order(
                                token_id=token, token_label=side_label,
                                side=Side.BUY, price=min(ask + 0.05, 0.98),
                                size=SHARES, order_type=OrderType.FAK,
                            )
                            open_positions.append({
                                "entry_order": o,
                                "minute_end": m_end,
                                "direction": side_label,
                                "entry_price": None,
                                "k": k, "d": d, "prev_k": prev_k, "prev_d": prev_d,
                            })
                        except Exception:
                            pass
                prev_k, prev_d = k, d

        rep = ReplayEngine()
        rep.replay(f, cb)

        # Resolve all open positions at window-end
        res = resolutions.get(window_start)
        if res and res.get("resolved"):
            up_won = res.get("up_winning")
        else:
            up_won = None

        for pos in open_positions:
            o = pos["entry_order"]
            if o.filled_size < 1:
                pos["exit_reason"] = "NOFILL"
                continue
            pos["entry_price"] = o.fill_avg_price
            if up_won is not None:
                won = (pos["direction"] == "UP" and up_won) or (pos["direction"] == "DN" and not up_won)
                exit_price = 1.00 if won else 0.00
            else:
                last = last_state["s"]
                if last:
                    final_bid = last.up_bid if pos["direction"] == "UP" else last.down_bid
                    exit_price = 1.00 if final_bid >= 0.95 else (0.00 if final_bid <= 0.05 else final_bid)
                else:
                    exit_price = 0.0
            sh = o.filled_size
            entry_fee = crypto_fee(pos["entry_price"], sh)
            # No exit fee — held to resolution = market settles, not a trade
            pnl = (exit_price - pos["entry_price"]) * sh - entry_fee
            pos["pnl"] = pnl
            pos["exit_price"] = exit_price
            pos["shares"] = sh
            pos["exit_reason"] = "WIN" if pnl > 0.001 else ("LOSS" if pnl < -0.001 else "SCRATCH")
            all_trades.append(pos)

        if (fi + 1) % 20 == 0:
            print(f"  {fi+1}/{len(files)} windows, {len(all_trades)} trades so far", flush=True)

    report(all_trades, days)


def report(trades, days):
    filled = [t for t in trades if t.get("pnl") is not None]
    print(flush=True)
    print(f"=== STOCH RSI K/D CROSSOVER ({len(filled)} fills) ===", flush=True)
    if not filled:
        print("No trades.", flush=True)
        return

    by_dir = defaultdict(list)
    for t in filled:
        by_dir[t["direction"]].append(t)

    print(flush=True)
    print(f"{'Direction':<10} {'n':>5} {'pct':>5} {'WR':>5} {'avg PnL':>10} {'total':>10}",
          flush=True)
    print("-" * 60, flush=True)
    for direction in ["UP", "DN"]:
        bucket = by_dir.get(direction, [])
        if not bucket:
            continue
        pnls = [t["pnl"] for t in bucket]
        wins = sum(1 for p in pnls if p > 0.001)
        losses = sum(1 for p in pnls if p < -0.001)
        wr = 100 * wins / max(1, wins + losses)
        pct = 100 * len(bucket) / max(1, len(filled))
        print(f"{direction:<10} {len(bucket):>5} {pct:>4.0f}% {wr:>4.0f}% "
              f"${mean(pnls):>+8.3f} ${sum(pnls):>+8.2f}", flush=True)

    # Overall
    pnls = [t["pnl"] for t in filled]
    wins = sum(1 for p in pnls if p > 0.001)
    losses = sum(1 for p in pnls if p < -0.001)
    wlo, whi = wilson_ci(wins, wins + losses)
    total = sum(pnls)
    print("-" * 60, flush=True)
    print(f"OVERALL: n={len(filled)} W={wins} L={losses}  "
          f"WR={wins/max(1,wins+losses)*100:.0f}% "
          f"CI [{wlo*100:.0f}% — {whi*100:.0f}%]", flush=True)
    print(f"Total PnL: ${total:+.2f}  /day=${total/max(0.01,days):+.2f}",
          flush=True)


if __name__ == "__main__":
    main()
