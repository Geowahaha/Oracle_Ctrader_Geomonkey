"""Tests for the Mutation Sampler."""
from __future__ import annotations

from learning.self_mutation.sampler import KNOB_BY_NAME, Sampler
from learning.self_mutation.types import LossEvent


def _make_event(*, pnl: float = -25.0, mode: str = "signal_market", entry_type: str = "market", pair_cap: bool = False, mfe_r: float = 0.0) -> LossEvent:
    return LossEvent(
        position_id=1,
        source="scalp_xauusd:winner",
        symbol="XAUUSD",
        direction="short",
        pnl_usd=pnl,
        closed_utc="2026-05-17T01:00:00Z",
        raw_meta={
            "mode": mode,
            "entry_type": entry_type,
            "xau_pair_risk_cap_applied": pair_cap,
            "mfe_r": mfe_r,
        },
    )


def _baseline(knob: str) -> float:
    return {
        "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE": 7.0,
        "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_BIAS": 0.70,
        "XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER": 0.35,
        "XAU_OPENAPI_ENTRY_ROUTER_LIMIT_RETEST_RISK_RATIO": 0.08,
        "XAU_GUARDIAN_RUNNER_PRESERVE_R": 1.5,
        "CTRADER_XAU_PAIR_RISK_MAX_USD": 3.0,
        "RISK_PER_TRADE": 0.01,
    }[knob]


def _no_cooldown(_knob: str) -> None:
    return None


def test_sampler_skips_when_no_knob_is_relevant():
    event = LossEvent(
        position_id=1,
        source="other",
        symbol="EURUSD",
        direction="long",
        pnl_usd=-10.0,
        closed_utc="2026-05-17T01:00:00Z",
        raw_meta={},
    )
    sampler = Sampler(baseline_resolver=_baseline, last_mutation_resolver=_no_cooldown)
    assert sampler.sample(event) == []


def test_sampler_signal_market_loss_increases_min_score():
    event = _make_event(mode="signal_market", entry_type="market")
    sampler = Sampler(baseline_resolver=_baseline, last_mutation_resolver=_no_cooldown)
    candidates = sampler.sample(event, now_utc="2026-05-17T01:00:00Z")
    assert candidates
    knobs = {c.knob for c in candidates}
    assert "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE" in knobs
    for c in candidates:
        if c.knob == "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE":
            assert c.direction == "increase"
            assert c.proposed_value > c.baseline_value


def test_sampler_respects_sanity_band_at_edge():
    # Force baseline at the band max so a further increase clamps to max.
    sampler = Sampler(
        baseline_resolver=lambda k: 10.0 if k.endswith("MIN_SCORE") else _baseline(k),
        last_mutation_resolver=_no_cooldown,
    )
    candidates = sampler.sample(_make_event(mode="signal_market"))
    # MIN_SCORE has max=10.0 and direction=increase → at max, proposed == baseline → skipped.
    for c in candidates:
        if c.knob.endswith("MIN_SCORE"):
            assert c.proposed_value != c.baseline_value


def test_sampler_cooldown_filters_recent_knob():
    sampler = Sampler(
        baseline_resolver=_baseline,
        last_mutation_resolver=lambda k: "2026-05-17T00:30:00Z" if k.endswith("MIN_SCORE") else None,
        cooldown_hours=24.0,
    )
    candidates = sampler.sample(_make_event(mode="signal_market"), now_utc="2026-05-17T01:00:00Z")
    knobs = {c.knob for c in candidates}
    assert "XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE" not in knobs


def test_sampler_caps_at_max_candidates():
    sampler = Sampler(
        baseline_resolver=_baseline,
        last_mutation_resolver=_no_cooldown,
        max_candidates=3,
    )
    # Event triggers multiple knobs (signal_market mode + market entry + huge loss).
    event = _make_event(pnl=-60.0, mode="signal_market", entry_type="market")
    candidates = sampler.sample(event)
    assert len(candidates) <= 3


def test_sampler_huge_loss_triggers_risk_per_trade_decrease():
    sampler = Sampler(baseline_resolver=_baseline, last_mutation_resolver=_no_cooldown)
    # Use entry_type="limit" so the signal_market hint does not fire — we want to
    # isolate the global RISK_PER_TRADE knob's response to a large loss.
    candidates = sampler.sample(_make_event(pnl=-80.0, mode="other", entry_type="limit"))
    knobs = [c.knob for c in candidates]
    assert "RISK_PER_TRADE" in knobs
    for c in candidates:
        if c.knob == "RISK_PER_TRADE":
            assert c.direction == "decrease"


def test_sampler_runner_preserve_r_triggers_on_mfe_giveback():
    sampler = Sampler(baseline_resolver=_baseline, last_mutation_resolver=_no_cooldown)
    event = _make_event(pnl=-15.0, mode="other", entry_type="limit", mfe_r=1.4)
    candidates = sampler.sample(event)
    knobs = [c.knob for c in candidates]
    assert "XAU_GUARDIAN_RUNNER_PRESERVE_R" in knobs
    for c in candidates:
        if c.knob == "XAU_GUARDIAN_RUNNER_PRESERVE_R":
            assert c.direction == "decrease"
