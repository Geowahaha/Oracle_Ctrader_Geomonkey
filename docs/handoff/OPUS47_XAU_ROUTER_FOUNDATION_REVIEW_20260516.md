# Opus 4.7 Foundation Review — XAU Fake-Smart Confidence Stacking

Timestamp: 2026-05-16
Role: Claude Opus 4.7 reviewer only
Scope: feature-flag-off foundation implementation review

## Verdict

APPROVE for shipping as a foundation with `XAU_NEW_ROUTER_ENABLED=0`.

Reasons:
- Modules are pure helpers with no broker/scheduler side effects.
- `scheduler.py` does not import `route_classifier`, `confidence_composer`, or `risk_basket` yet, so no live behavior change.
- Config flag default is OFF.
- Tests passed locally: six new tests + existing blind-limit router tests = 13/13.

## Notes / non-blocking issues

1. `analysis/route_classifier.py` returns `harvest_zone_pm_only` before the RASG branch when harvest pressure and weak live execution are both true. This is correct because `leg_count <= 1`, but the reason trail could be clearer.

2. `"winner" in source` is not structural proof. Safe for foundation because weak-live-execution branches run first, but before flag-on the scheduler must not treat source naming as proof of route quality.

3. `confidence_composer` treats blank/None impulse state as missing. This is correct for safety, but before live wiring the scheduler must plumb real `impulse_state` for all relevant signals, otherwise good signals could be over-penalized.

4. `BasketRegistry` is in-memory. Before live admission, persist basket state to SQLite or `data/runtime/` so restart cannot admit canary after main is already open.

## Test contract confirmed

- Row7/row8-like loss cluster routes to `harvest_zone_pm_only` or `wait_retest_plan`, not fresh entry.
- Row2/row22/row24/row26 valid short winners remain allowed as `retest_entry` or `breakdown_continuation`.
- Main+canary duplicate same `signal_run_id` is denied in the foundation registry test.
- RASG active forces a single safer route.

## Safe VM deploy statement

Safe to place these files on VM with flag OFF because live behavior impact is 0 until scheduler wiring exists and the flag is enabled.

## Required next steps before flag ON

1. Plumb real scheduler features: `flow_confirmed`, `impulse_state`, `price_near_liquidity_target`, `equal_lows_distance_atr`, `extended_move_atr`, `rasg_active`, `correlated_component_groups`.
2. Shadow-only wiring first: log `would_route` / `would_compose`, do not override decisions.
3. Persist `BasketRegistry` state before live admission.
4. Run 14-day shadow/backtest metrics.
5. Owner approval before flipping flag gradually: canary first, then main.
6. Add integration test: flag OFF unchanged; flag ON emits shadow route/composed-confidence telemetry.
