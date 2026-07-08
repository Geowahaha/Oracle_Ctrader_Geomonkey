# DEXTER3 — M5 Hunter Blueprint (Fable = brain/PM)

Owner goal (2026-07-05): a leader-market intraday engine on the cTrader MCP/OpenAPI hybrid that
**evaluates every M5 close without fail**, hunts entries with full-dimension reasoning (math/stat/
probability + external context, no legacy indicators), and when a position goes wrong **repairs the
position set so the basket aggregate resolves in profit, then closes all**. Focus now: XAUUSD + BTCUSD.
Extensible to futures/indices/stocks later.

## Honest engineering translation

- "ปิดโอกาสการสูญเสียทั้งหมด" cannot be a literal guarantee (no such system exists). The enforceable
  version: **participation-first M5 evaluation** (never skip a decision), **exact invalidation on every
  entry**, and a **repair engine with hard caps** that converts many would-be full-SL losses into
  basket-level resolutions — while capping the tail so one trending day can never blow the account.
- Repair without caps = martingale = eventual ruin (readiness plan explicitly rejects it). Repair WITH
  structure evidence + hard caps = legitimate basket management. We build the second one.

## Non-negotiables (constitution)

1. **Additive only.** Never modify the live loops (`scripts/xau_scalp_monitor.py`,
   `scripts/btc_scalp_monitor.py`), `scheduler.py`, `execution/`, `api/` behavior. Dexter3 is a new
   standalone package `dexter3/`.
2. **Label isolation.** All Dexter3 orders (when live) carry label prefix `dexter3:fable:m5h-v1`.
   Loops ignore foreign labels (per AGENTS.md). Own lock: `data/runtime/dexter3_loop.lock`.
3. **Demo 9922808** until readiness gates pass (`docs/XAU_MCP_REAL_MONEY_READINESS_PLAN.md`).
4. **M5 close-only decisions.** One decision record per symbol per M5 close — including SKIP records
   with full reasoning. Missing an M5 evaluation is a bug.
5. **Broker re-read after every mutation** (MCP post-order validation loop). No assumed fills.
6. **Every decision journaled** to `data/runtime/dexter3_journal.db` — the self-learning substrate.
7. Local MCP: `http://127.0.0.1:9876/mcp/` (string symbol names, ISO-8601 `Z`). Zombie 404 → follow
   `docs/MCP_ZOMBIE_RECOVERY_RUNBOOK.md` / `scripts/ctrader_mcp_watchdog.py`, never busy-loop.

## Architecture (package `dexter3/`)

| Module | Role |
|--------|------|
| `mcp_client.py` | Thin HTTP client for local MCP (port proven patterns from `scripts/xau_scalp_monitor.py`). Reads: trendbars M5/M15/H1, spot, positions, balance. Writes (Phase 2+): market/limit/stop orders, amend, close. |
| `market_lens.py` | Leader tools — pure math/stat features per M5 close: swing structure (HH/HL/LH/LL), liquidity sweep + reclaim, displacement bars, compression→release, close-location pressure, day-range position (Dragon shelves), true-range quantile volatility, session context (Asia/London/NY). No classic indicators. |
| `hunter_brain.py` | Decision engine. Candidate setups: Dragon upper-shelf rejection short, lower-shelf reclaim long, leader continuation, sweep-reclaim reversal. Combines leader score + empirical probability (journal stats by setup/session/regime) → decision contract. Participation-first: skip needs a stated reason, not vague fear. |
| `basket_manager.py` | Position-set state machine: `FLAT → OPEN → (structure break) REPAIR → RESOLVE`. Repair options: hedge-lock, scaled re-entry at better level, opposite-capture — chosen by structure evidence, never blind averaging. **Close-all when basket aggregate ≥ min-profit target, or when a hard cap fires.** |
| `decision_journal.py` | SQLite journal: decisions (incl. skips), features snapshot, basket transitions, would-have outcomes for skipped entries (filled in later by evaluator). |
| `shadow_runner.py` | The loop: detect each M5 close per symbol → lens → brain → basket sim → journal → one-line log. Phase 1 = NO ORDERS. |

## Decision contract (every M5 close, per symbol)

```json
{
  "ts_close": "2026-07-05T09:35:00Z", "symbol": "XAUUSD",
  "action": "enter|skip|manage", "side": "buy|sell|null",
  "entry_type": "market|limit|stop", "entry": 0.0, "sl": 0.0, "tp": 0.0,
  "size_class": "small|normal", "leader_score": 0.0, "p_win_est": 0.0,
  "setup": "dragon_shelf_short|shelf_reclaim_long|leader_continuation|sweep_reclaim|none",
  "reasons": ["…Thai/English…"], "features": { "…full lens snapshot…" }
}
```

## Basket repair — HARD CAPS (may never be exceeded; unit-tested)

| Cap | Value (Phase 1 defaults) |
|-----|--------------------------|
| Max legs per basket (incl. original) | 3 |
| Max basket risk (worst-case USD, all legs to stop) | 3.0 × base per-trade risk |
| Basket time stop | 180 min → force resolve at best available |
| Daily basket loss stop | 2 resolved-loss baskets → done for the day |
| Repair trigger | structure break WITH evidence (level lost + M5 close beyond), never price-distance alone |
| Resolve-in-profit target | basket aggregate ≥ +0.2R of base risk → CLOSE ALL |

## HUNT MODE — participation contract v2 (owner directive 2026-07-05)

Owner's challenge: entry EVERY M5 close, not evaluate-and-mostly-skip. Sniper-style setup gating
(v1) is retired as the primary path; intelligence moves into direction/size/geometry and the basket
engine. On every M5 close:

- **No lane basket open** → the direction committee (8 weighted math/stat votes: CLP, swing
  structure, day-range tilt, displacement, compression release, M15 OLS drift, sweep-reclaim
  override, H1 context) ALWAYS chooses a side + conviction; entry fires at market with structural
  SL (≥ max(6×spread, TR_q50)) and TP (RR ≥ 1.2 AND ≥ 8×spread — cost guard). Conviction shapes
  size (scout/small), never participation.
- **Lane basket open** → the M5 close routes to basket management on REAL broker PnL: hold /
  same-side repair at better price (structure swept) / hedge-lock (level truly lost) /
  **close-all-in-profit** at aggregate ≥ +0.2R / cap-stop. Campaign management IS that bar's action.
- The ONLY permitted skips (hard vetoes, journaled): insufficient bars, invalid quote,
  spread blowout, daily basket-loss cap, MCP unverified. "No setup" is no longer a reason to sit out.
- Hard caps unchanged and unbreachable (3 legs, 3× base risk, 180 min, 2 basket losses/day).
  At $0.50 base risk the worst day is bounded ≈ $3 — the price of a full day of live evidence.

## OPENING MANAGER (OM) — fast intrabar defense (owner directive 2026-07-07)

**The bug OM fixes:** basket management only ran on NEW M5 closes (every 5 min). Between M5 closes,
open positions were unmonitored, and `peak_r` was sampled only at M5 boundaries — so an intrabar
spike to +2R that fell back to −1R was invisible; the trail never saw the peak. Result (owner's
words): "ได้กำไรมากแล้วไม่ปิด รอจนโครงสร้างเปลี่ยนติดลบถึงปิด" — fear/defensive trading, not
profit-hunting. OM adds a fast (~4s) monitor that runs EVERY tick, independent of M5 cadence.

**Principle: ความหิวกระหายกำไร — hunger for profit, not defensive fear.** Every open basket is hunted
toward maximum realized profit with fast protection so a big winner is never given back to breakeven.

### OM part 1 — Profit Hunter (continuous ratcheting trail)
- Every fast tick: live spot + lane positions → live `aggregate_r` and a CONTINUOUS `peak_r`
  (true running max, persisted, restart-safe — NOT 5-min sampled).
- Ratcheting profit lock: once `peak_r >= arm_trail_r`, locked floor = `peak_r × trail_keep_frac`;
  floor ratchets UP as peak grows (peak 1R→lock 0.7R, peak 2R→lock 1.4R…). If live `aggregate_r`
  falls to the floor → **CLOSE ALL NOW** at tick resolution, don't wait for M5.
- Spike capture: `aggregate_r >= spike_take_r` (news/liquidity burst) → take immediately.
- Hard take at `take_r`. This kills "cut-winner-then-give-back."

### OM part 2 — Basket Doctor (edge-measured repair)
- When a basket is negative AND structure broke: measure CURRENT edge via the hunt committee (which
  side has edge NOW).
- If the WINNING side is OPPOSITE the losing leg AND edge is strong → open a repair leg in the
  WINNING direction, sized to drag the basket aggregate to net-positive on a modest favorable move
  ("ถ้ามีแนวโน้มด้านตรงข้ามจะชนะควรเปิดซ้ำ"). If the losing side still holds edge → hold / same-side
  add at a better level. Never blind-hedge; always edge-measured.
- Goal: manage the multi-leg basket so the AGGREGATE turns net-positive, then close_all ("รวบยอด").
- Hard caps UNCHANGED and unbreachable (max_legs, max_basket_risk_mult, time_stop_min,
  daily_loss_baskets) — enforced at the single choke-point.

### Integration
- Main loop becomes a fast tick (~4s). Each tick: (1) ALWAYS run OM on any open lane (Profit Hunter
  + Basket Doctor); (2) on a new M5 close, additionally run the entry decision (hunt). Single
  process, single lock → no concurrency. When flat, OM is a cheap no-op and entries still fire at M5.

## DRAGON LADDER — adaptive profit-hunting exit (owner directive 2026-07-08)

**The gap it closes:** the flat peak-R trail (arm 0.4R / keep 0.70) gave NO protection below 0.4R
peak, so a small winner (peak 0.3R ≈ +$13 on a real trade) could ride all the way back to a full
−1R stop. Owner: "ที่กำไรแล้วปล่อยให้ SL ขัดแย้งกับ mindset ล่ากำไร" — unacceptable. The exit must be
adaptive to how big the move actually got, and hungrier.

**Principle (owner's words): ขี่บนหลังมังกร — ride the dragon.** Small green → bank it FAST but with a
floor so it can never become a full loss. Genuinely big peak → give it room, full peak-R, let it run.
Re-shape every 4s to real-time behaviour.

### 1. Concave giveback ladder (the floor is ALWAYS present once green, and ratchets up)
Track continuous `peak_r` (already fixed via openTime). Floor = `peak_r − giveback(peak_r)` where
`giveback` is concave (tight for small peaks = bank fast; generous for big peaks = ride):

| peak_r reached | floor (close if live_r ≤ floor) | behaviour |
|----------------|---------------------------------|-----------|
| ≥ 0.15R | 0.00R (breakeven) | **never let a real winner become a full SL** |
| ≥ 0.30R | 0.18R | lock a small scalp, take-fast bias |
| ≥ 0.50R | 0.32R (give back ≤0.18) | tight trail |
| ≥ 0.80R | 0.55R | trail |
| ≥ 1.20R | 0.85R | let it breathe |
| ≥ 2.00R | 1.45R (keep ~72%) | ride the dragon |
| ≥ 3.00R | 2.25R (keep ~75%) | full run |

Implement as a monotonic function (giveback grows sub-linearly), not a lookup, so it's smooth. The
floor only ever RISES. Config-driven; the table above is the default curve.

### 2. Stall-take (hungry scalp)
If a small position went green but has NOT made a new peak for `stall_ticks` (≈ 45–60s at 4s ticks)
AND live_r is decaying toward the floor, take it now rather than wait for the floor — small edges
decay fast. Only applies below a peak tier (e.g. peak < 0.5R); above that the ladder rides.

### 3. Opportunity add — pyramid the dragon (hungrier, capped)
While holding a WINNER (live_r ≥ +0.3R AND floor already ≥ breakeven), if the 4s read shows strong
continuation in the SAME direction (leader score high + displacement + close-location pressure), ADD
one leg in the position's direction to ride harder. Hard-gated: only when the basket is already
floor-protected (adding can't turn a green basket red past breakeven), obeys all basket caps
(max_legs, max_basket_risk_mult), one add per peak tier crossed. This is the "hunt more while holding"
the owner asked for — never a martingale (we only add to winners, never to losers).

### 4. Quality repair (unchanged principle, sharpened)
Repair stays edge-measured (open the winning side to drag the basket net-positive), now also feeds the
Dragon Ladder: once a repaired basket crosses breakeven, the same laddered floor protects it.

Caps unchanged and unbreachable. This replaces the flat OM trail; `arm/keep/take` env knobs remain for
override but the ladder is the default shape.

## Phases

- **P1 (now):** package + tests + shadow runner live on BTCUSD (24/7) and XAUUSD (from Monday open).
  Deliverable: journal filling with M5 decisions; zero orders.
- **P2:** demo-live micro entries via MCP for decisions with `p_win_est ≥ floor`; broker re-read checks.
- **P3:** repair engine live (caps enforced); close-all resolution telemetry.
- **P4:** external fusion — reuse `api/daytrader_service.py` (ForexFactory calendar, Grok macro bias)
  as decision features; news windows shrink size, never blanket-block (demo = let strategies trade).
- **P5:** promotion per readiness gates (PF > 1.2, ≥100 trades or 4 weeks, drawdown acceptable).

## KPIs (from journal, reviewed every loop iteration)

participation rate (decisions/expected M5 closes), entered vs skipped, would-have PnL of skips
(the "fear cost"), PF per setup, repair engine: % baskets resolved in profit vs cap-stopped,
max basket drawdown vs cap.

## Coordination

Codex owns the live scalp loops (M1 lane). Dexter3 is the M5 lane — separate label, separate lock.
Status flows through `docs/AGENT_SYNC_BOARD.md` each iteration. Fable = PM/brain; Sonnet agents code.
