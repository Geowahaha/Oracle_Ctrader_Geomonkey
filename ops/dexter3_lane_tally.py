#!/usr/bin/env python3
"""Per-lane per-day PnL tally from REAL broker deals (label-filtered).

The recurring proof tool for the two-lane mission: is each lane supplementing
or destroying? Run any time (read-only):

    python ops/dexter3_lane_tally.py            # all days in the deals window
    python ops/dexter3_lane_tally.py --today    # today (UTC) only

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

from dexter3.transport import make_client  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=500)
    ap.add_argument("--today", action="store_true", help="today (UTC) only + alert exit code")
    ap.add_argument("--alert-net", type=float, default=-15.0)
    args = ap.parse_args()

    c = make_client()
    deals = c.get_deals(count=args.count) or []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    by_lane_day: dict[tuple[str, str], list[float]] = defaultdict(list)
    for d in deals:
        lbl = str(d.get("label") or "")
        if "dexter3" not in lbl:
            continue
        day = str(d.get("time") or d.get("executionTime") or "")[:10]
        if args.today and day != today:
            continue
        pnl = d.get("netProfit")
        if pnl is None:
            pnl = d.get("grossProfit")
        if pnl is None:
            continue
        lane = "grok" if "grok" in lbl else "fable"
        by_lane_day[(lane, day)].append(float(pnl))

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
    return 1 if (args.today and alert) else 0


if __name__ == "__main__":
    raise SystemExit(main())
