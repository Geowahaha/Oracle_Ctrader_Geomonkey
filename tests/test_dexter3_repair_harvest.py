"""Tests for the Repair-Scalp Harvester (owner hypothesis, 2026-07-15) —
see docs/AGENT_SYNC_BOARD.md 2026-07-15 ~08:15Z and
scripts/dexter3_repair_scalp_replay.py for the exact semantics this engine
keeps identical: when a lane basket is TRAPPED (single leg, aggregate_r <=
-trigger_r, reliable pnl), harvest the mirror side with v1.0-style
bank-green scalps until the parent resolves.

NO live MCP calls, NO touching data/runtime/* — every test drives a fake
transport / temp sqlite DB / tmp_path state file, same conventions as
tests/test_dexter3_om_blindspot_fixes.py.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dexter3.executor as ex_mod
import dexter3.shadow_runner as sr
from dexter3.basket_live import aggregate_lane, lane_positions, repair_harvest_legs
from dexter3.decision_journal import DecisionJournal
from dexter3.executor import Dexter3Executor, ExecutorConfig, recent_exec_events


# ---------------------------------------------------------------------------
# shared fixtures / fakes (mirrors tests/test_dexter3_om_blindspot_fixes.py)
# ---------------------------------------------------------------------------


class FakeMcp:
    """Records calls; canned responses configurable per test via attrs."""

    def __init__(self) -> None:
        self.symbol_details = {
            "minVolume": 0.01, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0, "pipSize": 0.01,
        }
        self.spot = {"bid": 100.0, "ask": 100.1}
        self._positions: list[dict] = []
        self.order_response = {"dealStatus": "FILLED"}
        self.post_entry_position: dict | None = None
        self.balance = {"traderId": 9922808, "balance": 10000.0}
        self.deals: list[dict] = []
        self.m5_bars: list[dict] = []
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

    def get_trendbars(self, symbol: str, period: str, count: int) -> list[dict]:
        self.calls.append(("get_trendbars", {"period": period, "count": count}))
        if period == "m5":
            return list(self.m5_bars)
        return []

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
        self._positions = [p for p in self._positions if int(p.get("positionId", 0) or 0) != position_id]
        return {"status": "closed"}

    def get_deals(self, count: int = 200, from_timestamp_ms: int | None = None) -> list[dict]:
        self.calls.append(("get_deals", {"count": count, "from_timestamp_ms": from_timestamp_ms}))
        return list(self.deals)


DEMO_ACCOUNT = {"traderId": 9922808}


def _lane_pos(
    pid: int,
    pnl: float,
    *,
    side: str = "Sell",
    entry: float = 100.0,
    sl: float = 110.0,
    tp: float = 70.0,
    volume: float = 1.0,
    open_time: str = "2026-07-15T04:00:00Z",
    label: str | None = None,
) -> dict:
    return {
        "positionId": pid,
        "symbolName": "XAUUSD",
        "tradeSide": side,
        "volumeInUnits": volume,
        "entryPrice": entry,
        "stopLoss": sl,
        "takeProfit": tp,
        "label": label or ex_mod.LABEL,
        "netProfit": pnl,
        "openTime": open_time,
    }


def _m5_bar(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "repair_harvest_journal.db")
    yield j
    j.close()


def _isolate_shadow_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sr, "STATE_FILE", tmp_path / "dexter3_shadow_state.json")
    monkeypatch.setattr(sr, "log_line", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sr, "log_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sr, "_OM_BAR_CACHE", {})
    monkeypatch.setattr(sr, "_OM_INSTANCE", None)
    monkeypatch.setattr(sr, "_RSH_MODE_WARNED", set())


def _basket_events(journal: DecisionJournal, event: str | None = None) -> list[dict]:
    rows = journal.recent_basket_events(limit=500)
    if event is None:
        return rows
    return [r for r in rows if r["event"] == event]


# ---------------------------------------------------------------------------
# basket_live: rsh exclusion / repair_harvest_legs
# ---------------------------------------------------------------------------


def test_lane_positions_excludes_rsh_suffix_when_requested():
    parent = _lane_pos(1, -13.0, label="dexter3:fable:v1.8")
    scalp = _lane_pos(2, 0.5, side="Buy", label="dexter3:fable:v1.8:rsh")
    positions = [parent, scalp]

    # default behavior unchanged (no exclude) -- both included
    assert lane_positions(positions, "dexter3:fable") == [parent, scalp]

    # exclude_label_suffix drops ONLY the rsh leg
    excl = lane_positions(positions, "dexter3:fable", exclude_label_suffix="rsh")
    assert excl == [parent]

    # repair_harvest_legs is the complementary view: ONLY the rsh leg
    only_scalps = repair_harvest_legs(positions, "dexter3:fable", "rsh")
    assert only_scalps == [scalp]


def test_rsh_exclusion_leaves_basket_aggregate_r_unchanged_by_open_scalp():
    parent = _lane_pos(501, -13.0, label="dexter3:fable:v1.8")
    scalp = _lane_pos(502, +50.0, side="Buy", label="dexter3:fable:v1.8:rsh")
    positions = [parent, scalp]

    lane_excl = lane_positions(positions, "dexter3:fable", exclude_label_suffix="rsh")
    agg_with_scalp_excluded = aggregate_lane(lane_excl, base_risk_usd=10.0)

    agg_parent_only = aggregate_lane([parent], base_risk_usd=10.0)
    assert agg_with_scalp_excluded["aggregate_r"] == pytest.approx(agg_parent_only["aggregate_r"])
    assert agg_with_scalp_excluded["legs"] == 1


def test_vanish_reconcile_still_owns_the_scalp_close_despite_exclusion(journal, monkeypatch):
    """Even though the harvester's own lane view excludes ":rsh" legs, the
    executor's vanish-reconcile is family-prefix-based and must still own
    (journal) the scalp's REAL broker-side close once a deal appears."""
    mcp = FakeMcp()
    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:v-test")
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())

    scalp_label = "dexter3:fable:v-test:rsh"
    mcp.post_entry_position = {
        "positionId": 900, "symbolName": "XAUUSD", "tradeSide": "Buy",
        "volumeInUnits": 1.0, "stopLoss": 90.0, "takeProfit": 5000.0, "label": scalp_label,
    }
    from dexter3.executor import position_id_of  # noqa: F401 - sanity import only

    class FakeDecision(SimpleNamespace):
        pass

    decision = FakeDecision(
        symbol="XAUUSD", action="enter", side="buy", entry=100.0, sl=90.0, tp=5000.0,
        setup="repair_harvest", reasons=["test"], session="london",
    )
    result = ex.execute_entry(decision, DEMO_ACCOUNT, basket_authorized=True, label_override=scalp_label)
    assert result["action"] == "entered" and result["position_id"] == 900
    assert result["label"] == scalp_label
    mcp.calls = [c for c in mcp.calls if c[0] != "place_market_order"]
    mcp.post_entry_position = None

    # broker closed it (SL fill) -- rsh-EXCLUDED lane is now empty, but the
    # RAW open_positions (unfiltered) reflects the closed state too here.
    mcp._positions = []
    mcp.deals = [
        {"positionId": 900, "netProfit": -10.0, "hasCloseDetail": True, "time": "2026-07-15T05:00:00Z"},
    ]
    reconciled = ex.reconcile_vanished_lane_positions("XAUUSD", [])  # rsh-excluded lane -> []
    assert len(reconciled) == 1
    assert reconciled[0]["position_id"] == 900
    assert reconciled[0]["pnl"] == pytest.approx(-10.0)

    events = recent_exec_events(journal._conn, symbol="XAUUSD")
    closed_row = next(e for e in events if e["event"] == "lane_position_closed" and e["position_id"] == 900)
    assert closed_row["payload"]["pnl"] == pytest.approx(-10.0)


def test_duplicate_gate_does_not_block_scalp_after_scalp(journal, monkeypatch):
    """basket_authorized=True must let a SECOND harvester scalp open even
    though the PARENT (and, transiently, a just-closed prior scalp) still
    carries the same family label — never refused as duplicate_label_position_open."""
    mcp = FakeMcp()
    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:v-test")
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    parent = _lane_pos(1, -13.0, label="dexter3:fable:v-test")
    mcp._positions = [parent]

    class FakeDecision(SimpleNamespace):
        pass

    def _decision(entry: float) -> FakeDecision:
        return FakeDecision(
            symbol="XAUUSD", action="enter", side="buy", entry=entry, sl=entry - 10.0, tp=entry + 500.0,
            setup="repair_harvest", reasons=["test"], session="london",
        )

    mcp.post_entry_position = {
        "positionId": 901, "symbolName": "XAUUSD", "tradeSide": "Buy",
        "volumeInUnits": 1.0, "stopLoss": 90.0, "takeProfit": 600.0, "label": "dexter3:fable:v-test:rsh",
    }
    r1 = ex.execute_entry(_decision(100.0), DEMO_ACCOUNT, basket_authorized=True, label_override="dexter3:fable:v-test:rsh")
    assert r1["action"] == "entered"
    mcp.calls = [c for c in mcp.calls if c[0] != "place_market_order"]

    # first scalp "closed" -- parent still open, first scalp gone
    mcp._positions = [parent]
    mcp.post_entry_position = {
        "positionId": 902, "symbolName": "XAUUSD", "tradeSide": "Buy",
        "volumeInUnits": 1.0, "stopLoss": 91.0, "takeProfit": 601.0, "label": "dexter3:fable:v-test:rsh",
    }
    r2 = ex.execute_entry(_decision(101.0), DEMO_ACCOUNT, basket_authorized=True, label_override="dexter3:fable:v-test:rsh")
    assert r2["action"] == "entered"
    assert r2["position_id"] == 902


# ---------------------------------------------------------------------------
# episode trigger / multi-leg skip
# ---------------------------------------------------------------------------


def test_episode_opens_only_on_single_leg_reliable_pnl_and_threshold(journal, monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "shadow")

    # -- above threshold: aggregate_r = -0.5 (trigger default 1.2) -> no episode
    lane_above = [_lane_pos(1, -5.0)]  # risk=10 -> -0.5R
    state: dict = {}
    sr._run_repair_harvest_tick(FakeMcp(), journal, state, "XAUUSD", None, lane_above, lane_above)
    assert "XAUUSD" not in state.get("repair_harvest", {})

    # -- unreliable pnl: no episode even though "trapped"
    lane_unreliable = [{**_lane_pos(1, -20.0), "netProfit": None}]
    state2: dict = {}
    sr._run_repair_harvest_tick(FakeMcp(), journal, state2, "XAUUSD", None, lane_unreliable, lane_unreliable)
    assert "XAUUSD" not in state2.get("repair_harvest", {})

    # -- trapped + reliable -> episode opens with correct parent fields
    lane_trapped = [_lane_pos(501, -13.0, side="Sell", entry=100.0, sl=110.0)]
    state3: dict = {}
    sr._run_repair_harvest_tick(FakeMcp(), journal, state3, "XAUUSD", None, lane_trapped, lane_trapped)
    ep = state3["repair_harvest"]["XAUUSD"]
    assert ep["parent_position_id"] == 501
    assert ep["parent_side"] == "sell"
    assert ep["parent_entry"] == pytest.approx(100.0)
    assert ep["parent_sl"] == pytest.approx(110.0)
    assert ep["mode"] == "shadow"
    assert ep["cum_scalp_r"] == pytest.approx(0.0)
    opened = _basket_events(journal, "rsh_episode_opened")
    assert len(opened) == 1
    assert opened[0]["payload"]["parent_position_id"] == 501


def test_multileg_skips_and_journals_once(journal, monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "shadow")
    lane = [_lane_pos(1, -30.0), _lane_pos(2, -5.0, side="Sell")]
    state: dict = {}
    sr._run_repair_harvest_tick(FakeMcp(), journal, state, "XAUUSD", None, lane, lane)
    sr._run_repair_harvest_tick(FakeMcp(), journal, state, "XAUUSD", None, lane, lane)  # same legs -> no re-journal
    assert "XAUUSD" not in state.get("repair_harvest", {})
    skips = _basket_events(journal, "rsh_skip_multileg")
    assert len(skips) == 1
    assert skips[0]["payload"]["legs"] == 2


def test_governor_locked_blocks_new_episode(journal, monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "shadow")
    lane = [_lane_pos(501, -13.0)]
    state: dict = {"governor": {"state": "LOSS_STOPPED"}}
    sr._run_repair_harvest_tick(FakeMcp(), journal, state, "XAUUSD", None, lane, lane)
    assert "XAUUSD" not in state.get("repair_harvest", {})


# ---------------------------------------------------------------------------
# mode resolution: off / shadow / live / typo
# ---------------------------------------------------------------------------


def test_off_mode_is_zero_engine_activity(journal, monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.delenv("DEXTER3_REPAIR_HARVEST", raising=False)
    lane = [_lane_pos(501, -30.0)]  # deeply trapped -- would trigger if mode were on
    state: dict = {}
    before = journal.recent_basket_events(limit=500)
    sr._run_repair_harvest_tick(FakeMcp(), journal, state, "XAUUSD", None, lane, lane)
    after = journal.recent_basket_events(limit=500)
    assert before == after
    assert state == {}  # no "repair_harvest" key, no other mutation


def test_typo_mode_degrades_to_shadow_with_one_time_warning(monkeypatch):
    monkeypatch.setattr(sr, "_RSH_MODE_WARNED", set())
    monkeypatch.setattr(sr, "log_line", lambda *_a, **_k: None)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "bogus_typo")
    assert sr._repair_harvest_mode_from_env() == "shadow"
    assert "bogus_typo" in sr._RSH_MODE_WARNED
    # calling again does not re-add / re-warn (set stays size 1)
    assert sr._repair_harvest_mode_from_env() == "shadow"
    assert len(sr._RSH_MODE_WARNED) == 1


def test_mode_off_and_live_resolve_directly(monkeypatch):
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "off")
    assert sr._repair_harvest_mode_from_env() == "off"
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "live")
    assert sr._repair_harvest_mode_from_env() == "live"
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "SHADOW")  # case-insensitive
    assert sr._repair_harvest_mode_from_env() == "shadow"


# ---------------------------------------------------------------------------
# shadow counterfactual: hand-computed bank / loss / timeout
# ---------------------------------------------------------------------------


def test_shadow_bar_outcome_bank_loss_timeout_hand_computed():
    # buy scalp: entry=100, sl=90 (risk=10)
    open_scalp = {"side": "buy", "entry": 100.0, "sl": 90.0}

    # bank: close=102.5 -> r=0.25 >= 0.2 target
    outcome, r = sr._shadow_scalp_bar_outcome(open_scalp, _m5_bar("t", 100, 103, 99, 102.5), 0.2)
    assert outcome == "bank" and r == pytest.approx(0.25)

    # loss: low breaches sl (conservative SL-first, even if close would bank)
    outcome, r = sr._shadow_scalp_bar_outcome(open_scalp, _m5_bar("t", 100, 105, 89.9, 104.0), 0.2)
    assert outcome == "loss" and r == pytest.approx(-1.0)

    # holding: neither bank nor loss (close R below target, no SL breach)
    outcome, r = sr._shadow_scalp_bar_outcome(open_scalp, _m5_bar("t", 100, 101, 95, 100.5), 0.2)
    assert outcome is None and r == pytest.approx(0.05)

    # sell scalp mirror check
    open_scalp_sell = {"side": "sell", "entry": 100.0, "sl": 110.0}
    outcome, r = sr._shadow_scalp_bar_outcome(open_scalp_sell, _m5_bar("t", 100, 98, 96, 97.0), 0.2)
    assert outcome == "bank" and r == pytest.approx(0.3)

    # zero-risk degenerate -> skip
    outcome, r = sr._shadow_scalp_bar_outcome({"side": "buy", "entry": 100.0, "sl": 100.0}, _m5_bar("t", 100, 101, 99, 100), 0.2)
    assert outcome == "skip"


def test_shadow_episode_full_lifecycle_bank_reenter_and_episode_close(journal, monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "shadow")
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST_TRIGGER_R", "1.2")
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST_BANK_TARGET_R", "0.2")

    mcp = FakeMcp()
    parent = _lane_pos(501, -13.0, side="Sell", entry=100.0, sl=110.0)
    state: dict = {}

    # tick 1: bar closes at 100 -> episode opens + first scalp opens (mirror=buy) at 100
    mcp.m5_bars = [_m5_bar("2026-07-15T05:00:00Z", 100, 101, 99, 100.0)]
    lane = [parent]
    sr._OM_BAR_CACHE.clear()
    sr._run_repair_harvest_tick(mcp, journal, state, "XAUUSD", None, lane, lane)
    ep = state["repair_harvest"]["XAUUSD"]
    assert ep["open_scalp"] is not None
    assert ep["open_scalp"]["side"] == "buy"
    assert ep["open_scalp"]["entry"] == pytest.approx(100.0)
    assert ep["open_scalp"]["sl"] == pytest.approx(90.0)  # scalp_sl_frac=1.0 x parent risk(10)

    # tick 2: NEW m5 close banks it (close=102.5 -> r=0.25 >= 0.2) — must NOT
    # also open a fresh scalp on this SAME bar (replay's own "gap bar" rule).
    mcp.m5_bars.append(_m5_bar("2026-07-15T05:05:00Z", 100, 103, 99.5, 102.5))
    sr._OM_BAR_CACHE.clear()  # force a fresh bar re-fetch (production ticks are 4s apart; tests are instant)
    sr._run_repair_harvest_tick(mcp, journal, state, "XAUUSD", None, lane, lane)
    ep = state["repair_harvest"]["XAUUSD"]
    assert ep["open_scalp"] is None
    assert ep["cum_scalp_r"] == pytest.approx(0.25)
    assert len(ep["scalps"]) == 1
    assert ep["scalps"][0]["outcome"] == "bank"
    banks = _basket_events(journal, "rsh_bank")
    assert len(banks) == 1

    # tick 3 (next NEW m5 close, one bar after the resolution bar): the
    # SECOND scalp opens now, entry = this bar's close.
    mcp.m5_bars.append(_m5_bar("2026-07-15T05:10:00Z", 102.5, 103, 102, 102.8))
    sr._OM_BAR_CACHE.clear()
    sr._run_repair_harvest_tick(mcp, journal, state, "XAUUSD", None, lane, lane)
    ep = state["repair_harvest"]["XAUUSD"]
    assert ep["open_scalp"] is not None
    assert ep["open_scalp"]["entry"] == pytest.approx(102.8)
    assert len(ep["scalps"]) == 1  # still just the first, resolved one

    # parent leaves the lane (closed/vanished) -> episode ends, open scalp
    # marked-to-market at the last bar's close, summary journaled.
    sr._OM_BAR_CACHE.clear()
    sr._run_repair_harvest_tick(mcp, journal, state, "XAUUSD", None, [], [])
    assert "XAUUSD" not in state.get("repair_harvest", {})
    closed = _basket_events(journal, "rsh_episode_closed")
    assert len(closed) == 1
    assert closed[0]["payload"]["parent_position_id"] == 501
    assert closed[0]["payload"]["n_scalps"] == 2  # the banked one + the mark-to-market one


def test_shadow_episode_loss_stop_halts_new_scalps(journal, monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "shadow")
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST_EPISODE_LOSS_STOP_R", "1.5")
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST_SCALP_MAX_HOLD_BARS", "1")

    mcp = FakeMcp()
    parent = _lane_pos(501, -13.0, side="Sell", entry=100.0, sl=110.0)
    lane = [parent]
    state: dict = {}
    ts = 0

    def _tick(o, h, l, c):
        nonlocal ts
        ts += 5
        mcp.m5_bars.append(_m5_bar(f"2026-07-15T05:{ts:02d}:00Z", o, h, l, c))
        sr._OM_BAR_CACHE.clear()  # force a fresh bar re-fetch (production ticks are 4s apart; tests are instant)
        sr._run_repair_harvest_tick(mcp, journal, state, "XAUUSD", None, lane, lane)

    # tick 1: episode opens + first scalp opens at close=100 (buy, sl=90)
    _tick(100, 101, 99, 100.0)
    # tick 2: SL breach -> loss -1.0R (max_hold=1 so this is also the only
    # bar this scalp gets before a timeout would fire anyway)
    _tick(100, 101, 89, 95.0)
    ep = state["repair_harvest"]["XAUUSD"]
    assert ep["cum_scalp_r"] == pytest.approx(-1.0)
    assert ep["loss_stopped"] is False

    # tick 3: next scalp opens at close=95 (sl=85)
    _tick(95, 96, 94, 95.0)
    ep = state["repair_harvest"]["XAUUSD"]
    assert ep["open_scalp"] is not None

    # tick 4: another SL breach -> cum -2.0R <= -1.5 threshold -> loss-stopped
    _tick(95, 96, 84, 90.0)
    ep = state["repair_harvest"]["XAUUSD"]
    assert ep["cum_scalp_r"] == pytest.approx(-2.0)
    assert ep["loss_stopped"] is True
    stopped = _basket_events(journal, "rsh_loss_stopped")
    assert len(stopped) == 1

    # tick 5: NO new scalp opens even though none is currently open
    _tick(90, 91, 89, 90.5)
    ep = state["repair_harvest"]["XAUUSD"]
    assert ep["open_scalp"] is None
    assert len(ep["scalps"]) == 2  # unchanged -- no third scalp taken


def test_max_scalps_per_episode_cap(journal, monkeypatch, tmp_path):
    """Uses two WINNING (banked) scalps so cum_scalp_r stays well clear of
    the (default) episode loss-stop threshold — isolates the max-scalps cap
    from the loss-stop guard tested separately above."""
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "shadow")
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST_MAX_SCALPS_PER_EPISODE", "2")

    mcp = FakeMcp()
    parent = _lane_pos(501, -13.0, side="Sell", entry=100.0, sl=110.0)
    lane = [parent]
    state: dict = {}
    ts = 0

    def _tick(o, h, l, c):
        nonlocal ts
        ts += 5
        mcp.m5_bars.append(_m5_bar(f"2026-07-15T05:{ts:02d}:00Z", o, h, l, c))
        sr._OM_BAR_CACHE.clear()  # force a fresh bar re-fetch (production ticks are 4s apart; tests are instant)
        sr._run_repair_harvest_tick(mcp, journal, state, "XAUUSD", None, lane, lane)

    _tick(100, 101, 99, 100.0)        # episode opens, scalp #1 opens (buy entry=100, sl=90)
    _tick(100, 103, 99, 102.5)        # scalp #1 banks: r=(102.5-100)/10=0.25 >= 0.2
    _tick(102.5, 103, 102, 102.5)     # scalp #2 opens (entry=102.5, sl=92.5)
    _tick(102.5, 105.5, 102, 105.0)   # scalp #2 banks: r=(105.0-102.5)/10=0.25 >= 0.2
    ep = state["repair_harvest"]["XAUUSD"]
    assert len(ep["scalps"]) == 2
    assert ep["loss_stopped"] is False
    assert ep["cum_scalp_r"] == pytest.approx(0.5)

    _tick(105.0, 106, 104, 105.5)  # would open scalp #3 -- blocked by the cap
    ep = state["repair_harvest"]["XAUUSD"]
    assert ep["open_scalp"] is None
    assert len(ep["scalps"]) == 2
    cap_events = _basket_events(journal, "rsh_scalp_cap_reached")
    assert len(cap_events) >= 1


# ---------------------------------------------------------------------------
# LIVE mode: label + SL, bank/timeout, vanish on parent close, restart safe
# ---------------------------------------------------------------------------


def test_live_mode_places_order_with_rsh_label_and_sl_banks_and_times_out(journal, monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "live")
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST_BANK_TARGET_R", "0.2")
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST_SCALP_MAX_HOLD_BARS", "2")

    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=5.0))
    # sr.LIVE_ORDER_LABEL is a snapshot import of dexter3.executor.LABEL taken
    # at shadow_runner's own module-load time — it is what _active_order_label()
    # actually returns, NOT a live read of ex_mod.LABEL (monkeypatching the
    # latter would not affect it). Parent's label only needs to match the
    # FAMILY root ("dexter3:fable", a true constant) to be lane-recognized.
    expected_label = f"{sr.LIVE_ORDER_LABEL}:rsh"
    parent = _lane_pos(501, -13.0, side="Sell", entry=100.0, sl=110.0, label=sr.LIVE_ORDER_LABEL)
    mcp._positions = [parent]
    lane = [parent]
    state: dict = {}

    mcp.m5_bars = [_m5_bar("2026-07-15T05:00:00Z", 100, 101, 99, 100.0)]
    mcp.post_entry_position = {
        "positionId": 900, "symbolName": "XAUUSD", "tradeSide": "Buy",
        "volumeInUnits": 1.0, "stopLoss": 90.0, "takeProfit": 5000.0, "label": expected_label,
    }
    sr._OM_BAR_CACHE.clear()
    sr._run_repair_harvest_tick(mcp, journal, state, "XAUUSD", ex, lane, mcp.get_positions())

    place_calls = [c for c in mcp.calls if c[0] == "place_market_order"]
    assert len(place_calls) == 1
    order_kwargs = place_calls[0][1]
    assert order_kwargs["label"] == expected_label
    assert order_kwargs["side"] == "buy"
    # SL distance = scalp_sl_frac(1.0) x parent risk (|100-110|=10) -> 10 price units;
    # pipSize=0.01 -> sl_pips = 1000
    assert order_kwargs["stop_loss_pips"] == pytest.approx(1000, abs=1)

    ep = state["repair_harvest"]["XAUUSD"]
    assert ep["open_scalp"]["position_id"] == 900

    # persist the scalp as an actual open position for subsequent ticks
    mcp._positions = [parent, dict(mcp.post_entry_position)]
    mcp.post_entry_position = None
    mcp.calls = [c for c in mcp.calls if c[0] != "place_market_order"]

    # tick 2: floating pnl on the scalp is BELOW bank target -> keep holding
    scalp_live = next(p for p in mcp._positions if p["positionId"] == 900)
    scalp_live["netProfit"] = 0.5  # risk_usd for scalp sizing = executor.config.risk_usd = 5.0 -> R=0.1
    mcp.m5_bars.append(_m5_bar("2026-07-15T05:05:00Z", 100, 101, 99, 100.2))
    sr._OM_BAR_CACHE.clear()
    sr._run_repair_harvest_tick(mcp, journal, state, "XAUUSD", ex, lane, mcp.get_positions())
    ep = state["repair_harvest"]["XAUUSD"]
    assert ep["open_scalp"] is not None
    close_calls = [c for c in mcp.calls if c[0] == "close_position"]
    assert not close_calls

    # tick 3: floating pnl now clears bank target (1.5 / 5.0 risk_usd = 0.3R >= 0.2) -> banked at market
    scalp_live["netProfit"] = 1.5
    mcp.m5_bars.append(_m5_bar("2026-07-15T05:10:00Z", 100, 101, 99, 100.3))
    sr._OM_BAR_CACHE.clear()
    sr._run_repair_harvest_tick(mcp, journal, state, "XAUUSD", ex, lane, mcp.get_positions())
    close_calls = [c for c in mcp.calls if c[0] == "close_position"]
    assert len(close_calls) == 1 and close_calls[0][1]["position_id"] == 900
    ep = state["repair_harvest"]["XAUUSD"]
    assert ep["open_scalp"] is None
    assert ep["scalps"][-1]["outcome"] == "bank"
    banks = _basket_events(journal, "rsh_bank")
    assert len(banks) == 1


def test_live_episode_close_closes_open_scalp_at_market_when_parent_vanishes(journal, monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(ex_mod, "LABEL", "dexter3:fable:v-test")
    monkeypatch.setenv("DEXTER3_REPAIR_HARVEST", "live")

    mcp = FakeMcp()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=5.0))
    parent = _lane_pos(501, -13.0, side="Sell", entry=100.0, sl=110.0, label="dexter3:fable:v-test")
    scalp = _lane_pos(900, 1.0, side="Buy", entry=100.0, sl=90.0, label="dexter3:fable:v-test:rsh")
    mcp._positions = [parent, scalp]
    lane = [parent]
    state: dict = {
        "repair_harvest": {
            "XAUUSD": {
                "parent_position_id": 501, "parent_side": "sell", "parent_entry": 100.0, "parent_sl": 110.0,
                "started_ts": "2026-07-15T04:00:00Z", "trigger_r_at_open": -1.3, "mode": "live",
                "cum_scalp_r": 0.0, "scalps": [], "loss_stopped": False,
                "last_m5_close_ts": "2026-07-15T05:00:00Z",
                "open_scalp": {
                    "position_id": 900, "side": "buy", "entry": 100.0, "sl": 90.0,
                    "opened_bar_ts": "2026-07-15T05:00:00Z", "bars_evaluated": 0, "scalp_risk_usd": 5.0,
                },
            }
        }
    }
    mcp.m5_bars = [_m5_bar("2026-07-15T05:00:00Z", 100, 101, 99, 100.0)]

    # parent vanishes (closed at broker) -- next tick's lane is empty
    sr._run_repair_harvest_tick(mcp, journal, state, "XAUUSD", ex, [], [scalp])
    close_calls = [c for c in mcp.calls if c[0] == "close_position"]
    assert len(close_calls) == 1 and close_calls[0][1]["position_id"] == 900
    assert "XAUUSD" not in state.get("repair_harvest", {})
    closed = _basket_events(journal, "rsh_episode_closed")
    assert len(closed) == 1
    assert closed[0]["payload"]["reason"] == "parent_resolved"


# ---------------------------------------------------------------------------
# restart persistence
# ---------------------------------------------------------------------------


def test_episode_and_open_scalp_survive_a_state_reload(monkeypatch, tmp_path):
    _isolate_shadow_runtime(monkeypatch, tmp_path)
    state = {
        "repair_harvest": {
            "XAUUSD": {
                "parent_position_id": 501, "parent_side": "sell", "parent_entry": 100.0, "parent_sl": 110.0,
                "started_ts": "2026-07-15T04:00:00Z", "trigger_r_at_open": -1.3, "mode": "shadow",
                "cum_scalp_r": 0.35, "scalps": [{"position_id": 0, "side": "buy", "entry": 100.0,
                                                  "entry_ts": "2026-07-15T05:00:00Z", "outcome": "bank", "r": 0.35}],
                "loss_stopped": False, "last_m5_close_ts": "2026-07-15T05:05:00Z",
                "open_scalp": {"position_id": 0, "side": "buy", "entry": 102.8, "sl": 92.8,
                                "opened_bar_ts": "2026-07-15T05:05:00Z", "bars_evaluated": 1, "scalp_risk_usd": 1.0},
            }
        }
    }
    sr.save_shadow_state(state)
    restarted = sr.load_shadow_state()
    ep = restarted["repair_harvest"]["XAUUSD"]
    assert ep["parent_position_id"] == 501
    assert ep["cum_scalp_r"] == pytest.approx(0.35)
    assert ep["open_scalp"]["entry"] == pytest.approx(102.8)
    assert ep["open_scalp"]["bars_evaluated"] == 1
    assert len(ep["scalps"]) == 1
