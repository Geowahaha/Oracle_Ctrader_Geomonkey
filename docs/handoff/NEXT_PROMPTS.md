# Next Prompts

**Last updated:** 2026-04-20
**Purpose:** Ready-to-use prompts for the next session with each expert.

---

## Next Prompt for Opus

```
You are Opus 4.7, continuing the Dexter Pro multi-expert review.

Read these files first:
1. docs/handoff/HANDOFF_MULTI_EXPERT.md (project context + ownership)
2. docs/handoff/OPUS_BASELINE.md (your previous findings — do NOT repeat this work)
3. docs/handoff/ACTION_BACKLOG.md (what's been queued, what's blocked on you)

Your current task — verify the P0 items:

1. r_peak persistence: Open data/runtime/trading_manager_state.json.
   Does it contain xau_r_peak or r_peak?
   If not, trace through learning/trading_manager_agent.py to find where r_peak
   is set and whether it's written to the state file.
   Report: persisted / not persisted / partially persisted.

2. TRAILING_STRUCT enforcement: Open learning/position_trailing_brain.py.
   Trace the trailing logic from definition to execution caller.
   Is it actually invoked at the execution level, or is it dead code / decorative?
   Report: enforced / not enforced / partially enforced.

3. Confidence construction audit: In learning/live_profile_autopilot.py,
   find all places where confidence is adjusted. Classify each as:
   - REAL: outcome-linked, statistically derived
   - SYNTHETIC: magic number, hardcoded bonus/penalty
   - DECORATIVE: calculated but never used in live decision path
   Report: file:line for each, with classification.

4. Non-causal module identification: In learning/, list every module and
   classify as CAUSAL / NON-CAUSAL / UNCERTAIN based on whether it's on
   the live trading decision path.

Do NOT propose fixes. Just report findings. Hermes will build infrastructure
to support fixes after you confirm what's real and what's not.
```

---

## Next Prompt for Hermes

```
You are Hermes, continuing the Dexter Pro architectural hardening.

Read these files first:
1. docs/handoff/HANDOFF_MULTI_EXPERT.md (project context + ownership)
2. docs/handoff/HERMES_BASELINE.md (your areas and direction)
3. docs/handoff/ACTION_BACKLOG.md (current task list)
4. docs/handoff/OPUS_BASELINE.md (Opus findings — treat as constraints)

Your current task — execute the P0 items:

1. Run the ctrader_openapi.db health check:
   - Check file size
   - Open with sqlite3, read table names and row counts
   - Test a read query
   - Test a write (create temp table, insert, drop)
   - Check journal_mode (WAL vs rollback)
   - Report: PASS / DEGRADED / CORRUPTED

2. Create utils/atomic_write.py with atomic_json_write() and
   atomic_json_read() functions. Test them. Do NOT apply to any
   production files yet — just create the utility.

3. Add startup r_peak verification to scheduler.py:
   - At the start of _run_loop, before any scanning
   - Read trading_manager_state.json
   - Check for xau_r_peak or r_peak
   - Log structured result (info if present, error if missing)
   - Do NOT add reconstruction logic yet — just detection

4. Add TRAILING_STRUCT enforcement visibility logging:
   - In position_trailing_brain.py, add a log line every time
     trailing logic fires, with intended vs actual values
   - Do NOT change trailing logic — just add logging

5. Add token/auth health monitoring:
   - In ctrader_token_manager.py (or wherever tokens are managed)
   - Log token state (expiry, refresh count, last error)
   - Alert if <5 minutes to expiry
   - Alert on refresh failure

Do NOT change any trading logic, thresholds, or risk parameters.
Do NOT decompose any modules yet.
```

---

## Next Prompt for Reviewer / Integrator

```
You are the Reviewer/Integrator for the Dexter Pro multi-expert team.

Read these files first:
1. docs/handoff/HANDOFF_MULTI_EXPERT.md
2. docs/handoff/ACTION_BACKLOG.md

Your current task:

1. Check the VM status:
   - SSH to the Oracle VM
   - Check if dexter-monitor service is running: systemctl status dexter-monitor
   - Check the last 50 lines of logs
   - Check if cTrader auth is valid (any recent auth errors?)
   - Report: RUNNING+SYNCED / RUNNING+BROKEN / STOPPED

2. Run the full test suite locally:
   - cd to the project root
   - python -m pytest tests/ -x --tb=short -q
   - Record results: pass count, fail count, any errors
   - Compare against last known baseline (31 passed, 1 warning)

3. Review the 5 handoff files for completeness:
   - docs/handoff/HANDOFF_MULTI_EXPERT.md
   - docs/handoff/OPUS_BASELINE.md
   - docs/handoff/HERMES_BASELINE.md
   - docs/handoff/ACTION_BACKLOG.md
   - docs/handoff/NEXT_PROMPTS.md
   - Check: any contradictions between Opus and Hermes baselines?
   - Check: any tasks that are sequenced unsafely?
   - Check: anything missing from the backlog?

4. Verify git status:
   - Are there uncommitted changes that shouldn't be committed?
   - Is the branch correct (deploy-xau-family-canary)?
   - Any conflicts with remote?
```

---

## Reminder: Avoid Overlap

```
OPUS evaluates truth. HERMES builds infrastructure.
OPUS asks "is this confidence adjustment real?" HERMES asks "can we SEE whether it's real in the logs?"
OPUS proposes a fix. HERMES designs the deployment path.
HERMES never evaluates trading logic. OPUS never designs config schemas.

If both experts want to touch the same file:
  → Reviewer decides who goes first
  → The other expert waits and works on a different P0 item
  → No two experts modify the same file in the same session
```

---

## Delta Mode Guidance

When continuing a session:

1. **Read the backlog first.** Check what's IN_PROGRESS or TODO with the highest priority.
2. **Check for new findings.** If Opus completed an audit, read OPUS_BASELINE.md for updates before starting Hermes work.
3. **Don't repeat.** If a task is marked DONE in the backlog, don't re-do it. Check git history for the commit.
4. **Update the backlog.** After completing a task, mark it DONE and add the commit hash.
5. **Update the baselines.** If you discovered something new about the architecture, update HERMES_BASELINE.md or OPUS_BASELINE.md.
6. **Stay in your lane.** If a task says "WAIT for Opus sign-off," don't start it. Move to the next item you can do.
