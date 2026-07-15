#!/usr/bin/env python3
"""Repair cross-lane vanish-reconcile poisoning in the dexter3 journal.

Background (2026-07-15 incident): before ``dexter3/executor.py``'s
``reconcile_vanished_lane_positions`` filtered candidates to the executor's
own lane label, one lane's reconcile pass could pick up ANOTHER lane's still-
open ``entry_executed`` row as a "vanished" candidate (it is absent from the
caller's own label-filtered ``open_positions`` list, so it LOOKS vanished).
The foreign position's zero-pnl entry-leg deal alone was enough to satisfy
the old code's ``pnl is not None`` check, so it got journaled as a real
broker-side close with ``pnl=0.0`` — a fabricated outcome for a position that
was, and still is, open under a different lane's label.

Detection signature (confirmed against the live vanish writer, NOT a label
mismatch — the vanish writer copies the entry row's own label onto the close
row, so close-label == entry-label even when poisoned; see
``reconcile_vanished_lane_positions``'s ``record["label"]`` assignment):
an ``exec_events`` row with ``event == "lane_position_closed"`` AND
``payload["reconciled"] is True`` (the marker unique to the vanish writer —
``close_lane_position``'s own ``lane_position_closed`` rows never set this
key) AND ``payload["pnl"] == 0.0`` exactly.

Dry-run by default: lists every matching row (ts, position_id, label, pnl)
and changes nothing. ``--apply`` deletes exactly those rows — safe and self-
healing, because deleting a poisoned close row clears the dedup marker
``reconcile_vanished_lane_positions`` checks for, so the OWNING lane's next
reconcile pass either re-journals the real close from broker deals (now
label-filtered, so it can no longer be poisoned the same way) or correctly
defers while the position is genuinely still open. Rows where the vanish
signature is absent, or the label is missing, or the pnl is anything other
than exactly 0.0, are never touched.

Usage:
    python ops/dexter3_repair_cross_lane_vanish.py              # dry-run (default)
    python ops/dexter3_repair_cross_lane_vanish.py --apply      # delete the poisoned rows
    python ops/dexter3_repair_cross_lane_vanish.py --db PATH    # a different journal DB
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dexter3.decision_journal import DEFAULT_DB_PATH  # noqa: E402


def find_poisoned_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every cross-lane-poisoned ``lane_position_closed`` row.

    See the module docstring for the exact signature. Never raises on a
    malformed payload row — it is simply skipped (not a candidate), same
    "degrade rather than crash" posture as the rest of the dexter3 journal
    tooling.
    """
    cur = conn.execute(
        "SELECT id, ts, symbol, position_id, payload_json FROM exec_events "
        "WHERE event = 'lane_position_closed' ORDER BY id"
    )
    out: list[dict[str, Any]] = []
    for row_id, ts, symbol, position_id, payload_json in cur.fetchall():
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            continue
        if payload.get("reconciled") is not True:
            continue  # not the vanish writer's row — never a candidate
        label = payload.get("label")
        if not label:
            continue  # unlabeled — never touched, per the module docstring
        pnl = payload.get("pnl")
        try:
            pnl_val = float(pnl) if pnl is not None else None
        except (TypeError, ValueError):
            continue
        if pnl_val != 0.0:
            continue  # real nonzero (or unknown/None) pnl — a legit close, not poisoning
        out.append(
            {
                "id": int(row_id),
                "ts": ts,
                "symbol": symbol,
                "position_id": position_id,
                "label": label,
                "pnl": pnl_val,
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find/delete cross-lane vanish-reconcile poisoned journal rows.")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH), help="path to the dexter3 journal sqlite DB")
    ap.add_argument("--apply", action="store_true", help="delete the poisoned rows (default: dry-run/list only)")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"no journal DB at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        rows = find_poisoned_rows(conn)
        if not rows:
            print(f"no poisoned cross-lane vanish rows found in {db_path}")
            return 0
        for r in rows:
            print(
                f"id={r['id']} ts={r['ts']} symbol={r['symbol']} "
                f"position_id={r['position_id']} label={r['label']!r} pnl={r['pnl']}"
            )
        if args.apply:
            ids = [r["id"] for r in rows]
            conn.executemany("DELETE FROM exec_events WHERE id = ?", [(i,) for i in ids])
            conn.commit()
            print(f"{len(ids)} poisoned row(s) deleted (dry-run listed them above)")
        else:
            print(f"{len(rows)} poisoned row(s) found (dry-run — use --apply to delete)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
