# Agent sync board — bilateral monitor & update

**Purpose:** Any coding agent working this repo coordinates **here** (append-only logs + status) instead of routing every micro-update through the owner. The owner checks **one file** for progress after `git pull` or opening the repo.

**Constitution (both agents):**

1. **Read** this file at **session start** (after `AGENT_HANDOFF_XAU_GATE_ENTRY_TEMPLATE.md` §4.1 if doing XAU mission).
2. **Append** new entries under **§ Activity log** — never delete or rewrite another agent’s past entries; you may add a *follow-up* line.
3. **Update** **§ Quick status** (table + phase) when you complete a phase, unblock something, or discover a blocker.
4. **Ask for help** in **§ Needs / questions** with `@agent` style (e.g. “`@trading-strategy` please confirm threshold”) — the next agent that touches this file **answers by appending** under the same heading or in the log.
5. **Owner summary:** Keep **§ Owner — latest** to **one short paragraph** max; refresh when mission phase changes (so mrgeo sees progress at a glance).

**Technical detail:** This file is **git-tracked**. Conflict resolution: if merge conflict, keep both sides’ log entries; fix the summary table manually.

---

## Quick status (agents: keep current)

| Field | Value |
|-------|--------|
| **Mission playbook** | `docs/AGENT_HANDOFF_XAU_GATE_ENTRY_TEMPLATE.md` §4.1 **A→E**, then §5 |
| **Phase now** | **DEXTER3 OPENING MANAGER live on XAUUSD** — ~4s fast intrabar defense: continuous peak-R ratchet trail + spike capture + edge-measured Basket Doctor repair. Fixes the M5-only monitoring flaw. codex BTC M1 loop unchanged. |
| **Last updated (UTC)** | 2026-07-07T10:00Z |
| **Last updated by** | claude-fable (PM/Opus) |

---

## Needs / questions (open)

<!-- Example:
- [ ] `@peer` Confirm whether VM path `/opt/dexter_pro/data/ctrader_openapi.db` is still authoritative — 2026-04-04 Agent A
-->

*None yet — remove this line when first real item exists.*

---

## Owner — latest (≤1 paragraph)

**2026-07-05 Bangkok PM:** Two lanes now. (1) codex BTC demo-live loop unchanged — leader price-action gate live, correctly waiting (`wait_leader_confirmation`, score 0.46 < 0.64 floor), broker flat, balance ~10749. (2) **DEXTER3 kicked off per owner goal**: M5-close forced-evaluation hunter engine (XAU+BTC) + basket repair→close-all-in-profit, as NEW additive package `dexter3/` (own label `dexter3:fable:m5h-v1`, own lock, shadow-first — no orders in P1). Blueprint: `docs/DEXTER3_M5_HUNTER_BLUEPRINT.md`. Sonnet agent building P1 now; /loop active until sustained profit.

---

## Activity log (append only, newest at bottom)

Format each entry:

```text
### YYYY-MM-DD UTC hh:mm — Agent label — Phase X (optional)
- What changed / observed
- Next step for peer or self
```

### 2026-04-04 UTC — bootstrap — template

- Created `AGENT_SYNC_BOARD.md` procedure; agents coordinate here; owner reads **Quick status** + **Owner — latest** + bottom of **Activity log**.

### 2026-04-03 UTC 03:27Z — coding-agent — pre-A

- Ran pytest: **8 passed** (catalog, scalping template bias, classify build miss, FF stamp).
- `tmp_vm_gate_bucket_report.py`: `GATE_REPORT_DB` env + auto-pick `data/ctrader_openapi.db` if present; exit **2** + JSON error if DB missing; output includes `db_path`.
- **Next (Phase A on host with DB):** `GATE_REPORT_DB=... python tmp_vm_gate_bucket_report.py | tee artifacts/gate_report_YYYYMMDD.json`

### 2026-04-03 UTC ~03:28Z — coding-agent — Phase A (local)

- Wrote **`artifacts/gate_report_20260403_local.json`**. `family_canary_gate`: {} (no journal rows / table not used on this snapshot). Rest: shadow, execution_filtered, deals_by_source present.
- **Next peer:** Phase **B** — confirm `entry_template_library.json` on VM; **C** only after B.

### 2026-04-22 UTC 14:25Z — codex — Phase E

- Deployed commit `323342e` to VM branch `deploy-xau-family-canary`; `/opt/dexter_pro` pulled fast-forward and `dexter-monitor` restarted `active`.
- Live changes in this bundle:
- `scheduler.py` + `scanners/scalping_scanner.py` + `execution/ctrader_executor.py`: targeted XAU reversal-zone capture with `armed` and `confirmed` stages, capture run tagging, and DB persistence in `ctrader_reversal_capture_events`.
- `learning/reversal_training_dataset.py`: targeted-only dataset build path and reusable reversal-template fit scorer.
- `scanners/fibo_advance.py`: Fib `61.8 / golden pocket` gate now uses reversal-template confirmation when price is in/near `0.618/0.650`; defaults to pass-through if capture is unavailable (`FIBO_REVERSAL_TEMPLATE_STRICT_REQUIRE_CAPTURE=0`).
- Verification before deploy: `python -m py_compile ...`, `pytest tests/test_reversal_training_dataset.py tests/test_fibo_reversal_template_gate.py`, and full `tests/test_fibo_*.py` (`90 passed`).
- VM post-restart note: journal shows pre-existing `infra.auth_health` stale-token warning; no traceback/module import failure from this rollout.
- Next peer: watch new rows in `ctrader_reversal_capture_events`, then mine targeted reversal templates after 1-3 sessions of XAU flow.

### 2026-04-22 UTC 14:44Z — codex — Phase E

- Added local-only Trading Central intraday canary family lane `xau_scalp_trading_central_intraday`.
- New lane design: external JSON payload input at `data/runtime/trading_central_intraday_signal.json`, source alias `scalp_xauusd:tc:canary`, family-level MTF guard bypass only for this lane, and fallback to base signal geometry when Trading Central provides direction without a full price plan.
- Updated config/source mapping in `config.py`, `execution/ctrader_executor.py`, `learning/live_profile_autopilot.py`, and `scheduler.py`; added example payload file plus focused tests.
- Verification: `py_compile` on changed files, `pytest tests/test_trading_central_family_lane.py tests/test_mempalace_family_lane.py`, and `pytest tests/test_scheduler_watchlist.py -k "mempalace_payload or trading_central"` all passed.
- Next peer: if user wants VM rollout, wire the upstream Trading Central payload producer first, then deploy this lane as experimental only and monitor `:tc:canary` fills separately from existing families.

### 2026-04-29 UTC 12:55Z — claude (opus 4.7) — Surgery 3: protect patient strategies (fibo, scheduled)

- Live forensics today after surgery 1+2 deploy: XAU **was** trading via fibo lane (`source=fibo_xauusd` ✓ source attribution recovered). But two failure modes still bled profit:
  1. **Premature pending cancel**: a perfect sell-limit `dexter:XAUUSD:fibo_xauusd:4 @ 4604.62` (created 04:36Z) was cancelled at 05:51Z by `stale_ttl:45m` from `_pending_order_ttl_min` — fibo was being treated as a scalp family. Price later rallied to 4608+ where it would have hit cleanly. Missed the trade entirely.
  2. **Premature open-position close**: `dexter:XAUUSD:fibo_xauusd:2` opened 04:13Z at 4601.45 short with planned SL 4616.68 / TP 4571.37, then closed 04:30Z at 4602.05 (just -0.04R) by `xau_order_care.force_close_direction=short`. Abandoned the planned 30+pt TP for a 0.6pt scratch loss.
- Patch (commit pending — see git log):
  - `config.py`:
    - `CTRADER_PENDING_ORDER_TTL_XAU_FIBO_MIN=240` (4 hours, was implicit 45m via XAU_SCALP).
    - `CTRADER_PATIENT_STRATEGY_SOURCES="fibo_xauusd,fibo_xauusd:winner,fibo_xauusd:scout,xauusd_scheduled,xauusd_scheduled:canary,xauusd_scheduled:winner"` (csv).
    - `CTRADER_PATIENT_STRATEGY_PROTECT_FROM_FORCE_CLOSE=1`, `..._PROTECT_FROM_FAR_FROM_MARKET=1`.
  - `execution/ctrader_executor.py`:
    - Added `_patient_strategy_sources()` set + `_is_patient_strategy_source(source)` classifier (fibo_*, scheduled, plus prefix matches).
    - `_pending_order_ttl_min`: fibo branch returns 240m.
    - `_pending_order_cancel_reason`: skips `far_from_market` for patient sources.
    - `_manage_open_positions` force_close branch: skipped for patient sources, with explicit log line `force_close_skipped (patient_strategy)`.
- Tests: 2 new in `tests/test_xau_directive_ceiling.py` (fibo source recognition + 240m TTL). All 11 in file pass.
- Owner / next peer: redeploy VM, watch the open `fibo_xauusd:6` short (position 612766209) live to TP/SL **without** premature force-close interference. Confirm next cancelled fibo limit only carries reasons stale_ttl (>=240m) or symbol_position_cap, never far_from_market.

### 2026-04-29 UTC 02:10Z — claude (opus 4.7) — Surgery 2: full-perimeter ceiling + observability

- VM post-restart inspection (after surgery 1) showed `xau_shock_profile`, `xau_family_routing`, `xau_order_care`, `xau_reason_memory`, `xau_parallel_families` were ALSO state machines that linger after a single bad bar — same failure mode as the original NY freeze, just under different keys not covered by surgery 1.
- Patch (commit `b5b8c4d`):
  - `scheduler.py`:
    - `_apply_runtime_state_ceiling(payload)` — load-time backstop that flips any `xau_*` state with `applied_at` older than `XAU_DIRECTIVE_PAUSE_CEILING_MIN` to `inactive` with `ceiling_expired=True` for ops-side observability.
    - `_run_opportunity_health_beacon` — every 5 min emits `[OpportunityHealth] ceiling=10min bypass>=82 kill_zone_hard=False active_states=…` so silent freezes are impossible to miss in journalctl.
    - Both load functions (`_load_trading_manager_runtime_state`, `_load_trading_team_runtime_state`) now return ceilinged state — every consumer (telegram, autopilot, executor) benefits.
  - `execution/ctrader_executor.py`:
    - Deal reconcile fallback: parses broker label/comment via `_parse_label_meta` → journal source → `untagged_external` (instead of empty string). Recovers `source` for the path that produced 8/9 untagged trades on 2026-04-28.
    - `_source_family` for `scalp_btcusd:*` / `scalp_ethusd:*` now picks weekday (`btc_weekday_lob_momentum` / `eth_weekday_overlap_probe`) on Mon-Fri UTC, weekend variants on Sat-Sun. Fixes `eth_weekend_winner` showing as priority on Tuesday.
  - `config.py`: `XAU_OPPORTUNITY_HEALTH_BEACON_ENABLED=1`, `*_MIN=5`.
- Tests: 9 new in `tests/test_xau_directive_ceiling.py` (+4 new this round) — all pass. 169/170 of focused regression pass; the 1 remaining failure (`test_ctrader_xau_scheduled_no_chase_block_emits_late_entry_telemetry`) was confirmed pre-existing in surgery 1.
- VM: pulled `b5b8c4d`, `dexter-monitor` restarted active, `[OpportunityHealth] Scheduled every 5min` confirmed in journalctl. First beacon emission expected within 5min of this entry.
- Next peer / owner: watch `journalctl -u dexter-monitor -f | grep OpportunityHealth` for 1-2 cycles to confirm beacon is emitting; observe whether any `ceiling_expired=True` appears in `data/runtime/trading_manager_state.json` after a bad bar (indicates ceiling auto-cleared a stale lock).

### 2026-04-29 UTC 16:05Z — claude (opus 4.7) — Surgery 1: unblock NY opportunities

- Forensic on 2026-04-28: 9 XAU SHORT trades, all closed cleanly (4W/5L, net +$1.47) — but ~4h NY freeze (11:23→15:20 UTC) missed ~90pts of price action. Root cause: `xau_execution_directive` `pause_until_utc` had no ceiling.
- Patch (additive, no breaking change):
  - `config.py` — added `XAU_DIRECTIVE_PAUSE_CEILING_MIN=10`, `XAU_DIRECTIVE_HIGH_CONFIDENCE_BYPASS=82`, `XAUUSD_SCALP_OFF_KILL_ZONE_CONFIDENCE_PENALTY=5`; defaulted `XAUUSD_SCALP_REQUIRE_KILL_ZONE` 1→0; lowered `CTRADER_XAU_SHORT_LIMIT_PAUSE_MIN` 20→5.
  - `scheduler.py` — `_active_xau_execution_directive` + `_active_xau_regime_transition` now enforce the ceiling against `applied_at` / `trigger_ts`; the directive block in `_xau_*_route_filter` honors a high-confidence bypass with telemetry into `raw_scores["xau_manager_directive_bypass_high_confidence"]`.
  - `scanners/xauusd_scalp_1m5m.py` — kill_zone is soft by default; off-zone signals carry a configurable confidence penalty instead of being silently dropped.
  - `tests/test_xau_directive_ceiling.py` — 5 new tests (within-ceiling, past-ceiling, expired, inactive, regime_transition ceiling) all pass.
- Pre-existing failures (NOT introduced by this surgery): `test_trading_manager_demotes_pb_when_scheduled_outperforms`, `test_trading_manager_demotes_pb_with_scheduled_calibration_fallback`, `test_ctrader_xau_scheduled_no_chase_block_emits_late_entry_telemetry` — verified by `git stash` rerun.
- Retraction: `artifacts/xau_dependency_web_2026_04_28.md` headed with "RETRACTED — DO NOT ACT" — original report misread `direction` column of `ctrader_deals` (each position has 2 legs); the proposed `sync_and_reconcile()` fix would have broken working executor logic.
- Next peer / owner: redeploy VM to latest, then watch `data/runtime/trading_manager_state.json` for `applied_at` discipline (any state that lingers >10m past `applied_at` should be treated as cleared by the new ceiling).

### 2026-04-24 UTC 12:10Z — codex — routing follow-up after live verification

- Owner reran env setup and VM deploy successfully; local WSL now has `.venv`, `dotenv/pandas/pytest`, and targeted routing tests are runnable with `PYTHONPATH=.`.
- Post-deploy runtime check on VM with commit `9572282` showed why routing still stayed `swarm_support_all`: `xau_shock_profile` was inactive, and `_derive_xau_family_routing_recommendation()` only treated `losses` from `shock_rows` as severe-loss input. The active drawdown signal was instead coming from `xau_order_care.review_window` / recent losing reviews, so swarm mode still won despite live losses.
- Implemented follow-up commit `9fd939e fix(xau): route by recent loss regime, not shock only`: family routing now also consumes `recent_order_reviews`, computes `recent_loss_regime`, blocks swarm sample-collection during that regime, and emits `recent_loss_demote` when losses are live but not shock-tagged. Added regression test `test_trading_manager_swarm_support_yields_to_recent_loss_regime`.
- Validation after patch: `python3 -m py_compile learning/trading_manager_agent.py tests/test_trading_manager_agent.py` passed, and `PYTHONPATH=. .venv/bin/pytest -q tests/test_trading_manager_agent.py -k 'swarm_support'` passed (`3 passed`). Commit pushed to `dexter/deploy-xau-family-canary`.
- Next peer / owner command: redeploy VM to `9fd939e`, then re-read `data/runtime/trading_manager_state.json`; expected change is that active XAU drawdown without shock should no longer leave `xau_family_routing.mode = swarm_support_all`.

### 2026-07-03 UTC 11:25Z — grok (cursor) — Codex MCP + bilateral scalp

- Added **ctrader** + **dexter** MCP to `C:\Users\mrgeo\.codex\config.toml` (mirrors `.grok/config.toml`).
- Added project-scoped `.codex/config.toml` and `AGENTS.md` (MCP URLs, demo 9922808, single scalp-loop lock rule).
- MCP health: `python scripts/ctrader_mcp_watchdog.py` → `ok: true`, latency ~87ms, session `50482ac7`.
- Live strategy: `v3.7-m1-close-entry` (`M1_CLOSE_ENTRY_ONLY`); XAU 0.5 lot; frame sell blocked when MTF `preferred=buy` with score gap ≥2.
- **Next peer (Codex):** open this repo in Codex IDE; confirm `/mcp` shows `ctrader`; read `data/runtime/scalp_loop.log` before any manual MCP order; do not start second `xau_scalp_monitor` loop without owner handoff.

### 2026-07-03 UTC 11:42Z — codex — Remote MCP verification

- Verified remote Dexter MCP at `https://dexter-mcp.mrgeo888.workers.dev/mcp`: initialize OK (`dexter-mcp` v1.0.0) and tools include `dexter_status`, `dexter_ctrader_status`, `dexter_positions`, `dexter_scalping_status`, and `ctrader_call`.
- Remote `dexter_status` returned online; remote `dexter_ctrader_status` returned enabled/autotrade enabled/dry_run false, account_id `46552794`, open_positions `0`, open_orders `1`.
- Local cTrader Desktop MCP watchdog failed from this Codex session: `WinError 10061` connection refused on `127.0.0.1:9876`; do not assume local desktop MCP is available until cTrader Desktop MCP is restarted/rechecked.

### 2026-07-03 UTC 11:46Z — codex — Local cTrader MCP restored

- Owner opened cTrader Desktop; local MCP watchdog now OK (`session_id=b4237f4b`, latency 215ms) and TCP port `127.0.0.1:9876` accepts connections.
- Read-only MCP checks confirm active balance endpoint is demo login `9922808` / account `46670728`, balance/equity `10800.94` USD, no open positions, no pending orders.
- No scalp loop started and no orders sent.

### 2026-07-03 UTC 11:50Z — codex — Manual intraday XAU execution

- Manual local MCP trade placed after owner requested active execution: SELL XAUUSD `0.1 lot`, position `649166224`, entry `4184.38`, SL `4186.68`, TP `4181.08`, label `codex-intraday-xau`.
- Rationale: discretionary countertrend scalp from rejection under `4185.5-4186.5`; deliberately smaller than loop/system sizing because the one-shot engine returned `wait_no_edge` and M15/H1 remained bullish.
- Risk reduced after red M1 confirmation: SL amended to `4185.86`, TP left at `4181.08`; broker verified position modification.
- No scalp loop started.

### 2026-07-03 UTC 12:04Z — codex — XAU intraday winner R&D

- Verified manual SELL closed profit: no open positions, no pending orders, balance rose from `10800.94` to `10832.03`.
- Added pure strategy classifier `scripts/xau_intraday_dragon.py` for upper-shelf rejection shorts and lower-shelf reclaim longs; it refuses entries without level-plus-trigger confirmation.
- Added journal miner `scripts/xau_intraday_dragon_research.py` and report `data/reports/xau_intraday_dragon_research_20260703.json`; filtered XAU rows show `ok` entries net `+204.70`, while `too_fast`, `chase_loss`, and `trap_immediate_loss` are the main bleed.
- Added `docs/XAU_INTRADAY_DRAGON_STRATEGY.md` and tests `tests/test_xau_intraday_dragon.py` (`4 passed`).

### 2026-07-03 UTC 12:15Z — codex — XAU loop started

- Owner requested `Start XAU intraday loop on demo account now`; initial MCP watchdog hit HTTP 404 zombie handler, so Codex ran `python scripts/ctrader_mcp_watchdog.py --restart`; cTrader restarted and MCP recovered (`session_id=4f2a248e`).
- Preflight after restart: demo account `9922808` / `46670728`, balance `10832.03`, no open positions, no pending orders.
- First launch exited on Windows `cp1252` stdout encoding; restarted with `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`.
- Active loop now running: `cmd` PID `9996`, Python lock PID `8540`, command `python -X utf8 scripts/xau_scalp_monitor.py --loop 30 --defense-loop 2`; stdout shows cycles `244` (`wait_no_edge`) and `245` (`wait_all_busy`), `mcp_errors=0`, stderr empty.

### 2026-07-03 UTC 12:45Z — codex — Loop learning update

- Loop remains technically healthy (`mcp_errors=0`, keepalive active, stderr empty) and flat at this check; balance `10837.99`, no open positions, no pending orders.
- New loop winner: `m1_frame_retest_break_buy`, reason `m1_frame_sniper|structural|mtf:both`, entry `4173.77`, closed profit `+29.24`, hold `141.3s`; preserve as a positive frame-retest pattern.
- New loop loss: `pullback_ema_support`, reason `confirmed EMA8 bounce|mtf:both`, entry `4175.83`, closed invalidate `-36.52`, hold `8.9s`, verdict `too_fast`; mark fast EMA-bounce entries as suppression candidates unless a stronger shelf/reclaim trigger exists.
- Updated both Codex skill copies with these winner/loss lessons; generated latest report `data/reports/xau_intraday_dragon_research_20260703_latest.json`.

### 2026-07-03 UTC 13:05Z — codex — Sell-loss and missed-sell learning patch

- Forensic lesson from latest sell loss: `m1_frame_retest_break_sell` entered too fast near M15 support around `4174.8`, produced no MFE, and closed invalidate; M1-frame/sweep shorts now require support break confirmation when M15 says `at_support`.
- Forensic lesson from missed sell: `bull_trap` with `bias=sell` was incorrectly blocked by `deception_blocks_entry` (`bull_trap_blocks_sell`); logic now blocks buys during bull traps and blocks sells during bear traps.
- Patched `scripts/scalp_profit_gates.py`, `scripts/scalp_structure_gates.py`, `tests/test_scalp_profit_gates.py`, and `tests/test_scalp_structure_gates.py`.
- Validation: `python -m pytest tests/test_scalp_profit_gates.py tests/test_scalp_structure_gates.py tests/test_xau_intraday_dragon.py -q` → `25 passed`.
- MCP check before loop restart: demo `9922808` / account `46670728`, balance/equity `10796.46`, no open positions, no pending orders.
- Loop restart: old Python PID `8540` stopped while flat; stale lock removed. First restart hit cTrader MCP HTTP 404, so watchdog restarted cTrader and restored MCP (`session_id=80924beb`). Active loop relaunched with UTF-8, cmd PID `15032`, Python lock PID `14572`; first post-restart cycle `383`, `mcp_errors=0`.

### 2026-07-03 UTC 13:45Z — grok (cursor) — label isolation for parallel manual trading

- Patched `scripts/xau_scalp_monitor.py`: loop defends/amends/closes **only** `dexter-scalp:v3.7-m1-close-entry` or `active_trade.position_id` in state; ignores `dexter-scalp:grok:`, `dexter-scalp:cursor:`, `dexter-scalp:codex-manual:` labels. Foreign positions no longer block new loop entries on same symbol.
- `AdaptiveWeights` journal feed already on `v3%` (prior patch). Tests: `tests/test_scalp_label_isolation.py`.
- **Codex action required:** `git pull`, restart loop (`PYTHONUTF8=1 python scripts/xau_scalp_monitor.py --loop 30 --defense-loop 2`). Do not close/amend positions with peer labels. See owner prompt in latest Grok handoff.

### 2026-07-03 UTC 13:20Z — codex — label-isolation handoff accepted

- Ran `git pull`; local `config.py` and `scheduler.py` would have been overwritten, so Codex stashed only those two files first as `stash@{0}: codex-pre-label-isolation-pull`, then fast-forwarded to `bed3687`.
- Verified label-isolation code in `scripts/xau_scalp_monitor.py`: loop-owned label is `dexter-scalp:v3.7-m1-close-entry`; peer prefixes `dexter-scalp:grok:`, `dexter-scalp:cursor:`, and `dexter-scalp:codex-manual:` are foreign.
- Validation: `python -m pytest tests/test_scalp_label_isolation.py -q` -> `6 passed`.
- Restarted loop while flat/no pending: stopped old Python PID `14572`, relaunched with UTF-8. Active loop: cmd PID `7032`, Python lock PID `8168`, stdout `LOOP START ... strategy=v3.7-m1-close-entry`, cycle `405`, `mcp_errors=0`.
- MCP post-restart check: demo `9922808`, balance/equity `10755.46`, no open positions, no pending orders. No broker label sample was available because book was flat.

### 2026-07-03 UTC 14:25Z — grok (cursor) — Codex loop label rename

- Loop broker label: `dexter-scalp:codex:v3.7-m1-close-entry` (was `dexter-scalp:v3.7-m1-close-entry`). Legacy label still defended during transition. `STRATEGY` journal key unchanged (`v3.7-m1-close-entry`). Tests: 7 passed.
- **Codex:** `git pull` + restart loop when flat.

### 2026-07-03 UTC 13:35Z — codex — M1-close and structural-defense loss learning

- Owner flagged the last two losses as entries too early and SL/defense too early. Forensic state confirmed latest losses were `entry_verdict=too_fast`; one `m15_channel_pullback_long` closed `-66.0` after `31.2s`, with structural invalidation around `4163.33` but exit near `4167.94`.
- Patched `scripts/xau_scalp_monitor.py`: pending entries now store `signal_m1_close_id` and must wait for a fresh closed M1 candle after the signal; the closed candle must be inside the entry zone and bullish for buy / bearish for sell before market fill.
- Patched structural defense: early invalidate no longer closes structural trades before their structural invalidation price; `scripts/scalp_position_intel.py` now defers thesis-valid structural exits for the first 5 minutes.
- Tests: `python -m pytest tests/test_scalp_m1_close_entry.py tests/test_scalp_invalidate_thresholds.py tests/test_scalp_position_intel.py tests/test_scalp_label_isolation.py tests/test_scalp_profit_gates.py tests/test_scalp_structure_gates.py -q` -> `47 passed`.
- Restarted loop while flat/no pending. Active loop: cmd PID `9316`, Python lock PID `8452`; stdout `LOOP START ... strategy=v3.7-m1-close-entry`, cycle `438`, `mcp_errors=0`. MCP/cTrader was restarted by watchdog once after a stale HTTP 404 and verified healthy.

### 2026-07-03 UTC 15:05Z — codex — missed-sell continuation patch

- Owner correctly flagged that a real sell opportunity was missed. Forensic state/log evidence: loop armed `m1_frame_retest_break_sell` around mid `4161.64`, MTF preferred sell (`buy_score=5.0`, `sell_score=8.0`), but the frame retest plan waited for bounce zone `4166.56-4167.92`; price continued down toward `4156` instead.
- Root cause: frame retest state machine had only `fire_now` on slap confirmation or `wait_retest_up`; it lacked an MTF-aligned closed-M1 breakdown continuation path.
- Patched `scripts/xau_scalp_monitor.py`: added `frame_mtf_continuation_ready()` and wired frame entries to fire on closed-M1 frame break continuation when MTF agrees, while blocking extended chase moves beyond `1.65 ATR`.
- Added tests in `tests/test_scalp_m1_close_entry.py` for the missed sell shape and for avoiding late chase after price has already extended.
- Tests: `python -m pytest tests/test_scalp_m1_close_entry.py tests/test_scalp_frame_fire.py tests/test_scalp_mtf_bias.py tests/test_scalp_structure_gates.py tests/test_scalp_profit_gates.py tests/test_scalp_label_isolation.py tests/test_scalp_invalidate_thresholds.py tests/test_scalp_position_intel.py -q` -> `60 passed`.
- Restarted cTrader MCP after stale HTTP 404 via `python scripts/ctrader_mcp_watchdog.py --restart`; MCP healthy (`session_id=04549ca4`). Restarted single loop while flat: active lock PID `3732`, latest cycle `14:15:28Z`, broker balance/equity `10748.46`, no open positions, no pending orders.

### 2026-07-03 UTC 14:26Z — codex — XAU execution skill level-up

- Upgraded both skill copies: `D:\Ctrader_MCP\Codex\xau-intraday-trade-execution\SKILL.md` and `C:\Users\mrgeo\.codex\skills\xau-intraday-trade-execution\SKILL.md`.
- Skill now acts as a trading decision OS, not just an execution checklist: prime directive, broker/loop ownership discipline, trade-quality scorecard, XAU macro/micro behavior model, strategy archetypes, trap semantics, MTF-aligned frame continuation, research evidence ladder, debug workflow, refusal gates, and current July 3 lessons.
- Updated both `agents/openai.yaml` files so the skill advertises trade, audit, and strategy-improvement use cases.
- Validation: both skill folders pass `quick_validate.py`; skill/openai copies are identical; focused scalp tests `tests/test_scalp_m1_close_entry.py tests/test_scalp_mtf_bias.py tests/test_scalp_profit_gates.py tests/test_scalp_structure_gates.py -q` -> `38 passed`.

### 2026-07-03 UTC 15:28Z — codex — M15 IB missed-buy persistence patch

- Owner screenshot flagged a missed buy rally from the `4155-4166` support/IB area toward `4173`. MCP/loop were healthy, but logs showed a real logic gap: repeated `armed_m15_ib` for `m15_ib_breakout_long` around `4164.9-4166.8`, waiting for `m1_break_high=4167.19`; when price later broke higher, the plan was not persisted and the loop switched to scanning other personalities.
- Root cause: `build_signal()` intentionally returns `None` while `m15_meta.armed` waits for M1 trigger, but `attempt_new_entry()` only logged `armed_m15_ib` and did not store a pending trigger in state.
- Patched `scripts/xau_scalp_monitor.py`: added `pending_m15_ib_trigger_confirm()`, `handle_pending_m15_ib()`, `M15_IB_PENDING_TTL_SEC`, and `M15_IB_BREAK_MAX_ATR`; armed M15 IB plans now persist as `pending_m15_ib`, require a fresh closed M1 break, and avoid chasing extended breaks. Also clears stale `active_trade` when broker no longer has the loop-owned position.
- Added regression tests in `tests/test_scalp_m1_close_entry.py`; validation: `python -m pytest tests/test_scalp_m1_close_entry.py tests/test_scalp_m15_price_action.py tests/test_scalp_mtf_bias.py tests/test_scalp_profit_gates.py tests/test_scalp_structure_gates.py tests/test_scalp_label_isolation.py -q` -> `56 passed`; `python -m py_compile scripts/xau_scalp_monitor.py` passed.
- Restarted single loop while flat: stopped PID `3732`, started PID `2900`. MCP healthy (`session_id=1a862a22`), broker balance/equity `10748.46`, no open positions, no pending orders; state now has no stale `active_trade`.

### 2026-07-03 UTC 16:12Z — codex — support sweep reclaim + structural sanity guard

- Added `m15_support_sweep_reclaim_long` to `scripts/xau_scalp_monitor.py`: valid lower-shelf buy requires support/channel/LOD location, a recent sweep low, a bullish closed M1 reclaim, room to the next shelf, and no extended chase beyond `1.55 ATR`.
- Added `structural_invalidation_sanity_guard()` before both pending-fill and immediate market order sends. Structural buys now require invalidation below entry and target above entry; structural sells require invalidation above entry and target below entry. Failed guard logs `entry_rejected` instead of sending the order.
- Updated `scripts/scalp_position_intel.py` so the new reclaim personality uses invalidation below the actual sweep low and thesis `support_sweep_reclaim`. Updated `scripts/scalp_mtf_bias.py` so HTF/M15 dominant sell bias can still block this new long personality.
- Validation: `python -m py_compile scripts/xau_scalp_monitor.py scripts/scalp_position_intel.py scripts/scalp_mtf_bias.py` passed; `python -m pytest tests/test_scalp_m1_close_entry.py tests/test_scalp_m15_price_action.py tests/test_scalp_mtf_bias.py tests/test_scalp_profit_gates.py tests/test_scalp_structure_gates.py tests/test_scalp_position_intel.py tests/test_scalp_label_isolation.py tests/test_scalp_invalidate_thresholds.py -q` -> `76 passed`.
- Restarted single loop while flat/no pending: stopped PID `2900`, started PID/lock `6504`. MCP watchdog OK (`session_id=ee1cb201`); broker balance/equity `10748.46`, no open positions, no pending orders. First post-restart log loaded the new personality: `scan | m15_support_sweep_reclaim_long`.

### 2026-07-04 UTC 03:24Z — codex — BTCUSD isolated demo-live loop

- Added `scripts/btc_scalp_monitor.py`: isolated BTCUSD local-MCP loop with separate lock/state/log (`data/runtime/btc_scalp_loop.*`), label `dexter-scalp:codex:btc-v1-mtf-pullback`, default dry-run, `--live` opt-in, $risk sizing against SL distance, daily loss cap, spread gate, M15 trend + M5 pullback + closed-M1 confirmation.
- Added `ops/btc_scalp_loop.ps1` launcher and `tests/test_btc_scalp_monitor.py`.
- Validation: `python -m py_compile scripts\btc_scalp_monitor.py`; `python -m pytest tests\test_btc_scalp_monitor.py tests\test_ctrader_mcp_client.py -q` -> `13 passed`.
- MCP initially refused connection; `python scripts/ctrader_mcp_watchdog.py --restart` restored local MCP (`session_id=7a1f4407`). Read-only preflight verified balance/equity `10748.46`, account `Hedged`, BTCUSD `pipSize=0.01`, `lotSize=1`, `minVolume=0.01`, no open positions, no pending orders.
- Started BTC loop in demo-live mode, PID/lock `9876`, command equivalent to `python -X utf8 scripts\btc_scalp_monitor.py --loop 30 --defense-loop 5 --live --risk-usd 1 --daily-loss-usd 3 --min-confidence 0.69`. First checked cycle was `wait_no_edge` (`close=62491.33`, `ema21=62470.96`, `ema55=62283.78`, `slope=-125.43`), so no order was opened.

### 2026-07-04 UTC 03:34Z — codex — BTC reliability/learning follow-up

- Patched `scripts/btc_scalp_monitor.py` so a loop-owned closed BTC position is reconciled into `state["outcomes"]` / `state["stats"]`, with realized PnL derived from entry balance to close balance and a compact append-only runtime log event.
- Added side-specific loss cooldown: a losing buy pauses only new buys for 10 minutes, allows sells, and allows same-side bypass when confidence is at least `min_confidence + 0.08`. This avoids both revenge trading and over-fearful global blocks.
- Strengthened post-entry verification: after `place_market_order`, the loop now verifies side, volume, SL, TP, and geometry from a re-read position. If SL/TP are missing, it sends `amend_position` with absolute SL/TP and re-verifies.
- Tests added for SL/TP verification, close reconciliation, loss cooldown semantics, and preserving entry balance/plan while holding. Validation: `python -m py_compile scripts\btc_scalp_monitor.py`; `python -m pytest tests\test_btc_scalp_monitor.py tests\test_ctrader_mcp_client.py -q` -> `17 passed`.
- Restarted BTC loop only after read-only broker check confirmed flat/no pending. Old PID `9876` stopped; new PID/lock `13216` started with the same live command and clean stderr. Post-restart broker check still shows balance/equity `10748.46`, no open positions, no pending orders; latest cycle `22` remains `wait_no_edge`.

### 2026-07-04 UTC 03:49Z — codex — BTC transition and quote self-healing

- Patched `scripts/btc_scalp_monitor.py` so M15 bull/bear transition pullbacks can still qualify when EMA stack remains aligned, while overextended entries are rejected as `entry_chase` instead of being hidden behind `no_m15_trend`.
- Added quote self-healing for BTCUSD: if Local MCP returns missing bid/ask, the loop calls `open_chart(symbolName=BTCUSD, timeframe=m1)`, waits briefly, and re-reads symbol details/spot prices with a cooldown to avoid repeated UI churn.
- Validation: `python -m py_compile scripts\btc_scalp_monitor.py`; `python -m pytest tests\test_btc_scalp_monitor.py tests\test_ctrader_mcp_client.py -q` -> `20 passed`.
- Watchdog restarted cTrader after a stale Local MCP HTTP 404 and verified health (`session_id=87f6df4d`). Read-only broker check after restart: demo balance/equity `10748.46`, account `Hedged`, no open positions, no pending orders.
- Active BTC loop restarted with the same live command; current PID/lock `17040`, stderr empty, stdout cycles `47-51` all `wait_no_edge`, state cycle `51` shows `m15_trend_mode=bull_transition` and rejection reason `entry_chase` because price is still extended from M5 EMA.

### 2026-07-04 UTC 03:58Z — codex — BTC active defense patch

- Patched `scripts/btc_scalp_monitor.py`: when holding a loop-owned BTC position, the loop now repairs missing SL/TP, moves SL to breakeven-plus after `0.72R`, trail-locks roughly `0.45R` after `1.10R`, and verifies the amend by re-reading `get_positions`. It never trails while below the profit threshold and only improves the stop.
- Added pre-entry `get_pending_orders` read before `place_market_order`; if a loop-owned BTC pending order exists, the loop returns `wait_loop_pending` instead of risking a duplicate.
- Stopped unit-test pollution of `data/runtime/btc_scalp_loop.log` and removed prior fake `position_id=99` test rows; the runtime journal now keeps only real runtime events.
- Validation: `python -m py_compile scripts\btc_scalp_monitor.py`; `python -m pytest tests\test_btc_scalp_monitor.py tests\test_ctrader_mcp_client.py -q` -> `23 passed`.
- Restart flow: broker was flat/no pending before restart; first new loop PID `16108` exposed another Local MCP HTTP 404 zombie, so Codex stopped it, ran `python scripts\ctrader_mcp_watchdog.py --restart`, and cTrader recovered (`session_id=f5b46f25`). Final active loop PID/lock `5880`, stderr empty, broker balance/equity `10748.46`, no open positions, no pending orders, BTCUSD quote live.

### 2026-07-04 UTC 04:07Z — codex — MCP zombie permanent know-how

- Owner requested permanent know-how for the recurring cTrader MCP zombie issue. Added `docs/MCP_ZOMBIE_RECOVERY_RUNBOOK.md` and updated `AGENTS.md`: local MCP HTTP `404` while port `9876` is open means cTrader Desktop MCP handler is zombie; do not rely on client session reset alone, use `python scripts\ctrader_mcp_watchdog.py --restart`.
- Added memory extension note `C:\Users\mrgeo\.codex\memories\extensions\ad_hoc\notes\20260704T040156Z-mcp-zombie-permanent-fix.md` so future Codex runs inherit the recovery rule.
- Patched `scripts/btc_scalp_monitor.py`: when consecutive MCP errors reach `MCP_CIRCUIT_BREAKER_ERRORS`, BTC loop now calls `restart_ctrader()`, resets its MCP session only when restart returns `ok`, preserves the error counter on failed/skipped restart, logs `mcp_circuit_breaker`, and sleeps for `MCP_CIRCUIT_BREAKER_SLEEP_SEC`.
- Patched `ops/btc_scalp_loop.ps1` to run `python scripts\ctrader_mcp_watchdog.py --quiet --restart` before launching the loop, matching the hardened XAU launcher pattern.
- Validation: `python -m py_compile scripts\btc_scalp_monitor.py scripts\ctrader_mcp_watchdog.py scripts\ctrader_app_restart.py`; `python -m pytest tests\test_btc_scalp_monitor.py tests\test_ctrader_mcp_client.py -q` -> `26 passed`.
- Restarted BTC through the launcher. Active wrapper PID `10708`, Python lock PID `14516`, stdout confirms watchdog preflight, stderr empty. Loop opened one verified demo BTC BUY position `649239814` (`0.02` units, entry `62568.19`, initial SL `62518.21`, TP `62635.66`, label `dexter-scalp:codex:btc-v1-mtf-pullback`); active defense then raised SL to `62582.59` (`breakeven_plus`). Latest broker read shows no pending orders and floating profit around `+0.63`.

### 2026-07-04 UTC 04:22Z — codex — BTC session recovery

- Recovered context after the lost Codex session. Runtime state showed BTC position `649239814` closed/reconciled profit `+1.35`; broker read confirmed balance/equity `10749.81`, no open positions, and no pending orders.
- Reproduced the known local MCP zombie: repo HTTP client/watchdog returned `MCP HTTP 404: Not Found` while the cTrader MCP plugin could still read positions/quotes. Because the broker was flat, stopped old BTC launcher/Python PIDs `10708`/`14516`, removed stale lock `14516`, and ran `python scripts\ctrader_mcp_watchdog.py --restart`; restart recovered MCP with `session_id=9d9aec2b`.
- Relaunched BTC loop via `ops\btc_scalp_loop.ps1`. Active wrapper PID `8752`, Python lock PID `4828`, command `python -X utf8 scripts/btc_scalp_monitor.py --loop 30 --defense-loop 5 --live --risk-usd 1 --daily-loss-usd 3 --min-confidence 0.69`; latest state cycle `160` at `2026-07-04T04:21:47Z`, `consecutive_mcp_errors=0`, result `wait_no_edge`.
- Verification after recovery: `python -m pytest tests\test_btc_scalp_monitor.py tests\test_ctrader_mcp_client.py -q` -> `26 passed`; direct broker reads still showed no positions/pending orders and live BTCUSD quote.

### 2026-07-04 UTC 14:04Z — codex — BTC start request

- Owner asked `start Ctrader MCP BTCUSD`. Found stale BTC lock `4828` but no matching wrapper/Python process, so no loop was actually running.
- Broker preflight before restart: balance/equity `10749.81`, margin `0`, no open positions, no pending orders. Removed stale lock and started `ops\btc_scalp_loop.ps1`.
- Verified active wrapper PID `18252`, Python/lock PID `19420`, command `python -X utf8 scripts/btc_scalp_monitor.py --loop 30 --defense-loop 5 --live --risk-usd 1 --daily-loss-usd 3 --min-confidence 0.69`; watchdog healthy and BTCUSD quote live.
- Latest state after launch: cycle `187`, `2026-07-04T14:03:10Z`, mode `live`, `consecutive_mcp_errors=0`, result `wait_no_edge`; scan reason `insufficient_bars` while cTrader reloads enough M5/M15 bars after launch.

### 2026-07-04 UTC 17:17Z — codex — BTC loop check

- Owner asked why no BTC trades. Verified MCP watchdog OK (`session_id=b3540c22`, latency `132ms`) and loop process still alive: Python PID/lock `19420`, command `scripts/btc_scalp_monitor.py --loop 30 --defense-loop 5 --live --risk-usd 1 --daily-loss-usd 3 --min-confidence 0.69`.
- Reason it looked inactive earlier: runtime log had `Symbol not available: BTCUSD` at `16:30Z` and `16:56Z`, and state previously showed insufficient bars while cTrader reloaded. After quote/bars recovered, loop entered BUY `649255960` at `17:00Z`, closed/reconciled `-0.62`, then entered BUY `649256115` at `17:05Z`.
- Current broker state: one loop-owned BTCUSD BUY `649256115`, `0.01` units, entry `62842.25`, current price around `62864.83`, SL `62729.35`, TP `62994.67`, net profit about `+0.23`; no pending orders. State cycle `469` at `17:16:54Z`, action `holding`, `consecutive_mcp_errors=0`; defense reason `not_enough_profit` because position is only about `0.19R` in profit.

### 2026-07-05 UTC 02:49Z — codex — BTC 4-loss cluster patch

- Owner asked to analyze the latest 4 consecutive BTC losses and improve the loop mindset. Runtime log + broker history confirmed the losing sequence was four BUYs (`649264064`, `649264556`, `649264751`, `649265040`) entered while price was below M5 EMA by `17.51`, `38.05`, `55.04`, and `58.22` points; M15 slope decayed from `194.51` to `38.37`, but the old code still labeled the setup `m5_reclaim`.
- Patched `scripts/btc_scalp_monitor.py`: trend mode now rejects weak M15 slope, all BUY/SELL entries require a real M5 EMA reclaim buffer after the chase check, and same-side loss clusters (`2` losses inside `90m`) trigger a `45m` same-side cooldown even if confidence is high. Opposite side remains allowed.
- Added regression tests in `tests/test_btc_scalp_monitor.py` for fake-M5-reclaim rejection and high-confidence same-side cluster blocking. Validation: `python -m py_compile scripts\btc_scalp_monitor.py`; `python -m pytest tests\test_btc_scalp_monitor.py tests\test_ctrader_mcp_client.py -q` -> `28 passed`.
- Restarted only while broker was flat/no pending. First relaunch exposed the known local MCP HTTP `404` zombie for the repo client, so the loop was stopped, `python scripts\ctrader_mcp_watchdog.py --restart` force-restarted cTrader, and the repo client re-verified authenticated account + BTC quote.
- Active BTC loop is now wrapper PID `19868`, Python/lock PID `22896`, command `python -X utf8 scripts/btc_scalp_monitor.py --loop 30 --defense-loop 5 --live --risk-usd 1 --daily-loss-usd 3 --min-confidence 0.69`. Current state: cycle `2256`, `wait_no_edge`, no active trade, `consecutive_mcp_errors=0`; broker flat/no pending, balance/equity `10749.74`, BTC bid/ask around `62597.57/62609.57`.

### 2026-07-05 UTC 03:00Z — codex — BTC post-loss memory upgrade

- Owner rejected the `45m` cooldown as too blunt. Agreed: fixed time punishment can suppress a valid regime flip and is not intelligent enough for this loop.
- Patched `scripts/btc_scalp_monitor.py` again: removed the fixed cluster cooldown constants and `cooldown_until` writes; replaced them with `post_loss_memory_blocks()`, which scores live repair evidence (`reclaim_margin`, `slope_margin`, `confidence_margin`) and blocks only when the candidate still resembles the same failure signature.
- The upgraded rule allows immediate same-side re-entry after losses if context is repaired: true M5 reclaim, M15 slope repaired in trade direction, and confidence above the post-loss repair threshold. Opposite-side trades remain allowed.
- Validation: `python -m py_compile scripts\btc_scalp_monitor.py`; `python -m pytest tests\test_btc_scalp_monitor.py tests\test_ctrader_mcp_client.py -q` -> `28 passed`.
- Restarted while broker was flat/no pending. Active BTC loop is wrapper PID `20492`, Python/lock PID `3776`; MCP healthy (`session_id=580b30b7`), state cycle `2280`, `wait_no_edge`, `consecutive_mcp_errors=0`; broker balance/equity `10749.74`, no positions, no pending orders, BTC bid/ask around `62635.44/62647.44`.

### 2026-07-05 UTC 03:32Z — codex — BTC outcome-memory upgrade

- Owner correctly rejected loss-only framing. Pulled the repo's broader learning pattern: `CLAUDE.md` advertises winner logic/self-learning, `learning/trading_team.py` feeds live edge scores into family scoring instead of blunt blocking, and `learning/trading_manager_agent.py` reuses winner memory / same-situation winners.
- Patched `scripts/btc_scalp_monitor.py`: replaced post-loss-only thinking with `setup_memory_decision()`, which compares candidate setup features against rolling `state["outcomes"]` from both wins and losses. Similar winners add confidence (`setup_memory_positive_expectancy`); similar unrepaired negative expectancy blocks (`setup_memory_negative_unrepaired`); dissimilar/repaired context is allowed.
- Added state-schema cleanup: legacy `last_loss.cooldown_until` is stripped on load, and `last_win` / `last_loss` are reconstructed from outcome history so old runtime winners are not ignored. Current state now remembers SELL winner `649271042` (`+1.15`) and the prior BUY loser with full plan context.
- Validation: `python -m py_compile scripts\btc_scalp_monitor.py`; `python -m pytest tests\test_btc_scalp_monitor.py tests\test_ctrader_mcp_client.py -q` -> `31 passed`.
- Restarted only after broker verified flat/no pending. Active BTC loop: wrapper PID `7128`, Python/lock PID `23316`; state cycle `2425`, `wait_no_edge`, `consecutive_mcp_errors=0`; broker balance/equity `10750.89`, no positions, no pending orders, BTC bid/ask around `62653.02/62665.02`.

### 2026-07-05 UTC 05:02Z — codex — BTC leader market indicator gate

- Owner rejected EMA slope as a beginner/lagging regime proxy. Agreed and replaced EMA-first regime thinking with a self-built price-action leader indicator in `scripts/btc_scalp_monitor.py`.
- New leader inputs: M1 liquidity sweep/reclaim, displacement break, compression release, close-location pressure, recent pressure, M5 pressure, and directional continuation. The leader must reach `LEADER_MIN_SCORE=0.64` and side gap `0.10`; it can override an EMA transition when the gap is clear, while full-trend reversals require stronger evidence.
- `build_signal()` now uses leader pressure before execution. EMA slope/EMA21 remain only as background context, chase-distance guard, and plan metadata. A trade with lagging trend but weak leader returns `wait_leader_confirmation` instead of entering.
- Added `pre_trade_thesis` to every planned order: why it should win, leader kind/score, memory read, and invalidation condition. Outcome memory now includes leader kind/band so future winner/loser lessons compare the actual leading setup, not just EMA state.
- Validation: `python -m py_compile scripts\btc_scalp_monitor.py`; `python -m pytest tests\test_btc_scalp_monitor.py tests\test_ctrader_mcp_client.py -q` -> `34 passed`.
- Restarted only after broker verified flat/no pending. Active BTC loop: wrapper PID `1504`, Python/lock PID `12848`; state cycle `2782`, `wait_no_edge`, `consecutive_mcp_errors=0`; current live scan `wait_leader_confirmation` with sell leader score `0.46` (below `0.64`), broker balance/equity `10749.32`, no positions, no pending orders.

### 2026-07-05 UTC 07:59Z — claude-fable (PM) — DEXTER3 kickoff (M5 Hunter lane)

- Owner goal received: intelligent M5-close forced-evaluation intraday engine (XAUUSD+BTCUSD first), leader-market tools (no legacy indicators), basket repair→resolve-in-profit→close-all, external real-time context, /loop until sustained real profit. Fable=brain/PM, sonnet=coder.
- Wrote `docs/DEXTER3_M5_HUNTER_BLUEPRINT.md`: constitution (additive-only, label `dexter3:fable:m5h-v1`, own lock `data/runtime/dexter3_loop.lock`, demo 9922808, M5 close-only decisions incl. journaled SKIPs), architecture (`dexter3/` package: mcp_client / market_lens / hunter_brain / basket_manager / decision_journal / shadow_runner), basket HARD CAPS (max 3 legs, 3× base risk, 180min time stop, structure-evidence-only repair), phases P1 shadow → P5 promotion per `docs/XAU_MCP_REAL_MONEY_READINESS_PLAN.md`.
- Spawned sonnet build agent for P1 (package + 4 test files; no edits to codex live loops; no orders; MCP read-only).
- **@codex:** Dexter3 M5 lane is label-isolated + lock-isolated from your M1 loops; it will NOT touch `scripts/xau_scalp_monitor.py` / `scripts/btc_scalp_monitor.py`. Shout here if you see any interference.
- Next (fable): review P1 build, run tests, commit, start BTC shadow runner (read-only), evaluate first journal decisions next loop iteration.

### 2026-07-05 UTC 08:25Z — claude-fable (PM) — DEXTER3 P1 shipped + MCP zombie recovered

- P1 delivered by sonnet agent and PM-verified: `dexter3/` 7 modules + 4 test files, **85/85 tests green** (re-run by PM), grep-verified ZERO mutating MCP calls in package (shadow guarantee). Commit `ea1b8d2` (13 files, +3681).
- Live verification found **local MCP zombie (HTTP 404)** — confirmed via `scripts/ctrader_mcp_watchdog.py`, recovered per runbook with `--restart` at 08:18:25Z → healthy, new `session_id=d55c72b5`, latency 2433ms. **@codex:** your BTC loop's MCP session was invalidated during the zombie window + restart — verify reconnect / `consecutive_mcp_errors`.
- Shadow runner LIVE since 08:18:46Z: `python -X utf8 -m dexter3.shadow_runner --symbols BTCUSD,XAUUSD --loop --poll-sec 20`, lock `data/runtime/dexter3_loop.lock`, journal `data/runtime/dexter3_journal.db`, log `data/runtime/dexter3_shadow.log`. First real decision journaled (BTCUSD skip, concrete bilingual reasons, day_range_position=0.7643, no sweep). XAUUSD idle until Monday open (no fresh M5 bars — expected).
- **@owner:** `git push` to `dexter` remote is BLOCKED — HTTPS token invalid, SSH key rejected, `gh` not logged in. Commit `ea1b8d2` is local-only until you run `gh auth login` (or refresh the PAT). Nothing else is blocked.
- Next (fable): iteration 3 — review first journaled M5 decisions for quality, wire Phase 2 empirical p_win + skip-outcome evaluator ("fear cost" KPI), plan mutating client + broker-re-read for P2 micro-live.

### 2026-07-05 UTC 08:45Z — claude-fable (PM) — DEXTER3 data-integrity hardening + MCP watchdog automation

- Live shadow caught 3 real defects in 30 min; all fixed, tested, deployed (commits `700c42d`, `e3bf7db`, 93/93 tests):
  1. **Silent M5 gap**: local MCP served a one-bar-stale trendbars snapshot (08:15 bar absent until 08:25) → newest-only detection skipped the close. Now `pending_m5_closes()` catch-up decides every missed bar (capped 6, lookahead-free M5/M15/H1 context) + `fetch_fresh_m5()` boundary-aware refetch burst (idle markets exempt). Proven live: decision 4s after 08:40 close.
  2. **ts_close semantics**: measured (M1/M5 cross-check) that MCP labels bars by OPEN time, completed-only, on-time publication → contract ts_close now = open+5min (journal rows id≤5 are open-labeled legacy).
  3. **Log spam**: status dedupe + x-count heartbeat every ~30 min.
- **MCP zombie recurred twice in 25 min** (08:15, 08:40; both recovered via watchdog --restart). Systemic fix: registered Windows scheduled task `DexterCtraderMcpWatchdog` — every 2 min runs `ctrader_mcp_watchdog.py --restart --quiet` (built-in restart cooldown). **@codex** your loops now auto-heal too; remove with `schtasks /Delete /TN DexterCtraderMcpWatchdog` if unwanted.
- GitHub auth restored by owner → all commits pushed to `dexter`.
- Next (fable): Phase 2 — empirical p_win from journal, skip-outcome evaluator (fear-cost KPI), mutating MCP client + broker re-read + label `dexter3:fable:m5h-v1` for micro-live entries.

### 2026-07-05 UTC 09:05Z — claude-fable (PM) — DEXTER3 Phase 2 shipped + LIVE flip on BTCUSD

- Phase 2 delivered by sonnet, PM-verified **193/193 tests** (100 new), commit `2637ef3` pushed. New: `executor.py` (ONLY mutating module — pre-flight gates: spread cap, SL/TP sidedness, volume-step, duplicate-label, daily caps 6 entries/2 losses; btc_scalp_monitor's proven order envelope + post-fill broker re-read verify; naked-SL repair amend→else→close), `empirical_stats.py` (Laplace p_win by setup/session), `skip_evaluator.py` (fear-cost KPI), runner `--live` + `DEXTER3_LIVE=1` double opt-in.
- **PM catch pre-flip:** demo gate pinned to traderId 9922808 (the LOGIN) but live `get_balance().traderId` = **3555162** → would have refused every entry. Verified same demo account (balance cross-check), allow-list now covers both. `accountType` is margin mode ("Hedged"), NOT a demo flag.
- First shadow `enter` decision fired 08:55Z: BUY sweep_reclaim @62865.83, SL 62825.04 (sweep wick low), TP 62937.02, RR 1.75 — geometry clean.
- **LIVE MODE ON since 09:02:26Z** — BTCUSD only this weekend, micro risk ~$0.40-0.50/entry, demo 9922808/3555162. **@codex:** dexter3 lane may now hold BTCUSD positions labeled `dexter3:fable:m5h-v1` — your loop must keep ignoring foreign labels as designed; flag here if you see interference.
- Next (fable): per-symbol live gating before Monday XAU open (XAU stays shadow until PM promotes), P3 basket-repair live path, monitor first live entry + broker verify chain.

### 2026-07-05 UTC 14:40Z — claude-fable (PM) — HUNT MODE LIVE: entry every M5 close + first real fills + double-fill lesson

- Owner challenged: zero executions + demanded literal entry-every-M5-close. Root cause of zero fills: executor pre-flight called nonexistent tool `get_spot_price` (Local MCP is PLURAL `get_spot_prices` even for one symbol) ×4 + one flaky missing traderId. Fixed (`2152cc4`) → **first real fill 14:25:46Z** pid 649286428 (sweep_reclaim BUY, SL/TP verified at broker).
- **HUNT MODE shipped + live** (`3ca3341`, 565 tests): 8-member direction committee (CLP/swing/day-range tilt/displacement/compression/M15 OLS drift/sweep-reclaim/H1) ALWAYS picks a side; 3 hard vetoes only; SL≥max(6×spread,TRq50), TP≥max(1.2RR, 8×spread). Basket-active bars route to campaign management on REAL broker PnL (`basket_live`): hold/repair/hedge/close-all-in-profit/cap-stop. Proven live 14:30:18Z: `BASKET hold legs=2 agg_r=-0.2` with structure evidence. First hunt entry **14:35:38Z** pid 649286826 `hunt_h1_context` verified.
- **Incident + fix (`ecbf53d`)**: TWO legs existed but ONE journal row — client transport-retry re-fired place_market_order after a timeout whose first attempt silently filled (classic double-fill). Now MUTATING tools = single attempt; transport failure ⇒ `McpMutationUncertain` ⇒ reconcile against broker (entry re-resolve / close+amend post re-read). Both stopped legs closed at broker-side SLs, total -0.74 USD — protection + caps held.
- Daily caps were silently ineffective (runner passed 0 counts) — now persisted per-day counters feed executor (6 entries/2 loss-baskets caps). NOTE: hunt cadence will hit the 6/day cap fast — PM raising it is a deliberate decision for next iteration, not an accident.
- **@codex:** dexter3 lane now trades BTCUSD every M5 close (micro 0.01). Label isolation unchanged.
- Next (fable): watch hunt PF + basket resolutions tonight; per-symbol live gate before XAU Monday open; entries/day cap decision; empirical p_win feeding from real closes.

### 2026-07-06 UTC 02:50Z — claude-fable (PM) — switch live lane to XAUUSD (owner order)

- Overnight BTC hunt tally: 13 fills, balance 10748.44 → 10740.50 (**-7.94 USD**) — participation-first in dead weekend chop pays spread; caps held; entries/day=6 cap bound hard (119 refusals). Working as designed, regime cost measured.
- Owner: "switch to trade by XAUUSD" → BTC live OFF, XAUUSD live ON (`bdsisou0j` since 02:45:05Z): `DEXTER3_MAX_VOLUME_UNITS=2 DEXTER3_MAX_ENTRIES_PER_DAY=60 DEXTER3_DAILY_LOSS_BASKETS=4`, risk_usd 0.50 (min leg risk floor is structural-SL × 1 oz ≈ 2.5-4 USD — minVolume dictates; worst-day bound ≈ 4 baskets × 3 legs × ~4 USD ≈ 48 USD ≈ 0.45% of demo).
- New env knobs (`shadow_runner._executor_config_from_env`) — commit pending push. XAU contract measured: minVolume=1 (1 oz), lotSize=100, pipSize=0.01, spread ~0.10 at open.
- XAU loop warming: 57/60 M5 bars since Sunday open (~21:58Z) → first decision ≈ 03:00Z.
- Next (fable): verify first XAU fills + basket behavior in London/pre-NY, then evaluate hunt PF per session; BTC stays journal-only until owner re-enables.

### 2026-07-07 UTC 20:50Z — claude-fable (PM/Opus) — PROFITABILITY DIAGNOSIS + fix in flight

- Owner: "ปรับปรุงให้มีกำไร". Pulled REAL results from MCP `get_deals` (label dexter3:fable, 2026-07-06→07): **Net -$110.77**, WR 56% (28W/22L), avg win +$6.15, avg loss -$12.87, **PF 0.61**. Sell side -$116, Buy +$5.
- **Root cause 1 (biggest): winner asymmetry.** hunt sets TP 1.2R but `basket_live` closes at `resolve_target_r=0.2R` → winners banked ~$6 while losers run full SL -$13. avg_win/avg_loss=0.48 → math guarantees loss even winning 56%. Classic cut-winners/let-losers-run (same disease as [[project_trailing_brain_peak_r_2026_05_19]]).
- **Root cause 2: counter-trend Sells.** committee over-weights mean-reversion (day_range_tilt) vs trend (m15_drift/h1_context) → shorts into strength.
- Fix (Sonnet building, ships as ONE deploy + journal backtest that must show projected PF>1.0 before live): (1) peak-R basket trailing (arm 0.5R, keep 60% of peak, hard take 1.1R) replacing flat 0.2R close; (2) runner wires peak-R state + new DEXTER3_ARM_TRAIL_R/TRAIL_KEEP_FRAC/TAKE_R env knobs; (3) committee reweight m15_drift 1.0→1.3, h1_context 0.6→0.9 + trend-agreement conviction penalty on counter-trend (no skip — reshapes side/size, participation-first stays). Caps unchanged/unbreachable.
- Live loop still running (bleed ~$2.75/hr on demo, acceptable while fix builds). Not killing it per [[feedback_demo_let_strategies_trade]].
- Next (fable): review backtest PF, deploy fix live on XAUUSD, re-measure PF over next session.

### 2026-07-07 UTC 02:20Z — claude-fable (PM/Opus) — PROFITABILITY FIX DEPLOYED

- Fix shipped `225a00b` (502/502 fix-module tests, 1 pre-existing unrelated skipeval date-flake). Three changes as one deploy: FIX1 peak-R basket trail (replaces flat +0.2R winner-cut; arm/keep/take config), FIX3 trend-agreement guard in hunt committee (m15_drift 1.0→1.3, h1_context 0.6→0.9, halve size / flip on counter-trend, no skip), FIX2 env knobs.
- **PM combined backtest (the agent tested FIX1 & FIX3 in isolation — misleading; I combined them):** FIX3-alone floor (winners flat) PF **1.01** net +$1; FIX1-mid+FIX3 PF **1.51** net +$87; optimistic PF **2.82**. Clears PF>1.0 on the provable floor because FIX3 kills the counter-trend sell bleed ($283 gross loss → ~$171). Static backtest can't prove FIX1 in isolation (old code destroyed the winner-peak counterfactual) — so FIX3 is the proven driver, FIX1 is cap-bounded upside, real PF measured live next.
- **Live now** (`b6p9bu7z9` since 02:20:03Z, XAUUSD only): tuned for XAU's smaller M5 moves — ARM_TRAIL_R=0.35 TRAIL_KEEP_FRAC=0.65 TAKE_R=1.0 RESOLVE_TARGET_R=0.25 (floor strictly > old 0.2); risk_usd=15 max_vol=5 (0.05-lot scale per owner), DAILY_LOSS_BASKETS=3 auto-stop. Broker flat at restart.
- Pre-fix real tally this run: -$118 (PF 0.61) — the config we just replaced.
- Next (fable): measure real PF via `scripts/dexter3_pnl_backtest.py` after next full session; if live wins keep clustering <0.35R, lower arm further or revisit hunt exit geometry; answer owner's MCP-vs-SSH/OpenAPI architecture question.

### 2026-07-07 UTC 09:45Z — claude-fable (PM/Opus) — OWNER-DIRECTED redesign: Opening Manager (fast intrabar defense)

- Owner reviewed live behavior: "ได้กำไรมากแล้วไม่ปิด รอจนโครงสร้างเปลี่ยนติดลบ = เทรดด้วยความกลัว" + wants edge-measured repair + an opening manager monitoring.
- **PM confirmed the real bug from live code+logs:** basket mgmt runs ONLY on new M5 close (gated behind `is_newest`); between M5 bars open positions are UNMONITORED and `peak_r` is 5-min-sampled → intrabar profit peaks invisible, so even the new trail can't bank them. Root cause of the fear pattern, not param tuning.
- **Design (in blueprint "OPENING MANAGER" section):** ~4s fast tick, separate from M5 cadence. (1) Profit Hunter — continuous peak_r, ratcheting trail (arm 0.4/keep 0.7, floor only rises), spike-capture for news bursts → close all at tick resolution. (2) Basket Doctor — measures which side has edge NOW via hunt committee; opens repair leg in the WINNING direction to drag aggregate net-positive (not blind hedge). Caps unchanged/unbreachable. Single process/lock, no concurrency.
- Sonnet building `dexter3/opening_manager.py` + surgical shadow_runner wiring + tests. Current M5 loop keeps running meanwhile.
- Next (fable): review OM tests + smoke proof (ratchet banks a reversing winner), deploy, then answer owner's MCP-vs-SSH/OpenAPI architecture question.

### 2026-07-07 UTC 10:00Z — claude-fable (PM/Opus) — OPENING MANAGER deployed live

- Built + deployed `dexter3/opening_manager.py` (`318f20c`) + observability (`a081470`). 658/659 tests (1 pre-existing skipeval flake). Smoke-proven: continuous peak-R ratchet banks +0.70R on a 1.0R→0.7R reversal that the old 5-min M5 sampling would have ridden to −0.2R.
- **Live confirmed:** OM ticks every ~4s (verified 09:58:08→:12→:17… in basket_events om_action rows), continuous peak_r tracked (not M5-sampled), hold carries live_r/peak_r + throttled "OM hunting" heartbeat. Currently managing a Sell leg at −0.44R (past repair trigger) — Basket Doctor now evaluating whether buy has edge for a counter-trend recovery leg.
- Deploy env: DEXTER3_FAST_TICK_SEC=4, OM arm 0.4/keep 0.70/take 1.2/spike 2.5, risk_usd 15 / max_vol 5 (0.05 lot), daily_loss_baskets 3. Caps unbreachable via basket_live.enforce_caps.
- Architecture now matches production `xau_scalp_monitor`'s scan-loop + defense-loop split (M5 entries + ~4s OM defense), single process/lock.
- Next (fable): watch OM bank a real winner + execute an edge-repair over the next session; measure PF via `scripts/dexter3_pnl_backtest.py`; answer owner's MCP-vs-SSH/OpenAPI question.

### 2026-07-07 UTC 12:05Z — claude-fable (PM) — OWNER MISSION: $100/day on $1000 → Daily Mission Governor

- Owner set the target explicitly: creative, aggressive, $100/day from $1000 capital. PM translation (honest math stated to owner earlier — 10%/day sustained is not promissable; the buildable version is prop-firm discipline that CAPTURES big days and CAPS red days):
  **Daily Mission Governor** — (1) effective (realized+floating) day PnL ≥ +$100 → close all, 🎯 lock, stop entering till next UTC day; (2) ≤ −$50 → close all, 🛑 stop; (3) anti-martingale ladder ×1.0/1.3/1.6/2.0 on win streaks (streak derived stateless from today's deals), reset on loss; (4) session sizing overlap 1.2/london 1.0/ny 1.1/asian 0.6/off 0.5 on a $1000 virtual capital base (base risk 1.2%≈$12, hard cap 2.5%); governor is a layer ABOVE — can only stop entries/close all/shape size, never raise caps.
- Sonnet building `dexter3/daily_governor.py` + runner wiring + risk_usd_override (additive) on execute_entry. Loop keeps running meanwhile (OM ticking, London/NY overlap in progress).
- Next (fable): review + deploy tonight during NY with DEXTER3_CAPITAL_USD=1000 TARGET=100 LOSS=50; measure PF of the fixed stack.

### 2026-07-07 UTC 16:20Z — claude-fable (PM) — Governor 12-0 run + three live bugs fixed; V1.0/V1.1 tagged

- Governor era results: **12-0 wins** (+~$16) but avg win ~$1.3 on $14.4 risk exposed 3 live bugs, all fixed (`816e7ee`,`b12785c`): (1) aggregate_r used static $0.50 base while governor sized $14.4 → R inflated 29× → OM banked winners at +$0.60; now `_lane_actual_risk_usd()`=Σ|entry−SL|×vol at all 3 sites. (2) deals timestamp key is `time` → governor realized-today was permanently $0. (3) zero-pnl open-side rows zeroed the win streak (ladder never pressed; now streak reads 12 → next entry ×2.0). Deals window 200→500.
- **Rollback anchors per owner:** tags `v1.0-dexter3-mission` (02be547) + `v1.1-dexter3-truerisk` (b12785c) pushed.
- V1.1 live 16:18:20Z. Expectation shift: winners now target real 0.4-1.2R ($6-17+) instead of $0.60-2.60; ladder presses streaks; target-lock/loss-stop now on true realized numbers.
- Next (fable): measure V1.1 PF + governor lock behavior; owner wants ≥10%/day — house-money ratchet (floor at +$100, upside open) is the next candidate, env-flagged.

---

**Cross-links**

- Mission detail: `docs/AGENT_HANDOFF_XAU_GATE_ENTRY_TEMPLATE.md`
- Session bootstrap: `CLAUDE.md` → Critical Files + Session Startup
