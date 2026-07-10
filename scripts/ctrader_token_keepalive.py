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


def main() -> int:
    if not bool(getattr(config, "CTRADER_ENABLED", False)):
        print(json.dumps({"ok": True, "skipped": True, "reason": "ctrader_disabled"}))
        return 0
    keepalive_min = max(15, int(getattr(config, "CTRADER_TOKEN_KEEPALIVE_MIN", 30) or 30))
    meta = refresh_stale_token_if_needed(max_age_minutes=float(keepalive_min))
    out = {"ok": True, **meta}
    print(json.dumps(out))
    return 0 if meta.get("reason") != "refresh_failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())