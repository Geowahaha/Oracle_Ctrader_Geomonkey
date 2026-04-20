# Next Prompts

> Ready-to-use prompts for continuing multi-expert work.
> Copy-paste into a new session. Adjust as needed.

---

## Next Prompt for Opus

```
You are Opus 4.7, continuing your review of Dexter Pro.

You have a full baseline in docs/handoff/OPUS_BASELINE.md.
Read it before starting. Do not re-audit what you've already covered.

YOUR CURRENT TASKS (in priority order):

1. r_peak PERSISTENCE VERIFICATION
   - Search the codebase for where r_peak is defined, updated, and read
   - Does it write to trading_manager_state.json?
   - Does it read back at startup?
   - If not persisted, what happens after restart?
   - Report: mechanism, gap, and fix recommendation

2. TRAILING_STRUCT ENFORCEMENT VERIFICATION
   - Search for TRAILING_STRUCT, position_trailing_brain, trailing logic
   - Is trailing stop logic actually invoked by execution callers?
   - Where is the gap between intent and enforcement?
   - Report: exact location of gap and fix recommendation

3. CAUSAL AUDIT OF LEARNING/ MODULES
   - For each file in learning/, determine: is it on the live signal→execution path?
   - Classify: REAL / DECORATIVE / MIXED / UNCERTAIN
   - Focus on: strategy_evolution, strategy_lab_team, adaptive_directional_intelligence,
     signal_simulator, entry_template_catalog, scalping_runtime
   - Report: table of module → status → evidence

4. CONFIDENCE PATH VERIFICATION
   - Trace confidence from signal generation → _apply_neural_soft_adjustment → gate → execution
   - Is the final confidence value that gates use actually calibrated?
   - Or is it synthetic (magic numbers, no outcome linkage)?
   - Report: which paths are calibrated, which are synthetic

Output format: structured findings with file:line references.
Do NOT modify any code. Read-only review.
```

---

## Next Prompt for Hermes

```
You are Hermes, continuing architectural hardening of Dexter Pro.

You have a full baseline in docs/handoff/HERMES_BASELINE.md.
Read it before starting. Do not repeat analysis already done.

YOUR CURRENT TASKS (in priority order):

1. DB HEALTH CHECK
   - Run diagnostic on data/ctrader_openapi.db
   - Check: size, journal mode, table counts, read test, write test
   - If I/O errors: assess severity and recommend immediate action
   - Report: health status + recommended immediate actions

2. ATOMIC WRITE UTILITY
   - Create utils/atomic_write.py (or similar location)
   - Implement atomic_json_write() and atomic_json_read()
   - Apply to data/runtime/trading_manager_state.json
   - Verify the write is actually atomic (write to .tmp, then rename)
   - Report: utility created, files migrated

3. STRUCTURED LOGGING SETUP
   - Add structlog to requirements
   - Create logging_setup.py with JSON processor configuration
   - Add signal correlation ID propagation (bind to structlog contextvar)
   - Convert 3-5 key methods in scheduler.py to use structured logging
   - Report: setup complete, sample log output

4. STARTUP CONFIG ASSERTIONS
   - Create config/validators.py (or add to existing config.py)
   - Implement assertions for:
     * r_peak presence in trading_manager_state.json
     * TRAILING_STRUCT + POSITION_MANAGER consistency
     * Token timeout vs healthcheck timeout
     * Opportunity suppression risk (high confidence + high sharpness thresholds)
   - Run at startup, log warnings (do not block startup)
   - Report: assertions created, sample output

Output format: files created/modified, test results, any issues found.
Do NOT modify trading logic, execution logic, or risk logic.
```

---

## Reminder: No-Overlap Rules

```
IF Opus is asked about:     → Refuse. That's Hermes's domain:
  - Refactoring
  - Config architecture
  - Logging infrastructure
  - Scheduler decomposition
  - DB archival
  - Thread model

IF Hermes is asked about:   → Refuse. That's Opus's domain:
  - Whether a confidence adjustment is "real"
  - Which modules are fake-smart
  - Execution wiring correctness
  - Per-gate ROI evaluation
  - Winner protection logic
  - TRAILING_STRUCT enforcement correctness

OVERLAP ZONE (coordinate via reviewer):
  - Runtime state persistence (Opus identifies need, Hermes implements)
  - Startup verification (Opus defines what to check, Hermes implements)
  - Observability for live-trading issues (Opus identifies issue, Hermes instruments)
```

---

## Delta Mode Guidance

Both experts should work in **delta mode** — only analyze what's new or changed since last review.

**For Opus:**
- Start from `OPUS_BASELINE.md`. Only re-examine code that has changed since 2026-04-20.
- Focus on the 4 tasks above. Do not broaden the review.
- If you find something that contradicts the baseline, note it as a DELTA.

**For Hermes:**
- Start from `HERMES_BASELINE.md`. Only re-examine code that has changed since 2026-04-20.
- Focus on the 4 tasks above. Do not broaden the infrastructure work.
- If you discover a new architectural risk not in the baseline, note it as a DELTA.

**After completing tasks:**
1. Update the relevant baseline file (OPUS_BASELINE.md or HERMES_BASELINE.md)
2. Update ACTION_BACKLOG.md status field
3. Commit with message format: `[Opus|Hermes] <scope>: <description>`
4. Push to `deploy-xau-family-canary` branch
