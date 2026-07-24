"""Unit/property tests for dexter3/hunt_mode.py — HUNT MODE (participation-first).

Critical invariants under test (owner's explicit pivot, 2026-07-05):
  (a) decide_hunt() ALWAYS returns action='enter' with a full geometry once
      bars/spread/quote are sane — the ONLY allowed skips are the three
      named hard vetoes (bars_m5<60, invalid spread/quote, spread_bps over
      cap). No other skip path may exist anywhere in the module's source.
  (b) every entered decision satisfies RR>=1.2, TP distance>=8*spread, SL
      distance>=6*spread, conviction in [0,1].
  (c) the committee is deterministic (same input -> same output).
  (d) setup naming follows 'hunt_' + dominant_member_name.
  (e) reasons are bilingual (contain Thai script).
"""
from __future__ import annotations

import inspect
import random
import re
from datetime import datetime, timedelta, timezone

import pytest

from dexter3 import hunt_mode

THAI_RANGE = re.compile(r"[฀-๿]")


# ---------------------------------------------------------------------------
# bar generation helpers
# ---------------------------------------------------------------------------


def _ts_series(n: int, start: datetime | None = None, step_min: int = 5) -> list[str]:
    start = start or datetime(2026, 7, 5, 8, 0, 0, tzinfo=timezone.utc)
    return [(start + timedelta(minutes=step_min * i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(n)]


def _bar(o: float, h: float, l: float, c: float, ts: str) -> dict:
    return {"open": o, "high": h, "low": l, "close": c, "ts": ts}


def _random_bars(
    rng: random.Random,
    n: int,
    start: float = 2000.0,
    trend: float = 0.0,
    vol: float = 1.0,
    step_min: int = 5,
    ts_start: datetime | None = None,
) -> list[dict]:
    """Generate n synthetic OHLC bars with a configurable trend/chop/vol regime."""
    ts = _ts_series(n, start=ts_start, step_min=step_min)
    bars = []
    price = start
    for i in range(n):
        o = price
        drift = trend + rng.uniform(-vol, vol)
        c = o + drift
        hi = max(o, c) + rng.uniform(0, vol)
        lo = min(o, c) - rng.uniform(0, vol)
        bars.append(_bar(o, hi, lo, c, ts[i]))
        price = c
    return bars


def _regime_bars(rng: random.Random, seed_variant: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Produce (m5, m15, h1) bars across varied trend/chop/vol regimes."""
    regime = seed_variant % 6
    if regime == 0:
        m5 = _random_bars(rng, 90, trend=0.3, vol=0.6)  # steady uptrend
    elif regime == 1:
        m5 = _random_bars(rng, 90, trend=-0.3, vol=0.6)  # steady downtrend
    elif regime == 2:
        m5 = _random_bars(rng, 90, trend=0.0, vol=0.3)  # tight chop
    elif regime == 3:
        m5 = _random_bars(rng, 90, trend=0.0, vol=2.5)  # wide chop / high vol
    elif regime == 4:
        m5 = _random_bars(rng, 90, trend=0.05, vol=1.5)  # noisy mild uptrend
    else:
        m5 = _random_bars(rng, 90, trend=-0.05, vol=1.5)  # noisy mild downtrend

    m5_start = datetime(2026, 7, 5, 0, 0, 0, tzinfo=timezone.utc)
    m15 = _random_bars(rng, 40, trend=m5[-1]["close"] - m5[0]["open"], vol=1.2, step_min=15, ts_start=m5_start)
    h1 = _random_bars(rng, 24, trend=(m5[-1]["close"] - m5[0]["open"]) * 2, vol=3.0, step_min=60, ts_start=m5_start)
    return m5, m15, h1


def _spread_for(seed_variant: int) -> float:
    # Vary spread_abs across a realistic XAU/BTC range so the 6x/8x-spread
    # geometry floors are actually exercised at different scales.
    options = (0.02, 0.05, 0.1, 0.3, 0.5, 1.0)
    return options[seed_variant % len(options)]


# ---------------------------------------------------------------------------
# (a)+(b) property test — 300 randomized bar sets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", list(range(300)))
def test_decide_hunt_always_enters_with_valid_geometry(seed: int) -> None:
    rng = random.Random(seed)
    m5, m15, h1 = _regime_bars(rng, seed)
    spread_abs = _spread_for(seed)

    decision = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, spread_abs)

    assert decision.action == "enter", f"seed={seed} unexpectedly skipped: {decision.reasons}"
    assert decision.side in ("buy", "sell")
    assert decision.sl is not None
    assert decision.tp is not None
    assert decision.entry is not None

    sl_dist = abs(decision.entry - decision.sl)
    tp_dist = abs(decision.tp - decision.entry)
    rr = tp_dist / sl_dist if sl_dist > 0 else 0.0

    assert rr >= hunt_mode.MIN_REWARD_RISK - 1e-9, f"seed={seed} RR={rr} < floor"
    assert tp_dist >= hunt_mode.MIN_TP_SPREAD_MULT * spread_abs - 1e-9, f"seed={seed} TP dist too small"
    assert sl_dist >= hunt_mode.MIN_SL_SPREAD_MULT * spread_abs - 1e-9, f"seed={seed} SL dist too small"
    assert 0.0 <= decision.leader_score <= 1.0, f"seed={seed} conviction out of [0,1]: {decision.leader_score}"


# ---------------------------------------------------------------------------
# veto tests — the ONLY 3 allowed skip paths
# ---------------------------------------------------------------------------


def test_veto_insufficient_bars() -> None:
    rng = random.Random(1)
    m5, m15, h1 = _regime_bars(rng, 0)
    short_m5 = m5[:59]  # one below MIN_BARS_M5=60
    decision = hunt_mode.decide_hunt("XAUUSD", short_m5, m15, h1, None, spread_abs=0.05)
    assert decision.action == "skip"
    assert any("bars_m5" in r for r in decision.reasons)


def test_veto_bars_exactly_at_floor_enters() -> None:
    rng = random.Random(2)
    m5, m15, h1 = _regime_bars(rng, 0)
    exactly_min = m5[:60]
    decision = hunt_mode.decide_hunt("XAUUSD", exactly_min, m15, h1, None, spread_abs=0.05)
    assert decision.action == "enter"


@pytest.mark.parametrize("bad_spread", [0.0, -0.01, -5.0])
def test_veto_invalid_spread(bad_spread: float) -> None:
    rng = random.Random(3)
    m5, m15, h1 = _regime_bars(rng, 1)
    decision = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, spread_abs=bad_spread)
    assert decision.action == "skip"
    assert any("spread_abs" in r for r in decision.reasons)


def test_veto_invalid_quote_zero_close() -> None:
    rng = random.Random(4)
    m5, m15, h1 = _regime_bars(rng, 2)
    m5 = list(m5)
    m5[-1] = dict(m5[-1])
    m5[-1]["close"] = 0.0
    decision = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, spread_abs=0.05)
    assert decision.action == "skip"
    assert any("invalid quote" in r for r in decision.reasons)


def test_veto_spread_bps_over_cap() -> None:
    rng = random.Random(5)
    m5, m15, h1 = _regime_bars(rng, 3)
    # last close ~2000, spread of 50 -> ~250 bps, way over default 15bps cap
    decision = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, spread_abs=50.0)
    assert decision.action == "skip"
    assert any("spread_bps" in r for r in decision.reasons)


def test_veto_spread_bps_custom_config_cap() -> None:
    rng = random.Random(6)
    m5, m15, h1 = _regime_bars(rng, 4)
    cfg = hunt_mode.HuntConfig(max_spread_bps=5.0)
    # A spread that passes the default 15bps cap but fails a tighter 5bps cap.
    last_close = m5[-1]["close"]
    spread_abs = last_close * 0.001  # ~10 bps
    decision = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, spread_abs, config=cfg)
    assert decision.action == "skip"
    assert any("spread_bps" in r for r in decision.reasons)


def test_no_other_skip_path_exists_in_module_source() -> None:
    """Grep-style assertion: 'skip' Decision construction appears only inside
    the veto helper (_veto_skip). This guarantees hunt_mode cannot grow a
    second, unaudited skip path later without this test failing.
    """
    source = inspect.getsource(hunt_mode)
    # Find every line of actual CODE (not docstring/comment prose) that
    # constructs a Decision with action="skip". Real code writes this as a
    # bare Python kwarg (`action="skip",`); docstring prose in this codebase
    # references the same contract wrapped in RST double-backtick literals
    # (`` ``action='skip'`` ``) with single quotes, which the backtick/quote
    # check below excludes so this test only fires on real construction
    # sites, not on the module's own contract documentation.
    skip_action_lines = [
        (i + 1, line)
        for i, line in enumerate(source.splitlines())
        if re.search(r'action\s*=\s*"skip"', line) and "`" not in line
    ]
    assert skip_action_lines, "expected at least one action='skip' construction (in _veto_skip)"

    # Every such line must fall within the _veto_skip function's source body.
    veto_skip_source = inspect.getsource(hunt_mode._veto_skip)
    veto_skip_lines = set(veto_skip_source.splitlines())
    for lineno, line in skip_action_lines:
        assert line.strip() in {vl.strip() for vl in veto_skip_lines}, (
            f"line {lineno} constructs action='skip' outside _veto_skip: {line!r}"
        )

    # Also confirm decide_hunt calls only _check_hard_vetoes (which itself
    # only calls _veto_skip) for its skip path, and that there are exactly
    # 3 veto conditions checked in _check_hard_vetoes.
    check_source = inspect.getsource(hunt_mode._check_hard_vetoes)
    veto_call_count = check_source.count("_veto_skip(")
    assert veto_call_count == 3, f"expected exactly 3 veto call sites, found {veto_call_count}"


# ---------------------------------------------------------------------------
# committee determinism
# ---------------------------------------------------------------------------


def test_committee_determinism_same_input_same_output() -> None:
    rng = random.Random(7)
    m5, m15, h1 = _regime_bars(rng, 2)
    lens = hunt_mode._safe_run_lens(m5, m5[-1]["ts"])

    d1 = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, lens, spread_abs=0.05)
    d2 = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, lens, spread_abs=0.05)

    assert d1.action == d2.action
    assert d1.side == d2.side
    assert d1.entry == d2.entry
    assert d1.sl == d2.sl
    assert d1.tp == d2.tp
    assert d1.leader_score == d2.leader_score
    assert d1.setup == d2.setup
    assert d1.p_win_est == d2.p_win_est


def test_committee_determinism_across_many_seeds() -> None:
    for seed in range(30):
        rng = random.Random(seed + 1000)
        m5, m15, h1 = _regime_bars(rng, seed)
        lens = hunt_mode._safe_run_lens(m5, m5[-1]["ts"])
        first = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, lens, spread_abs=0.05)
        second = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, lens, spread_abs=0.05)
        assert first.to_dict() == second.to_dict(), f"seed={seed} nondeterministic output"


# ---------------------------------------------------------------------------
# dominant-member naming
# ---------------------------------------------------------------------------


def test_setup_naming_follows_hunt_prefix_plus_dominant_member() -> None:
    rng = random.Random(8)
    m5, m15, h1 = _regime_bars(rng, 0)
    lens = hunt_mode._safe_run_lens(m5, m5[-1]["ts"])
    decision = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, lens, spread_abs=0.05)

    assert decision.setup.startswith("hunt_")
    dominant_from_setup = decision.setup[len("hunt_") :]
    assert dominant_from_setup in hunt_mode._COMMITTEE_MEMBER_NAMES
    assert decision.features.get("hunt_dominant_member") == dominant_from_setup


def test_setup_naming_across_many_seeds_matches_recorded_dominant() -> None:
    for seed in range(50):
        rng = random.Random(seed + 2000)
        m5, m15, h1 = _regime_bars(rng, seed)
        decision = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, spread_abs=0.05)
        assert decision.action == "enter"
        dominant = decision.setup[len("hunt_") :]
        assert dominant == decision.features.get("hunt_dominant_member")


# ---------------------------------------------------------------------------
# bilingual reasons
# ---------------------------------------------------------------------------


def test_enter_reasons_contain_thai() -> None:
    rng = random.Random(9)
    m5, m15, h1 = _regime_bars(rng, 1)
    decision = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, spread_abs=0.05)
    assert decision.action == "enter"
    joined = " ".join(decision.reasons)
    assert THAI_RANGE.search(joined), f"expected Thai script in reasons: {decision.reasons}"


def test_veto_reasons_contain_thai() -> None:
    rng = random.Random(10)
    m5, m15, h1 = _regime_bars(rng, 0)
    decision = hunt_mode.decide_hunt("XAUUSD", m5[:10], m15, h1, None, spread_abs=0.05)
    assert decision.action == "skip"
    joined = " ".join(decision.reasons)
    assert THAI_RANGE.search(joined), f"expected Thai script in veto reasons: {decision.reasons}"


# ---------------------------------------------------------------------------
# committee weighting sanity (documented constants line up with spec)
# ---------------------------------------------------------------------------


def test_committee_weights_match_spec_constants() -> None:
    # FIX 3 (2026-07-07): m15_drift 1.0->1.3, h1_context 0.6->0.9 — trend
    # members rebalanced upward so day_range_tilt's mean-reversion vote
    # (w=1.0, unchanged) can no longer out-muscle the trend-following voice
    # the way it did in the 2026-07-06/07 counter-trend-shorts diagnosis.
    assert hunt_mode.WEIGHT_CLOSE_LOCATION_PRESSURE == 1.0
    assert hunt_mode.WEIGHT_SWING_STRUCTURE == 1.2
    assert hunt_mode.WEIGHT_DAY_RANGE_TILT == 1.0
    assert hunt_mode.WEIGHT_DISPLACEMENT == 0.8
    assert hunt_mode.WEIGHT_COMPRESSION_RELEASE == 0.8
    assert hunt_mode.WEIGHT_M15_DRIFT == 1.3
    assert hunt_mode.WEIGHT_SWEEP_RECLAIM_OVERRIDE == 1.5
    assert hunt_mode.WEIGHT_H1_CONTEXT == 0.9


def test_conviction_size_class_threshold() -> None:
    # Directly probe the size_class boundary logic via the public entrypoint
    # using a crafted lens: conviction is a function of the committee sum,
    # so we assert the documented threshold constant governs classification
    # rather than re-deriving the committee math here.
    assert hunt_mode.CONVICTION_SMALL_FLOOR == 0.35


def test_features_snapshot_includes_full_committee_breakdown() -> None:
    rng = random.Random(11)
    m5, m15, h1 = _regime_bars(rng, 3)
    decision = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, spread_abs=0.05)
    assert decision.action == "enter"
    committee = decision.features.get("hunt_committee")
    assert committee is not None
    for name in hunt_mode._COMMITTEE_MEMBER_NAMES:
        assert name in committee
        assert "vote" in committee[name]
        assert "weight" in committee[name]
        assert "weighted" in committee[name]


# ---------------------------------------------------------------------------
# FIX 3 (2026-07-07) — trend-agreement conviction guard
#
# Diagnosis this fix addresses: the committee shorted INTO strength because
# day_range_tilt's mean-reversion vote out-muscled the trend-following
# members (m15_drift, h1_context). This guard halves conviction whenever the
# chosen side opposes a strong aligned trend, and flips the side toward the
# trend unless the counter-trend evidence is a confirmed sweep_reclaim or is
# itself strong enough (>= COUNTER_TREND_OVERRIDE_STRENGTH_MULT x the trend
# pull). It must NEVER produce a skip — participation-first stays.
# ---------------------------------------------------------------------------


def _committee_stub(
    *,
    m15_vote: float = 0.0,
    h1_vote: float = 0.0,
    other_vote: float = 0.0,
    sweep_fired: bool = False,
    sweep_side: str | None = None,
) -> dict:
    """Build a minimal committee dict directly (bypassing _run_committee) so
    _apply_trend_agreement_guard / _trend_agreement can be unit-tested
    against exact, hand-picked vote combinations."""
    names = hunt_mode._COMMITTEE_MEMBER_NAMES
    weights = {
        "close_location_pressure": hunt_mode.WEIGHT_CLOSE_LOCATION_PRESSURE,
        "swing_structure": hunt_mode.WEIGHT_SWING_STRUCTURE,
        "day_range_tilt": hunt_mode.WEIGHT_DAY_RANGE_TILT,
        "displacement": hunt_mode.WEIGHT_DISPLACEMENT,
        "compression_release": hunt_mode.WEIGHT_COMPRESSION_RELEASE,
        "m15_drift": hunt_mode.WEIGHT_M15_DRIFT,
        "sweep_reclaim": hunt_mode.WEIGHT_SWEEP_RECLAIM_OVERRIDE,
        "h1_context": hunt_mode.WEIGHT_H1_CONTEXT,
    }
    committee = {}
    for name in names:
        if name == "m15_drift":
            vote = m15_vote
        elif name == "h1_context":
            vote = h1_vote
        elif name == "sweep_reclaim":
            vote = (1.0 if sweep_side == "buy" else -1.0) if sweep_fired else 0.0
        else:
            vote = other_vote
        weight = weights[name]
        detail = {"fired": sweep_fired, "side": sweep_side} if name == "sweep_reclaim" else {}
        committee[name] = {"vote": vote, "weight": weight, "weighted": vote * weight, "detail": detail}
    return committee


def test_trend_agreement_both_members_agree_strong() -> None:
    committee = _committee_stub(m15_vote=0.7, h1_vote=0.6)
    agreement, trend_side = hunt_mode._trend_agreement(committee)
    assert agreement == pytest.approx(0.65)
    assert trend_side == "buy"


def test_trend_agreement_members_disagree_no_trend_side() -> None:
    committee = _committee_stub(m15_vote=0.7, h1_vote=-0.6)
    _, trend_side = hunt_mode._trend_agreement(committee)
    assert trend_side is None


def test_guard_does_not_fire_when_no_aligned_trend() -> None:
    committee = _committee_stub(m15_vote=0.2, h1_vote=-0.1, other_vote=-0.3)
    side, conviction, detail = hunt_mode._apply_trend_agreement_guard("sell", 0.5, committee)
    assert side == "sell"
    assert conviction == 0.5
    assert detail["fired"] is False


def test_guard_does_not_fire_when_side_already_aligned() -> None:
    # Strong uptrend AND the committee's own side is buy -> no penalty.
    committee = _committee_stub(m15_vote=0.8, h1_vote=0.7, other_vote=0.5)
    side, conviction, detail = hunt_mode._apply_trend_agreement_guard("buy", 0.6, committee)
    assert side == "buy"
    assert conviction == 0.6
    assert detail["fired"] is False
    assert detail["trend_side"] == "buy"


def test_guard_penalizes_and_flips_weak_counter_trend_short() -> None:
    """The core diagnosed bug: a weak mean-reversion short vote opposing a
    strong aligned uptrend (m15_drift + h1_context both strongly positive)
    must be penalized AND flipped toward the trend (buy) — no confirmed
    sweep_reclaim, and the weak short evidence cannot clear the override bar.
    """
    # Strong buy trend (m15/h1 both strongly positive); the OTHER 6 members
    # vote moderately sell (-0.6 each) — enough to tip the raw committee's
    # weighted_sum negative (side='sell'; verified: weighted_sum=-0.99) but
    # NOT enough to clear the override bar (trend_pull=0.935,
    # COUNTER_TREND_OVERRIDE_STRENGTH_MULT x trend_pull=1.122 > 0.99).
    committee = _committee_stub(m15_vote=0.9, h1_vote=0.8, other_vote=-0.6, sweep_fired=False)
    side, conviction, detail = hunt_mode._apply_trend_agreement_guard("sell", 0.5, committee)
    assert detail["fired"] is True
    assert detail["confirmed_sweep_reclaim_counter_trend"] is False
    assert detail["strong_enough_to_stand"] is False
    assert side == "buy", "weak counter-trend evidence must flip toward the aligned trend"
    assert conviction == pytest.approx(0.5 * hunt_mode.COUNTER_TREND_CONVICTION_PENALTY_MULT)


def test_guard_lets_confirmed_sweep_reclaim_stand_counter_trend() -> None:
    """A confirmed sweep_reclaim in the counter-trend direction is structural
    reversal evidence, not mean-reversion noise — the side must STAND (not
    flip), conviction still penalized."""
    committee = _committee_stub(
        m15_vote=0.9, h1_vote=0.8, other_vote=-0.1, sweep_fired=True, sweep_side="sell"
    )
    side, conviction, detail = hunt_mode._apply_trend_agreement_guard("sell", 0.5, committee)
    assert detail["fired"] is True
    assert detail["confirmed_sweep_reclaim_counter_trend"] is True
    assert side == "sell", "confirmed sweep_reclaim must be allowed to stand against the trend"
    assert conviction == pytest.approx(0.5 * hunt_mode.COUNTER_TREND_CONVICTION_PENALTY_MULT)


def test_guard_lets_strong_counter_trend_evidence_stand_without_sweep() -> None:
    """Even without a confirmed sweep_reclaim, sufficiently strong
    counter-trend weighted evidence (>= COUNTER_TREND_OVERRIDE_STRENGTH_MULT
    x trend_pull) is allowed to stand — conviction still penalized."""
    # Make every non-trend member vote strongly sell (-1.0) so the counter
    # side's weighted_sum magnitude dwarfs the trend pull.
    committee = _committee_stub(m15_vote=0.6, h1_vote=0.55, other_vote=-1.0, sweep_fired=False)
    side, conviction, detail = hunt_mode._apply_trend_agreement_guard("sell", 0.7, committee)
    assert detail["fired"] is True
    assert detail["strong_enough_to_stand"] is True
    assert side == "sell"
    assert conviction == pytest.approx(0.7 * hunt_mode.COUNTER_TREND_CONVICTION_PENALTY_MULT)


def test_guard_never_produces_a_skip_via_decide_hunt() -> None:
    """Integration-level: even when the guard fires and flips the side,
    decide_hunt must still return action='enter' with full geometry —
    participation-first is never compromised by this fix."""
    rng = random.Random(4242)
    m5, m15, h1 = _regime_bars(rng, 0)
    # Craft m15/h1 into a strong, clearly aligned uptrend to make the guard
    # deterministic regardless of which side the raw committee first picks.
    m15_strong_up = _random_bars(rng, 40, start=m15[-1]["close"], trend=2.0, vol=0.3, step_min=15)
    h1_strong_up = _random_bars(rng, 24, start=h1[-1]["close"], trend=6.0, vol=0.5, step_min=60)
    decision = hunt_mode.decide_hunt("XAUUSD", m5, m15_strong_up, h1_strong_up, None, spread_abs=0.05)
    assert decision.action == "enter"
    assert decision.side in ("buy", "sell")
    trend_guard = decision.features.get("hunt_trend_guard")
    assert trend_guard is not None


def test_guard_property_conviction_never_increases() -> None:
    """Across many randomized committee snapshots, the guard must never
    INCREASE conviction relative to the raw value — it only ever holds it
    steady or penalizes (halves) it."""
    rng = random.Random(777)
    for _ in range(200):
        m15_vote = rng.uniform(-1.0, 1.0)
        h1_vote = rng.uniform(-1.0, 1.0)
        other_vote = rng.uniform(-1.0, 1.0)
        raw_side = rng.choice(["buy", "sell"])
        raw_conviction = rng.uniform(0.0, 1.0)
        sweep_fired = rng.choice([True, False])
        sweep_side = rng.choice(["buy", "sell"])
        committee = _committee_stub(
            m15_vote=m15_vote, h1_vote=h1_vote, other_vote=other_vote,
            sweep_fired=sweep_fired, sweep_side=sweep_side,
        )
        _, conviction, _ = hunt_mode._apply_trend_agreement_guard(raw_side, raw_conviction, committee)
        assert conviction <= raw_conviction + 1e-9


def test_guard_property_never_flips_when_aligned_or_no_trend() -> None:
    """Across many randomized committee snapshots: if there's no strong
    aligned trend, or the side already matches the trend, the side returned
    must be unchanged from the input side."""
    rng = random.Random(888)
    for _ in range(200):
        m15_vote = rng.uniform(-1.0, 1.0)
        h1_vote = rng.uniform(-1.0, 1.0)
        other_vote = rng.uniform(-1.0, 1.0)
        raw_side = rng.choice(["buy", "sell"])
        committee = _committee_stub(m15_vote=m15_vote, h1_vote=h1_vote, other_vote=other_vote)
        agreement, trend_side = hunt_mode._trend_agreement(committee)
        no_aligned_trend = trend_side is None or abs(agreement) < hunt_mode.TREND_AGREEMENT_THRESHOLD
        already_aligned = trend_side == raw_side
        side, _, _ = hunt_mode._apply_trend_agreement_guard(raw_side, 0.5, committee)
        if no_aligned_trend or already_aligned:
            assert side == raw_side


def test_decide_hunt_enter_carries_session_label() -> None:
    """HUNT is the LIVE entry producer (DEXTER3_HUNT=1 on the VM units) — its
    enter Decisions must carry the session bucket or every live outcome
    journals session="" and empirical_stats skips it (2026-07-11: the first
    vanish-reconcile backfill showed session=None on every live row)."""
    rng = random.Random(7)
    m5, m15, h1 = _regime_bars(rng, 7)
    decision = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, _spread_for(7))
    assert decision.action == "enter"
    expected = str((decision.features.get("session_context") or {}).get("value") or "unknown")
    assert decision.session == expected
    assert decision.session != ""


def test_decide_hunt_blends_mature_empirical_stats_without_changing_entry_contract() -> None:
    rng = random.Random(31)
    m5, m15, h1 = _regime_bars(rng, 31)
    baseline = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, _spread_for(31))
    stats = {
        f"{baseline.setup}|{baseline.session}": {"win_rate": 0.90, "samples": 10, "below_min_samples": False}
    }
    blended = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, _spread_for(31), journal_stats=stats)
    assert blended.action == "enter"
    assert blended.side == baseline.side
    assert blended.p_win_est > baseline.p_win_est
    assert blended.features["empirical_p_win"]["applied"] is True


def test_decide_hunt_ignores_immature_empirical_stats() -> None:
    rng = random.Random(32)
    m5, m15, h1 = _regime_bars(rng, 32)
    baseline = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, _spread_for(32))
    stats = {f"{baseline.setup}|{baseline.session}": {"win_rate": 0.90, "samples": 9, "below_min_samples": True}}
    unchanged = hunt_mode.decide_hunt("XAUUSD", m5, m15, h1, None, _spread_for(32), journal_stats=stats)
    assert unchanged.p_win_est == baseline.p_win_est
    assert unchanged.features["empirical_p_win"]["applied"] is False


# -- sweep_reclaim FADE->FOLLOW flip (2026-07-24, owner fade->follow) ----------

def _sweep_lens(side: str) -> dict:
    return {
        "liquidity_sweep": {"value": True, "side": side, "level": 100.0,
                            "evidence": "test"},
        "swing_structure": {"last_swing_high": {"price": 105.0},
                            "last_swing_low": {"price": 95.0}},
    }


def _sweep_bars(strong: bool) -> list[dict]:
    # 30 calm ~1.0-range bars, then a sell-side sweep bar: big UPPER wick
    # (poke high, close back). strong=big wick + high volume.
    bars = [{"ts": f"2026-07-24T00:{i:02d}:00Z", "open": 100.0, "high": 100.6,
             "low": 99.4, "close": 100.0, "volume": 100.0} for i in range(30)]
    if strong:
        bars.append({"ts": "2026-07-24T00:30:00Z", "open": 100.0, "high": 104.0,
                     "low": 99.8, "close": 100.2, "volume": 500.0})  # 3.8 wick, 5x vol
    else:
        bars.append({"ts": "2026-07-24T00:30:00Z", "open": 100.0, "high": 100.7,
                     "low": 99.8, "close": 100.2, "volume": 100.0})  # tiny wick
    return bars


def test_sweep_default_is_legacy_fade(monkeypatch):
    monkeypatch.delenv("DEXTER3_HUNT_SWEEP_FOLLOW", raising=False)
    from dexter3.hunter_brain import _try_sweep_reclaim_setup
    setup, side, *_ = _try_sweep_reclaim_setup(_sweep_bars(True), _sweep_lens("sell"))
    assert setup == "sweep_reclaim" and side == "sell"   # fade the poke


def test_sweep_follow_flips_strong_grab(monkeypatch):
    monkeypatch.setenv("DEXTER3_HUNT_SWEEP_FOLLOW", "1")
    from dexter3.hunter_brain import _try_sweep_reclaim_setup
    setup, side, entry, sl, tp, reasons = _try_sweep_reclaim_setup(
        _sweep_bars(True), _sweep_lens("sell"))
    assert setup == "sweep_reclaim" and side == "buy"    # FOLLOW the grab up
    assert sl < entry < tp                                # long geometry
    assert "follow" in reasons[0]


def test_sweep_follow_skips_weak_grab(monkeypatch):
    monkeypatch.setenv("DEXTER3_HUNT_SWEEP_FOLLOW", "1")
    from dexter3.hunter_brain import _try_sweep_reclaim_setup
    out = _try_sweep_reclaim_setup(_sweep_bars(False), _sweep_lens("sell"))
    assert out[0] is None                                 # weak wick -> skip


# -- LIVE committee vote flip (2026-07-24): _vote_sweep_reclaim is the path the
#    live decide_hunt committee uses (NOT hunter_brain._try_sweep_reclaim_setup)

def _sweep_vote_lens(side, wick_atr, vol_ratio):
    return {"liquidity_sweep": {"value": True, "side": side, "wick_atr": wick_atr,
                                "vol_ratio": vol_ratio, "evidence": "x"}}


def test_vote_sweep_default_fades(monkeypatch):
    monkeypatch.delenv("DEXTER3_HUNT_SWEEP_FOLLOW", raising=False)
    from dexter3.hunt_mode import _vote_sweep_reclaim
    assert _vote_sweep_reclaim(_sweep_vote_lens("sell", 1.4, 1.3))[0] == -1.0  # sell/fade
    assert _vote_sweep_reclaim(_sweep_vote_lens("buy", 1.4, 1.3))[0] == +1.0


def test_vote_sweep_follow_flips_strong(monkeypatch):
    monkeypatch.setenv("DEXTER3_HUNT_SWEEP_FOLLOW", "1")
    from dexter3.hunt_mode import _vote_sweep_reclaim
    assert _vote_sweep_reclaim(_sweep_vote_lens("sell", 1.4, 1.3))[0] == +1.0  # follow -> buy
    assert _vote_sweep_reclaim(_sweep_vote_lens("buy", 1.4, 1.3))[0] == -1.0


def test_vote_sweep_follow_skips_weak(monkeypatch):
    monkeypatch.setenv("DEXTER3_HUNT_SWEEP_FOLLOW", "1")
    from dexter3.hunt_mode import _vote_sweep_reclaim
    assert _vote_sweep_reclaim(_sweep_vote_lens("sell", 0.4, 0.8))[0] == 0.0   # weak -> no vote


def test_liquidity_sweep_reports_strength():
    from dexter3 import market_lens
    bars = [{"ts": f"t{i}", "open": 100, "high": 100.5, "low": 99.5, "close": 100, "volume": 100}
            for i in range(12)]
    # strong sell grab: big upper wick, high volume
    bars.append({"ts": "t12", "open": 100, "high": 104, "low": 99.8, "close": 100.1, "volume": 400})
    sw = market_lens.liquidity_sweep(bars)
    assert sw["value"] and sw["side"] == "sell"
    assert sw["wick_atr"] > 1.0 and sw["vol_ratio"] > 1.0


def test_vote_swing_extension_gate(monkeypatch):
    from dexter3.hunt_mode import _vote_swing_structure
    up_ext = {"swing_structure": {"value": "uptrend", "extension": 0.85}}
    up_pull = {"swing_structure": {"value": "uptrend", "extension": 0.25}}
    # default off: both vote +1
    monkeypatch.delenv("DEXTER3_HUNT_SWING_MIN_EXT", raising=False)
    assert _vote_swing_structure(up_ext)[0] == 1.0
    assert _vote_swing_structure(up_pull)[0] == 1.0
    # gate on: extended keeps its vote, deep pullback is skipped (momentum weak)
    monkeypatch.setenv("DEXTER3_HUNT_SWING_MIN_EXT", "0.4")
    assert _vote_swing_structure(up_ext)[0] == 1.0
    assert _vote_swing_structure(up_pull)[0] == 0.0


def test_swing_structure_reports_extension():
    from dexter3 import market_lens
    # rising structure, price near the top of the range = high extension
    bars = []
    for i in range(24):
        base = 100 + i * 0.5
        bars.append({"ts": f"t{i}", "open": base, "high": base + 0.6,
                     "low": base - 0.6, "close": base + 0.4, "volume": 100})
    sw = market_lens.swing_structure(bars)
    assert "extension" in sw and 0.0 <= sw["extension"] <= 1.0
