#!/usr/bin/env python3
"""Zero-AI-token Telegram watcher for the Dexter3 lanes.

Replaces expensive in-session AI monitors (owner directive 2026-07-10):
tails the dexter3 shadow log + MCP watchdog log for CRITICAL events only and
pushes them to the owner's Telegram. Pure stdlib, no MCP, no AI. Patterns:

- governor day events:   TARGET_LOCKED / LOSS_STOP / house_money lock
- ratio-cap refusals:    min_volume_risk_exceeds_ratio_cap (Grok SL guard)
- account guard:         ACCOUNT GUARD / account_not_confirmed_demo
- V1.8 size levers:      v18-size: (first proofs, then throttled)
- MCP zombies:           "ok": false in the watchdog log

Throttle: per-pattern cooldown (default 300s) so a repeating condition sends
one message per window, not a flood. State (file offsets) kept in memory —
on restart it starts from the current end of each file (no replay spam).

Run detached via ops/dexter3_telegram_watcher.ps1 (lock:
data/runtime/dexter3_telegram_watcher.lock).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "data" / "runtime"
LOCK = RUNTIME / "dexter3_telegram_watcher.lock"
SELF_LOG = RUNTIME / "dexter3_telegram_watcher.log"

BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8536612154:AAGMbUo2mH45TSyWV1Eq22NX_-M_ZnlnPwA"
)
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1585019324")
POLL_SEC = float(os.environ.get("DEXTER3_TG_WATCH_POLL_SEC", "2"))
COOLDOWN_SEC = float(os.environ.get("DEXTER3_TG_WATCH_COOLDOWN_SEC", "300"))

WATCHES = [
    # (file, compiled pattern, pattern key)
    (RUNTIME / "dexter3_shadow.log", re.compile(r"TARGET_LOCKED|LOSS_STOP"), "governor"),
    (RUNTIME / "dexter3_shadow.log", re.compile(r"min_volume_risk_exceeds_ratio_cap"), "ratio_cap"),
    (RUNTIME / "dexter3_shadow.log", re.compile(r"ACCOUNT GUARD|account_not_confirmed_demo"), "account"),
    (RUNTIME / "dexter3_shadow.log", re.compile(r"v18-size:"), "v18_size"),
    (RUNTIME / "dexter3_shadow.log", re.compile(r"house_money"), "house_money"),
    (RUNTIME / "ctrader_mcp_watchdog.log", re.compile(r'"ok": false'), "mcp_zombie"),
]


def _log(msg: str) -> None:
    try:
        with SELF_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")
    except Exception:
        pass


def send_telegram(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        data = urllib.parse.urlencode(
            {"chat_id": CHAT_ID, "text": text[:3900], "disable_web_page_preview": "true"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:  # noqa: BLE001 - watcher must never die on send
        _log(f"send_failed: {exc}")
        return False


def main() -> int:
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    _log(f"watcher started pid={os.getpid()} poll={POLL_SEC}s cooldown={COOLDOWN_SEC}s")
    send_telegram(
        "🛰️ Dexter3 watcher ONLINE (zero-AI-token)\n"
        "เฝ้า: governor lock/stop, ratio-cap, account guard, v18-size, MCP zombie"
    )
    offsets: dict[Path, int] = {}
    last_sent: dict[str, float] = {}
    pending: dict[str, list[str]] = {}
    # start at end-of-file: no replay of history
    for path in {w[0] for w in WATCHES}:
        try:
            offsets[path] = path.stat().st_size
        except OSError:
            offsets[path] = 0

    while True:
        try:
            for path in {w[0] for w in WATCHES}:
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                start = offsets.get(path, 0)
                if size < start:  # truncated/rotated
                    start = 0
                if size == start:
                    continue
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(start)
                    chunk = f.read(min(size - start, 512_000))
                    offsets[path] = f.tell()
                for line in chunk.splitlines():
                    for wpath, pat, key in WATCHES:
                        if wpath == path and pat.search(line):
                            pending.setdefault(key, []).append(line.strip()[:300])
            now = time.time()
            for key, lines in list(pending.items()):
                if not lines:
                    continue
                if now - last_sent.get(key, 0.0) < COOLDOWN_SEC:
                    continue
                head = {"governor": "🎯 GOVERNOR", "ratio_cap": "🛡️ RATIO-CAP refusal",
                        "account": "🚨 ACCOUNT GUARD", "v18_size": "📈 V1.8 size lever",
                        "house_money": "💰 HOUSE MONEY", "mcp_zombie": "🧟 MCP zombie"}.get(key, key)
                body = "\n".join(lines[-5:])
                more = f"\n(+{len(lines) - 5} more)" if len(lines) > 5 else ""
                if send_telegram(f"{head}\n{body}{more}"):
                    last_sent[key] = now
                    pending[key] = []
                    _log(f"sent {key} x{len(lines)}")
        except Exception as exc:  # noqa: BLE001 - keep the watch alive
            _log(f"loop_error: {exc}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
