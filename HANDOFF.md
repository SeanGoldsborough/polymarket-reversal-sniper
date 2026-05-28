# Polymarket Strategy Lab — Session Handoff

**Last updated:** 2026-05-28, ~15:25 ET
**Session ID (for `claude --resume`):** `eef07e03-b6d8-4cae-a772-19261cad8af7`
**Wallet:** $9.57 USDC (down from $16.28 start — S7 live forward-test losses)

---

## 🚨 THE BIG FINDING

After 4 backtest iterations and a live data-collection run, we **identified the live/sim WR gap**:

**Live PM (Polymarket) reacts ~50% faster to Coinbase moves than the backtest tick files suggest.**

| Source | p50 PM lag | aligned% |
|---|---|---|
| **LIVE (885 events, 1hr DRY_RUN)** | **520ms** | **71%** |
| BACKTEST (80,321 events, 434 windows) | 1079ms | 62% |

**Implication:** S7's fade thesis ("CB moves, PM hasn't caught up yet, fade") is structurally broken — live PM catches up before our 1500ms order lands. This explains the live 25% WR (3/12) vs sim 67% WR (n=27).

S18 (aligned momentum) actually benefits from fast PM tracking and is the recommended pivot.

---

## 📍 What's running RIGHT NOW

| Process | Where | Purpose |
|---|---|---|
| **S7 DRY_RUN bot** | EC2 tmux `btcS7dry`, started 18:12 UTC | Collecting PM lag samples (no orders, no risk) — Sean asked to keep running for $10+ samples |
| **S7 LIVE bot** | STOPPED (was tmux `btcS7`) | Manually killed after 6-loss streak |
| **v4 backtest** | EC2 `/home/ubuntu/sniper-dev`, `s7_full_backtest_v4.py` | Calibrates `pm_feed_lag_ms=559` to match live; should be done by ~16:00 UTC |

EC2 SSH: `ssh -i ~/.ssh/polymarket-ireland.pem ubuntu@54.246.52.236`

---

## 🛠️ Tools shipped this session

All deployed to EC2 `/home/ubuntu/polymarket-bot/` and/or `/home/ubuntu/sniper-dev/`:

1. **`circuit_breaker.py`** — rolling-5 WR breaker. Engages if WR < 40%. Integrated into S7 bot via env vars `S7_CB_*`.
2. **`cb_regime_tagger.py`** — CB autocorrelation regime classifier (MOMENTUM/EXHAUSTION/RANGING).
3. **`pm_lag_tracker.py`** — instrumentation that logs CB→PM bid reactions to JSONL. Integrated into S7 bot.
4. **`backtest_split_by_regime.py`** — runs S7/S18 on full 434 windows, splits results by regime.
5. **`s7_full_backtest_v2.py`** — combined backtest with all 4 tools.
6. **`s7_full_backtest_v3.py`** — adds bid-evaporation model (`quote_evap_rate=0.19`, `book_walk_slip=$0.012`).
7. **`s7_full_backtest_v4.py`** — adds `pm_feed_lag_ms=559` (currently running).
8. **`measure_ask_drift.py`** — empirical ask drift on 47 S7 signals (34% reject rate).
9. **`pm_lag_compare.py`** — side-by-side live vs backtest PM lag.
10. **`circuit_breaker.py` `cb_regime_tagger.py` `pm_lag_tracker.py`** — all deployed to live bot.

Also patched:
- **`btc_s7_fade.py`** — DRY_RUN mode (`S7_DRY_RUN=1`), absolute LOG_FILE path (fixed split-log bug), breaker + lag tracker hooks.
- **`strategies.py`** — `S7FadeStrategy(regime_filter_enabled=True)` knob. Uses deque for O(1) CB history maintenance.
- **`order_engine.py`** — `quote_evap_rate` already existed; just set to 0.19 in v3+.

---

## 📊 What the backtests proved

| Config | n | WR | $/day | Verdict |
|---|---|---|---|---|
| v1 baseline (no fixes) | 27 | 67% | +$11.77 | Original sim |
| v2 + post-hoc regime split (RANGING only) | 19 | 74% | +$18.57 | **Data-snooping; not real** |
| v3 + bid-evap | 20 | 65% | +$9.69 | Closes 19pp eval gap |
| v3 + live-realistic regime filter | 12 | **50%** | +$0.64 | Filter HURTS at signal-time |
| **LIVE** | 12 | **25%** | -$4.47 | The reality |
| v4 + PM feed lag (running) | TBD | TBD | TBD | Should approach live |

**Key methodology lesson:** post-hoc regime tagging in v2 was data-snooping. When the classifier uses only info available at signal-time, RANGING is 50% WR (no edge).

---

## 📁 Key files locally (this Mac)

- **CSV for Notion board:** `/tmp/sniper-fresh/strategy_lab_board.csv` (44 tickets)
- **Notion board:** `https://www.notion.so/3bf55eef848046408d288999fa1262a1` — exists, but **Notion MCP isn't loaded yet**. After `claude --resume`, OAuth flow → push updates directly.
- **Pulled S7 bot for inspection:** `/tmp/s7_current.py`
- **Pulled strategies.py:** `/tmp/strategies.py`
- **Pulled order_engine.py:** `/tmp/order_engine.py`
- **Live PM lag data:** `/tmp/live_pm_lag.jsonl` (885 events)
- **Ask-drift distribution:** `/tmp/ask_drift_distribution.json` on EC2

---

## ❓ Open questions / awaiting Sean's decision

1. **What does v4 say?** Does `pm_feed_lag_ms=559` close the sim/live WR gap? (Result will land in `/tmp/s7_v4_full.log` on EC2.)
2. **Pivot to S18 forward-test?** S18 has +$57/day backtest, n=205, and structurally benefits from fast PM tracking. Currently paper-validated, not yet live.
3. **Stop DRY_RUN or keep going?** Sean said keep collecting $10+ CB delta samples (currently 0 live samples in that bucket).
4. **Notion integration:** restart Claude Code to load the MCP and push updates from this session forward?

---

## 🧠 Memory entries written this session

(Saved at `/Users/seangoldsborough/.claude/projects/-Users-seangoldsborough-CineBuild-io-main/memory/`)

The auto-memory index already has these from prior sessions:
- `project_polymarket_mantra.md` — the core edge thesis (read first)
- `project_s7_forward_test.md` — S7 live setup
- `project_s18_5sdelta_strategy.md` — S18 spec
- `reference_live_bot_bug_catalog.md` — 18 bugs, 14-point pre-deploy checklist
- `feedback_always_ask_before_delete.md` — hard rule, includes timestamp-check warning

NEW findings worth saving (haven't written yet this session):
- The PM-lag live/sim gap finding (520ms vs 1079ms) — this is the most important new insight
- v2 regime split was data-snooping (don't repeat)

---

## 🔄 To resume in a new Claude Code session

```bash
cd /private/tmp/sniper-fresh   # or any CineBuild project dir
claude --resume eef07e03-b6d8-4cae-a772-19261cad8af7
```

Or read this file directly — a fresh agent should be able to pick up cold from here.
