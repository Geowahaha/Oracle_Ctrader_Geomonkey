# Fable 5 ↔ Codex mission handoff — 2026-07-10

**Scope:** make the $100/day virtual-capital mission measurable and safe to
iterate. This handoff records a narrow P0 safety repair and the current
operational facts. It does **not** authorize live orders, amendments, closes,
service restarts, or `.env` changes.

## What Codex changed

`tests/test_dexter3_wiring.py` previously called `shadow_runner.run_om_tick()`
with the module's production `STATE_FILE` and production logging functions.
Those tests can run the normal state-save path, so a local test run could
overwrite `data/runtime/dexter3_shadow_state.json` or pollute the live shadow
log while a lane is trading.

Added `_isolate_shadow_runtime()` and applied it to both OM runner-glue tests:

- redirects both Fable and Grok state files to pytest `tmp_path`;
- replaces `log_line` and `log_error` with test-local no-ops;
- leaves production code and production runtime configuration unchanged.

Verification: `python -m pytest -q tests/test_dexter3_wiring.py -k
"run_om_tick"` → **2 passed, 53 deselected** (0.46s), and `git diff --check`
was clean.

## Current operational concern — do not ignore

The board declares VM cutover complete and says PC lanes were stopped, but this
machine currently shows local `shadow_runner --live` processes for both Fable
and Grok with matching local locks. The VM unit files explicitly prohibit
running those PC-hosted lanes at the same time as the VM because labels and the
demo account overlap. This is a **possible duplicate-owner condition**, not a
license to stop anything blindly.

Observed read-only broker snapshot during the audit:

- demo login `9922808`, account `46670728`;
- one Fable-labelled XAUUSD sell position, id `650869928`, 1 oz, floating near
  `-$21` at the time of observation;
- Grok had no open labelled position and its local state reported loss-stopped.

Before any strategy/size change, nominate exactly one live owner and prove it
with VM `systemctl` status + local PID/lock status + broker labels. If a handoff
requires stopping a live process or touching the position, get explicit owner
authority first.

## Mission truth as of this handoff

- V1.8's $100/day is a **modelled P5 target**, not forward-validated profit.
- The audit's production-shaped replay was roughly $31/day before all live
  portfolio constraints; history remains negative overall. Do not scale risk.
- Keep the freeze until at least 100 forward trades or four weeks, PF > 1.2,
  and no state/ownership mismatch.
- `fear_cost` is look-ahead contaminated and learner outcomes are not yet
  complete enough to validate a self-learning claim. Neither may justify a
  promotion.

## Ordered next work for Fable 5

1. **Resolve ownership, read-only first.** Reconcile VM service status with
   local PIDs/locks and the broker. Record the single canonical owner on
   `docs/AGENT_SYNC_BOARD.md`; do not issue trading mutations under this
   handoff.
2. **Make the learner real.** At close, persist labelled `setup`, session,
   direction/context, R/PnL, and exit reason in the schema consumed by
   `empirical_stats`. Add a close-to-stats integration test using a temporary
   database.
3. **Remove look-ahead from skip evaluation.** Freeze existing `fear_cost`
   claims as invalid; evaluate an entry only using information available at its
   timestamp, then score the later realised outcome.
4. **Build the execution-exact promotion report.** Replay must include
   min-volume floors, current governor/OM rules, spread/costs, daily stops and
   one active lane. It should produce provenance that joins decision → order →
   deal → outcome.
5. **Only then evaluate the edge.** Run the loss firewall/context-switchboard
   candidates in shadow with confidence intervals; promote one bounded canary
   only if it improves the same forward window.

## Files to read first

- `docs/DEXTER3_HANDOFF.md`
- `docs/AGENT_SYNC_BOARD.md`
- `docs/AGENT_HANDOFF_XAU_GATE_ENTRY_TEMPLATE.md`
- `dexter3/shadow_runner.py`
- `dexter3/executor.py`
- `dexter3/empirical_stats.py`
- `tests/test_dexter3_wiring.py`

## Guardrails

- Do not run unisolated pytest against live state; this patch protects the two
  `run_om_tick` tests only.
- Preserve the dirty worktree; do not reset, clean, or merge broad VM changes.
- Do not restart a lane or submit/modify an order without explicit owner
  authority.
