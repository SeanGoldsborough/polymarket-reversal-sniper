# SESSION_STATE — point-in-time snapshot
<!-- Companion to HANDOFF.md. HANDOFF has the narrative; this file has the raw state. -->

```yaml
snapshot_at: 2026-05-28T19:33Z   # 15:33 ET
session_id: eef07e03-b6d8-4cae-a772-19261cad8af7
working_directory: /private/tmp/sniper-fresh
ec2_host: ubuntu@54.246.52.236   # Ireland, Polymarket-optimal
ssh_key: ~/.ssh/polymarket-ireland.pem
```

---

## Account / position

```yaml
wallet_usdc: 9.57
starting_usdc: 16.28
drawdown_pct: 41.2          # since S7 forward-test launch
held_positions: none        # all settled
open_orders: none
last_live_trade: "#13 DN $0.61 → $0.00, -$3.89 at 19:25 UTC"
live_trade_total: 12
live_wr: 0.25
live_wins: 3
live_losses: 9
```

---

## Live processes (EC2)

```yaml
btcS7_live:
  state: STOPPED              # manually killed after 6-loss streak
  last_active: ~14:25 UTC
  tmux_session: btcS7         # killed
  reason_stopped: "6-trade losing streak; user paused"

btcS7_dry_run:
  state: RUNNING
  started: 18:12 UTC          # ~1h 21m runtime
  tmux_session: btcS7dry
  pid: 2690781
  env: { S7_DRY_RUN: "1" }
  cmd: /home/ubuntu/botenv/bin/python btc_s7_fade.py
  cpu_pct: 4.0
  mem_mb: 59
  log: /home/ubuntu/polymarket-bot/logs/btc_s7_dry_stdout.log
  pm_lag_log: /home/ubuntu/polymarket-bot/logs/btc_s7_pm_lag.jsonl
  pm_lag_events_collected: 1216    # at snapshot
  s7_signals_fired_in_window: 0    # no $18+ moves this session
  purpose: "Collect live PM-CB lag (esp. $10+ CB delta samples)"
  stop_command: "tmux kill-session -t btcS7dry"

backtest_v4_full:
  state: RUNNING
  started: 19:26 UTC
  pid: 2767073
  cpu_pct: 97.7
  cmd: /home/ubuntu/botenv/bin/python s7_full_backtest_v4.py
  log: /tmp/s7_v4_full.log
  progress: "Config B (v3 evap only) of 4 configs"
  eta: "~10-15 min from snapshot"
  config_a_done:
    name: "Baseline (no fixes)"
    attempts: 41
    fills: 24
    wr: 0.75
    per_day: 13.31

log_tail_processes:
  - pid: 2455534
    cmd: "tail -F btc_s7_stdout.log btc_s7_trades.jsonl"
    state: "harmless tail leftover from earlier session"
```

---

## Engine configuration (current v4 defaults)

```yaml
PaperOrderEngine:
  taker_latency_ms: 1500       # Ireland → Polymarket round-trip
  pm_feed_lag_ms: 559          # NEW v4 — calibrated from live DRY_RUN
  quote_evap_rate: 0.19        # v3 — empirical
  book_walk_excess_slippage: 0.012  # v3 — empirical
  min_notional_usdc: 1.0
  use_trade_events: false
  rng_seed: 42

S7FadeStrategy (live bot config):
  threshold: 18.0              # $18 CB move in 1.5s
  max_gap_ms: 1500
  shares_base: 6
  min_notional_target: 1.20    # Option B dynamic shares
  max_fade_ask: 0.60           # cap (preserves EV at 54% CI floor)
  min_fade_ask: 0.10           # floor (death zone proven 0/5)
  entry_mode: taker_market     # FAK at ask+$0.05
  regime_filter_enabled: false # off by default — hurts WR in v3

S7Bot (live bot env vars in current config):
  S7_CB_ENABLED: "1"           # circuit breaker on
  S7_CB_WINDOW_N: 5
  S7_CB_MIN_WR: 0.40
  S7_CB_PAUSE_HOURS: 4.0
  S7_PM_LAG_ENABLED: "1"
  S7_PM_LAG_CB_THRESHOLD: 1.0
  S7_DRY_RUN: "1"              # CURRENTLY SET in btcS7dry session
```

---

## Files modified this session (Mac → EC2)

```yaml
new_files:
  - circuit_breaker.py       # → polymarket-bot/ + sniper-dev/
  - cb_regime_tagger.py      # → sniper-dev/
  - pm_lag_tracker.py        # → polymarket-bot/
  - backtest_split_by_regime.py  # → sniper-dev/
  - s7_full_backtest_v2.py   # → sniper-dev/
  - s7_full_backtest_v3.py   # → sniper-dev/
  - s7_full_backtest_v4.py   # → sniper-dev/
  - measure_ask_drift.py     # → sniper-dev/
  - pm_lag_compare.py        # → sniper-dev/

patched_files:
  - btc_s7_fade.py:
      changes:
        - "absolute LOG_FILE path (fixes split-log bug)"
        - "RollingWRBreaker integration (env var gated)"
        - "PMLagTracker integration (env var gated)"
        - "S7_DRY_RUN mode (signal log only, no orders)"
      backup_on_ec2: btc_s7_fade.py.v1.preBreaker
  - strategies.py:
      changes:
        - "S7FadeStrategy(regime_filter_enabled, regime_lookback_s, allowed_regimes)"
        - "deque-based CB history (O(1) popleft)"

local_artifacts:
  - /tmp/sniper-fresh/strategy_lab_board.csv     # 44 tickets for Notion
  - /tmp/sniper-fresh/HANDOFF.md                 # narrative resume doc
  - /tmp/sniper-fresh/SESSION_STATE.md           # this file
  - /tmp/live_pm_lag.jsonl                       # copy of EC2 PM lag log (1216 events)
  - /tmp/s7_current.py                           # local copy of patched bot
  - /tmp/strategies.py                           # local copy of patched strategies
  - /tmp/order_engine.py                         # local copy (unchanged)
```

---

## Empirical findings (numbers worth keeping)

```yaml
pm_lag_comparison:
  live_n: 885
  backtest_n: 80321
  live_p50_ms: 520
  backtest_p50_ms: 1079
  delta_ms: 559                # → used as pm_feed_lag_ms in v4
  live_aligned_pct: 71
  backtest_aligned_pct: 62
  by_cb_delta:
    $1-3:  { live_p50: 644, bt_p50: 1111 }
    $3-5:  { live_p50: 393, bt_p50: 963 }
    $5-10: { live_p50: 277, bt_p50: 1095 }
    $10+:  { live_p50: null, bt_p50: 1348 }   # no live $10+ yet
  conclusion: "Live PM is ~50% faster than backtest tick files. Bigger CB moves → bigger gap. Explains S7 live/sim WR gap."

ask_drift_distribution:
  source: 47 historical S7 signals
  reject_rate: 0.34            # vs sim's 15% before v3 — closed with quote_evap=0.19
  median_drift_cents: 2
  by_cb_delta:
    $18-25: { n: 30, median: 0.5, reject_pct: 30 }
    $25-50: { n: 15, median: 3,   reject_pct: 40 }
    $50+:   { n: 2,  median: 26.5, reject_pct: 50 }

regime_distribution_434_windows:
  RANGING: 293     # 68%
  EXHAUSTION: 83   # 19%
  MOMENTUM: 57     # 13%
  INSUFFICIENT: 1  # 0%

backtest_progression_summary:
  v1_baseline:       { wr: 0.67, per_day: 11.77, n: 27 }
  v2_post_hoc_RANGING_only:  { wr: 0.74, per_day: 18.57, n: 19, note: "data-snooping" }
  v3_bid_evap_only:  { wr: 0.65, per_day: 9.69,  n: 20 }
  v3_regime_only:    { wr: 0.50, per_day: 0.64,  n: 12, note: "filter HURTS at signal-time" }
  v4_pm_feed_lag:    { status: "computing" }
  live:              { wr: 0.25, per_day: -4.47, n: 12 }
```

---

## Active tasks (claude code TaskList)

```yaml
in_progress:
  - "#12: Compare S7 live fills vs simulator predictions"
  - "#53: v4 backtest with calibrated pm_feed_lag_ms=559"

recently_completed:
  - "#51: Build live-vs-backtest PM lag comparison tool"
  - "#52: DRY_RUN S7 live data collection (1-2 hours)"
  - "#50: Re-run S7 full backtest with bid-evap + regime filter"
  - "#41-49: All 4 regime-detection tools + supporting work"

planned (not started):
  - S18 live forward-test (paper-validated, awaiting Sean's go)
  - Chart Raiders 5-phase build plan
  - S15 + S16 hybrid (level entry + trailing taker exit)
  - Time-of-day filter
  - VP Phase B backtest
```

---

## What to do next (priority-ordered)

```yaml
1_check_v4_result:
  when: "When v4 backtest finishes (~10-15 min from snapshot)"
  where: "ssh ... 'cat /tmp/s7_v4_full.log'"
  decision_criteria:
    - "If v4 sim WR ≤ 40%: pm_feed_lag fully explains the gap. Recommend pivot to S18."
    - "If 40% < WR ≤ 55%: partial explanation. Look for residual factors."
    - "If WR > 55%: PM feed lag alone insufficient. Investigate adverse selection or sample variance."

2_decide_on_dry_run:
  options:
    a: "Stop DRY_RUN once $10+ samples collected (no live $10+ events yet)"
    b: "Keep running indefinitely as background lag monitor"
    c: "Stop now — we have enough"

3_pivot_decision:
  candidates: [S18 forward-test, build VP Phase B, stay paused]
  default_recommendation: "S18 forward-test — benefits from fast PM tracking, 5x backtest $/day vs S7"

4_notion_integration:
  status: "MCP installed but not loaded — need claude --resume to pick up tools + OAuth"
  blocker: "User chose Path B (CSV-based sync). Can revisit anytime."
```

---

## To resume from this snapshot

```bash
# Option 1 — restore the exact session (best)
cd /private/tmp/sniper-fresh
claude --resume eef07e03-b6d8-4cae-a772-19261cad8af7

# Option 2 — fresh session, read this + HANDOFF
cd /private/tmp/sniper-fresh
claude
# then: "read HANDOFF.md and SESSION_STATE.md, then ssh and check v4 backtest status"
```
