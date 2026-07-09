#!/usr/bin/env python3
"""Probe cTrader local MCP health; optional alert when hung (404 with port open).

Also verifies the ACTIVE trading account on every healthy probe (owner
directive 2026-07-09: cTrader must trade demo 9922808). The active account is
resolved via ``get_balance().traderId`` (skill quirk Q-L15: get_accounts_list
does not expose which account is active). On mismatch the watchdog shows an
in-app error popup so the owner sees it inside cTrader within one watchdog
cycle (schtask runs every 2 min) — order mutations are separately blocked by
the executor demo gate, so this is the VISIBILITY half of the guard.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ctrader_mcp_client import CtraderMcpClient

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "runtime" / "ctrader_mcp_watchdog.log"

# traderId(3555162) + login(9922808) of the required demo account.
DEFAULT_REQUIRED_TRADER_IDS = "9922808,3555162"


def _required_trader_ids() -> set[int]:
    raw = os.environ.get("CTRADER_REQUIRED_TRADER_IDS", DEFAULT_REQUIRED_TRADER_IDS)
    ids: set[int] = set()
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def check_account(client: CtraderMcpClient) -> dict:
    """Best-effort active-account verdict; never raises.

    ``account_ok`` is None (unknown) when the balance read fails or carries
    no traderId — the executor demo gate fail-closes on that same condition,
    so unknown here never means unguarded there."""
    try:
        balance = client.tool("get_balance") or {}
        trader_id_raw = balance.get("traderId")
        trader_id = int(trader_id_raw) if trader_id_raw is not None else None
    except Exception as exc:  # noqa: BLE001 - health probe must not die on this
        return {"account_ok": None, "account_error": str(exc)}
    if trader_id is None:
        return {"account_ok": None, "account_error": "traderId_missing"}
    required = _required_trader_ids()
    ok = (not required) or trader_id in required
    verdict: dict = {"account_ok": ok, "trader_id": trader_id}
    if not ok:
        verdict["account_alert"] = (
            f"ACTIVE ACCOUNT MISMATCH: traderId={trader_id} not in required "
            f"{sorted(required)} — switch cTrader to demo 9922808"
        )
        try:
            client.tool(
                "show_notification",
                {
                    "caption": "DEXTER ACCOUNT GUARD",
                    "description": (
                        "Wrong trading account active! Switch cTrader to demo "
                        "9922808. All Dexter entries are blocked until then."
                    ),
                    "type": "error",
                },
            )
            verdict["account_notified"] = True
        except Exception:  # noqa: BLE001
            verdict["account_notified"] = False
    return verdict


def append_log(payload: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(timezone.utc).isoformat()} {json.dumps(payload, ensure_ascii=False)}\n"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--restart", action="store_true", help="Restart cTrader if MCP unhealthy")
    parser.add_argument("--force-restart", action="store_true", help="Ignore restart cooldown")
    args = parser.parse_args()
    client = CtraderMcpClient(client_name="ctrader-mcp-watchdog", client_version="1.0")
    try:
        health = client.ping_health()
        health.update(check_account(client))
        if not args.quiet:
            print(json.dumps(health, indent=2))
        append_log({"ok": True, **health})
        return 0
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "hint": "Restart cTrader desktop app. MCP port may listen while handler returns 404.",
        }
        if args.restart:
            from ctrader_app_restart import restart_ctrader

            payload["restart"] = restart_ctrader(force=args.force_restart)
            payload["ok"] = bool(payload["restart"].get("ok"))
            if payload["ok"]:
                payload.pop("error", None)
                payload["hint"] = "cTrader restarted; MCP healthy"
        if not args.quiet:
            print(json.dumps(payload, indent=2))
        append_log(payload)
        return 0 if payload.get("ok") else 1
    finally:
        # SESSION HYGIENE (2026-07-09 root-cause fix): this watchdog runs every
        # 2 min from schtask and used to LEAK its MCP session every run — 645
        # sessions/day from this script alone. The cTrader plugin's session
        # table exhausts and 404s everything (the recurring "zombie"). Delete
        # our session on the way out; best-effort, never raises.
        try:
            client._delete_session()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())