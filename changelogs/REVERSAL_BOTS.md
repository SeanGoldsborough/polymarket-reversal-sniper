# Reversal Bots Changelog
**Files:** `reversal_sniper_live.py`, `reversal_live_fixed.py`, `reversal_fix_mid_limit.py`, `reversal_trailing.py`
**Strategy:** Buy cheap tokens on Coinbase momentum reversal detection
**Status:** RETIRED — all stopped, were bleeding money

## 2026-04-24
- `2fa795c` Revert MID TP back to $0.35 (keep time exit)
- `91e0704` Add time-based exit + lower MID TP to $0.30

## 2026-04-23
- `fb9b405` Sync all deployed EC2 bot files to repo
- `dda9989` Add WebSocket position monitor to MID and TRAIL bots

## 2026-04-20
- `95f82de` Initial commit: Reversal Sniper LIVE — production-ready
