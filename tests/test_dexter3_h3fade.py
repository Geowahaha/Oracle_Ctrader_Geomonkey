"""Pins dexter3/h3fade.py — the pre-registered M1 streak-fade lane
(docs/handoff/H3_BUILD_BRIEF.md 2026-08-05). Trigger/sidedness/hours/ATR,
geometry, off-by-default flag, runner isolation, and the M1-close bank /
time exit decision."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dexter3 import h3fade as h3


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _ts(i: int, start_hour: int = 13) -> str:
    """M1 bar ts label i minutes after start_hour:00Z."""
    return f"2026-08-05T{start_hour + i // 60:02d}:{i % 60:02d}:00Z"


def _bar(ts, o, h, l, c, v=10.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _base(n, start_px=3300.0, start_hour=13, step=0.2):
    """Drifting M1 bars with CONSTANT true range 1.0 -> ATR1(RMA) == 1.0
    exactly, so trigger thresholds are analytic. Per-bar move +step keeps the
    base itself below the 2xATR1 streak-move gate (5 x 0.2 = 1.0 < 2.0)."""
    bars, px = [], start_px
    for i in range(n):
        o, c = px, px + step
        bars.append(_bar(_ts(i, start_hour), o, c + (1.0 - step) / 2, o - (1.0 - step) / 2, c))
        px = c
    return bars


def _streak(bars, k=5, delta=0.5, start_hour=13):
    """Append k same-direction closes of |delta| each, still TR == 1.0."""
    px = float(bars[-1]["close"])
    n = len(bars)
    for j in range(k):
        o, c = px, px + delta
        hi = max(o, c) + (1.0 - abs(delta)) / 2
        lo = min(o, c) - (1.0 - abs(delta)) / 2
        bars.append(_bar(_ts(n + j, start_hour), o, hi, lo, c))
        px = c
    return bars


# ---------------------------------------------------------------------------
# producer: trigger / sidedness / hours / ATR / geometry
# ---------------------------------------------------------------------------


def test_fades_up_streak_with_sell():
    bars = _streak(_base(30), k=5, delta=0.5)  # move 2.5 >= 2.0 x ATR1(1.0)
    d = h3.decide_h3fade("XAUUSD", bars, spread_abs=0.3)
    assert d.action == "enter"
    assert d.side == "sell"
    assert d.setup == "h3fade_streak"
    assert d.entry_type == "market"
    assert d.entry == pytest.approx(float(bars[-1]["close"]), abs=1e-6)
    risk = d.sl - d.entry  # sell: SL above entry
    assert risk == pytest.approx(2.0, abs=0.05)      # 2.0 x ATR1, ATR1 == 1.0
    assert d.tp == pytest.approx(d.entry - 3.0 * risk, abs=1e-3)


def test_fades_down_streak_with_buy():
    bars = _streak(_base(30), k=5, delta=-0.5)
    d = h3.decide_h3fade("XAUUSD", bars, spread_abs=0.3)
    assert d.action == "enter"
    assert d.side == "buy"
    risk = d.entry - d.sl
    assert risk == pytest.approx(2.0, abs=0.05)
    assert d.tp == pytest.approx(d.entry + 3.0 * risk, abs=1e-3)


def test_skips_broken_streak():
    bars = _streak(_base(30), k=4, delta=0.6)
    bars = _streak(bars, k=1, delta=-0.6)  # 5th delta flips -> no streak
    d = h3.decide_h3fade("XAUUSD", bars, spread_abs=0.3)
    assert d.action == "skip"
    assert "no_streak" in d.reasons[0]


def test_skips_move_below_2x_atr():
    bars = _streak(_base(30), k=5, delta=0.3)  # move 1.5 < 2.0 x ATR1
    d = h3.decide_h3fade("XAUUSD", bars, spread_abs=0.3)
    assert d.action == "skip"
    assert "move_too_small" in d.reasons[0]


def test_skips_outside_hours():
    bars = _streak(_base(30, start_hour=10), k=5, delta=0.5, start_hour=10)
    d = h3.decide_h3fade("XAUUSD", bars, spread_abs=0.3)
    assert d.action == "skip"
    assert "outside_hours" in d.reasons[0]


def test_hour_window_is_13_to_16_inclusive():
    # trigger bar labeled 16:59Z -> IN window; 17:00Z -> out.
    in_bars = _streak(_base(30, start_hour=16, step=0.1), k=5, delta=0.5, start_hour=16)
    assert in_bars[-1]["ts"].startswith("2026-08-05T16:")
    assert h3.decide_h3fade("XAUUSD", in_bars, 0.3).action == "enter"
    out_bars = _streak(_base(56, start_hour=16, step=0.1), k=5, delta=0.5, start_hour=16)
    assert out_bars[-1]["ts"].startswith("2026-08-05T17:")
    d = h3.decide_h3fade("XAUUSD", out_bars, 0.3)
    assert d.action == "skip" and "outside_hours" in d.reasons[0]


def test_skips_on_insufficient_bars():
    bars = _streak(_base(8), k=5, delta=0.5)  # 13 < min_m1_bars() == 16
    d = h3.decide_h3fade("XAUUSD", bars, spread_abs=0.3)
    assert d.action == "skip"
    assert "bars<" in d.reasons[0]


def test_atr_rma_constant_tr_is_exact():
    bars = _base(30)
    assert h3._atr_rma(bars, 14) == pytest.approx(1.0, abs=1e-9)


def test_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert h3.h3fade_mode_enabled() is False
    monkeypatch.setenv("DEXTER3_MODE", "h3fade")
    assert h3.h3fade_mode_enabled() is True


def test_label_isolation_and_runner_resolvers(monkeypatch):
    from dexter3.executor import label_matches_family

    assert h3.H3FADE_LABEL == "dexter3:h3fade:canary"
    assert label_matches_family(h3.H3FADE_LABEL, "dexter3:h3fade") is True
    assert label_matches_family(h3.H3FADE_LABEL, "dexter3:scalp") is False
    assert label_matches_family("dexter3:scalp:canary", "dexter3:h3fade") is False

    import dexter3.shadow_runner as sr
    monkeypatch.setenv("DEXTER3_MODE", "h3fade")
    assert sr._active_order_label() == "dexter3:h3fade:canary"
    assert sr._active_label_family() == "dexter3:h3fade"
    assert sr._active_state_file().name == "dexter3_h3fade_shadow_state.json"
    assert sr._active_log_file().name == "dexter3_h3fade_shadow.log"
    assert sr._h3fade_producer_enabled() is True
    assert sr._alt_producer_enabled() is True


def test_exit_branch_env_gated_off_by_default(monkeypatch):
    import dexter3.shadow_runner as sr

    monkeypatch.delenv("DEXTER3_H3FADE_EXIT", raising=False)
    assert sr._h3fade_exit_enabled() is False
    monkeypatch.setenv("DEXTER3_H3FADE_EXIT", "1")
    assert sr._h3fade_exit_enabled() is True


def test_lane_tally_family():
    from ops.dexter3_lane_tally import _lane_family

    assert _lane_family("dexter3:h3fade:canary") == "h3fade"


# ---------------------------------------------------------------------------
# M1-close bank / time exit decision (pure half of _run_h3fade_exit_tick)
# ---------------------------------------------------------------------------


def _pos(side="SELL", entry=100.0, sl=102.0, open_ts="2026-08-05T13:10:00Z", pid=7):
    return {"positionId": pid, "tradeSide": side, "entryPrice": entry,
            "stopLoss": sl, "openTimestamp": open_ts}


def _m1(ts, close):
    return {"ts": ts, "open": close, "high": close + 0.2, "low": close - 0.2,
            "close": close}


def test_exit_banks_on_closed_m1_at_half_r():
    # sell from 100, SL 102 -> risk 2.0; bar closed 98.9 -> +0.55R >= 0.5R
    lane = [_pos()]
    bars = [_m1("2026-08-05T13:11:00Z", 98.9)]
    verdict = h3.h3fade_exit_decision(lane, bars, now_epoch=_epoch("2026-08-05T13:12:05Z"))
    assert verdict is not None
    reason, info = verdict
    assert reason == "h3_bank"
    assert info["r_close"] == pytest.approx(0.55, abs=1e-3)


def test_exit_no_bank_below_half_r_and_no_time_before_4min():
    lane = [_pos()]
    bars = [_m1("2026-08-05T13:11:00Z", 99.5)]  # +0.25R only
    verdict = h3.h3fade_exit_decision(lane, bars, now_epoch=_epoch("2026-08-05T13:12:05Z"))
    assert verdict is None


def test_exit_ignores_bar_closed_before_entry():
    # the stale pre-entry bar sits past the threshold, but live PnL is ~0 —
    # banking on it would realize nothing (spec: FIRST M1 close AFTER entry).
    lane = [_pos(open_ts="2026-08-05T13:12:30Z")]
    bars = [_m1("2026-08-05T13:11:00Z", 98.0)]  # closed 13:12:00 < open
    verdict = h3.h3fade_exit_decision(lane, bars, now_epoch=_epoch("2026-08-05T13:13:00Z"))
    assert verdict is None


def test_exit_time_stop_at_4_minutes():
    lane = [_pos()]
    bars = [_m1("2026-08-05T13:11:00Z", 100.2)]  # underwater — no bank
    early = h3.h3fade_exit_decision(lane, bars, now_epoch=_epoch("2026-08-05T13:13:50Z"))
    assert early is None
    verdict = h3.h3fade_exit_decision(lane, bars, now_epoch=_epoch("2026-08-05T13:14:00Z"))
    assert verdict is not None and verdict[0] == "h3_time"


def test_exit_bank_takes_priority_over_time():
    lane = [_pos()]
    bars = [_m1("2026-08-05T13:15:00Z", 98.5)]
    verdict = h3.h3fade_exit_decision(lane, bars, now_epoch=_epoch("2026-08-05T13:16:10Z"))
    assert verdict is not None and verdict[0] == "h3_bank"


def test_exit_missing_stop_never_banks_but_time_still_fires():
    # missing broker SL -> risk would be the gold price and r ~ 0 forever
    # (2026-07-26 OM bank audit class of bug) — bank disarmed, time stop
    # remains the resolver before the (absent) SL.
    lane = [_pos(sl=0.0)]
    bars = [_m1("2026-08-05T13:11:00Z", 90.0)]
    at_3min = h3.h3fade_exit_decision(lane, bars, now_epoch=_epoch("2026-08-05T13:13:00Z"))
    assert at_3min is None
    at_4min = h3.h3fade_exit_decision(lane, bars, now_epoch=_epoch("2026-08-05T13:14:00Z"))
    assert at_4min is not None and at_4min[0] == "h3_time"


def test_exit_empty_lane_or_no_bars_is_none():
    assert h3.h3fade_exit_decision([], [_m1("2026-08-05T13:11:00Z", 98.0)], 0.0) is None
    lane = [_pos()]
    assert h3.h3fade_exit_decision(lane, [], now_epoch=_epoch("2026-08-05T13:11:00Z")) is None


def test_h3fade_ctx_cache_ttl_and_fail_soft():
    # 2026-08-06 day-1 bug fix: a rate-limited m15/h1 context fetch must
    # never abort an M1 decision minute — cache 5 min, serve stale on error.
    import dexter3.shadow_runner as sr

    class Stub:
        def __init__(self):
            self.calls = 0
            self.fail = False

        def get_trendbars(self, symbol, tf, n):
            self.calls += 1
            if self.fail:
                raise sr.McpClientError("You are being rate limited")
            return [{"ts": f"{tf}-{self.calls}"}]

    stub = Stub()
    sr._H3_CTX_CACHE.clear()
    m15, h1 = sr._h3fade_ctx_bars(stub, "XAUUSD")
    assert stub.calls == 2 and m15 and h1
    m15b, _h1b = sr._h3fade_ctx_bars(stub, "XAUUSD")
    assert stub.calls == 2 and m15b == m15  # within TTL -> cache, no calls
    sr._H3_CTX_CACHE["XAUUSD"]["epoch"] = 0.0  # expire
    stub.fail = True
    m15c, h1c = sr._h3fade_ctx_bars(stub, "XAUUSD")
    assert m15c == m15 and h1c == h1  # stale copy served, no raise
    sr._H3_CTX_CACHE.clear()
    e15, e1 = sr._h3fade_ctx_bars(stub, "XAUUSD")
    assert e15 == [] and e1 == []  # first-cycle failure -> empty, no raise
    sr._H3_CTX_CACHE.clear()
