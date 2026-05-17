# Corrected XAU Post-Unlock Statement Reanalysis — 2026-05-06

Status: discussion-only / no code change.
Source of truth for realized PnL: user-provided cTrader statement export:
`C:\Users\mrgeo\Downloads\cT_9900897_2026-05-06_18-33.htm`
WSL path parsed: `/mnt/c/Users/mrgeo/Downloads/cT_9900897_2026-05-06_18-33.htm`

## Correction

Previous DB-only interpretation was wrong for realized direction.

Root cause:
- `ctrader_deals.direction` can represent deal/closing side, not the original opening position direction.
- Example from DB sanity check:
  - cTrader statement: Opening direction `Buy`, entry `4647.51`, close `4715.18`, PnL `+67.39`.
  - DB `ctrader_deals`: same PnL/position but `deal_direction='short'`, while joined `ctrader_positions.direction='long'`.
- Therefore realized direction analysis must use cTrader statement `Opening direction`, or DB joined `ctrader_positions.direction`, not `ctrader_deals.direction` alone.

## Statement Summary

Statement summary table:
- Realised P&L: `224.85 USD`
- Unr. P&L: `26.16 USD`
- Equity: `1,238.09 USD`
- Balance: `1,211.93 USD`

Parsed history rows: `69`
Close-time range: `06/05/2026 11:53:24.265` to `06/05/2026 18:19:47.221` UTC+7.

## Correct Realized PnL by Opening Direction

Opening `Buy` / LONG:
- trades: `40`
- net PnL: `+254.74 USD`
- wins: `22`
- losses: `18`
- win rate: `55.0%`
- avg per trade: `+6.37 USD`
- max win: `+67.39`
- max loss: `-7.79`

Opening `Sell` / SHORT:
- trades: `29`
- net PnL: `-29.89 USD`
- wins: `11`
- losses: `18`
- win rate: `37.9%`
- avg per trade: `-1.03 USD`
- max win: `+7.64`
- max loss: `-7.73`

Correct conclusion:
- LONG/Buy is the main profit driver.
- SELL/Short is net negative in the statement window.
- The prior statement that short was the main profit driver is invalid because it used DB deal direction incorrectly.

## Top Profit Trades from Statement

All largest wins are opening Buy/LONG:

1. Buy `4647.51` -> `4715.18`, `+67.39`
2. Buy `4668.81` -> `4721.00`, `+51.91`
3. Buy `4650.91` -> `4696.92`, `+45.73`
4. Buy `4675.83` -> `4701.39`, `+25.28`
5. Buy `4678.89` -> `4693.32`, `+14.15`
6. Buy `4701.13` -> `4714.15`, `+12.74`
7. Buy `4665.01` -> `4677.93`, `+12.64`
8. Buy `4700.11` -> `4709.90`, `+9.51`

## Open Positions in Statement

Open positions at statement time:
- 11 Buy positions, combined unrealized `+27.96 USD`
- 1 Sell position, unrealized `-1.80 USD`
- Statement total unrealized `+26.16 USD`

The blind/noise SELL reported by user is present in statement:
- XAUUSD Sell
- entry `4709.00`
- TP `2663.43`
- SL `5670.62`
- created `06/05/2026 18:32:40` UTC+7
- unrealized `-1.80 USD`

This SELL is suspicious/noise because:
- it is the only open Sell while the open book is strongly Buy-heavy;
- TP/SL are extremely far and likely not intelligent for the current uptrend context;
- it appears after profitable Buy trend sequence;
- Sell side in realized statement is net negative.

## Updated Trading/PM Interpretation

Preserve:
- opportunity-first XAU entry unlock;
- LONG/Buy trend-catching logic;
- Fibo/scalp long opportunities that caught the main uptrend;
- no return to broad entry blocking.

Improve:
- SELL/noise suppression after entry, especially open short positions against strong uptrend;
- broker-live monitoring for open positions and pending orders;
- PM state machine to close/defend blind counter-trend Sell;
- profit protection and trailing for Long trend runners.

## Revised PM Plan

### P0 — Fix analytics/source-of-truth first

- For realized direction, never group by `ctrader_deals.direction` alone.
- Use cTrader statement `Opening direction`, or join `ctrader_deals.position_id -> ctrader_positions.direction`.
- If `ctrader_positions` row is missing, parse direction from statement or broker live position state.
- Add analysis convention: `deal_direction` != `opening_direction`.

### P1 — Broker-live PM monitor

Must monitor cTrader live state directly:
- open positions
- pending orders
- direction
- entry/SL/TP
- unrealized PnL
- distance to SL/TP
- source/comment/label
- trend alignment

### P2 — Blind SELL detection

A blind SELL candidate:
- open direction Sell/Short;
- market trend bullish/uptrend;
- Buy book and recent realized edge dominate;
- Sell unrealized goes negative or adverse drift increases;
- no clear reversal/reclaim/fibo reason;
- absurd/far TP/SL or stale risk parameters.

Proposed would-actions first:
- would_close_blind_sell
- would_cancel_blind_sell_pending_order
- would_tighten_sell_sl
- would_block_new_sell_noise_only if user later authorizes entry-side sell-noise suppression

### P3 — Long trend-runner PM

For profitable Long positions:
- protect BE+buffer after >=0.8R;
- trail after >=1.5R while trend remains aligned;
- do not TP too early on strong uptrend;
- scale out only on spike-reversal evidence, not random chop.

## Next Discussion Question

Should first implementation be:
1. read-only statement/broker-live corrected analytics + PM shadow monitor, or
2. immediate live PM permission to close/cancel blind SELL positions only?

No coding has been done in this correction pass.
