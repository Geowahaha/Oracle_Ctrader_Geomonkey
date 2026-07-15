"""Dexter3 PRICE ACTION EYE — Phase A tests (2026-07-15, additive, shadow-only).

Covers ``dexter3/price_action_eye.py`` (pure Layer-1 bar-anatomy detectors +
v0 verdict) and its wiring into ``dexter3/shadow_runner.py`` (env
``DEXTER3_PA_EYE=off|shadow``, journaled BEFORE ``DecisionJournal.insert_decision``
so the existing journaling captures it automatically).

Includes the two 2026-07-15 owner-flagged regression fixtures the design doc
(``docs/DEXTER3_PRICE_ACTION_EYE_DESIGN.md``) calls out as ACCEPTANCE
fixtures:
  Fixture A -- fable buy 652554512: uptrend into a micro swing high, last two
               bars long upper wicks closing low-in-range at that level ->
               evaluate(side="buy") MUST return "oppose".
  Fixture B -- grok sell 652509725: decline into a micro swing low, last bar
               sweeps below the low and CLOSES bullish with a long lower
               wick -> evaluate(side="sell") MUST return "oppose".

NO live MCP calls, NO touching data/runtime/* -- every test drives a fake
mcp / temp sqlite DB and monkeypatches ``sr.log_line``, same conventions as
tests/test_dexter3_lane_decoupling.py and tests/test_dexter3_wiring.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import dexter3.shadow_runner as sr
from dexter3 import price_action_eye as pae
from dexter3.decision_journal import DecisionJournal
from dexter3.hunter_brain import Decision


def _bar(o: float, h: float, l: float, c: float, ts: str) -> dict[str, Any]:
    return {"open": o, "high": h, "low": l, "close": c, "ts": ts}


# ---------------------------------------------------------------------------
# Fixture A -- fable buy 652554512 (rejection cluster + sweep at resistance)
# ---------------------------------------------------------------------------


def _fixture_a_bars() -> list[dict[str, Any]]:
    bars = []
    prices = [2000, 2003, 2006, 2009, 2012, 2015]
    for i, p in enumerate(prices):
        bars.append(_bar(p - 0.2, p + 2.5, p - 0.5, p + 2.2, f"2026-07-15T0{i}:00:00Z"))
    level = 2017.2
    # confirms the pivot at index 5 (price 2017.5) as a swing high
    bars.append(_bar(level - 2.0, level + 0.3, level - 3.0, level - 1.8, "2026-07-15T06:00:00Z"))
    # two long-upper-wick bars closing low-in-range AT the resistance level
    bars.append(_bar(2016.3, 2017.5, 2016.1, 2016.4, "2026-07-15T07:00:00Z"))
    bars.append(_bar(2016.2, 2017.6, 2016.0, 2016.3, "2026-07-15T08:00:00Z"))
    return bars


def test_fixture_a_fable_buy_into_resistance_wicks_must_oppose():
    result = pae.evaluate(_fixture_a_bars(), "buy")
    assert result["verdict"] == "oppose"
    assert result["verdict_reasons"]
    # both the 2-of-3 rejection cluster AND the sweep-and-reclaim mirror fire
    assert result["features"]["rejection_cluster_upper"]["value"] is True
    assert result["features"]["swing_location"]["at_minor_resistance"] is True


def test_fixture_a_does_not_falsely_oppose_the_other_side():
    result = pae.evaluate(_fixture_a_bars(), "sell")
    assert result["verdict"] == "neutral"


# ---------------------------------------------------------------------------
# Fixture B -- grok sell 652509725 (single-bar sweep+bullish-close at support)
# ---------------------------------------------------------------------------


def _fixture_b_bars() -> list[dict[str, Any]]:
    bars = []
    prices = [2020, 2017, 2014, 2011, 2008, 2005]
    for i, p in enumerate(prices):
        bars.append(_bar(p + 0.2, p + 0.5, p - 2.5, p - 2.2, f"2026-07-15T0{i}:00:00Z"))
    # confirm the pivot at index 5 (low 2002.5) as a swing low
    bars.append(_bar(2003.5, 2003.8, 2003.0, 2003.2, "2026-07-15T06:00:00Z"))
    bars.append(_bar(2003.4, 2003.6, 2003.2, 2003.3, "2026-07-15T07:00:00Z"))
    # last bar: sweeps below the swing low, CLOSES bullish, long lower wick
    bars.append(_bar(2002.6, 2003.0, 2001.8, 2002.9, "2026-07-15T08:00:00Z"))
    return bars


def test_fixture_b_grok_sell_into_support_sweep_must_oppose():
    result = pae.evaluate(_fixture_b_bars(), "sell")
    assert result["verdict"] == "oppose"
    # this fixture is a SINGLE qualifying bar -- the 2-of-3 cluster rule
    # alone would NOT catch it; the sweep_and_reclaim mirror must.
    assert result["features"]["rejection_cluster_lower"]["value"] is False
    sweep = result["features"]["sweep_and_reclaim"]
    assert sweep["value"] is True
    assert sweep["side"] == "buy"
    assert result["features"]["swing_location"]["at_minor_support"] is True


def test_fixture_b_does_not_falsely_oppose_the_other_side():
    result = pae.evaluate(_fixture_b_bars(), "buy")
    assert result["verdict"] == "neutral"


# ---------------------------------------------------------------------------
# single-bar classification (trend bar / doji / pin bar / exhaustion)
# ---------------------------------------------------------------------------


def test_classify_bar_trend_bar():
    cfg = pae.PriceActionEyeConfig()
    anatomy = pae._classify_bar({"open": 100, "high": 110, "low": 99, "close": 109}, atr_ref=1.0, cfg=cfg)
    assert anatomy["is_trend_bar"] is True
    assert anatomy["trend_direction"] == "buy"
    assert anatomy["body_frac"] >= cfg.trend_body_frac_min


def test_classify_bar_doji():
    cfg = pae.PriceActionEyeConfig()
    anatomy = pae._classify_bar({"open": 100, "high": 102, "low": 98, "close": 100.5}, atr_ref=1.0, cfg=cfg)
    assert anatomy["is_doji"] is True
    assert anatomy["is_trend_bar"] is False


def test_classify_bar_hammer_and_shooting_star():
    cfg = pae.PriceActionEyeConfig()
    hammer = pae._classify_bar({"open": 100, "high": 100.7, "low": 95, "close": 100.5}, atr_ref=1.0, cfg=cfg)
    assert hammer["is_hammer"] is True
    assert hammer["is_shooting_star"] is False

    star = pae._classify_bar({"open": 100, "high": 105, "low": 99.3, "close": 99.5}, atr_ref=1.0, cfg=cfg)
    assert star["is_shooting_star"] is True
    assert star["is_hammer"] is False


def test_classify_bar_exhaustion_vs_normal_range():
    cfg = pae.PriceActionEyeConfig()
    exhaustion = pae._classify_bar({"open": 100, "high": 103, "low": 100, "close": 101}, atr_ref=1.0, cfg=cfg)
    assert exhaustion["is_exhaustion"] is True  # range=3.0 >= 2.0x atr_ref=1.0

    normal = pae._classify_bar({"open": 100, "high": 101, "low": 100, "close": 100.6}, atr_ref=1.0, cfg=cfg)
    assert normal["is_exhaustion"] is False  # range=1.0 < 2.0x atr_ref=1.0


# ---------------------------------------------------------------------------
# always_in + support verdict on a clean synthetic trend
# ---------------------------------------------------------------------------


def _clean_bull_trend_bars(n: int = 12) -> list[dict[str, Any]]:
    bars = []
    price = 2000.0
    for i in range(n):
        bars.append(_bar(price - 0.2, price + 2.5, price - 0.5, price + 2.2, f"2026-07-15T{i:02d}:00:00Z"))
        price += 3.0
    return bars


def test_always_in_long_on_clean_bull_trend():
    result = pae.evaluate(_clean_bull_trend_bars(), "buy")
    assert result["features"]["always_in"]["value"] == "long"
    assert result["features"]["always_in"]["bull_trend_bars"] > result["features"]["always_in"]["bear_trend_bars"]


def test_support_verdict_on_trend_with_follow_through():
    result = pae.evaluate(_clean_bull_trend_bars(), "buy")
    assert result["verdict"] == "support"
    assert result["features"]["trend_follow_through"]["value"] is True
    assert result["features"]["trend_follow_through"]["direction"] == "buy"


def test_no_support_verdict_against_the_trend():
    result = pae.evaluate(_clean_bull_trend_bars(), "sell")
    assert result["verdict"] != "support"


# ---------------------------------------------------------------------------
# neutral on chop
# ---------------------------------------------------------------------------


def _chop_bars(n: int = 12) -> list[dict[str, Any]]:
    bars = []
    for i in range(n):
        px = 2000 + (3 if i % 2 == 0 else -3)
        bars.append(_bar(px, px + 5, px - 5, px + (1 if i % 2 == 0 else -1), f"2026-07-15T{i:02d}:00:00Z"))
    return bars


def test_neutral_on_chop_both_sides():
    chop = _chop_bars()
    assert pae.evaluate(chop, "buy")["verdict"] == "neutral"
    assert pae.evaluate(chop, "sell")["verdict"] == "neutral"


# ---------------------------------------------------------------------------
# guard rails: invalid side / insufficient bars never raise
# ---------------------------------------------------------------------------


def test_evaluate_rejects_invalid_side_without_raising():
    result = pae.evaluate(_clean_bull_trend_bars(), "hold")
    assert result["verdict"] == "neutral"
    assert result["features"]["error"].startswith("invalid_side")


def test_evaluate_insufficient_bars_without_raising():
    result = pae.evaluate(_clean_bull_trend_bars()[:2], "buy")
    assert result["verdict"] == "neutral"
    assert result["features"]["error"] == "insufficient_bars"


# ---------------------------------------------------------------------------
# env override changes a threshold
# ---------------------------------------------------------------------------


def _borderline_rejection_bars() -> list[dict[str, Any]]:
    """Three identical bars with upper_wick_frac == 4/9 ~= 0.444 -- BELOW the
    default wick_frac_min (0.5) but ABOVE a lowered override (0.4)."""
    bars = []
    for j in range(3):
        base = 2016.5 - j * 0.05
        bars.append(_bar(base, base + 4, base - 5, base - 1, f"2026-07-15T{7 + j:02d}:00:00Z"))
    return bars


def test_rejection_cluster_threshold_is_env_tunable_via_config():
    bars = _borderline_rejection_bars()
    default_cfg = pae.PriceActionEyeConfig()
    lowered_cfg = pae.PriceActionEyeConfig(wick_frac_min=0.4)

    default_cluster = pae._rejection_cluster(bars, atr_ref=1.0, cfg=default_cfg, wick_side="upper")
    lowered_cluster = pae._rejection_cluster(bars, atr_ref=1.0, cfg=lowered_cfg, wick_side="upper")

    assert default_cluster["value"] is False  # 0.444 < default 0.5 -> no bar qualifies
    assert lowered_cluster["value"] is True  # 0.444 >= lowered 0.4 -> all 3 qualify


def test_pa_eye_config_from_env_reads_override_and_ignores_invalid(monkeypatch):
    monkeypatch.setenv("DEXTER3_PA_EYE_WICK_FRAC_MIN", "0.35")
    cfg = sr._pa_eye_config_from_env()
    assert cfg.wick_frac_min == pytest.approx(0.35)

    monkeypatch.setenv("DEXTER3_PA_EYE_WICK_FRAC_MIN", "not_a_float")
    cfg2 = sr._pa_eye_config_from_env()
    assert cfg2.wick_frac_min == pae.PriceActionEyeConfig().wick_frac_min  # falls back to default, does not raise


# ---------------------------------------------------------------------------
# shadow wiring -- _apply_pa_eye_shadow unit tests
# ---------------------------------------------------------------------------


def _fable_decision(**kw: Any) -> Decision:
    base = dict(
        ts_close="2026-07-15T09:00:00Z", symbol="XAUUSD", action="enter", side="buy",
        entry_type="market", entry=2000.0, sl=1995.0, tp=2010.0, size_class="scout",
        leader_score=0.5, p_win_est=0.5, setup="test_setup", reasons=["test"], features={},
    )
    base.update(kw)
    return Decision(**base)


@pytest.fixture(autouse=True)
def _silence_log_line(monkeypatch):
    """Every test in this module that may touch shadow_runner must never
    write to the real data/runtime/*.log files."""
    monkeypatch.setattr(sr, "log_line", lambda *_a, **_kw: None)


@pytest.fixture(autouse=True)
def _reset_pa_eye_env_and_warn_cache(monkeypatch):
    monkeypatch.delenv("DEXTER3_PA_EYE", raising=False)
    monkeypatch.delenv("DEXTER3_HUNT", raising=False)
    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    sr._PA_EYE_MODE_WARNED.clear()
    yield
    sr._PA_EYE_MODE_WARNED.clear()


def test_apply_pa_eye_shadow_off_by_default_does_nothing():
    d = _fable_decision()
    sr._apply_pa_eye_shadow(d, _clean_bull_trend_bars())
    assert d.features == {}


def test_apply_pa_eye_shadow_stamps_features_in_shadow_mode(monkeypatch):
    monkeypatch.setenv("DEXTER3_PA_EYE", "shadow")
    d = _fable_decision()
    sr._apply_pa_eye_shadow(d, _clean_bull_trend_bars())
    assert "pa_eye" in d.features
    assert d.features["pa_eye"]["verdict"] == "support"


def test_apply_pa_eye_shadow_unrecognized_mode_runs_shadow_with_one_time_warning(monkeypatch):
    monkeypatch.setenv("DEXTER3_PA_EYE", "veto")
    d1 = _fable_decision()
    sr._apply_pa_eye_shadow(d1, _clean_bull_trend_bars())
    assert "pa_eye" in d1.features  # unrecognized mode still journals (treated as shadow)
    assert "veto" in sr._PA_EYE_MODE_WARNED

    # a second unrecognized-mode call must not re-add / re-warn (one-time cache) --
    # exercised implicitly by not raising and the set staying a singleton.
    d2 = _fable_decision()
    sr._apply_pa_eye_shadow(d2, _clean_bull_trend_bars())
    assert sr._PA_EYE_MODE_WARNED == {"veto"}


def test_apply_pa_eye_shadow_fails_open_on_evaluate_exception(monkeypatch):
    monkeypatch.setenv("DEXTER3_PA_EYE", "shadow")

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("boom-eye")

    monkeypatch.setattr(sr.price_action_eye, "evaluate", _boom)
    d = _fable_decision()
    sr._apply_pa_eye_shadow(d, _clean_bull_trend_bars())  # must NOT raise
    assert d.features["pa_eye"] == {"error": "boom-eye"}


def test_apply_pa_eye_shadow_never_touches_non_dict_features(monkeypatch):
    """Decision-shaped objects with a non-dict ``features`` attribute (e.g. a
    minimal test double) must not crash the stamp -- it just silently skips
    the assignment rather than raising."""
    monkeypatch.setenv("DEXTER3_PA_EYE", "shadow")

    class _Bare:
        symbol = "XAUUSD"
        side = "buy"
        features = None

    d = _Bare()
    sr._apply_pa_eye_shadow(d, _clean_bull_trend_bars())  # must not raise
    assert d.features is None


# ---------------------------------------------------------------------------
# shadow wiring -- end-to-end through run_symbol_cycle (HUNT mode always
# enters once bars_m5 >= 60, so this reliably exercises the "enter" path)
# ---------------------------------------------------------------------------


class _HuntFakeMcp:
    def __init__(self, m5_bars: list[dict[str, Any]]) -> None:
        self._m5 = list(m5_bars)
        self.spot = {"bid": 2000.0, "ask": 2000.5}
        self.calls: list[str] = []

    def get_trendbars(self, symbol: str, period: str, count: int) -> list[dict[str, Any]]:
        self.calls.append(period)
        return list(self._m5) if period == "m5" else []

    def get_spot_price(self, symbol: str) -> dict[str, Any]:
        return dict(self.spot)


def _m5_bars_for_hunt(n: int = 65) -> list[dict[str, Any]]:
    """Far-past timestamps (year 2020) so shadow_runner.fetch_fresh_m5's
    freshness-retry logic sees a gap far beyond FRESHNESS_MAX_GAP_SEC and
    returns on the first read -- no retry/sleep needed in the test."""
    bars = []
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    price = 2000.0
    for i in range(n):
        ts = (base + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        c = price + (0.5 if i % 2 == 0 else -0.3)
        h = max(price, c) + 0.8
        l = min(price, c) - 0.8
        bars.append(_bar(price, h, l, c, ts))
        price = c
    return bars


@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "pa_eye_wiring_journal.db")
    yield j
    j.close()


def test_shadow_wiring_journals_pa_eye_on_enter_decision(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_HUNT", "1")
    monkeypatch.setenv("DEXTER3_PA_EYE", "shadow")
    mcp = _HuntFakeMcp(_m5_bars_for_hunt())

    status = sr.run_symbol_cycle(mcp, journal, {}, {}, "XAUUSD", executor=None)

    assert status.startswith("decided:enter")
    rows = journal.recent_decisions(limit=5)
    assert rows
    row = rows[0]
    assert row["action"] == "enter"
    assert "pa_eye" in row["features"]
    assert row["features"]["pa_eye"]["verdict"] in ("support", "neutral", "oppose")
    assert isinstance(row["features"]["pa_eye"]["verdict_reasons"], list)


def test_pa_eye_shadow_does_not_change_trade_flow(monkeypatch, tmp_path):
    """off vs shadow must produce the IDENTICAL entry/side/sl/tp/setup --
    the Eye is powerless in Phase A, only the journaled features differ."""
    monkeypatch.setenv("DEXTER3_HUNT", "1")
    bars = _m5_bars_for_hunt()

    monkeypatch.setenv("DEXTER3_PA_EYE", "off")
    j_off = DecisionJournal(tmp_path / "off.db")
    try:
        sr.run_symbol_cycle(_HuntFakeMcp(bars), j_off, {}, {}, "XAUUSD", executor=None)
        row_off = j_off.recent_decisions(limit=1)[0]
    finally:
        j_off.close()

    monkeypatch.setenv("DEXTER3_PA_EYE", "shadow")
    j_shadow = DecisionJournal(tmp_path / "shadow.db")
    try:
        sr.run_symbol_cycle(_HuntFakeMcp(bars), j_shadow, {}, {}, "XAUUSD", executor=None)
        row_shadow = j_shadow.recent_decisions(limit=1)[0]
    finally:
        j_shadow.close()

    for key in ("action", "side", "entry", "sl", "tp", "setup", "leader_score", "p_win_est"):
        assert row_off[key] == row_shadow[key], f"{key} differed between off and shadow"
    assert "pa_eye" not in row_off["features"]
    assert "pa_eye" in row_shadow["features"]


def test_shadow_wiring_fails_open_on_evaluate_exception(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_HUNT", "1")
    monkeypatch.setenv("DEXTER3_PA_EYE", "shadow")

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("boom-eye-e2e")

    monkeypatch.setattr(sr.price_action_eye, "evaluate", _boom)
    mcp = _HuntFakeMcp(_m5_bars_for_hunt())

    status = sr.run_symbol_cycle(mcp, journal, {}, {}, "XAUUSD", executor=None)

    assert status.startswith("decided:enter")  # the decision itself is unaffected
    row = journal.recent_decisions(limit=1)[0]
    assert row["features"]["pa_eye"] == {"error": "boom-eye-e2e"}
