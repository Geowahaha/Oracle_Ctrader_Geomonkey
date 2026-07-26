"""The last LOW/MED audit defects (2026-07-26) — none had ever fired live.

  A) scalp's bank exit silently fell through to the RETIRED dragon ladder when
     the bar fetch came back empty (dispatch was not exhaustive)
  B) the bank guard tested the wrong quantity, so a MISSING stop meant the
     lane could never bank at all
  C) max_basket_risk_mult read keys aggregate_lane never emitted -> dead cap
  D) two independent pip sizes existed across the order boundary
  E) a blind single read left position_id NULL, skipping naked-repair and
     blinding vanish-reconcile forever
  F) fable-paper shared the LIVE fable lane's state/lock/log/label
"""
from __future__ import annotations

import pytest

import dexter3.shadow_runner as sr
from dexter3.basket_live import BasketConfig, aggregate_lane, enforce_caps


def _pos(entry: float, sl: float, vol: float, pnl: float = 0.0) -> dict:
    return {"entryPrice": entry, "stopLoss": sl, "volumeInUnits": vol,
            "netProfit": pnl, "tradeSide": "BUY", "positionId": 1}


# --- C) the dead max_basket_risk_mult cap ----------------------------------

def test_aggregate_lane_emits_the_keys_enforce_caps_reads():
    """THE BUG: base_risk_usd was only a PARAMETER, never a returned key, so
    enforce_caps evaluated `if 0.0 > 0` and the cap could never fire."""
    agg = aggregate_lane([_pos(4000.0, 3996.0, 2.0)], base_risk_usd=8.0)
    assert agg["base_risk_usd"] == pytest.approx(8.0)
    assert agg["current_basket_risk_usd"] == pytest.approx(8.0)   # |4000-3996| * 2


def test_max_basket_risk_cap_now_actually_fires():
    agg = aggregate_lane([_pos(4000.0, 3996.0, 2.0)], base_risk_usd=8.0)
    cfg = BasketConfig(max_legs=5, max_basket_risk_mult=1.5)
    out = enforce_caps(cfg, agg=agg, now_utc_iso="2026-07-26T00:00:00Z",
                       daily_state=None, proposed_repair_risk_usd=10.0)  # 8+10 > 8*1.5
    assert out is not None and out.get("cap") == "max_basket_risk_mult"


def test_max_basket_risk_cap_allows_within_budget():
    agg = aggregate_lane([_pos(4000.0, 3996.0, 2.0)], base_risk_usd=8.0)
    cfg = BasketConfig(max_legs=5, max_basket_risk_mult=3.0)
    out = enforce_caps(cfg, agg=agg, now_utc_iso="2026-07-26T00:00:00Z",
                       daily_state=None, proposed_repair_risk_usd=4.0)   # 12 <= 24
    assert out is None or out.get("cap") != "max_basket_risk_mult"


def test_aggregate_lane_risk_ignores_unusable_rows():
    agg = aggregate_lane([_pos(4000.0, 0.0, 2.0), _pos(4000.0, 3996.0, 1.0)], base_risk_usd=4.0)
    assert agg["current_basket_risk_usd"] == pytest.approx(4.0)   # only the valid leg


# --- F) fable-paper must not collide with the LIVE fable lane --------------

def test_paper_mode_has_its_own_everything():
    """THE BUG: with no DEXTER3_MODE the paper unit resolved to "v16" — the
    same state file, lock file, log file and label as the money lane. The lock
    made them mutually exclusive, so if paper won the race the LIVE lane exited
    and never traded."""
    assert sr._active_state_file("fable-paper") != sr._active_state_file("v16")
    assert sr._active_log_file("fable-paper") != sr._active_log_file("v16")
    assert sr._active_order_label("fable-paper") != sr._active_order_label("v16")
    assert sr.PAPER_LOCK_FILE != sr.LOCK_FILE


def test_paper_label_stays_inside_the_fable_family():
    """It must still be recognisable as fable's family for reporting, while
    being a distinct label for ownership."""
    from dexter3.executor import label_matches_family
    lbl = sr._active_order_label("fable-paper")
    assert label_matches_family(lbl, sr._active_label_family("fable-paper"))
    assert lbl.endswith(":paper")


def test_every_live_mode_keeps_its_own_files():
    modes = ("v16", "fable-paper", "vp", "daytrend", "scalp", "dpull-cs", "channelfade")
    states = [sr._active_state_file(m).name for m in modes]
    logs = [sr._active_log_file(m).name for m in modes]
    assert len(set(states)) == len(modes), f"state file collision: {states}"
    assert len(set(logs)) == len(modes), f"log file collision: {logs}"


# --- E) resolve-position retry knobs ---------------------------------------

def test_resolve_position_retry_env_defaults():
    from dexter3 import executor as ex
    assert ex._env_int("DEXTER3_RESOLVE_POSITION_TRIES", 3) == 3
    assert ex._env_float_local("DEXTER3_RESOLVE_POSITION_DELAY_SEC", 0.7) == pytest.approx(0.7)


def test_resolve_position_retry_env_override(monkeypatch):
    from dexter3 import executor as ex
    monkeypatch.setenv("DEXTER3_RESOLVE_POSITION_TRIES", "5")
    monkeypatch.setenv("DEXTER3_RESOLVE_POSITION_DELAY_SEC", "0.1")
    assert ex._env_int("DEXTER3_RESOLVE_POSITION_TRIES", 3) == 5
    assert ex._env_float_local("DEXTER3_RESOLVE_POSITION_DELAY_SEC", 0.7) == pytest.approx(0.1)
    monkeypatch.setenv("DEXTER3_RESOLVE_POSITION_TRIES", "garbage")
    assert ex._env_int("DEXTER3_RESOLVE_POSITION_TRIES", 3) == 3   # never raises


def test_resolve_new_position_retries_until_the_fill_propagates(monkeypatch):
    """THE BUG: one blind read. When get_positions had not propagated, pid
    stayed 0 -> naked-repair skipped and a NULL position_id blinded
    vanish-reconcile permanently."""
    from dexter3 import executor as ex
    monkeypatch.setattr(ex.time, "sleep", lambda *_: None)

    calls = {"n": 0}

    class _C:
        def get_positions(self):
            calls["n"] += 1
            if calls["n"] < 3:
                return []                      # not propagated yet
            return [{"positionId": 4242, "symbolName": "XAUUSD",
                     "label": ex.LABEL, "tradeSide": "BUY"}]

    obj = ex.Dexter3Executor.__new__(ex.Dexter3Executor)
    obj.client = _C()
    pid = obj._resolve_new_position("XAUUSD", set())
    assert pid == 4242
    assert calls["n"] >= 3, "must have retried rather than giving up on read #1"


def test_resolve_new_position_gives_up_cleanly(monkeypatch):
    from dexter3 import executor as ex
    monkeypatch.setattr(ex.time, "sleep", lambda *_: None)
    monkeypatch.setenv("DEXTER3_RESOLVE_POSITION_TRIES", "2")

    class _Empty:
        def get_positions(self):
            return []

    obj = ex.Dexter3Executor.__new__(ex.Dexter3Executor)
    obj.client = _Empty()
    assert obj._resolve_new_position("XAUUSD", set()) == 0
