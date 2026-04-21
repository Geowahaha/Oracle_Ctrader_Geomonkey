"""
ops/analyze_xau_suppression.py — Offline analyzer for XAU high-confidence
suppression shadow data.

Goal
----
Turn `data/runtime/xau_conf_suppression_shadow.jsonl` (+ rotated copies)
into a decision-grade report answering: is winner-logic confidence
suppression killing profitable XAU opportunity?

Design invariants
-----------------
- Read-only. Opens ctrader_openapi.db via `mode=ro&uri=true`.
- Never writes to any DB, runtime state, or shadow log.
- Never imports or calls live-trading code paths.
- stdlib only.

Join strategy (shadow event → ctrader_deals)
--------------------------------------------
1. Primary key: (signal_run_id, signal_run_no) where both sides populated.
2. Fallback: (signal_run_no, symbol, direction) within a ±N-minute window
   around the shadow event's ts_utc, picking the closest deal.
3. Shadow events with no matching deal are counted as "unrealized" —
   they still contribute to arrived/rejected breakdowns but not pnl stats.

Outputs
-------
- Per-regime report: arrived / executed / rejected / win_rate_executed /
  avg_pnl_usd_executed, bucketed by raw.winner_logic_regime.
- Severe-regime focus: rejection-reason breakdown, plus the subset that
  got through — their win rate and avg pnl (the core question).
- Top normalized rejection reasons overall.
- Optional JSON dump for downstream tooling.

Usage
-----
    python ops/analyze_xau_suppression.py
    python ops/analyze_xau_suppression.py --since 2026-04-10 --until 2026-04-21
    python ops/analyze_xau_suppression.py --format json --top-n 15
    python ops/analyze_xau_suppression.py --fallback-window-min 3

Exit codes: 0 on success, non-zero only on IO/usage errors (empty input
is not an error — the report states "no events").
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Optional

logger = logging.getLogger("analyze_xau_suppression")

# ---------------------------------------------------------------------------
# Defaults (overridable via CLI)
# ---------------------------------------------------------------------------

DEFAULT_SHADOW_GLOB = "data/runtime/xau_conf_suppression_shadow*.jsonl"
DEFAULT_DB_PATH = "data/ctrader_openapi.db"
DEFAULT_FALLBACK_WINDOW_MIN = 5.0
DEFAULT_TOP_N = 10

# Normalization families for rejection reasons. Ordered by specificity:
# first matching prefix wins.
REJECTION_FAMILIES: list[tuple[str, tuple[str, ...]]] = [
    ("winner_hard_block", ("winner_hard_block", "winner_severe", "winner_block")),
    ("health_gate_blocked", ("health_gate_blocked", "health_critical", "health_warning")),
    ("source_not_allowed", ("source_not_allowed",)),
    ("symbol_not_allowed", ("symbol_not_allowed",)),
    ("governance_blocked", ("governance_blocked", "governance_")),
    ("disabled", ("disabled", "ctrader_disabled", "sdk_unavailable")),
    ("pattern_filtered", ("test_pattern_filtered", "pattern_filtered", "pattern_not_allowed")),
    ("risk_blocked", ("risk_blocked", "max_positions", "max_risk")),
    ("direction_blocked", ("direction_blocked", "opposite_direction")),
    ("duplicate", ("duplicate", "already_open", "repeat_guard")),
]

# Regime buckets we report on. "none" captures missing/empty regime.
KNOWN_REGIMES = ("strong", "weak", "severe", "none")


# ---------------------------------------------------------------------------
# Shadow-event loading
# ---------------------------------------------------------------------------


def _parse_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
    return None


def _iter_shadow_files(pattern: str) -> Iterable[Path]:
    for p in sorted(glob.glob(pattern)):
        path = Path(p)
        if path.is_file():
            yield path


def load_shadow_events(
    glob_pattern: str,
    since: Optional[datetime],
    until: Optional[datetime],
) -> list[dict]:
    """Load + parse all shadow JSONL lines within [since, until]."""
    events: list[dict] = []
    for path in _iter_shadow_files(glob_pattern):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = _parse_utc(rec.get("ts_utc"))
                    if ts is None:
                        continue
                    if since is not None and ts < since:
                        continue
                    if until is not None and ts > until:
                        continue
                    rec["_ts"] = ts
                    events.append(rec)
        except OSError as exc:
            logger.warning("skip shadow file %s: %s", path, exc)
    events.sort(key=lambda r: r["_ts"])
    return events


# ---------------------------------------------------------------------------
# Deals loading
# ---------------------------------------------------------------------------


def load_deals(
    db_path: str,
    since: Optional[datetime],
    until: Optional[datetime],
) -> list[dict]:
    """Load XAU deals from ctrader_deals (read-only)."""
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        logger.error("cannot open DB: %s", exc)
        return []
    try:
        conn.row_factory = sqlite3.Row
        pad = timedelta(minutes=30)
        since_s = (since - pad).isoformat() if since else None
        until_s = (until + pad).isoformat() if until else None
        sql = (
            "SELECT deal_id, position_id, symbol, direction, pnl_usd, outcome, "
            "signal_run_id, signal_run_no, execution_utc, source, lane "
            "FROM ctrader_deals "
            "WHERE UPPER(COALESCE(symbol,'')) IN ('XAUUSD','GOLD','XAU')"
        )
        params: list[Any] = []
        if since_s:
            sql += " AND execution_utc >= ?"
            params.append(since_s)
        if until_s:
            sql += " AND execution_utc <= ?"
            params.append(until_s)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    deals: list[dict] = []
    for r in rows:
        ts = _parse_utc(r["execution_utc"])
        if ts is None:
            continue
        deals.append({
            "deal_id": r["deal_id"],
            "position_id": r["position_id"],
            "symbol": str(r["symbol"] or "").upper(),
            "direction": str(r["direction"] or "").lower(),
            "pnl_usd": r["pnl_usd"],
            "outcome": r["outcome"],
            "signal_run_id": str(r["signal_run_id"] or ""),
            "signal_run_no": int(r["signal_run_no"] or 0),
            "source": str(r["source"] or ""),
            "lane": str(r["lane"] or ""),
            "_ts": ts,
        })
    return deals


# ---------------------------------------------------------------------------
# Join shadow → deals
# ---------------------------------------------------------------------------


def build_deal_indexes(deals: list[dict]) -> tuple[dict, dict]:
    """Build two lookup indexes for join."""
    primary: dict[tuple[str, int], list[dict]] = defaultdict(list)
    fallback: dict[tuple[int, str, str], list[dict]] = defaultdict(list)
    for d in deals:
        rid = d["signal_run_id"]
        rno = d["signal_run_no"]
        if rid and rno:
            primary[(rid, rno)].append(d)
        if rno:
            fallback[(rno, d["symbol"], d["direction"])].append(d)
    return primary, fallback


def match_deal(
    event: dict,
    primary: dict,
    fallback: dict,
    window_min: float,
) -> Optional[dict]:
    rid = str(event.get("signal_run_id") or "")
    rno = int(event.get("signal_run_no") or 0)
    sym = str(event.get("symbol") or "").upper()
    direction = str(event.get("direction") or "").lower()
    ev_ts: datetime = event["_ts"]

    if rid and rno:
        candidates = primary.get((rid, rno), [])
        if candidates:
            return min(candidates, key=lambda d: abs((d["_ts"] - ev_ts).total_seconds()))

    if rno and sym and direction:
        candidates = fallback.get((rno, sym, direction), [])
        if candidates:
            window = timedelta(minutes=window_min)
            within = [d for d in candidates if abs(d["_ts"] - ev_ts) <= window]
            if within:
                return min(within, key=lambda d: abs((d["_ts"] - ev_ts).total_seconds()))
    return None


# ---------------------------------------------------------------------------
# Rejection-reason normalization
# ---------------------------------------------------------------------------


def normalize_rejection(reason: str) -> str:
    """Map a raw rejection reason to a coarse family name."""
    r = (reason or "").strip().lower()
    if not r:
        return "unknown"
    head = r.split(":", 1)[0]
    body = r
    for family, prefixes in REJECTION_FAMILIES:
        for p in prefixes:
            if head == p or body.startswith(p):
                return family
    return "other"


def regime_of(event: dict) -> str:
    raw = event.get("raw") or {}
    reg = str(raw.get("winner_logic_regime") or "").strip().lower()
    if reg in KNOWN_REGIMES:
        return reg
    return "none"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _outcome_is_win(outcome: Any) -> Optional[bool]:
    if outcome is None:
        return None
    try:
        return int(outcome) == 1
    except (TypeError, ValueError):
        return None


def aggregate(
    events: list[dict],
    primary: dict,
    fallback: dict,
    window_min: float,
) -> dict:
    """Compute per-regime and severe-focus aggregates."""

    per_regime = {
        r: {
            "arrived": 0,
            "executed": 0,
            "rejected": 0,
            "health_warn": 0,
            "wins": 0,
            "losses": 0,
            "open": 0,
            "pnl_sum": 0.0,
            "pnl_count": 0,
            "pnl_values": [],
            "rejection_reasons": defaultdict(int),
            "rejection_reasons_raw": defaultdict(int),
        }
        for r in KNOWN_REGIMES
    }

    severe_rejected_sample: list[dict] = []
    severe_executed_sample: list[dict] = []
    overall_rejection = defaultdict(int)

    # Collapse multiple events per signal: a single signal may log "arrived"
    # then later "executed" or "rejected". We treat each event as its own
    # row so counts add up as: arrived >= executed + rejected. That is OK —
    # the report exposes both counts explicitly.
    for ev in events:
        decision = str(ev.get("decision") or "").lower()
        reg = regime_of(ev)
        bucket = per_regime[reg]
        deal = match_deal(ev, primary, fallback, window_min)

        if decision == "arrived":
            bucket["arrived"] += 1
        elif decision == "executed":
            bucket["executed"] += 1
            win = _outcome_is_win(deal["outcome"]) if deal else None
            if deal is not None and deal.get("pnl_usd") is not None:
                try:
                    p = float(deal["pnl_usd"])
                    bucket["pnl_sum"] += p
                    bucket["pnl_count"] += 1
                    bucket["pnl_values"].append(p)
                except (TypeError, ValueError):
                    pass
            if win is True:
                bucket["wins"] += 1
            elif win is False:
                bucket["losses"] += 1
            else:
                bucket["open"] += 1
            if reg == "severe" and len(severe_executed_sample) < 25:
                severe_executed_sample.append({
                    "ts_utc": ev.get("ts_utc"),
                    "direction": ev.get("direction"),
                    "confidence": ev.get("confidence"),
                    "signal_run_id": ev.get("signal_run_id"),
                    "signal_run_no": ev.get("signal_run_no"),
                    "pnl_usd": deal.get("pnl_usd") if deal else None,
                    "outcome": deal.get("outcome") if deal else None,
                })
        elif decision == "rejected":
            bucket["rejected"] += 1
            raw_reason = str(ev.get("reason") or "")
            fam = normalize_rejection(raw_reason)
            bucket["rejection_reasons"][fam] += 1
            bucket["rejection_reasons_raw"][raw_reason] += 1
            overall_rejection[fam] += 1
            if reg == "severe" and len(severe_rejected_sample) < 25:
                severe_rejected_sample.append({
                    "ts_utc": ev.get("ts_utc"),
                    "direction": ev.get("direction"),
                    "confidence": ev.get("confidence"),
                    "reason_raw": raw_reason,
                    "reason_family": fam,
                    "signal_run_id": ev.get("signal_run_id"),
                    "signal_run_no": ev.get("signal_run_no"),
                })
        elif decision == "health_warn":
            bucket["health_warn"] += 1

    # Derived stats per regime
    summary = {}
    for reg, b in per_regime.items():
        decided = b["wins"] + b["losses"]
        wr = (b["wins"] / decided) if decided else None
        avg_pnl = (b["pnl_sum"] / b["pnl_count"]) if b["pnl_count"] else None
        summary[reg] = {
            "arrived": b["arrived"],
            "executed": b["executed"],
            "rejected": b["rejected"],
            "health_warn": b["health_warn"],
            "wins": b["wins"],
            "losses": b["losses"],
            "open": b["open"],
            "win_rate_executed": wr,
            "avg_pnl_usd_executed": avg_pnl,
            "total_pnl_usd_executed": b["pnl_sum"] if b["pnl_count"] else None,
            "rejection_reasons_top": dict(
                sorted(b["rejection_reasons"].items(), key=lambda kv: -kv[1])
            ),
        }

    return {
        "per_regime": summary,
        "overall_rejection_families": dict(
            sorted(overall_rejection.items(), key=lambda kv: -kv[1])
        ),
        "severe_rejected_sample": severe_rejected_sample,
        "severe_executed_sample": severe_executed_sample,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_pct(v: Optional[float]) -> str:
    return "   n/a" if v is None else f"{v * 100:5.1f}%"


def _fmt_usd(v: Optional[float]) -> str:
    return "      n/a" if v is None else f"{v:+9.2f}"


def render_text(report: dict, meta: dict, top_n: int) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("XAU High-Confidence Suppression -- Shadow Analysis")
    lines.append("=" * 78)
    lines.append(f"  Shadow events loaded : {meta['events_total']}")
    lines.append(f"  Window               : {meta.get('since') or '-'} -> {meta.get('until') or '-'}")
    lines.append(f"  Deals considered     : {meta['deals_total']}")
    lines.append(f"  Matched events/deals : {meta['matched_events']}")
    lines.append(f"  Fallback window      : {meta['fallback_window_min']:.1f} min")
    lines.append("")

    lines.append("Per-regime decision funnel + realized outcomes")
    lines.append("-" * 78)
    hdr = f"  {'regime':8s} {'arr':>5s} {'exec':>5s} {'rej':>5s} {'warn':>5s} {'W':>4s} {'L':>4s} {'O':>4s} {'WR':>6s} {'avgPnL':>10s}"
    lines.append(hdr)
    for reg in KNOWN_REGIMES:
        s = report["per_regime"][reg]
        lines.append(
            f"  {reg:8s} {s['arrived']:5d} {s['executed']:5d} {s['rejected']:5d} "
            f"{s['health_warn']:5d} {s['wins']:4d} {s['losses']:4d} {s['open']:4d} "
            f"{_fmt_pct(s['win_rate_executed'])} {_fmt_usd(s['avg_pnl_usd_executed'])}"
        )
    lines.append("")

    lines.append("Overall rejection families (top)")
    lines.append("-" * 78)
    fams = list(report["overall_rejection_families"].items())[:top_n]
    if not fams:
        lines.append("  (no rejections recorded)")
    for name, count in fams:
        lines.append(f"  {name:30s} {count:6d}")
    lines.append("")

    lines.append("Severe-regime focus -- the core question")
    lines.append("-" * 78)
    sev = report["per_regime"]["severe"]
    lines.append(f"  arrived   : {sev['arrived']}")
    lines.append(f"  rejected  : {sev['rejected']}  (what suppression rejected)")
    lines.append(f"  executed  : {sev['executed']}  (slipped through suppression)")
    if sev["win_rate_executed"] is not None:
        lines.append(
            f"  executed-win-rate : {_fmt_pct(sev['win_rate_executed'])}  "
            f"(if high, suppression was wrong to block similar signals)"
        )
        lines.append(
            f"  executed-avg-pnl  : {_fmt_usd(sev['avg_pnl_usd_executed'])} USD"
        )
    else:
        lines.append("  executed-win-rate : no closed deals yet")

    lines.append("")
    lines.append("  Severe rejection-family breakdown:")
    for name, count in list(sev["rejection_reasons_top"].items())[:top_n]:
        lines.append(f"    {name:30s} {count:6d}")

    if report["severe_executed_sample"]:
        lines.append("")
        lines.append("  Sample severe-regime executions (up to 25):")
        for s in report["severe_executed_sample"]:
            pnl = s.get("pnl_usd")
            oc = s.get("outcome")
            oc_lbl = "win" if oc == 1 else "loss" if oc == 0 else "open"
            pnl_s = f"{pnl:+8.2f}" if isinstance(pnl, (int, float)) else "    n/a"
            lines.append(
                f"    {s.get('ts_utc')}  {str(s.get('direction')):5s} "
                f"conf={s.get('confidence')}  pnl={pnl_s}  {oc_lbl}"
            )

    if report["severe_rejected_sample"]:
        lines.append("")
        lines.append("  Sample severe-regime rejections (up to 25):")
        for s in report["severe_rejected_sample"]:
            lines.append(
                f"    {s.get('ts_utc')}  {str(s.get('direction')):5s} "
                f"conf={s.get('confidence')}  {s.get('reason_family')} "
                f"[{s.get('reason_raw')}]"
            )

    lines.append("")
    lines.append("=" * 78)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyze XAU suppression shadow log.")
    p.add_argument("--shadow-glob", default=DEFAULT_SHADOW_GLOB,
                   help=f"glob pattern for shadow JSONL (default: {DEFAULT_SHADOW_GLOB})")
    p.add_argument("--db", default=DEFAULT_DB_PATH,
                   help=f"ctrader DB path (default: {DEFAULT_DB_PATH})")
    p.add_argument("--since", default=None,
                   help="ISO-8601 lower bound (inclusive), e.g. 2026-04-10")
    p.add_argument("--until", default=None,
                   help="ISO-8601 upper bound (inclusive)")
    p.add_argument("--fallback-window-min", type=float,
                   default=DEFAULT_FALLBACK_WINDOW_MIN,
                   help=f"time window for fallback join (default: {DEFAULT_FALLBACK_WINDOW_MIN})")
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                   help=f"top-N rejection families to print (default: {DEFAULT_TOP_N})")
    p.add_argument("--format", choices=("text", "json"), default="text")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    since = _parse_utc(args.since) if args.since else None
    until = _parse_utc(args.until) if args.until else None

    events = load_shadow_events(args.shadow_glob, since, until)
    deals = load_deals(args.db, since, until)
    primary, fallback = build_deal_indexes(deals)

    # Match count for the meta header (events → deals, arrived-or-executed only).
    matched = 0
    for ev in events:
        if str(ev.get("decision") or "") in ("executed", "arrived"):
            if match_deal(ev, primary, fallback, args.fallback_window_min) is not None:
                matched += 1

    report = aggregate(events, primary, fallback, args.fallback_window_min)
    meta = {
        "events_total": len(events),
        "deals_total": len(deals),
        "matched_events": matched,
        "fallback_window_min": float(args.fallback_window_min),
        "since": args.since,
        "until": args.until,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }

    if args.format == "json":
        out = {"meta": meta, "report": report}
        print(json.dumps(out, indent=2, default=str))
    else:
        print(render_text(report, meta, args.top_n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
