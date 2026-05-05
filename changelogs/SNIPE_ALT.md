# SNIPE-ALT Changelog
**File:** `snipe_alt_live.py`
**Strategy:** Buy altcoins at $0.97-$0.99 in last 15s of window, hold to $1.00 resolution

## 2026-04-29
- `40278c3` Add time filters — trade only 12am-8am + 7pm-12am ET (off-peak hours)

## 2026-04-28
- `bbce34f` Go LIVE: 4 altcoins (ETH, DOGE, SOL, XRP) at 8% bet each
- `429a542` Migrate to py-clob-client-v2 for pUSD order signing
- `f5b9803` Migrate from USDC.e to pUSD collateral token

## 2026-04-27
- `eaadb6f` FAK first for partial fills, FOK fallback
- `8d67d9c` Fix hedge threshold: use percentage-based per-asset, not hardcoded BTC $15
- `08fc93f` Track ETH+DOGE prices via Coinbase for hedge trigger
- `50e77c7` BTC-price hedge trigger replaces token-price trigger
- `8ef3e47` Add reactive hedge on reversal detection
- `99ec7b4` Drop XRP — ETH+DOGE only (later re-added)

## 2026-04-24
- `1c208a7` 34% compounding, $5K cap, remove DD pause
