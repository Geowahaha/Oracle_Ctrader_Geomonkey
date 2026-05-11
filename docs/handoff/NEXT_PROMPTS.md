# Next Prompts

> Purpose: Ready-to-use prompts for the next session with each expert.
> Update after every session. Remove used prompts. Add new ones based on backlog.

---

## Next Prompt for Opus

```
You are Opus 4.7, reviewer only for Dexter Pro Fibo MTF trading behavior.

Read first:
- docs/handoff/OPUS47_FIBO_MTF_PLANNER_REVIEW_20260511.md
- docs/handoff/ACTION_BACKLOG.md P0-7/P0-8/P0-9

Current accepted truth:
- FIBO_MTF_SHADOW live leak caused major losses and must not dispatch directly.
- Hermes wired `FiboMtfTradePlanner` as shadow-only route metadata: observe_only/probe/base_live/runner_add.
- Current patch forces `fibo_mtf_live_enabled=False` and stores `xau_shadow_journal.block_reason=fibo_mtf_planner:<route>`.

TASK:
After shadow telemetry exists, evaluate route evidence for profitability and decide whether `probe` may be promoted to micro-live.

Required gates before approve:
1. ≥30 shadow probe decisions, ≥14 calendar days, ≥2 sessions.
2. Planner-geometry shadow expectancy ≥ +0.4R and win-rate ≥50%.
3. No simulated MAE > 3.5R.
4. ≥95% probe decisions have real local execution anchors.
5. Fresh cTrader tick/spread/min-stop/news/conflict context included in any future live adapter.
6. Distinct non-shadow source token and env kill switch are mandatory.

Output:
- APPROVE/BLOCK/NEEDS CHANGES for micro-live probe only.
- Exact evidence and line-level blockers.
- Do not implement code unless explicitly asked.
```

---

## Next Prompt for Hermes

```
You are Hermes, continuing architectural hardening of Dexter Pro.

CONTEXT:
- Your baseline is in docs/handoff/HERMES_BASELINE.md
- Opus's findings are in docs/handoff/OPUS_BASELINE.md
- The action backlog is in docs/handoff/ACTION_BACKLOG.md

TASK (pick the highest-priority unfinished P0 item from the backlog):

Priority order:
1. Run ctrader_openapi.db health check:
   - Verify reads work (query each table)
   - Verify writes work (create temp table, insert, drop)
   - Check journal mode (WAL vs rollback)
   - Report size and row counts
   - If I/O errors, diagnose and propose recovery

2. Add atomic_json_write utility:
   - Create utils/atomic_write.py
   - atomic_json_write(path, data) — write to .tmp, rename
   - atomic_json_read(path, default) — handle interrupted writes
   - Apply to data/runtime/trading_manager_state.json
   - Zero behavior change — drop-in replacement

3. Add r_peak persistence verification at startup:
   - In scheduler _run_loop, before any scanning
   - Read trading_manager_state.json
   - Check for xau_r_peak or r_peak key
   - Log error if missing
   - Attempt reconstruction from trade history if missing

CONSTRAINTS:
- Do NOT modify trading logic, execution logic, or risk logic
- Only add logging, verification, or infrastructure
- Every change must be backward-compatible
- Create/update tests for new infrastructure code

OUTPUT:
- Create or modify files as needed
- Update docs/handoff/HERMES_BASELINE.md with progress
- Update docs/handoff/ACTION_BACKLOG.md with status changes
- Commit with message: "[Hermes] <description> — documentation/infrastructure only"
```

---

## Reminder: Avoid Overlap

```
IF the task involves evaluating whether trading logic is profitable → OPUS
IF the task involves evaluating whether a module adds causal value → OPUS
IF the task involves changing confidence thresholds → OPUS
IF the task involves changing execution logic → OPUS
IF the task involves changing risk parameters → OPUS

IF the task involves making a problem visible → HERMES
IF the task involves restructuring code safely → HERMES
IF the task involves preventing data corruption → HERMES
IF the task involves monitoring production health → HERMES
IF the task involves database maintenance → HERMES
IF the task involves config safety → HERMES

WHEN IN DOUBT: check the ownership split in HANDOFF_MULTI_EXPERT.md
```

---

## Delta Mode Guidance

```
DO NOT:
  - Re-read the entire codebase from scratch
  - Re-state findings already in the baseline files
  - Praise the system's complexity
  - Propose rewrites of working code
  - Modify trading/execution/risk logic

DO:
  - Read the baseline files first (OPUS_BASELINE.md, HERMES_BASELINE.md)
  - Check ACTION_BACKLOG.md for highest-priority unfinished item
  - Work on that item
  - Update the baseline files with new findings
  - Update the backlog with status changes
  - Commit and push

EVERY SESSION SHOULD END WITH:
  1. Updated baseline file(s) with new findings
  2. Updated backlog with status changes
  3. A clean commit on the working branch
  4. Clear handoff to the next expert
```
