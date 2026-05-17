# Dexter Opportunity-First Trading System Design — 2026-05-16

## Mission

Build Dexter as an opportunity hunter, not a blocker stack.

Rules:
- Do not blanket-block LONG, SHORT, family, or TF just because a recent cluster lost.
- Convert weak setups into better execution routes: wait-break stop, probe size, split entry, PM-only harvest, or evidence shadow.
- Preserve and mine winning reservoirs, especially routes that prove they can extract profit in live/demo broker truth.
- Hard blocks are only for true impossibility: broker/margin failure, stale severe data, invalid geometry that cannot be repaired, duplicate/idempotency risk, or explicit kill switch.

## Current live truth from VM DB

Time checked: 2026-05-16 01:52 UTC / 08:52 TH.

Exposure:
- Open XAU positions: none.
- Open XAU pending orders: none.
- Naked open XAU SL: none.

Today Thailand-local PnL since 2026-05-15 17:00 UTC:
- xauusd_scheduled:canary SHORT: -50.93, 1 position.
- xauusd_scheduled LONG: -2.04, 1 position.
- xauusd_scheduled SHORT: 0.00, 1 position.
- scalp_xauusd:winner SHORT: +2.33, 2 positions.

24h XAU PnL:
- scalp_xauusd:winner LONG: +165.44, 6 positions, profit reservoir.
- xauusd_scheduled:canary SHORT: -146.45, 6 positions, active bleed.
- xauusd_scheduled SHORT: -112.60, 10 positions, active bleed.
- scalp_xauusd:winner SHORT: -49.41, 12 positions, mixed but recent TH-day recovered +2.33.

7d XAU PnL:
- scalp_xauusd:winner LONG: +130.27, best reservoir.
- fibo_xauusd SHORT/LONG: negative historical leak, now contained by planner/shadow.
- scheduled SHORT/canary SHORT: current bleed to repair by routing/sizing, not direction block.

Fibo MTF:
- Planner rows active: observe_only/probe.
- micro_live=0 in scheduler telemetry.
- Prior FIBO_MTF_SHADOW live leak remains historical evidence; do not reopen Fibo live without non-shadow planner review.

## Architecture

### 1. Signal intelligence layer
Classify every candidate into a route, not allow/block:
- continuation / breakdown impulse
- retest / reclaim
- liquidity harvest zone
- range/sweep reversal
- plan-only evidence gap

Route output must include:
- selected entry type
- risk multiplier
- evidence reasons
- old blocker reason if any
- would-have-blocked flag
- PM action before entry if needed

### 2. Entry routing layer
Bad entry is not bad direction.

Live rule deployed in this patch:
- Unanchored pullback limit with no absorption/continuation/Fibo/Kronos/candle zone is no longer killed as `blocked_blind_limit`.
- It is converted to `wait_break_probe_stop`:
  - entry type: buy_stop/sell_stop
  - risk multiplier: default 0.35, compounded with sharpness caution when present
  - broker only participates if price proves continuation
  - avoids mid-air limit fills

Existing preserved rule:
- Strong immediate continuation may still use market only when structure_break is present; otherwise wait-break stop.

### 3. Position management layer
Profit must run in trend and bank in chop:
- Keep guardian/rachet protection active.
- PM should harvest weak add-ons but preserve runners.
- Existing red exposure should trigger repair/size downgrade/hedge-or-flip route, not pre-entry blanket block.

### 4. Basket/risk layer
Repeated main+canary same-thesis losses are not a side problem.
Next implementation target:
- Persist signal-run basket risk so main+canary are alternative legs inside one thesis budget.
- Preferred behavior: duplicate thesis gets probe/alternate route, not full-size duplicate.

### 5. Evidence/mining loop
Every day:
- Mine PnL by source/side/opening direction using position direction truth.
- Promote profit reservoirs via tests and live telemetry.
- Convert systematic losers into market-weakness detectors.
- Keep Fibo MTF in contained planner/probe until non-shadow source is reviewed.

## Deployed patch intent

Patch class: narrow live repair.

Before:
- unanchored pullback limit returned `blocked=True` and never participated.

After:
- unanchored pullback limit returns `blocked=False`, `entry_type=sell_stop/buy_stop`, `mode=wait_break_probe_stop`, small risk.

Why this matches mission:
- No blanket block.
- No mid-air limit.
- Market must reveal weakness/continuation before exposure.
- Risk is reduced while opportunity remains alive.

## Test evidence

Local:
- `tests/test_xau_openapi_entry_router_blind_limit.py::test_router_converts_unanchored_pullback_limit_to_probe_stop_not_block` was RED first, then GREEN.
- Focused suite: 15 passed.
- py_compile: config.py, scheduler.py, analysis route/confidence modules, risk_basket, focused tests passed.

## Next TODO

P0:
- Deploy this wait-break-probe conversion to VM, run VM tests, restart dexter-monitor, verify logs.

P1:
- Implement durable signal-run basket risk admission in scheduler/cTrader path: main+canary duplicate thesis becomes probe/alternate, not full duplicate.

P2:
- Mine winner LONG reservoir (`scalp_xauusd:winner`) for entry context and convert its winning archetype into route boost/runner-preserve logic.

P3:
- Analyze scheduled SHORT losses as possible stop-hunt/reversal detector rather than block SHORT.
