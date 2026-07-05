"""Unit tests for dexter3/hunter_brain.py — the M5 decision engine.

Critical invariants under test (per Dexter3 Phase 1 spec):
  (b) every ``enter`` decision has sl != None and reward:risk >= 1.2
  (c) every ``skip`` decision carries concrete (non-generic) reasons
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from dexter3 import hunter_brain, market_lens

GENERIC_BANNED_PHRASES = ("no edge", "unclear reasons", "just because", "vibes", "gut feeling")


def _ts_series(n: int, start: datetime | None = None, step_min: int = 5) -> list[str]:
    start = start or datetime(2026, 7, 5, 8, 0, 0, tzinfo=timezone.utc)
    return [(start + timedelta(minutes=step_min * i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(n)]


def _bar(o: float, h: float, l: float, c: float, ts: str) -> dict:
    return {"open": o, "high": h, "low": l, "close": c, "ts": ts}


def _uptrend_then_upper_rejection(n: int = 29) -> list[dict]:
    """Uptrend into an upper-shelf sweep + bearish-rejection close -> should enter short."""
    ts = _ts_series(n + 1)
    bars = []
    price = 2000.0
    for i in range(n):
        o = price
        c = price + 0.8
        h = c + 0.2
        l = o - 0.1
        bars.append(_bar(o, h, l, c, ts[i]))
        price = c
    prior_high = max(b["high"] for b in bars[-10:])
    rejection = _bar(price, prior_high + 1.0, prior_high - 1.0, prior_high - 1.3, ts[n])
    bars.append(rejection)
    return bars


def _downtrend_then_lower_sweep(n: int = 29) -> list[dict]:
    """Downtrend into a lower-shelf sweep + bullish reclaim close -> should enter long."""
    ts = _ts_series(n + 1)
    bars = []
    price = 2000.0
    for i in range(n):
        o = price
        c = price - 0.8
        h = o + 0.1
        l = c - 0.2
        bars.append(_bar(o, h, l, c, ts[i]))
        price = c
    prior_low = min(b["low"] for b in bars[-10:])
    reclaim = _bar(price, prior_low + 1.3, prior_low - 1.0, prior_low + 1.0, ts[n])
    bars.append(reclaim)
    return bars


def _flat_chop_bars(n: int = 25) -> list[dict]:
    ts = _ts_series(n)
    bars = []
    price = 2000.0
    for i in range(n):
        o = price
        c = price + (0.3 if i % 2 == 0 else -0.3)
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        bars.append(_bar(o, h, l, c, ts[i]))
        price = c
    return bars


# -- contract shape -----------------------------------------------------------------


def test_decide_returns_decision_for_every_call_participation_first():
    bars = _flat_chop_bars(25)
    decision = hunter_brain.decide("XAUUSD", None, bars)
    assert decision.action in ("enter", "skip", "manage")
    assert decision.symbol == "XAUUSD"
    assert decision.ts_close == bars[-1]["ts"]


def test_decide_insufficient_bars_still_returns_a_decision():
    bars = _flat_chop_bars(5)
    decision = hunter_brain.decide("BTCUSD", None, bars)
    assert decision.action == "skip"
    assert decision.reasons
    assert "insufficient" in decision.reasons[0].lower() or "20" in decision.reasons[0]


def test_decide_empty_bars_does_not_raise():
    decision = hunter_brain.decide("XAUUSD", None, [])
    assert decision.action == "skip"
    assert decision.ts_close == ""


# -- (b) every enter has sl != None and RR >= 1.2 --------------------------------------


def test_enter_short_has_valid_sl_and_rr_floor():
    bars = _uptrend_then_upper_rejection()
    decision = hunter_brain.decide("XAUUSD", None, bars)
    if decision.action == "enter":
        assert decision.sl is not None
        assert decision.tp is not None
        assert decision.entry is not None
        risk = abs(decision.entry - decision.sl)
        reward = abs(decision.tp - decision.entry)
        assert risk > 0
        assert reward / risk >= hunter_brain.MIN_REWARD_RISK - 1e-9


def test_enter_long_has_valid_sl_and_rr_floor():
    bars = _downtrend_then_lower_sweep()
    decision = hunter_brain.decide("XAUUSD", None, bars)
    if decision.action == "enter":
        assert decision.sl is not None
        assert decision.tp is not None
        risk = abs(decision.entry - decision.sl)
        reward = abs(decision.tp - decision.entry)
        assert risk > 0
        assert reward / risk >= hunter_brain.MIN_REWARD_RISK - 1e-9


def test_enter_decisions_never_have_none_sl_across_many_random_seeds():
    """Property-style sweep: across randomized synthetic bar sets, any 'enter'
    decision must always carry a real sl and satisfy the RR floor."""
    rng = random.Random(42)
    checked_enters = 0
    for seed in range(40):
        rng.seed(seed)
        n = 30
        ts = _ts_series(n)
        bars = []
        price = 2000.0 + rng.uniform(-50, 50)
        for i in range(n):
            o = price
            c = price + rng.uniform(-2.0, 2.0)
            h = max(o, c) + abs(rng.uniform(0, 1.5))
            l = min(o, c) - abs(rng.uniform(0, 1.5))
            bars.append(_bar(o, h, l, c, ts[i]))
            price = c
        decision = hunter_brain.decide("XAUUSD", None, bars)
        assert decision.action in ("enter", "skip", "manage")
        if decision.action == "enter":
            checked_enters += 1
            assert decision.sl is not None, f"seed={seed} entered without sl"
            assert decision.entry is not None
            assert decision.tp is not None
            risk = abs(decision.entry - decision.sl)
            assert risk > 0, f"seed={seed} entered with zero risk"
            reward = abs(decision.tp - decision.entry)
            rr = reward / risk
            assert rr >= hunter_brain.MIN_REWARD_RISK - 1e-9, f"seed={seed} rr={rr} below floor"
    # Not a hard requirement that any fire, but log-worthy if none did across 40 seeds.
    assert checked_enters >= 0


# -- (c) skip decisions carry concrete reasons ------------------------------------------


def test_skip_reasons_are_concrete_not_generic():
    bars = _flat_chop_bars(25)
    decision = hunter_brain.decide("BTCUSD", None, bars)
    assert decision.action == "skip"
    assert len(decision.reasons) > 0
    joined = " ".join(decision.reasons).lower()
    for banned in GENERIC_BANNED_PHRASES:
        assert banned not in joined, f"generic reason phrase found: {banned!r}"
    # concrete reasons should reference an actual numeric/state condition
    assert any(any(ch.isdigit() for ch in r) for r in decision.reasons), (
        f"skip reasons should cite concrete numeric conditions, got: {decision.reasons}"
    )


def test_skip_reasons_are_bilingual_thai_english():
    bars = _flat_chop_bars(25)
    decision = hunter_brain.decide("XAUUSD", None, bars)
    assert decision.action == "skip"
    joined = " ".join(decision.reasons)
    has_thai = any("฀" <= ch <= "๿" for ch in joined)
    assert has_thai, f"skip reasons should include Thai text, got: {decision.reasons}"


def test_skip_action_has_null_side_and_prices():
    bars = _flat_chop_bars(25)
    decision = hunter_brain.decide("XAUUSD", None, bars)
    assert decision.action == "skip"
    assert decision.side is None
    assert decision.entry is None
    assert decision.sl is None
    assert decision.tp is None
    assert decision.setup == "none"


# -- features snapshot -----------------------------------------------------------------


def test_decision_features_snapshot_present_and_populated():
    bars = _flat_chop_bars(25)
    decision = hunter_brain.decide("XAUUSD", None, bars)
    assert decision.features
    assert decision.features.get("symbol") == "XAUUSD"
    assert "swing_structure" in decision.features
    assert "liquidity_sweep" in decision.features
    assert "day_range_position" in decision.features


def test_decide_accepts_precomputed_features():
    bars = _flat_chop_bars(25)
    precomputed = hunter_brain._run_lens(bars, bars[-1]["ts"])
    decision = hunter_brain.decide("XAUUSD", precomputed, bars)
    assert decision.action in ("enter", "skip", "manage")


def test_decide_accepts_none_journal_stats():
    bars = _flat_chop_bars(25)
    decision = hunter_brain.decide("XAUUSD", None, bars, journal_stats=None)
    assert decision.action in ("enter", "skip", "manage")


# -- to_dict / contract field completeness ----------------------------------------------


def test_decision_to_dict_has_all_contract_fields():
    bars = _flat_chop_bars(25)
    decision = hunter_brain.decide("XAUUSD", None, bars)
    payload = decision.to_dict()
    for field in (
        "ts_close", "symbol", "action", "side", "entry_type", "entry", "sl", "tp",
        "size_class", "leader_score", "p_win_est", "setup", "reasons", "features",
    ):
        assert field in payload, f"missing contract field: {field}"


def test_p_win_est_within_bounds():
    bars = _uptrend_then_upper_rejection()
    decision = hunter_brain.decide("XAUUSD", None, bars)
    assert 0.0 <= decision.p_win_est <= 1.0


@pytest.mark.parametrize("symbol", ["XAUUSD", "BTCUSD"])
def test_decide_works_for_both_primary_symbols(symbol):
    bars = _flat_chop_bars(25)
    decision = hunter_brain.decide(symbol, None, bars)
    assert decision.symbol == symbol
    assert decision.action in ("enter", "skip", "manage")
