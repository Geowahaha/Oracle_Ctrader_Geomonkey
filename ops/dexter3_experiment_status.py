#!/usr/bin/env python3
"""Freeze-and-measure readout for the SL-floor experiment (2026-07-25).

The whole-project audit found the project's binding constraint is not sample
RATE but config CHURN: 5.1 live deploys per trading day against 0.5-4 closed
trades per lane per day, so no lane has ever held a frozen configuration long
enough to judge it, and every session ended with "no verdict yet". This script
exists so the freeze is *observable* — one command answers all three questions
that decide the experiment:

  1. DID IT EXECUTE?   sl_floor stamps on live decisions (the audit's #1
                       defect class was flags that were set but never ran —
                       `systemctl show` proves configuration, not execution).
  2. DID IT BACKFIRE?  entry refusals from the 1-oz volume floor. Widening a
                       stop raises dollar risk at minVolume=1, and the executor
                       REFUSES above DEXTER3_MIN_VOLUME_RISK_ABS_CAP_USD. If
                       refusals spike, we bought "fewer trades", not "better
                       stops".
  3. IS THERE EVIDENCE YET?  closed trades and R-expectancy per lane since the
                       freeze start, TREATED (fable, daytrend) vs CONTROL
                       (vp, scalp, dpull-cs, chf).

R is computed as pnl / (volume * volume_meta.sl_distance) — the *actual* risk.
The journal's volume_meta.risk_usd is pre-rounding INTENT and overstates R by
~1.7x median at the XAU 1-ounce floor, which is why it is not used here.

Read-only. On the VM:

    python3 ops/dexter3_experiment_status.py --since 2026-07-25T09:30:00Z
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

TREATED = ("fable", "dtr")
FREEZE_TARGET = {"fable": 40, "dtr": 40, "vp": 20, "chf": 20, "dpull-cs": 20, "scalp": 20}
DB_CANDIDATES = ("data/runtime/dexter3_journal.db",
                 "/opt/dexter_pro/data/runtime/dexter3_journal.db")


def _db(explicit: str | None) -> str:
    if explicit:
        return explicit
    for c in DB_CANDIDATES:
        p = Path(c)
        if p.is_file() and p.stat().st_size > 0:
            return c
    raise SystemExit("journal not found; pass --db")


def _lane(label: str) -> str:
    parts = str(label or "").split(":")
    return parts[1] if len(parts) > 1 else "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--since", required=True, help="freeze start, UTC ISO (e.g. 2026-07-25T09:30:00Z)")
    args = ap.parse_args()
    since = args.since.replace("Z", "")

    con = sqlite3.connect(_db(args.db))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # -- 1. did the SL floor execute? ---------------------------------------
    print(f"=== 1. SL-FLOOR EXECUTION (decisions since {args.since}) ===")
    applied: dict[str, list[float]] = defaultdict(list)
    not_applied: dict[str, int] = defaultdict(int)
    no_stamp: dict[str, int] = defaultdict(int)
    capped: dict[str, int] = defaultdict(int)
    for r in cur.execute(
        "SELECT label, features_json FROM decisions WHERE action='enter' AND ts_close >= ?",
        (since,),
    ):
        lane = _lane(r["label"])
        try:
            f = json.loads(r["features_json"] or "{}")
            f = f if isinstance(f, dict) else {}
        except (TypeError, ValueError):
            f = {}
        meta = f.get("sl_floor")
        if not isinstance(meta, dict):
            no_stamp[lane] += 1
        elif meta.get("applied"):
            applied[lane].append(float(meta.get("sl_over_tr_after") or 0.0))
            if meta.get("capped_by_max_abs"):
                capped[lane] += 1
        else:
            not_applied[lane] += 1

    lanes = sorted(set(list(applied) + list(not_applied) + list(no_stamp)))
    if not lanes:
        print("  (no enter decisions in window — market closed, or freeze just started)")
    for ln in lanes:
        a, n, s = len(applied[ln]), not_applied[ln], no_stamp[ln]
        avg = (sum(applied[ln]) / a) if a else 0.0
        tag = "TREATED" if ln in TREATED else "control"
        note = ""
        if ln in TREATED and (a + n) == 0:
            note = "  <== NOT EXECUTING (expected stamps on a treated lane!)"
        if ln not in TREATED and a > 0:
            note = "  <== LEAK! floor fired on a CONTROL lane"
        print(f"  {ln:10} [{tag:7}] applied={a:3} not_applied={n:3} unstamped={s:3} "
              f"avg_sl/TR_after={avg:.2f} capped={capped[ln]}{note}")

    # -- 2. did it backfire into refusals? ----------------------------------
    print(f"\n=== 2. REFUSALS / min-volume abs cap (exec_events since {args.since}) ===")
    ref = defaultdict(int)
    for r in cur.execute("SELECT event, payload_json FROM exec_events WHERE ts >= ?", (since,)):
        pj = r["payload_json"] or ""
        if "min_volume_risk_exceeds_abs_cap" in pj:
            try:
                p = json.loads(pj)
                ref[_lane(p.get("label", "?"))] += 1
            except (TypeError, ValueError):
                ref["?"] += 1
    print("  " + (", ".join(f"{k}={v}" for k, v in sorted(ref.items())) if ref
                  else "none (good — the floor is not converting trades into refusals)"))

    # -- 3. evidence accumulated --------------------------------------------
    print(f"\n=== 3. EVIDENCE SINCE FREEZE ({args.since}) ===")
    fills: dict[int, float] = {}
    for r in cur.execute("SELECT position_id, payload_json FROM exec_events "
                         "WHERE event='entry_executed' AND ts >= ?", (since,)):
        try:
            p = json.loads(r["payload_json"] or "{}")
            vm = p.get("volume_meta") or {}
            risk = float(p.get("volume") or 0.0) * float(vm.get("sl_distance") or 0.0)
            if risk > 0 and r["position_id"]:
                fills[int(r["position_id"])] = risk
        except (TypeError, ValueError):
            continue

    agg: dict[str, list[float]] = defaultdict(list)
    money: dict[str, float] = defaultdict(float)
    for r in cur.execute("SELECT position_id, payload_json FROM exec_events "
                         "WHERE event='lane_position_closed' AND ts >= ?", (since,)):
        try:
            p = json.loads(r["payload_json"] or "{}")
            pnl = p.get("pnl")
            if pnl is None:
                continue
            lane = _lane(p.get("label", "?"))
            money[lane] += float(pnl)
            risk = fills.get(int(r["position_id"] or 0))
            if risk:
                agg[lane].append(float(pnl) / risk)
        except (TypeError, ValueError):
            continue

    if not money:
        print("  (no closed trades yet in the freeze window)")
    for ln in sorted(money):
        rs = agg[ln]
        ev = (sum(rs) / len(rs)) if rs else float("nan")
        target = FREEZE_TARGET.get(ln, 20)
        n = len(rs) if rs else 0
        tag = "TREATED" if ln in TREATED else "control"
        ready = "READY TO JUDGE" if n >= target else f"need {target - n} more"
        print(f"  {ln:10} [{tag:7}] closed={n:3}/{target} net=${money[ln]:+8.2f} "
              f"EV={ev:+.3f}R  {ready}")

    print("\nBASELINE to beat (whole-project audit, N=105): system EV −0.136R  "
          "| fable −0.174R (N=58) | dtr −0.201R (N=23)")
    print("RULE: do NOT change fable/daytrend config until both reach their freeze target. "
          "A change before then voids the experiment — the failure mode that has "
          "ended every prior session with 'no verdict yet'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
