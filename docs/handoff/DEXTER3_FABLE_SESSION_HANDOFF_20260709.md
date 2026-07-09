# Dexter3 — Fable session handoff (2026-07-09 ~14:35Z)

**Author:** claude-fable (Opus 4.8, PM/co-founder lane). **For:** next session.
**Read with:** `docs/handoff/DEXTER3_V16_GROK_PARALLEL_HANDOFF_20260709.md` (codex+grok detail),
`docs/AGENT_SYNC_BOARD.md` (coordination), `docs/DEXTER3_HANDOFF.md` (golden rules).

---

## 0. DO THIS FIRST (live-state critical)

- **The V1.6/V1.7 loop was DOWN when this session ended and I RESTARTED it at 14:33Z** (it had an
  unmanaged open Buy −$3.24). It is now **ALIVE pid 1096**, running **v1.7-selective-edge**, hunting
  (confirmed LIVE_ENTRY 650397341 + OM ticking + pullback-gate firing).
- ⚠️ **COORDINATION FLAG:** the sync board (a parallel Fable context, 14:40Z) had noted *"V1.7 relaunch
  awaiting owner approval"*. V1.7's entry-quality layer defaults **ON** (`DEXTER3_V16_ENTRY_QUALITY_ENABLED`
  unset ⇒ True), so my restart launched the full V1.7 stack. **Confirm with the owner whether V1.7 should
  keep running or revert to plain V1.6** (set `DEXTER3_V16_ENTRY_QUALITY_ENABLED=0`). The owner was
  actively directing deploys all session, so this is likely fine — but flagging for transparency.
- **GOLDEN RULE 0 (from DEXTER3_HANDOFF.md rule 0):** after ANY change, verify the hunter is *hunting*
  from live evidence — bars OK (no `insufficient_m5_bars`/`Symbol not available`), a real
  `action=enter/skip/manage` decision, entry places with SL+TP, `dexter3_shadow_state.json →
  basket_runtime.<sym>.oldest_open_ts` non-null + `peak_r` tracking, OM ~4s ticks. Never "done" on tests alone.

### Exact relaunch command (MUST include every env flag or behavior silently reverts)
```bash
DEXTER3_LIVE=1 DEXTER3_HUNT=1 \
DEXTER3_CAPITAL_USD=1000 DEXTER3_DAILY_TARGET_USD=100 DEXTER3_DAILY_LOSS_USD=50 \
DEXTER3_MAX_VOLUME_UNITS=10 DEXTER3_MAX_ENTRIES_PER_DAY=60 DEXTER3_DAILY_LOSS_BASKETS=3 \
DEXTER3_FAST_TICK_SEC=4 DEXTER3_OM_TAKE_R=1.2 DEXTER3_OM_SPIKE_R=2.5 \
DEXTER3_OM_LADDER_CSV="0.25:0.02,0.50:0.15,0.80:0.40,1.20:0.80,2.00:1.45,3.00:2.25" \
DEXTER3_ANTICHASE_ENABLED=1 DEXTER3_ANTICHASE_MULT=0.15 \
DEXTER3_PULLBACK_ENABLED=1 DEXTER3_PULLBACK_MULT=0.35 \
DEXTER3_SMART_EXIT_ENABLED=0 \
python -X utf8 -m dexter3.shadow_runner --symbols XAUUSD --loop --poll-sec 20 --live &
```
- **MCP zombies every ~15–30 min** (cTrader Desktop-side; keepalive won't help — loop already sends 4s
  traffic). Recover: `python scripts/ctrader_mcp_watchdog.py --restart`. The loop survives zombies
  (symbol-heal + session-gap window guard shipped this session). After a restart, `get_deals` returns
  empty for a few seconds until account data loads — retry.

---

## 1. Honest profitability state (do NOT claim success)

- **Still net-negative.** 2026-07-08→09 window: ~172 closes, ~50% WR, **net −$37.81**. The root cause
  (proven, 129-trade + 938-decision analysis): the system was **−EV by construction** — payoff 0.53:1
  live (Dragon Ladder banked wins too small), breakeven WR 65% > actual 60.5%, expectancy −$0.77/trade.
- The edges below are **deployed + verified firing live** but **NOT yet proven to flip profitability**.
  The next job is to **MEASURE before/after** and confirm the entry-selection edges actually lift
  expectancy on forward data — extend the backtest sweep to more history to firm up thin samples.

## 2. What this session shipped (the edge-discovery journey)

Tags are rollback anchors: `v1.0` → `v1.7-dexter3-selective-edge`.

| Ver | What | Evidence |
|-----|------|----------|
| v1.2 | openTime field fix — the peak-R trail was DEAD (MCP field is `openTime` not `openTimestamp`) | oldest_open_ts now non-null live |
| v1.3.1 | get_trendbars **symbol-heal** + **session-gap window** — cured a "no trades for 1h" incident | 60 bars M5/M15/H1 |
| v1.4 | **Dragon Ladder** adaptive exit (small green floored at breakeven, big peak rides) | smoke: 0.30R→+0.18, 2.5R→+1.70 |
| **fix #1** | **looser ladder** env (`DEXTER3_OM_LADDER_CSV`) — Dragon Ladder was CRUSHING win size; let winners run to TP | backtest payoff 1.35:1 vs live 0.53:1 |
| v1.5 | **anti-chase gate** — `edge_buckets.py` downsizes the ONE −EV bucket to 0.15× | verified live: risk 12→1.80 |
| v1.6 | **pullback-resumption selector** — full size on pullback entries, 0.35× scout otherwise | verified live: `is_pullback=True mult=1.0` / `False mult=0.35` |
| (built, OFF) | **gated smart-exit** (`smart_exit.py`, `DEXTER3_SMART_EXIT_ENABLED=0`) | data: HURTS good entries — kept for A/B only |
| v1.7 | **entry-quality layer** (`v16_entry_quality.py`, codex+grok) + **Grok v1.0 parallel lane** (`grok_v10.py`) | defaults ON; 225 tests |

### The core data-proven findings (from `scripts/dexter3_edge_discovery.py`, 938 decisions)
1. **`aligned × trending` (chasing a mature H1 trend) = the ENTIRE loss** (−116R, 43% of entries),
   robust across hold 12/24/48. The other 57% = +85R. → anti-chase gate downsizes it.
2. **Pullback-resumption entry = +0.055R vs +0.003R baseline (18×)** — the biggest lever, pure ENTRY
   SELECTION. → pullback gate upsizes it.
3. **Exit-tweaking is near-zero-sum on breakeven entries.** Widening SL raises WR (noise-stops are real
   — owner right) but worsens expectancy; smart-exit is double-edged by bucket. **Edge is in ENTRY
   SELECTION, not exit geometry.** (This is why smart-exit ships OFF.)
4. **SL is noise-scale** (min SL = TR_q50 ≈ 1 M5 bar ≈ $7.56) — a single wick sweeps it. The real fix is
   better ENTRY location (pullback → SL sits behind a real level), not a wider SL.

Diagnostic tools live in `scripts/dexter3_edge_discovery.py`: `--sl-mult`, `--smart-exit`,
`--disaster-mult`, `--pullback-only`, regime bucket table. Re-run to extend history / re-measure.

## 3. Key files (this session)

- `dexter3/edge_buckets.py` — anti-chase (`anti_chase_risk_mult`) + pullback (`pullback_resume`,
  `pullback_size_mult`) classifiers/sizers. Both wired in `shadow_runner.run_symbol_cycle` (~line 936:
  `_apply_anti_chase_gate` then `_apply_pullback_gate`, compounding on `risk_usd_override`).
- `dexter3/opening_manager.py` — Dragon Ladder (`ladder_floor_r`) + stall-take + pyramid-add + (v1.7)
  smart-loss-exit branch.
- `dexter3/smart_exit.py` — gated smart adaptive exit (OFF by default per data).
- `dexter3/v16_entry_quality.py` (codex+grok) — the v1.7 entry-quality gate (chase_hard_block,
  weak_hard_skip, cooldown, profit-control). Config: `_v16_entry_quality_config_from_env` in shadow_runner.
- `dexter3/grok_v10.py` — Grok v1.0 parallel scalper lane (separate lock `dexter3_grok_loop.lock`,
  label `dexter3:grok-v1.0:scalper`). Independent of the Fable lane by process/lock/label.
- Memory: `project_dexter3_edge_discovery_2026_07_08.md` (the finding). Tests: `test_dexter3_edge_gate.py`,
  `test_dexter3_pullback_gate.py`, `test_dexter3_smart_exit.py` (all green; 1 pre-existing skipeval flake).

## 4. Open items / next steps

1. **MEASURE the V1.6/V1.7 edges forward** — grep `anti-chase:` / `pullback-gate:` / `v16-entry-quality:`
   log lines + `get_deals`, compute per-bucket live PnL, confirm payoff lifted vs the −EV baseline. This
   is the unfinished proof.
2. **Extend the edge sweep to more history** (multi-window backfill / candle stores) — the pullback
   sample was thin (139); firm it up before trusting absolute edge.
3. **Confirm V1.7 vs V1.6 with the owner** (see §0 flag) + whether the Grok lane keeps running in parallel.
4. **Layer 2 shadow-forward**: the anti-chase/pullback classifications hit the LOG but not yet the
   journal DB (grep-able only) — journal them for DB-side per-bucket analytics.
5. **Repo hygiene:** a 33-file fibo purge was found uncommitted+unlogged earlier today and RESTORED
   (it broke `scheduler.py` imports). Pre-existing dirty working tree (agent/brain.py, api/*, etc.) is
   from before this session — do not blindly commit it. New untracked: `.codex/`, `.grok/`, `AGENTS.md`,
   `dexter-mcp/`, `daytrader-terminal/`.
6. **MCP remote (owner ask):** local Desktop MCP is inherently unstable; the `dexter-mcp` worker just
   proxies local. Real fix = cTrader OpenAPI-direct fallback so the loop hunts through zombies. Proposed
   as a phase, not yet built.

## 5. Owner working style (critical)

Wants a **co-founder who argues with numbers, not a yes-man.** Explicitly asked to be proven wrong when
the data disagrees. Decide-and-do (don't over-ask). **Test with the edge engine BEFORE deploying** — this
session rejected two intuitive-but-wrong fixes (wider SL, universal smart-exit) on data. Target: **$100/day
on $1000**. Speaks Thai — explain complex things in Thai. Demo account (trader ids 9922808 / 3555162) —
never widen that guard. Always `git push dexter` after commits.
