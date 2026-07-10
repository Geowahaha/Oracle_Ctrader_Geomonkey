#!/usr/bin/env python3
"""Standalone cTrader OpenAPI token keepalive — safe for systemd timer."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# This service is the designated token-refresh owner (docs/DEXTER3_VM_MIGRATION_DESIGN.md
# P2 token-architecture fix): when DEXTER3_TOKEN_SINGLE_OWNER=1 is set, every
# other cTrader OpenAPI consumer (dexter-monitor's stream, dexter3 workers)
# becomes read-only and defers refreshing to this process. Self-identifying
# here is a no-op while single-owner mode is off (default), so this is safe
# to always set. Must happen before any api.ctrader_token_manager access.
os.environ.setdefault("CTRADER_TOKEN_IS_OWNER", "1")

from config import config  # noqa: E402
from infra.auth_health import refresh_stale_token_if_needed  # noqa: E402


def _alert_telegram_on_persistent_failure() -> bool:
    """Auth failures must be LOUD (2026-07-01..09 incident: refresh died
    silently for 9 days). When the persisted state shows >= 2 consecutive
    failures, push a Telegram alert to the owner. Best-effort, never raises;
    threshold 2 (not 1) so a single transient Spotware blip stays quiet."""
    try:
        from api.ctrader_token_manager import CTraderTokenManager

        state_path = CTraderTokenManager()._state_path()
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        fails = int(state.get("consecutive_failures", 0) or 0)
        if fails < 2:
            return False
        tok = str(getattr(config, "TELEGRAM_BOT_TOKEN", "") or "")
        chat = str(getattr(config, "TELEGRAM_CHAT_ID", "") or "")
        if not tok or not chat:
            return False
        import urllib.parse
        import urllib.request

        data = urllib.parse.urlencode({
            "chat_id": chat,
            "text": (
                f"🚨 cTrader TOKEN REFRESH failing x{fails} consecutive.\n"
                f"OpenAPI auth will die if this persists — run the re-auth "
                f"runbook (AGENT_SYNC_BOARD: token install sequence)."
            ),
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - alarm must never break the keepalive
        return False


def main() -> int:
    if not bool(getattr(config, "CTRADER_ENABLED", False)):
        print(json.dumps({"ok": True, "skipped": True, "reason": "ctrader_disabled"}))
        return 0
    keepalive_min = max(15, int(getattr(config, "CTRADER_TOKEN_KEEPALIVE_MIN", 30) or 30))
    meta = refresh_stale_token_if_needed(max_age_minutes=float(keepalive_min))
    out = {"ok": True, **meta}
    if meta.get("reason") == "refresh_failed":
        out["alerted"] = _alert_telegram_on_persistent_failure()
    print(json.dumps(out))
    return 0 if meta.get("reason") != "refresh_failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())