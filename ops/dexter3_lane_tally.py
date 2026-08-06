#!/usr/bin/env python3
"""Per-lane per-day PnL tally from REAL broker deals (label-filtered).

The recurring proof tool for the two-lane mission: is each lane supplementing
or destroying? Run any time (read-only):

    python ops/dexter3_lane_tally.py            # all days in the deals window
    python ops/dexter3_lane_tally.py --today    # today (UTC) only
    python ops/dexter3_lane_tally.py --since 2026-07-10T16:55:00Z

Uses the DEXTER3_TRANSPORT factory (local_mcp on the PC, openapi on the VM) +
client-side day filtering — never pass from/to to get_deals directly (silently
ignored, see mcp_client docs). On the VM run with:
    DEXTER3_TRANSPORT=openapi DEXTER3_OPENAPI_DAEMON_URL=http://127.0.0.1:9877 \
        python ops/dexter3_lane_tally.py --today
Exit code 1 when --today is given and ANY lane is below --alert-net (default
-15), so schedulers/loops can alarm on it.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dexter3.executor import LABEL_FAMILY as FABLE_LABEL_FAMILY  # noqa: E402
from dexter3.executor import label_matches_family  # noqa: E402
from dexter3.transport import make_client  # noqa: E402
from dexter3.volume_profile import VP_LABEL_FAMILY  # noqa: E402

# Family root -> short display name (2026-07-15 versioned-labels design —
# bucket by FAMILY so each lane gets its own bucket instead of being lumped
# into "fable").
#
# 2026-07-25 audit fix: this listed ONLY grok/vp/fable, so dtr, dpull,
# dpull-cs, scalp and chf — five of the live lanes — all fell into "other" in
# the very tool this project calls "broker truth". A ledger that cannot name
# its own live lanes is a structural cause of the cherry-picked PnL prose the
# board later had to retract. Every current lane is now listed explicitly.
# grok was RETIRED 2026-07-17 and its lane code is being removed; its historical
# deals now bucket as "retired" rather than pretending it is a live lane.
#
# ORDER MATTERS for the longest-prefix cases: "dexter3:dpull-cs" must be tested
# before "dexter3:dpull". label_matches_family is boundary-aware as of the
# 2026-07-25 fix, but the explicit ordering keeps this correct even if a future
# label re-introduces an ambiguous pair.
_LANE_FAMILIES: tuple[tuple[str, str], ...] = (
    ("dexter3:dpull-cs", "dpull-cs"),
    ("dexter3:dpull", "dpull"),
    ("dexter3:dtr", "dtr"),
    ("dexter3:scalp", "scalp"),
    ("dexter3:chf", "chf"),
    ("dexter3:sniper-ustec", "sniper-ustec"),
    ("dexter3:mscalp-be", "mscalp-be"),
    ("dexter3:mscalp2", "mscalp2"),
    ("dexter3:mscalp", "mscalp"),
    ("dexter3:h3fade", "h3fade"),
    ("dexter3:sniper-us30", "sniper-us30"),
    ("dexter3:sniper", "sniper"),
    (VP_LABEL_FAMILY, "vp"),
    (FABLE_LABEL_FAMILY, "fable"),
    ("dexter3:grok", "retired"),
)


def _lane_family(label: str) -> str:
    """Short lane name for an exact broker label.

    Uses ``dexter3.executor.label_matches_family`` so the tally buckets exactly
    the way the live lanes claim ownership — before 2026-07-25 this was a bare
    ``.startswith()``, which bucketed "dexter3:dpull-cs:canary" as *dpull*.
    Falls back to "other" for a dexter3-prefixed label matching no known family
    rather than silently mis-bucketing it as fable.
    """
    for family_root, name in _LANE_FAMILIES:
        if label_matches_family(label, family_root):
            return name
    return "other"


def _parse_utc(raw: str) -> datetime:
    value = str(raw or "").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include UTC offset or Z")
    return parsed.astimezone(timezone.utc)


def _deal_time(deal: dict) -> datetime | None:
    raw = deal.get("time") or deal.get("executionTime") or deal.get("execution_time")
    try:
        return _parse_utc(str(raw))
    except (TypeError, ValueError):
        return None


def tally_deals(deals: list[dict], *, today: str | None = None, since: datetime | None = None) -> dict[tuple[str, str], list[float]]:
    """Return label-isolated lane/day PnLs from broker-normalized deals."""
    by_lane_day: dict[tuple[str, str], list[float]] = defaultdict(list)
    for d in deals:
        if not isinstance(d, dict):
            continue
        lbl = str(d.get("label") or "")
        if "dexter3" not in lbl:
            continue
        executed_at = _deal_time(d)
        if executed_at is None:
            continue
        day = executed_at.strftime("%Y-%m-%d")
        if today and day != today:
            continue
        if since and executed_at < since:
            continue
        pnl = d.get("netProfit", d.get("net_profit"))
        if pnl is None:
            pnl = d.get("grossProfit")
        try:
            pnl_value = float(pnl)
        except (TypeError, ValueError):
            continue
        lane = _lane_family(lbl)
        by_lane_day[(lane, day)].append(pnl_value)
    return by_lane_day


def tally_deals_by_full_label(
    deals: list[dict], *, today: str | None = None, since: datetime | None = None
) -> dict[tuple[str, str], list[float]]:
    """Same filtering as ``tally_deals`` but keyed by the EXACT broker label
    (not the family) — gives per-VERSION attribution within a family, so the
    owner can see e.g. how ``dexter3:fable:m5h-v1`` performed vs
    ``dexter3:fable:v1.7-selective-edge`` on the same day (2026-07-15
    versioned-labels design)."""
    by_label_day: dict[tuple[str, str], list[float]] = defaultdict(list)
    for d in deals:
        if not isinstance(d, dict):
            continue
        lbl = str(d.get("label") or "")
        if "dexter3" not in lbl:
            continue
        executed_at = _deal_time(d)
        if executed_at is None:
            continue
        day = executed_at.strftime("%Y-%m-%d")
        if today and day != today:
            continue
        if since and executed_at < since:
            continue
        pnl = d.get("netProfit", d.get("net_profit"))
        if pnl is None:
            pnl = d.get("grossProfit")
        try:
            pnl_value = float(pnl)
        except (TypeError, ValueError):
            continue
        by_label_day[(lbl, day)].append(pnl_value)
    return by_label_day


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=500)
    ap.add_argument("--today", action="store_true", help="today (UTC) only + alert exit code")
    ap.add_argument("--since", help="inclusive UTC ISO timestamp; excludes contaminated pre-fix deals")
    ap.add_argument("--alert-net", type=float, default=-15.0)
    args = ap.parse_args()

    c = make_client()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        since = _parse_utc(args.since) if args.since else None
    except ValueError as exc:
        ap.error(f"invalid --since: {exc}")

    # H1 fix (2026-07-15 cross-lane entanglement audit): get_deals is
    # count-paged (not from/to-windowed) on every transport, so a deal-heavy
    # lane can push the OTHER lane's earlier-today closes out of a shared
    # --count window before the client-side day/label filters in
    # tally_deals() ever see them. When the caller asked for a bounded window
    # (--since, or --today's UTC day start), thread that same lower bound
    # into get_deals so the OpenAPI/daemon transport narrows its OWN
    # broker-side query too; the local-MCP transport accepts-and-drops the
    # param (see Dexter3McpClient.get_deals) and keeps relying on
    # tally_deals()'s client-side filters as its correctness backstop.
    # Unbounded invocations (neither --since nor --today) keep the pre-fix
    # count-only behavior unchanged.
    from_timestamp_ms: int | None = None
    if since is not None:
        from_timestamp_ms = int(since.timestamp() * 1000)
    elif args.today:
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        from_timestamp_ms = int(day_start.timestamp() * 1000)

    deals = c.get_deals(count=args.count, from_timestamp_ms=from_timestamp_ms) or []
    by_lane_day = tally_deals(deals, today=today if args.today else None, since=since)

    alert = False
    for (lane, day), pnls in sorted(by_lane_day.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gw, gl = sum(wins), sum(losses)
        pf = (gw / abs(gl)) if gl else float("inf")
        net = sum(pnls)
        flag = ""
        if net <= args.alert_net:
            flag = "  <== ALERT below alert-net"
            alert = True
        print(
            f"{lane:6} {day} N={len(pnls):>3} net={net:>+8.2f} "
            f"W={len(wins):>3} L={len(losses):>3} "
            f"avgW={gw / max(1, len(wins)):+.2f} avgL={gl / max(1, len(losses)):+.2f} "
            f"PF={pf:.2f}{flag}"
        )
    if not by_lane_day:
        print("no dexter3-labeled deals in window")

    # Per-full-label sub-breakdown (2026-07-15 versioned-labels design): the
    # per-family view above answers "is this LANE supplementing or
    # destroying"; this answers "which VERSION of it" — direct per-code-
    # version attribution once a version bump changes the broker label.
    by_label_day = tally_deals_by_full_label(deals, today=today if args.today else None, since=since)
    if by_label_day:
        print("\n-- per-version breakdown (exact broker label) --")
        for (label, day), pnls in sorted(by_label_day.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            net = sum(pnls)
            print(f"{label:40} {day} N={len(pnls):>3} net={net:>+8.2f}")
    return 1 if (args.today and alert) else 0


if __name__ == "__main__":
    raise SystemExit(main())
