# BTC-SNIPE-LADDER Changelog
**File:** `btc_snipe_ladder.py`
**Strategy:** T-70 maker ladder bids at $0.87-$0.92 on BTC winning side, hold to resolution

## 2026-05-05
- `5fb8375` **FIXED: Hedge trigger rewritten** — replaced BTC-price hedge (fired on wins, missed losses) with token-bid websocket hedge. Triggers when token bid drops below $0.60. Added Polymarket websocket for real-time monitoring. Hedge fields now logged in trade records.

## 2026-05-01
- `f97778e` Sync bankroll from wallet every loop, fallback to internal if API blocked

## 2026-04-29
- `40278c3` Add time filter — trade only 2pm-10pm ET

## 2026-04-28
- `bbce34f` Go LIVE alongside SNIPE-ALT
- `af967c0` Add reactive hedge via Coinbase BTC feed (later found to be broken — see 2026-05-05)
- `b595444` $5/order to meet 5-share minimum
- `d97f84a` Scan from T-70 to T-30, not just at T-70
- `ccb84aa` Fresh start: reset tracking, $1/order
- `ebe32fb` Fix stale-bid-as-WIN bug
- `b35e416` Fix: losses logged as wins due to stale book data at timeout
- `429a542` Migrate to py-clob-client-v2 for pUSD order signing
- `f5b9803` Migrate from USDC.e to pUSD collateral token

## 2026-04-27
- `e8d41ff` Initial creation: T-70 maker ladder on BTC winning side
