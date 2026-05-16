# XAU Route Classifier / Confidence Composer Spec v1

Authoring context: Opus 4.7 reviewer verdict translated into an implementation spec by Hermes.
Default rollout requirement: feature-flagged OFF (`XAU_NEW_ROUTER_ENABLED=0`) until fixtures/tests/owner approval pass.

## Principle

Bias chooses side. Live location chooses route. `signal_run_id` chooses basket risk.

This is not a short blocker. The system must preserve valid XAU shorts while preventing fake-smart narrative confidence, harvest-zone chase entries, and duplicate main+canary same-thesis exposure.

## Dataclasses

### RouteDecision

Fields:
- `route: str` — one of `retest_entry`, `breakdown_continuation`, `harvest_zone_pm_only`, `wait_retest_plan`.
- `allowed_entry_types: tuple[str, ...]`
- `leg_count: int`
- `max_legs: int`
- `action: str` — `allow_entry`, `pm_only`, or `plan_only`.
- `size_mult: float`
- `invalidation_policy: str`
- `reasons: tuple[str, ...]`

### ComposedConfidence

Fields:
- `final_confidence: float`
- `base_confidence: float`
- `additive_total: float`
- `penalty_total: float`
- `correlation_penalty: float`
- `missing_evidence_penalty: float`
- `proximity_penalty: float`
- `soft_cap_applied: bool`
- `passed_execution_gate: bool`
- `reasons: tuple[str, ...]`

### AdmissionDecision

Fields:
- `admitted: bool`
- `signal_run_id: str`
- `requested_risk_usd: float`
- `open_risk_usd: float`
- `basket_risk_budget: float`
- `leg_role: str`
- `reason: str`

## Initial penalty coefficients

- Correlated narrative stack penalty: 1.5 to 4.0 depending on count of correlated `htf/smc/ob_fvg/session/historical` components.
- Missing flow penalty: 3.0.
- Missing impulse penalty: 3.0.
- Missing both flow and impulse: at least 6.0 and `passed_execution_gate=False` unless route is `retest_entry` with structural rejection.
- Proximity-to-target penalty: 2.0 to 4.0 when price is near equal lows/highs/support/TP liquidity.
- RASG active: not merely a penalty. It must override route/leg count: `leg_count <= 1`, no-add, safer route.

## State route table

| State | Conditions | Route decision |
| --- | --- | --- |
| Retest entry | Price is in/near structural retest zone and rejection evidence exists | `retest_entry`, limit-only, max 1 leg |
| Breakdown continuation | Fresh break with flow + impulse aligned and not near target | `breakdown_continuation`, stop/market allowed, basket risk enforced |
| Harvest zone | Price is already near equal lows/highs, support/resistance, TP liquidity, or move is extended; especially with missing impulse/flow | `harvest_zone_pm_only`, no new entry, PM trail/partial/protect only |
| Wait retest | Directional bias exists but live execution evidence is insufficient | `wait_retest_plan`, no lane dispatch |

## Basket risk rule

One `signal_run_id` / thesis owns one basket risk budget. Main and canary are alternatives or conditional follow-ons, not additive full-risk orders.

Admission rules:
- If another leg is open for the same `signal_run_id` and `no_add=True`, deny.
- If `open_risk_usd + requested_risk_usd > basket_risk_budget`, deny.
- If `leg_role=canary` after main fill, admit only when explicitly classified as conditional profitable retest; otherwise deny.

## Required tests

1. `test_route_classifier_harvest_zone.py`: row7/row8-like features route to `harvest_zone_pm_only` or `wait_retest_plan`.
2. `test_route_classifier_valid_short.py`: row2/row22/row24/row26-like profitable short features are not blocked.
3. `test_confidence_composer_correlation_penalty.py`: correlated narrative stack with missing flow must not pass execution gate.
4. `test_confidence_composer_missing_evidence.py`: impulse idle + delta weak activates missing-evidence penalty.
5. `test_basket_registry_no_double_fill.py`: main open for one `signal_run_id` denies canary duplicate.
6. `test_rasg_actuator.py`: RASG active forces safer route and `leg_count=1`.

## Verification metrics

- Same `signal_run_id` two fills within 30s: 0.
- Valid short pass rate: >=85% of baseline; hard fail if total short count drops >25% without explicit owner approval.
- `harvest_zone_pm_only` live fills: 0.
- `confidence > 80` with `flow_confirmed=False`: reduce >=70%.
- Worst-two-trade loss cluster: reduce >=40% vs 14-day baseline.
- RASG active observed leg count: <=1.
- Valid short winner analogs pass: >=90%.
