# Changelog

Per-bot changelogs are in the `changelogs/` directory:
- [SNIPE_ALT.md](changelogs/SNIPE_ALT.md) — Altcoin taker sniper (LIVE)
- [BTC_SNIPE_LADDER.md](changelogs/BTC_SNIPE_LADDER.md) — BTC maker ladder (LIVE)
- [BTC_COINBASE_SCALP.md](changelogs/BTC_COINBASE_SCALP.md) — CB signal scalper (PAPER)
- [BTC_PENNY.md](changelogs/BTC_PENNY.md) — BTC penny both-sides (PAUSED)
- [BTC_BOTH_SIDES.md](changelogs/BTC_BOTH_SIDES.md) — BTC both-sides at $0.50 (STOPPED)
- [BTC_LADDER.md](changelogs/BTC_LADDER.md) — BTC gap signal ladder (PAPER)
- [SNIPE_PAPER_ALL.md](changelogs/SNIPE_PAPER_ALL.md) — Multi-asset paper sniper (PAPER)
- [DASHBOARD.md](changelogs/DASHBOARD.md) — Dashboard + Telegram
- [REVERSAL_BOTS.md](changelogs/REVERSAL_BOTS.md) — Reversal strategies (RETIRED)
- [RETIRED_STRATEGIES.md](changelogs/RETIRED_STRATEGIES.md) — All retired/paper strategies

## 2026-05-05
- NEW: BTC-COINBASE-SCALP — scalp tokens on Coinbase >$5 moves, TP +$0.04, SL -$0.03
  - Realistic paper mode: 125ms latency, book depth fills, taker fees on SL
- Fix hedge trigger on BTC-SNIPE-LADDER: websocket-based token bid monitoring replaces broken BTC-price hedge
- Fix BTC-PENNY: SL 0.04->0.03, TP 0.15->0.11, balance approval bug fix
- Add all EC2 bot files to repo (22 previously untracked files)
- Create per-bot changelogs

## 2026-05-04
- Restart all live bots after weekend pause
- BTC-PENNY stopped after 1W/83L analysis

## 2026-05-01
- BTC-PENNY: sync bankroll from wallet, remove hardcoded Telegram token
- Multiple stability fixes across bots

## 2026-04-30
- BTC-PENNY created
- Dashboard fixes

## 2026-04-29
- BTC-BOTH-SIDES: extensive iteration (15 commits), goes LIVE
- Time filters added to SNIPE-ALT and BTC-SNIPE-LADDER

## 2026-04-28
- SNIPE-ALT + BTC-SNIPE-LADDER go LIVE
- BTC-BOTH-SIDES created (paper)
- Migrate to py-clob-client-v2 / pUSD

## 2026-04-27
- BTC-SNIPE-LADDER created
- BTC-LADDER created (paper)
- SNIPE-ALT: hedge improvements, multi-asset

## 2026-04-24
- SNIPE-ALT: compounding config
- Reversal MID TP adjustments

## 2026-04-23
- Repo sync, WebSocket monitors for reversal bots

## 2026-04-20
- Initial commit: Reversal Sniper LIVE
