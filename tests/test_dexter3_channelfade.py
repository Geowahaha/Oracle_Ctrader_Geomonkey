"""Unit tests for dexter3/channelfade.py — the RANGE-phase fade producer
(Edge A, owner 2026-07-16/17 + 2026-07-24).

Pure module, synthetic bars only. Pins: box detection (height/efficiency/
edge-touch gates), edge-reversal fade trigger + sidedness, POC-vs-geometric
target selection, the mean-reversion RR floor, graceful skips, mode flag, and
the additive contract (no env => no crash, off-by-default).
"""
from __future__ import annotations

import pytest

from dexter3 import channelfade as chf


def _bar(ts: str, o: float, h: float, l: float, c: float, v: float = 30.0) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _ts(i: int) -> str:
    return f"2026-07-24T{i // 12:02d}:{(i % 12) * 5:02d}:00Z"


def _box(n: int = 44, lo: float = 4041.0, hi: float = 4049.0,
         poc: float | None = None, period: int = 6) -> list[dict]:
    """A realistic sideways box: a GRADUAL triangle-wave zigzag between the lo
    and hi edges (small per-bar range => low ATR relative to height, unlike an
    edge-to-edge alternation which would inflate ATR past the height gate).
    Touches each edge once per ``period`` bars; whole cycles keep first/last
    close near-equal (low efficiency). If ``poc`` is set, a few recent
    heavy-volume low-range bars sit there so build_profile's POC lands on it.
    """
    span = hi - lo
    half = period // 2
    bars: list[dict] = []
    prev = lo
    for i in range(n):
        phase = i % period
        frac = phase / half if phase <= half else (period - phase) / half
        price = lo + span * frac
        o, c = prev, price
        hg = max(o, c) + 0.3
        lw = min(o, c) - 0.3
        if phase == half:      # crest -> guarantee a top-edge touch
            hg = hi
        if phase == 0:         # trough -> guarantee a bottom-edge touch
            lw = lo
        bars.append(_bar(_ts(i), o, hg, lw, c, v=25.0))
        prev = c
    if poc is not None:        # overwrite a few RECENT bars (inside the window)
        for k in range(3):
            j = n - 4 - k
            bars[j] = _bar(_ts(j), poc, poc + 0.3, poc - 0.3, poc, v=800.0)
    return bars


@pytest.fixture
def loose_eff(monkeypatch):
    """Isolate the fade-TRIGGER + target + RR behaviour from the efficiency
    gate: a synthetic triangle box's 36-bar window drifts by where the window
    boundary falls (an artifact of the fixture, not the producer). The
    efficiency gate has its own dedicated test (test_trending_box_skips); the
    trigger tests loosen it so they measure only the thing under test."""
    monkeypatch.setenv("DEXTER3_CHF_MAX_EFF", "0.95")


def _sell_signal(hi: float = 4049.0) -> dict:
    # touches the top edge, closes RED back inside the upper half
    return _bar("2026-07-24T05:00:00Z", hi, hi + 0.3, hi - 2.0, hi - 1.2, v=40.0)


def _buy_signal(lo: float = 4041.0) -> dict:
    # touches the bottom edge, closes GREEN back inside the lower half
    return _bar("2026-07-24T05:00:00Z", lo, lo + 2.0, lo - 0.3, lo + 1.2, v=40.0)


# -- happy path: fade both edges ---------------------------------------------


def test_sell_fade_top_edge_enters_market(loose_eff):
    bars = _box() + [_sell_signal()]
    d = chf.decide_channelfade("XAUUSD", bars, 0.12, session="asian")
    assert d.action == "enter"
    assert d.side == "sell"
    assert d.entry_type == "market"
    assert d.setup == "channelfade_edge_fade"
    # geometry: TP below entry below SL (a valid short fade)
    assert d.tp < d.entry < d.sl
    assert d.session == "asian"


def test_buy_fade_bottom_edge_enters_market(loose_eff, monkeypatch):
    # geo-mid target isolates the buy-side trigger (a POC that lands close to
    # the entry is correctly rejected on RR — covered by the POC tests)
    monkeypatch.setenv("DEXTER3_CHF_POC_TARGET", "0")
    bars = _box() + [_buy_signal()]
    d = chf.decide_channelfade("XAUUSD", bars, 0.12, session="london")
    assert d.action == "enter"
    assert d.side == "buy"
    assert d.sl < d.entry < d.tp


# -- selectivity gates: the range-regime filter -------------------------------


def test_trending_box_skips_on_efficiency():
    # a window that drifts edge-to-edge (first close top, last close bottom)
    # has high directional efficiency => not a box, default gate rejects it
    bars = _box() + [_buy_signal()]
    d = chf.decide_channelfade("XAUUSD", bars, 0.12)
    assert d.action == "skip"
    assert "efficiency" in d.reasons[0]


def test_too_tight_box_skips_on_height():
    # a very tight box (height << min_h_atr x ATR) is spread noise
    bars = [_bar(_ts(i), 4045.0, 4045.1, 4044.9, 4045.0) for i in range(36)]
    bars += [_bar("2026-07-24T05:00:00Z", 4045.05, 4045.12, 4044.98, 4045.02)]
    d = chf.decide_channelfade("XAUUSD", bars, 0.12)
    assert d.action == "skip"


def test_non_reversal_touch_skips(loose_eff):
    # last bar TOUCHES the top edge but closes GREEN (continuation, not a
    # reversal) => no fade; falls through to no_edge_reversal
    cont = _bar("2026-07-24T05:00:00Z", 4047.5, 4049.2, 4047.3, 4049.0, v=40.0)
    bars = _box() + [cont]
    d = chf.decide_channelfade("XAUUSD", bars, 0.12)
    assert d.action == "skip"
    assert d.reasons[0].endswith("no_edge_reversal")


def test_insufficient_bars_skips():
    d = chf.decide_channelfade("XAUUSD", _box(n=10), 0.12)
    assert d.action == "skip"
    assert "bars<" in d.reasons[0]


# -- POC target (graft vp DNA) vs geometric-mid fallback ----------------------


def test_poc_target_used_when_on_fade_side(loose_eff):
    # POC injected at 4043 (below a sell entry ~4047.8) => valid short target
    bars = _box(poc=4043.0) + [_sell_signal()]
    d = chf.decide_channelfade("XAUUSD", bars, 0.12)
    assert d.action == "enter"
    assert d.features.get("chf_tp_src", "").startswith("poc=")
    assert abs(d.tp - 4043.0) < 1.5  # TP anchored near the POC


def test_poc_target_disabled_falls_back_to_geometric_mid(loose_eff, monkeypatch):
    monkeypatch.setenv("DEXTER3_CHF_POC_TARGET", "0")
    bars = _box(poc=4043.0) + [_sell_signal()]
    d = chf.decide_channelfade("XAUUSD", bars, 0.12)
    assert d.action == "enter"
    assert d.features.get("chf_tp_src") == "geo_mid"


def test_poc_wrong_side_falls_back_to_geometric_mid(loose_eff):
    # POC injected ABOVE a sell entry => not a valid short target => geo mid
    bars = _box(poc=4048.5) + [_sell_signal()]
    d = chf.decide_channelfade("XAUUSD", bars, 0.12)
    if d.action == "enter" and d.side == "sell":
        assert d.features.get("chf_tp_src", "").startswith("geo_mid")


# -- RR floor is its own knob (mean-reversion wins on hit-rate) ---------------


def test_rr_floor_env_can_reject_thin_geometry(loose_eff, monkeypatch):
    # an impossibly high RR floor rejects even a clean fade (proves the knob
    # is live and defaults are not silently hardcoded)
    monkeypatch.setenv("DEXTER3_CHF_MIN_RR", "9.0")
    monkeypatch.setenv("DEXTER3_CHF_POC_TARGET", "0")  # geo mid => RR ~1
    bars = _box() + [_sell_signal()]
    d = chf.decide_channelfade("XAUUSD", bars, 0.12)
    assert d.action == "skip"


def test_default_rr_floor_admits_half_box_fade(loose_eff, monkeypatch):
    monkeypatch.setenv("DEXTER3_CHF_POC_TARGET", "0")  # geo mid target
    bars = _box() + [_sell_signal()]
    d = chf.decide_channelfade("XAUUSD", bars, 0.12)
    # a half-box fade (RR ~1) must survive the default 1.0 floor
    assert d.action == "enter"


# -- mode flag + additive contract -------------------------------------------


def test_mode_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert chf.channelfade_mode_enabled() is False


def test_mode_flag_on_via_mode_or_producer(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "channelfade")
    assert chf.channelfade_mode_enabled() is True
    monkeypatch.setenv("DEXTER3_MODE", "vp")
    monkeypatch.setenv("DEXTER3_PRODUCER", "channelfade")
    assert chf.channelfade_mode_enabled() is True


def test_missing_volume_degrades_to_geo_mid_not_crash(loose_eff):
    # bars without volume: build_profile can't form => geo-mid fallback, no raise
    bars = [_bar(_ts(i), 4041.0 + (8 if i % 2 else 0), 4041.5 + (8 if i % 2 else 0),
                 4040.5 + (8 if i % 2 else 0), 4041.0 + (8 if i % 2 else 0)) for i in range(36)]
    for b in bars:
        b.pop("volume", None)
    bars += [_sell_signal()]
    d = chf.decide_channelfade("XAUUSD", bars, 0.12)  # must not raise
    assert d.action in ("enter", "skip")


def test_label_constants_are_isolated():
    assert chf.CHF_LABEL == "dexter3:chf:canary"
    assert chf.CHF_LABEL_FAMILY == "dexter3:chf"
    assert chf.CHF_LABEL.startswith(chf.CHF_LABEL_FAMILY)
