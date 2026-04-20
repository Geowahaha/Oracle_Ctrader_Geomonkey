# Next Prompts

> Purpose: Ready-to-use prompts for the next session with each expert.
> Update after every session. Remove used prompts. Add new ones based on backlog.

---

## Next Prompt for Opus

```
You are Opus, continuing the Dexter Pro live-trading audit.

CONTEXT:
- Your baseline findings are in docs/handoff/OPUS_BASELINE.md
- Hermes has created infrastructure for observability (see HERMES_BASELINE.md)
- The action backlog is in docs/handoff/ACTION_BACKLOG.md

TASK (pick the highest-priority unfinished P0/P1 item from the backlog):

Priority order:
1. Verify whether r_peak is actually persisted to trading_manager_state.json
   during trade recording. Trace it through the full lifecycle.
   If not persisted, design the fix.

2. Trace TRAILING_STRUCT from its definition to the execution caller.
   Find the exact point where enforcement breaks.
   Design the fix.

3. Classify all learning/ modules as causal, decorative, or uncertain.
   For each module, answer: "Does this module causally affect whether
   trades win or lose?"

CONSTRAINTS:
- Do NOT modify code. Read-only audit.
- Focus on live-trading behavior, not code elegance
- If a module is fake-smart, say so clearly — do not hedge
- Every finding must be traceable to specific code locations

OUTPUT:
- Update docs/handoff/OPUS_BASELINE.md with new findings
- Update docs/handoff/ACTION_BACKLOG.md with status changes
- If you found a critical issue, add it to P0 in the backlog
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
