# Dexter Full Release — Tier 2 & Tier 3 Design

Status: design only — Tier 1 (#2, #3, #7) implemented this session.
Date: 2026-05-17.

This document captures the design for the next two waves of capability now that
the foundation (Self-Mutation Loop, Equity Governor, Archetype Tournament) is
in place. Each feature is described to the level of detail needed to start
implementation without further design work.

## Composition principle

Every feature in this document must be **additive** — it lays a new layer on
top of the existing trader without altering proven strategy paths. Whitelist
flag, shadow phase, canary, then promotion. The Self-Mutation Loop is the
operator of last resort: any knob a new feature introduces becomes a candidate
for autonomous tuning once shadowed for at least 14 days.

---

## Tier 2 — next 2 weeks

### #5 News-as-Edge Engine (replaces News Guard kill in narrow window)

**Today.** `notifier/macro_news.py` blanks the lane for T1 events (NFP, FOMC,
CPI) from `t-45m` to `t+30m`. The blocker is necessary because pre-news flow
is misleading, and ad-hoc news scalping has bled the account historically.

**Idea.** Replace the blanket kill with a **consensus-deviation detector**. The
deviation is real edge — if 5 minutes before T1 the price is already pricing in
a surprise direction, the lane should ride it on a probe with tight stop. If
the price is neutral, the kill stays.

**Architecture.**
```
notifier/news_edge/
  __init__.py
  calendar.py            # reads upcoming T1 events (forexfactory ingest already exists)
  consensus.py           # pulls actual vs consensus for the next event
  deviation.py           # computes "surprise direction" from pre-news price action
  router.py              # exposes pre_news_route(event) → "kill" | "probe_long" | "probe_short"
  config.py              # NEWS_EDGE_* flags (default OFF)
tests/test_news_edge_deviation.py
tests/test_news_edge_router.py
```

**Inputs.**
- Upcoming T1 event with consensus values.
- `cTrader` last 60 minutes of XAU 1-minute bars.
- Volume profile delta (already telemetered).

**Decision.**
- Compute `pre_news_drift = (m1_close[now] - m1_close[now-60m]) / atr_60m`.
- If `|drift| < 0.4` → `route = "kill"` (current behaviour).
- If `drift > 0.6` and volume confirms → `route = "probe_long"`.
- If `drift < -0.6` and volume confirms → `route = "probe_short"`.
- Probe is **always small** (risk multiplier 0.20) with `t-3m` entry, `t+5m`
  forced exit if no follow-through.

**Safety.**
- Master flag `NEWS_EDGE_ENABLED=0` default.
- Hard veto: never place an order between `t-30s` and `t+30s` (broker-level
  market chaos window).
- Daily cap: 1 probe per T1 event maximum.
- Shadow mode first — emit `would_route` for 14 days before going live.

**Self-mutation handoff.**
Add knobs to `MUTABLE_KNOBS`:
- `NEWS_EDGE_DRIFT_THRESHOLD` (band 0.40–1.20)
- `NEWS_EDGE_PROBE_RISK_MULTIPLIER` (band 0.10–0.40)
- `NEWS_EDGE_EXIT_WINDOW_MINUTES` (band 3–10)
The loop tunes these from real probe outcomes within 30 days.

---

### #6 Kronos-Primary Router (uncertainty-gated)

**Today.** Technical setup chooses the route. Kronos forecast is consumed as a
secondary anchor for limit confluence in the wait-break and limit-retest paths
shipped on 2026-05-16.

**Idea.** When `kronos.uncertainty < 0.30` AND `kronos.direction == bias`,
**Kronos becomes the primary route picker**. The technical setup becomes
the secondary confirmation. This inverts the relationship in exactly the cases
where Kronos has demonstrated high-conviction skill.

**Architecture.**
```
analysis/kronos_router/
  __init__.py
  primary.py           # decides if Kronos can take the wheel
  consensus.py         # combines Kronos + technical confidence
  config.py            # KRONOS_ROUTER_* flags
tests/test_kronos_router_primary.py
tests/test_kronos_router_consensus.py
```

**Decision logic.**
```
if not kronos.is_fresh(max_age_sec=120):
    return "technical_primary"
if kronos.uncertainty >= 0.30:
    return "technical_primary"
if kronos.direction != bias:
    return "technical_primary"  # disagreement → stick with conservative path
return "kronos_primary"  # Kronos takes the wheel
```

When Kronos is primary:
- Entry timing follows `kronos.target_zone_first_touch_estimate` (not just
  technical retest geometry).
- Stop placement follows `kronos.invalidation_atr` (not just structural lows).
- Take-profit follows `kronos.target_band_high` (Kronos's own projection).

**Safety.**
- `KRONOS_ROUTER_PRIMARY_ENABLED=0` default.
- Shadow mode emits `kronos_primary_would_have_routed` for 14 days before
  going live.
- Per-day cap: at most 30% of XAU entries can be Kronos-primary; if exceeded,
  remaining entries fall back to technical primary.
- If a Kronos-primary trade loses 3× in a 24h window, auto-disable Kronos
  primary for 24h and surface a Telegram alert.

**Self-mutation handoff.**
Add knobs:
- `KRONOS_ROUTER_MAX_UNCERTAINTY` (band 0.20–0.45)
- `KRONOS_ROUTER_DAILY_SHARE_CAP` (band 0.10–0.50)

---

### #1 Parallel Multi-Route Execution Engine

**Today.** `_xau_openapi_entry_router` returns a single route per signal
(limit / stop / market / probe). When the route loses, the signal's entire
hypothesis dies with it.

**Idea.** For high-confidence signals, **dispatch up to 3 legs in parallel
through `BasketRegistry`**, each with a different route:
- Probe (0.20× risk) at market.
- Retest limit (0.40× risk) at Fibo zone.
- Breakout stop (0.40× risk) above structure.

Each leg shares the same `signal_run_id` and basket risk budget. The first leg
to hit `R ≥ 0.8` triggers a basket-wide free-runner upgrade on the other legs
(stop to BE + small).

**Why this is bold but not reckless.**
- Total basket risk is the same as a single full-size leg — split into 0.2 +
  0.4 + 0.4 = 1.0× of baseline.
- Three different entry locations means the basket captures the *route* the
  market actually offered, not the one we guessed first.
- Existing `risk_basket.BasketRegistry` (shipped 2026-05-16 in foundation
  modules) already supports this — just needs scheduler wiring.

**Architecture.**
```
execution/multi_route/
  __init__.py
  planner.py             # turns a signal into a tuple[RoutePlan]
  dispatcher.py          # places each leg through cTrader with basket admission
  free_runner.py         # when one leg hits R, others go BE+small
  config.py              # MULTI_ROUTE_* flags (default OFF)
tests/test_multi_route_planner.py
tests/test_multi_route_dispatcher.py
tests/test_multi_route_free_runner.py
```

**Eligibility.**
Multi-route triggers only when:
- Signal `confidence >= 80`.
- `route_classifier` returns `breakdown_continuation` or `retest_entry`.
- Account free margin ≥ 3× single-leg margin.
- No active main promotion from Self-Mutation that would have lowered risk.

**Self-mutation handoff.**
Knobs:
- `MULTI_ROUTE_MIN_CONFIDENCE` (band 70–90)
- `MULTI_ROUTE_PROBE_SHARE` (band 0.10–0.40)
- `MULTI_ROUTE_RETEST_SHARE` (band 0.20–0.50)
- `MULTI_ROUTE_BREAKOUT_SHARE` (band 0.20–0.50)
The loop optimises the three shares to add up to 1.0 over 30 days.

---

### #4 Cross-Family Confluence Booster

**Today.** Conductor agents (`openclaw/`) vote independently. Each family
(`scalp_xauusd`, `xauusd_scheduled`, `fibo_xauusd`, `psc`, …) acts on its own
signal stream. There is no aggregate "system consensus" multiplier.

**Idea.** When ≥3 families agree on `(symbol, side)` within a 10-minute window,
**boost the next signal in that direction by `× 1.5–2.0`**. When families
diverge, shrink to probe size.

**Architecture.**
```
analysis/confluence/
  __init__.py
  ledger.py              # rolling family votes per (symbol, side)
  booster.py             # multiplier given current vote state
  config.py              # CONFLUENCE_* flags
tests/test_confluence_ledger.py
tests/test_confluence_booster.py
```

**Decision.**
```
votes = ledger.votes_for(symbol, window_minutes=10)
agree = max(votes.long, votes.short)
disagree = min(votes.long, votes.short)
if agree >= 3 and disagree == 0:
    multiplier = 2.0
elif agree >= 3 and disagree >= 1:
    multiplier = 1.3
elif agree == 2 and disagree == 0:
    multiplier = 1.15
else:
    multiplier = 1.0
```

**Safety.**
- Multiplier never exceeds `CONFLUENCE_MAX_MULTIPLIER` (default 2.0).
- Multiplier composes with Equity Governor: final = confluence × equity_rec,
  clamped to `[0.30, 2.0]`.
- Per-day cap: max 3 confluence-boosted entries per symbol.

**Self-mutation handoff.**
Knobs:
- `CONFLUENCE_MIN_AGREE` (band 2–4)
- `CONFLUENCE_MULTIPLIER_HIGH` (band 1.5–2.5)
- `CONFLUENCE_MULTIPLIER_MID` (band 1.10–1.40)

---

## Tier 3 — strategic

### #8 Multi-Broker Risk Hedge

**Idea.** When cTrader spread blows out (`spread_avg_pct > 0.012`) during an
open XAU position, **mirror a tiny hedge on Binance perpetual** (or MT5 if
Binance is unavailable). The hedge offsets adverse fills until cTrader spread
normalises, at which point the hedge is closed and the cTrader position
resumes its planned defence.

**Why.** Most live XAU bleed is not from bad direction — it's from broker
spread expansion during news/illiquid sessions. A 5-minute hedge buys time.

**Components.**
- `execution/binance_hedge.py` — places small ETHUSDT-perp short/long aligned
  to XAU correlation when XAU broker spread blows out.
- `execution/spread_watchdog.py` — monitors cTrader spread, fires hedge
  open/close events.
- Two-leg P&L accounting in a new `data/runtime/hedge_journal.db`.

**Risk envelope.**
- Hedge notional capped at 30% of the XAU position's risk_usd.
- Hedge auto-closes when cTrader spread falls below 0.008 for 60 seconds.
- Independent kill switch `HEDGE_ENABLED=0`.

---

### #9 Regime-Switching Capital Allocator

**Idea.** Treat the 6 strategy families as competing portfolios. Allocate the
account's total risk_per_day across families based on rolling 7-day Sharpe.
Family with high Sharpe gets a larger share; underperformers shrink.

**Components.**
- `learning/family_allocator.py` — computes per-family Sharpe and target weight.
- `data/runtime/family_allocation.json` — current per-family share, written
  by the allocator daily at 00:00 UTC.
- Scheduler reads the allocation to adjust per-family `risk_per_trade`.

**Constraints.**
- No family below 0.05 share (so they keep generating data).
- No family above 0.40 share (so the allocator can't all-in on one).
- Hard daily turnover cap to prevent flapping: allocation can shift at most
  ±0.10 per day.

---

### #10 Predictive Pre-Signal Engine

**Idea.** The system already computes Volume Profile, DOM Liquidity, and
Sharpness scores in real time. Right now they confirm setups *after* the
signal fires. Flip it: when **all three converge into a high-conviction
configuration before** the signal generator fires, pre-arm a tiny pending
order at the projected entry zone with a 90-second TTL.

**Why.** The signal generator takes ~3 seconds to confirm. By that time the
move is sometimes 0.4× ATR done. Pre-arming captures the missing ATR.

**Components.**
- `analysis/pre_signal_engine.py` — fuses VP + DOM + Sharpness into a
  conviction score every second.
- `execution/pre_arm.py` — places limit/stop with 90s TTL when conviction ≥ X.
- `analysis/pre_signal_telemetry.py` — logs every pre-arm so we can measure
  hit rate, slippage saved.

**Safety.**
- Pre-arm risk is always 0.10× of normal entry.
- Pre-arm cancels itself if the real signal generator does not fire within
  the TTL.
- Pre-arm is disabled around T1 news windows.

---

## Composition order

A bottom-up build order so each layer can rely on the one below:

1. **Tier 1** (done in this session): Self-Mutation Loop, Equity Governor,
   Archetype Tournament.
2. **Tier 2 #4** (Confluence): cheap to ship, gives every later feature a
   global system-consensus signal.
3. **Tier 2 #6** (Kronos-Primary): Kronos already used as anchor, this is the
   smallest reach.
4. **Tier 2 #1** (Multi-Route): once Confluence + Kronos exist, multi-route
   has the conviction signals it needs.
5. **Tier 2 #5** (News-as-Edge): needs Confluence to confirm consensus deviation.
6. **Tier 3 #9** (Regime Allocator): wraps Tier 1+2 with portfolio-level risk
   shaping.
7. **Tier 3 #10** (Pre-Signal Engine): adds another second of front-running edge.
8. **Tier 3 #8** (Multi-Broker Hedge): last, because it's an external
   dependency (Binance API) and the biggest blast radius.

## Self-Mutation expansion

Every Tier 2/3 feature adds knobs to `MUTABLE_KNOBS` (3–4 per feature). After
each feature ships and runs 14 days, the loop graduates from `DRY_RUN=1` to
proposing live mutations against its own knobs. After 30 days, all Tier 2
knobs are under autonomous management.

The endgame: the operator only writes config when introducing a brand-new
knob. Everything else, the system tunes itself.
