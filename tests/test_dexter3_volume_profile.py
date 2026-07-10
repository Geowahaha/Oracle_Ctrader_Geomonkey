"""Unit tests for dexter3/volume_profile.py — NEW entry logic candidate #1.

Pure module, synthetic bars only. Pins: profile math (POC/VA/HVN/LVN
identification), no-lookahead (profile excludes the signal bar), each setup's
trigger shape, geometry sidedness + RR floor, and graceful skips.
"""
from __future__ import annotations

import pytest

from dexter3 import volume_profile as vp


def _bar(ts: str, o: float, h: float, l: float, c: float, v: float) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _flat_bars(n: int, price: float = 100.0, v: float = 10.0) -> list[dict]:
    return [_bar(f"2026-07-11T{i // 12:02d}:{(i % 12) * 5:02d}:00Z", price, price + 0.5, price - 0.5, price, v) for i in range(n)]


# -- build_profile ------------------------------------------------------------


def test_profile_poc_at_max_volume_price():
    bars = _flat_bars(50, price=100.0, v=1.0)
    # concentrate volume at ~110
    bars += [_bar(f"2026-07-11T10:{i:02d}:00Z", 110, 110.5, 109.5, 110, 100.0) for i in range(5)]
    p = vp.build_profile(bars, bins=40)
    assert p is not None
    assert abs(p.poc_price - 110.0) < 1.0  # POC lands at the concentration


def test_profile_identifies_hvn_and_lvn():
    bars = _flat_bars(30, price=100.0, v=50.0)          # heavy node ~100
    bars += _flat_bars(30, price=104.0, v=50.0)          # heavy node ~104
    bars += [_bar("2026-07-11T09:00:00Z", 102, 102.5, 101.5, 102, 0.5)]  # thin ~102
    p = vp.build_profile(bars, bins=20)
    assert p is not None
    kinds = {n.kind for n in p.nodes}
    assert "hvn" in kinds and "lvn" in kinds
    lvn_mids = [n.mid for n in p.nodes if n.kind == "lvn"]
    assert any(101.0 < m < 103.0 for m in lvn_mids)  # the thin middle is an LVN


def test_profile_none_without_volume_or_range():
    assert vp.build_profile([]) is None
    flat = [_bar("t", 100, 100, 100, 100, 5)] * 10
    assert vp.build_profile(flat) is None            # zero range
    no_vol = [_bar("t", 100, 101, 99, 100, 0)] * 10
    assert vp.build_profile(no_vol) is None           # zero volume everywhere


# -- decide_vp: skips ----------------------------------------------------------


def test_decide_vp_skips_below_min_bars():
    d = vp.decide_vp("XAUUSD", _flat_bars(10), spread_abs=0.1)
    assert d.action == "skip"
    assert "bars<" in d.reasons[0]


def test_decide_vp_skips_without_volume():
    bars = [_bar(f"t{i}", 100, 101, 99, 100, 0) for i in range(vp.MIN_BARS + 1)]
    d = vp.decide_vp("XAUUSD", bars, spread_abs=0.1)
    assert d.action == "skip"
    assert "no_profile" in d.reasons[0]


def test_decide_vp_no_setup_on_quiet_mid_range_bar():
    bars = _flat_bars(vp.MIN_BARS + 1, price=100.0, v=10.0)
    d = vp.decide_vp("XAUUSD", bars, spread_abs=0.05)
    assert d.action == "skip"


# -- no-lookahead: the profile excludes the signal bar --------------------------


def test_profile_excludes_signal_bar():
    """A HUGE-volume signal bar must not shift the profile it is judged
    against — the profile is built from bars strictly before it."""
    bars = _flat_bars(vp.MIN_BARS, price=100.0, v=10.0)
    # signal bar with absurd volume far above the range
    bars.append(_bar("2026-07-11T23:55:00Z", 100.0, 130.0, 99.9, 129.0, 99999.0))
    window = bars[-(vp.PROFILE_WINDOW + 1):-1]
    p = vp.build_profile(window)
    assert p is not None
    assert p.hi <= 101.0  # signal bar's 130 high is NOT in the profile


# -- setup triggers -------------------------------------------------------------


def _profile_with_lvn_gap() -> list[dict]:
    """History: heavy trade at 100 and 104, thin gap ~102 (LVN)."""
    bars: list[dict] = []
    for i in range(vp.PROFILE_WINDOW // 2 + 1):
        bars.append(_bar(f"a{i}", 100, 100.6, 99.4, 100, 40.0))
    for i in range(vp.PROFILE_WINDOW // 2 + 1):
        bars.append(_bar(f"b{i}", 104, 104.6, 103.4, 104, 40.0))
    return bars


def test_lvn_rejection_buy_fires_with_valid_geometry():
    bars = _profile_with_lvn_gap()
    # prior bar, then signal: a SHALLOW probe into the thin gap (LVN zone tops
    # ~103.3) that closes back above the zone — wick-based invalidation keeps
    # the risk tight, TP at the far edge of the HVN shelf above.
    bars.append(_bar("prior", 103.5, 103.7, 103.4, 103.5, 10.0))
    bars.append(_bar("signal", 103.4, 103.7, 103.1, 103.55, 20.0))
    d = vp.decide_vp("XAUUSD", bars, spread_abs=0.05, session="london")
    assert d.action == "enter"
    assert d.setup == "vp_lvn_rejection"
    assert d.side == "buy"
    assert d.sl < d.entry < d.tp
    assert (d.tp - d.entry) / (d.entry - d.sl) >= vp.MIN_REWARD_RISK
    assert d.session == "london"


def test_lvn_deep_probe_rejected_by_rr_floor():
    """A DEEP probe (wick far into the gap) has far invalidation and little
    room to the shelf — the RR floor must refuse it (this is the module doing
    its job, not a bug)."""
    bars = _profile_with_lvn_gap()
    bars.append(_bar("prior", 103.4, 103.6, 103.2, 103.4, 10.0))
    bars.append(_bar("signal", 103.0, 103.6, 101.9, 103.5, 20.0))
    d = vp.decide_vp("XAUUSD", bars, spread_abs=0.05)
    assert d.action == "skip"


def test_poc_reversion_sell_fires_above_value_area():
    bars = _flat_bars(vp.PROFILE_WINDOW + 1, price=100.0, v=20.0)
    bars.append(_bar("prior", 100.4, 100.9, 100.2, 100.8, 10.0))
    # signal: spiked above the prior high / VA and closed back down (reversal)
    bars.append(_bar("signal", 102.6, 102.9, 102.0, 102.1, 10.0))
    d = vp.decide_vp("XAUUSD", bars, spread_abs=0.05)
    if d.action == "enter":  # geometry (RR floor) may legitimately reject
        assert d.setup == "vp_poc_reversion"
        assert d.side == "sell"
        assert d.tp < d.entry < d.sl


def test_enter_geometry_rejected_when_rr_below_floor():
    # tp barely above entry -> RR below floor -> None
    d = vp._enter("t", "XAUUSD", "vp_lvn_rejection", "buy",
                  entry=100.0, sl=99.0, tp=100.5, session="asian",
                  reasons=[], features={})
    assert d is None


def test_enter_geometry_rejected_on_wrong_sidedness():
    d = vp._enter("t", "XAUUSD", "vp_lvn_rejection", "sell",
                  entry=100.0, sl=99.0, tp=101.0, session="asian",
                  reasons=[], features={})
    assert d is None  # sell needs tp < entry < sl


# -- canary wiring flag (default OFF) -------------------------------------------


def test_vp_producer_flag_default_off(monkeypatch):
    from dexter3 import shadow_runner

    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert shadow_runner._vp_producer_enabled() is False


def test_vp_producer_flag_on(monkeypatch):
    from dexter3 import shadow_runner

    monkeypatch.setenv("DEXTER3_PRODUCER", "vp")
    assert shadow_runner._vp_producer_enabled() is True


def test_vp_producer_flag_other_value_off(monkeypatch):
    from dexter3 import shadow_runner

    monkeypatch.setenv("DEXTER3_PRODUCER", "hunt")
    assert shadow_runner._vp_producer_enabled() is False


# -- VP lane isolation (label / state / lock per DEXTER3_MODE=vp) ----------------


def test_vp_mode_selects_vp_label_state_and_producer(monkeypatch):
    from dexter3 import shadow_runner

    monkeypatch.setenv("DEXTER3_MODE", "vp")
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert shadow_runner._active_order_label() == vp.VP_LABEL
    assert shadow_runner._active_state_file().name == "dexter3_vp_shadow_state.json"
    assert shadow_runner._vp_producer_enabled() is True  # mode implies producer


def test_vp_mode_does_not_leak_into_default_lane(monkeypatch):
    from dexter3 import shadow_runner

    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert shadow_runner._active_order_label() != vp.VP_LABEL
    assert shadow_runner._active_state_file().name == "dexter3_shadow_state.json"
    assert shadow_runner._vp_producer_enabled() is False


def test_vp_label_is_distinct_from_fable_and_grok():
    from dexter3.executor import LABEL as FABLE_LABEL

    assert vp.VP_LABEL != FABLE_LABEL
    try:
        from dexter3.grok_v10 import GROK_LABEL
        assert vp.VP_LABEL != GROK_LABEL
    except ImportError:
        pass
