# Dexter3 V1.6 + Grok v1.0 Parallel Handoff

Snapshot time: 2026-07-09T05:30:45Z / 2026-07-09 12:30 Bangkok
Owner request: keep both loops running, test independence, and prepare strategy upgrades toward a possible $100/day target.

## Required Reading For Next Agent

Read these before any code, loop, or broker action:

- `AGENTS.md`
- `CLAUDE.md`
- `docs/AGENT_SYNC_BOARD.md`
- `docs/DEXTER3_HANDOFF.md`
- `C:\Users\mrgeo\.agents\skills\ctrader-mcp-servers\SKILL.md`
- `C:\Users\mrgeo\.codex\skills\xau-intraday-trade-execution\SKILL.md`

## Live State At Handoff

MCP health:

- `python scripts\ctrader_mcp_watchdog.py` returned OK.
- Session id: `4a0c8d7d`
- Server UTC: `2026-07-09T05:30:45.1661694Z`

Running loops:

- V1.6 PID: `23676`
- V1.6 command: `python -X utf8 -m dexter3.shadow_runner --symbols XAUUSD --loop --poll-sec 20 --live`
- V1.6 lock: `data/runtime/dexter3_loop.lock`
- Grok v1.0 PID: `11208`
- Grok command: `python -X utf8 -m dexter3.shadow_runner --symbols XAUUSD --loop --poll-sec 20 --live --grok`
- Grok lock: `data/runtime/dexter3_grok_loop.lock`

Broker state:

- Account type: `Hedged`
- Trader id: `3555162`
- Balance: `10605.12`
- Equity: `10607.14`
- Open net PnL: `+2.02`
- Pending orders: none

Open positions:

- Grok v1.0: position `650204326`, XAUUSD SELL, volume `1`, entry `4068.18`, current `4068.14`, net `-0.20`, SL `4075.32`, TP `4056.03`, label `dexter3:grok-v1.0:scalper`
- V1.6: position `650205308`, XAUUSD SELL, volume `1`, entry `4070.60`, current `4068.14`, net `+2.22`, SL `4074.89`, TP `4054.82`, label `dexter3:fable:m5h-v1`

## Current Verified Conclusions

- V1.6 and Grok v1.0 are technically independent by process, lock, runtime state, and broker label.
- Broker label proof has shown no cross-close or cross-amend between labels.
- V1.6 TP/SL absolute-price differences after entry are explained by market-fill slippage plus local MCP pip-distance SL/TP placement, not by Grok.
- Both loops share the same account and local MCP pipe, so they are economically and infrastructure-coupled even when label-isolated.
- Local MCP has shown timeout/refused/zombie behavior under load. If health degrades, recover with `python scripts\ctrader_mcp_watchdog.py --restart`, then re-read positions/orders/balance before any restart.

## V1.6 Profit Audit Summary

V1.6 by Bangkok day from live broker `get_deals`:

- 2026-07-07: `-22.74`, PF `0.89`, 39 closes
- 2026-07-08: `+27.41`, PF `1.16`, 100 closes
- 2026-07-09 so far at last audit: negative before the latest open winners, PF below 1 on the sampled closed set

Best 2026-07-08 contributors:

- `hunt_h1_context`: `+22.73`
- `hunt_swing_structure`: `+15.24`
- `opening_manager_repair`: `+9.53`
- `basket_repair`: `+7.31`

Weak 2026-07-08 contributors:

- `hunt_m15_drift`: `-18.30`
- `hunt_day_range_tilt`: `-9.31`
- weak `hunt_sweep_reclaim`: `-1.83`

Main strategy implication:

- Do not globally 4x size. A naive 4x would have made about `+109.64` on 2026-07-08, but it would also multiply bad days.
- The next upgrade should be selective: only press proven buckets after the day is already green, and scout/downsize weak buckets.

## Requested Next Work

Owner asked if items 1-4 need fixing. Answer: yes, as strategy upgrades, not bug fixes.

Priority implementation plan:

1. Add a daily green threshold before scaling, for example only after realized plus floating PnL is `+20` to `+30`.
2. Scale only proven winner buckets after the threshold: `hunt_h1_context`, `hunt_swing_structure`, and high-quality OM/basket repair.
3. Downsize or block weak buckets, especially `hunt_m15_drift` and weak `hunt_day_range_tilt`.
4. Add house-money mode: after early profit, press toward `$100/day` while protecting a day floor so a green day cannot become red.

Do not implement while live positions are open unless the owner explicitly accepts handoff risk. Preferred flow:

1. Wait until broker is flat or close/stop is explicitly authorized by owner.
2. Stop both loop PIDs cleanly.
3. Verify locks are stale/removed and broker state is flat.
4. Create or confirm rollback point.
5. Patch code and tests.
6. Run focused tests.
7. Relaunch loops with documented env.
8. Verify full hunting chain, not just process start.

## Before-Work Handoff Checklist

Before starting any future change:

- Record timestamp, current branch/status, and dirty files.
- Run `python scripts\ctrader_mcp_watchdog.py`.
- Re-read broker balance, positions, and pending orders.
- Re-read `data/runtime/dexter3_loop.lock` and `data/runtime/dexter3_grok_loop.lock`.
- Re-read latest `data/runtime/dexter3_shadow.log`.
- State whether broker is flat. If not flat, say whether work is read-only or owner-approved live-risk work.
- Append a short note to `docs/AGENT_SYNC_BOARD.md`.

## After-Work Handoff Checklist

After any future change:

- Summarize files changed and why.
- Record tests run and exact results.
- Record whether loops were stopped/restarted.
- Record final MCP health and broker book.
- Confirm V1.6 label `dexter3:fable:m5h-v1` and Grok label `dexter3:grok-v1.0:scalper` remain isolated.
- Append final state to `docs/AGENT_SYNC_BOARD.md`.
- If positions are open, list every `positionId`, label, side, entry, SL, TP, and floating PnL.

## Do Not Do

- Do not merge V1.6 and Grok labels into one manager lane.
- Do not let Grok close or amend V1.6 positions.
- Do not globally increase size to chase `$100/day`.
- Do not trust stale `scalp_loop_state.json` or runtime state without broker re-read.
- Do not keep retrying MCP session calls if local MCP returns 404/refused/timeouts. Use watchdog recovery, then re-read broker state.

## After-Work Handoff — 2026-07-09T05:42Z

Work completed:

- Stopped V1.6 PID `23676` and Grok PID `11208` while broker was flat.
- Recovered local cTrader MCP with `python scripts\ctrader_mcp_watchdog.py --restart`.
- Patched `dexter3/shadow_runner.py` with V1.6-only profit controls:
  - weak buckets downsize immediately: `hunt_m15_drift`, `hunt_day_range_tilt`, `hunt_sweep_reclaim`;
  - winner buckets scale only after green-day effective PnL: `hunt_h1_context`, `hunt_swing_structure`, `basket_repair`, `opening_manager_repair`;
  - house-money floor arms after `DEXTER3_V16_HOUSE_THRESHOLD_USD` and locks the day at `DEXTER3_V16_HOUSE_FLOOR_USD` if giveback reaches the floor;
  - Grok mode bypasses these controls by `DEXTER3_MODE=grok`.
- Added focused tests in `tests/test_dexter3_wiring.py`.

Verification:

- `python -m py_compile dexter3\shadow_runner.py dexter3\grok_v10.py dexter3\opening_manager.py dexter3\daily_governor.py dexter3\edge_buckets.py dexter3\basket_live.py dexter3\executor.py tests\test_dexter3_opening_manager.py tests\test_dexter3_wiring.py tests\test_dexter3_governor.py` passed.
- `python -m pytest -q tests\test_dexter3_opening_manager.py tests\test_dexter3_governor.py tests\test_dexter3_basket_live.py tests\test_dexter3_wiring.py` -> `174 passed`.
- MCP health after restart OK, session `0c75b2ad`.

Running loops after work:

- V1.6 PID `1772`, lock `data/runtime/dexter3_loop.lock`, command `python -X utf8 -m dexter3.shadow_runner --symbols XAUUSD --loop --poll-sec 20 --live`.
- Grok PID `22404`, lock `data/runtime/dexter3_grok_loop.lock`, command `python -X utf8 -m dexter3.shadow_runner --symbols XAUUSD --loop --poll-sec 20 --live --grok`.

Broker state after restart:

- Initial post-restart broker read had two separate XAUUSD BUY positions, one per label.
- Final verification at `2026-07-09T05:43Z`: V1.6 position `650208638` was closed by its own OM `stall_take`; V1.6 is flat.
- Balance `10612.09`, equity about `10611.78`, no pending orders.
- Grok open position `650208650`, XAUUSD BUY, volume `1` unit / `0.01` lot, entry `4074.35`, SL `4059.41`, TP `4092.30`, label `dexter3:grok-v1.0:scalper`, net about `-0.31`.

Live rule proof:

- V1.6 logged `v16-profit-control: setup=hunt_h1_context reason=winner_waiting_for_green_day effective=-2.15 mult=1.0 risk_usd 4.03->4.03`, proving it did not scale a winner setup before the daily green threshold.

Next checks:

- Watch for the first weak setup log and confirm `reason=weak_bucket_downsize`.
- Watch for effective PnL crossing `+20` and confirm winner setup scaling changes to `green_day_winner_scale`.
- Watch for effective PnL crossing `+30`; house-money mode should arm, and if giveback later reaches `+20`, governor should lock the day.

## Codex V1.7 Update - 2026-07-09 08:30Z

Owner asked to compare original Fable, V1.1, V1.6, and Grok v1.0, then build V1.7 from the useful edges.

Findings:
- V1.1's useful edge was true-R accounting plus real governor sizing, not a new signal. That behavior is still required because it lets winners target real R instead of fake tiny-R exits.
- V1.6's useful edge is anti-chase downsize plus pullback-resumption sizing. The missing piece was that the later entry-quality layer could classify a setup as A+ `winner_pullback` and still block it as `chase_hard_block`.
- Grok v1.0's mistake was over-broad scalping: default `use_on_high_score=True` plus wrapper-forced `is_grok_scalp=True` meant high-quality pullback/winner trades could be cut by the small-lock path.

Changes shipped:
- `dexter3/v16_entry_quality.py`: A+ pullback/winner setups bypass chase hard-block as `pass_a_plus_chase_bypass`; MCP pause, min score, weak hard-skip, and anti-chase sizing remain active.
- `dexter3/grok_v10.py`: default Grok classifier no longer scalps high-score pullback winners, and `GrokV10OpeningManager` respects an existing `is_grok_scalp=False` classifier.
- `dexter3/shadow_runner.py`: startup log includes `version=v1.7-selective-edge`; V1.7 entry-quality config logs cooldown status.
- `ops/dexter3_xau_v16_loop.ps1`: sets `DEXTER3_FABLE_VERSION=v1.7-selective-edge`; cooldown remains disabled with `DEXTER3_V16_COOLDOWN_ENABLED=0`.

Verification:
- `python -m py_compile dexter3\shadow_runner.py dexter3\grok_v10.py dexter3\v16_entry_quality.py dexter3\opening_manager.py tests\test_dexter3_v16_entry_quality.py tests\test_dexter3_opening_manager.py`
- `python -m pytest -q tests\test_dexter3_v16_entry_quality.py tests\test_dexter3_opening_manager.py tests\test_dexter3_wiring.py tests\test_dexter3_governor.py tests\test_dexter3_basket_live.py` -> `189 passed`

Live handoff:
- V1.7 was started successfully during the rollout and its startup log proved `version=v1.7-selective-edge` plus `cooldown_enabled=False`.
- Final safe state is **Grok only**. Both-loop load repeatedly made local cTrader MCP return HTTP 404 to fresh audit clients, so Codex stopped V1.7 and the cTrader-open watcher, recovered MCP, then relaunched Grok to manage the already-open Grok position.
- Final verified Grok PID `4804`, lock `data/runtime/dexter3_grok_loop.lock`, label `dexter3:grok-v1.0:scalper`.
- Final verified broker read: open Grok BUY `650252269`, XAUUSD, volume `1`, entry `4115.5`, current `4114.12`, SL `4104.51`, TP `4128.7`, net about `-1.62`, no pending orders. V1.7 has no running PID and no open position in the final safe state.
- Important caveat: local cTrader MCP can still return HTTP 404 for a fresh audit client under two-loop load. Treat this as MCP session-pressure/handler fragility. Relaunch V1.7 only after Grok is flat or after fixing the MCP session-pressure issue, then require a fresh broker audit while both lanes have been alive for several minutes.

## Codex Mission-Control Replay - 2026-07-09 09:35Z

Owner approved the next mission-control step after the initial V1.7 BT.

Changes:
- `scripts/dexter3_edge_discovery.py` now supports exact entry-quality gate replay with `--entry-gate none|v16|v17|v17-mission`.
- Replay now stamps live-equivalent `anti_chase` and `pullback_gate` features before calling `evaluate_v16_entry_gate`.
- `dexter3/v16_entry_quality.py` has an optional diagnostic guard `block_chase_bypass_on_aligned_trending`; it is intentionally default-off because the replay rejected it.
- `shadow_runner.py` can log/read the optional guard via `DEXTER3_V17_BLOCK_ALIGNED_TRENDING_CHASE_BYPASS`, but the launcher does not set it.

Exact replay results, same 1,000 M5 window around `2026-07-03T14:10:00Z -> 2026-07-09T09:25:00Z`:
- V1.6 gate: 112 accepted / 938 candidates, `+28.4R`, `+0.253R/trade`.
- Current V1.7 gate: 127 accepted / 938 candidates, `+32.3R`, `+0.255R/trade`.
- Strict `v17-mission` guard: 106 accepted / 938 candidates, `+24.1R`, `+0.227R/trade`.

Decision:
- Current `v1.7-selective-edge` remains the winner. Do not enable the strict aligned-trending bypass block unless new forward evidence changes this.
- V1.7 should still remain stopped live until Grok is flat or local MCP session pressure is solved. The replay validates strategy edge, not two-loop operational readiness.

Verification:
- `python -m py_compile dexter3\v16_entry_quality.py dexter3\shadow_runner.py scripts\dexter3_edge_discovery.py tests\test_dexter3_v16_entry_quality.py`
- `python -m pytest -q tests\test_dexter3_v16_entry_quality.py tests\test_dexter3_edge_gate.py` -> `49 passed`
- `python -m pytest -q tests\test_dexter3_v16_entry_quality.py tests\test_dexter3_opening_manager.py tests\test_dexter3_wiring.py tests\test_dexter3_governor.py tests\test_dexter3_basket_live.py tests\test_dexter3_edge_gate.py` -> `225 passed`

Live/MCP state after work:
- A final replay initially hit the known local cTrader MCP zombie (`HTTP 404`). Codex ran `python scripts\ctrader_mcp_watchdog.py --restart`; recovered MCP session `7fe552ce`.
- Grok lane still running: PID `4804`, lock `data/runtime/dexter3_grok_loop.lock`.
- V1.7/Fable lock is absent.
- Broker read after recovery/final sanity check: balance `10634.50`, equity `10627.17`, one XAUUSD BUY `650252269`, label `dexter3:grok-v1.0:scalper`, volume `1` unit / `0.01` lot, entry `4115.50`, current `4108.41`, SL `4104.51`, TP `4128.70`, net about `-7.33`, no pending orders.
