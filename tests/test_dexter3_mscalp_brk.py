"""Pins the MSCALP-BRK blue-sky entry twin (owner observation 2026-08-07):
M15-pivot aggregation from M5, the binary blue-sky gate, the gated producer,
label isolation, resolvers."""
from __future__ import annotations

from dexter3 import mscalp as ms


def _bar(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


def _m5_series(n, start="2026-08-07T00:00:00Z", px=29000.0, step=0.0,
               spike_at=None, spike=30.0):
    """n M5 bars from start; optional single M15-pivot spike at group index."""
    from datetime import datetime, timedelta

    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    bars = []
    for i in range(n):
        ts = (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        o = px + step * i
        hi, lo, c = o + 2.0, o - 2.0, o + step * 0.5
        if spike_at is not None and i // 3 == spike_at:
            hi += spike
        bars.append(_bar(ts, o, hi, lo, c))
    return bars


def test_m15_pivots_from_m5_finds_the_spike():
    # 60 M5 bars -> 20 M15 groups (last one dropped as partial-boundary);
    # a lone high spike in group 8 must surface as the ONLY pivot high.
    bars = _m5_series(60, spike_at=8)
    piv_h, piv_l = ms.m15_pivots_from_m5(bars, look_m5=180, pivot_k=2)
    assert len(piv_h) == 1
    assert piv_h[0] > 29025.0  # the spiked group high


def test_blue_sky_gate_sides():
    piv_h, piv_l = [29100.0, 29150.0], [28900.0, 28850.0]
    # buy: must clear EVERY pivot high
    assert ms.mscalp_brk_blue_sky("buy", 29151.0, piv_h, piv_l) is True
    assert ms.mscalp_brk_blue_sky("buy", 29120.0, piv_h, piv_l) is False
    # sell: must be below EVERY pivot low
    assert ms.mscalp_brk_blue_sky("sell", 28849.0, piv_h, piv_l) is True
    assert ms.mscalp_brk_blue_sky("sell", 28860.0, piv_h, piv_l) is False
    # empty pivots = blue sky by definition; unknown side never passes
    assert ms.mscalp_brk_blue_sky("buy", 1.0, [], piv_l) is True
    assert ms.mscalp_brk_blue_sky("", 29999.0, piv_h, piv_l) is False


def test_decide_mscalp_brk_blocks_wall_and_journals_skip(monkeypatch):
    # Build an impulse+drift BUY (reuse the mscalp test recipe) but plant an
    # M15 pivot high ABOVE the entry -> the gate must convert the enter into
    # an sr_wall skip; removing the wall lets the same signal through.
    def _ts(i):
        return f"2026-08-07T{i // 12:02d}:{(i % 12) * 5:02d}:00Z"

    def drifting(n, spike_at=None):
        bars, px = [], 28000.0
        for i in range(n):
            o, c = px, px + 1.2
            hi, lo = o + 2.5, o - 1.0
            if spike_at is not None and i // 3 == spike_at:
                hi += 400.0  # a wall far above every later entry
            bars.append(_bar(_ts(i), o, hi, lo, c))
            px = c
        return bars

    def impulse_up(i, px, atr_guess=3.5):
        rng = atr_guess * 1.6
        return _bar(_ts(i), px, px + rng, px - rng * 0.1, px + rng * 0.8)

    walled = drifting(80, spike_at=20)
    walled.append(impulse_up(80, float(walled[-1]["close"])))
    d = ms.decide_mscalp_brk("USTEC", walled, spread_abs=1.5)
    assert d.action == "skip"
    assert "sr_wall" in d.reasons[0]

    clear = drifting(80)
    clear.append(impulse_up(80, float(clear[-1]["close"])))
    d2 = ms.decide_mscalp_brk("USTEC", clear, spread_abs=1.5)
    assert d2.action == "enter"
    assert d2.setup == "mscalp_brk_bluesky"
    base = ms.decide_mscalp("USTEC", clear, spread_abs=1.5)
    assert (d2.entry, d2.sl, d2.tp, d2.side) == (base.entry, base.sl, base.tp, base.side)


def test_mode_flags_independent(monkeypatch):
    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert ms.mscalp_brk_mode_enabled() is False
    monkeypatch.setenv("DEXTER3_MODE", "mscalp-brk")
    assert ms.mscalp_brk_mode_enabled() is True
    assert ms.mscalp_mode_enabled() is False
    assert ms.mscalp2_mode_enabled() is False
    assert ms.mscalp_be_mode_enabled() is False


def test_label_isolation_and_runner_resolvers(monkeypatch):
    from dexter3.executor import label_matches_family

    assert ms.MSCALP_BRK_LABEL == "dexter3:mscalp-brk:canary"
    assert label_matches_family(ms.MSCALP_BRK_LABEL, "dexter3:mscalp-brk") is True
    assert label_matches_family(ms.MSCALP_BRK_LABEL, "dexter3:mscalp") is False
    assert label_matches_family(ms.MSCALP_LABEL, "dexter3:mscalp-brk") is False

    import dexter3.shadow_runner as sr
    monkeypatch.setenv("DEXTER3_MODE", "mscalp-brk")
    assert sr._active_order_label() == "dexter3:mscalp-brk:canary"
    assert sr._active_label_family() == "dexter3:mscalp-brk"
    assert sr._active_state_file().name == "dexter3_mscalp_brk_shadow_state.json"
    assert sr._active_log_file().name == "dexter3_mscalp_brk_shadow.log"
    assert sr._mscalp_brk_producer_enabled() is True
    assert sr._mscalp_producer_enabled() is False
    assert sr._alt_producer_enabled() is True


def test_lane_tally_family_split():
    from ops.dexter3_lane_tally import _lane_family

    assert _lane_family("dexter3:mscalp-brk:canary") == "mscalp-brk"
    assert _lane_family("dexter3:mscalp-be:canary") == "mscalp-be"
    assert _lane_family("dexter3:mscalp:canary") == "mscalp"


def test_entry_selector_env_default_off(monkeypatch):
    # the exit twins switch entries with DEXTER3_MSCALP_ENTRY=brk; absent ->
    # original decide_mscalp (the mscalp control lane never sets it)
    monkeypatch.delenv(ms.ENV_MSCALP_ENTRY, raising=False)
    assert ms.mscalp_entry_is_brk() is False
    monkeypatch.setenv(ms.ENV_MSCALP_ENTRY, "brk")
    assert ms.mscalp_entry_is_brk() is True
    monkeypatch.setenv(ms.ENV_MSCALP_ENTRY, "base")
    assert ms.mscalp_entry_is_brk() is False
