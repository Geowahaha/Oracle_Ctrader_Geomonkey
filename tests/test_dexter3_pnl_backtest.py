"""Unit tests for scripts/dexter3_pnl_backtest.py — pure functions only.

No MCP calls (mocked/synthetic data), no journal DB dependency (in-memory
sqlite fixtures built ad-hoc per test) — per task constraints "unit tests
use synthetic data / mocked transport only".

Covers:
  (a) deal field parsing helpers (pnl/label/side/position_id fallback chains).
  (b) filter_lane_deals label matching.
  (c) compute_pf_stats — PF/win-rate/avg-win/avg-loss math against
      hand-computed expected values, including the zero-loss (PF=inf) edge.
  (d) load_entry_geometry against a synthetic sqlite exec_events table.
  (e) project_fix1_winners — conservative floor projection: never-armed
      floor, armed-and-capped-at-take_r, geometry-missing fallback (A0).
  (f) project_fix1_winners_optimistic — TP-intended R projection, capped at
      take_r.
  (g) estimate_fix3_impact — counter-trend sell-loss counting against a
      synthetic committee snapshot.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from dexter3.basket_manager import BasketConfig
from scripts import dexter3_pnl_backtest as bt


# ---------------------------------------------------------------------------
# (a) deal field parsing helpers
# ---------------------------------------------------------------------------


def test_deal_net_profit_fallback_chain():
    assert bt._deal_net_profit({"netProfit": 5.0}) == 5.0
    assert bt._deal_net_profit({"profit": 3.0}) == 3.0
    assert bt._deal_net_profit({"grossProfit": 2.0}) == 2.0
    assert bt._deal_net_profit({"pnl": 1.0}) == 1.0
    assert bt._deal_net_profit({"closedNetProfit": 0.5}) == 0.5
    assert bt._deal_net_profit({}) is None
    assert bt._deal_net_profit({"netProfit": "garbage"}) is None


def test_deal_label_fallback():
    assert bt._deal_label({"label": "dexter3:fable:m5h-v1"}) == "dexter3:fable:m5h-v1"
    assert bt._deal_label({"comment": "dexter3:fable:m5h-v1"}) == "dexter3:fable:m5h-v1"
    assert bt._deal_label({}) == ""


def test_deal_side_normalization():
    assert bt._deal_side({"tradeSide": "BUY"}) == "buy"
    assert bt._deal_side({"tradeSide": "SELL"}) == "sell"
    assert bt._deal_side({"side": "sell"}) == "sell"
    assert bt._deal_side({}) == ""


def test_deal_position_id_fallback():
    assert bt._deal_position_id({"positionId": 123}) == 123
    assert bt._deal_position_id({"position_id": 456}) == 456
    assert bt._deal_position_id({"id": 789}) == 789
    assert bt._deal_position_id({}) == 0
    assert bt._deal_position_id({"positionId": "garbage"}) == 0


def test_deal_symbol():
    assert bt._deal_symbol({"symbolName": "xauusd"}) == "XAUUSD"
    assert bt._deal_symbol({"symbol": "BTCUSD"}) == "BTCUSD"
    assert bt._deal_symbol({}) == ""


# ---------------------------------------------------------------------------
# (b) filter_lane_deals
# ---------------------------------------------------------------------------


def test_filter_lane_deals_matches_label_substring():
    deals = [
        {"label": "dexter3:fable:m5h-v1:hunt", "netProfit": 1.0},
        {"label": "some_other_bot", "netProfit": 2.0},
        {"comment": "dexter3:fable:m5h-v1:repair", "netProfit": 3.0},
    ]
    lane = bt.filter_lane_deals(deals, "dexter3:fable")
    assert len(lane) == 2


def test_filter_lane_deals_ignores_non_dict_and_missing_label():
    deals = ["not_a_dict", {}, None]
    lane = bt.filter_lane_deals(deals, "dexter3:fable")  # type: ignore[arg-type]
    assert lane == []


# ---------------------------------------------------------------------------
# (c) compute_pf_stats
# ---------------------------------------------------------------------------


def test_compute_pf_stats_basic_math():
    deals = [
        {"tradeSide": "BUY", "netProfit": 10.0},
        {"tradeSide": "BUY", "netProfit": 5.0},
        {"tradeSide": "SELL", "netProfit": -20.0},
    ]
    stats = bt.compute_pf_stats(deals)
    assert stats["n_wins"] == 2
    assert stats["n_losses"] == 1
    assert stats["gross_profit"] == 15.0
    assert stats["gross_loss"] == -20.0
    assert stats["net_pnl"] == pytest.approx(-5.0)
    assert stats["win_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert stats["avg_win"] == pytest.approx(7.5)
    assert stats["avg_loss"] == pytest.approx(-20.0)
    assert stats["profit_factor"] == pytest.approx(15.0 / 20.0)
    assert stats["side_pnl"]["buy"] == pytest.approx(15.0)
    assert stats["side_pnl"]["sell"] == pytest.approx(-20.0)


def test_compute_pf_stats_reproduces_diagnosed_asymmetry_shape():
    """Sanity check the methodology against the PM's diagnosed shape: 56%
    win rate, avg_win << |avg_loss| -> PF well below 1.0."""
    wins = [6.15] * 28
    losses = [-12.87] * 22
    deals = [{"tradeSide": "BUY", "netProfit": p} for p in wins] + [
        {"tradeSide": "SELL", "netProfit": p} for p in losses
    ]
    stats = bt.compute_pf_stats(deals)
    assert stats["win_rate"] == pytest.approx(28 / 50)
    assert stats["profit_factor"] < 1.0
    assert stats["net_pnl"] < 0


def test_compute_pf_stats_no_losses_gives_infinite_pf():
    deals = [{"tradeSide": "BUY", "netProfit": 5.0}]
    stats = bt.compute_pf_stats(deals)
    assert stats["profit_factor"] == float("inf")


def test_compute_pf_stats_empty_deals():
    stats = bt.compute_pf_stats([])
    assert stats["n_deals"] == 0
    assert stats["profit_factor"] == 0.0
    assert stats["net_pnl"] == 0.0


def test_compute_pf_stats_unreadable_pnl_excluded_not_crashed():
    deals = [{"tradeSide": "BUY", "netProfit": "garbage"}, {"tradeSide": "BUY", "netProfit": 5.0}]
    stats = bt.compute_pf_stats(deals)
    assert stats["n_unreadable_pnl"] == 1
    assert stats["n_wins"] == 1


def test_compute_pf_stats_breakeven_counted_in_neither():
    deals = [{"tradeSide": "BUY", "netProfit": 0.0}]
    stats = bt.compute_pf_stats(deals)
    assert stats["n_wins"] == 0
    assert stats["n_losses"] == 0


# ---------------------------------------------------------------------------
# (d) load_entry_geometry — synthetic sqlite exec_events table
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_journal_db(tmp_path):
    db_path = tmp_path / "synthetic_journal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE exec_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            symbol TEXT NOT NULL,
            event TEXT NOT NULL,
            position_id INTEGER,
            verified INTEGER,
            payload_json TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_close TEXT NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            side TEXT,
            setup TEXT,
            features_json TEXT NOT NULL
        )"""
    )

    def _entry(position_id, side, entry, sl, tp, risk_usd, clamped=False):
        payload = {
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "setup": "hunt_swing_structure",
            "volume_meta": {
                "risk_usd": risk_usd,
                "sl_distance": abs(entry - sl),
                "min_volume_clamped_up": clamped,
                "estimated_min_volume_risk_usd": risk_usd * 1.5 if clamped else None,
            },
        }
        conn.execute(
            "INSERT INTO exec_events (ts, symbol, event, position_id, verified, payload_json) VALUES (?,?,?,?,?,?)",
            ("2026-07-06T10:00:00Z", "XAUUSD", "entry_executed", position_id, 1, json.dumps(payload)),
        )

    _entry(1001, "buy", 2000.0, 1990.0, 2020.0, risk_usd=15.0)  # RR intended = 2.0
    _entry(1002, "sell", 2000.0, 2010.0, 1976.0, risk_usd=15.0)  # RR intended = 2.4
    _entry(1003, "buy", 2000.0, 1990.0, 2012.0, risk_usd=10.0, clamped=True)  # clamped -> risk 15.0

    conn.commit()
    conn.close()
    return db_path


def test_load_entry_geometry_basic_fields(synthetic_journal_db):
    geo = bt.load_entry_geometry(synthetic_journal_db)
    assert geo[1001]["side"] == "buy"
    assert geo[1001]["entry"] == 2000.0
    assert geo[1001]["risk_usd"] == 15.0
    assert geo[1001]["min_volume_clamped"] is False


def test_load_entry_geometry_clamped_uses_estimated_risk(synthetic_journal_db):
    geo = bt.load_entry_geometry(synthetic_journal_db)
    assert geo[1003]["min_volume_clamped"] is True
    assert geo[1003]["risk_usd"] == pytest.approx(15.0)  # 10.0 * 1.5


def test_load_entry_geometry_missing_db_returns_empty(tmp_path):
    missing = tmp_path / "does_not_exist.db"
    assert bt.load_entry_geometry(missing) == {}


# ---------------------------------------------------------------------------
# (e) project_fix1_winners — conservative floor projection
# ---------------------------------------------------------------------------


def _win_deal(position_id: int, net_profit: float) -> dict:
    return {"positionId": position_id, "netProfit": net_profit, "tradeSide": "BUY"}


def test_project_fix1_winners_never_armed_below_threshold_unchanged():
    cfg = BasketConfig(resolve_target_r=0.2, arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    geometry = {1: {"risk_usd": 15.0, "entry": 2000.0, "sl": 1990.0, "tp": 2020.0}}
    win = _win_deal(1, 3.0)  # realized_r = 3.0/15.0 = 0.2 -> below arm_trail_r
    proj = bt.project_fix1_winners([win], geometry, cfg)
    row = proj["rows"][0]
    assert row["realized_r"] == pytest.approx(0.2)
    assert row["trigger"] == "never_armed_conservative_floor"
    assert row["projected_pnl"] == pytest.approx(3.0)  # unchanged
    assert proj["delta"] == pytest.approx(0.0)


def test_project_fix1_winners_armed_and_within_take_r_unchanged():
    cfg = BasketConfig(arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    geometry = {1: {"risk_usd": 15.0, "entry": 2000.0, "sl": 1990.0, "tp": 2050.0}}
    win = _win_deal(1, 9.0)  # realized_r = 0.6 -> armed, below take_r
    proj = bt.project_fix1_winners([win], geometry, cfg)
    row = proj["rows"][0]
    assert row["realized_r"] == pytest.approx(0.6)
    assert row["trigger"] == "take_r_or_peak_floor"
    assert row["projected_pnl"] == pytest.approx(9.0)  # min(1.1, 0.6) == 0.6 -> unchanged


def test_project_fix1_winners_armed_and_capped_at_take_r():
    cfg = BasketConfig(arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    geometry = {1: {"risk_usd": 15.0, "entry": 2000.0, "sl": 1990.0, "tp": 2100.0}}
    win = _win_deal(1, 18.0)  # realized_r = 1.2 -> above take_r=1.1, must be capped
    proj = bt.project_fix1_winners([win], geometry, cfg)
    row = proj["rows"][0]
    assert row["realized_r"] == pytest.approx(1.2)
    assert row["projected_r"] == pytest.approx(1.1)
    assert row["projected_pnl"] == pytest.approx(18.0 * (1.1 / 1.2))
    assert proj["delta"] < 0  # capped down from the realized win


def test_project_fix1_winners_no_geometry_match_uses_a0_fallback():
    cfg = BasketConfig(resolve_target_r=0.25)
    win = _win_deal(999, 5.0)  # no matching geometry entry
    proj = bt.project_fix1_winners([win], {}, cfg)
    row = proj["rows"][0]
    assert row["realized_r"] == pytest.approx(0.25)
    assert row["note"] == "assumed_flat_resolve_target_r_no_geometry_match"
    assert row["geometry_available"] is False


def test_project_fix1_winners_zero_risk_usd_falls_back_to_a0():
    cfg = BasketConfig(resolve_target_r=0.2)
    geometry = {1: {"risk_usd": 0.0, "entry": 2000.0, "sl": 1990.0, "tp": 2020.0}}
    win = _win_deal(1, 5.0)
    proj = bt.project_fix1_winners([win], geometry, cfg)
    assert proj["rows"][0]["note"] == "assumed_flat_resolve_target_r_no_geometry_match"


def test_project_fix1_winners_totals_and_geometry_match_count():
    cfg = BasketConfig(arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    geometry = {
        1: {"risk_usd": 15.0, "entry": 2000.0, "sl": 1990.0, "tp": 2020.0},
        2: {"risk_usd": 15.0, "entry": 2000.0, "sl": 1990.0, "tp": 2050.0},
    }
    wins = [_win_deal(1, 3.0), _win_deal(2, 9.0), _win_deal(3, 4.0)]  # #3 has no geometry
    proj = bt.project_fix1_winners(wins, geometry, cfg)
    assert proj["n_wins_projected"] == 3
    assert proj["n_wins_with_geometry_match"] == 2
    assert proj["total_realized_win_pnl"] == pytest.approx(16.0)


# ---------------------------------------------------------------------------
# (f) project_fix1_winners_optimistic
# ---------------------------------------------------------------------------


def test_optimistic_projection_uses_tp_intended_rr():
    cfg = BasketConfig(arm_trail_r=0.5, trail_keep_frac=0.6, take_r=1.1)
    # entry=2000, sl=1990 (dist=10), tp=2020 (dist=20) -> rr_intended = 2.0
    geometry = {1: {"risk_usd": 15.0, "entry": 2000.0, "sl": 1990.0, "tp": 2020.0}}
    win = _win_deal(1, 3.0)  # realized_r = 0.2
    proj = bt.project_fix1_winners_optimistic([win], geometry, cfg)
    row = proj["rows"][0]
    assert row["rr_intended_tp"] == pytest.approx(2.0)
    # optimistic_peak = max(0.2, 2.0) = 2.0, capped at take_r=1.1
    assert row["projected_r"] == pytest.approx(1.1)
    assert row["projected_pnl"] == pytest.approx(3.0 * (1.1 / 0.2))


def test_optimistic_projection_never_exceeds_take_r():
    cfg = BasketConfig(take_r=1.1)
    geometry = {1: {"risk_usd": 15.0, "entry": 2000.0, "sl": 1990.0, "tp": 2500.0}}  # RR=50, absurdly large
    win = _win_deal(1, 3.0)
    proj = bt.project_fix1_winners_optimistic([win], geometry, cfg)
    assert proj["rows"][0]["projected_r"] == pytest.approx(1.1)


def test_optimistic_projection_missing_geometry_falls_back_to_realized():
    cfg = BasketConfig(resolve_target_r=0.2, take_r=1.1)
    win = _win_deal(1, 3.0)
    proj = bt.project_fix1_winners_optimistic([win], {}, cfg)
    row = proj["rows"][0]
    assert row["rr_intended_tp"] is None
    assert row["projected_r"] == pytest.approx(0.2)  # unchanged, no TP info available


def test_optimistic_projection_warning_present():
    cfg = BasketConfig()
    proj = bt.project_fix1_winners_optimistic([], {}, cfg)
    assert "OPTIMISTIC" in proj["warning"]


# ---------------------------------------------------------------------------
# (g) estimate_fix3_impact
# ---------------------------------------------------------------------------


def _sell_loss_deal(position_id: int) -> dict:
    return {"positionId": position_id, "netProfit": -10.0, "tradeSide": "SELL", "symbolName": "XAUUSD"}


def _decision_with_committee(*, side: str, m15_vote: float, h1_vote: float, weighted_sum: float) -> dict:
    committee = {
        "m15_drift": {"vote": m15_vote, "weight": 1.3, "weighted": m15_vote * 1.3},
        "h1_context": {"vote": h1_vote, "weight": 0.9, "weighted": h1_vote * 0.9},
        "sweep_reclaim": {"vote": 0.0, "weight": 1.5, "weighted": 0.0, "detail": {"fired": False, "side": None}},
    }
    # Pad weighted_sum to the requested total by adjusting a synthetic filler key.
    current = sum(m["weighted"] for m in committee.values())
    filler = weighted_sum - current
    committee["_filler"] = {"vote": 0.0, "weight": 1.0, "weighted": filler}
    return {"ts_close": "2026-07-06T10:00:00Z", "symbol": "XAUUSD", "side": side, "setup": "hunt_x", "committee": committee}


def test_estimate_fix3_impact_counts_counter_trend_matches():
    losses = [_sell_loss_deal(1)]
    decisions = [_decision_with_committee(side="sell", m15_vote=0.9, h1_vote=0.8, weighted_sum=-0.99)]
    result = bt.estimate_fix3_impact(losses, decisions)
    assert result["n_sell_losses"] == 1
    assert result["n_counter_trend_matched"] == 1
    assert result["n_would_be_flipped"] == 1  # weak evidence -> flip


def test_estimate_fix3_impact_strong_evidence_not_flipped():
    losses = [_sell_loss_deal(1)]
    decisions = [_decision_with_committee(side="sell", m15_vote=0.6, h1_vote=0.55, weighted_sum=-3.5)]
    result = bt.estimate_fix3_impact(losses, decisions)
    assert result["n_counter_trend_matched"] == 1
    assert result["n_would_be_penalized_only"] == 1
    assert result["n_would_be_flipped"] == 0


def test_estimate_fix3_impact_no_aligned_trend_not_counted():
    losses = [_sell_loss_deal(1)]
    decisions = [_decision_with_committee(side="sell", m15_vote=0.9, h1_vote=-0.8, weighted_sum=-0.5)]
    result = bt.estimate_fix3_impact(losses, decisions)
    assert result["n_counter_trend_matched"] == 0


def test_estimate_fix3_impact_no_sell_losses():
    result = bt.estimate_fix3_impact([], [])
    assert result["n_sell_losses"] == 0
    assert result["n_counter_trend_matched"] == 0


def test_estimate_fix3_impact_only_considers_sell_side_losses():
    buy_loss = {"positionId": 2, "netProfit": -5.0, "tradeSide": "BUY", "symbolName": "XAUUSD"}
    decisions = [_decision_with_committee(side="sell", m15_vote=0.9, h1_vote=0.8, weighted_sum=-0.99)]
    result = bt.estimate_fix3_impact([buy_loss], decisions)
    assert result["n_sell_losses"] == 0


# ---------------------------------------------------------------------------
# fetch_deals / never raises on transport failure
# ---------------------------------------------------------------------------


class _StubMcpClientRaises:
    def call(self, name, args=None):
        from dexter3.mcp_client import McpClientError

        raise McpClientError("simulated transport failure")


def test_fetch_deals_never_raises_on_mcp_error():
    result = bt.fetch_deals(_StubMcpClientRaises(), 50)  # type: ignore[arg-type]
    assert result == []


class _StubMcpClientDict:
    def call(self, name, args=None):
        return {"deals": [{"netProfit": 1.0}]}


def test_fetch_deals_unwraps_deals_key():
    result = bt.fetch_deals(_StubMcpClientDict(), 50)  # type: ignore[arg-type]
    assert result == [{"netProfit": 1.0}]


class _StubMcpClientList:
    def call(self, name, args=None):
        return [{"netProfit": 2.0}]


def test_fetch_deals_accepts_raw_list_payload():
    result = bt.fetch_deals(_StubMcpClientList(), 50)  # type: ignore[arg-type]
    assert result == [{"netProfit": 2.0}]
