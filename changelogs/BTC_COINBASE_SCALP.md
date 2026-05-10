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
