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
| **Phase now** | **E** — reversal-zone capture + Fib `61.8` template gate live; Trading Central intraday canary lane + payload producer deployed to VM (experimental) |
| **Last updated (UTC)** | 2026-04-23T08:45Z |
| **Last updated by** | codex |

---

## Needs / questions (open)

<!-- Example:
- [ ] `@peer` Confirm whether VM path `/opt/dexter_pro/data/ctrader_openapi.db` is still authoritative — 2026-04-04 Agent A
-->

*None yet — remove this line when first real item exists.*

---

## Owner — latest (≤1 paragraph)

**2026-04-23:** Trading Central intraday canary lane is now deployed on the VM (commit `213cd3e`) as experimental-only. New module `learning/trading_central_payload_producer.py` normalizes Trading Central panel text / raw JSON into `data/runtime/trading_central_intraday_signal.json`, and the scheduler can execute `xau_scalp_trading_central_intraday` via `scalp_xauusd:tc:canary` (family-level MTF guard bypass applies only to this lane). VM demo test trade succeeded (ORDER_ACCEPTED) after increasing VM `.env.local` `CTRADER_EXECUTOR_TIMEOUT_SEC` from `25` to `60` and restarting `dexter-monitor` (timeouts were blocking `:tc:` executions).

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

### 2026-04-23 UTC 08:45Z — codex — Phase E

- VM rollout: head is now `213cd3e` ("Add Trading Central intraday canary producer"); `dexter-monitor` active.
- Producer output path: `data/runtime/trading_central_intraday_signal.json` (see `docs/trading_central_intraday_signal.example.json`).
- Live test (DEMO, non-dry-run): executed one `scalp_xauusd:tc:canary` market BUY; broker response `ORDER_ACCEPTED` with `order_id=959356719` and `position_id=610034895`.
- Operational issue: repeated `worker timeout after 25s` blocked execution and cTrader sync; fixed by updating VM `/opt/dexter_pro/.env.local`:
- `CTRADER_EXECUTOR_TIMEOUT_SEC=60`
- `CTRADER_HEALTHCHECK_TIMEOUT_SEC=45`
- and restarting `dexter-monitor`.

---

**Cross-links**

- Mission detail: `docs/AGENT_HANDOFF_XAU_GATE_ENTRY_TEMPLATE.md`
- Session bootstrap: `CLAUDE.md` → Critical Files + Session Startup
