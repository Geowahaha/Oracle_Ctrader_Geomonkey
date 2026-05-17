# Dexter Full Release — Shipped 2026-05-17

Branch: `claude/vibrant-lamarr-65d661`
Final commit: `d14a5ec`
Tests: **100 / 100** green across all new modules.

All feature flags ship **OFF by default**. Each module is additive — proven
strategy paths are untouched. Operator turns features on in a staged rollout.

## What landed

| # | Module | Files | Tests |
|---|---|---|---|
| **Tier 1 — Foundation** | | | |
| #7 | Self-Mutation Loop | `learning/self_mutation/` (15 modules) | 42 + 9 |
| #3 | Equity Governor | `learning/equity_governor.py` | 4 |
| #2 | Archetype Tournament | `learning/archetype_tournament.py` | 4 |
| **Tier 1 — Activation wiring** | | | |
| | KnobResolver | `learning/self_mutation/resolver.py` | 5 |
| | Tick runner CLI | `learning/self_mutation/run_tick.py` | (smoke) |
| | Telegram notifier | `learning/self_mutation/telegram_notifier.py` | 4 |
| **Tier 2 — Opportunity layers** | | | |
| #4 | Confluence Booster | `analysis/confluence/` | 8 |
| #6 | Kronos-Primary Router | `analysis/kronos_router/` | 8 |
| #5 | News-as-Edge Engine | `analysis/news_edge/` | 10 |
| #1 | Multi-Route Engine | `execution/multi_route/` | 11 |
| **Tier 3 — Capital + Predictive** | | | |
| #9 | Family Allocator | `learning/family_allocator.py` | 5 |
| #10 | Pre-Signal Engine | `analysis/pre_signal_engine/` | 7 |

*Tier 3 #8 (Multi-Broker Hedge) deferred — requires external Binance API.*

## Commit log (this session)

```
d14a5ec  feat(tier3): regime-switching family allocator + predictive pre-signal engine
2b5fc48  feat(multi_route): parallel 3-leg execution + free-runner upgrade engine
ec3a736  feat(news_edge): flip blanket news kill into narrow consensus-deviation probe
b625090  feat(kronos_router): primary routing when forecast confidence is high
994a7e7  feat(confluence): cross-family booster ledger + multiplier engine
21fbb71  feat(self_mutation): activation wiring — resolver, tick CLI, telegram
5e03472  feat(learning): self-mutation loop + equity governor + archetype tournament
```

## How the organism composes

```
                              ┌─────────────────────────┐
                              │  Pre-Signal Engine      │
                              │  (1Hz conviction →      │
                              │  pre-arm, 90s TTL)      │
                              └────────────┬────────────┘
                                           │ pre-arm or wait
                                           ▼
┌──────────────────┐  vote  ┌──────────────────────────┐  bias × multiplier
│ Conductor agents │ ─────► │ Confluence Booster       │ ──────────────────┐
│ + each family    │        │ (>=3 agree → 2.0x)       │                   │
└──────────────────┘        └──────────────────────────┘                   │
                                                                            ▼
┌──────────────────┐  forecast  ┌────────────────────┐  primary plan  ┌─────────────────┐
│  Kronos          │ ─────────► │ Kronos Router      │ ────────────► │  Multi-Route    │
│  (uncertainty)   │            │ (low u + aligned)  │                │  Planner        │
└──────────────────┘            └────────────────────┘                │  (3-leg basket) │
                                                                       └────────┬────────┘
┌──────────────────┐  T1 event   ┌────────────────────┐                         │
│ macro_news       │ ──────────► │ News-as-Edge       │ ─── kill/probe ─────────┤
│ calendar         │             │ Router             │                         │
└──────────────────┘             └────────────────────┘                         ▼
                                                                       ┌──────────────────┐
┌──────────────────┐  rolling Sharpe  ┌────────────────────┐  share   │  cTrader / MT5   │
│ execution_journal│ ───────────────► │ Family Allocator   │ ───────► │  + BasketRegistry│
└──────────────────┘                  │ (water-filled)     │           │  + free-runner   │
        │  closed PnL                 └────────────────────┘           └────────┬─────────┘
        │                                                                       │ closed PnL
        ▼                                                                       │
┌──────────────────┐ multiplier hint ┌──────────────────┐                        │
│ Archetype        │ ──────────────► │ Equity Governor  │ ─── x risk ────────────┘
│ Tournament       │                 │ (24h / 7d slope) │
└──────────────────┘                 └──────────────────┘
        ▲                                     ▲
        │                                     │
        │              loss > $20             │
        └─────────────┬───────────────────────┘
                      │
                      ▼
              ┌──────────────────┐
              │ Self-Mutation    │   sample 3 mutations → counterfactual →
              │ Governor.tick()  │   verdict → canary 24h → main → 7d rollback
              └──────────────────┘
```

## Rollout sequence (recommended)

**Week 1 — observe**
- `SELF_MUTATION_ENABLED=1 SELF_MUTATION_DRY_RUN=1` — loop runs but only writes ledger; no overrides applied.
- `EQUITY_GOVERNOR_ENABLED=1` and `ARCHETYPE_TOURNAMENT_ENABLED=1` in shadow (read-only — caller logs hint, does not apply).
- All Tier 2/3 flags remain OFF.

**Week 2 — activate Tier 1**
- `SELF_MUTATION_DRY_RUN=0` → canary overrides become real (trading code needs to opt in by calling `knob_resolver.get(...)` on whitelisted knobs).
- `CONFLUENCE_BOOSTER_ENABLED=1` first (smallest blast radius).

**Week 3 — activate Tier 2**
- `KRONOS_ROUTER_ENABLED=1` (with `DAILY_SHARE_CAP=0.10` initially).
- `NEWS_EDGE_ENABLED=1` for one T1 event family at a time.

**Week 4 — activate Multi-Route + Tier 3**
- `MULTI_ROUTE_ENABLED=1` for confidence ≥ 85 only.
- `FAMILY_ALLOCATOR_ENABLED=1` with shadow reads first.
- `PRE_SIGNAL_ENGINE_ENABLED=1` last.

Every step gated by an operator review of the ledger + Telegram log.

## Required integration points

The modules above are pure platform layers. To wire them into live trading, the
scheduler and executor need three small changes (NOT done in this session — they
require XAU-lane test plans per the project's "test lane first" rule):

1. **Risk-resolver call** at every dispatch:
   ```python
   from learning.self_mutation.resolver import get_default_resolver
   risk_mult = get_default_resolver().get("XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER")
   ```
2. **Governor tick** every 15 minutes, either inline in `scheduler.py` or via cron:
   ```bash
   */15 * * * * cd /opt/dexter && python -m learning.self_mutation.run_tick
   ```
3. **Confluence vote** in each family's signal emitter:
   ```python
   confluence_ledger.record_vote(family="scalp_xauusd", symbol="XAUUSD", side=side)
   ```

## Out of scope (next session)

- Wire knob_resolver into `scheduler.py` and `ctrader_executor.py` on the XAU lane only.
- Schedule `Governor.tick()` from `scheduler.py` heartbeat.
- Wire Telegram bot's `send_admin_message` to the production notifier path.
- Add Confluence votes from each family's signal entry path.
- Backtest baseline → canary diff over the last 30 days of journal data to
  calibrate `MIN_PNL_DELTA`.
- Tier 3 #8 (Multi-Broker Hedge) — Binance perp SDK + spread watchdog.

## Memory of decisions (for future sessions)

- **All flags OFF by default.** Operator-driven activation.
- **Whitelist + sanity bands** for Self-Mutation — only safe knobs are mutable.
- **Counterfactual replay** instead of real backtester — exact for risk/threshold knobs, marks others as `needs_pts`.
- **Water-filling** for floor/ceiling + daily-shift in Family Allocator — handles infeasibility gracefully.
- **Misaligned components count as zero** in Pre-Signal Engine — cannot lift conviction against bias.
- **Same-`signal_run_id` legs share thesis budget** in Multi-Route (via existing BasketRegistry from 2026-05-16).
- **Kronos primary only when uncertainty < 0.30 AND aligned with bias** — narrow, well-defined window.
- **Pre-news probe only in (-15m, -3m) opportunity window** — legacy kill behaviour preserved when engine disabled.
