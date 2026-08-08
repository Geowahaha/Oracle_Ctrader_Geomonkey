# Trial verdicts at their registered N≥30 bars — 2026-08-08 (Fable 5, owner delegation)

Owner delegation: *"ผมให้คุณตัดสินใจแล้วจัดการเลย ... ขอลึกซึ้งและจริงจังจริงๆ
อย่าเสียเวลาเปล่า"*. The deepest non-wasteful act available today is not another
measurement — it is **reading the verdicts of live trials that have already
crossed their pre-registered N≥30 judgment lines**, and executing them. Several
lanes ran past their finish lines with no one reading the result, bleeding
while newer experiments got the attention.

**Judgment basis (the registered standard, [[feedback_live_wins_only]]):
broker deals in dollars, N≥30 per lane, negative → kill without ceremony, no
tuning.** Numbers = full-lane-life broker truth via the `dexter3_lane_tally`
machinery (get_deals, netProfit, per exact label), pulled 2026-08-08 ~07:20Z;
every listed lane started on/after 2026-07-23 so the since-07-15 window covers
its entire life.

| lane | N closes | net $ | registered bar | **VERDICT** |
|---|---|---|---|---|
| `dexter3:mscalp:canary` | **76** | **−136.50** | N≥30 dollars | **KILL** — 2.5× past the bar, negative |
| `dexter3:mscalp2:canary` | **54** | **−35.40** | N≥30 vs mscalp | **KILL** — negative at 1.8× the bar (beats its control −136.50, but the A/B's question is moot when both arms are negative) |
| `dexter3:h3fade:canary` | **34** | **−14.17** | its own prereg: *"negative → kill without ceremony, no tuning"* | **KILL** — by its own frozen sentence |
| `dexter3:dpull-cs:canary` | **130** | **−11.36** | forward realized PnL vs dpull (retired) | **KILL** — negative at 4.3× the bar; the A/B answered "≈ zero either way" |
| `dexter3:mscalp-be:canary` | **30** | **+23.60** | N≥30 dollars | **KEEP** — positive at exactly the bar. Honesty stamp: +$0.79/deal ≈ +0.25 SE = statistically zero; it survives by the registered letter, and it is the only surviving USTEC cell. Regime stamp: its N accrued in the wk31-32 favourable regime. |
| `dexter3:mscalp-brk:canary` | 6 | −25.30 | N≥30 vs mscalp (T15 control) | **CLOSE (design-orphan)** — not an early judgment: its registered comparison is against mscalp's T15 control, which is now killed; the trial's question is no longer answerable. |
| `dexter3:mscalp-be-base:canary` | 12 | −197.20 | N≥30; pairs with mscalp-be | **CONTINUE** — N not reached and its pair (mscalp-be) survives, so the registered subtraction be − be-base (entry effect under BEW) remains answerable. Watch: worst per-deal loss in the fleet (−$22 avg). |
| `dexter3:sniper-us30/-ustec` | 20 / 16 | −224.50 / −94.10 | N≥30 | already disabled (pre-existing); no action |
| fable / daytrend | — | — | 40-trade freeze | **UNTOUCHED** (frozen) |
| vp / scalp / chf / sniper(XAU) / xaudaily | — | — | below bar or no bar due | **UNTOUCHED** |

## Execution plan (in order)

1. Flatten any open position under a killed lane's label (kill = trial over;
   an unmanaged position with only broker SL/TP is not "the strategy").
2. `systemctl stop` + `disable` the four killed units + mscalp-brk. Unit files
   stay in the repo and on disk (history, reactivation possible by owner order).
3. Verify: units inactive, no orphan positions, remaining lanes healthy.

## Ops incident discovered en route (fixed before verdicts executed)

While pulling the verdict numbers, found the **openapi daemon wedged in a
"Too many open files" reconnect spiral** (first hit 2026-08-08 05:55:36Z,
40,517 log lines, 42k+ reconnect attempts) — every lane's OM tick and governor
read had been failing since ~05:55Z (no bank/time-stop actions fleet-wide;
broker SL/TP backstops were the only protection). Restarted the daemon
(07:50Z); app-auth + token fine (demo 9922808 present in the grant), but
account-auth then returned Spotware-side "Cannot route request" — consistent
with the reconnect storm tripping a server-side throttle. Waiting it out with
slow polling before executing the verdicts. **Follow-up (next session): the
daemon needs an fd-leak fix or a `LimitNOFILE` + self-restart-on-fd-exhaustion
guard — a wedged daemon silently disables every exit manager in the fleet.**

Effect on the verdicts: none — all judged N was accrued before 08-08 05:55Z.
