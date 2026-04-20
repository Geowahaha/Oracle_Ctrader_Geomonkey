# Action Backlog

**Last updated:** 2026-04-20
**Branch:** `deploy-xau-family-canary`

---

## How to Read This Backlog

- **Owner:** Opus | Hermes | Reviewer | Opus+Hermes
- **Status:** TODO | IN_PROGRESS | BLOCKED | DONE | CANCELLED
- **Safe to do live?** YES | NO | WEEKEND_ONLY | DRY_RUN_ONLY
- Sequenced for minimal risk. Do not skip ahead.

---

## P0 — Immediate (this week)

| # | Task | Owner | Status | Safe? | Depends | File(s) |
|---|------|-------|--------|-------|---------|---------|
| 1 | Run `ctrader_openapi.db` health check — verify reads, writes, WAL mode, table counts | Hermes | TODO | YES | — | `data/ctrader_openapi.db` |
| 2 | Implement `atomic_json_write()` utility | Hermes | TODO | YES | — | new: `utils/atomic_write.py` |
| 3 | Apply atomic writes to `trading_manager_state.json` | Hermes | TODO | YES | #2 | `data/runtime/trading_manager_state.json` |
| 4 | Add r_peak persistence verification at startup | Hermes | TODO | YES | #3 | `scheduler.py` (startup) |
| 5 | Add TRAILING_STRUCT enforcement visibility logging | Hermes | TODO | YES | — | `learning/position_trailing_brain.py`, `scheduler.py` |
| 6 | Add token/auth refresh health monitoring | Hermes | TODO | YES | — | `api/ctrader_token_manager.py` |
| 7 | Verify r_peak — is it persisted? What breaks after restart? | Opus | TODO | YES | — | `learning/trading_manager_agent.py`, `data/runtime/trading_manager_state.json` |
| 8 | Verify TRAILING_STRUCT — is enforcement missing at caller level? | Opus | TODO | YES | — | `learning/position_trailing_brain.py`, execution callers |
| 9 | Audit confidence construction — which paths are synthetic? | Opus | TODO | YES | — | `learning/live_profile_autopilot.py`, `learning/neural_brain.py` |
| 10 | Check cTrader auth status on VM — is broker-synced? | Reviewer | TODO | YES | — | VM `/opt/dexter_pro` |

---

## P1 — Short-term (next 2 weeks)

| # | Task | Owner | Status | Safe? | Depends | File(s) |
|---|------|-------|--------|-------|---------|---------|
| 11 | Add startup config assertions (r_peak, trailing, suppression risk) | Hermes | TODO | YES | #4 | `config.py` (new: `config/validators.py`) |
| 12 | Enable WAL mode on `ctrader_openapi.db` if not already | Hermes | TODO | WEEKEND_ONLY | #1 | `data/ctrader_openapi.db` |
| 13 | Add per-gate rejection telemetry (structured logging) | Hermes | TODO | YES | — | `scheduler.py` (all gate methods) |
| 14 | Add gate ROI attribution data collection | Hermes | TODO | YES | #13 | new: `telemetry/gate_roi.py` |
| 15 | Add opportunity suppression tracker | Hermes | TODO | YES | #13 | new: `telemetry/opportunity_suppression.py` |
| 16 | Add scheduler heartbeat monitoring | Hermes | TODO | YES | — | `scheduler.py` (_run_loop) |
| 17 | Identify which gates are validated by outcome attribution | Opus | TODO | YES | #14 | `scheduler.py` (gate methods) |
| 18 | Identify which "smart" modules are non-causal | Opus | TODO | YES | — | `learning/*.py` |
| 19 | Validate opportunity suppression — which gates block good trades? | Opus | TODO | YES | #15 | gate data + trade outcomes |
| 20 | Run full test suite, record baseline | Reviewer | TODO | YES | — | `tests/` |

---

## P2 — Medium-term (weeks 3-6)

| # | Task | Owner | Status | Safe? | Depends | File(s) |
|---|------|-------|--------|-------|---------|---------|
| 21 | Set up structured logging (structlog) | Hermes | TODO | YES | — | new: `logging_setup.py` |
| 22 | Extract report generators from scheduler.py → `scheduler/reports/` | Hermes | TODO | WEEKEND_ONLY | #20 | `scheduler.py` |
| 23 | Apply atomic writes to all `data/runtime/*.json` files | Hermes | TODO | YES | #2 | `data/runtime/*.json` |
| 24 | Implement DB archival — first pass (backup only, no deletion) | Hermes | TODO | WEEKEND_ONLY | #1, #12 | `ops/db_archive.py` |
| 25 | Extract family builders from scheduler.py → `scheduler/families/` | Hermes | TODO | WEEKEND_ONLY | #22 | `scheduler.py` |
| 26 | Fix confidence construction in paths Opus flagged | Opus | TODO | DRY_RUN_ONLY | #9 | `learning/*.py` |
| 27 | Remove or deprioritize non-causal modules | Opus+Hermes | TODO | WEEKEND_ONLY | #18 | `learning/*.py`, `scheduler.py` |
| 28 | Add rejection funnel dashboard report | Hermes | TODO | YES | #13 | `scheduler/reports/` |

---

## P3 — Long-term (weeks 6+, after Opus completes live audit)

| # | Task | Owner | Status | Safe? | Depends | File(s) |
|---|------|-------|--------|-------|---------|---------|
| 29 | Extract guard logic from scheduler.py → `scheduler/guards/` | Hermes | TODO | WEEKEND_ONLY | #17, #20 | `scheduler.py` |
| 30 | Extract routing logic from scheduler.py → `scheduler/routing.py` | Hermes | TODO | WEEKEND_ONLY | #29, #20 | `scheduler.py` |
| 31 | Decompose ctrader_executor.py (DB, source gating, execution) | Hermes | TODO | WEEKEND_ONLY | Opus sign-off | `execution/ctrader_executor.py` |
| 32 | Evaluate live_profile_autopilot.py decomposition | Hermes | TODO | YES | #18 | `learning/live_profile_autopilot.py` |
| 33 | Migrate config to Pydantic validation | Hermes | TODO | YES | — | `config.py` |
| 34 | Separate read/write DB connections in ctrader_executor | Hermes | TODO | WEEKEND_ONLY | #12, #24 | `execution/ctrader_executor.py` |
| 35 | Fix confidence magic numbers → statistically derived values | Opus | TODO | DRY_RUN_ONLY | #9 | `learning/live_profile_autopilot.py` |
| 36 | Add notification queue (prevent Telegram I/O blocking) | Hermes | TODO | YES | #21 | `notifier/telegram_bot.py` |

---

## Completed

| # | Task | Owner | Date | Notes |
|---|------|-------|------|-------|
| — | Multi-expert handoff structure created | Hermes | 2026-04-20 | This file + 4 sibling docs |

---

## Blocked / Waiting

| # | Task | Blocked By | Notes |
|---|------|-----------|-------|
| 29-31 | Guard/routing/executor extraction | Opus: must validate causal status first | Do not decompose non-causal modules |
| 32 | autopilot.py decomposition | Opus: must identify causal sub-modules first | Flag non-causal parts for removal, not refactoring |
| 35 | Confidence magic number fix | Opus: must map which paths are synthetic first | Statistical calibration requires knowing which adjustments are real |

---

## Dependency Graph (simplified)

```
#1 (DB health check)
 ├→ #12 (WAL mode)
 │   └→ #24 (DB archival)
 │       └→ #34 (separate R/W connections)
 └→ #27 (remove non-causal — needs Opus audit first)

#2 (atomic write utility)
 ├→ #3 (apply to trading_manager_state)
 │   └→ #4 (r_peak verification)
 │       └→ #11 (config assertions)
 └→ #23 (apply to all runtime JSON)

#13 (gate rejection telemetry)
 ├→ #14 (gate ROI attribution)
 │   └→ #17 (Opus validates gates)
 │       └→ #29 (extract guards — only causal ones)
 └→ #15 (opportunity suppression)
     └→ #19 (Opus validates suppression)
         └→ #26 (fix confidence in flagged paths)

#20 (test suite baseline)
 ├→ #22 (extract reports)
 │   └→ #25 (extract families)
 │       └→ #29 (extract guards)
 │           └→ #30 (extract routing)
 └→ #27 (remove non-causal)
```
