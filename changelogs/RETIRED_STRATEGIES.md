# Retired Strategies Changelog
**Status:** All retired — kept in repo for reference only

## E5 (Directional Snipe)
**Files:** `e5_live.py`, `e5_live_backup.py`
- T-30 directional pick, single asset
- 138 trades, 28.3% WR, lost 98.6% of bankroll
- **Lesson:** Picking direction on 5-min crypto is a coin flip minus fees

## F2 (Directional with Gap Signal)
**Files:** `f2_live_limit.py`, `f2_live_market.py`
- T-45 directional pick with Coinbase gap signal
- Paper showed 62.5% WR ($100 → $104K) but biased simulation data
- Live lost -$62 due to execution failures
- **Lesson:** Paper results using biased data are worthless

## Alpha1
**File:** `alpha1_live.py`
- Early directional strategy
- Retired after losses

## Omega / SOL Omega
**Files:** `omega_live.py`, `paper_omega.py`, `paper_sol_omega.py`
- Both-sides strategy variant
- Paper showed promise but never went live

## Ironclad
**File:** `ironclad_live.py`
- BTC-focused strategy
- 415 summary shows limited data

## Latency Arb
**File:** `latency_arb.py`
- Coinbase → Polymarket latency arbitrage
- Coinbase leads by 2-55 seconds
- Deployed but no significant results

## Paper Strategies D-H
**Files:** `paper_strategy_d.py` through `paper_strategy_h.py`
- Various experimental paper strategies
- D: directional with history
- E: multi-asset directional
- F: trajectory-based
- G: enhanced directional
- H: oscillation scalping (1,249 trades, 19.4% WR, lost 100%)

## Paper Market Maker
**File:** `paper_market_maker.py`
- Tight spread market making
- 0 fills in 50+ minutes — can't compete with latency arb bots

## Paper Endgame Low Entry
**File:** `paper_endgame_lowentry.py`
- Low entry price variant of endgame strategy

## Prod Endgame 5min
**Files:** `prod_endgame_5min.py`, `prod_endgame_5min_ALT.py`
- Production endgame strategy, 5-min windows
- ALT version for altcoins

## Auto Redeem
**File:** `polymarket_auto_redeem.py`
- Runs via cron every 5 min to redeem resolved positions
- **Still active** — not retired
