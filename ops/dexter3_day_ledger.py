#!/usr/bin/env python3
"""Dexter3 NIGHTLY FLYWHEEL — the system grades its own day (owner order
2026-07-17 "ทำเลยข้อ 2").

One page per day, written from three sources that already exist (nothing new
is instrumented — this is pure measurement of what the lanes journaled):

  1. decision journal (data/runtime/dexter3_journal.db) — every enter/skip
     per lane label;
  2. broker deals (transport get_deals) — realized $ per lane family;
  3. lane logs (data/runtime/dexter3*_shadow.log) — block reasons, intent
     lifecycle (set/filled/killed/expired + fill_vs_level), governor events.

For every BLOCKED or KILLED entry the ledger simulates the counterfactual
(the journaled entry/sl/tp walked forward on the day's real M5 bars,
SL-first conservative, hold 48) so each protective layer's daily save/cost
is a NUMBER: "bias blocked 5 buys: would have lost -3.2R" is the flywheel's
whole point — verdicts become an automatic morning ritual instead of a
manual research session.

Output: data/reports/day_ledger_<YYYYMMDD>.md (+ one summary line appended
to data/reports/day_ledger_index.md). Run nightly ~23:45Z (systemd timer
ops/dexter3-ledger.{service,timer}) or manually:

    DEXTER3_TRANSPORT=openapi DEXTER3_OPENAPI_DAEMON_URL=http://127.0.0.1:9877 \
        python ops/dexter3_day_ledger.py [--date 2026-07-16]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dexter3_edge_discovery import _simulate  # noqa: E402
from dexter3.transport import make_client  # noqa: E402

JOURNAL_DB = ROOT / "data" / "runtime" / "dexter3_journal.db"
REPORTS = ROOT / "data" / "reports"

LANES = {
    "fable": {"family": "dexter3:fable", "log": "dexter3_shadow.log"},
    "grok": {"family": "dexter3:grok", "log": "dexter3_grok_shadow.log"},
    "vp": {"family": "dexter3:vp", "log": "dexter3_vp_shadow.log"},
    "daytrend": {"family": "dexter3:dtr", "log": "dexter3_daytrend_shadow.log"},
    # scalp lane live 2026-07-17 (grok's successor) — added 2026-07-19 so the
    # 07-21/22 verdict can see it; label prefix matches dexter3:scalp:canary.
    "scalp": {"family": "dexter3:scalp", "log": "dexter3_scalp_shadow.log"},
}

BLOCK_RE = re.compile(r"cycle_status=decided:enter:([a-z0-9_]+):(?:live_blocked_|governor_)([a-z0-9_]+)")
INTENT_RE = re.compile(r"vp_limit_intent_(set|entered|killed_break|expired)\b(?:.*?level=([0-9.]+))?"
                       r"(?:.*?fill_vs_level=([+-][0-9.]+))?")


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _deal_pnl(deal: dict) -> float:
    for k in ("pnl_usd", "netProfit", "net_profit", "profit", "pnl"):
        if deal.get(k) is not None:
            return _f(deal.get(k))
    return 0.0


def _deal_day(deal: dict) -> str:
    for k in ("closeTimestamp", "executionTimestamp", "close_ts", "ts", "executionTime"):
        v = deal.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):        # epoch ms
            return datetime.fromtimestamp(float(v) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        s = str(v)
        if len(s) >= 10:
            return s[:10]
    return ""


def _counterfactual(entry: float, sl: float, tp: float, side: str,
                    day_bars: list, ts_close: str, max_hold: int = 48) -> float | None:
    """Journaled-geometry replay from the decision's bar forward — the same
    conservative SL-first sim every proof in this repo uses."""
    if not (entry and sl and tp) or not day_bars:
        return None
    future = [b for b in day_bars if str(b.get("ts") or "") > ts_close][:max_hold + 2]
    if len(future) < 2:
        return None
    outcome, r, _held = _simulate(side, entry, sl, tp, future, max_hold)
    return None if outcome == "skip" else r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    help="UTC day to grade (default: today)")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--deals-count", type=int, default=500)
    args = ap.parse_args()
    day = args.date

    # -- bars of the day (for counterfactuals), fetched once -----------------
    c = make_client()
    try:
        m5 = c.get_trendbars(args.symbol, "m5", 900)
        day_bars = [b for b in m5 if str(b.get("ts") or "").startswith(day)]
    except Exception as exc:                                  # noqa: BLE001
        print(f"WARN bars unavailable ({exc}) — counterfactuals skipped")
        day_bars = []

    # -- realized deals per family --------------------------------------------
    day_start_ms = int(datetime.fromisoformat(day + "T00:00:00+00:00").timestamp() * 1000)
    deals_by_lane: dict[str, list] = {k: [] for k in LANES}
    try:
        deals = c.get_deals(count=args.deals_count, from_timestamp_ms=day_start_ms) or []
        for d in deals:
            if _deal_day(d) != day:
                continue
            label = str(d.get("label") or "")
            for lane, cfg in LANES.items():
                if label.startswith(cfg["family"]):
                    deals_by_lane[lane].append(d)
    except Exception as exc:                                  # noqa: BLE001
        print(f"WARN deals unavailable ({exc})")

    # -- journal decisions per family -----------------------------------------
    conn = sqlite3.connect(str(JOURNAL_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts_close, action, side, entry, sl, tp, setup, label "
        "FROM decisions WHERE symbol=? AND ts_close LIKE ? ORDER BY id",
        (args.symbol, day + "%"),
    ).fetchall()
    dec_by_lane: dict[str, list] = {k: [] for k in LANES}
    for r in rows:
        label = str(r["label"] or "")
        for lane, cfg in LANES.items():
            if label.startswith(cfg["family"]):
                dec_by_lane[lane].append(r)

    # -- logs: blocks + intents per lane ---------------------------------------
    lines_out = [f"# Day ledger — {args.symbol} {day}", ""]
    index_bits = []
    for lane, cfg in LANES.items():
        decs = dec_by_lane[lane]
        enters = [r for r in decs if r["action"] == "enter"]
        realized = sum(_deal_pnl(d) for d in deals_by_lane[lane])
        n_deals = len(deals_by_lane[lane])
        lines_out += [f"## {lane} (`{cfg['family']}`)",
                      f"- decisions: {len(decs)} (enter {len(enters)})",
                      f"- broker deals: {n_deals}, realized **{realized:+.2f} USD**"]

        log_path = ROOT / "data" / "runtime" / cfg["log"]
        blocks: dict[str, list] = {}
        intents = {"set": 0, "entered": 0, "killed_break": 0, "expired": 0, "fill_deltas": []}
        if log_path.exists():
            try:
                for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if day not in line[:40]:
                        continue
                    mb = BLOCK_RE.search(line)
                    if mb:
                        blocks.setdefault(mb.group(2), []).append((line[:20], mb.group(1)))
                    mi = INTENT_RE.search(line)
                    if mi:
                        kind = mi.group(1)
                        intents[kind] = intents.get(kind, 0) + 1
                        if kind == "entered" and mi.group(3):
                            intents["fill_deltas"].append(_f(mi.group(3)))
            except OSError as exc:
                lines_out.append(f"- WARN log unreadable: {exc}")

        # counterfactuals: what would each BLOCKED enter have done (journaled
        # geometry, plain SL/TP, conservative)?
        if blocks:
            lines_out.append("- blocks (counterfactual R at journaled SL/TP, hold 48):")
            for reason, evs in sorted(blocks.items(), key=lambda kv: -len(kv[1])):
                cf_sum, cf_n = 0.0, 0
                for ts20, setup in evs:
                    match = next((r for r in enters
                                  if str(r["ts_close"])[:16] <= ts20[:16] and r["setup"] == setup
                                  and abs_minutes(str(r["ts_close"]), ts20) <= 6), None)
                    if match and day_bars:
                        cf = _counterfactual(_f(match["entry"]), _f(match["sl"]), _f(match["tp"]),
                                             str(match["side"] or "buy"), day_bars,
                                             str(match["ts_close"]))
                        if cf is not None:
                            cf_sum += cf
                            cf_n += 1
                cf_txt = f", cf {cf_sum:+.2f}R over {cf_n}" if cf_n else ""
                lines_out.append(f"    - `{reason}` ×{len(evs)}{cf_txt}")
        if intents["set"]:
            deltas = intents["fill_deltas"]
            d_txt = (f", fill_vs_level mean {sum(deltas)/len(deltas):+.3f}" if deltas else "")
            lines_out.append(
                f"- intents: set {intents['set']} / filled {intents['entered']} / "
                f"killed_break {intents['killed_break']} / expired {intents['expired']}{d_txt}")
        lines_out.append("")
        index_bits.append(f"{lane} {realized:+.2f}({n_deals})")

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"day_ledger_{day.replace('-', '')}.md"
    out.write_text("\n".join(lines_out), encoding="utf-8")
    with (REPORTS / "day_ledger_index.md").open("a", encoding="utf-8") as fh:
        fh.write(f"- {day}: " + " | ".join(index_bits) + "\n")
    print("\n".join(lines_out))
    print(f"\nwritten: {out}")
    return 0


def abs_minutes(ts_a: str, ts_b: str) -> float:
    try:
        a = datetime.fromisoformat(ts_a.replace("Z", "+00:00"))
        b = datetime.fromisoformat(ts_b.replace("Z", "+00:00").replace(" ", "T"))
        return abs((a - b).total_seconds()) / 60.0
    except ValueError:
        return 999.0


if __name__ == "__main__":
    raise SystemExit(main())
