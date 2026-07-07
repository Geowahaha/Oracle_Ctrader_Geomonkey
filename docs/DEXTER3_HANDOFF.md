# DEXTER3 — MASTER HANDOFF (seamless takeover for any model)

> **Read this ONE file before touching Dexter3.** It carries the mission, the design philosophy,
> the working rules, what is live right now, and the forward plan. Author: Claude Fable 5 (PM/co-founder),
> 2026-07-07. If you are a new model taking over: your job is to keep this system **profitable and
> alive**, never to "improve" it into breakage. Read the Golden Rules, then act.

---

## 0. THE MISSION (owner's words, non-negotiable)

Build a leader-market intraday engine on the cTrader MCP/OpenAPI hybrid that makes **at least $100/day
on a $1000 virtual capital base (≥10%/day)** — aggressively but survivably. Enter every M5 close,
hunt profit like the best trader alive, repair losing baskets toward net-positive then close all,
and **lock the day once the target is hit**. Demo account only until readiness gates pass.

**Honest framing already agreed with the owner:** 10%/day *compounded forever* is mathematically
impossible and "never lose" is impossible. The buildable version = **capture big green days, cap red
days, press winners, lock the target**. Report REAL numbers (from `get_deals`), never fantasy. Do not
re-lecture the owner on this — they know; execute and measure.

---

## 1. GOLDEN RULES (break these and you break the system)

1. **NEVER modify the live loops of other agents.** `scripts/xau_scalp_monitor.py`,
   `scripts/btc_scalp_monitor.py`, `scheduler.py`, `execution/`, `api/`, `config.py` are OFF-LIMITS.
   Dexter3 is a **standalone additive package `dexter3/`** with its own label + lock. codex owns the
   M1 scalp loops; we never touch them. Coordinate in `docs/AGENT_SYNC_BOARD.md`.
2. **Additive only.** Improvements extend; they never overwrite proven logic. New behavior lands
   behind an env flag / config default so it can be turned off without a code edit.
3. **Label + lock isolation.** Every order carries `LABEL = "dexter3:fable:m5h-v1"`. Own lock
   `data/runtime/dexter3_loop.lock`. We ignore foreign labels; peers ignore ours.
4. **Demo only.** Executor hard-refuses unless `get_balance().traderId ∈ {9922808, 3555162}` (same
   demo account, two ids). Never widen this without the owner.
5. **Broker re-read after every mutation.** No assumed fills. Mutating MCP calls get ONE attempt then
   reconcile (see `McpMutationUncertain` — a blind retry caused a live double-fill on 2026-07-05).
6. **Hard caps are unbreachable.** Basket caps (`max_legs`, `max_basket_risk_mult`, `time_stop_min`,
   `daily_loss_baskets`) live at a single choke-point (`basket_live.enforce_caps`). The governor is a
   layer ABOVE — it may only stop entries / close all / shape size, never raise a cap.
7. **Tag before risky change.** Rollback anchors exist: `git tag v1.0-dexter3-mission`,
   `v1.1-dexter3-truerisk`. Tag a new `vN` before any structural change so the owner can roll back.
8. **Tests are the contract.** ~660 dexter3 tests. Run `python -m pytest tests/test_dexter3_*.py -q`
   before every commit. One known pre-existing failure: `test_dexter3_skipeval.py::
   test_fear_cost_summary_aggregates_evaluated_rows_only` (date-window flake, unrelated — leave it).
9. **Verify against real deals, not vibes.** Truth = `Dexter3McpClient().call('get_deals', {...})`
   filtered to label `dexter3:fable`, field `netProfit`. The internal journal is telemetry, not P&L.
10. **The loop must never die.** MCP zombie → log + sleep 60 + continue. Any other exception → log +
    continue. This is why the loop survives cTrader restarts.

---

## 2. WHAT IS LIVE RIGHT NOW (2026-07-07, verify before trusting)

- **Process:** `python -m dexter3.shadow_runner --symbols XAUUSD --loop --poll-sec 20 --live`
  running in background, single instance under `data/runtime/dexter3_loop.lock`. Version = V1.1.
- **Account:** demo, traderId `3555162` (login 9922808), balance ≈ $10,600 (the *demo* balance; the
  governor's `$1000` is a **virtual capital base** for sizing, independent of demo equity).
- **Exact launch env (this IS the current config — reproduce to restart):**
  ```
  DEXTER3_LIVE=1 DEXTER3_HUNT=1 \
  DEXTER3_CAPITAL_USD=1000 DEXTER3_DAILY_TARGET_USD=100 DEXTER3_DAILY_LOSS_USD=50 \
  DEXTER3_MAX_VOLUME_UNITS=10 DEXTER3_MAX_ENTRIES_PER_DAY=60 DEXTER3_DAILY_LOSS_BASKETS=3 \
  DEXTER3_FAST_TICK_SEC=4 \
  DEXTER3_OM_ARM_R=0.4 DEXTER3_OM_TRAIL_KEEP=0.70 DEXTER3_OM_TAKE_R=1.2 DEXTER3_OM_SPIKE_R=2.5 \
  DEXTER3_ARM_TRAIL_R=0.35 DEXTER3_TRAIL_KEEP_FRAC=0.65 DEXTER3_TAKE_R=1.0 DEXTER3_RESOLVE_TARGET_R=0.25 \
  python -X utf8 -m dexter3.shadow_runner --symbols XAUUSD --loop --poll-sec 20 --live
  ```
- **MCP health:** local server `http://127.0.0.1:9876/mcp/`. It zombies (HTTP 404) periodically.
  A Windows scheduled task `DexterCtraderMcpWatchdog` auto-restarts cTrader every 2 min, windowless
  (runs via `pythonw.exe`; `ctrader_app_restart.py` uses `CREATE_NO_WINDOW`). Manual:
  `python scripts/ctrader_mcp_watchdog.py --restart`. Runbook: `docs/MCP_ZOMBIE_RECOVERY_RUNBOOK.md`.

**To restart the loop safely:** (1) `taskkill /PID <lock pid> /F`; (2) verify broker flat/known via
`get_positions`; (3) relaunch with the env block above. The loop auto-adopts any open lane basket and
the OM/governor resume managing it.

---

## 3. ARCHITECTURE — the layers (data → decision → execution → defense → governance)

```
market data (MCP trendbars M5/M15/H1 + spot)
   │
   ▼
market_lens.py    ── pure leader tools: swing structure, liquidity sweep+reclaim, displacement,
                     compression→release, close-location pressure, day-range position, session,
                     volatility quantiles, composite leader_score. NO legacy indicators.
   │
   ▼
hunt_mode.py      ── PARTICIPATION-FIRST decision on every M5 close. 8-member weighted committee
                     (CLP, swing, day-range tilt, displacement, compression, M15 OLS drift 1.3,
                     h1_context 0.9, sweep-reclaim override) ALWAYS picks a side + conviction.
                     Trend-agreement guard halves size / flips side on counter-trend (no skip).
                     Only 3 hard vetoes (insufficient bars, bad quote, spread blowout).
                     Geometry: SL structural ≥max(6×spread, TRq50); TP ≥max(1.2R, 8×spread).
   │  (hunter_brain.py = the older selective "sniper" brain; still used for catch-up telemetry.
   │   Entries use hunt_mode when DEXTER3_HUNT=1.)
   ▼
executor.py       ── the ONLY module allowed to place/amend/close. Demo gate, pre-flight gates
                     (quote sanity, SL/TP sidedness, min-volume/step, duplicate-label, daily caps),
                     risk sizing (risk_usd / SL distance → units), post-fill broker re-read + verify,
                     naked-SL repair. Accepts risk_usd_override (governor sizing). Mutation-uncertain
                     reconcile (no blind retry). execute_close_all / execute_repair_leg for baskets.
   │
   ▼
basket_live.py / basket_manager.py ── the OPEN position set is a basket. aggregate_lane() computes
                     real broker-PnL aggregate_r; structure_evidence(); decide_basket_action()
                     (hold / repair / hedge / close_all_in_profit / cap_stop). enforce_caps() is the
                     single unbreachable cap choke-point. Peak-R trailing lives here + in OM.
   │
   ▼
opening_manager.py (OM) ── FAST INTRABAR DEFENSE, ~4s tick (not M5). This is the "hunter, not the
                     fearful holder." Profit Hunter: CONTINUOUS peak_r + ratcheting trail (arm/keep/
                     take/spike, floor only rises) → close all at tick resolution. Basket Doctor:
                     measures edge NOW via hunt committee, opens repair leg in the WINNING direction
                     (counter_trend_recovery) to drag aggregate positive, else same-side-better, else
                     hold. Priority: cap-stop > profit > repair.
   │
   ▼
daily_governor.py ── THE MISSION LAYER. status(realized+floating): ≥+$100 → TARGET_LOCKED (close all,
                     🎯 lock, no entries till next UTC day); ≤−$50 → LOSS_STOPPED (close all, 🛑).
                     Floating counts BOTH ways. risk_for_entry(): anti-martingale ladder
                     ×1.0/1.3/1.6/2.0 by win streak (stateless from today's deals, reset on loss) ×
                     session multiplier (overlap 1.2/london 1.0/ny 1.1/asian 0.6/off 0.5) on the
                     $1000 base (1.2%≈$12, cap 2.5%≈$25, floor $1). It can ONLY stop/close/size.
   │
   ▼
shadow_runner.py  ── the loop. run_loop ticks every fast_tick_sec: each tick runs OM on open lanes +
                     the governor gate; every ceil(poll_sec/fast_tick_sec) ticks runs the M5 entry
                     path (run_once → run_symbol_cycle → hunt decision → executor). Single process,
                     single lock. decision_journal.py = SQLite telemetry (decisions, basket_events,
                     exec_events). empirical_stats.py / skip_evaluator.py = learning telemetry.
```

**Key data-flow invariants (bugs were found here — respect them):**
- `aggregate_r` R-base MUST be the lane's **actual** risk = `Σ|entry−SL|×volume` (`_lane_actual_risk_usd`),
  NOT the static config risk. Getting this wrong (V1.0 bug) inflated R ~29× and made the trail bank
  winners at pennies.
- `get_deals` rows: timestamp field is **`time`**; realized P&L is **`netProfit`**; **zero-pnl rows are
  the open side of a deal** — exclude them or they corrupt realized-today and the win streak.
- Bars are labelled by **OPEN** time; `ts_close = open + period` (`hunter_brain._bar_close_ts`).
- MCP tool for quotes is **plural** `get_spot_prices`; a symbol with no open chart returns "no live
  quote" → `mcp_client.get_spot_price` runs the open_chart subscribe chain.

---

## 4. WORKING RULES (how to develop here without breaking it)

1. **PM/coder split.** Fable (or whoever holds this seat) is brain/PM: diagnoses from real data,
   designs, reviews, commits, deploys, updates the sync board. Heavy coding is delegated to a Sonnet
   sub-agent with a *precise spec* (constraints, files-allowed, tests-required, no-commit, no-live-run).
   The PM verifies tests + a smoke proof before deploying.
2. **Every change ships with:** unit/property tests, a green full-suite run, a one-line smoke that
   proves the behavior, a commit with a clear "why + evidence" body, a push to remote `dexter`, and a
   `docs/AGENT_SYNC_BOARD.md` activity entry.
3. **Deploy = stop loop → commit → push → relaunch with the env block → confirm startup lines →
   watch first live effect.** Never edit the running code in place expecting it to hot-reload.
4. **Diagnose from `get_deals`, not the journal.** The three V1 bugs were only visible in real broker
   P&L vs journaled telemetry. Reconcile the two whenever numbers feel off.
5. **When you find a bug in a shared script** (e.g. the window popup in `ctrader_app_restart.py`), the
   fix must be surgical + additive (a flag, a guard) and justified to the owner — never a rewrite.
6. **Commit message + PR discipline:** end commit messages with the Co-Authored-By line. Pushing
   straight to `deploy-xau-family-canary` is the norm (owner is sole approver); a GitHub PR into `main`
   is only for a stable-release snapshot.
7. **Memory:** durable facts go to `C:\Users\mrgeo\.claude\...\memory\` (see
   `project_dexter3_mission_100_per_day.md`, `project_dexter3_m5_hunter_2026_07_05.md`).

---

## 5. HOW WE THINK ABOUT EDGE (design philosophy)

- **Participation-first, intelligence in management.** We enter every M5 (the owner's mandate); the
  edge is not in *whether* to trade but in *direction/size/geometry* and above all in **managing the
  open position aggressively** (OM) and **governing the day** (governor).
- **Hunger, not fear.** The disease we killed: winners rode back to negative because positions were
  only checked every 5 min. Cure: continuous peak-R + ratchet trail at 4s. Never give a big winner
  back to breakeven.
- **Repair with edge, not blind hedge.** A losing basket is repaired by measuring which side wins NOW
  and opening in that direction to drag the aggregate positive, then close all ("รวบยอด").
- **Asymmetric math is the whole game.** PF>1 requires avg_win ≥ avg_loss. We lost at PF 0.61 because
  we cut winners at 0.2R and let losers run to full SL. Every future change is judged by: does it make
  winners bigger or losers smaller *without* raising ruin risk?
- **Press winners, cap losers, lock the target.** Anti-martingale (size UP on streaks, reset on loss)
  + daily loss-stop + target-lock is how prop traders post big days without blowing up. Martingale
  (size up on losses) is forbidden — it's the hidden-ruin pattern the owner explicitly rejected.

---

## 6. KNOWN-GOOD BASELINE & OPEN RISKS

- **Baseline:** V1.1 (`b12785c`), ~660 tests green (1 known unrelated flake). Governor + OM + hunt +
  true R-base all live. Tags `v1.0-dexter3-mission`, `v1.1-dexter3-truerisk` for rollback.
- **Not yet proven:** sustained PF>1.2 over a full clean session on V1.1. The 12-0 run happened under
  the inflated-R bug (winners were tiny); V1.1's real expectancy needs a fresh measurement window.
- **Open risks to watch:** (a) MCP zombie frequency — the watchdog covers it but heavy 4s polling adds
  load; raise `DEXTER3_FAST_TICK_SEC` first if strained. (b) XAU M5 moves may rarely reach 1R — if
  live winners cluster <0.35R, lower `arm_trail_r` or rethink hunt exit geometry. (c) Governor realized
  window = 500 deals; a very busy day could still truncate — widen if a mission day exceeds it.

---

## 7. FORWARD PLAN (priority order — do the top item, measure, then reconsider)

1. **MEASURE V1.1 first.** Run one clean session, then
   `python scripts/dexter3_pnl_backtest.py` + a `get_deals` PF pull. Confirm winners now bank at real
   0.4–1.2R and the ladder presses streaks. Do NOT stack new features before this reads true.
2. **House-money ratchet toward ≥10%/day** (env-flagged, off by default): once the +$100 target is
   banked, instead of hard-stopping, raise the floor to +$100 and let a *house-money* sub-session run
   with tighter risk — capped downside (never below the locked +$100), open upside. This is the honest
   path to >10% days without martingale. Tag before shipping.
3. **Empirical p_win feedback:** wire `empirical_stats` real rates into hunt sizing/conviction once the
   journal has enough closed trades — bigger size on setups that actually win.
4. **BTCUSD second lane** (24/7) once XAU V1.1 is proven — same stack, per-symbol governor accounting.
5. **Readiness gates** (`docs/XAU_MCP_REAL_MONEY_READINESS_PLAN.md`): PF>1.2, ≥100 trades / 4 weeks,
   controlled drawdown before any real-money conversation.

---

## 8. FILE MAP (dexter3/)

| File | Role |
|------|------|
| `market_lens.py` | pure leader features (no I/O) |
| `hunt_mode.py` | participation-first committee + geometry (primary entry brain) |
| `hunter_brain.py` | older selective sniper brain (catch-up telemetry) + `_bar_close_ts` |
| `executor.py` | ONLY mutator: gates, sizing, verify, reconcile, close-all/repair-leg |
| `basket_live.py` | real-PnL basket aggregation, structure evidence, decide_basket_action, enforce_caps |
| `basket_manager.py` | BasketConfig + paper basket state machine |
| `opening_manager.py` | ~4s Profit Hunter (ratchet trail) + Basket Doctor (edge repair) |
| `daily_governor.py` | mission layer: target-lock / loss-stop / ladder / session sizing |
| `mcp_client.py` | HTTP client, zombie + mutation-uncertain handling, spot subscribe chain |
| `decision_journal.py` | SQLite telemetry |
| `empirical_stats.py`, `skip_evaluator.py` | learning telemetry |
| `shadow_runner.py` | the loop (fast OM tick + governor gate + M5 entry path) |

Tests: one `tests/test_dexter3_<area>.py` per module. Backtest: `scripts/dexter3_pnl_backtest.py`.
Coordination: `docs/AGENT_SYNC_BOARD.md`. Design: `docs/DEXTER3_M5_HUNTER_BLUEPRINT.md`.

---

## 9. FIRST 10 MINUTES FOR A NEW MODEL

1. Read this file + `docs/DEXTER3_M5_HUNTER_BLUEPRINT.md` + latest `docs/AGENT_SYNC_BOARD.md` entries.
2. `python -m pytest tests/test_dexter3_*.py -q` → expect ~660 pass, 1 known flake.
3. Confirm the loop is alive (`data/runtime/dexter3_loop.lock` pid) and MCP healthy
   (`python scripts/ctrader_mcp_watchdog.py`).
4. Pull real state: `get_balance`, `get_positions`, and today's `get_deals` (label `dexter3:fable`).
5. Update the sync board, then work the top item in §7 — **measure before you build.**

**If in doubt: don't touch the running trade logic. Tag, test, delegate the code, verify, deploy,
measure. Keep it profitable and alive.**
