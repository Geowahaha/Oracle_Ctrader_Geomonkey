#!/usr/bin/env python3
"""Standalone cTrader window guardian (permanent off-screen-window fix).

WHY THIS EXISTS
---------------
On this machine cTrader repeatedly corrupts its own cached window placement:
``WINDOWPLACEMENT.ptMinPosition`` / ``rcNormalPosition`` land at the magic
off-screen offset ``-21333`` (confirmed 2026-07-07, -08, and again AFTER a
cTrader app-version update, so it is a persistent behaviour of the app on
this box, not a one-off). Two visible symptoms:
  * the window renders off-screen / only the taskbar icon shows;
  * the window opens small but VANISHES the moment the user maximizes it
    (cTrader's custom maximize control recomputes from the corrupt
    ptMinPosition).

The fix inside ``ctrader_app_restart.reposition_window_if_offscreen()`` was
only invoked from the MCP restart path, which has a 5-minute cooldown and
only fires when MCP goes zombie — so it missed the (common) case where
cTrader re-corrupts its placement while running normally. This guardian
decouples the fix from MCP entirely: run it on a short, independent
schedule (Task Scheduler, every 1 min, windowless via pythonw) so the
placement is kept healthy continuously and the maximize bug is pre-empted.

It reuses the SAME proven repair (``SetWindowPlacement`` on the real
title-bar window, virtual-screen bounds check, clean-minimize respected)
from ``ctrader_app_restart`` — no duplicated logic.

USAGE
-----
  python  scripts/ctrader_window_guardian.py            # one pass, exit (for Task Scheduler)
  python  scripts/ctrader_window_guardian.py --loop     # foreground watch loop (debug)
  python  scripts/ctrader_window_guardian.py --interval 30 --loop

Windowless install (no console flashes) is done by the setup helper
``scripts/install_ctrader_window_guardian.py`` / the schtasks command in the
handoff. This script itself never opens a console child.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = ROOT / "data" / "runtime" / "ctrader_window_guardian.log"

if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _log(payload: dict) -> None:
    # Only log when something actually happened (a correction) or on error —
    # a healthy no-op every minute would bloat the file. Keeps forensics
    # useful: every line in this log is a real corruption event we caught.
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now(timezone.utc).isoformat()} {json.dumps(payload, ensure_ascii=False)}\n"
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001 - logging must never crash the guardian
        pass


def run_once() -> dict:
    try:
        from ctrader_app_restart import reposition_window_if_offscreen
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "reason": f"import_failed:{exc}"}
        _log(result)
        return result

    # Short internal wait budget: this runs every ~60s, so don't sit here
    # for 12x2s if the window isn't found — one quick look is enough.
    result = reposition_window_if_offscreen(attempts=2, delay_sec=1.0)
    action = result.get("action")
    if action == "placement_corrected" or not result.get("ok"):
        _log(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="cTrader off-screen-window guardian")
    parser.add_argument("--loop", action="store_true", help="run continuously (debug); default is one pass then exit")
    parser.add_argument("--interval", type=int, default=60, help="seconds between checks in --loop mode")
    parser.add_argument("--quiet", action="store_true", help="suppress stdout JSON")
    args = parser.parse_args()

    if not args.loop:
        result = run_once()
        if not args.quiet:
            print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    while True:
        result = run_once()
        if not args.quiet and result.get("action") == "placement_corrected":
            print(json.dumps(result, ensure_ascii=False))
        time.sleep(max(5, int(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
