"""Daily Mission Governor tests — owner directive 2026-07-07 ($100/day on $1000)."""
from __future__ import annotations

import pytest

from dexter3.daily_governor import DailyGovernor, GovernorConfig


def gov(**kw) -> DailyGovernor:
    return DailyGovernor(GovernorConfig(**kw))


# -- status transitions --------------------------------------------------------


def test_hunting_below_target_and_above_cap():
    s = gov().status(day_pnl_usd=40.0, floating_pnl_usd=10.0)
    assert s["state"] == "HUNTING"


def test_target_lock_on_realized():
    s = gov().status(day_pnl_usd=105.0, floating_pnl_usd=0.0)
    assert s["state"] == "TARGET_LOCKED"


def test_floating_crosses_target_triggers_lock():
    # a floating winner counts toward the mission — lock-and-realize
    s = gov().status(day_pnl_usd=70.0, floating_pnl_usd=35.0)
    assert s["state"] == "TARGET_LOCKED"
    assert s["effective_pnl"] == pytest.approx(105.0)


def test_floating_crosses_loss_cap_triggers_stop():
    # an unrealized loser must NOT be exempt from the daily cap
    s = gov().status(day_pnl_usd=-20.0, floating_pnl_usd=-35.0)
    assert s["state"] == "LOSS_STOPPED"


def test_exact_boundaries():
    assert gov().status(100.0, 0.0)["state"] == "TARGET_LOCKED"
    assert gov().status(-50.0, 0.0)["state"] == "LOSS_STOPPED"


# -- ladder / sizing -----------------------------------------------------------


def test_ladder_presses_win_streaks_and_caps():
    g = gov()  # capital 1000, base 1.2% = $12, max 2.5% = $25
    r0 = g.risk_for_entry("london", 0)["risk_usd"]
    r1 = g.risk_for_entry("london", 1)["risk_usd"]
    r3 = g.risk_for_entry("london", 3)["risk_usd"]
    r9 = g.risk_for_entry("london", 9)["risk_usd"]  # beyond ladder end
    assert r0 == pytest.approx(12.0)
    assert r1 == pytest.approx(12.0 * 1.3)
    assert r3 == pytest.approx(min(12.0 * 2.0, 25.0))
    assert r9 == r3  # ladder saturates, never grows unbounded


def test_hard_risk_cap_and_floor():
    g = gov(base_risk_frac=0.02)  # $20 base; ladder 2.0 → $40 would exceed $25 cap
    assert g.risk_for_entry("overlap", 3)["risk_usd"] <= 25.0 + 1e-9
    tiny = gov(capital_usd=10.0)  # base $0.12 → floored at $1
    assert tiny.risk_for_entry("london", 0)["risk_usd"] >= 1.0


def test_session_multiplier_shapes_size():
    g = gov()
    asian = g.risk_for_entry("asian", 0)["risk_usd"]
    overlap = g.risk_for_entry("overlap", 0)["risk_usd"]
    unknown = g.risk_for_entry("weird_tag", 0)["risk_usd"]
    assert asian < overlap
    assert unknown == pytest.approx(12.0 * g.config.session_mult.get("unknown", 0.8))


# -- high-conviction bypass (2026-07-22 owner audit) ----------------------------


def test_bypass_disabled_by_default():
    g = gov()  # bypass_min_score defaults to None -> feature OFF
    assert g.bypass_allowed(0.99) is False


def test_bypass_requires_a_real_score_even_when_threshold_configured():
    g = gov(bypass_min_score=0.74)
    assert g.bypass_allowed(None) is False


def test_bypass_allows_when_score_clears_threshold():
    g = gov(bypass_min_score=0.74)
    assert g.bypass_allowed(0.80) is True


def test_bypass_boundary_is_inclusive():
    g = gov(bypass_min_score=0.74)
    assert g.bypass_allowed(0.74) is True


def test_bypass_refuses_when_score_below_threshold():
    g = gov(bypass_min_score=0.74)
    assert g.bypass_allowed(0.50) is False


def test_bypass_is_pure_no_side_effects():
    g = gov(bypass_min_score=0.74)
    g.bypass_allowed(0.80)
    g.bypass_allowed(0.10)
    # calling it never mutates config or holds state between calls
    assert g.config.bypass_min_score == pytest.approx(0.74)
    assert g.bypass_allowed(0.80) is True


# -- streak derivation (stateless from today's ordered closes) ------------------


@pytest.mark.parametrize(
    "pnls,expect",
    [
        ([], 0),
        ([5.0], 1),
        ([5.0, -2.0], 0),
        ([-2.0, 5.0, 6.0], 2),
        ([5.0, 5.0, -1.0, 3.0, 4.0, 2.0], 3),
        ([0.0, 5.0], 1),  # zero is not a win; trailing win run = 1
    ],
)
def test_win_streak_from_closes(pnls, expect):
    assert gov().win_streak_from_closes(pnls) == expect


# -- governor is a layer ABOVE: it holds no reference to cap configs ------------


def test_governor_cannot_touch_caps():
    g = gov()
    for attr in ("basket_config", "executor_config", "max_legs", "allow_basket_legs"):
        assert not hasattr(g, attr)
    assert not hasattr(g.config, "max_legs")


# -- executor risk override plumbing --------------------------------------------


def test_risk_override_reaches_sizing(tmp_path):
    from dexter3.decision_journal import DecisionJournal
    from dexter3.executor import Dexter3Executor, ExecutorConfig

    class FakeMcp:
        def __init__(self):
            self.calls = []
            self.placed = {}

        def get_symbol_details(self, s):
            return {"minVolume": 1.0, "maxVolume": 100.0, "volumeStep": 1.0, "lotSize": 100.0, "pipSize": 0.01}

        def get_spot_price(self, s):
            return {"bid": 4150.0, "ask": 4150.2}

        def get_positions(self):
            if self.placed:
                return [dict(self.placed)]
            return []

        def get_balance(self):
            return {"traderId": 9922808, "balance": 10000.0}

        def place_market_order(self, **kw):
            self.calls.append(kw)
            self.placed = {
                "positionId": 42, "symbolName": "XAUUSD", "tradeSide": kw["side"].capitalize(),
                "volumeInUnits": kw["volume"], "stopLoss": 4147.0, "takeProfit": 4156.0,
                "label": "dexter3:fable:m5h-v1",
            }
            return {"dealStatus": "FILLED"}

        def amend_position(self, *a, **k):
            return {"status": "ok"}

        def close_position(self, pid):
            return {"status": "closed"}

    class D:
        symbol = "XAUUSD"; action = "enter"; side = "buy"; entry_type = "market"
        entry = 4150.2; sl = 4147.2; tp = 4156.2; size_class = "small"; setup = "hunt_test"
        ts_close = "2026-07-07T12:00:00Z"; reasons = ["t"]; features = {}; leader_score = 0.5; p_win_est = 0.5

        def to_dict(self):
            return {}

    mcp = FakeMcp()
    j = DecisionJournal(tmp_path / "gov_exec.db")
    # max_volume must allow the larger size so the override is observable
    ex = Dexter3Executor(mcp, j, ExecutorConfig(risk_usd=3.0, max_volume_units=20.0))
    out_small = ex.execute_entry(D(), {"traderId": 9922808})
    vol_small = mcp.calls[-1]["volume"]
    mcp.placed = {}
    out_big = ex.execute_entry(D(), {"traderId": 9922808}, risk_usd_override=24.0)
    vol_big = mcp.calls[-1]["volume"]
    j.close()
    assert out_small.get("action") == "entered" and out_big.get("action") == "entered"
    # SL distance = 3.0 → risk 3/3=1 unit vs 24/3=8 units
    assert vol_big > vol_small
    assert vol_big == pytest.approx(8.0)
