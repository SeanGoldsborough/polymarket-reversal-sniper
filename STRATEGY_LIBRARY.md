# Strategy Library

Canonical reference for all validated profitable strategies for the Polymarket BTC 5-min binary market. Each strategy here has been backtested against 207 windows (~5.5 days, 6,503+ CB signals, 17,326 swing events) of tick-level data with bilateral order book + Coinbase BTC price aligned at sub-second resolution.

**All P&L numbers below assume maker-bid entry (no taker fees) unless noted.**

---

## Quick Reference

| ID | Strategy | Trigger | Action | Hold style | Validated edge |
|---|---|---|---|---|---|
| S1 | CB-aligned hold (small) | CB ±$5-9 in 1s | Buy aligned | To window resolution | +$0.022/sh avg, 48-53% WR |
| S2 | CB-fade hold (large) | CB ±$15+ in 1s | Buy opposite | To window resolution | +$0.05 to +$0.25/sh, 68-83% WR |
| S3 | Swing capture | Local-low indicator | Buy near low, sell at next high | Seconds to minutes | +$0.009/sh, 63% WR, 17K events |
| S4* | Conditional MM | Spread > $0.03 | Maker ladder both sides | While quoted | Not yet validated — proposed |

\* S4 is the proposed hybrid that integrates with S1-S3 — see "Composite Strategy" section.

---

## Data validation context

- **Tick data source**: `/home/ubuntu/reports/ticks/` on Ireland EC2 (54.246.52.236), captured by `tick_recorder.py` 2026-05-12 through 2026-05-18
- **Total ticks**: ~17.6M across 207 windows
- **Format**: bilateral order book (up/down bid+ask+sizes) + Coinbase BTC price, ~286 ticks/sec
- **Statistical bar**: Any claim of an edge requires n ≥ 100 and a stated confidence interval

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

### Validated profitability

| Signal bucket | n | WR (P&L > 0) | $/share | Net at 10sh × n |
|---|---:|---:|---:|---:|
| $5-6 | 2,105 | 53% | +$0.035 | +$733 |
| $6-7 | 1,119 | 48% | +$0.022 | +$246 |
| $7-8 | 679 | 49% | +$0.017 | +$116 |
| $8-10 | 759 | 48% | +$0.030 | +$230 |
| **Combined** | **4,662** | **~50%** | **~$0.028 avg** | **+$1,325** |

Over the 5.5 day sample at 10sh: **~$241/day**. At 100sh: **~$2,410/day**.

### Caveats

- Maker fill is not guaranteed; may sit unfilled if bid moves away fast
- Ties up capital for full 5-min window
- Per-trade P&L is modest; volume comes from frequency

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

### Validated profitability

| Signal bucket | n | WR (P&L > 0) | $/share | Net at 10sh × n |
|---|---:|---:|---:|---:|
| $15-20 | 93 | 68% | +$0.059 | +$55 |
| $20+ | 71 | 83% | +$0.246 | +$175 |
| **Combined** | **164** | **~75%** | **~$0.14 avg** | **+$230** |

95% CI on $20+ WR: 69-88% — even pessimistic bound is profitable.

Over 5.5 days at 10sh: **~$42/day**. At 100sh: **~$420/day**. Rare events but high per-event P&L.

### Caveats

- Small sample (n=164 combined). Wider CI than S1
- Requires monitoring CB ticks for $15+ moves (rare)
- $20+ signals are flash events — must be detected within 1-2 second window

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

### Validated profitability

| Setup | n | WR | Avg P&L/share | Total at 10sh | Per day at 10sh |
|---|---:|---:|---:|---:|---:|
| 200ms reactive (modeled) | 17,326 | 63% | +$0.0090 | +$1,560 | **~$284/day** |
| 1500ms reactive (slow) | 17,326 | 56% | +$0.0037 | +$636 | ~$115/day |
| Perfect oracle (ceiling) | 17,326 | 95% | +$0.050 | +$8,722 | ~$1,585/day (theoretical) |

Hour-of-day breakdown — **every hour profitable**, best evening (20-22 ET), worst US market open (10 ET, 54% WR).

Realistic expectation when accounting for indicator lag + queue position: **55-58% WR, ~$0.005-$0.007/share, ~$150-$200/day at 10sh**.

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

**Status: needs validation via the replay engine and order execution module before any live deploy.**

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

### Expected combined daily P&L

| Strategy | 10sh per signal | 100sh per signal |
|---|---:|---:|
| S1 (CB aligned $5-10) | ~$241/day | ~$2,410/day |
| S2 (CB fade $15+) | ~$42/day | ~$420/day |
| S3 (swing, realistic) | ~$150-200/day | ~$1,500-2,000/day |
| S4 (MM, estimated) | TBD | TBD |
| **Combined target** | **~$430-480/day** | **~$4,300-4,800/day** |

Real-world execution discount: assume 50-70% of theoretical due to fills/queue/slippage. So **$215-340/day at 10sh, $2,150-3,400/day at 100sh**.

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
| 2026-05-21 | Initial document. S1, S2, S3 validated; S4 proposed pending validation. |
