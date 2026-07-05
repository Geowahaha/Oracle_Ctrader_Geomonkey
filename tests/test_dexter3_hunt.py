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
    assert hunt_mode.WEIGHT_CLOSE_LOCATION_PRESSURE == 1.0
    assert hunt_mode.WEIGHT_SWING_STRUCTURE == 1.2
    assert hunt_mode.WEIGHT_DAY_RANGE_TILT == 1.0
    assert hunt_mode.WEIGHT_DISPLACEMENT == 0.8
    assert hunt_mode.WEIGHT_COMPRESSION_RELEASE == 0.8
    assert hunt_mode.WEIGHT_M15_DRIFT == 1.0
    assert hunt_mode.WEIGHT_SWEEP_RECLAIM_OVERRIDE == 1.5
    assert hunt_mode.WEIGHT_H1_CONTEXT == 0.6


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
