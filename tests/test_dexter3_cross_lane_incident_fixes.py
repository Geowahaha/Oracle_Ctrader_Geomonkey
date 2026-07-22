"""Regression tests for the 2026-07-15 cross-lane incident triad:

  1. executor.py ``reconcile_vanished_lane_positions`` — lane-label filter +
     defer-not-guess when a candidate's closing deal is not yet visible.
  2. shadow_runner.py ``_lane_realized_today`` — UTC-day cache invalidation
     (a fetch computed just before midnight must not keep being served into
     the new UTC day) + ``_governor_entry_risk`` pre-entry loss-cap refusal.
  3. shadow_runner.py ``_apply_v16_entry_quality_gate`` — grok minimum
     leader_score floor (grok bypasses the v16 gate entirely otherwise).

Plus the one-shot repair script for the pre-fix poisoned journal backlog,
``ops/dexter3_repair_cross_lane_vanish.py``.

NO live MCP calls, NO touching data/runtime/* — every test drives a fake
transport / temp sqlite DB, same conventions as
tests/test_dexter3_executor.py and tests/test_dexter3_wiring.py.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dexter3.executor as ex_mod
import dexter3.shadow_runner as sr
from dexter3.decision_journal import DecisionJournal
from dexter3.executor import (
    Dexter3Executor,
    ExecutorConfig,
    ensure_exec_events_table,
    insert_exec_event,
    position_id_of,
    recent_exec_events,
)
from dexter3.grok_v10 import GROK_LABEL


# ---------------------------------------------------------------------------
# shared fixtures / fakes (mirrors tests/test_dexter3_executor.py)
# ---------------------------------------------------------------------------


class FakeMcp:
    """Records calls; canned responses configurable per test via attrs."""

    def __init__(self) -> None:
        self.symbol_details = {
            "minVolume": 0.01, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0, "pipSize": 0.01,
        }
        self.spot = {"bid": 62000.0, "ask": 62006.0}
        self._positions: list[dict] = []
        self.order_response = {"dealStatus": "FILLED"}
        self.post_entry_position: dict | None = None
        self.balance = {"traderId": 9922808, "balance": 10000.0}
        self.deals: list[dict] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_symbol_details(self, symbol: str) -> dict:
        self.calls.append(("get_symbol_details", {}))
        return dict(self.symbol_details)

    def get_spot_price(self, symbol: str) -> dict:
        self.calls.append(("get_spot_price", {}))
        return dict(self.spot)

    def get_positions(self) -> list[dict]:
        self.calls.append(("get_positions", {}))
        if self.post_entry_position is not None and any(c[0] == "place_market_order" for c in self.calls):
            return list(self._positions) + [dict(self.post_entry_position)]
        return list(self._positions)

    def get_balance(self) -> dict:
        self.calls.append(("get_balance", {}))
        return dict(self.balance)

    def place_market_order(self, **kwargs: Any) -> dict:
        self.calls.append(("place_market_order", kwargs))
        return dict(self.order_response)

    def amend_position(self, position_id: int, stop_loss=None, take_profit=None) -> dict:
        self.calls.append(("amend_position", {"position_id": position_id}))
        return {"status": "ok"}

    def close_position(self, position_id: int) -> dict:
        self.calls.append(("close_position", {"position_id": position_id}))
        self._positions = [p for p in self._positions if position_id_of(p) != position_id]
        return {"status": "closed"}

    def get_deals(self, count: int = 200, from_timestamp_ms: int | None = None) -> list[dict]:
        # from_timestamp_ms: accepted for call-site parity with the H1 fix
        # (2026-07-15 cross-lane entanglement audit) — this fake stands in
        # for the local-MCP transport, which also accepts-and-drops it.
        self.calls.append(("get_deals", {"count": count, "from_timestamp_ms": from_timestamp_ms}))
        return list(self.deals)


class FakeDecision(SimpleNamespace):
    def __init__(self, **kw: Any) -> None:
        base = dict(
            symbol="BTCUSD", action="enter", side="buy", entry=62000.0, sl=61900.0, tp=62150.0,
            setup="leader_continuation", reasons=["test_reason"], session="london",
        )
        base.update(kw)
        super().__init__(**base)


DEMO_ACCOUNT = {"traderId": 9922808}


def _entered(ex: Dexter3Executor, mcp: FakeMcp, pid: int, **decision_kw: Any) -> dict:
    """Drive a real entry so entry_executed exists for pid, stamped with
    whatever dexter3.executor.LABEL is active right now."""
    mcp.post_entry_position = {
        "positionId": pid, "symbolName": "BTCUSD", "tradeSide": "Buy",
        "volumeInUnits": 0.01, "stopLoss": 61900.0, "takeProfit": 62150.0, "label": ex_mod.LABEL,
    }
    result = ex.execute_entry(FakeDecision(**decision_kw), DEMO_ACCOUNT)
    assert result["action"] == "entered" and result["position_id"] == pid
    mcp.calls = [c for c in mcp.calls if c[0] != "place_market_order"]
    mcp.post_entry_position = None
    return result


@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "cross_lane_journal.db")
    yield j
    j.close()


# ---------------------------------------------------------------------------
# 1. reconcile_vanished_lane_positions — cross-lane isolation
# ---------------------------------------------------------------------------


def test_reconcile_never_touches_a_foreign_lanes_open_elsewhere_entry(journal, monkeypatch):
    mcp = FakeMcp()

    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:m5h-v1")
    ex_fable = Dexter3Executor(mcp, journal, ExecutorConfig())
    _entered(ex_fable, mcp, 501, setup="dragon_shelf_short", session="overlap")

    monkeypatch.setattr(ex_mod, "LABEL", GROK_LABEL)
    ex_grok = Dexter3Executor(mcp, journal, ExecutorConfig())

    # Position 501 is still open under Fable's label — invisible to Grok's
    # own label-filtered lane list, so it LOOKS vanished from Grok's view.
    # Its zero-pnl entry-leg deal is enough to satisfy a naive "found a pnl"
    # check, which is exactly how the 2026-07-15 incident fabricated a close.
    mcp.deals = [{"positionId": 501, "netProfit": 0.0}]
    assert ex_grok.reconcile_vanished_lane_positions("BTCUSD", []) == []
    events = recent_exec_events(journal._conn, symbol="BTCUSD")
    assert not any(e["event"] == "lane_position_closed" for e in events)

    # Fable's OWN reconcile pass, with a real closing deal in the window,
    # still works correctly.
    mcp.deals = [{"positionId": 501, "netProfit": 0.0}, {"positionId": 501, "netProfit": -4.0}]
    out = ex_fable.reconcile_vanished_lane_positions("BTCUSD", [])
    assert len(out) == 1
    assert out[0]["position_id"] == 501
    assert out[0]["pnl"] == pytest.approx(-4.0)


# ---------------------------------------------------------------------------
# 2. reconcile_vanished_lane_positions — defer, never guess pnl=0.0
# ---------------------------------------------------------------------------


def test_reconcile_defers_own_lane_candidate_when_closing_deal_not_yet_visible(journal, monkeypatch):
    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:m5h-v1")
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    _entered(ex, mcp, 601)

    # deals fetch succeeds but carries nothing at all for pid 601 this round
    mcp.deals = [{"positionId": 999, "netProfit": 3.0}]
    assert ex.reconcile_vanished_lane_positions("BTCUSD", []) == []
    events = recent_exec_events(journal._conn, symbol="BTCUSD")
    assert not any(e["event"] == "lane_position_closed" for e in events)
    assert not any(e["event"] == "vanish_reconcile_deferred" for e in events)  # per-candidate defer, not the whole-round path

    # closing deal appears next round -> reconciles for real, no guess
    mcp.deals = [{"positionId": 601, "netProfit": 2.4}]
    out = ex.reconcile_vanished_lane_positions("BTCUSD", [])
    assert len(out) == 1 and out[0]["pnl"] == pytest.approx(2.4)


def test_reconcile_ignores_own_lane_entry_leg_when_close_detail_marker_present(journal, monkeypatch):
    """Openapi-normalized deals stamp netProfit=0.0 on ENTRY legs too — an
    own-lane candidate whose only visible deal is its zero-pnl entry leg must
    DEFER (hasCloseDetail=False), not journal a pnl=0.0 guess."""
    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:m5h-v1")
    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    _entered(ex, mcp, 701)

    # only the entry leg is in the window (openapi shape: marker present, False)
    mcp.deals = [{"positionId": 701, "netProfit": 0.0, "hasCloseDetail": False}]
    assert ex.reconcile_vanished_lane_positions("BTCUSD", []) == []
    events = recent_exec_events(journal._conn, symbol="BTCUSD")
    assert not any(e["event"] == "lane_position_closed" for e in events)

    # close leg arrives -> real pnl journaled, entry leg still excluded
    mcp.deals = [
        {"positionId": 701, "netProfit": 0.0, "hasCloseDetail": False},
        {"positionId": 701, "netProfit": -4.47, "hasCloseDetail": True},
    ]
    out = ex.reconcile_vanished_lane_positions("BTCUSD", [])
    assert len(out) == 1 and out[0]["pnl"] == pytest.approx(-4.47)


# ---------------------------------------------------------------------------
# 3a. _lane_realized_today — UTC-day cache invalidation across midnight
# ---------------------------------------------------------------------------


def _ms(iso: str) -> int:
    return int(_dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _frozen_datetime(iso: str):
    frozen = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))

    class _Frozen:
        strptime = _dt.datetime.strptime
        fromisoformat = _dt.datetime.fromisoformat
        fromtimestamp = _dt.datetime.fromtimestamp

        @staticmethod
        def now(tz=None):
            return frozen

    return _Frozen


def test_lane_realized_today_only_counts_the_current_utc_day(monkeypatch):
    monkeypatch.setattr(sr, "_LANE_REALIZED_CACHE", {})
    mcp = FakeMcp()
    mcp.deals = [
        {"label": "dexter3:fable:m5h-v1", "netProfit": -20.0, "execution_timestamp_ms": _ms("2026-07-14T23:58:00Z")},
        {"label": "dexter3:fable:m5h-v1", "netProfit": -5.0, "execution_timestamp_ms": _ms("2026-07-15T00:00:10Z")},
    ]
    monkeypatch.setattr(sr, "datetime", _frozen_datetime("2026-07-15T00:05:00Z"))
    realized, pnls = sr._lane_realized_today(mcp, label_filter="dexter3:fable")
    assert realized == pytest.approx(-5.0)
    assert pnls == [pytest.approx(-5.0)]


def test_lane_realized_today_cache_invalidates_across_utc_midnight(monkeypatch):
    """The exact 2026-07-15 incident mechanism: a fetch computed just before
    UTC midnight must not keep being served (within the 60s cache window)
    once the calendar day has rolled over."""
    monkeypatch.setattr(sr, "_LANE_REALIZED_CACHE", {})
    calls = {"n": 0}

    class _MultiCallMcp:
        def get_deals(self, count: int = 500, from_timestamp_ms: int | None = None) -> list[dict]:
            calls["n"] += 1
            if calls["n"] == 1:
                return [
                    {"label": "dexter3:fable:m5h-v1", "netProfit": -20.0,
                     "execution_timestamp_ms": _ms("2026-07-14T23:58:00Z")},
                ]
            return [
                {"label": "dexter3:fable:m5h-v1", "netProfit": -20.0,
                 "execution_timestamp_ms": _ms("2026-07-14T23:58:00Z")},
                {"label": "dexter3:fable:m5h-v1", "netProfit": -2.0,
                 "execution_timestamp_ms": _ms("2026-07-15T00:00:05Z")},
            ]

    mcp = _MultiCallMcp()

    monkeypatch.setattr(sr, "datetime", _frozen_datetime("2026-07-14T23:59:50Z"))
    realized1, _ = sr._lane_realized_today(mcp, label_filter="dexter3:fable")
    assert realized1 == pytest.approx(-20.0)

    # Only 30s later (inside LANE_REALIZED_CACHE_SEC=60) but a NEW UTC day —
    # must refetch and reflect ONLY today's -2.0, not the cached -20.0.
    monkeypatch.setattr(sr, "datetime", _frozen_datetime("2026-07-15T00:00:20Z"))
    realized2, pnls2 = sr._lane_realized_today(mcp, label_filter="dexter3:fable")
    assert realized2 == pytest.approx(-2.0)
    assert pnls2 == [pytest.approx(-2.0)]
    assert calls["n"] == 2  # proves it actually refetched instead of serving stale cache


# ---------------------------------------------------------------------------
# 4. _governor_entry_risk — pre-entry loss-cap refusal
# ---------------------------------------------------------------------------


def _reset_governor(monkeypatch: pytest.MonkeyPatch, *, daily_loss_usd: str = "15") -> None:
    monkeypatch.setattr(sr, "_GOVERNOR_INSTANCE", None)
    monkeypatch.setenv("DEXTER3_DAILY_LOSS_USD", daily_loss_usd)
    monkeypatch.delenv("DEXTER3_MODE", raising=False)


def test_governor_entry_risk_refuses_when_cap_already_breached(monkeypatch):
    _reset_governor(monkeypatch)
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-16.32, [-16.32]))
    d = FakeDecision(symbol="XAUUSD", features={})
    result = sr._governor_entry_risk(FakeMcp(), d, {"governor": {"floating_by_symbol": {}}})
    assert result is not None
    assert result["allow"] is False
    assert result["reason"] == "governor_loss_stop_pre_entry"


def test_governor_entry_risk_allows_when_below_cap(monkeypatch):
    _reset_governor(monkeypatch)
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-5.0, [-5.0]))
    d = FakeDecision(symbol="XAUUSD", features={})
    result = sr._governor_entry_risk(FakeMcp(), d, {"governor": {"floating_by_symbol": {}}})
    assert result is not None
    assert result.get("allow", True) is True
    assert "risk_usd" in result


def test_governor_entry_risk_counts_floating_toward_the_cap(monkeypatch):
    """'realized + current floating if already available in state'."""
    _reset_governor(monkeypatch)
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-10.0, [-10.0]))
    d = FakeDecision(symbol="XAUUSD", features={})
    state = {"governor": {"floating_by_symbol": {"XAUUSD": -6.0}}}  # -10 + -6 = -16 <= -15 cap
    result = sr._governor_entry_risk(FakeMcp(), d, state)
    assert result is not None
    assert result["allow"] is False
    assert result["reason"] == "governor_loss_stop_pre_entry"


def test_governor_entry_risk_reuses_the_same_cap_run_governor_tick_uses(monkeypatch):
    """No duplicated constant — both read _get_governor().config.daily_loss_usd."""
    _reset_governor(monkeypatch, daily_loss_usd="9")
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-9.0, [-9.0]))
    d = FakeDecision(symbol="XAUUSD", features={})
    result = sr._governor_entry_risk(FakeMcp(), d, {"governor": {"floating_by_symbol": {}}})
    assert result["allow"] is False
    assert sr._get_governor().config.daily_loss_usd == pytest.approx(9.0)


# ---------------------------------------------------------------------------
# 4b. _governor_entry_risk — high-conviction bypass (2026-07-22 owner audit:
# a dayreversal_structure_flip candidate was refused live by LOSS_STOPPED
# with zero exception path — "good opportunities must bypass every block")
# ---------------------------------------------------------------------------


def test_governor_entry_risk_bypass_off_by_default_even_with_elite_score(monkeypatch):
    _reset_governor(monkeypatch)  # DEXTER3_GOVERNOR_BYPASS_MIN_SCORE left unset
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-16.32, [-16.32]))
    d = FakeDecision(symbol="XAUUSD", features={}, leader_score=0.99)
    result = sr._governor_entry_risk(FakeMcp(), d, {"governor": {"floating_by_symbol": {}}})
    assert result is not None
    assert result["allow"] is False
    assert result["reason"] == "governor_loss_stop_pre_entry"


def test_governor_entry_risk_bypass_allows_high_conviction_when_cap_breached(monkeypatch):
    _reset_governor(monkeypatch)
    monkeypatch.setenv("DEXTER3_GOVERNOR_BYPASS_MIN_SCORE", "0.74")
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-16.32, [-16.32]))
    d = FakeDecision(symbol="XAUUSD", features={}, leader_score=0.80)
    result = sr._governor_entry_risk(FakeMcp(), d, {"governor": {"floating_by_symbol": {}}})
    assert result is not None
    assert result.get("allow", True) is True
    assert "risk_usd" in result  # normal sizing still runs, not a raw pass-through


def test_governor_entry_risk_bypass_refuses_when_score_below_threshold(monkeypatch):
    _reset_governor(monkeypatch)
    monkeypatch.setenv("DEXTER3_GOVERNOR_BYPASS_MIN_SCORE", "0.74")
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-16.32, [-16.32]))
    d = FakeDecision(symbol="XAUUSD", features={}, leader_score=0.50)
    result = sr._governor_entry_risk(FakeMcp(), d, {"governor": {"floating_by_symbol": {}}})
    assert result is not None
    assert result["allow"] is False
    assert result["reason"] == "governor_loss_stop_pre_entry"


def test_governor_entry_risk_bypass_boundary_is_inclusive(monkeypatch):
    _reset_governor(monkeypatch)
    monkeypatch.setenv("DEXTER3_GOVERNOR_BYPASS_MIN_SCORE", "0.74")
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-16.32, [-16.32]))
    d = FakeDecision(symbol="XAUUSD", features={}, leader_score=0.74)
    result = sr._governor_entry_risk(FakeMcp(), d, {"governor": {"floating_by_symbol": {}}})
    assert result.get("allow", True) is True


def test_governor_entry_risk_missing_leader_score_defaults_to_zero_never_bypasses(monkeypatch):
    # a decision with no leader_score attr at all (e.g. a producer that
    # never sets it) must never accidentally bypass — getattr(...) default
    # must be 0.0, not None (None would (incorrectly) never compare < threshold).
    _reset_governor(monkeypatch)
    monkeypatch.setenv("DEXTER3_GOVERNOR_BYPASS_MIN_SCORE", "0.0")
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (-16.32, [-16.32]))
    d = SimpleNamespace(symbol="XAUUSD", features={})  # no leader_score attr
    result = sr._governor_entry_risk(FakeMcp(), d, {"governor": {"floating_by_symbol": {}}})
    # threshold 0.0 and default score 0.0 -> 0.0 >= 0.0 -> bypass fires
    assert result.get("allow", True) is True


# ---------------------------------------------------------------------------
# 4c. _stamp_skip_bias_fallback — fear-cost bias fallback (2026-07-22 audit:
# vp/daytrend/scalp skip rows were structurally 100% unevaluable because
# their features never carry a hunt-lens-shaped candidate side)
# ---------------------------------------------------------------------------


def _bar(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


def test_stamp_skip_bias_fallback_buy_day():
    prefix = [
        _bar("2026-07-16T00:00:00Z", 4000.0, 4001.0, 3999.0, 4000.0),  # anchor/day-open bar
        _bar("2026-07-16T01:00:00Z", 4000.0, 4012.0, 3999.5, 4010.0),  # close well above open
    ]
    d = FakeDecision(action="skip", features={})
    sr._stamp_skip_bias_fallback(d, prefix)
    assert d.features.get("skip_bias_side") == "buy"


def test_stamp_skip_bias_fallback_sell_day():
    prefix = [
        _bar("2026-07-16T00:00:00Z", 4000.0, 4001.0, 3999.0, 4000.0),
        _bar("2026-07-16T01:00:00Z", 4000.0, 4000.5, 3988.0, 3990.0),  # close well below open
    ]
    d = FakeDecision(action="skip", features={})
    sr._stamp_skip_bias_fallback(d, prefix)
    assert d.features.get("skip_bias_side") == "sell"


def test_stamp_skip_bias_fallback_neutral_bias_leaves_features_unstamped():
    prefix: list[dict] = []  # empty prefix -> dayopen_bias returns (0, 0.0)
    d = FakeDecision(action="skip", features={})
    sr._stamp_skip_bias_fallback(d, prefix)
    assert "skip_bias_side" not in d.features


def test_stamp_skip_bias_fallback_never_raises_on_garbage_prefix():
    d = FakeDecision(action="skip", features={})
    sr._stamp_skip_bias_fallback(d, [{"ts": "not-a-timestamp"}])  # malformed
    assert "skip_bias_side" not in d.features  # degrades silently, no crash


def test_governor_bypass_threshold_stays_in_sync_with_leader_strong_score():
    """No-careless-hardcode guard (owner rule 2026-07-10): the deployed
    DEXTER3_GOVERNOR_BYPASS_MIN_SCORE=0.74 in ops/dexter3-fable.service is
    documented as "= market_lens.LEADER_STRONG_SCORE, not a new number
    invented here" -- a literal duplicate the .service file cannot import.
    This test is the enforcement the comment alone can't provide: it reads
    the ACTUAL deployed value out of the tracked service file and asserts it
    still equals the actual constant. If either drifts, this fails loudly
    instead of the two silently disagreeing."""
    import re
    from dexter3.market_lens import LEADER_STRONG_SCORE

    service_path = (
        Path(__file__).resolve().parent.parent / "ops" / "dexter3-fable.service"
    )
    text = service_path.read_text(encoding="utf-8")
    m = re.search(r"^Environment=DEXTER3_GOVERNOR_BYPASS_MIN_SCORE=([\d.]+)\s*$", text, re.MULTILINE)
    assert m is not None, "DEXTER3_GOVERNOR_BYPASS_MIN_SCORE not found in ops/dexter3-fable.service"
    deployed_value = float(m.group(1))
    assert deployed_value == pytest.approx(LEADER_STRONG_SCORE)


def test_governor_bypass_scope_assumption_vp_daytrend_scalp_stay_below_threshold():
    """No-careless-hardcode guard: my 2026-07-22 report to the owner claimed
    "vp/daytrend/scalp hardcode leader_score so the bypass is a structural
    no-op there" -- self-audit caught that VP's ENTER decisions are actually
    0.5, not 0.0 (volume_profile.py:212), unlike daytrend/scalp which really
    are 0.0 (daytrend.py:139/158/242/269/311). The bypass is only actually
    safe on non-fable lanes because 0.5 and 0.0 both sit below the deployed
    0.74 threshold -- an implicit cross-file assumption nothing enforced.
    This test makes it explicit: it fails loudly the moment either producer's
    hardcoded score, or the deployed threshold, changes enough to close that
    gap, forcing a conscious decision instead of a silent bypass leak onto a
    lane that was never meant to have one."""
    import re
    from dexter3 import daytrend, volume_profile

    service_path = (
        Path(__file__).resolve().parent.parent / "ops" / "dexter3-fable.service"
    )
    text = service_path.read_text(encoding="utf-8")
    m = re.search(r"^Environment=DEXTER3_GOVERNOR_BYPASS_MIN_SCORE=([\d.]+)\s*$", text, re.MULTILINE)
    assert m is not None
    deployed_threshold = float(m.group(1))

    vp_entry = volume_profile._enter(
        ts_close="2026-07-22T00:00:00Z", symbol="XAUUSD", setup="vp_lvn_rejection",
        side="buy", entry=4000.0, sl=3995.0, tp=4010.0, session="london",
        reasons=["test"], features={},
    )
    assert vp_entry is not None
    assert vp_entry.leader_score == pytest.approx(0.5)
    assert vp_entry.leader_score < deployed_threshold

    daytrend_skip = daytrend._skip("2026-07-22T00:00:00Z", "XAUUSD", "london", "test")
    assert daytrend_skip.leader_score == pytest.approx(0.0)
    assert daytrend_skip.leader_score < deployed_threshold


def test_stamp_skip_bias_fallback_respects_anchor_hour_env(monkeypatch):
    # anchor_hour=12: the "day" starts at 12:00Z, so a bar at 01:00Z belongs
    # to the PREVIOUS day's anchor window and open_px comes from the first
    # bar at/after 12:00Z the PRIOR calendar day — verifies the env actually
    # reaches vp_lane.dayopen_bias rather than being ignored.
    monkeypatch.setenv("DEXTER3_SKIP_BIAS_ANCHOR_HOUR", "12")
    prefix = [
        _bar("2026-07-15T12:00:00Z", 4000.0, 4001.0, 3999.0, 4000.0),  # anchor bar @12:00Z
        _bar("2026-07-16T01:00:00Z", 4000.0, 4012.0, 3999.5, 4010.0),  # still within the same 24h anchor window
    ]
    d = FakeDecision(action="skip", features={})
    sr._stamp_skip_bias_fallback(d, prefix)
    assert d.features.get("skip_bias_side") == "buy"


# ---------------------------------------------------------------------------
# 5. _apply_v16_entry_quality_gate — grok minimum leader_score floor
# ---------------------------------------------------------------------------


def test_grok_min_leader_score_floor_blocks_low_score_by_default(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "grok")
    monkeypatch.delenv("DEXTER3_GROK_MIN_LEADER_SCORE", raising=False)
    d = FakeDecision(leader_score=0.056, features={})
    result = sr._apply_v16_entry_quality_gate({}, d)
    assert result["allow"] is False
    assert result["reason"] == "grok_min_leader_score"


def test_grok_min_leader_score_floor_allows_above_default(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "grok")
    monkeypatch.delenv("DEXTER3_GROK_MIN_LEADER_SCORE", raising=False)
    d = FakeDecision(leader_score=0.25, features={})
    result = sr._apply_v16_entry_quality_gate({}, d)
    assert result["allow"] is True
    assert result["reason"] == "grok_bypass"


def test_grok_min_leader_score_floor_disabled_by_non_positive_env(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "grok")
    monkeypatch.setenv("DEXTER3_GROK_MIN_LEADER_SCORE", "0")
    d = FakeDecision(leader_score=0.056, features={})
    result = sr._apply_v16_entry_quality_gate({}, d)
    assert result["allow"] is True
    assert result["reason"] == "grok_bypass"


def test_grok_min_leader_score_floor_handles_missing_leader_score_as_zero(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "grok")
    monkeypatch.delenv("DEXTER3_GROK_MIN_LEADER_SCORE", raising=False)
    d = SimpleNamespace(symbol="XAUUSD", setup="s", features={})  # no leader_score attr at all
    result = sr._apply_v16_entry_quality_gate({}, d)
    assert result["allow"] is False
    assert result["reason"] == "grok_min_leader_score"


def test_fable_mode_is_unaffected_by_the_grok_leader_score_floor(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "v16")
    d = FakeDecision(
        symbol="XAUUSD", setup="hunt_test", leader_score=0.0, features={},
    )
    result = sr._apply_v16_entry_quality_gate({}, d)
    # a leader_score of 0.0 would fail the grok floor if it wrongly applied
    # here — fable mode must run the real v16 gate instead.
    assert result["reason"] not in ("grok_bypass", "grok_min_leader_score")
    assert d.features.get("grok_bypass") is None
    assert "v16_entry_quality" in d.features


# ---------------------------------------------------------------------------
# 6. ops/dexter3_repair_cross_lane_vanish.py
# ---------------------------------------------------------------------------


def test_repair_script_dry_run_lists_only_poisoned_then_apply_deletes_only_it(tmp_path):
    from ops.dexter3_repair_cross_lane_vanish import find_poisoned_rows, main

    db_path = tmp_path / "repair_journal.db"
    conn = sqlite3.connect(str(db_path))
    ensure_exec_events_table(conn)
    # poisoned: vanish-writer signature (reconciled=True) + pnl exactly 0.0
    insert_exec_event(
        conn, symbol="BTCUSD", event="lane_position_closed", position_id=701, verified=True,
        payload={
            "reason": "broker_side_close_reconciled", "label": "dexter3:fable:m5h-v1",
            "pnl": 0.0, "exit_reason": "broker_side_close", "reconciled": True,
        },
    )
    # legit: same vanish signature, but a REAL nonzero pnl -> must NOT be listed
    insert_exec_event(
        conn, symbol="BTCUSD", event="lane_position_closed", position_id=702, verified=True,
        payload={
            "reason": "broker_side_close_reconciled", "label": "dexter3:grok-v1.0:scalper",
            "pnl": -6.4, "exit_reason": "broker_side_close", "reconciled": True,
        },
    )
    conn.close()

    conn_check = sqlite3.connect(str(db_path))
    poisoned = find_poisoned_rows(conn_check)
    assert len(poisoned) == 1
    assert poisoned[0]["position_id"] == 701
    conn_check.close()

    # dry-run (default): lists but changes nothing
    rc = main(["--db", str(db_path)])
    assert rc == 0
    conn_after_dry = sqlite3.connect(str(db_path))
    assert conn_after_dry.execute("SELECT COUNT(*) FROM exec_events").fetchone()[0] == 2
    conn_after_dry.close()

    # --apply: deletes exactly the poisoned row
    rc2 = main(["--db", str(db_path), "--apply"])
    assert rc2 == 0
    conn_after_apply = sqlite3.connect(str(db_path))
    remaining = [r[0] for r in conn_after_apply.execute("SELECT position_id FROM exec_events").fetchall()]
    conn_after_apply.close()
    assert remaining == [702]


def test_repair_script_never_touches_rows_without_the_vanish_signature(tmp_path):
    from ops.dexter3_repair_cross_lane_vanish import main

    db_path = tmp_path / "repair_journal_manual.db"
    conn = sqlite3.connect(str(db_path))
    ensure_exec_events_table(conn)
    # a normal manual close (close_lane_position's own writer) — no
    # "reconciled" key at all, even though it also happens to be pnl=0.0
    # (breakeven) — must never be treated as poisoned.
    insert_exec_event(
        conn, symbol="BTCUSD", event="lane_position_closed", position_id=703, verified=True,
        payload={"reason": "om_exit", "label": "dexter3:fable:m5h-v1", "pnl": 0.0, "exit_reason": "om_exit"},
    )
    conn.close()

    rc = main(["--db", str(db_path), "--apply"])
    assert rc == 0
    conn2 = sqlite3.connect(str(db_path))
    assert conn2.execute("SELECT COUNT(*) FROM exec_events").fetchone()[0] == 1
    conn2.close()
