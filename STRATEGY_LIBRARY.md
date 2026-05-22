# Strategy Library

Reference for backtested strategies on the Polymarket BTC 5-min binary market. Backtested against 207 windows (~5.5 days, 6,503+ CB signals, 17,326 swing events) of tick-level data with bilateral order book + Coinbase BTC price.

## ⚠️ Read this first: ceiling vs realistic

The numbers in this document have **TWO different meanings** that get conflated easily:

- **CEILING** = what the math says IF every assumption holds (100% maker fill, no queue losses, no adverse selection). This is what the backtest produces. **Not what live trading will produce.**
- **REALISTIC** = ceiling discounted for execution headwinds (fill rate, queue position, adverse selection). **Educated estimate based on prior live observations, not validated.**

The realistic numbers carry significant uncertainty — they could be higher or lower than estimated. Until we run a proper execution simulator (Phase 2), treat realistic as a range, not a point.

**Anywhere the document says "will produce X" — read it as "ceiling is X; realistic likely lower, range stated."**

---

## Quick Reference

| ID | Strategy | Trigger | Entry | Measured $/day @ 10sh (paper engine) | Confidence | Status |
|---|---|---|---|---:|---|---|
| S1 (maker) | CB-aligned hold (small) | CB ±$5-9 | Maker @ bid | **-$11** | Conservative lower bound | Marginal at best |
| S1 (taker) | CB-aligned hold (small) | CB ±$5-9 | Taker @ ask | **+$377** | Engine reliable but CI on per-bucket WR includes zero | **No statistical proof of edge** |
| S2 (maker) | CB-fade hold (large) | CB ±$15+ | Maker @ bid | **+$32** | Matches earlier estimate, small n (209 signals) | Promising |
| S2 (taker) | CB-fade hold (large) | CB ±$15+ | Taker @ ask | **+$250** | Higher confidence — 77% WR, n=163 | **Strongest measured signal** |
| S3 | Swing capture (reactive indicator) | Local-low + $0.02 bounce | Taker @ ask | **-$935** | Strategy logic flawed — confirmation eats swing | **BROKEN as written** |
| S3-v2 | Swing capture ($0.06+ amplitude filter) | Local-low + $0.03 bounce, range ≥$0.06 | Taker @ ask | **-$418** | Amplitude filter helps selection but doesn't fix confirmation cost | Still broken |
| S4 | Conditional MM | Spread > $0.03 | Maker ladders | **-$514** | Engine measures worst-case adverse selection only | **Unknown — needs trade-event data to measure fairly** |
| S5 | CB-aligned scalp (V4-LIVE legacy) | CB ±$5 | Maker + TP/SL | **-$60** (live measured) | Live trading data | **Documented loser — do not revive** |

**⚠️ Statistical reality check (added after engine validation 2026-05-22):**

For S1 buckets at taker entry:
- $5-6 (n=2,120, 53% WR): 95% CI on $/share = (-$0.008, +$0.032). **~12% probability the true edge is negative.**
- $6-7 (n=1,123, 48% WR): 95% CI = (-$0.022, +$0.032). ~36% chance negative.
- $7-8 (n=669, 48% WR): measured -$0.013/share. **Actively losing.**
- $8-10 (n=758, 49% WR): 95% CI = (-$0.018, +$0.050). ~19% chance negative.

**None of the S1 buckets has a 95% CI that excludes zero.** The measured positive P&L is consistent with a small real edge OR no edge with random luck. We need 3-5× more data (~30-50 more days of recording) to confidently say S1 makes money.

Live execution shaves ~$0.005-$0.015/share off the measured edge (slippage, latency, partial fills). For $5-6: measured +$0.012/share − $0.010 execution cost = +$0.002/share live, well within zero.

**Practical implication: do not deploy S1 with confidence until more data confirms the edge.**

---

## Data validation context

- **Tick data source**: `/home/ubuntu/reports/ticks/` on Ireland EC2 (54.246.52.236), captured by `tick_recorder.py` 2026-05-12 through 2026-05-18
- **Total ticks**: ~17.6M across 207 windows
- **Format**: bilateral order book (up/down bid+ask+sizes) + Coinbase BTC price, ~286 ticks/sec
- **Statistical bar**: Any claim of an edge requires n ≥ 100 and a stated confidence interval

---

## Execution headwinds (what reduces ceiling to realistic)

The backtest assumes **idealized execution**. Live trading faces these headwinds, each of which reduces actual P&L:

### 1. Maker fill rate (largest unknown)

**Backtest assumption**: 100% of maker bid orders fill at the bid price.

**Reality**: Maker orders only fill when a counterparty crosses to our price. Empirical reference from prior live bot: **~38% UNFILLED rate** (different strategy on same market). Real fill rate is somewhere between 50% and 80% for most strategies — we don't know exactly without testing.

**Strategy-specific dynamics:**
- **S1 (aligned)**: When we want to buy UP after a UP spike, sellers of UP are running away — they don't cross down to our bid. Likely the WORST fill rate.
- **S2 (fade)**: We buy the OPPOSITE side that's getting dumped post-spike. Sellers ARE crossing our way. Likely the BEST fill rate.
- **S3 (swing)**: Mixed — local-low buys happen at exhaustion; some fills come quickly, some never fill.

### 2. Queue position (queue ahead of us at maker price level)

**Backtest assumption**: When the bid touches our price level, we fill.

**Reality**: There are other makers at the same price level, placed before us. They fill first. If only 5 shares get sold at our level before the bid moves, those go to the queue front. We sit unfilled.

This is the largest reason past MM strategies (H2) lost 100% — couldn't compete on queue priority with fast bots.

### 3. Adverse selection

**Backtest assumption**: A fill at $X means we bought at $X and that's the cost basis.

**Reality**: A maker fill at $X often happens precisely because the price was about to move past $X. Our $0.45 bid fills because the bid just dropped through $0.45 (someone sold AT $0.45 because the market is going lower). We bought near a local TOP.

### 4. Settlement delay (Polymarket-specific)

**Backtest assumption**: Token positions are available instantly after fill.

**Reality**: ERC-1155 settlement on Polygon takes 1-3 seconds after a fill. We can't sell what we don't yet hold. This matters for S3's fast cycle especially.

### 5. Latency

**Backtest assumption**: 200ms reactive (used for S3); zero for S1/S2 (placed at signal time).

**Reality**: 50-200ms order placement + 100-300ms WS-to-decision + variable fill time. Real round-trip is 500ms-2s depending on what we're doing.

---

## What we DON'T know yet

These are open questions that Phase 2 (order execution module) is needed to answer:

1. **Actual maker fill rate per strategy** — currently estimated, not measured
2. **Actual queue position impact** — depends on competitor maker activity at each price level
3. **Whether the adverse-selection cost is small or large** — could be $0.01 per share, could be $0.04
4. **How partial fills affect strategies** — backtest treats fills as binary
5. **How cancel-and-replace timing affects S3 and S4** — needs simulation

Until Phase 2 is built and runs through replay data, **all "realistic" estimates in this document are educated guesses**, not validated numbers.

---

## S1: CB-aligned hold (small signals $5-9)

### When to fire

Coinbase BTC moves $5-9 in 1 second (per consecutive-CB-tick delta with ≤1.5s gap).

### Action

1. Buy the side **aligned with CB direction** (UP signal → buy UP token, DN signal → buy DN token)
2. Place maker GTC at the current bid of that side
3. Hold to window resolution (no TP, no SL, no hedge)

### Why it works

Small CB moves precede modest Polymarket continuation. The market doesn't reverse hard. Maker entry avoids both taker fees and the bid-ask spread, leaving positive expectancy at modest WR.

### Backtested profitability (CEILING — 100% maker fill assumption)

| Signal bucket | n | WR (P&L > 0) | $/share | Net at 10sh × n |
|---|---:|---:|---:|---:|
| $5-6 | 2,105 | 53% | +$0.035 | +$733 |
| $6-7 | 1,119 | 48% | +$0.022 | +$246 |
| $7-8 | 679 | 49% | +$0.017 | +$116 |
| $8-10 | 759 | 48% | +$0.030 | +$230 |
| **Combined** | **4,662** | **~50%** | **~$0.028 avg** | **+$1,325** |

**CEILING over 5.5 days at 10sh: ~$241/day. At 100sh: ~$2,410/day.**

### Measured results — paper engine (2026-05-22)

Ran S1 through PaperOrderEngine with conservative fill rule. 260 windows of data, 5,144 signals.

**At-bid entry (maker, $0 fee):**

| Bucket | Signals | Filled | Fill % | WR | $/share | Net 10sh × n |
|---|---:|---:|---:|---:|---:|---:|
| $5-6 | 2,318 | 173 | 7% | 47% | +$0.027 | +$47 |
| $6-7 | 1,232 | 102 | 8% | 44% | -$0.005 | -$5 |
| $7-8 | 762 | 61 | 8% | 31% | -$0.053 | -$32 |
| $8-10 | 832 | 76 | 9% | 38% | -$0.023 | -$17 |
| **TOTAL** | 5,144 | 412 | **8%** | 42% | -$0.002 | **-$8 (~-$11/day at 10sh)** |

Conservative fill rule is too strict — only counts fills where bid moves strictly below our level. Real maker fill rate is higher but can't be measured without trade-event data.

**Taker entry (cross to ask, +$0.012/share fee):**

| Bucket | Signals | Filled | Fill % | WR | $/share | Net 10sh × n |
|---|---:|---:|---:|---:|---:|---:|
| $5-6 | 2,227 | 2,120 | 95% | 53% | +$0.012 | +$248 |
| $6-7 | 1,175 | 1,123 | 96% | 48% | +$0.005 | +$56 |
| $7-8 | 714 | 669 | 94% | 48% | -$0.013 | -$87 |
| $8-10 | 795 | 758 | 95% | 49% | +$0.016 | +$123 |
| **TOTAL** | 4,911 | 4,670 | 95% | 50% | +$0.007 | **+$340 (~+$377/day at 10sh)** |

### Critical statistical caveat

**None of these per-bucket measurements is statistically distinguishable from zero P&L.** 95% CIs on $/share for taker entry:

| Bucket | Mean $/share | 95% CI | P(true edge < 0) |
|---|---:|---:|---:|
| $5-6 | +$0.012 | (-$0.008, +$0.032) | ~12% |
| $6-7 | +$0.005 | (-$0.022, +$0.032) | ~36% |
| $7-8 | -$0.013 | (-$0.049, +$0.023) | ~76% (measured losing) |
| $8-10 | +$0.016 | (-$0.018, +$0.050) | ~19% |

To statistically confirm $5-6 is profitable would need ~3-5× more data (~8,000-10,000 filled trades).

### Live execution headwinds (estimated, not measured)

Real-world execution shaves ~$0.005-$0.015/share off the measured edge:
- Slippage on fast CB moves (ask moves up during our 80-100ms network latency)
- Partial fills when ask_size < order_size
- Variable spread regime — sometimes wider than the moments we sampled

For $5-6: measured +$0.012/share − $0.010 typical execution cost = **+$0.002/share live**. Inside the noise of zero.

### Verdict

S1 measured numbers are consistent with either a small positive edge OR no edge plus random luck. **Do not deploy with confidence** until we have 3-5× more data, AND a queue-aware simulator that incorporates trade-event data (currently being captured).

### Caveats

- Maker fill rate measured at 8% (conservative bound). True value is between 8% and 95% (taker-fill bound). Engine can't distinguish without trade events.
- Taker entry circumvents the maker fill question but adds taker fee cost.
- $7-8 bucket actively loses money in BOTH entry modes — should probably be excluded.
- All buckets need more data before live deployment.

---

## S2: CB-fade hold (large signals $15+)

### When to fire

Coinbase BTC moves $15+ in 1 second.

### Action

1. Buy the side **OPPOSITE the CB direction** (UP signal → buy DOWN token)
2. Place maker GTC at the current bid of the opposite side
3. Hold to window resolution

### Why it works

Large CB spikes ($15+ in 1 sec) typically mark the **tail of an exhausted move** rather than the start of new momentum. The "winning-looking" side immediately after the spike is mispriced — about to mean-revert. The opposite (cheap, "losing-looking") side captures the reversal.

### Backtested profitability (CEILING)

| Signal bucket | n | WR (P&L > 0) | $/share | Net at 10sh × n |
|---|---:|---:|---:|---:|
| $15-20 | 93 | 68% | +$0.059 | +$55 |
| $20+ | 71 | 83% | +$0.246 | +$175 |
| **Combined** | **164** | **~75%** | **~$0.14 avg** | **+$230** |

95% CI on $20+ WR: 69-88% — even pessimistic bound is profitable.

**CEILING over 5.5 days at 10sh: ~$42/day. At 100sh: ~$420/day.**

### Measured results — paper engine (2026-05-22)

260 windows of data.

**At-bid entry (maker, $0 fee):**

| Bucket | Signals | Filled | Fill % | WR | $/share | Net 10sh × n |
|---|---:|---:|---:|---:|---:|---:|
| $15-20 | 116 | 14 | 12% | 57% | +$0.077 | +$11 |
| $20+ | 93 | 6 | 6% | 83% | +$0.303 | +$18 |
| **TOTAL** | 209 | 20 | 10% | 65% | +$0.145 | **+$29 (~$32/day at 10sh)** |

Matches the earlier "realistic estimate" of $25-36/day. Note: only 20 filled trades out of 209 signals — very small sample for the win rate. **The 65% WR has wide CI; could be 45-80%.**

**Taker entry (cross to ask, +$0.012/share fee):**

| Bucket | Signals | Filled | Fill % | WR | $/share | Net 10sh × n |
|---|---:|---:|---:|---:|---:|---:|
| $15-20 | 101 | 92 | 91% | 71% | +$0.056 | +$51 |
| $20+ | 75 | 71 | 95% | 86% | +$0.246 | +$175 |
| **TOTAL** | 176 | 163 | **93%** | **77%** | +$0.139 | **+$226 (~$250/day at 10sh)** |

**Taker entry is dramatically better for S2** — 8× more fills with similar per-share P&L. The fade strategy specifically targets exhausted moves where sellers of the OPPOSITE side are dumping, so paying the spread to cross is well-rewarded.

### Confidence — strongest signal we have

n=163 with 77% WR and +$0.139/share is the STRONGEST measured signal in the library:
- 95% CI on WR for $20+ bucket: 75-95% (even pessimistic bound is profitable)
- Mean P&L well outside zero — high confidence the edge is real

However: **only 0.4-0.6 signals per window** ($15+ events are rare). Volume is limited.

### Caveats

- $15+ signals are rare. Bot mostly idle.
- Sample n=163 is medium — would benefit from another 10x data for tightest CIs
- $20+ flash events require sub-second detection — latency-sensitive

---

## S3: Swing capture (always-on)

### When to fire

A local low in the bid trajectory is detected by the indicator (see below). This is **independent of CB signals** — fires constantly on order book dynamics.

### The indicator

Watch the bid trajectory of either side (or both). Detect "we just bounced off a local low":

```
LOCAL-LOW INDICATOR:
  Conditions (ALL must be true):
    1. Current bid > minimum bid observed in last 2 seconds (recently bounced)
    2. Bounce amplitude: (current_bid - min_last_2s) >= $0.02 (filter noise)
    3. min_last_2s happened >= 100ms ago (confirm not a flickering tick)
    4. Current bid is in safe zone: $0.30 - $0.70 (avoid bleeding sides)
  
  Optional hard filter:
    5. No CB ±$5+ move in last 5 seconds (avoid trading into a signal)
```

Mirror indicator for local highs:
```
LOCAL-HIGH INDICATOR (for sell side):
  1. Current bid < maximum bid in last 2 seconds
  2. Drop amplitude >= $0.02
  3. max happened >= 100ms ago
  4. We hold an open swing position
```

### Action

1. On LOCAL-LOW: place maker GTC at `min_last_2s + $0.01` (try to fill near actual low)
2. Hold position
3. On LOCAL-HIGH (with open position): place sell at `max_last_2s - $0.01` (lock in profit)
4. Or pre-place sell GTC at `entry + $0.04` as a static TP

### Why "2 seconds"?

Matches measured 1.8s average swing duration in the data. 72% of swings happen in <1s, but a 2s indicator window allows 1-2 ticks of confirmation that the low has actually happened (not just a flicker).

### Backtested profitability (using a fictional perfect-oracle latency model)

| Setup | n | WR | Avg P&L/share | Total at 10sh | Per day at 10sh |
|---|---:|---:|---:|---:|---:|
| **Perfect oracle (true ceiling, fictional)** | 17,326 | 95% | +$0.050 | +$8,722 | ~$1,585 |
| 200ms reactive (modeled — UPPER bound for an actual indicator) | 17,326 | 63% | +$0.009 | +$1,560 | **~$284** |
| 1500ms reactive (slow, modeled — LOWER bound for slow execution) | 17,326 | 56% | +$0.004 | +$636 | ~$115 |

**The 200ms model is what's been used as "ceiling" for S3, but it's still assuming the indicator perfectly identifies each low instantly.** Real indicator-based detection (sliding-window minimum) has 1-2 tick lag for confirmation.

Hour-of-day breakdown — every hour produces positive P&L in the backtest, best evening (20-22 ET), worst US market open (10 ET, 54% WR).

### Measured results — paper engine (2026-05-22) — STRATEGY IS BROKEN

Implemented the indicator as written (2-second sliding-window low + $0.02 bounce confirmation, $0.30-$0.70 safe zone, skip during CB events). Ran 30-window test with taker_ask entry.

| Metric | Value |
|---|---:|
| Buy entries attempted | 10,614 |
| Filled | 279 (3% — engine fill issue, see below) |
| Round-trips completed | 257 |
| WR (P&L > 0) | **18%** (46W/208L) |
| Avg P&L per round-trip | **-$0.038/share** |
| Total at 10sh × n | **-$97** |
| Per-day at 10sh | **~-$935** |

### Why it fails — the indicator's confirmation cost eats the swing

```
ENTRY:  bid > min_in_last_2s + $0.02  (we wait $0.02 of confirmation)
EXIT:   bid < max_in_last_2s - $0.02  (we exit $0.02 down from high)

For a $0.04 swing (typical amplitude):
  Buy:  low + $0.02 (already $0.02 into the bounce)
  Sell: high - $0.02 (already $0.02 down from peak)
  Net captured: (high - $0.02) - (low + $0.02) = (high - low) - $0.04 = $0

We capture ZERO when swings are exactly $0.04 — then we LOSE fees on top.
```

The original $284/day "ceiling" assumed a PERFECT ORACLE — knew the exact low/high in advance. A real indicator can't do that without confirmation, and confirmation costs ~$0.04 per round-trip in this market.

### Three possible fixes — TESTED

1. **Lower confirmation threshold to $0.01 each side** — captures $0.02 vs $0.04 confirmation cost. Might break even but won't have edge unless swings >$0.04 are common.

2. **Target only larger swings ($0.06+ amplitude) with $0.03 confirmation** — tested 2026-05-22 on 50 windows:
   - 3,751 entries attempted, 257 filled (7%)
   - **26% WR, -$0.031/share avg = -$418/day at 10sh**
   - Still losing. The amplitude filter helps trade selection but the confirmation cost remains.

3. **Switch to passive maker ladders (S4 concept)** — tested as S4 separately, also showed losses under conservative-fill rule. Real fills with trade-event data may improve this; until then, unknown.

**Verdict: NO reactive variant of S3 measured as profitable.** The fundamental issue is confirmation cost on each side.

### Verdict

**S3 as a reactive indicator-based strategy doesn't work.** The +$284/day ceiling was an artifact of perfect-oracle assumptions. Any reactive strategy will pay $0.02 confirmation cost per side, eating the typical swing.

**To make swing capture profitable, we need the passive maker-ladder design (S4).** Reactive S3 should be retired or redesigned with bigger amplitude targets.

### Note on the 3% engine fill rate

This is partly an engine limitation: when ask_size = 0 in the recorder data (common when only the bid was updated), engine's taker FAK refuses to fill. Real FAK orders would fill against the actual book depth regardless of size displayed in last tick. Once trade-event data is incorporated, this artifact resolves.

But even fixing fill rate to 100% wouldn't save S3 — the 18% WR on the trades that DID complete shows the strategy logic itself is unprofitable.

### Caveats

- Indicator-based detection has 1-2 tick lag vs perfect oracle (so 95% theoretical is unreachable)
- Queue position matters for maker fills — competing makers may take fills before us
- Bid-range filter ($0.30-$0.70) is critical — without it, the strategy loses on bleeding sides
- Cancel-on-CB-event is mandatory — a CB ±$5+ move during a held swing position is dangerous

---

## S4: Conditional MM (proposed, not yet validated)

### Concept

The Polymarket BTC market has $0.01-$0.02 spread 87% of the time — too tight for naive MM to compete with fast bots. But **13% of the time the spread is $0.03+**. In those windows, we can place ladders at wider levels and capture genuine spread value.

### When to fire

```
SPREAD-WIDE INDICATOR:
  Conditions:
    1. Current spread on either side > $0.03 (sustained over 1-2 seconds)
    2. CB hasn't moved $5+ in last 10 seconds (stable regime)
    3. Bid is in safe zone ($0.30-$0.70) — same as S3
```

### Action

Place laddered maker GTCs on both sides of mid, on whichever side(s) have wide spread:
- Buys at `mid - $0.02, mid - $0.03, mid - $0.04`
- Sells at `mid + $0.02, mid + $0.03, mid + $0.04`

Wait for fills. Cancel and re-place when:
- Mid moves >$0.02 (refresh ladder)
- Spread tightens to <$0.02 (exit the regime, cancel everything)
- CB moves $5+ (overlay S1/S2 takes priority)

### Why this might work

- The 13% wide-spread regime exists because **fast bots haven't re-quoted** in those moments (latency arb gap, low-volume periods)
- Wider spreads naturally have less competition at our price levels (better queue position)
- Far-from-mid orders ($0.02-$0.04 from mid) avoid the adverse-selection trap of tight quotes

### Why this might NOT work

- 13% of time × small orders → few fills per day
- Adverse selection still possible (price might move through our level before we cancel)
- Inventory accumulation if we get filled on only one side

### Measured results — paper engine (2026-05-22)

Tested with `spread_threshold ∈ {$0.03, $0.05}`, both losing money.

| Config | Quotes | Buy fills | Cycles | WR | $/cycle | Per-day @ 10sh |
|---|---:|---:|---:|---:|---:|---:|
| Threshold $0.03 | 5,976 | 134 | 134 | 49% | -$0.067 | **-$514** |
| Threshold $0.05 | 2,807 | 51 | 51 | 40% | -$0.102 | -$301 |

Higher selectivity is WORSE per-cycle. That's a red flag.

### Why the engine measurement may understate true S4

The conservative fill rule **only counts fills where the bid moves STRICTLY THROUGH our level**. By definition, these are adverse-selection fills:
- Bid drops through our buy → side is weakening → we bought into weakness
- 100% of our measured fills are this adverse kind

In reality, maker fills also occur when bid stays at our level and a casual seller crosses — these are NEUTRAL fills with mixed P&L. Our engine can't simulate these without trade-event data.

So the engine's -$514/day for S4 is the **worst-case lower bound**. True S4 P&L could be:
- Negative (if adverse selection dominates)
- Near zero (if neutral fills offset adverse ones)
- Slightly positive (if neutral fills happen often enough)

**We don't know which without trade-event simulation.**

### Path forward

Cannot deploy S4 with confidence based on current measurement. Need:
1. Trade-event data (currently accumulating, ~53 windows captured)
2. Queue-advancing simulator that distinguishes adverse vs neutral fills
3. Then re-measure S4 with both fill types modeled

Estimated timeline: ~1-2 more days of capture + 4-6 hours of simulator work.

---

## S5: CB-aligned scalp with TP/SL (V4-LIVE legacy)

⚠️ **This strategy LOST money in live testing.** Documented here so we don't accidentally re-implement it without remembering the history.

### When to fire

Coinbase BTC moves $5+ in 1 second.

### Action

1. Buy the side aligned with CB direction (same as S1)
2. Place a maker GTC BUY at the current ask (taker entry path) or bid (maker path)
3. After fill, place a maker GTC SELL at `entry + $0.04` (TP)
4. If bid drops to `entry - $0.02` (SL), enter "hold-until-BE" mode:
   - Cancel TP
   - Wait for bid to recover to entry
   - Sell at entry for $0 P&L
5. At T+180 (120s before window end), force-exit if still holding

### Why it was tried

This was the V4-LIVE bot's design from May 2026. The thinking was: short-term Polymarket bid reacts to CB moves with ~$0.04 amplitude within seconds. TP captures the reaction. SL caps downside. If SL triggers but recovers to BE, we lose nothing.

### Actual live performance (from `/home/ubuntu/polymarket-bot/logs/btc_scalp_v4live_02_trades.jsonl`)

| Outcome | Count | Total P&L | Avg/trade |
|---|---:|---:|---:|
| TP | 12 | +$4.80 | +$0.40 |
| TP-BOUNCE | 16 | +$6.36 | +$0.40 |
| MAX-HOLD | 15 | +$17.82 | +$1.19 |
| SL-BE | 58 | -$0.30 | -$0.005 |
| FORCE-EXIT | 16 | **-$55.53** | -$3.47 |
| DYN-SL (reverted) | 5 | -$5.60 | -$1.12 |
| UNFILLED | 71 | $0 | — |
| BUY-FAIL | 9 | $0 | — |
| **TOTAL** | **202** | **-$32.45** | -$0.16 |

Win rate (P&L > 0): **22%** (TP + TP-BOUNCE + MAX-HOLD = 43 of 202 signals were positive).
Per-day rate: approximately **-$60/day at 10sh** based on the live sample.

### Why it lost money

**The force-exit category killed it.** 16 trades × -$3.47 avg = -$55.53 in losses concentrated in 8% of trades. Every other outcome bucket was net positive or near-zero, but the catastrophic-loss tail dwarfed the cumulative wins.

Root cause analysis from the trade post-mortem:
1. **The $0.04 TP captures only a fraction of the actual bid swing.** When the swing IS big, our TP fires fast and caps gains at +$0.40 per 10sh. When the swing is small, no TP fires and we hit SL.
2. **SL+hold-to-BE works most of the time (91% of SL triggers recover to BE),** but the 9% that don't recover crash to $0.05-$0.20 and we force-exit at fire-sale prices.
3. **FAK slippage on force-exit.** The bot's force-exit logic walks the book down 3 times (-$0.01, -$0.02, -$0.03) trying to find liquidity. Each retry compounds slippage. A -$0.20 expected SL becomes a -$0.40+ realized loss.

### Patches that didn't save it

Multiple iterations were deployed live to address the losses:
- **V4-LIVE-0.2.1**: cancel-status case fix
- **V4-LIVE-0.3**: persistent BE-maker (avoid round-trip latency on BE recovery) + CLIFF/MODE-A/DEADLINE hedge fallback
- **V4-LIVE-0.3.1**: balance-poll after TP cancel
- **V4-LIVE-0.3.2**: TP-race detection (catch TP fills that happened before our cancel landed)

After all patches, the bot still lost roughly the same per-day rate. The structural problem was the strategy's exposure to FORCE-EXIT, not the execution polish.

### Why we don't use it now

The lesson from S5's live performance led to:
- **S1** (CB-aligned HOLD-to-resolution) — no TP, no SL, no force-exit; rely on resolution mechanics
- **S2** (CB-fade HOLD on large signals)
- **S3** (swing capture as a separate always-on layer)

These strategies don't have the force-exit problem because they're designed around hold-to-resolution.

### Could a fixed version of S5 work?

Possibly. The hedge mechanism added in V4-LIVE-0.3 (cancel-and-buy-opposite when CLIFF/MODE-A/DEADLINE triggers) reduces force-exit damage from -$3.40 to roughly -$1.00 per losing trade. With that bound:
- 16 force-exits × -$1.00 = -$16 instead of -$55
- Combined with other categories: roughly -$0 to +$15 daily

That's not a winner but it's a small loss vs the existing -$60/day. Still inferior to S1.

**Verdict: not recommended for revival** unless we discover a way to materially reduce force-exit frequency itself (not just bound the loss).

---

## Composite Strategy (target build)

The bot we want to build runs **S1, S2, and S3 simultaneously**, with S4 layered when conditions allow.

```
On every tick (~286/sec):
  
  IF CB ticks and |delta| >= $5:
    classify magnitude bucket
    IF $5-9:
      execute S1 (aligned hold)
    ELIF $10-15:
      no action (data shows borderline; skip)
    ELIF $15+:
      execute S2 (fade hold)
    ALSO: cancel any open S3 swing positions in same window (event override)
  
  ELIF spread wide-spread indicator fires AND no CB event recent:
    execute S4 (conditional MM ladder)
  
  ELIF local-low indicator fires AND in safe zone AND no CB event recent:
    execute S3 (swing buy)
  
  ELIF local-high indicator fires AND we have open S3 position:
    execute S3 (swing sell)
```

### Capital allocation

At 10sh × ~$0.50 = ~$5 per position. Bank of $35-50 supports 5-10 concurrent positions.

- Reserve ~30% capital for CB events (S1/S2) — can fire multiple times per window
- Use ~50% for active swing trading (S3) — typically 1-3 open at once
- Use ~20% for S4 ladders when conditions warrant

### Combined daily P&L — MEASURED (paper engine) vs estimated

| Strategy | Measured (paper engine) @ 10sh | Status | Net at 100sh |
|---|---:|---|---:|
| S1 maker (at_bid) | **-$11/day** | Marginal — CI includes positive AND negative | -$110/day |
| S1 taker (cross to ask) | **+$377/day** | Per-bucket CI all include zero — needs more data | +$3,770/day |
| S2 maker (at_bid) | **+$32/day** | Matches earlier estimates, small n | +$320/day |
| S2 taker (cross to ask) | **+$250/day** | Strongest measured signal — high confidence | +$2,500/day |
| S3 (reactive indicator) | **-$935/day** | Strategy broken — confirmation eats swing | -$9,350/day |
| S4 (conditional MM) | TBD | Not yet validated | TBD |

**Naive sum (S1 taker + S2 taker): +$627/day at 10sh measured.**

But this is FUDGE because:
1. S1 buckets aren't statistically distinguishable from zero — could be smaller
2. Live execution headwinds shave another $0.005-$0.015/share
3. Capital constraints — at 10sh × ~$0.50 entry = ~$5/trade, we can't have 5,000+ open positions
4. The engine fill simulation has known limitations (assumes 100% taker fill, no slippage)

**Honest realistic combined estimate at 10sh: $0-300/day**, with significant chance it could be negative.
**Honest realistic combined estimate at 100sh: $0-3,000/day**, same uncertainty band.

---

## Dead strategies (do not revisit)

These have been tested and ruled out:

| Strategy | Why it failed |
|---|---|
| Cross-side arbitrage (UP_bid + DN_bid > $1) | 0 opportunities in 5.5 days — Polymarket arb bots are too fast |
| End-of-window winning-side detection (bid > $0.70 at T+240s) | 80% WR but -$0.148/share avg — asymmetric losses kill it |
| Naive symmetric MM ladders (H2 historical) | 19.4% WR live — adverse selection + tight spreads + queue position |
| $10-15 fade buy-and-hold | 47% WR maker-bid, -$0.018/share — borderline negative |
| Aligned scalp with hold fallback ($0.04 TP, hold if miss) | Lower P&L than pure hold across all buckets — TP caps wins, hold takes full losses |
| Aligned hold for $15+ | 17-31% WR, -$0.04 to -$0.25/share — big signals are exhausted not extending |

---

## Statistical hygiene

Before adding any new strategy to this document:
- Sample size n ≥ 100
- Report 95% confidence interval on win rate
- Validate on the replay engine against the tick data
- Confirm the underlying mechanic makes sense (not just lucky stats)

Past mistake: claimed "$10+ has 58% continuation" based on n=19. At n=834 the true rate was 32%. Don't repeat.

---

## Update log

| Date | Change |
|---|---|
| 2026-05-21 | Initial document. S1, S2, S3 backtested; S4 proposed pending validation. |
| 2026-05-21 | Updated to distinguish CEILING vs REALISTIC estimates throughout. Added Execution Headwinds section and "What we DON'T know yet" section. Backtest numbers are CEILINGS only. All realistic estimates explicitly labeled as educated guesses, not validated. |
| 2026-05-21 | Added S5 (V4-LIVE legacy scalp) with documented live losses. |
| 2026-05-22 | **Major update — measured all strategies via PaperOrderEngine.** Key findings: (1) S1 taker entry beats maker entry, contradicting earlier guidance. (2) None of S1 buckets statistically excludes zero — need ~3-5× more data for confidence. (3) S2 is the strongest measured signal (+$250/day taker). (4) S3 reactive indicator is broken — confirmation cost eats the swing. (5) Added explicit statistical confidence intervals and "P(true edge < 0)" probabilities for each S1 bucket. |
| 2026-05-22 | Added S3-v2 ($0.06+ amplitude filter) and S4 measurement results. S3-v2 still loses (-$418/day) — amplitude filter helps selection but confirmation cost remains. S4 measures -$514/day but this is worst-case adverse selection only; true S4 unknown until trade-event simulation. |
