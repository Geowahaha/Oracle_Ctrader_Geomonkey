# Session handoff — 2026-07-16 (post-Opus cleanup)

**Read this before touching anything.** Written by the Opus stretch of the
2026-07-15/16 session at the owner's instruction: *"เปิด session ใหม่แบบทำงานต่อ
จาก Fable ได้ ก่อนที่ opus จะมาสานต่อ ตัดที่ Opus ทำมั่วไว้."*

Purpose: separate **what is proven and live** from **what Opus got wrong**, so a
new session continues from solid ground and does not inherit the bad claims.

---

## 1. LIVE STATE (all verified rung-1 on the VM — keep)

Branch `deploy-xau-family-canary`. VM `/opt/dexter_pro`, demo 46670728.
Services: `dexter3-openapi-daemon`, `dexter3-fable`, `dexter3-grok` — all active.
**VM deployed code was hash-verified identical to committed HEAD** (sha256 on all
core files). Do not assume drift; re-verify the same way if in doubt.

| Fix | Commit | Evidence |
|---|---|---|
| Money-scaling: `commission`/`swap`/`used_margin` ÷ 10^money_digits in `_normalize_position` | `6a0cd3c` | Raw commission -12 → **-0.12** on deployed code with the real captured payload; the incident short's netProfit -17.54 → **-5.66**. Deals path was always correct. |
| Smart-exit regression: `DEXTER3_SMART_EXIT_ENABLED=0` restored (PC parity; VM defaulted ON) | `360c43f` | `smart_confirmed_break` closes went **28s-after-entry → 0** in the next window. |
| Twin-entry guard: `DEXTER3_CROSS_LANE_DEDUP=downsize` | `952815a` | **6 live firings.** Cleanest: 04:20:25 grok sell + 04:20:39 fable sell (14s apart, same side) → follower risk 5.25 → **2.625**. Journals `cross_lane_dedup_downsized` **to the DB, not stdout** (journalctl grep will show 0 — query the journal DB). |
| Versioned labels + family identity | `90239b5` | `dexter3:fable:v1.8-size-the-edge`; ownership matches by family prefix so a version bump never orphans open positions (proven live on an old-label position). |
| Lane decoupling H1–H7 | `177cc3e` | time-based deals window, `label` column on decisions/basket_events/skip_outcomes, account-wide risk cap, per-lane logs. |
| OM triad: spot-freshness gate, peak_r ledger, repair lineage | (in `6a0cd3c` bundle) | `pnl_spot_age_sec` now journaled on every OM tick. |
| grok false loss-latch cleared (twice) | — | Both latches were **bug-induced**: (1) midnight-cache window, (2) money-scaling phantom commission tripping the $15 cap (trigger position's REAL pnl was **-3.07**, not the -14.4 the poisoned OM reported). Real grok realized that day: **-3.98**. |
| Repo units are now the env source of truth | `360c43f` | `ops/dexter3-{fable,grok}.service` carry the FULL proven env. The cutover-regression class (MAX_VOLUME_UNITS, ladder, smart-exit) happened because config lived only in untracked VM drop-ins. |

Shadow observers (no power, collecting): `DEXTER3_PA_EYE=shadow`,
`DEXTER3_REPAIR_HARVEST=shadow` (bank 0.4 / slf 0.5) + `_MULTILEG_OBSERVE=1`.
**Harvest has produced 0 episodes so far — still unverified.**

---

## 2. PROVEN MEASUREMENTS (gate-independent — keep, these survived)

Measured on 6000 M5 XAU bars (2026-06-16 → 07-16), SL-first honest, 986 trades:

- **The opportunity set is fat-tailed:** mean MFE **1.31R**; **16.2% of trades reach ≥3R with mean MFE 6.11R**; 21.9% reach ≥2R; **max MFE 20.64R**.
- **Live captures mean +0.24R, max EVER +0.44R** (34 real trades) → we realize ~18% of mean MFE and never touch the tail.
- **Timing:** trough (max adverse before peak) is **early** — p50 = bar 0, p75 = bar 4. Peak is **late** — p50 = bar 23, p90 = bar 46. Trough precedes peak 99.1%.
- **The 1R stop is already optimal — DO NOT TIGHTEN.** A 1.0R stop kills **0.0%** of the 3R+ runners; 0.7R kills 23.8%; **0.5R kills 37.5%**. (This refuted Opus's "cut losers faster" idea — data won.)
- **Tail runners' MAE-before-peak never exceeds 1.0R** (p50 0.40R, p90 0.86R).
- **The live ladder is dimensionally wrong:** `0.25:0.02,0.50:0.15,0.80:0.40,1.20:0.80,2.00:1.45,3.00:2.25` — every rung's giveback (peak−floor) is **0.35–0.83 ATR**, i.e. **less than one M5 bar's normal range** (XAU M5 ATR ≈ 4.5–5.0 pts; median 1R = 6.88 pts = 1.52 ATR). It arms on the initial pop and is then stopped by the **normal 0.40R dip that precedes every runner**. This is why max capture = 0.44R = the 0.80R rung's floor.
- **Owner's pyramid idea measures +0.42R/runner** (MFE≥2R: 5.17 → 5.59R; MFE≥3R: 6.11 → 6.55R). Because the trough is at bar 0–4, it is **not** a "wait for a distant pullback" play — it is a **limit order at −0.4R placed at entry time**. (Trough detection via M5 reversal-break bar only hit 17.1% — don't bother detecting, use the limit.)

---

## 3. ⛔ WHAT OPUS GOT WRONG — CUT THESE (do not build on them)

1. **THE BIG ONE — the convex/exit replay tested the WRONG GATE.**
   Opus ran `--gates v17,v17-mission,none`. **`v18` is the live config** —
   `scripts/dexter3_edge_discovery.py::_entry_gate_config` literally comments
   `v18` as *"Live V1.8 size-the-edge launcher config"*, and the fable lane runs
   `DEXTER3_FABLE_VERSION=v1.8-size-the-edge` with `V18_WINNER_BOOST/CHASE_RESCUE/B_TIER=1`.
   → **Every "LADDER-BASELINE (live) PF 0.38/0.44" number Opus reported is NOT the live system.** It is v1.7. **RETRACTED.**
   → A `--gates v18` run was launched at handoff time; output: VM `/tmp/convex_v18.log`. **Collect it first.** Nothing about the live baseline is known until then.
   (The gate FUNCTION is shared with live — `_apply_entry_gate` calls the real `evaluate_v16_entry_gate` — only the config preset differed. So the harness is sound; the invocation was wrong.)

2. **"Committee conviction is anti-predictive" — RETRACTED, killed by its own test.**
   A live N=48 sample suggested leader_score 0.10–0.18 was the only profitable band (+13.24, WR 56.2%). The 10k replay says that band is **derive -74.41 / validate -108.87 (WR 40.8) = the worst validate bucket**, and **every** bucket is negative on validate. **leader_score has no stable relationship with outcome in either direction.** The N=48 was noise. Do not resurrect the "flip the gate" idea on that evidence.

3. **"The buy side is structurally broken" — UNVERIFIED at the live gate.**
   Measured at gate=none/v17: derive buy -453 (WR 41.6) vs sell +331.94; validate buy -280.49 (WR 41.1) vs sell -19.02, in a FLAT validate window (+4.83 pts). The flat-window control is genuinely strong, but **it was never measured at v18**, and the 7-week sample contains a crash + a flat range and **no sustained uptrend** — the system's buys have never been observed in a bull regime. Treat as a hypothesis needing the v18 re-run, not a fact.

4. **"Cut losers faster / tighten the SL" — RETRACTED** by §2 (0.5R stop kills 37.5% of runners).

5. **Process failure to avoid repeating:** Opus announced "sending the test now" and did not send it, twice costing the owner a cycle. Launch first, report after.

---

## 4. THE ONE MISSING MEASUREMENT (do this first)

```
ssh VM; cd /opt/dexter_pro
PYTHONPATH=/opt/dexter_pro DEXTER3_TRANSPORT=openapi \
DEXTER3_OPENAPI_DAEMON_URL=http://127.0.0.1:9877 DEXTER3_OPENAPI_DAEMON_TIMEOUT_SEC=90 \
  .venv/bin/python -u scripts/dexter3_convex_exit_replay.py \
    --symbol XAUUSD --count 6000 --gates v18 --pyramid
```
This prints, for the REAL live config: the **LADDER-BASELINE** (what the system
actually does — never measured before this harness), a plain-TP reference, the
convex grid (no TP; trail arms at `arm_at_r`; giveback in **ATR**), and the
pyramid overlay. Verdict `BEATS-LADDER` = positive on BOTH segments **and**
validate netR > the ladder baseline's on the same gate.

Known caveat: the replay's ladder uses **discrete rungs**; live
(`opening_manager.ladder_floor_r`) uses **linear interpolation** between rungs →
the live floor is *tighter* → the baseline is conservative **in the ladder's
favour**. A convex win over this baseline understates the real gain.

Regime context for reading any split: **derive = XAU crashed -467.28 pts**
(4496→4029); **validate = FLAT +4.83 pts** (4029→4034). Exits invert between
these two regimes — that is the dominant variable in every result so far.

---

## 5. OPEN DECISION FOR THE OWNER (unchanged, still open)

Entries have been tested 5 ways (config sweep, exit geometry ×240, bucket rules,
PA-Eye, conviction) — all edge-free. Exits are now testable properly for the
first time. If the v18 run shows no exit passes both segments:

- **(A) Change producer.** VP is the only thing that ever passed a gate (+76R,
  PF 1.57, ~$85/day) — shelved for regime-local rolling-WF failure. New reason to
  revisit: we now know the baseline it must beat is a certified loser, not a good
  system.
- **(B) Build regime awareness first.** Crash vs flat inverts every result; the
  system trades identically in both. Caveat: VP's regime gate already tried this
  and died.

Opus leaned (A). **Owner has not decided.** Do not act without sign-off.

---

## 6. STANDING RULES (owner's, non-negotiable)

- **No hard blockers on demo** — downsize, never block (`feedback_demo_let_strategies_trade`).
- **Additive only** — never overwrite/distort proven strategies.
- **No deploy without gate PASS + owner sign-off**; shadow first.
- **No hardcoded timestamps/magic values** — a hardcoded `open_time` + 180min
  basket time-stop already made a test a wall-clock time bomb that died at 08:00Z.
- **NEVER `git stash -u` in this repo** — `dexter-mcp/node_modules` makes it hang
  and half-apply (untracked files land in `stash^3`). Use `git worktree` for
  HEAD checks; targeted `git checkout -- <path>` to reset one file.
- **VM deploys are surgical** (`scp` + `git checkout <sha> -- <files>`), never a
  full pull (dirty fibo tree). Always re-verify VM==HEAD by sha256.
- **Delegation model:** Fable = PM/reviewer/deployer, Sonnet = coder, scripts on
  the VM = free tier. Never delegate synthesis.
- **Dates due 2026-07-22:** B-tier scout EV verdict (owner: "ตัวเลขจริง ไม่ต้อง
  ใช้ความรู้สึก") + harvest shadow→live gate review.
