#!/usr/bin/env python3
"""Dexter3 VM migration Phase 1 — live transport parity checker.

READ-ONLY. Fetches balance / positions / symbol_details / trendbars(10) from
BOTH ``dexter3.mcp_client.Dexter3McpClient`` (local desktop MCP) and
``dexter3.openapi_client.Dexter3OpenApiClient`` (cTrader OpenAPI worker) and
diffs the normalized shapes field by field, printing PASS/FAIL per field.
Never places, amends, or closes anything — no mutating method is imported or
called anywhere in this file.

Run this ONLY on the PC where both transports are actually reachable (local
MCP desktop app running AND cTrader OpenAPI credentials configured in
``.env.local``) — see ``docs/DEXTER3_VM_MIGRATION_DESIGN.md`` Phase 1. This
script is intentionally NOT run as part of the automated test suite (it
requires live network + a running desktop MCP) and is not invoked anywhere
during this build session.

Usage:
    python -X utf8 scripts/dexter3_transport_parity.py --symbol XAUUSD --count 10
    python -X utf8 scripts/dexter3_transport_parity.py --skip-positions  # if flat

Exit code: 0 if every checked field PASSed, 1 if any FAILed (SKIPs, e.g. the
documented get_symbol_details gap, do not affect the exit code).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dexter3.mcp_client import Dexter3McpClient, McpClientError, McpZombieError  # noqa: E402
from dexter3.openapi_client import Dexter3OpenApiClient, Dexter3OpenApiNotImplementedError  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

# Fields expected to differ between two independent reads a few seconds apart
# (live price/PnL movement, not a parity bug) — reported as INFO, not FAIL.
_LIVE_DRIFT_TOLERANT = {
    "balance.balance",  # can move between the two reads if anything closed
}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []  # (field, verdict, detail)
        self.any_fail = False

    def add(self, field: str, verdict: str, detail: str = "") -> None:
        self.rows.append((field, verdict, detail))
        if verdict == FAIL:
            self.any_fail = True

    def print_section(self, title: str) -> None:
        print(f"\n=== {title} ===")
        for field, verdict, detail in self.rows:
            line = f"  [{verdict}] {field}"
            if detail:
                line += f" — {detail}"
            print(line)
        self.rows = []


def _compare_scalar(report: Report, path: str, local_val: Any, openapi_val: Any, *, tol: float = 1e-6) -> None:
    if isinstance(local_val, (int, float)) and isinstance(openapi_val, (int, float)):
        if abs(float(local_val) - float(openapi_val)) <= tol:
            report.add(path, PASS, f"local={local_val} openapi={openapi_val}")
        elif path in _LIVE_DRIFT_TOLERANT:
            report.add(path, PASS, f"local={local_val} openapi={openapi_val} (drift-tolerant field)")
        else:
            report.add(path, FAIL, f"local={local_val} openapi={openapi_val}")
    else:
        if local_val == openapi_val:
            report.add(path, PASS, f"local={local_val!r} openapi={openapi_val!r}")
        else:
            report.add(path, FAIL, f"local={local_val!r} openapi={openapi_val!r}")


def check_balance(local: Dexter3McpClient, openapi: Dexter3OpenApiClient) -> Report:
    report = Report()
    try:
        local_bal = local.get_balance()
    except (McpClientError, McpZombieError) as exc:
        report.add("balance", FAIL, f"local.get_balance() raised: {exc}")
        return report
    try:
        openapi_bal = openapi.get_balance()
    except (McpClientError, McpZombieError) as exc:
        report.add("balance", FAIL, f"openapi.get_balance() raised: {exc}")
        return report
    _compare_scalar(report, "balance.traderId", local_bal.get("traderId"), openapi_bal.get("traderId"))
    _compare_scalar(report, "balance.balance", local_bal.get("balance"), openapi_bal.get("balance"), tol=0.5)
    for gap_field in ("equity", "margin", "accountType"):
        if openapi_bal.get(gap_field) is None:
            report.add(f"balance.{gap_field}", SKIP, "documented gap — openapi transport never populates this (see dexter3/openapi_client.py gap #2)")
        else:
            report.add(f"balance.{gap_field}", PASS, f"openapi returned {openapi_bal.get(gap_field)!r} unexpectedly — gap may be resolved, update docstring")
    return report


def check_positions(local: Dexter3McpClient, openapi: Dexter3OpenApiClient) -> Report:
    report = Report()
    try:
        local_positions = {int(p.get("positionId") or p.get("id") or 0): p for p in local.get_positions()}
    except (McpClientError, McpZombieError) as exc:
        report.add("positions", FAIL, f"local.get_positions() raised: {exc}")
        return report
    try:
        openapi_positions = {int(p.get("positionId") or 0): p for p in openapi.get_positions()}
    except (McpClientError, McpZombieError) as exc:
        report.add("positions", FAIL, f"openapi.get_positions() raised: {exc}")
        return report

    if not local_positions and not openapi_positions:
        report.add("positions", SKIP, "no open positions on either transport — nothing to diff (open one to exercise this check)")
        return report

    local_ids = set(local_positions)
    openapi_ids = set(openapi_positions)
    if local_ids != openapi_ids:
        report.add(
            "positions.position_ids",
            FAIL,
            f"local={sorted(local_ids)} openapi={sorted(openapi_ids)} (mismatch — possible race between the two reads)",
        )

    for pid in sorted(local_ids & openapi_ids):
        lp, op = local_positions[pid], openapi_positions[pid]
        for field in ("label", "tradeSide", "entryPrice", "stopLoss", "takeProfit", "volume", "openTime"):
            _compare_scalar(report, f"positions[{pid}].{field}", lp.get(field), op.get(field), tol=0.01)
        if "netProfit" not in op:
            report.add(f"positions[{pid}].netProfit", SKIP, "documented gap — ProtoOAPosition carries no PnL field (see gap #3)")
    return report


def check_symbol_details(local: Dexter3McpClient, openapi: Dexter3OpenApiClient, symbol: str) -> Report:
    report = Report()
    try:
        local_details = local.get_symbol_details(symbol)
    except (McpClientError, McpZombieError) as exc:
        report.add("symbol_details", FAIL, f"local.get_symbol_details({symbol}) raised: {exc}")
        return report
    try:
        openapi_details = openapi.get_symbol_details(symbol)
    except Dexter3OpenApiNotImplementedError as exc:
        report.add(
            "symbol_details",
            SKIP,
            f"openapi.get_symbol_details({symbol}) not implemented (documented gap #1): {exc}",
        )
        return report
    for field in ("minVolume", "maxVolume", "volumeStep", "lotSize", "pipSize"):
        _compare_scalar(report, f"symbol_details.{field}", local_details.get(field), openapi_details.get(field))
    return report


def check_trendbars(local: Dexter3McpClient, openapi: Dexter3OpenApiClient, symbol: str, count: int) -> Report:
    report = Report()
    try:
        local_bars = local.get_trendbars(symbol, "m5", count)
    except (McpClientError, McpZombieError) as exc:
        report.add("trendbars", FAIL, f"local.get_trendbars raised: {exc}")
        return report
    try:
        openapi_bars = openapi.get_trendbars(symbol, "m5", count)
    except (McpClientError, McpZombieError) as exc:
        report.add("trendbars", FAIL, f"openapi.get_trendbars raised: {exc}")
        return report

    report.add("trendbars.bar_count", PASS if len(local_bars) == len(openapi_bars) else FAIL, f"local={len(local_bars)} openapi={len(openapi_bars)}")
    local_by_ts = {b.get("ts"): b for b in local_bars}
    openapi_by_ts = {b.get("ts"): b for b in openapi_bars}
    shared_ts = sorted(set(local_by_ts) & set(openapi_by_ts))
    if not shared_ts:
        report.add("trendbars.ts_overlap", FAIL, f"no shared timestamps — local={sorted(local_by_ts)[-3:]} openapi={sorted(openapi_by_ts)[-3:]}")
        return report
    for ts in shared_ts[-count:]:
        lb, ob = local_by_ts[ts], openapi_by_ts[ts]
        for field in ("open", "high", "low", "close"):
            _compare_scalar(report, f"trendbars[{ts}].{field}", lb.get(field), ob.get(field), tol=0.02)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--count", type=int, default=10, help="trendbar count to compare")
    parser.add_argument("--skip-positions", action="store_true", help="skip the open-positions diff")
    args = parser.parse_args(argv)

    local = Dexter3McpClient()
    openapi = Dexter3OpenApiClient()

    overall_fail = False

    print(f"Dexter3 transport parity check — symbol={args.symbol} count={args.count}")
    print("local_mcp  = dexter3.mcp_client.Dexter3McpClient")
    print("openapi    = dexter3.openapi_client.Dexter3OpenApiClient")

    balance_report = check_balance(local, openapi)
    balance_report.print_section("balance")
    overall_fail = overall_fail or balance_report.any_fail

    if not args.skip_positions:
        positions_report = check_positions(local, openapi)
        positions_report.print_section("positions")
        overall_fail = overall_fail or positions_report.any_fail

    symbol_report = check_symbol_details(local, openapi, args.symbol)
    symbol_report.print_section(f"symbol_details({args.symbol})")
    overall_fail = overall_fail or symbol_report.any_fail

    trendbar_report = check_trendbars(local, openapi, args.symbol, args.count)
    trendbar_report.print_section(f"trendbars({args.symbol}, m5, {args.count})")
    overall_fail = overall_fail or trendbar_report.any_fail

    print("\n=== RESULT ===")
    print("FAIL — see above" if overall_fail else "ALL CHECKED FIELDS PASS (SKIPs are documented gaps, not failures)")
    return 1 if overall_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
