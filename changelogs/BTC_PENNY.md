# BTC-PENNY Changelog
**File:** `btc_penny_snipe.py`
**Strategy:** Buy both sides at $0.09, TP $0.11, SL $0.03, hold to resolution if no TP fill

## 2026-05-05
- `5fb8375` SL lowered $0.04 → $0.03. TP lowered $0.15 → $0.11. **FIXED: Balance approval bug** — added `update_balance_allowance()` before every TP and SL sell (was failing with "not enough balance").

## 2026-05-04
- Bot stopped after analysis: 1W/83L, strategy bleeding. SL at $0.04 firing on nearly every trade.

## 2026-05-01
- `f97778e` Sync bankroll from wallet every loop
- `966a60d` Fix halting: sync bankroll when internal tracking drifts
- `44df7c8` SECURITY: Remove hardcoded Telegram bot token, read from .env
- `64a3771` Reduce to 5 shares/side ($0.45/side) for data collection
- `4df9abd` Rebuild with dual monitoring: API poll + WS with auto-reconnect

## 2026-04-30
- `ce65331` Fix missed fills: add post-WS fill check via order API
- `464a238` Initial creation: buy both sides at penny prices
