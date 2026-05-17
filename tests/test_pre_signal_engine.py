"""Tests for the Predictive Pre-Signal Engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analysis.pre_signal_engine import ConvictionInputs, PreSignalEngine
from analysis.pre_signal_engine.engine import PreSignalEngineConfig


_T0 = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)


def _inputs(bias: str = "long", vp: float = 0.9, dom: float = 0.8, sharp: float = 0.7) -> ConvictionInputs:
    return ConvictionInputs(
        bias=bias,
        vp_alignment=vp,
        dom_alignment=dom,
        sharpness_score=sharp,
        bias_atr=4.0,
        last_price=2300.0,
    )


def _clock_factory(start: datetime):
    state = {"now": start}

    def now() -> datetime:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return now, advance


def test_engine_disabled_returns_none():
    e = PreSignalEngine(config=PreSignalEngineConfig(enabled=False))
    d = e.evaluate(symbol="XAUUSD", inputs=_inputs(), now=_T0)
    assert d is None


def test_engine_first_sample_alone_is_not_enough():
    e = PreSignalEngine(config=PreSignalEngineConfig(enabled=True, sustained_seconds=2.0, arm_threshold=0.5))
    d = e.evaluate(symbol="XAUUSD", inputs=_inputs(), now=_T0)
    assert d is None  # needs at least 2 samples within sustained window


def test_engine_sustained_conviction_emits_directive():
    clock, advance = _clock_factory(_T0)
    e = PreSignalEngine(
        config=PreSignalEngineConfig(enabled=True, sustained_seconds=2.0, arm_threshold=0.5),
        clock=clock,
    )
    e.evaluate(symbol="XAUUSD", inputs=_inputs(), now=clock())
    advance(1.0)
    d = e.evaluate(symbol="XAUUSD", inputs=_inputs(), now=clock())
    assert d is not None
    assert d.bias == "long"
    assert d.conviction >= 0.5
    assert d.entry_type == "stop"
    assert d.risk_multiplier == 0.10


def test_engine_drop_below_threshold_breaks_sustain():
    clock, advance = _clock_factory(_T0)
    e = PreSignalEngine(
        config=PreSignalEngineConfig(enabled=True, sustained_seconds=2.0, arm_threshold=0.6),
        clock=clock,
    )
    # First two samples above threshold.
    e.evaluate(symbol="XAUUSD", inputs=_inputs(vp=0.95, dom=0.95, sharp=0.95), now=clock())
    advance(0.5)
    # Drop conviction below threshold.
    d = e.evaluate(symbol="XAUUSD", inputs=_inputs(vp=0.05, dom=0.05, sharp=0.05), now=clock())
    assert d is None


def test_engine_misaligned_inputs_dont_arm():
    clock, advance = _clock_factory(_T0)
    e = PreSignalEngine(
        config=PreSignalEngineConfig(enabled=True, sustained_seconds=2.0, arm_threshold=0.5),
        clock=clock,
    )
    # Bias=long but vp_alignment & dom_alignment are negative → aligned() returns 0.
    e.evaluate(symbol="XAUUSD", inputs=_inputs(bias="long", vp=-0.9, dom=-0.9, sharp=0.0), now=clock())
    advance(1.0)
    d = e.evaluate(symbol="XAUUSD", inputs=_inputs(bias="long", vp=-0.9, dom=-0.9, sharp=0.0), now=clock())
    assert d is None


def test_engine_cooldown_prevents_back_to_back_arms():
    clock, advance = _clock_factory(_T0)
    e = PreSignalEngine(
        config=PreSignalEngineConfig(enabled=True, sustained_seconds=2.0, arm_threshold=0.5, cooldown_seconds=60.0),
        clock=clock,
    )
    e.evaluate(symbol="XAUUSD", inputs=_inputs(), now=clock())
    advance(1.0)
    d1 = e.evaluate(symbol="XAUUSD", inputs=_inputs(), now=clock())
    assert d1 is not None
    # Immediately try again — cooldown blocks.
    advance(10.0)
    e.evaluate(symbol="XAUUSD", inputs=_inputs(), now=clock())
    advance(1.0)
    d2 = e.evaluate(symbol="XAUUSD", inputs=_inputs(), now=clock())
    assert d2 is None
    # After cooldown expires, can arm again.
    advance(60.0)
    e.evaluate(symbol="XAUUSD", inputs=_inputs(), now=clock())
    advance(1.0)
    d3 = e.evaluate(symbol="XAUUSD", inputs=_inputs(), now=clock())
    assert d3 is not None


def test_engine_short_bias_inverts_alignment():
    clock, advance = _clock_factory(_T0)
    e = PreSignalEngine(
        config=PreSignalEngineConfig(enabled=True, sustained_seconds=2.0, arm_threshold=0.5),
        clock=clock,
    )
    # Negative components favour short.
    e.evaluate(symbol="XAUUSD", inputs=_inputs(bias="short", vp=-0.9, dom=-0.9, sharp=0.7), now=clock())
    advance(1.0)
    d = e.evaluate(symbol="XAUUSD", inputs=_inputs(bias="short", vp=-0.9, dom=-0.9, sharp=0.7), now=clock())
    assert d is not None
    assert d.bias == "short"
    assert d.entry_price < 2300.0  # sell_stop sits below current price
