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

## 2026-05-17
- **V4-LIVE first deployment** — 10 shares, real money. First trade: buy filled but TP failed
  (token not settled on-chain). Trade exited at BE via hold-until-BE logic. Lost $4.40 on
  earlier V4-LIVE (0.1) from partial fill bug (Bug 1).
- **V4-LIVE-0.2 deployed** — All 12 bugs fixed:
  - Bug 1: Partial fills detected (any matched > 0), post-cancel recheck, wallet safety
  - Bug 2/3: Cancel verification via post-cancel recheck
  - Bug 4: All bare except:pass replaced with error logging
  - Bug 5: Sell failures retry with FAK at progressively lower prices
  - Bug 7: TP order re-verified in monitoring loop, re-placed if cancelled by exchange
  - Bug 8: Balance allowance retries 3x with verification
  - Bug 9: Websocket staleness detection (10s) + HTTP fallback for bid data
  - Bug 10: Orphaned order cleanup on startup
  - Bug 11: HTTP debug wrapped safely
  - Bug 12: Force-exit at T+180 instead of T+295
- **TP placement fix** — Pre-approve both tokens at window start. TP retries every 100ms
  during 1-3s settlement. Monitors price via websocket during settlement — if price hits
  TP before order is placed, sells manually via FOK. No more blocking gaps.
- **V4-180 paper test** running alongside (same as V4 but T+180 force-exit)
- **V5 fixed** — taker_fee init, try/finally on in_trade, recording trades correctly.
  Paper fill logic relaxed (bid within $0.01 = fill).
- Wallet: $66.65 → $70.68 (partial recovery from first trade BE exit)
- **V4-LIVE-0.2 settlement fix** — TP placement now checks actual on-chain token
  balance via get_token_balance() instead of assuming fill_shares available. Waits
  for settlement up to 3s. Added safe_sell() helper for all sell operations.
  MAX-HOLD sell cancels TP first, waits for shares to unlock, checks balance.

## 2026-05-18
- **Bug 3 REAL FIX** — Cancel verification was broken in production. Old code waited 100ms
  and only checked `size_matched` — too fast for CLOB fill propagation. Cost $5.10 on
  12:25AM trade (orphaned 10 DN shares held to worthless resolution). New code checks
  order `status` field immediately after cancel: if status != cancelled/expired, treat as
  filled and manage position. Wallet balance check as final safety net. No delays added.
- **On-chain reconciliation** — CSV vs bot data revealed bot reported +$4.71 but wallet
  shows -$12.16 total (-$2.88 post-restart at 9:45PM). 3 major losses identified:
  - 12:25AM -$5.10: Bug 3 (orphaned position) — NOW FIXED
  - 3:15AM -$3.36: Hold-until-BE force-exit at $0.20
  - 5:15AM -$3.15: Hold-until-BE force-exit at $0.06
- **Hold-until-BE analysis** — 35 SL-triggered trades: 32 recovered (91%), 3 force-exited.
  Entry gap does NOT predict failure. Hold strategy nets -$7.67 vs -$7.00 for simple SL.
  Under review.
- **Dynamic SL deployed (9:58AM, commit 69271a1)** — Exit hold if bid drops >$0.05 below
  entry. Intended to cut the rare $0.80-$1.00 cliff losses while keeping 91% BE recoveries.
- **Dynamic SL REVERTED (11:20AM, commit 9b5553a)** — 80min live: 6 trades, 1W/4L/1F, bank
  $57.15 → $50.85 (DD 23.6%). Dyn-SL fired on 3 normal hold-to-BE scenarios and sold into
  thin liquidity at $0.07-$0.08 below entry, converting -$0.20 SL events into -$0.70 to
  -$0.80 slippage losses. The thesis was wrong: when bid drops $0.05 fast, the book is
  too thin for clean exit. Kept cancel-status case-sensitivity fix from same commit.
- **V4-LIVE-0.3 — Persistent BE maker + hedge mechanism** — Major rewrite of SL recovery path:
  - Root cause found: 100% of force-exit trades had bid TOUCH entry during hold but failed
    to fill. The cancel-TP → place-FOK round-trip (~200-350ms) was too slow to catch brief
    bid touches at entry. Original V4 design assumed 100% recovery; live data shows ~87%.
  - New flow: on SL trigger, cancel TP and place persistent GTC maker SELL at entry
    (sits in queue, captures any future bid touch). No round-trip latency.
  - Hedge fallback when BE maker won't fill:
    - `CLIFF`: bid drops to entry - $0.08 (data: recovery rate 89% → 29% at this depth)
    - `MODE-A`: BTC moves $50+ adverse from fill within 90s (catches trend-against-us early)
    - `DEADLINE`: T+150 since fill with no BE recovery
  - Hedge mechanism: buy opposite token at ask. UP+DOWN always sums to $1 at resolution,
    so cost basis = entry + opp_ask, redemption guaranteed = $1. Net loss bounded to
    ~$0.10-$0.20 per share (the spread/fee), vs $0.30-$0.50 from FAK ladder dump.
  - Safety: hedge skipped if cost per share > $0.20 (opposite side too wide), falls
    through to legacy force-exit FAK.
  - Counterfactual on 14 historical force-exits: dump P&L -$47.73 → hedge P&L -$17.00,
    estimated savings +$30.73 even with conservative spread assumptions.
  - Tradeoff: TP-BOUNCE (+$0.40 wins) goes to $0 because TP gets cancelled on SL trigger.
    Estimated cost: -$6 across 15 historical TP-BOUNCE trades. Net positive vs hedge gain.
- **V4-LIVE-0.3.1 — Fix BE-MAKER place failure** — First live trade after 0.3 deploy hit
  `not enough balance / allowance: balance: 0` when trying to place the BE GTC after
  cancelling TP. Polymarket needs more than the 0.3s sleep to release the share lock
  after a TP cancel. Fix: poll get_token_balance() up to 3s waiting for shares to
  unlock (same pattern the TP-placement code already uses), then place BE maker with
  one retry on failure. Without this fix, every SL-triggered trade falls through to
  the hedge fallback instead of getting the free BE recovery — costing ~$0.90 per
  trade that would have recovered to BE.
- **V4-LIVE-0.3.2 — Fix TP-race + accidental directional hedge** — Polymarket on-chain
  CSV revealed that trade #1's TP at $0.56 actually filled before the bot's cancel
  command landed, but the bot didn't detect the fill and proceeded to hedge — turning a
  clean +$4.80 win (recorded as -$0.90 hedge) into an unintended directional bet on
  the opposite side that happened to win. On a reversing market this could equally
  have produced -$5+ instead.
  Two fixes:
  1. After cancelling TP on SL trigger, call get_order(tp_order_id) — if size_matched
     >= fill_shares, TP filled mid-cancel. Exit as TP-BOUNCE, skip BE-MAKER and hedge.
  2. Before placing hedge buy, call get_token_balance() on our token. If 0, our
     position already exited via BE-MAKER fill or TP. Exit as SL-BE, don't hedge.

### V4-LIVE-0.2 vs V4 Paper — Strategy Comparison

| Feature | V4 Paper | V4-LIVE-0.2 |
|---|---|---|
| Mode | Paper (simulated) | LIVE (real money) |
| Shares | 69 | 10 |
| TP offset | +$0.04 | +$0.04 |
| SL offset | -$0.02 | -$0.02 |
| SL behavior | Hold until BE | Hold until BE |
| Force-exit | T+295 (5s before end) | T+180 (120s before end) |
| Fill detection | Simulated (ask <= entry) | Real API (get_order + partial fill detection) |
| Token settlement | Instant (simulated) | 1-3s delay, checks actual balance |
| TP placement | Instant | Retries every 100ms until settled, monitors price during wait |
| Manual TP | N/A | If price hits TP during settlement, FOK sells immediately |
| Pre-approval | N/A | Both tokens pre-approved at window start |
| Sell verification | Simulated | Checks actual balance, cancels TP to free locked shares |
| Sell retries | N/A | 3 attempts at progressively lower prices |
| Balance safety | N/A | Wallet balance check after unfilled trades |
| Orphan cleanup | N/A | Checks for orphaned orders on startup |
| WS staleness | N/A | HTTP fallback if no WS update for 10s |
| Error handling | except: pass | All errors logged with [WARN] |
| TP re-verify | N/A | Re-places TP if cancelled by exchange |
