# Winner Archetype Mining Plan

Date UTC: 2026-05-12
Project: Dexter Pro
Scope: Use historical good orders to avoid wasting future Fibo MTF/probe collection time.

## Why This Exists

The Fibo MTF evidence effort should not only wait for future shadow/probe rows. Dexter already has historical closed orders. The best use of those rows is to mine winning archetypes and compare new shadow candidates against them.

This plan is read-only first. It must not change live execution.

## Current Read-Only Snapshot From VM

Database: `/opt/dexter_pro/data/ctrader_openapi.db`

XAU position-level aggregation using `ctrader_positions.direction` as direction truth and summing `ctrader_deals.pnl_usd` by `position_id`:

- positions: 1139
- winners: 444
- losers: 695
- gross_win_usd: 3092.54
- gross_loss_usd: -3608.76
- net_pnl_usd: -516.22
- first_seen: 2026-03-22T22:23:24Z
- latest_deal: 2026-05-12T17:49:08Z
- long positions: 851, winners 316, total_pnl_usd -383.89
- short positions: 288, winners 128, total_pnl_usd -132.33

Top winner examples include several `fibo_xauusd` long winners around 2026-04-08 to 2026-05-07 and `xauusd_scheduled`/`scalp_xauusd` winners.

## Important Data Rule

Do not use `ctrader_deals.direction` alone for direction truth. Use `ctrader_positions.direction` or statement opening direction. This prevents close-deal direction inversion errors.

## Proposed Winner Buckets

1. Big clean winners
- Top 10–20% by pnl_usd or R multiple.
- Low MAE if reconstructable.
- Good for identifying ideal entry conditions and profit-run behavior.

2. Ugly winners
- Positive PnL but large MAE.
- Good for detecting trades that should maybe be smaller or protected differently.

3. Fast winners
- Short time from first_seen to close_utc.
- Good for session/timeframe filters and quick target behavior.

4. Runner winners
- High MFE or long duration, if candles/ticks can reconstruct.
- Good for trend-following and partial/runner logic.

5. False-good winners
- Won money but bad R:R, huge drawdown, or lucky same-bar ambiguity.
- Should not be copied into strategy rules.

## Features To Extract Per Position

From DB directly:
- position_id
- symbol / broker_symbol
- source / lane / label / comment
- position direction
- entry_price
- stop_loss
- take_profit
- first_seen_utc
- close_utc
- pnl_usd
- deal_count
- volume

Derived:
- initial_risk_price = abs(entry_price - stop_loss)
- pnl_R approximation if volume/point value can be normalized safely
- duration_minutes
- target_distance_R
- session bucket: Asia/London/NY/overlap
- family: fibo_xauusd / xauusd_scheduled / scalp_xauusd / etc.

If candle/tick data supports it:
- MAE_R
- MFE_R
- max favorable before first PM action
- retracement before TP
- whether TP/SL same-bar ambiguous

## Comparison Against Fibo MTF Shadow

For every new Fibo MTF shadow row, report should answer:

- Does this candidate resemble known good archetypes?
- If yes, which archetype and with what similarity score?
- If no, why is it different?
- Does its projected MTF anchor improve or worsen the historical winner archetype behavior?

This avoids waiting days for abstract counts and makes evidence interpretable.

## Minimal Read-Only Implementation

Add an ops report, no live behavior:

`ops/xau_winner_archetype_report.py`

Inputs:
- `--db data/ctrader_openapi.db`
- `--symbol XAUUSD`
- `--min-pnl-usd 0`
- `--json`

Outputs:
- position-level winner/loser summary
- top winner table
- by-family performance
- by-session performance
- winner archetype clusters using simple deterministic buckets first
- optional CSV/JSON artifact under `reports/`

## Gates For Usefulness

After report exists, use these questions:

1. Are current Fibo MTF shadow candidates similar to historical winners?
2. Are they similar to historical losers?
3. Which source/family created the best winner archetypes?
4. Did the best winners have low MAE, or did they survive ugly drawdowns?
5. Would MTF anchor have improved TP/hold behavior on those winners?

## No-Live Boundary

This is analysis only.
Do not use archetype score to route live orders until a separate review and Opus approval.
Do not auto-promote based on historical fit alone.
Historical winners can guide hypotheses; forward shadow/probe validation is still required.
