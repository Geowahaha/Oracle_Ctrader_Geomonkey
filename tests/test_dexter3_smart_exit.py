"""Tests for dexter3/smart_exit.py — GATED SMART ADAPTIVE EXIT.

Owner directive 2026-07-08: ``scripts/dexter3_edge_discovery.py --smart-exit``
(938 decisions) proved a close-confirmed exit (survive noise wicks, cut only
on a bar CLOSE beyond the structural invalidation, wide disaster hard-stop
for tail risk) AMPLIFIES edge on non-chase buckets (aligned-ranging
+0.254R -> +0.320R, WR 52->57%) but AMPLIFIES the LOSS on the chase bucket
(aligned-trending -0.285R -> -0.469R). So it must be gated OFF for chase
entries — this module's ``resolve_stop_regime`` reuses
``edge_buckets.classify_bucket``'s ``is_chase`` for that gate, never
reimplemented.

These tests cover:
  (a) resolve_stop_regime: chase -> always 'tight'; non-chase -> 'disaster'
      when enabled, 'tight' (but still journaled) when disabled.
  (b) disaster_stop_distance / size_scale_for_disaster_stop: the exact
      size-down math that keeps $ risk to the wide stop equal to the
      intended tight-stop risk.
  (c) should_smart_exit: mirrors _simulate_smart's confirmed-break
      definition exactly — fires ONLY on regime='disaster' AND
      level_lost AND m5_close_beyond; a wick-only (close back inside)
      survives; a 'tight' regime lane never fires regardless of evidence.
"""
from __future__ import annotations

import pytest

from dexter3.smart_exit import (
    REGIME_DISASTER,
    REGIME_TIGHT,
    SmartExitConfig,
    disaster_stop_distance,
    resolve_stop_regime,
    should_smart_exit,
    size_scale_for_disaster_stop,
)

# ---------------------------------------------------------------------------
# fixtures — mirrors tests/test_dexter3_edge_gate.py's H1 bar builders
# ---------------------------------------------------------------------------


def _bar(close: float) -> dict:
    return {"close": close, "open": close, "high": close, "low": close}


def _trending_up_h1(n: int = 10) -> list[dict]:
    return [_bar(2000.0 + i * 1.0) for i in range(n)]


def _trending_down_h1(n: int = 10) -> list[dict]:
    return [_bar(2000.0 - i * 1.0) for i in range(n)]


def _ranging_h1(n: int = 10) -> list[dict]:
    seq = [2000.0]
    for i in range(1, n):
        seq.append(seq[-1] + (6.0 if i % 2 == 1 else -5.8))
    return [_bar(c) for c in seq]


# ---------------------------------------------------------------------------
# resolve_stop_regime — the entry-side chase gate
# ---------------------------------------------------------------------------


def test_chase_entry_always_tight_regime_when_enabled():
    # aligned + trending == is_chase -> smart exit OFF, tight regime, even
    # though the module is enabled.
    result = resolve_stop_regime("buy", _trending_up_h1())
    assert result["is_chase"] is True
    assert result["would_apply_smart_exit"] is False
    assert result["applied"] is False
    assert result["regime"] == REGIME_TIGHT


def test_non_chase_entry_gets_disaster_regime_when_enabled():
    # counter-trend (not aligned+trending) -> not a chase -> smart exit ON.
    result = resolve_stop_regime("sell", _trending_up_h1())
    assert result["is_chase"] is False
    assert result["would_apply_smart_exit"] is True
    assert result["applied"] is True
    assert result["regime"] == REGIME_DISASTER


def test_ranging_non_chase_entry_gets_disaster_regime():
    result = resolve_stop_regime("buy", _ranging_h1())
    assert result["is_chase"] is False
    assert result["regime"] == REGIME_DISASTER


def test_disabled_config_always_tight_but_still_classifies_shadow_mode():
    cfg = SmartExitConfig(enabled=False)
    non_chase = resolve_stop_regime("sell", _trending_up_h1(), cfg)
    chase = resolve_stop_regime("buy", _trending_up_h1(), cfg)
    # both stay 'tight' when disabled...
    assert non_chase["regime"] == REGIME_TIGHT
    assert chase["regime"] == REGIME_TIGHT
    # ...but the shadow classification is still fully present.
    assert non_chase["would_apply_smart_exit"] is True
    assert non_chase["applied"] is False
    assert non_chase["enabled"] is False
    assert chase["would_apply_smart_exit"] is False
    assert chase["is_chase"] is True


def test_resolve_stop_regime_no_key_collision_between_h1_regime_and_stop_regime():
    # classify_bucket's own 'regime' (trending/ranging) must not be clobbered
    # by this function's stop 'regime' (tight/disaster) — exposed separately
    # as 'h1_regime'.
    result = resolve_stop_regime("sell", _trending_up_h1())
    assert result["h1_regime"] == "trending"
    assert result["regime"] in (REGIME_TIGHT, REGIME_DISASTER)


def test_resolve_stop_regime_default_disaster_mult_is_2():
    result = resolve_stop_regime("sell", _trending_up_h1())
    assert result["disaster_mult"] == pytest.approx(2.0)


def test_resolve_stop_regime_custom_disaster_mult_carried_through():
    cfg = SmartExitConfig(disaster_mult=3.5)
    result = resolve_stop_regime("sell", _trending_up_h1(), cfg)
    assert result["disaster_mult"] == pytest.approx(3.5)


def test_resolve_stop_regime_shared_regime_thresh_matches_anti_chase():
    # passing the SAME regime_thresh the anti-chase gate uses must produce
    # the SAME is_chase verdict on an ambiguous bucket (the two gates must
    # never disagree about which bucket an entry is in).
    from dexter3.edge_buckets import classify_bucket

    bars = [_bar(2000.0), _bar(2004.0), _bar(2002.0), _bar(2006.0), _bar(2004.0), _bar(2008.0), _bar(2006.0), _bar(2010.0)]
    anti_chase_bucket = classify_bucket("buy", bars, regime_thresh=0.1)
    smart_exit_bucket = resolve_stop_regime("buy", bars, regime_thresh=0.1)
    assert smart_exit_bucket["is_chase"] == anti_chase_bucket["is_chase"]


# ---------------------------------------------------------------------------
# disaster_stop_distance / size_scale_for_disaster_stop — size-down math
# ---------------------------------------------------------------------------


def test_disaster_stop_distance_scales_by_mult():
    assert disaster_stop_distance(10.0, 2.0) == pytest.approx(20.0)
    assert disaster_stop_distance(1.5, 2.5) == pytest.approx(3.75)


def test_disaster_stop_distance_never_negative():
    assert disaster_stop_distance(-5.0, 2.0) == pytest.approx(0.0)


def test_disaster_stop_distance_mult_clamped_to_at_least_1():
    # a misconfigured mult < 1 must never NARROW the stop below the tight one
    assert disaster_stop_distance(10.0, 0.5) == pytest.approx(10.0)


def test_size_scale_for_disaster_stop_is_reciprocal_of_mult():
    assert size_scale_for_disaster_stop(2.0) == pytest.approx(0.5)
    assert size_scale_for_disaster_stop(4.0) == pytest.approx(0.25)


def test_equal_risk_property_disaster_size_times_disaster_distance_equals_tight_risk():
    # THE core invariant: risk_usd computed against the WIDE stop with a
    # PROPORTIONALLY SMALLER size must equal the original tight-stop risk.
    tight_distance = 5.0
    risk_usd = 1.0
    tight_size = risk_usd / tight_distance
    disaster_mult = 2.0
    wide_distance = disaster_stop_distance(tight_distance, disaster_mult)
    scaled_size = tight_size * size_scale_for_disaster_stop(disaster_mult)
    disaster_risk_usd = scaled_size * wide_distance
    assert disaster_risk_usd == pytest.approx(risk_usd)
    assert scaled_size == pytest.approx(tight_size / disaster_mult)


# ---------------------------------------------------------------------------
# should_smart_exit — the OM soft-cut decision
# ---------------------------------------------------------------------------


def test_should_smart_exit_fires_on_confirmed_break():
    evidence = {"level_lost": True, "m5_close_beyond": True}
    verdict = should_smart_exit(REGIME_DISASTER, evidence)
    assert verdict["fire"] is True
    assert verdict["reason"] == "smart_confirmed_break"


def test_should_smart_exit_survives_noise_wick_reclaimed():
    # a wick beyond the level that has since closed back inside — level_lost
    # True (some bar crossed it) but m5_close_beyond False (last close is
    # back inside) -> HOLD, this is exactly the "survive noise" behavior.
    evidence = {"level_lost": True, "m5_close_beyond": False}
    verdict = should_smart_exit(REGIME_DISASTER, evidence)
    assert verdict["fire"] is False
    assert verdict["reason"] == "noise_wick_reclaimed"


def test_should_smart_exit_holds_with_no_evidence_at_all():
    evidence = {"level_lost": False, "m5_close_beyond": False}
    verdict = should_smart_exit(REGIME_DISASTER, evidence)
    assert verdict["fire"] is False
    assert verdict["reason"] == "no_confirmed_break"


def test_should_smart_exit_never_fires_on_tight_regime():
    # even with a fully confirmed break, a 'tight'-regime lane (chase entry,
    # or smart exit disabled) never fires this path — its tight hard SL is
    # the only exit mechanism, unchanged.
    evidence = {"level_lost": True, "m5_close_beyond": True}
    verdict = should_smart_exit(REGIME_TIGHT, evidence)
    assert verdict["fire"] is False
    assert verdict["reason"] == "not_disaster_regime"


def test_should_smart_exit_handles_malformed_evidence_gracefully():
    assert should_smart_exit(REGIME_DISASTER, None)["fire"] is False
    assert should_smart_exit(REGIME_DISASTER, {})["fire"] is False
    assert should_smart_exit(REGIME_DISASTER, "not_a_dict")["fire"] is False  # type: ignore[arg-type]
