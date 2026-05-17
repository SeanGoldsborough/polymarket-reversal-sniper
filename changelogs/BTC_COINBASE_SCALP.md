# BTC-COINBASE-SCALP Changelog
**File:** `btc_coinbase_scalp.py`
**Strategy:** Scalp Polymarket tokens on Coinbase BTC price signals (>$5 move in 1s)
**Status:** Paper testing (Phase 1)

## 2026-05-05
- Initial creation
- CB websocket monitors BTC/USD for >$5 moves in 1 second
- GTC maker limit buy on corresponding side (0% fee)
- GTC maker TP sell at entry + $0.04 (0% fee)
- Websocket SL monitoring at entry - $0.03 (FAK taker sell)
- Sequential: one trade at a time
- 4s fill timeout, cancel if unfilled
- Entry bounds: $0.15 - $0.85
- No trades after T+280, force exit at T+295
- Realistic paper simulation:
  - 125ms latency on every operation
  - Book depth from websocket determines fills
  - Ask-side depth for buy fills, bid-side depth for TP sells
  - Price re-checked after latency delay (slippage)
  - Taker fee simulated on SL/force-exit sells
  - Partial fill detection and tracking

## 2026-05-10
- **Bounce-wait SL**: When SL triggers, keep TP on book and wait up to 10 seconds for bounce. Exit hierarchy: TP-BOUNCE (if price recovers to TP) > SL-BE (breakeven at entry) > SL (at SL price) > SL-FAK (panic sell). Based on data showing 65% of SL triggers bounce back.
- **Event-driven SL detection**: Websocket triggers SL event instantly when bid updates, instead of 300ms polling. Reduced to 50ms monitoring loop.
- **50ms paper latency**: Changed from 125ms to match actual API benchmark.
- **REMINDER**: Build price-hit-counter approach — track how many times price touches TP/BE/SL before deciding exit.

## 2026-05-13
- **V2: Tiered exit replaces ladder sell** — Split shares into 3 tiers on SL trigger:
  Tier 1: 1/3 shares at entry + $0.01, Tier 2: 1/3 at entry (BE), Tier 3: 1/3 at entry - $0.01.
  Each tier retries $0.01 lower until filled. 50ms fill checks (reactive, not polling).
  Simulation on 106 trades: +$105 vs -$48 with old ladder. Emergency FAK only for unsold remainder.
- **V3: Same tiered exit applied** — replaces old ladder sell + panic floor.
- **V2 scaled to 69 shares** — based on book depth analysis showing consistent fills.
- **V3 skip zone $0.43-$0.55** — mid-price entries had 20% FAK rate, now excluded.

- **V2: 5s bounce wait before tiered exit** — SL triggers, keep TP on book for 5s watching
  for TP-BOUNCE or SL-BE. If no bounce, then tiered exit. Catches 60% of bounces.
- **V4: NEW — Hold until BE, no time limit** — When SL triggers, hold position with TP on
  book until price returns to entry (100% of SL trades eventually return to BE based on data).
  Only force-exits if window is about to resolve. No tiered exit, no ladder, no FAK.
  The ultimate test: if price always returns to BE, this should have zero SL losses.
- **V5: NEW — Tiered TP (sell in 4 chunks at +$0.01/+$0.02/+$0.03/+$0.04)** —
  Instead of all-or-nothing TP at +$0.04, sell 25% at each level. If price only
  reaches +$0.02, captures partial profit on 50% of shares. Unfilled tiers cascade
  down to BE then -$0.01, -$0.02. Simulation: +$100 vs -$110 on same data.
  Max loss per trade ~$1 instead of ~$19.

## 2026-05-14
- **V6: NEW — V4 + volatility gate** — Same hold-until-BE as V4, but only trades when
  CB price shows trending behavior: <4 reversals, >$10 net move, >0.3 directionality
  ratio over 30s window. Filters out choppy markets that caused V4's force-exit losses.
- **V5 bug fixes**: Fixed NameError (min_bid_during), fixed NameError (ws_updates_during_trade).
  V5 tiered TP now recording trades correctly.
- **V1, V2, V3 stopped** — final results: V1 -$26, V2 -$5, V3 +$11.
- **REMINDER (V5)**: Option 3 volatility — track per-window reversal patterns across many
  windows to identify time-of-day trends.

## 2026-05-17
- **V4-LIVE: FIRST LIVE DEPLOYMENT** — V4 strategy with 10 shares, real money.
  Based on 580 paper trades: 62% WR, +$225.67, $4.18/hr.
- **V4-180: Paper test** — V4 with force-exit at T+180 instead of T+295.
  Data shows 100% of BE bounces within 180s. Earlier exit should prevent
  catastrophic force-exit losses that account for all V4 losses.
- V4 original stopped after 580 trades of validation.
