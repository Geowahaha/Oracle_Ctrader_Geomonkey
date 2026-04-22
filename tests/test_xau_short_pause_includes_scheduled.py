"""Regression test for the directive-pause-families fix (2026-04-22).

Background — live forensics from Apr 22, 2026:
  When the trading-manager directive `live_range_transition_limit_pause`
  fired in response to "live short continuation degraded to
  reversal_exhaustion", it correctly blocked `scalp_xauusd:*` lanes but
  failed to block `xauusd_scheduled:*`. As a result, xauusd_scheduled:canary
  kept firing SHORTs into a $50 rally for 6 trades / -$28.

Root cause: the default for CTRADER_XAU_SHORT_LIMIT_PAUSE_FAMILIES did NOT
include `xau_scheduled_trend` (which is the family alias for the
`xauusd_scheduled` source per
`live_profile_autopilot._strategy_family_for_source`).

This test guards the default config to ensure scheduled is in the list.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock


_HERE = os.path.dirname(__file__)
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Stub heavy optional deps before importing config.
sys.modules.setdefault("yfinance", MagicMock())
sys.modules.setdefault("ccxt", MagicMock())


def test_short_limit_pause_families_default_includes_scheduled():
    """The default value must include xau_scheduled_trend so the trading
    manager directive covers the scheduled scanner alongside the scalp
    families."""
    # Strip any environment override to test the actual hard-coded default
    # in config.py.
    saved = os.environ.pop("CTRADER_XAU_SHORT_LIMIT_PAUSE_FAMILIES", None)
    try:
        # Force a fresh import so the default takes effect.
        for mod in list(sys.modules):
            if mod == "config" or mod.startswith("config."):
                del sys.modules[mod]
        from config import config as _cfg
        families_csv = str(getattr(_cfg, "CTRADER_XAU_SHORT_LIMIT_PAUSE_FAMILIES", "") or "")
        families = {f.strip().lower() for f in families_csv.split(",") if f.strip()}

        assert "xau_scheduled_trend" in families, (
            "CTRADER_XAU_SHORT_LIMIT_PAUSE_FAMILIES default must include "
            "xau_scheduled_trend so the directive's blocked_sources list "
            "covers xauusd_scheduled lanes too. Without this, the directive "
            "only pauses scalp_xauusd:* and the scheduled scanner keeps "
            "firing into the same losing direction."
        )
        # Spot-check that the scalp families are still there too.
        assert "xau_scalp_microtrend" in families
        assert "xau_scalp_tick_depth_filter" in families
    finally:
        if saved is not None:
            os.environ["CTRADER_XAU_SHORT_LIMIT_PAUSE_FAMILIES"] = saved
