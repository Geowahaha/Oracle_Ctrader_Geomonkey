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
| **Phase now** | **E** — reversal-zone capture + Fib `61.8` template gate deployed on VM |
| **Last updated (UTC)** | 2026-04-22T14:25Z |
| **Last updated by** | codex |

---

## Needs / questions (open)

<!-- Example:
- [ ] `@peer` Confirm whether VM path `/opt/dexter_pro/data/ctrader_openapi.db` is still authoritative — 2026-04-04 Agent A
-->

*None yet — remove this line when first real item exists.*

---

## Owner — latest (≤1 paragraph)

**2026-04-22:** Live bundle deployed on `deploy-xau-family-canary` at commit `323342e`: targeted XAU reversal-zone capture (`armed` + `confirmed`) now writes tagged capture events for dataset mining, and Fib `61.8/0.65` entries now require reversal-template fit when capture is available. Local verification passed (`py_compile`, reversal dataset tests, new Fib template tests, full Fib regression `90 passed`), VM pulled the commit and `dexter-monitor` restarted `active`; residual journal warning is the pre-existing stale auth token check, not a startup failure.

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

---

**Cross-links**

- Mission detail: `docs/AGENT_HANDOFF_XAU_GATE_ENTRY_TEMPLATE.md`
- Session bootstrap: `CLAUDE.md` → Critical Files + Session Startup
