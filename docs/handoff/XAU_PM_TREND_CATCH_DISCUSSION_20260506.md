# XAU Advanced Position Management / Trend Catching Discussion — 2026-05-06

Status: discussion-only, no coding approval yet.
Branch/VM evidence at review time: deploy-xau-family-canary, VM/origin HEAD `80c96c1`, `dexter-monitor` active, `XAU_OPPORTUNITY_FIRST_LIVE_UNLOCK_ENABLED=True`.

## User Direction

- Preserve the profitable state after the XAU opportunity-first unlock.
- Do not reintroduce entry blockers or shadow-only behavior for opportunities.
- Discuss with Opus 4.7, GPT-5.5/Hermes, and user before coding.
- Next target: advanced position management and trend catching.
- Specific pain: possible blind SELL/noise while broader XAU trend is up, almost hitting SL; PM/manager must monitor live data, protect profit before spike/reversal, and manage risk intelligently.

## Read-only Evidence Since Unlock

Window: UTC >= `2026-05-06 04:49:30`.

Runtime:
- VM HEAD = origin = `80c96c1`
- service active
- unlock flag true

Execution journal highlights:
- `fibo_xauusd` long accepted: 54 rows, avg confidence 32.39
- `fibo_xauusd` short accepted: 23 rows, avg confidence 22.58
- `scalp_xauusd:winner` long closed: 9 rows, avg confidence 68.99
- `xauusd_scheduled` short accepted: 1 row
- Recent accepted rows include `xau_opportunity_first_live_unlock` and bypass metadata.

cTrader deals since unlock:
- Total deal rows: 137
- Net PnL: `+196.01 USD`
- Long PnL: `-37.53 USD`
- Short PnL: `+233.54 USD`
- Scalp PnL: `+47.51 USD`
- Fibo PnL: `+145.92 USD`
- `fibo_xauusd` short winners: n=9, `+209.08`, avg `+23.23`, max `+67.39`
- `fibo_xauusd` short losses: n=9, `-23.05`, avg `-2.56`
- `scalp_xauusd:winner` short winners: n=7, `+63.03`; losses n=2, `-9.76`
- `fibo_xauusd` long winners: n=9, `+18.36`; losses n=18, `-58.47`

Current DB open-position snapshot:
- DB `is_open=1` XAU positions were long-only at the captured snapshot.
- No current open XAU orders were returned by `is_open=1` order query.
- User visually reports a blind SELL near SL. Treat this as a PM monitoring/source-of-truth gap until broker-live sync proves otherwise.

## Profitable Logic To Preserve

1. Keep opportunity-first entry policy:
   - do not revert `XAU_OPPORTUNITY_FIRST_LIVE_UNLOCK_ENABLED`
   - do not re-add pre-dispatch hard blocks for XAU opportunities
   - scanner opportunity should still reach cTrader; intelligence shifts to post-fill PM

2. Preserve strong post-unlock short families:
   - `fibo_xauusd` shorts are net strongly profitable in the observed window
   - `scalp_xauusd:winner` shorts are also profitable
   - Do not blindly kill every SELL; kill only blind/counter-trend/noise SELLs using PM state.

3. Preserve trend-run potential:
   - avoid fast TP-only logic that clips large winners
   - use stateful protection/trailing after position is in profit

## Opus 4.7 Reviewer Summary

Opus read-only review noted:
- Aggregate evidence does not support killing all SELLs. Short PnL is the main profit driver in the observed window.
- The true statistical leak in this slice is `fibo_xauusd` long: more losses and net negative.
- User-reported blind SELL is likely either a single outlier or a monitoring gap unless broker-live state proves a pattern.
- The correct design is not to reverse unlock; move intelligence to post-fill PM.

Opus recommended three layers:

### Layer A — Reliable Open-Position Discovery

- PM must read broker live open positions + pending orders continuously, not rely only on SQLite `is_open` snapshot.
- Reconcile broker open positions against DB state.
- Alert/log drift when broker says open but DB does not, or DB says open but broker does not.

### Layer B — Trend Context Overlay

For every live position calculate:
- `position_alignment`: aligned / neutral / counter-trend
- `trend_strength`: 0-100
- `time_in_trade`
- `mfe`, `mae`
- `current_unrealized_R`
- market spike/reversal warning features from tick/depth/delta if available

A blind SELL candidate can be defined as:
- direction short
- trend_strength > 70 bullish/uptrend
- counter-trend tag
- adverse drift or MAE > 0.4–0.5R
- time in trade > 15 minutes, unless it is a validated Fibo reversal/short scalp with strong favorable MFE

### Layer C — Adaptive PM State Machine

Post-fill states only:

```text
NEW -> ARMED -> RUNNING -> PROTECT -> TRAIL -> EXIT
                    |
                    -> DEFEND
```

State intent:
- NEW/ARMED: first 1-3 minutes, avoid whipsaw overreaction
- RUNNING: no premature cut while PnL < ~0.8R
- PROTECT: when unrealized_R >= ~0.8, move SL toward BE+small buffer
- TRAIL: when unrealized_R >= ~1.5 and trend remains aligned, trail behind market and let winner run
- DEFEND: when position becomes counter-trend with adverse drift, reduce risk by partial close or tightened SL
- EXIT_SPIKE: if profit exists and microstructure warns of spike reversal, close or sharply tighten to protect captured profit

## Hermes / GPT-5.5 Architecture Position

Hermes agrees with Opus on the boundary:
- Do not code yet without user approval.
- First implementation should be observability + simulation, not immediate irreversible closes.
- The only safe immediate code design would be PM shadow decisions with broker-vs-DB reconciliation.

Hermes proposed P0/P1/P2 sequencing:

### P0 — PM Source of Truth / Drift Monitor

Build a read-only broker-live position monitor:
- broker open positions
- broker pending orders
- DB open positions/orders
- mismatch/drift report
- per-position current distance to SL/TP
- estimated unrealized PnL/R using latest tick

Output:
- `data/reports/xau_pm_position_snapshot.json`
- journal log `[XAU_PM_MONITOR]`
- alert if blind SELL / stale DB / near-SL position exists

No trading actions.

### P1 — PM Shadow Decision Engine

For each open XAU position, produce would-action only:
- `would_move_sl_to_be`
- `would_trail_sl`
- `would_partial_close_defend`
- `would_full_close_spike`
- `would_cancel_blind_sell_order`

Output:
- `data/reports/xau_pm_shadow_decisions.json`
- execution_journal metadata enrichment if safe

No live close/amend yet.

### P2 — User-approved Live PM Actions

Enable actions one by one behind separate flags:
1. `XAU_PM_LIVE_BE_PROTECT_ENABLED`
2. `XAU_PM_LIVE_TRAIL_ENABLED`
3. `XAU_PM_LIVE_PARTIAL_CLOSE_DEFEND_ENABLED`
4. `XAU_PM_LIVE_SPIKE_EXIT_ENABLED`
5. `XAU_PM_LIVE_CANCEL_BLIND_SELL_ORDERS_ENABLED`

Suggested order:
- first allow reversible SL amendments / BE protection
- then trailing
- then partial close
- full close and hedge last

## Discussion Questions For User

1. Do we agree not to kill all SELLs, only blind/noise/counter-trend SELLs?
2. Should P0 broker-live PM monitor be implemented first, read-only, to prove the blind SELL/state drift?
3. For live phase, which action is acceptable first:
   - BE+SL protect only
   - trailing SL only
   - partial close 50% for counter-trend/noise
   - cancel blind pending SELL orders
4. Should `fibo_xauusd` long leakage be handled by PM only first, not entry gating?
5. What is the user’s preferred first live permission: protect profits, kill blind SELL, or trail runners?

## No-Code Verdict

- The latest unlock is profitable and should be preserved.
- The incredible profit is visible in post-unlock realized deal evidence: net `+196.01 USD`, led by XAU short families.
- The next improvement should not be an entry blocker. It should be a live-position manager that sees broker truth, labels trend alignment, protects open profit, and cuts/defends blind noise.
- Coding should wait until user confirms the first PM phase/action.
