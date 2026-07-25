#!/usr/bin/env python3
"""Entry funnel: why do enter DECISIONS not become broker ORDERS?

The 2026-07-25 vp deep-dive found the lane had journaled 155 ``enter``
decisions all-time (``vp_lvn_rejection`` 75, ``vp_poc_reversion`` 72,
``vp_hvn_break_retest`` 6) while the broker showed only 6 fills — all
lvn_rejection. ``vp_poc_reversion`` was 72-for-0. The journal could not say
WHY: gate verdicts and the synthetic-limit intent's fate were never stored on
the decision row.

This reads the instrumentation added the same day
(``features_json.entry_outcome`` = the post-gate branch, and
``features_json.intent_fate`` = how a pending limit intent ended:
filled / expired / killed_break / **replaced** by a newer signal) and prints a
per-setup funnel, so "72-for-0" resolves into a ranked list of real causes.

Read-only. Run on the VM (or any host with a copy of the journal):

    python ops/dexter3_vp_funnel.py                        # vp lane, all time
    python ops/dexter3_vp_funnel.py --label dexter3:fable  # another lane
    python ops/dexter3_vp_funnel.py --since 2026-07-25     # after a deploy
    python ops/dexter3_vp_funnel.py --db /path/journal.db

Decisions written BEFORE the instrumentation was deployed have no
``entry_outcome`` and are reported separately as ``(not instrumented)`` — they
are not evidence of anything, so they are never folded into the percentages.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_DB_CANDIDATES = (
    "data/runtime/dexter3_journal.db",
    "/opt/dexter_pro/data/runtime/dexter3_journal.db",
)


def _resolve_db(explicit: str | None) -> str:
    if explicit:
        return explicit
    for cand in DEFAULT_DB_CANDIDATES:
        if Path(cand).is_file() and Path(cand).stat().st_size > 0:
            return cand
    raise SystemExit(
        "journal db not found; pass --db (tried: " + ", ".join(DEFAULT_DB_CANDIDATES) + ")"
    )


def _pct(n: int, total: int) -> str:
    return f"{(100.0 * n / total):5.1f}%" if total else "  n/a"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="path to dexter3_journal.db")
    ap.add_argument("--label", default="dexter3:vp", help="lane label prefix (default dexter3:vp)")
    ap.add_argument("--since", default="", help="inclusive ts_close prefix/ISO lower bound")
    ap.add_argument("--setup", default="", help="only this setup")
    args = ap.parse_args()

    con = sqlite3.connect(_resolve_db(args.db))
    con.row_factory = sqlite3.Row
    sql = "SELECT ts_close, setup, side, features_json FROM decisions WHERE action='enter' AND label LIKE ?"
    params: list[object] = [f"%{args.label}%"]
    if args.since:
        sql += " AND ts_close >= ?"
        params.append(args.since)
    if args.setup:
        sql += " AND setup = ?"
        params.append(args.setup)
    rows = list(con.execute(sql + " ORDER BY id", params))

    by_setup: dict[str, Counter] = defaultdict(Counter)
    fates: dict[str, Counter] = defaultdict(Counter)
    uninstrumented: Counter = Counter()

    for r in rows:
        setup = r["setup"] or "?"
        try:
            f = json.loads(r["features_json"] or "{}")
            if not isinstance(f, dict):
                f = {}
        except (TypeError, ValueError):
            f = {}
        eo = f.get("entry_outcome")
        if not isinstance(eo, dict):
            uninstrumented[setup] += 1
            continue
        outcome = str(eo.get("outcome") or "?")
        reason = str(eo.get("reason") or "?")
        by_setup[setup][f"{outcome}:{reason}"] += 1
        fate = f.get("intent_fate")
        if isinstance(fate, dict):
            fates[setup][str(fate.get("fate") or "?")] += 1
        elif outcome == "limit_intent":
            fates[setup]["still_pending_or_unstamped"] += 1

    print(f"=== entry funnel  label~{args.label}  rows={len(rows)}"
          + (f"  since={args.since}" if args.since else "") + " ===")
    if uninstrumented:
        tot_u = sum(uninstrumented.values())
        print(f"\n(not instrumented — decided before the 2026-07-25 stamps: {tot_u} rows)")
        for s, n in uninstrumented.most_common():
            print(f"    {s:26} {n}")

    instrumented = sum(sum(c.values()) for c in by_setup.values())
    if not instrumented:
        print("\nNo instrumented rows yet — redeploy/restart the lane, then re-run after the next entries.")
        return 0

    for setup in sorted(by_setup, key=lambda s: -sum(by_setup[s].values())):
        counts = by_setup[setup]
        tot = sum(counts.values())
        filled = sum(n for k, n in counts.items() if k.startswith("filled"))
        print(f"\n-- {setup}   decisions={tot}   filled={filled} ({_pct(filled, tot)}) --")
        for key, n in counts.most_common():
            print(f"     {n:4}  {_pct(n, tot)}  {key}")
        if fates.get(setup):
            ftot = sum(fates[setup].values())
            print(f"     · limit-intent fates (n={ftot}):")
            for key, n in fates[setup].most_common():
                print(f"         {n:4}  {_pct(n, ftot)}  {key}")

    print("\nREAD: 'blocked:*' = a gate refused it (reason names the gate). "
          "'limit_intent' + fate 'replaced' = a newer signal evicted a pending "
          "intent before it could fill (frequency problem, not an edge problem). "
          "'expired' = the level was never touched inside the TTL. "
          "'killed_break' = the confirm state machine vetoed it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
