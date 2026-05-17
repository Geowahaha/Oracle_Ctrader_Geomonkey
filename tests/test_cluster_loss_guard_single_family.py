"""Regression tests for the cluster-loss-guard single-family bleed branch
(2026-04-22 fix).

Background — live forensics from Apr 22, 2026:
  xauusd_scheduled:canary fired 6 consecutive SHORT trades into a +$50 rally,
  losing $28 across the day. The existing xau_cluster_loss_guard did NOT
  trigger because it required >=2 distinct families (MIN_DISTINCT_FAMILIES=2)
  and all 6 losses came from ONE family.

Fix: add an OR-branch "single-family bleed" trigger. When one family loses
the same direction many times for material pnl, the guard still activates
and blocks THAT direction — leaving the opposite direction and other
families free to trade (preserves profit-seeking opportunity).

Philosophy note: we explicitly keep this branch stricter than the
multi-family branch (3 losses / -$10 vs 2 losses / -$5) so it doesn't
over-block on normal noise.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


_HERE = os.path.dirname(__file__)
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Stub optional heavy deps so importing learning.trading_manager_agent works.
sys.modules.setdefault("yfinance", MagicMock())
sys.modules.setdefault("ccxt", MagicMock())


def _fresh_agent():
    """Build a minimally-constructed TradingManagerAgent bypassing __init__
    so we can unit-test the pure _derive method in isolation."""
    from learning.trading_manager_agent import TradingManagerAgent

    return TradingManagerAgent.__new__(TradingManagerAgent)


def _regime(*, direction: str, resolved: int, losses: int, pnl_usd: float,
            families: list[str], window_min: int = 12) -> dict:
    return {
        "active": True,
        "dominant_direction": direction,
        "window_min": window_min,
        "dominant_bucket": {
            "resolved": resolved,
            "losses": losses,
            "pnl_usd": pnl_usd,
            "families": list(families or []),
        },
    }


# ── Path A: multi-family cluster (legacy branch, must still work) ───────────

def test_multi_family_cluster_still_triggers():
    agent = _fresh_agent()
    regime = _regime(
        direction="short",
        resolved=4,
        losses=3,
        pnl_usd=-15.0,
        families=["xau_scalp_microtrend", "xau_scheduled_trend"],
    )
    out = agent._derive_xau_cluster_loss_guard_recommendation(micro_regime_refresh=regime)
    assert out.get("active") is True
    assert out.get("mode") == "same_side_cluster_loss_guard"
    assert out.get("blocked_direction") == "short"
    assert out.get("losses") == 3
    assert len(out.get("families") or []) == 2


# ── Path B: single-family bleed (the NEW fix) ───────────────────────────────

def test_single_family_bleed_triggers_apr22_scenario():
    """The exact Apr 22 scenario: one family (xauusd_scheduled:canary lane
    maps to xau_scheduled_trend) lost 6 SHORTs for -$28. With min_distinct=2
    the legacy branch skipped this — the new single-family branch MUST
    trigger."""
    agent = _fresh_agent()
    regime = _regime(
        direction="short",
        resolved=6,
        losses=6,
        pnl_usd=-28.0,
        families=["xau_scheduled_trend"],
    )
    out = agent._derive_xau_cluster_loss_guard_recommendation(micro_regime_refresh=regime)
    assert out.get("active") is True, (
        "single-family bleed (6 losses / -$28 / one family) must activate the "
        "guard — this is the exact Apr 22 live regression this fix protects."
    )
    assert out.get("mode") == "same_side_single_family_bleed_guard", (
        "mode label must distinguish single-family bleed from multi-family "
        "cluster so VM telemetry/audit can tell them apart."
    )
    assert out.get("blocked_direction") == "short"
    assert "xau_scheduled_trend" in (out.get("reason") or "")


def test_single_family_bleed_stricter_than_multi_family():
    """Stricter thresholds on the single-family branch: only 2 losses should
    NOT fire the single-family branch (even though 2 losses would fire the
    multi-family branch given 2 distinct families)."""
    agent = _fresh_agent()
    # 2 losses, one family, -$8 — below single-family threshold (3 losses,
    # -$10) and below multi-family distinct-family requirement.
    regime = _regime(
        direction="short",
        resolved=2,
        losses=2,
        pnl_usd=-8.0,
        families=["xau_scheduled_trend"],
    )
    out = agent._derive_xau_cluster_loss_guard_recommendation(micro_regime_refresh=regime)
    assert out == {}, (
        "single-family branch must NOT trigger on modest 2-loss bleed — "
        "stricter thresholds protect against over-blocking."
    )


def test_single_family_bleed_needs_material_pnl():
    """3 losses but only -$3 pnl should NOT trigger — the bleed must be
    materially costly before we pause a direction."""
    agent = _fresh_agent()
    regime = _regime(
        direction="short",
        resolved=3,
        losses=3,
        pnl_usd=-3.0,  # small losses (e.g. tight stops) — don't over-react
        families=["xau_scheduled_trend"],
    )
    out = agent._derive_xau_cluster_loss_guard_recommendation(micro_regime_refresh=regime)
    assert out == {}, (
        "3 small losses (-$3 total) should not trigger single-family guard — "
        "philosophy: we block only when the bleed is material."
    )


def test_inactive_regime_returns_empty():
    """If the micro-regime isn't active, the guard must stay silent no matter
    what the bucket looks like."""
    agent = _fresh_agent()
    regime = {
        "active": False,
        "dominant_direction": "short",
        "dominant_bucket": {
            "resolved": 6, "losses": 6, "pnl_usd": -28.0,
            "families": ["xau_scheduled_trend"],
        },
    }
    out = agent._derive_xau_cluster_loss_guard_recommendation(micro_regime_refresh=regime)
    assert out == {}


def test_guard_disabled_returns_empty():
    """With TRADING_MANAGER_XAU_CLUSTER_LOSS_GUARD_ENABLED=False the method
    must short-circuit even on a clear single-family bleed."""
    agent = _fresh_agent()
    regime = _regime(
        direction="short", resolved=6, losses=6, pnl_usd=-28.0,
        families=["xau_scheduled_trend"],
    )
    with patch("learning.trading_manager_agent.config",
               SimpleNamespace(TRADING_MANAGER_XAU_CLUSTER_LOSS_GUARD_ENABLED=False)):
        out = agent._derive_xau_cluster_loss_guard_recommendation(micro_regime_refresh=regime)
    assert out == {}


# ── Config defaults for the new branch ──────────────────────────────────────

def test_single_family_branch_config_defaults():
    """The two new config keys must have sensible defaults:
      - MIN_LOSSES=3 (one more than multi-family's 2, so stricter)
      - MAX_PNL_USD=-10 (double the multi-family's -5, so stricter)
    """
    from config import config as _cfg
    min_losses = int(getattr(
        _cfg, "TRADING_MANAGER_XAU_CLUSTER_LOSS_GUARD_SINGLE_FAMILY_MIN_LOSSES", 0
    ) or 0)
    max_pnl = float(getattr(
        _cfg, "TRADING_MANAGER_XAU_CLUSTER_LOSS_GUARD_SINGLE_FAMILY_MAX_PNL_USD", 0.0
    ) or 0.0)
    assert min_losses >= 3, (
        "single-family branch default MIN_LOSSES must be >=3 to avoid "
        "over-blocking on short 2-loss streaks."
    )
    assert max_pnl <= -10.0, (
        "single-family branch default MAX_PNL_USD must be <=-10 USD — the "
        "branch should only fire on material bleeding, not tiny stop losses."
    )
