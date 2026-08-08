# What is actually possible — synthesis of every test and every live deal (2026-08-08)

Owner question: *"จากบทเรียนทดสอบและ Forward test ที่ผ่านมาทั้งหมด น่าจะพอทำให้เป็นไป
ได้จริงไม่ว่าทางไหนสักทางไหม ต่อยอดอะไรได้บ้าง"*

This is a synthesis, not a new experiment. Every number below is either a broker
deal or a pre-registered measurement already on the board. Nothing was deployed.

## 1. The live ledger — broker truth, not prose

`ops/dexter3_lane_tally.py` machinery, 944 deals, since 2026-07-15:

| lane | N | net |
|---|---|---|
| fable (XAU) | **244** | **+23.64** |
| mscalp-be (USTEC, BRK+BEW) | 30 | +23.60 |
| chf (XAU) | 10 | +11.84 |
| sniper (XAU) | 22 | +9.84 |
| scalp (XAU) | 4 | +2.37 |
| dtr (XAU) | 118 | −3.04 |
| vp (XAU) | 54 | −5.68 |
| dpull-cs (XAU) | 130 | −11.36 |
| h3fade (XAU) | 34 | −14.17 |
| dpull (XAU) | 14 | −17.40 |
| mscalp-brk (USTEC) | 6 | −25.30 |
| mscalp2 (USTEC) | 54 | −35.40 |
| grok (retired) | 92 | −35.87 |
| **sniper-ustec** | 16 | **−94.10** |
| **mscalp** (USTEC) | 76 | **−136.50** |
| **mscalp-be-base** (USTEC) | 12 | **−197.20** |
| **sniper-us30** | 20 | **−224.50** |
| **SYSTEM** | **944** | **−733.28** |

## 2. The one fact that reorganises everything

**Five INDEX lanes account for −$688 of the −$733 (94%). Eleven XAU lanes,
running 630 deals between them, net roughly ZERO (−$9 combined).**

Average loss per losing deal tells the whole story:

| family | avg loss / deal |
|---|---|
| XAU lanes (fable / dtr / vp / dpull-cs) | **−$1.40 … −$3.19** |
| USTEC lanes (mscalp / mscalp-be-base) | **−$14.65 … −$22.02** |

The index lanes are not worse strategies — several share their producer with a
break-even XAU cousin. They are the **same fair game played for 5–10× the
stake**, because `USTEC`/`US30` at the 1-unit volume floor carries a notional
that XAU at 1 oz does not. The system did not lose $733 by predicting badly. It
lost $733 by **betting big on a coin-flip it had already proven was a coin flip.**

## 3. What two days of pre-registered testing established

* **Direction on USTEC M5 is unforecastable.** 10 entry families dead (headroom,
  BRK, wick-tip, dip×3, VP thin-air, VP value-area, FVG retest, KillRBD DBR,
  Fibonacci zones ±confirmation). The 08-07 information campaign found no state
  exceeding 2 SE at any horizon.
* **Every configuration that "won" won by geometry a coin flip could exploit**
  — v1 A4 (wide TP), v2 C3 (PA rejection), v3 tight stop: all had *positive
  inverted arms*. Long gamma, not edge.
* **A dollar-take is a function of the stop it sits on.** $5 on a ~45-point stop
  needs 9 wins per loss (measured WR 84% → still loses). Removing the cap was
  worth +7–8 pts/structure in the two-way test. It works only on mscalp's
  original 1.3×ATR geometry.
* **A symmetric take equal to the stop cannot win** — best possible outcome is
  −1.5 points once spread is charged both sides. Arithmetic, not statistics.
* **Frequency IS the loss** for any structure whose edge is ≤ its cost: at
  1.5–3.0 points per firing, 1,079 firings in 50 trading days is the entire
  deficit.

## 4. So — is any path actually possible? Three, ranked by evidence

### PATH A — Fix the stake, not the forecast *(strongest evidence, arithmetic)*
The instrument's minimum volume, not the strategy, decides whether a fair game
bankrupts you. Concretely: **measure every lane's actual $ risk at the volume
floor and refuse to run an instrument whose floor-risk exceeds the account's
per-trade budget.** On the live ledger this alone removes ~94% of the loss while
deleting no hypothesis and no learning — the same signals keep being journaled.
This is not "cutting from fear" ([[feedback_innovator_not_fear]]): it changes
the stake, not the strategy set.

*Buildable now:* a pre-trade check comparing `floor_volume × stop_distance ×
pip_value` against `DEXTER3_BASE_RISK_FRAC × capital`, journaling a
`floor_risk_exceeds_budget` skip. Additive, env-gated, off by default.

### PATH B — Stop paying five times for one hypothesis *(strong evidence)*
Five mscalp-family lanes trade the same producer. The 2×2 factorial was correct
*as measurement* and is now largely answered on live deals (mscalp-be +23.60 on
N=30 vs mscalp-be-base −197.20 on N=12 — consistent with replay's "BEW on BRK is
the best cell"). Consolidating to the surviving cell costs nothing in knowledge
and stops multiplying a negative-EV population by five.
*Owner decision, not mine — the trials were opened by owner order.*

### PATH C — Cheap and rare, direction-agnostic *(the only design the data endorses)*
If magnitude is the sole forecastable quantity, express it where cost cannot eat
it: **one shot per day, wide target, unmanaged** — exactly the `xaudaily`
posture already live. Every expensive variant of the same idea (two-way,
straddle-per-signal, $5 caps) was measured and failed *on cost*, not on concept.
The buildable extension is a **direction-agnostic** version: a once-daily
breakout straddle at the measured-highest-volatility hour, no cap, structural
stop — one firing (~1.5 pts of cost) instead of 1,079.

### What is NOT possible on current evidence
A profitable directional M5 scalper on USTEC. Ten families, three
pre-registrations, one information campaign. Further entry filters are the
definition of doing the same thing again.

## 5. The honest caveat

fable is +$23.64 on **244 deals** — that is +$0.10 per deal, i.e. statistically
zero, not an edge. The correct reading of the XAU book is *"cost structure works,
edge unproven"*, not *"XAU wins"*. What the ledger licenses is a claim about
**stake and cost**, which is arithmetic, and no claim at all about **prediction**.

Nothing here was deployed. All three paths need owner sign-off.
