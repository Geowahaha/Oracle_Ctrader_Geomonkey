# Self-Mutation Loop — Specification v1

Author: Hermes (Opus 4.7 / GPT-5.5 collaborative session, 2026-05-17)
Status: Foundation — all components ship behind `SELF_MUTATION_ENABLED=0`.

## Mission

The system must teach itself. Every loss is a lesson; every config knob is a gene;
every config delta is a mutation; every shadow backtest is natural selection. The
operator should not have to retune by hand — the system proposes, validates, and
promotes its own improvements.

## Non-goals

- Replacing human judgement on strategy direction (we tune knobs, not invent strategies).
- Touching proven config keys outside the whitelist.
- Running live without canary phase.
- Live order placement during evaluation — runner is counterfactual only.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         Self-Mutation Loop                           │
│                                                                       │
│  execution_journal (cTrader)                                          │
│        │                                                              │
│        ▼                                                              │
│   ┌────────┐    loss > $X     ┌────────────┐                          │
│   │ Trigger│ ───────────────► │  Sampler   │                          │
│   └────────┘                  │ (3 mutants)│                          │
│                               └─────┬──────┘                          │
│                                     │                                  │
│                                     ▼                                  │
│                              ┌──────────────┐                          │
│                              │   Runner     │   counterfactual         │
│                              │ (14d replay) │   over journal           │
│                              └─────┬────────┘                          │
│                                    │                                   │
│                                    ▼                                   │
│                              ┌──────────────┐                          │
│                              │   Verdict    │   pick top mutant        │
│                              │ (Sharpe,     │   vs baseline            │
│                              │  WR, MaxDD)  │                          │
│                              └─────┬────────┘                          │
│                                    │ passed                            │
│                                    ▼                                   │
│   ┌────────────┐                                                       │
│   │  Promoter  │   write canary override → 24h shadow watch            │
│   │            │   if canary wins → promote to main override           │
│   └─────┬──────┘                                                       │
│         │                                                              │
│         ▼                                                              │
│   ┌────────────┐                                                       │
│   │  Rollback  │   7d post-promotion underperformance → revert         │
│   └────────────┘                                                       │
│                                                                       │
│         All transitions → Ledger (SQLite) + Telegram                  │
└──────────────────────────────────────────────────────────────────────┘
```

## Components

### 1. `learning/self_mutation/types.py`
Shared dataclasses: `LossEvent`, `Mutation`, `BacktestOutcome`, `Verdict`,
`Promotion`, `Rollback`. All frozen, type-annotated.

### 2. `learning/self_mutation/ledger.py`
SQLite-backed audit ledger. Tables:
- `mutations` — every candidate generated.
- `verdicts` — runner outputs.
- `promotions` — canary/main transitions with timestamps.
- `rollbacks` — auto-revert events.
- `cooldowns` — per-knob 24h cooldown index.

### 3. `learning/self_mutation/overrides.py`
Reads/writes `data/runtime/self_mutation_overrides.json`. Schema:
```json
{
  "main": {"<knob>": {"value": float, "applied_utc": str, "mutation_id": str}},
  "canary": {"<knob>": {"value": float, "applied_utc": str, "expires_utc": str, "mutation_id": str}}
}
```
Atomic write via temp + rename. Reader caches with mtime invalidation.

### 4. `learning/self_mutation/sampler.py`
Whitelist of mutable knobs with per-knob `(min, max, step, narrative)`. For a
loss event, score knobs by `relevance(loss_context)` and sample top-3 directional
deltas. Cooldown-aware: skip knobs touched in last 24h.

**Whitelist (v1):**
- `XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER` — bounds `(0.10, 1.0)`
- `XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE` — bounds `(4, 10)`
- `XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_BIAS` — bounds `(0.50, 0.90)`
- `XAU_OPENAPI_ENTRY_ROUTER_LIMIT_RETEST_RISK_RATIO` — bounds `(0.04, 0.20)`
- `XAU_GUARDIAN_RUNNER_PRESERVE_R` — bounds `(0.8, 3.0)`
- `CTRADER_XAU_PAIR_RISK_MAX_USD` — bounds `(1.0, 10.0)`
- `RISK_PER_TRADE` (global) — bounds `(0.005, 0.03)`

### 5. `learning/self_mutation/runner.py`
**Counterfactual replay** over `execution_journal` for last `lookback_days`:
1. Read closed positions with realized PnL.
2. For each mutation, recompute PnL as if knob delta were active.
3. Aggregate per-mutation outcome: total PnL, win count, max drawdown, Sharpe-like.

Supported knob types (recomputable from journal alone):
- `*_RISK_MULTIPLIER` → scale realized PnL proportionally.
- `*_MIN_SCORE` / `*_MIN_BIAS` → filter rows by stored score; rows below threshold get
  PnL = 0 (counterfactually not entered).
- `*_PRESERVE_R` → recompute closes that hit early TP partials.
- `*_RISK_MAX_USD` → cap per-thesis risk.
- `RISK_PER_TRADE` → scale all PnL proportionally.

Knobs requiring full market replay return `outcome.kind = "needs_pts"` and the
Verdict engine treats them as inconclusive (no promotion).

### 6. `learning/self_mutation/verdict.py`
For each mutation:
- `pnl_delta = mutation.pnl - baseline.pnl`
- `wr_delta = mutation.win_rate - baseline.win_rate`
- `maxdd_delta = baseline.maxdd - mutation.maxdd` (positive = improvement)
- `t_score` ≈ welch-style on per-trade returns (light-weight; no scipy).

Pass if (all):
- `pnl_delta >= MIN_PNL_DELTA` (default $30 over 14d)
- `maxdd_delta >= -MIN_MAXDD_TOLERANCE` (drawdown not worse by more than $20)
- `t_score >= MIN_T_SCORE` (default 1.0)
- `mutation.n_trades >= MIN_N_TRADES` (default 5)

Pick the top passing mutation by `pnl_delta`. If none pass → `verdict.passed = False`.

### 7. `learning/self_mutation/promoter.py`
On passing verdict:
1. Write `canary` override with `expires_utc = now + canary_hours`.
2. Telegram: "Mutation M{id} promoted to canary: {knob} {old}→{new}".
3. After `canary_hours`, run a post-canary verdict (last 24h):
   - If canary outcome `>= baseline + 0.5 * MIN_PNL_DELTA` → promote to `main`.
   - Else → revert canary.
4. Telegram on both branches.

### 8. `learning/self_mutation/rollback.py`
After promotion to `main`, evaluate `rollback_window_days` (default 7) post-promotion:
- If main outcome `< baseline - MIN_PNL_DELTA` (i.e. promoted change is hurting) → revert.
- Mark rollback in ledger with reason.
- Cooldown the knob for 7 days.
- Telegram alert.

### 9. `learning/self_mutation/trigger.py`
Polls `execution_journal` every `poll_seconds` for new closed losses where
`execution_meta_json.closed.pnl_usd < -loss_threshold_usd`. Deduplicates by
position_id (won't re-trigger). Emits `LossEvent` to governor.

### 10. `learning/self_mutation/governor.py`
Top-level orchestrator. Single entry: `Governor.tick()`:
1. Pull pending loss events from trigger.
2. For each: sampler → runner → verdict → promoter.
3. Evaluate active canaries: post-canary verdict.
4. Evaluate active main mutations: rollback check.

Idempotent: every step persisted; safe to restart mid-flight.

## Safety guarantees

1. **Feature flag** — `SELF_MUTATION_ENABLED=0` default. Even when modules
   are imported, no override files are written and no Telegram is sent.
2. **Whitelist** — Only knobs in `MUTABLE_KNOBS` can mutate. Hardcoded.
3. **Sanity bands** — Every knob has min/max bounds enforced at mutation time.
4. **Cooldown** — One mutation per knob per 24h max.
5. **Kill switch** — `SELF_MUTATION_KILL_ALL=1` removes all overrides (canary + main)
   at startup. Used for emergency rollback by operator.
6. **Atomic ledger** — All transitions through SQLite transactions.
7. **Idempotent overrides file** — Atomic write via temp + rename.
8. **Telegram on all transitions** — Operator never surprised.
9. **Audit trail** — Every step logged with timestamps, reasons.
10. **Counterfactual-only runner** — Never places orders during evaluation.

## Config flags

```
SELF_MUTATION_ENABLED=0                # master kill switch (default OFF)
SELF_MUTATION_KILL_ALL=0               # emergency: drop all overrides
SELF_MUTATION_LEDGER_PATH=data/runtime/self_mutation_ledger.db
SELF_MUTATION_OVERRIDES_PATH=data/runtime/self_mutation_overrides.json
SELF_MUTATION_LOSS_THRESHOLD_USD=20.0  # triggers a mutation cycle
SELF_MUTATION_LOOKBACK_DAYS=14         # backtest window
SELF_MUTATION_MIN_PNL_DELTA=30.0       # verdict pass threshold (per 14d)
SELF_MUTATION_MIN_MAXDD_TOLERANCE=20.0 # drawdown can be worse by this much only
SELF_MUTATION_MIN_T_SCORE=1.0
SELF_MUTATION_MIN_N_TRADES=5
SELF_MUTATION_CANARY_HOURS=24
SELF_MUTATION_ROLLBACK_WINDOW_DAYS=7
SELF_MUTATION_COOLDOWN_HOURS=24
SELF_MUTATION_NOTIFY_TELEGRAM=1
SELF_MUTATION_DRY_RUN=1                # default ON for first 30 days (logs only)
```

## Integration sketch (NOT implemented in v1)

Once the v1 ships green:
1. Wire `Config.__getattr__` (or a wrapper) to consult `overrides.read_main_override(key)`
   before falling back to env-based default.
2. Add a Telegram command `/self_mutation_status` to inspect active overrides.
3. Add `/self_mutation_revert <knob>` for manual revert.
4. Schedule `governor.tick()` every 15 minutes from `scheduler.py`.

## Test plan

- Sampler: respects whitelist, sanity bands, cooldown.
- Runner: counterfactual math is exact for `RISK_MULTIPLIER` scaling.
- Verdict: pass/fail thresholds at boundary; tie-break by `pnl_delta`.
- Ledger: idempotent inserts; reads survive restart.
- Overrides: atomic write; concurrent reader sees consistent state.
- Promoter: canary expiry → re-verdict → main or revert.
- Rollback: 7d underperformance triggers revert; cooldown applied.
- Governor: full loop with a synthetic loss event end-to-end.

## Rollout plan

- Week 1 — `SELF_MUTATION_DRY_RUN=1` (no overrides written, only ledger logs).
- Week 2 — `SELF_MUTATION_DRY_RUN=0`, but `Config.__getattr__` integration NOT
  wired yet (overrides written but not read by trading code).
- Week 3 — Wire `Config.__getattr__`. Canary phase becomes real.
- Week 4 — Full activation, including `main` promotions.

Each week gate requires owner approval after reviewing ledger + Telegram log.
