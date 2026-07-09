"""HUNT MODE wiring tests: executor basket surface + runner glue helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dexter3.decision_journal import DecisionJournal
from dexter3.executor import LABEL, Dexter3Executor, ExecutorConfig
import dexter3.shadow_runner as sr


class FakeMcp:
    def __init__(self, positions: list[dict] | None = None) -> None:
        self._positions = list(positions or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.symbol_details = {"minVolume": 0.01, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0, "pipSize": 0.01}
        self.spot = {"bid": 62000.0, "ask": 62006.0}
        self.balance = {"traderId": 9922808, "balance": 10000.0}
        self.filled_next: dict | None = None

    def get_symbol_details(self, symbol: str) -> dict:
        self.calls.append(("get_symbol_details", {}))
        return dict(self.symbol_details)

    def get_spot_price(self, symbol: str) -> dict:
        self.calls.append(("get_spot_price", {}))
        return dict(self.spot)

    def get_trendbars(self, symbol: str, period: str, count: int) -> list[dict]:
        self.calls.append(("get_trendbars", {"period": period, "count": count}))
        return []

    def get_positions(self) -> list[dict]:
        self.calls.append(("get_positions", {}))
        out = list(self._positions)
        if self.filled_next is not None and any(c[0] == "place_market_order" for c in self.calls):
            out.append(dict(self.filled_next))
        return out

    def get_balance(self) -> dict:
        self.calls.append(("get_balance", {}))
        return dict(self.balance)

    def place_market_order(self, **kwargs: Any) -> dict:
        self.calls.append(("place_market_order", kwargs))
        return {"dealStatus": "FILLED"}

    def amend_position(self, position_id: int, stop_loss=None, take_profit=None) -> dict:
        self.calls.append(("amend_position", {"position_id": position_id}))
        return {"status": "ok"}

    def close_position(self, position_id: int) -> dict:
        self.calls.append(("close_position", {"position_id": position_id}))
        self._positions = [p for p in self._positions if int(p.get("positionId", 0)) != int(position_id)]
        return {"status": "closed"}


def _lane_pos(pid: int, label: str = LABEL, side: str = "Buy") -> dict:
    return {
        "positionId": pid, "symbolName": "BTCUSD", "tradeSide": side,
        "volumeInUnits": 0.01, "stopLoss": 61900.0, "takeProfit": 62150.0, "label": label,
    }


class EnterDecision:
    symbol = "BTCUSD"
    action = "enter"
    side = "buy"
    entry_type = "market"
    entry = 62003.0
    sl = 61940.0
    tp = 62120.0
    size_class = "scout"
    setup = "hunt_test"
    ts_close = "2026-07-05T12:00:00Z"
    reasons: list[str] = ["test"]
    features: dict[str, Any] = {}
    leader_score = 0.0
    p_win_est = 0.5

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "action": self.action}


@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "wiring_journal.db")
    yield j
    j.close()


# -- execute_close_all ---------------------------------------------------------


def test_close_all_closes_every_lane_position(journal):
    mcp = FakeMcp(positions=[_lane_pos(1), _lane_pos(2, side="Sell")])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    out = ex.execute_close_all([1, 2], reason="close_all_in_profit")
    assert out["action"] == "closed_all"
    assert out["closed"] == 2
    assert [c for c in mcp.calls if c[0] == "close_position"] == [
        ("close_position", {"position_id": 1}),
        ("close_position", {"position_id": 2}),
    ]


def test_close_all_refuses_foreign_label_positions(journal):
    mcp = FakeMcp(positions=[_lane_pos(1), _lane_pos(9, label="dexter-scalp:codex:btc-v1")])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    out = ex.execute_close_all([1, 9], reason="cap_stop")
    assert out["action"] == "close_all_partial"
    assert out["closed"] == 1
    closed_ids = [c[1]["position_id"] for c in mcp.calls if c[0] == "close_position"]
    assert closed_ids == [1]  # peer position never receives a close call


# -- basket_authorized bypass ---------------------------------------------------


def test_repair_leg_bypasses_duplicate_guard_only(journal):
    mcp = FakeMcp(positions=[_lane_pos(1)])
    mcp.filled_next = _lane_pos(2, side="Sell")
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    plain = ex.execute_entry(EnterDecision(), {"traderId": 9922808})
    assert plain["action"] == "refused"
    assert plain["reason"] == "duplicate_label_position_open"
    repaired = ex.execute_repair_leg(EnterDecision(), {"traderId": 9922808})
    assert repaired["action"] != "refused" or repaired.get("reason") != "duplicate_label_position_open"


def test_repair_leg_still_enforces_demo_gate(journal):
    mcp = FakeMcp(positions=[_lane_pos(1)])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    out = ex.execute_repair_leg(EnterDecision(), {"traderId": 111111})
    assert out["action"] == "refused"
    assert out["reason"] == "account_not_confirmed_demo"


# -- runner glue helpers ---------------------------------------------------------


def test_daily_state_rollover(monkeypatch):
    state: dict[str, Any] = {}
    d1 = sr._daily_state(state)
    d1["entries"] = 4
    d1["loss_baskets"] = 1
    same = sr._daily_state(state)
    assert same["entries"] == 4  # same day: counters preserved

    import datetime as _dt

    class Tomorrow:
        strptime = _dt.datetime.strptime
        fromisoformat = _dt.datetime.fromisoformat
        fromtimestamp = _dt.datetime.fromtimestamp

        @staticmethod
        def now(tz=None):
            return _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)

    monkeypatch.setattr(sr, "datetime", Tomorrow)
    d2 = sr._daily_state(state)
    assert d2["entries"] == 0 and d2["loss_baskets"] == 0  # new day resets


def _chop_bars(n: int = 50, base: float = 62000.0) -> list[dict[str, Any]]:
    bars = []
    for i in range(n):
        px = base + (7.0 if i % 2 == 0 else -7.0)
        bars.append({
            "ts": f"2026-07-05T{8 + i // 12:02d}:{(i % 12) * 5:02d}:00Z",
            "open": px, "high": px + 30.0, "low": px - 30.0, "close": px + (5.0 if i % 2 else -5.0),
        })
    return bars


# -- get_trendbars symbol-resubscribe (no-trades incident 2026-07-08) -----------


def test_get_trendbars_resubscribes_on_symbol_unavailable(monkeypatch):
    """After a cTrader restart the symbol can lose its subscription; the entry
    path calls get_trendbars FIRST, so without a self-heal the loop silently
    stops trading (live: ~1h of no entries). get_trendbars must force an
    open_chart subscribe and retry once on 'Symbol not available'."""
    from dexter3.mcp_client import Dexter3McpClient, McpClientError

    c = Dexter3McpClient()
    calls = []

    def fake_call(name, args=None):
        calls.append(name)
        if name == "get_trendbars":
            # fail until an open_chart has happened
            if "open_chart" not in calls:
                raise McpClientError("MCP tool get_trendbars returned error: Symbol not available: XAUUSD")
            return {"bars": [{"open": 1, "high": 2, "low": 1, "close": 1.5, "ts": "2026-07-08T00:00:00Z"}]}
        if name == "open_chart":
            return {"ok": True}
        return {}

    monkeypatch.setattr(c, "call", fake_call)
    monkeypatch.setattr("dexter3.mcp_client.time.sleep", lambda s: None)

    bars = c.get_trendbars("XAUUSD", "m5", 1)
    assert len(bars) == 1
    assert "open_chart" in calls  # it subscribed
    assert calls.count("get_trendbars") == 2  # failed once, retried after subscribe


def test_get_trendbars_does_not_resubscribe_on_other_errors(monkeypatch):
    from dexter3.mcp_client import Dexter3McpClient, McpClientError

    c = Dexter3McpClient()
    calls = []

    def fake_call(name, args=None):
        calls.append(name)
        if name == "get_trendbars":
            raise McpClientError("some other MCP failure")
        return {}

    monkeypatch.setattr(c, "call", fake_call)
    monkeypatch.setattr("dexter3.mcp_client.time.sleep", lambda s: None)

    with pytest.raises(McpClientError):
        c.get_trendbars("XAUUSD", "m5", 1)
    assert "open_chart" not in calls  # a non-symbol error must NOT trigger subscribe
    assert calls.count("get_trendbars") == 1  # no retry


# -- mutation-uncertainty (double-fill guard) -----------------------------------


def test_client_never_retries_mutating_tools(monkeypatch):
    from dexter3.mcp_client import (
        Dexter3McpClient,
        McpMutationUncertain,
        McpTransportError,
    )

    c = Dexter3McpClient()
    attempts = {"n": 0}

    def failing_call_once(name, args=None):
        attempts["n"] += 1
        raise McpTransportError("boom")

    monkeypatch.setattr(c, "_call_once", failing_call_once)
    monkeypatch.setattr("dexter3.mcp_client.time.sleep", lambda s: None)

    with pytest.raises(McpMutationUncertain):
        c.call("place_market_order", {})
    assert attempts["n"] == 1  # SINGLE attempt — no blind retry

    attempts["n"] = 0
    with pytest.raises(McpTransportError):
        c.call("get_positions")
    assert attempts["n"] == 2  # reads still retry once


def test_uncertain_entry_reconciles_instead_of_refusing(journal, monkeypatch):
    from dexter3.mcp_client import McpMutationUncertain

    class UncertainMcp(FakeMcp):
        def place_market_order(self, **kwargs: Any) -> dict:
            self.calls.append(("place_market_order", kwargs))
            # broker filled it, but the response never arrived
            self._positions.append(_lane_pos(777))
            raise McpMutationUncertain("timeout mid-order")

    monkeypatch.setattr("dexter3.executor.time.sleep", lambda s: None)
    mcp = UncertainMcp(positions=[])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    out = ex.execute_entry(EnterDecision(), {"traderId": 9922808})
    assert out["action"] == "entered"
    assert out["position_id"] == 777
    assert out["order"]["transport"] == "uncertain_reconciled"
    assert len([c for c in mcp.calls if c[0] == "place_market_order"]) == 1


def test_uncertain_entry_not_filled_refuses(journal, monkeypatch):
    from dexter3.mcp_client import McpMutationUncertain

    class UncertainMcp(FakeMcp):
        def place_market_order(self, **kwargs: Any) -> dict:
            self.calls.append(("place_market_order", kwargs))
            raise McpMutationUncertain("timeout mid-order")  # and NO fill

    monkeypatch.setattr("dexter3.executor.time.sleep", lambda s: None)
    mcp = UncertainMcp(positions=[])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    out = ex.execute_entry(EnterDecision(), {"traderId": 9922808})
    assert out["action"] == "refused"
    assert out["reason"] == "mutation_uncertain_not_filled"
    assert len([c for c in mcp.calls if c[0] == "place_market_order"]) == 1


def test_uncertain_close_trusts_post_reread(journal, monkeypatch):
    from dexter3.mcp_client import McpMutationUncertain

    class UncertainMcp(FakeMcp):
        def close_position(self, position_id: int) -> dict:
            self.calls.append(("close_position", {"position_id": position_id}))
            # the close actually executed broker-side
            self._positions = [p for p in self._positions if int(p.get("positionId", 0)) != int(position_id)]
            raise McpMutationUncertain("timeout mid-close")

    monkeypatch.setattr("dexter3.executor.time.sleep", lambda s: None)
    mcp = UncertainMcp(positions=[_lane_pos(5)])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    out = ex.close_lane_position(5, reason="test")
    assert out["action"] == "closed"  # post re-read confirmed it is gone


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_repair_geometry_cost_guards(side):
    spread = 12.0
    entry, sl, tp = sr._repair_geometry(_chop_bars(), side, spread)
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    assert sl_dist >= 6 * spread - 1e-6
    assert tp_dist >= 8 * spread - 1e-6
    assert tp_dist >= 1.2 * sl_dist - 1e-6
    if side == "buy":
        assert sl < entry < tp
    else:
        assert tp < entry < sl


# ---------------------------------------------------------------------------
# FIX 2 (2026-07-07) — per-basket peak-R runtime state wiring
# ---------------------------------------------------------------------------


def test_basket_runtime_for_fresh_basket_starts_at_this_bars_aggregate_r():
    state: dict = {}
    runtime = sr._basket_runtime_for(state, "XAUUSD", "2026-07-06T10:00:00Z", 0.3)
    assert runtime["peak_r"] == pytest.approx(0.3)
    assert runtime["oldest_open_ts"] == "2026-07-06T10:00:00Z"
    assert state["basket_runtime"]["XAUUSD"] == runtime


def test_basket_runtime_for_tracks_peak_across_calls_same_basket():
    state: dict = {}
    sr._basket_runtime_for(state, "XAUUSD", "2026-07-06T10:00:00Z", 0.3)
    sr._basket_runtime_for(state, "XAUUSD", "2026-07-06T10:00:00Z", 0.7)  # new high -> peak updates
    runtime = sr._basket_runtime_for(state, "XAUUSD", "2026-07-06T10:00:00Z", 0.4)  # retrace -> peak holds
    assert runtime["peak_r"] == pytest.approx(0.7)


def test_basket_runtime_for_resets_on_new_oldest_open_ts():
    state: dict = {}
    sr._basket_runtime_for(state, "XAUUSD", "2026-07-06T10:00:00Z", 0.9)  # basket A peaks at 0.9
    # Basket A resolved, a brand-new basket B opens with a different oldest leg.
    runtime = sr._basket_runtime_for(state, "XAUUSD", "2026-07-06T14:00:00Z", 0.1)
    assert runtime["peak_r"] == pytest.approx(0.1), "new basket must not inherit the old basket's peak"
    assert runtime["oldest_open_ts"] == "2026-07-06T14:00:00Z"


def test_basket_runtime_for_resets_when_oldest_open_ts_missing():
    state: dict = {}
    sr._basket_runtime_for(state, "XAUUSD", "2026-07-06T10:00:00Z", 0.9)
    runtime = sr._basket_runtime_for(state, "XAUUSD", None, 0.05)
    assert runtime["peak_r"] == pytest.approx(0.05)


def test_basket_runtime_for_per_symbol_isolation():
    state: dict = {}
    sr._basket_runtime_for(state, "XAUUSD", "2026-07-06T10:00:00Z", 0.8)
    sr._basket_runtime_for(state, "BTCUSD", "2026-07-06T10:00:00Z", 0.1)
    assert state["basket_runtime"]["XAUUSD"]["peak_r"] == pytest.approx(0.8)
    assert state["basket_runtime"]["BTCUSD"]["peak_r"] == pytest.approx(0.1)


def test_clear_basket_runtime_removes_symbol_entry():
    state: dict = {}
    sr._basket_runtime_for(state, "XAUUSD", "2026-07-06T10:00:00Z", 0.8)
    sr._clear_basket_runtime(state, "XAUUSD")
    assert "XAUUSD" not in state.get("basket_runtime", {})


def test_clear_basket_runtime_noop_when_absent():
    state: dict = {}
    sr._clear_basket_runtime(state, "XAUUSD")  # must not raise
    assert state.get("basket_runtime", {}) == {}


def test_basket_config_from_env_reads_new_trail_knobs(monkeypatch):
    monkeypatch.setenv("DEXTER3_ARM_TRAIL_R", "0.4")
    monkeypatch.setenv("DEXTER3_TRAIL_KEEP_FRAC", "0.7")
    monkeypatch.setenv("DEXTER3_TAKE_R", "1.3")
    monkeypatch.setenv("DEXTER3_RESOLVE_TARGET_R", "0.25")
    cfg = sr._basket_config_from_env()
    assert cfg.arm_trail_r == pytest.approx(0.4)
    assert cfg.trail_keep_frac == pytest.approx(0.7)
    assert cfg.take_r == pytest.approx(1.3)
    assert cfg.resolve_target_r == pytest.approx(0.25)


def test_basket_config_from_env_defaults_when_unset(monkeypatch):
    for env in (
        "DEXTER3_ARM_TRAIL_R",
        "DEXTER3_TRAIL_KEEP_FRAC",
        "DEXTER3_TAKE_R",
        "DEXTER3_RESOLVE_TARGET_R",
        "DEXTER3_DAILY_LOSS_BASKETS",
    ):
        monkeypatch.delenv(env, raising=False)
    cfg = sr._basket_config_from_env()
    from dexter3.basket_manager import BasketConfig

    default = BasketConfig()
    assert cfg.arm_trail_r == default.arm_trail_r
    assert cfg.trail_keep_frac == default.trail_keep_frac
    assert cfg.take_r == default.take_r
    assert cfg.resolve_target_r == default.resolve_target_r


def test_basket_config_from_env_ignores_invalid_value(monkeypatch):
    monkeypatch.setenv("DEXTER3_ARM_TRAIL_R", "not_a_float")
    cfg = sr._basket_config_from_env()
    from dexter3.basket_manager import BasketConfig

    assert cfg.arm_trail_r == BasketConfig().arm_trail_r  # falls back to default, does not raise


# ---------------------------------------------------------------------------
# OPENING MANAGER (owner directive 2026-07-07) — fast-tick scheduling helpers
# ---------------------------------------------------------------------------


def test_m5_entry_tick_due_fires_on_first_tick():
    # Entries must not wait a full poll_sec before the loop's first M5 pass.
    assert sr.m5_entry_tick_due(1, poll_sec=20, fast_tick_sec=4) is True


def test_m5_entry_tick_due_fires_every_ceil_poll_over_fast_tick_ticks():
    # poll_sec=20, fast_tick_sec=4 -> ticks_per_poll=5 -> due on ticks 5, 10, 15...
    due_ticks = [t for t in range(1, 21) if sr.m5_entry_tick_due(t, poll_sec=20, fast_tick_sec=4)]
    assert due_ticks == [1, 5, 10, 15, 20]


def test_m5_entry_tick_due_handles_non_evenly_divisible_poll_sec():
    # poll_sec=20, fast_tick_sec=7 -> ceil(20/7)=3 -> due every 3rd tick (+ tick 1)
    due_ticks = [t for t in range(1, 13) if sr.m5_entry_tick_due(t, poll_sec=20, fast_tick_sec=7)]
    assert due_ticks == [1, 3, 6, 9, 12]


def test_m5_entry_tick_due_true_every_tick_when_fast_tick_sec_zero_or_negative():
    # Defensive: a misconfigured fast_tick_sec must never silently starve
    # the M5 entry path — fail open (run every tick) rather than never.
    assert sr.m5_entry_tick_due(2, poll_sec=20, fast_tick_sec=0) is True
    assert sr.m5_entry_tick_due(2, poll_sec=20, fast_tick_sec=-1) is True


def test_om_bars_refresh_due_first_call_and_after_interval():
    assert sr.om_bars_refresh_due(0.0, 1000.0, refresh_sec=30) is True
    assert sr.om_bars_refresh_due(980.0, 1000.0, refresh_sec=30) is False
    assert sr.om_bars_refresh_due(965.0, 1000.0, refresh_sec=30) is True


def test_fast_tick_sec_from_env_default_and_override(monkeypatch):
    monkeypatch.delenv("DEXTER3_FAST_TICK_SEC", raising=False)
    assert sr._fast_tick_sec_from_env() == sr.DEFAULT_FAST_TICK_SEC
    monkeypatch.setenv("DEXTER3_FAST_TICK_SEC", "7")
    assert sr._fast_tick_sec_from_env() == 7
    monkeypatch.setenv("DEXTER3_FAST_TICK_SEC", "not_an_int")
    assert sr._fast_tick_sec_from_env() == sr.DEFAULT_FAST_TICK_SEC


def test_om_config_from_env_reads_knobs_and_ignores_invalid(monkeypatch):
    monkeypatch.setenv("DEXTER3_OM_ARM_R", "0.3")
    monkeypatch.setenv("DEXTER3_OM_TRAIL_KEEP", "0.8")
    monkeypatch.setenv("DEXTER3_OM_TAKE_R", "1.5")
    monkeypatch.setenv("DEXTER3_OM_SPIKE_R", "3.0")
    monkeypatch.setenv("DEXTER3_OM_REPAIR_TRIGGER_R", "-0.6")
    monkeypatch.setenv("DEXTER3_OM_REPAIR_MIN_CONV", "0.5")
    cfg = sr._om_config_from_env()
    assert cfg.arm_trail_r == pytest.approx(0.3)
    assert cfg.trail_keep_frac == pytest.approx(0.8)
    assert cfg.take_r == pytest.approx(1.5)
    assert cfg.spike_take_r == pytest.approx(3.0)
    assert cfg.repair_trigger_r == pytest.approx(-0.6)
    assert cfg.repair_min_conviction == pytest.approx(0.5)

    monkeypatch.setenv("DEXTER3_OM_ARM_R", "garbage")
    cfg2 = sr._om_config_from_env()
    from dexter3.opening_manager import OMConfig

    assert cfg2.arm_trail_r == OMConfig().arm_trail_r  # falls back to default, does not raise


def test_run_om_tick_dry_mode_never_calls_executor_mutations(journal, monkeypatch):
    """Shadow mode (executor=None): OM must still evaluate + journal the
    would-be action, but must place/close NOTHING."""
    monkeypatch.setattr(sr, "_OM_BAR_CACHE", {})
    monkeypatch.setattr(sr, "_OM_INSTANCE", None)
    mcp = FakeMcp(positions=[_lane_pos(1)])
    state: dict = {}
    status = sr.run_om_tick(mcp, journal, state, "BTCUSD", executor=None)
    assert status.startswith("om_")
    # dry mode never calls place_market_order / close_position
    mutating_calls = [c for c in mcp.calls if c[0] in ("place_market_order", "close_position", "amend_position")]
    assert mutating_calls == []


def test_run_om_tick_no_lane_clears_runtime_and_is_noop(journal, monkeypatch):
    monkeypatch.setattr(sr, "_OM_BAR_CACHE", {})
    monkeypatch.setattr(sr, "_OM_INSTANCE", None)
    mcp = FakeMcp(positions=[])
    state: dict = {"basket_runtime": {"BTCUSD": {"oldest_open_ts": "x", "peak_r": 1.0}}}
    status = sr.run_om_tick(mcp, journal, state, "BTCUSD", executor=None)
    assert status == "om_no_lane"
    assert "BTCUSD" not in state.get("basket_runtime", {})


def test_active_order_label_selects_v16_or_grok_label():
    from dexter3.grok_v10 import GROK_LABEL

    assert sr._active_order_label("v16") == LABEL
    assert sr._active_order_label("grok") == GROK_LABEL


def test_active_state_file_selects_separate_v16_and_grok_files():
    assert sr._active_state_file("v16").name == "dexter3_shadow_state.json"
    assert sr._active_state_file("grok").name == "dexter3_grok_shadow_state.json"
    assert sr._active_state_file("v16") != sr._active_state_file("grok")


def test_grok_runtime_clear_does_not_touch_v16_runtime():
    state = {
        "basket_runtime": {"XAUUSD": {"peak_r": 1.2}},
        "grok_v10_basket_runtime": {"XAUUSD": {"peak_r": 0.4}},
    }
    sr._clear_basket_runtime(state, "XAUUSD", grok=True)
    assert state["basket_runtime"]["XAUUSD"]["peak_r"] == pytest.approx(1.2)
    assert "XAUUSD" not in state["grok_v10_basket_runtime"]


# ---------------------------------------------------------------------------
# V1.6 profit controls (2026-07-09) — green-day scaling + weak-bucket scout
# ---------------------------------------------------------------------------


def _profit_control_decision(setup: str) -> EnterDecision:
    d = EnterDecision()
    d.symbol = "XAUUSD"
    d.setup = setup
    d.features = {}
    return d


def test_v16_profit_controls_downsize_weak_bucket_before_green(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "v16")
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (0.0, []))
    d = _profit_control_decision("hunt_m15_drift")
    risk = sr._apply_v16_profit_controls(FakeMcp(), {"governor": {"floating_by_symbol": {}}}, d, 12.0)
    assert risk == pytest.approx(3.0)
    assert d.features["v16_profit_control"]["reason"] == "weak_bucket_downsize"


def test_v16_profit_controls_do_not_scale_winner_before_green(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "v16")
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (19.0, []))
    d = _profit_control_decision("hunt_h1_context")
    risk = sr._apply_v16_profit_controls(FakeMcp(), {"governor": {"floating_by_symbol": {}}}, d, 12.0)
    assert risk == pytest.approx(12.0)
    assert d.features["v16_profit_control"]["reason"] == "winner_waiting_for_green_day"


def test_v16_profit_controls_scale_winner_after_green(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "v16")
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (22.0, []))
    d = _profit_control_decision("hunt_swing_structure")
    risk = sr._apply_v16_profit_controls(FakeMcp(), {"governor": {"floating_by_symbol": {}}}, d, 12.0)
    assert risk == pytest.approx(24.0)
    assert d.features["v16_profit_control"]["reason"] == "green_day_winner_scale"


def test_v16_profit_controls_house_money_scale_after_cushion(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "v16")
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (35.0, []))
    d = _profit_control_decision("basket_repair")
    risk = sr._apply_v16_profit_controls(FakeMcp(), {"governor": {"floating_by_symbol": {}}}, d, 12.0)
    assert risk == pytest.approx(36.0)
    assert d.features["v16_profit_control"]["reason"] == "house_money_winner_scale"


def test_v16_profit_controls_skip_grok_mode(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "grok")
    monkeypatch.setattr(sr, "_lane_realized_today", lambda mcp, label_filter: (100.0, []))
    d = _profit_control_decision("hunt_m15_drift")
    risk = sr._apply_v16_profit_controls(FakeMcp(), {"governor": {"floating_by_symbol": {}}}, d, 12.0)
    assert risk == pytest.approx(12.0)
    assert "v16_profit_control" not in d.features


def test_v16_house_money_arms_then_locks_floor(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "v16")
    monkeypatch.setenv("DEXTER3_V16_HOUSE_THRESHOLD_USD", "30")
    monkeypatch.setenv("DEXTER3_V16_HOUSE_FLOOR_USD", "20")
    gov_state: dict[str, Any] = {}
    armed = sr._apply_v16_house_money_status(gov_state, {"state": "HUNTING", "effective_pnl": 35.0})
    assert armed["state"] == "HUNTING"
    assert gov_state["house_money_armed"] is True
    assert gov_state["house_money_floor_usd"] == pytest.approx(20.0)

    locked = sr._apply_v16_house_money_status(gov_state, {"state": "HUNTING", "effective_pnl": 19.5})
    assert locked["state"] == "TARGET_LOCKED"
    assert locked["house_money_floor_triggered"] is True


# -- account guard alarm (owner directive 2026-07-09: force demo 9922808) ----


class _GuardClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, name: str, args: dict[str, Any] | None = None) -> Any:
        if self.fail:
            raise RuntimeError("mcp down")
        self.calls.append((name, dict(args or {})))
        return {"ok": True}


def _reset_guard_throttle() -> None:
    sr._account_guard_last_notify = 0.0


def test_account_guard_alerts_on_demo_refusal():
    _reset_guard_throttle()
    client = _GuardClient()
    result = {"action": "refused", "reason": "account_not_confirmed_demo", "trader_id": 2017746}
    assert sr._maybe_alert_account_guard(client, result) is True
    assert client.calls and client.calls[0][0] == "show_notification"
    payload = client.calls[0][1]
    assert "9922808" in payload["description"]
    assert payload["type"] == "error"


def test_account_guard_throttles_repeat_notifications():
    _reset_guard_throttle()
    client = _GuardClient()
    result = {"action": "refused", "reason": "account_not_confirmed_demo"}
    assert sr._maybe_alert_account_guard(client, result) is True
    assert sr._maybe_alert_account_guard(client, result) is False
    assert len(client.calls) == 1


def test_account_guard_ignores_other_refusals_and_entries():
    _reset_guard_throttle()
    client = _GuardClient()
    assert sr._maybe_alert_account_guard(client, {"action": "refused", "reason": "spread_too_wide"}) is False
    assert sr._maybe_alert_account_guard(client, {"action": "entered", "position_id": 1}) is False
    assert sr._maybe_alert_account_guard(client, None) is False
    assert client.calls == []


def test_account_guard_never_raises_when_notification_fails():
    _reset_guard_throttle()
    client = _GuardClient(fail=True)
    result = {"action": "refused", "reason": "account_not_confirmed_demo"}
    assert sr._maybe_alert_account_guard(client, result) is False


# -- V1.8 size lever application (shadow_runner._apply_v18_size_levers) ------


def test_v18_size_levers_noop_when_defaults():
    assert sr._apply_v18_size_levers({}, 14.4, 5.04) == pytest.approx(5.04)
    assert sr._apply_v18_size_levers({"size_mult": 1.0, "size_floor_frac": 0.0}, 14.4, 5.04) == pytest.approx(5.04)


def test_v18_size_levers_boost_and_cap():
    # boost 1.6 on full-size chain risk
    assert sr._apply_v18_size_levers({"size_mult": 1.6}, 14.4, 14.4) == pytest.approx(23.04)
    # cap at governor capital*max_risk_frac (default 1000*0.025=25)
    assert sr._apply_v18_size_levers({"size_mult": 1.6}, 20.0, 20.0) == pytest.approx(25.0)


def test_v18_size_levers_rescue_floor():
    # A+ chase: chain crushed risk to 2.16; floor lifts to 0.5 x governor 14.4
    assert sr._apply_v18_size_levers({"size_floor_frac": 0.5}, 14.4, 2.16) == pytest.approx(7.2)
    # floor never lowers a larger chain risk
    assert sr._apply_v18_size_levers({"size_floor_frac": 0.5}, 14.4, 10.0) == pytest.approx(10.0)


def test_v18_b_tier_mult_halves_chain_risk():
    assert sr._apply_v18_size_levers({"size_mult": 0.5}, 14.4, 5.04) == pytest.approx(2.52)


# -- min-volume risk ratio cap (Grok supplement guard 2026-07-09) ------------


XAU_DETAILS = {"minVolume": 1.0, "maxVolume": 100.0, "volumeStep": 1.0, "lotSize": 100.0, "pipSize": 0.01}


def test_min_volume_ratio_cap_off_by_default_accepts_clamp_up(monkeypatch):
    monkeypatch.delenv("DEXTER3_MIN_VOLUME_RISK_RATIO_CAP", raising=False)
    from dexter3.executor import planned_volume_units
    # design $4.8 risk, SL 11 pts -> raw 0.43oz -> clamped UP to 1oz = $11 risk
    vol, meta = planned_volume_units(XAU_DETAILS, sl_distance=11.0, risk_usd=4.8, max_volume_units=10.0)
    assert vol == pytest.approx(1.0)
    assert meta.get("min_volume_clamped_up") is True
    assert meta.get("refuse_reason") is None
    assert meta["estimated_min_volume_risk_usd"] == pytest.approx(11.0)


def test_min_volume_ratio_cap_refuses_oversized_floor(monkeypatch):
    monkeypatch.setenv("DEXTER3_MIN_VOLUME_RISK_RATIO_CAP", "1.5")
    from dexter3.executor import planned_volume_units
    # $11 floor risk > 1.5 x $4.8 = $7.2 -> refuse
    vol, meta = planned_volume_units(XAU_DETAILS, sl_distance=11.0, risk_usd=4.8, max_volume_units=10.0)
    assert vol == 0.0
    assert meta["refuse_reason"] == "min_volume_risk_exceeds_ratio_cap"


def test_min_volume_ratio_cap_allows_within_ratio(monkeypatch):
    monkeypatch.setenv("DEXTER3_MIN_VOLUME_RISK_RATIO_CAP", "1.5")
    from dexter3.executor import planned_volume_units
    # SL 6 pts -> floor risk $6 <= 1.5 x $4.8 = $7.2 -> accepted clamp-up
    vol, meta = planned_volume_units(XAU_DETAILS, sl_distance=6.0, risk_usd=4.8, max_volume_units=10.0)
    assert vol == pytest.approx(1.0)
    assert meta.get("refuse_reason") is None


def test_min_volume_ratio_cap_irrelevant_without_clamp_up(monkeypatch):
    monkeypatch.setenv("DEXTER3_MIN_VOLUME_RISK_RATIO_CAP", "1.5")
    from dexter3.executor import planned_volume_units
    # $17.5 risk, SL 5 pts -> raw 3.5oz, no clamp-up -> cap never fires (V1.8 lane unaffected)
    vol, meta = planned_volume_units(XAU_DETAILS, sl_distance=5.0, risk_usd=17.5, max_volume_units=10.0)
    assert vol == pytest.approx(3.0)
    assert meta.get("min_volume_clamped_up") is None
    assert meta.get("refuse_reason") is None
