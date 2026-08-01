"""Unit tests for dexter3/sniper.py — the SHADOWCODES [PRO SNIPER] producer
(owner directive 2026-07-30: S/D zones + ST2 wick rejection + QM sweep
reclaim, live-test lane for XAUUSD and USTEC).

Pure module, synthetic bars only. Pins: zone birth (flip+base+impulse
triplet), mitigation-by-close, age expiry, ST2 in-zone wick trigger +
geometry (SL buffer, fixed 2R TP), QM sweep trigger + sidedness, the
max-risk-ATR guard, graceful skips, mode flag off-by-default, the
per-symbol label fork (DEXTER3_SNIPER_SUFFIX), and shadow_runner isolation.
"""
from __future__ import annotations

import importlib

import pytest

from dexter3 import sniper as sn


def _bar(ts: str, o: float, h: float, l: float, c: float, v: float = 30.0) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _ts(i: int) -> str:
    return f"2026-07-30T{i // 12:02d}:{(i % 12) * 5:02d}:00Z"


def _quiet(n: int, base: float = 4004.0, start: int = 0) -> list[dict]:
    """n low-volatility bars (range 2.0, alternating small red/green bodies)
    — enough TR history for a non-zero ATR without forming any zone (no bar
    has body > 0.6 x range, so the impulse gate can never pass)."""
    bars = []
    for i in range(n):
        up = i % 2 == 0
        o = base + (0.2 if up else 0.8)
        c = base + (0.8 if up else 0.2)
        bars.append(_bar(_ts(start + i), o, base + 1.6, base - 0.4, c))
    return bars


def _demand_triplet(start: int, zone_lo: float = 4000.0, zone_hi: float = 4002.0) -> list[dict]:
    """flip (red) -> base (tiny body inside [zone_lo, zone_hi]) -> impulse up
    (body > 0.6 x range, range > the quiet-regime ATR of ~2)."""
    flip = _bar(_ts(start), 4004.0, 4004.5, 4002.0, 4002.3)          # red
    base = _bar(_ts(start + 1), zone_lo + 1.0, zone_hi, zone_lo, zone_lo + 1.2)  # body 0.2 < 0.3*2.0
    impulse = _bar(_ts(start + 2), 4001.2, 4005.6, 4001.0, 4005.4)   # range 4.6, body 4.2, green
    return [flip, base, impulse]


def _drift_above(n: int, start: int, base: float = 4004.5) -> list[dict]:
    """bars that stay ABOVE the demand zone (never close < zone_lo)."""
    return _quiet(n, base=base, start=start)


# -- zone engine --------------------------------------------------------------


def test_demand_zone_born_from_flip_base_impulse():
    bars = _quiet(20) + _demand_triplet(20)
    zones = sn.build_zones(bars)
    demand = [z for z in zones if z["side"] == "demand"]
    assert len(demand) == 1
    assert demand[0]["bottom"] == pytest.approx(4000.0)
    assert demand[0]["top"] == pytest.approx(4002.0)


def test_supply_zone_mirror():
    bars = _quiet(20)
    flip = _bar(_ts(20), 4004.0, 4006.0, 4003.8, 4005.8)              # green
    base = _bar(_ts(21), 4007.0, 4008.0, 4006.0, 4006.8)              # body 0.2 < 0.3*2.0
    impulse = _bar(_ts(22), 4006.6, 4006.8, 4002.2, 4002.4)           # red, range 4.6, body 4.2
    zones = sn.build_zones(bars + [flip, base, impulse])
    supply = [z for z in zones if z["side"] == "supply"]
    assert len(supply) == 1
    assert supply[0]["top"] == pytest.approx(4008.0)
    assert supply[0]["bottom"] == pytest.approx(4006.0)


def test_zone_mitigated_by_close_through():
    bars = _quiet(20) + _demand_triplet(20)
    # a later bar CLOSES below the zone bottom -> zone must die
    bars.append(_bar(_ts(23), 4004.0, 4004.2, 3998.0, 3999.0))
    assert [z for z in sn.build_zones(bars) if z["side"] == "demand"] == []


def test_zone_wick_through_does_not_mitigate():
    bars = _quiet(20) + _demand_triplet(20)
    # wick pierces below the bottom but CLOSE holds inside/above -> zone lives
    bars.append(_bar(_ts(23), 4004.0, 4004.2, 3999.2, 4001.5))
    assert len([z for z in sn.build_zones(bars) if z["side"] == "demand"]) == 1


def test_zone_age_expiry(monkeypatch):
    monkeypatch.setenv(sn.ENV_ZONE_MAX_AGE, "5")
    bars = _quiet(20) + _demand_triplet(20) + _drift_above(8, start=23)
    assert [z for z in sn.build_zones(bars) if z["side"] == "demand"] == []


# -- ST2 wick rejection in-zone ------------------------------------------------


def _st2_setup() -> list[dict]:
    """demand zone at [4000, 4002], price drifts above, then the signal bar
    dips its LOW into the zone with a dominant lower wick and tiny body."""
    bars = _quiet(20) + _demand_triplet(20) + _drift_above(6, start=23)
    bars.append(_bar(_ts(29), 4004.5, 4004.6, 4001.5, 4004.4))  # dn_wick 2.9, body 0.1, up_wick 0.2
    return bars


def test_st2_buy_enters_market_with_2r_tp():
    d = sn.decide_sniper("XAUUSD", _st2_setup(), spread_abs=0.2)
    assert d.action == "enter"
    assert d.side == "buy"
    assert d.setup == "sniper_st2_zone"
    assert d.entry_type == "market"
    assert d.entry == pytest.approx(4004.4)
    assert d.sl < 4001.5                       # signal low minus the ATR buffer
    risk = d.entry - d.sl
    assert d.tp == pytest.approx(d.entry + 2.0 * risk, abs=1e-4)


def test_st2_needs_zone_touch():
    # same wicky bar but its low stays ABOVE the zone -> no signal
    bars = _quiet(20) + _demand_triplet(20) + _drift_above(6, start=23)
    bars.append(_bar(_ts(29), 4004.5, 4004.6, 4002.5, 4004.4))
    d = sn.decide_sniper("XAUUSD", bars, spread_abs=0.2)
    assert d.action == "skip"


def test_st2_needs_wick_dominance(monkeypatch):
    # low touches the zone but the bar is a fat body, not a rejection wick
    bars = _quiet(20) + _demand_triplet(20) + _drift_above(6, start=23)
    bars.append(_bar(_ts(29), 4004.5, 4004.6, 4001.5, 4001.8))  # body 2.7 >> wick
    d = sn.decide_sniper("XAUUSD", bars, spread_abs=0.2)
    assert d.action == "skip"


def test_max_risk_atr_guard(monkeypatch):
    monkeypatch.setenv(sn.ENV_MAX_RISK_ATR, "0.5")   # absurdly tight cap
    d = sn.decide_sniper("XAUUSD", _st2_setup(), spread_abs=0.2)
    assert d.action == "skip"
    assert any("st2_risk" in r for r in d.reasons)


def test_tp_r_env_override(monkeypatch):
    monkeypatch.setenv(sn.ENV_TP_R, "3.0")
    d = sn.decide_sniper("XAUUSD", _st2_setup(), spread_abs=0.2)
    assert d.action == "enter"
    risk = d.entry - d.sl
    assert d.tp == pytest.approx(d.entry + 3.0 * risk, abs=1e-4)


# -- QM sweep reclaim ----------------------------------------------------------


def test_qm_sell_on_swept_high():
    bars = _quiet(24)
    hi_prev = max(b["high"] for b in bars)  # 4005.6
    prev = _bar(_ts(24), 4004.6, 4005.0, 4003.9, 4004.2)
    bars.append(prev)
    # takes out the prior extreme, closes red BELOW it and below prev low
    bars.append(_bar(_ts(25), 4004.8, hi_prev + 1.2, 4003.2, 4003.5))
    d = sn.decide_sniper("USTEC", bars, spread_abs=0.5)
    assert d.action == "enter"
    assert d.side == "sell"
    assert d.setup == "sniper_qm_sweep"
    assert d.sl > hi_prev + 1.2                # sweep high plus the ATR buffer
    risk = d.sl - d.entry
    assert d.tp == pytest.approx(d.entry - 2.0 * risk, abs=1e-4)


def test_qm_buy_on_swept_low():
    bars = _quiet(24)
    lo_prev = min(b["low"] for b in bars)      # 4003.6
    prev = _bar(_ts(24), 4004.4, 4005.0, 4004.0, 4004.6)
    bars.append(prev)
    bars.append(_bar(_ts(25), 4004.2, 4005.4, lo_prev - 1.2, 4005.2))
    d = sn.decide_sniper("USTEC", bars, spread_abs=0.5)
    assert d.action == "enter"
    assert d.side == "buy"
    assert d.setup == "sniper_qm_sweep"


def test_sweep_without_displacement_skips():
    bars = _quiet(24)
    hi_prev = max(b["high"] for b in bars)
    prev = _bar(_ts(24), 4004.6, 4005.0, 4003.9, 4004.2)
    bars.append(prev)
    # sweeps and closes back below the extreme, but NOT below the prev low
    bars.append(_bar(_ts(25), 4004.8, hi_prev + 1.2, 4004.0, 4004.1))
    d = sn.decide_sniper("USTEC", bars, spread_abs=0.5)
    assert d.action == "skip"


# -- hygiene -------------------------------------------------------------------


def test_insufficient_bars_skips():
    d = sn.decide_sniper("XAUUSD", _quiet(10), spread_abs=0.2)
    assert d.action == "skip"
    assert "bars<" in d.reasons[0]


def test_no_setup_skips():
    d = sn.decide_sniper("XAUUSD", _quiet(40), spread_abs=0.2)
    assert d.action == "skip"
    assert "no_setup" in d.reasons[0]


def test_mode_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert sn.sniper_mode_enabled() is False


def test_mode_flag_on_via_mode_or_producer(monkeypatch):
    monkeypatch.setenv("DEXTER3_MODE", "sniper")
    assert sn.sniper_mode_enabled() is True
    monkeypatch.delenv("DEXTER3_MODE")
    monkeypatch.setenv("DEXTER3_PRODUCER", "sniper")
    assert sn.sniper_mode_enabled() is True


# -- label isolation -----------------------------------------------------------


def test_label_constants_are_isolated():
    assert sn.SNIPER_LABEL == "dexter3:sniper:canary"
    assert sn.SNIPER_LABEL_FAMILY == "dexter3:sniper"
    from dexter3.executor import label_matches_family

    assert label_matches_family(sn.SNIPER_LABEL, "dexter3:sniper") is True
    assert label_matches_family(sn.SNIPER_LABEL, "dexter3:fable") is False
    assert label_matches_family("dexter3:fable:v1.8", "dexter3:sniper") is False


def test_suffix_forks_a_separate_family(monkeypatch):
    """The USTEC unit (DEXTER3_SNIPER_SUFFIX=ustec) must be a DIFFERENT label
    family from the XAUUSD unit — otherwise each process's governor would
    count the other symbol's realized PnL as its own day."""
    from dexter3.executor import label_matches_family

    monkeypatch.setenv("DEXTER3_SNIPER_SUFFIX", "ustec")
    importlib.reload(sn)
    try:
        assert sn.SNIPER_LABEL == "dexter3:sniper-ustec:canary"
        assert sn.SNIPER_LABEL_FAMILY == "dexter3:sniper-ustec"
        assert label_matches_family(sn.SNIPER_LABEL, "dexter3:sniper") is False
        assert label_matches_family(sn.SNIPER_LABEL, "dexter3:sniper-ustec") is True
    finally:
        monkeypatch.delenv("DEXTER3_SNIPER_SUFFIX")
        importlib.reload(sn)
    assert sn.SNIPER_LABEL == "dexter3:sniper:canary"


def test_shadow_runner_resolvers_isolate_sniper(monkeypatch):
    import dexter3.shadow_runner as sr

    monkeypatch.setenv("DEXTER3_MODE", "sniper")
    assert sr._active_order_label() == "dexter3:sniper:canary"
    assert sr._active_label_family() == "dexter3:sniper"
    assert sr._active_state_file().name == "dexter3_sniper_shadow_state.json"
    assert sr._active_log_file().name == "dexter3_sniper_shadow.log"
    assert sr._sniper_producer_enabled() is True
    assert sr._alt_producer_enabled() is True
    monkeypatch.setenv("DEXTER3_MODE", "v16")
    assert sr._sniper_producer_enabled() is False


def test_index_symbols_have_usd_point_value():
    """Without a table entry `_enrich_positions_with_live_pnl` skips the
    symbol -> no netProfit -> basket unreliable -> OM holds forever (observed
    live on the first US30 sniper position, 2026-07-29). Pin the index
    entries so a refactor can never silently blind the index lanes again."""
    from dexter3.openapi_client import _USD_POINT_VALUE_PER_UNIT as tbl

    for sym in ("XAUUSD", "USTEC", "US30", "US500"):
        assert tbl.get(sym) == 1.0, sym


# -- Phase-3 lever: zone-to-zone TP (DEXTER3_SNIPER_TP_MODE) --------------------


def _st2_with_supply_overhead() -> list[dict]:
    """demand zone at [4000,4002] + a SUPPLY zone overhead at [4006,4008]
    (green flip -> base at 4006-4008 -> red impulse), then the ST2 buy signal
    bar. The buy's zone target must be the supply BOTTOM (4006.0)."""
    bars = _quiet(20) + _demand_triplet(20)
    bars.append(_bar(_ts(23), 4004.0, 4006.0, 4003.8, 4005.8))          # green flip
    bars.append(_bar(_ts(24), 4007.0, 4008.0, 4006.0, 4006.8))          # base (supply zone)
    bars.append(_bar(_ts(25), 4006.6, 4006.8, 4002.2, 4002.4))          # red impulse
    bars += _drift_above(3, start=26, base=4004.0)
    bars.append(_bar(_ts(29), 4004.5, 4004.6, 4001.5, 4004.4))          # ST2 buy signal
    return bars


def test_tp_mode_default_rr_unchanged(monkeypatch):
    monkeypatch.delenv(sn.ENV_TP_MODE, raising=False)
    d = sn.decide_sniper("XAUUSD", _st2_with_supply_overhead(), spread_abs=0.2)
    assert d.action == "enter"
    risk = d.entry - d.sl
    assert d.tp == pytest.approx(d.entry + 2.0 * risk, abs=1e-4)


def test_tp_mode_zone_targets_opposing_zone(monkeypatch):
    monkeypatch.setenv(sn.ENV_TP_MODE, "zone")
    d = sn.decide_sniper("XAUUSD", _st2_with_supply_overhead(), spread_abs=0.2)
    assert d.action == "enter"
    assert d.tp == pytest.approx(4006.0)          # the supply zone's bottom
    assert any("zone target" in r for r in d.reasons)


def test_tp_mode_zone_falls_back_to_rr_without_opposing_zone(monkeypatch):
    monkeypatch.setenv(sn.ENV_TP_MODE, "zone")
    d = sn.decide_sniper("XAUUSD", _st2_setup(), spread_abs=0.2)  # no supply zone
    assert d.action == "enter"
    risk = d.entry - d.sl
    assert d.tp == pytest.approx(d.entry + 2.0 * risk, abs=1e-4)


def test_tp_mode_zone_minrr1_skips_thin_target(monkeypatch):
    """Zone target closer than 1R -> the framework says no trade."""
    monkeypatch.setenv(sn.ENV_TP_MODE, "zone-minrr1")
    bars = _st2_with_supply_overhead()
    # signal bar with a DEEP low -> risk ~3+ pts while the zone target is
    # ~1.6 pts away -> < 1R -> skip
    bars[-1] = _bar(_ts(29), 4004.5, 4004.6, 4000.2, 4004.4)
    d = sn.decide_sniper("XAUUSD", bars, spread_abs=0.2)
    assert d.action == "skip"
