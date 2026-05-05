# BTC-BOTH-SIDES Changelog
**File:** `btc_both_sides.py`
**Strategy:** Buy both UP and DOWN at ~$0.50, SL loser, hold winner to $1.00
**Status:** Stopped — only 4 trades in 30 min runtime

## 2026-04-29
- `29bd8ae` TP +12% / SL -12%, SL active all window
- `d8efea2` TP +18% anytime, SL -18% only last 3 min on bounce
- `c314459` SL at $0.35 on both sides
- `495cf18` Replace SL $0.20 with TP $0.65
- `d10029d` Sequential trades — no window gaps
- `638b662` Verify fills via order API, not just ask price
- `a5088bd` Fix SL sell: shares mismatch — tried to sell 10 but only held 5
- `6c73aa2` Fix: HTTP resolution loop detects SL but never sells — add FAK sell
- `96c3d54` Target NEXT window for better prices near $0.50
- `ee50f8d` Pre-approve both tokens after fill for instant SL execution
- `95c7940` Fix SL sell: approve conditional token before FAK sell
- `2cde6ab` Go LIVE: 5 shares/side, GTC maker bids, FAK SL
- `9fb6570` Fix log spamming on every WS update
- `76df315` Fix BID_PRICE and CANCEL_THRESHOLD references after config rename
- `38eba91` SL at $0.20 on BOTH sides after fill
- `55e603a` Buy range $0.45-$0.55 + SL at $0.20
- `42f4ff8` Fix WebSocket — handle list snapshots + string prices
- `e3284c9` Replace broken WS with HTTP polling every 2s

## 2026-04-28
- `d0cea92` Add 15% stop loss on one-fill scenario
- `6faf845` Remove early cancel — let both bids sit patiently
- `22bfbc0` v2: GTC bids + WebSocket monitoring
- `e64da84` Only buy when market is undecided
- `5fe33c6` Initial creation: paper bot, buy both sides below $0.45
