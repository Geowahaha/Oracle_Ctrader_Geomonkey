"""Fixes for the 2026-07-26 replay-vs-live reconciliation.

Six divergences, each env-gated so the default is byte-identical to prior
behaviour (fable + daytrend are inside a frozen SL-floor measurement):
  1. governor TARGET lock counted FLOATING PnL -> amputated the convex tail
  2. sweep-follow geometry flags were dead (read only in the offline module)
  3. the replay scored a DIFFERENT committee than the deployed one
  4. the min-volume abs cap deletes the wide-stop trades live data says win
  6. the sdzone engine was rebuilt each close, capping zone age
"""
from __future__ import annotations

import os

import pytest

from dexter3.daily_governor import DailyGovernor, GovernorConfig


# --- 1) governor: TARGET must be able to ignore floating --------------------

def _gov(**kw) -> DailyGovernor:
    return DailyGovernor(GovernorConfig(daily_target_usd=15.0, daily_loss_usd=8.0, **kw))


def test_default_target_still_counts_floating():
    st = _gov().status(day_pnl_usd=0.0, floating_pnl_usd=15.0)
    assert st["state"] == "TARGET_LOCKED"      # unchanged legacy behaviour


def test_target_can_ignore_floating():
    """THE FIX: an open runner at +15 unrealized must NOT be liquidated."""
    st = _gov(target_ignores_floating=True).status(day_pnl_usd=0.0, floating_pnl_usd=15.0)
    assert st["state"] == "HUNTING"
    assert st["effective_pnl"] == 15.0          # still reported for observability


def test_realized_target_still_locks_when_ignoring_floating():
    st = _gov(target_ignores_floating=True).status(day_pnl_usd=15.0, floating_pnl_usd=0.0)
    assert st["state"] == "TARGET_LOCKED"


def test_loss_cap_always_counts_floating():
    """The LOSS side must never stop counting floating — a day must not bleed
    past the cap because 'it hasn't closed yet'."""
    for ignore in (False, True):
        st = _gov(target_ignores_floating=ignore).status(day_pnl_usd=0.0, floating_pnl_usd=-8.0)
        assert st["state"] == "LOSS_STOPPED", f"ignore_floating={ignore}"


def test_floating_runner_below_target_unaffected():
    st = _gov(target_ignores_floating=True).status(day_pnl_usd=0.0, floating_pnl_usd=40.0)
    assert st["state"] == "HUNTING", "a big runner must be left to the trail"


# --- 3) committee weight parity --------------------------------------------

def test_reload_weights_picks_up_env(monkeypatch):
    from dexter3 import hunt_mode
    monkeypatch.delenv("DEXTER3_HUNT_W_TILT", raising=False)
    base = hunt_mode.reload_weights()
    assert base["TOTAL_COMMITTEE_WEIGHT"] == pytest.approx(8.5)   # documented default

    for k, v in (("TILT", "1.3"), ("DISP", "0.4"), ("COMP", "0.4"),
                 ("SWEEP", "1.0"), ("H1", "1.4")):
        monkeypatch.setenv(f"DEXTER3_HUNT_W_{k}", v)
    live = hunt_mode.reload_weights()
    assert live["TOTAL_COMMITTEE_WEIGHT"] == pytest.approx(8.0)   # deployed fable
    assert hunt_mode.TOTAL_COMMITTEE_WEIGHT == pytest.approx(8.0), "module global must update"
    assert hunt_mode.WEIGHT_H1_CONTEXT == pytest.approx(1.4)


def test_conviction_denominator_actually_differs():
    """8.5 vs 8.0 is a 6.25% shift in every leader_score — and leader_score is
    what min_leader_score 0.18 / B-tier 0.15 / bypass 0.74 all threshold on."""
    weighted_sum = 1.6
    assert abs(weighted_sum) / 8.5 == pytest.approx(0.188, abs=1e-3)
    assert abs(weighted_sum) / 8.0 == pytest.approx(0.200, abs=1e-3)


def test_replay_unit_loader_reads_a_real_unit(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "rp_mod", "scripts/dexter3_entry_position_replay.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)

    unit = tmp_path / "u.service"
    unit.write_text(
        "[Service]\n"
        "Environment=DEXTER3_HUNT_W_H1=1.4\n"
        'Environment=DEXTER3_HUNT_SWEEP_FOLLOW=1\n'
        "Environment=NOT_OURS=9\n",
        encoding="utf-8",
    )
    for k in ("DEXTER3_HUNT_W_H1", "DEXTER3_HUNT_SWEEP_FOLLOW", "NOT_OURS"):
        os.environ.pop(k, None)
    try:
        applied = rp._load_unit_env(str(unit))
        assert applied == {"DEXTER3_HUNT_W_H1": "1.4", "DEXTER3_HUNT_SWEEP_FOLLOW": "1"}
        assert "NOT_OURS" not in applied, "only DEXTER3_* keys may be imported"
        assert os.environ["DEXTER3_HUNT_W_H1"] == "1.4"
    finally:
        for k in ("DEXTER3_HUNT_W_H1", "DEXTER3_HUNT_SWEEP_FOLLOW"):
            os.environ.pop(k, None)


def test_replay_unit_loader_does_not_override_explicit_env(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "rp_mod2", "scripts/dexter3_entry_position_replay.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    unit = tmp_path / "u2.service"
    unit.write_text("Environment=DEXTER3_HUNT_W_H1=1.4\n", encoding="utf-8")
    os.environ["DEXTER3_HUNT_W_H1"] = "0.9"
    try:
        applied = rp._load_unit_env(str(unit))
        assert applied == {}
        assert os.environ["DEXTER3_HUNT_W_H1"] == "0.9", "explicit shell export must win"
    finally:
        os.environ.pop("DEXTER3_HUNT_W_H1", None)


# --- 2) sweep-follow geometry ----------------------------------------------

def test_sweep_geometry_inert_when_unset(monkeypatch):
    from dexter3 import hunt_mode
    monkeypatch.delenv("DEXTER3_HUNT_SWEEP_SL_ATR", raising=False)
    monkeypatch.delenv("DEXTER3_HUNT_SWEEP_TP_RR", raising=False)
    bars = [{"high": 101.0, "low": 99.0, "close": 100.0, "open": 100.0} for _ in range(30)]
    sl, tp, meta = hunt_mode._apply_sweep_follow_geometry("buy", 100.0, 97.0, 106.0, bars, 0.1)
    assert (sl, tp, meta) == (97.0, 106.0, {})


def test_sweep_geometry_applies_measured_shape(monkeypatch):
    """The edge was measured at SL = 1xATR and TP = 1.5R; live was using the
    generic swing-clamped SL and the 1.2 RR floor."""
    from dexter3 import hunt_mode
    monkeypatch.setenv("DEXTER3_HUNT_SWEEP_SL_ATR", "1.0")
    monkeypatch.setenv("DEXTER3_HUNT_SWEEP_TP_RR", "1.5")
    bars = [{"high": 102.0, "low": 100.0, "close": 101.0, "open": 101.0} for _ in range(30)]
    sl, tp, meta = hunt_mode._apply_sweep_follow_geometry("buy", 100.0, 90.0, 200.0, bars, 0.1)
    assert meta.get("applied") is True
    risk = 100.0 - sl
    assert risk == pytest.approx(meta["tr_q50"], rel=1e-6)      # SL = 1 x ATR
    # 1.5R plus the module's deliberate _RR_ROUNDING_SAFETY_MARGIN, so the
    # post-rounding RR can never land BELOW the floor.
    expect = 1.5 * (1.0 + hunt_mode._RR_ROUNDING_SAFETY_MARGIN)
    assert (tp - 100.0) / risk == pytest.approx(expect, rel=1e-6)
    assert sl < 100.0 < tp


def test_sweep_geometry_sell_side(monkeypatch):
    from dexter3 import hunt_mode
    monkeypatch.setenv("DEXTER3_HUNT_SWEEP_SL_ATR", "1.0")
    monkeypatch.setenv("DEXTER3_HUNT_SWEEP_TP_RR", "1.5")
    bars = [{"high": 102.0, "low": 100.0, "close": 101.0, "open": 101.0} for _ in range(30)]
    sl, tp, meta = hunt_mode._apply_sweep_follow_geometry("sell", 100.0, 110.0, 10.0, bars, 0.1)
    assert meta.get("applied") is True
    assert tp < 100.0 < sl
    expect = 1.5 * (1.0 + hunt_mode._RR_ROUNDING_SAFETY_MARGIN)
    assert (100.0 - tp) / (sl - 100.0) == pytest.approx(expect, rel=1e-6)


def test_sweep_geometry_respects_spread_floor(monkeypatch):
    from dexter3 import hunt_mode
    monkeypatch.setenv("DEXTER3_HUNT_SWEEP_SL_ATR", "0.001")   # absurdly tight
    monkeypatch.setenv("DEXTER3_HUNT_SWEEP_TP_RR", "1.5")
    bars = [{"high": 100.1, "low": 100.0, "close": 100.05, "open": 100.0} for _ in range(30)]
    sl, _tp, _m = hunt_mode._apply_sweep_follow_geometry("buy", 100.0, 90.0, 200.0, bars, 1.0)
    assert 100.0 - sl >= hunt_mode.MIN_SL_SPREAD_MULT * 1.0 - 1e-9


# --- 6) sdzone engine persistence ------------------------------------------

def _zbars(n: int) -> list[dict]:
    out, price = [], 2000.0
    for i in range(n):
        c = price + (1.5 if i % 7 in (0, 1) else -0.8)
        out.append({"open": price, "high": max(price, c) + 0.6, "low": min(price, c) - 0.6,
                    "close": c, "volume": 100.0, "ts": f"2026-07-26T{i // 60:02d}:{i % 60:02d}:00Z"})
        price = c
    return out


def test_zone_engine_stateless_by_default():
    from dexter3 import sd_zones
    bars = _zbars(200)
    e1, _ = sd_zones.zones_from_prefix(bars)
    e2, _ = sd_zones.zones_from_prefix(bars)
    assert e1 is not e2, "no key => fresh engine each call (prior behaviour)"


def test_zone_engine_persists_with_key():
    from dexter3 import sd_zones
    sd_zones._PERSISTENT_ENGINE.clear()
    sd_zones._PERSISTENT_LAST_TS.clear()
    bars = _zbars(200)
    e1, _ = sd_zones.zones_from_prefix(bars, persist_key="XAUUSD")
    e2, _ = sd_zones.zones_from_prefix(bars + _zbars(1)[:1], persist_key="XAUUSD")
    assert e1 is e2, "the SAME engine must be advanced, not rebuilt"
    sd_zones._PERSISTENT_ENGINE.clear()
    sd_zones._PERSISTENT_LAST_TS.clear()


def test_zone_engine_rebuilds_on_gap():
    from dexter3 import sd_zones
    sd_zones._PERSISTENT_ENGINE.clear()
    sd_zones._PERSISTENT_LAST_TS.clear()
    e1, _ = sd_zones.zones_from_prefix(_zbars(200), persist_key="XAUUSD")
    # a disjoint prefix (restart / data gap) must NOT be silently advanced
    other = [{**b, "ts": b["ts"].replace("2026-07-26", "2026-08-01")} for b in _zbars(200)]
    e2, _ = sd_zones.zones_from_prefix(other, persist_key="XAUUSD")
    assert e1 is not e2
    sd_zones._PERSISTENT_ENGINE.clear()
    sd_zones._PERSISTENT_LAST_TS.clear()


def test_daytrend_persist_flag_is_off_by_default(monkeypatch):
    from dexter3 import daytrend
    monkeypatch.delenv("DEXTER3_SDZONE_PERSIST_ENGINE", raising=False)
    assert daytrend._env_flag("DEXTER3_SDZONE_PERSIST_ENGINE") is False
    monkeypatch.setenv("DEXTER3_SDZONE_PERSIST_ENGINE", "1")
    assert daytrend._env_flag("DEXTER3_SDZONE_PERSIST_ENGINE") is True
    monkeypatch.setenv("DEXTER3_SDZONE_PERSIST_ENGINE", "0")
    assert daytrend._env_flag("DEXTER3_SDZONE_PERSIST_ENGINE") is False
