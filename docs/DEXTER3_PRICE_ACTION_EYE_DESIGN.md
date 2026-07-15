# Dexter3 Price Action Eye — design (2026-07-15)

Owner seed idea (2026-07-15, "ไอเดียเล็กๆ ไม่ใช่คำสั่ง"): make both lanes
price-action intelligent — candle anatomy, down to tick and trend bars — so
the system SEES what the owner sees on a chart (wick rejection at a minor
level, green close before a sell signal) and joins momentum in the direction
the tape is actually going. This document completes that idea into a
buildable, gate-disciplined design.

## Why (evidence, not vibes)

1. **The certified bottleneck is entry direction quality**, not exits: the
   2026-07-11 sweep found no config-extractable edge in decide_hunt/brain
   (240 combos all negative on derive); the 2026-07-14 bank-green replay put
   ALL top-12 exit combos in one family — exits are workable, entries are not.
2. **The owner's eye out-filters the committee at levels.** Two live cases
   (2026-07-15): grok sold support after a bullish-close wick bar
   (leader 0.056, p_win 0.447 — its own committee called it sub-coinflip);
   fable bought a two-upper-wick minor resistance via the B-tier scout
   override. NO current lens reads bar anatomy or local S/R location — the
   main system's `analysis/entry_sharpness.py` (8 microstructure features)
   was never ported to dexter3.
3. Both flagged trades become **regression fixtures**: the Eye must veto
   exactly those two shapes (encoded from their journaled bars) before any
   flag turns on. The owner's eye is the unit test.

## Design principles (house rules)

- **Additive only**: new module + new daemon mode + flags default OFF.
  Live behavior unchanged until a flag flips (shadow first, always).
- **Journal everything from day one**: Eye features ride on every decision
  row even in shadow — the learner and later replay can condition on them.
- **Replay-gate what is replayable** (bar anatomy on historical M1/M5 via
  the existing edge_discovery/geometry harness); **shadow-live what is not**
  (tick layers — no tick history is stored today).
- External sources, honestly classified: `priceaction.com` (Al Brooks
  style) is a CONCEPT source for the anatomy rules — not a data feed.
  `github.com/spotware/Trend-bar-service` is sample code proving custom
  bars can be built from live spot events — a pattern to borrow, not a
  dependency to install.

## Layer 1 — Bar Anatomy Reader (M1 + M5) — pure, replayable

New `dexter3/price_action_eye.py`, pure functions over the bar lists the
lanes already fetch (no new I/O):

- Per closed bar: `body_frac` (body/range), `upper_wick_frac`,
  `lower_wick_frac`, `close_pos` (close position in range 0..1),
  `range_atr_ratio` (bar strength vs rolling ATR).
- Classification (Brooks-style, thresholds env-tunable, defaults documented):
  trend bar (body_frac >= ~0.6, close in the leading third), doji/indecision
  (body_frac <= ~0.3), rejection bar (dominant wick against close side).
- Sequence features: consecutive same-direction trend bars, follow-through
  (did the next bar confirm the prior trend bar?), two-bar rejection cluster
  (the fable case), sweep-and-reclaim wick at a swing point (the grok case).
- Location: micro swing highs/lows from the last N bars + round-number
  proximity → `at_minor_resistance` / `at_minor_support` flags.
- Output: one `pa_eye` dict per decision + a verdict
  `support|neutral|oppose` for the committee's proposed side.

Wiring (flagged):
- `DEXTER3_PA_EYE=off|shadow|veto|lens` — `shadow` journals features and
  verdict only; `veto` blocks entries whose verdict is `oppose`
  (e.g. buying INTO a rejection cluster at minor resistance); `lens`
  additionally votes in the hunt committee with its own weight.
- Applies to BOTH lanes (grok's floor stays; the Eye is a second,
  orthogonal check — anatomy, not score).

Gate: replay Layer 1 over 6000 bars with the geometry harness — verdict must
improve the accepted stream on BOTH segments before `veto` is proposed.

## Layer 2 — Tick Pulse — daemon collector + mode

The daemon already holds a live spot stream. Add a rolling tick ring buffer
(daemon-side, bounded memory) and a new read-only mode `tick_pulse`
returning a last-N-seconds summary per symbol:

- `tick_dir_ratio` (upticks/downticks), `tick_velocity` (ticks/sec vs its
  own baseline — urgency), `delta_proxy` (signed mid-move accumulation),
  `spread_state` (current vs rolling median).
- Lanes call it once at decision time (one cheap local HTTP call):
  committee side agreeing with pulse → normal/boost; disagreeing → downsize
  or skip (env-tunable, `DEXTER3_TICK_PULSE=off|shadow|gate`).

This ports the main system's order-flow confirmation concept (delta_proxy,
tick_up_ratio) into dexter3's transport. Not replayable (no stored ticks) →
must run `shadow` first and accumulate journaled agreement-vs-outcome
evidence before `gate`.

## Layer 3 — Synthetic trend bars + momentum-join trigger

Borrowing the spotware Trend-bar-service pattern: build N-tick bars (and
optionally range bars) lane-side from the Layer-2 buffer, apply Layer-1
anatomy to them → a `micro_trend` state (up/down/chop) that updates faster
than M1 and normalizes for market speed. The owner's "รู้ว่าจะไปทางไหน
ก็กระโดดเข้าทางนั้น": an entry TRIGGER that waits for micro_trend to flip or
confirm in the committee's direction before firing (market-in on
confirmation instead of limit-into-noise — the same philosophy as the main
system's BreakConfirmEntry, expressed at tick scale for scalping).

Layer 3 depends on Layer 2's buffer and inherits its shadow-first rule.

## Rollout (delegation model)

- **Phase A (Sonnet, ~1 session):** `price_action_eye.py` + tests (incl.
  the two regression fixtures) + journal wiring in `shadow` + replay run
  through the geometry harness. Decision point: replay verdict.
- **Phase B (Sonnet + careful Fable review — touches the daemon):** tick
  ring buffer + `tick_pulse` mode + lane-side shadow journaling.
- **Phase C (after A/B evidence):** synthetic bars + momentum-join trigger,
  shadow → gate per measured agreement.

Kill criteria at every phase: shadow metrics must show the Eye's `oppose`
verdicts have materially worse outcomes than `support` (e.g. mean R gap) on
>= 100 decisions before any blocking mode; any flag can revert to `off`
instantly (env drop-in).

## Explicit non-goals

- No third-party data dependency (priceaction.com has no API; concepts only).
- No committee rewrite — the Eye is a new lens/gate, existing lenses keep
  their weights.
- No sizing changes — sizing stays with governor/anti-chase/v18; the Eye
  only informs direction quality and entry timing.
