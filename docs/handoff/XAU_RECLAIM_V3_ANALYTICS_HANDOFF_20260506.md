# XAU Reclaim V3 Analytics Handoff — 2026-05-06

Agent: Hermes
Role: observability / production verification
Project/profile: Dexter Pro / deploy-xau-family-canary
Scope: evidence-only analytics for latest XAU Reclaim/Staircase V3 trading logic

## What changed

Added a read-only telemetry analytics script:

- `ops/xau_reclaim_v3_analytics.py`
- `tests/test_xau_reclaim_v3_analytics.py`

The script reads `execution_journal` rows containing `raw_scores.xau_reclaim_v3` and summarizes:

- total telemetry rows
- active reclaim rows
- possible live-effect rows
- phase/reason buckets
- confidence-bypass / winner-override / risk-multiplier flags
- sample recent rows

It does not change trading policy, execution, confidence, risk, order placement, or scheduler behavior.

## VM verification

VM: `/opt/dexter_pro`
Branch: `deploy-xau-family-canary`
Verified commit after push: `044bcc0`
Service: `dexter-monitor` active

Commands run on VM:

```bash
.venv/bin/python -m py_compile ops/xau_reclaim_v3_analytics.py tests/test_xau_reclaim_v3_analytics.py
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_xau_reclaim_v3_analytics.py tests/test_xau_reclaim_staircase.py
PYTHONPATH=. .venv/bin/python ops/xau_reclaim_v3_analytics.py --db data/ctrader_openapi.db --days 2 --samples 4
```

Result:

```text
14 passed in 0.41s
```

## Current evidence from live demo DB

For last 2 days at time of verification:

```text
rows: 12
active_rows: 0
possible_live_effect_rows: 0
active_rate: 0.0
score_max: 0.0
planned_rr_avg: 5.7711
by_source: xauusd_scheduled=12
by_status: filtered=12
by_phase: none=12
by_reason: no_reclaim_features=12
by_bypass_conf_below: False=12
by_winner_partial_override: False=12
```

Interpretation:

- Latest Reclaim V3 metadata is being written.
- It has not yet detected an active reclaim/staircase setup in the checked sample.
- No observed rows bypassed confidence, overrode winner partial logic, or changed risk sizing.
- Since no active rows exist yet, there is not enough outcome evidence to judge edge or promote/tune behavior.

## Existing runtime flags observed before this handoff

```text
XAU_RECLAIM_V3_ENABLED=True
XAU_RECLAIM_V3_SHADOW=False
XAU_RECLAIM_MIN_SCORE=62.0
XAU_IMPULSE_GUARD_ENABLED=True
FIBO_MTF_SHADOW_ENABLED=True
CTRADER_AUTOTRADE_ENABLED=True
CTRADER_ENABLED=True
```

Important: because `XAU_RECLAIM_V3_SHADOW=False`, any future active Reclaim V3 row could have live behavior impact. The new analytics script should be used to catch and review those rows quickly.

## Rollback

Ops-only rollback:

```bash
git revert 044bcc0
```

No service restart is required for this analytics script unless another runtime code change is deployed.

## Next safe action

Run the analytics after each active trading window until at least one active Reclaim V3 row appears:

```bash
cd /opt/dexter_pro
PYTHONPATH=. .venv/bin/python ops/xau_reclaim_v3_analytics.py --db data/ctrader_openapi.db --days 2 --samples 12
```

If `possible_live_effect_rows > 0`, prepare an Opus read-only review packet before changing thresholds or adding more trading behavior.
