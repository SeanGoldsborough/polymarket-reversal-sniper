# Changelog

## 2026-05-05

### btc_snipe_ladder.py
- **FIXED: Hedge trigger rewritten** — replaced BTC-price-based hedge (fired on wins, missed losses) with token-bid-based hedge via Polymarket websocket. Hedge now triggers when our token bid drops below $0.60, catching actual losing trades instead of false alarms.
- **Added Polymarket websocket** for real-time bid monitoring during resolution (was HTTP polling only, too slow to catch fast moves)
- **Hedge fields now logged** in trade records (hedge_placed, hedge_cost, hedge_shares) for proper P&L accounting
- HTTP poll retained as fallback alongside websocket

### btc_penny_snipe.py
- **SL lowered** from $0.04 to $0.03 to reduce premature stop-outs
- **TP lowered** from $0.15 to $0.11 to increase fill probability
- **FIXED: Balance approval bug** — added `update_balance_allowance()` call immediately before TP and SL sells. Was failing with "not enough balance" because pre-approval at order time went stale by fill time.

## 2026-05-04
- Restarted all live bots after weekend pause
- BTC-PENNY stopped after analysis showed 1W/83L — strategy needs rethink
