"""Pins for ops/nfp_shadow.py — the journal-only N4 forward collector.

Pure logic only (is_first_friday + compute_row on synthetic bars); the
daemon call and journal append are exercised in production by the timer.
"""
from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
from nfp_shadow import SPREAD, compute_row, is_first_friday  # noqa: E402


def test_is_first_friday_truth_table():
    assert is_first_friday(date(2026, 8, 7))          # Aug 2026: Fri the 7th
    assert not is_first_friday(date(2026, 8, 14))     # second Friday
    assert not is_first_friday(date(2026, 8, 6))      # a Thursday
    assert is_first_friday(date(2026, 5, 1))          # 1st itself a Friday
    assert not is_first_friday(date(2026, 5, 8))


def _bar(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


def _synthetic_day():
    """20 flat warm-up bars (TR=2 each -> ATR14=2.0 at 11Z) then a known
    12-15Z expansion. Day = 2026-08-07."""
    bars = []
    for k in range(17):                     # 18:00 prev day .. 10:00 -> flat
        hour = (18 + k) % 24
        day = "2026-08-06" if 18 + k < 24 else "2026-08-07"
        bars.append(_bar(f"{day}T{hour:02d}:00:00Z", 100.0, 101.0, 99.0, 100.0))
    bars.append(_bar("2026-08-07T11:00:00Z", 100.0, 101.0, 99.0, 100.0))   # coil bar
    bars.append(_bar("2026-08-07T12:00:00Z", 100.0, 106.0, 100.0, 105.0))  # breakout up
    bars.append(_bar("2026-08-07T13:00:00Z", 105.0, 108.0, 104.0, 107.0))
    bars.append(_bar("2026-08-07T14:00:00Z", 107.0, 109.0, 106.0, 108.0))
    bars.append(_bar("2026-08-07T15:00:00Z", 108.0, 108.5, 107.0, 108.0))
    return bars


def test_compute_row_known_values():
    row = compute_row(_synthetic_day(), "2026-08-07")
    assert row is not None
    assert row["atr11"] == 2.0
    assert row["coil"] == 1.0                          # (101-99)/2
    assert row["r_event"] == 4.5                       # (109-100)/2
    # bracket: prior-2-bar highs/lows are the flat 101/99 -> buy stop 101.30,
    # sell stop 98.70, coil width 2.60; 12Z bar fills the buy at 101.30,
    # TP = 101.30 + 5.20 = 106.50, touched by the 13Z high (108).
    assert row["bracket_side"] == "buy"
    assert row["bracket_pnl"] == round(106.50 - 101.30 - SPREAD, 2)


def test_compute_row_incomplete_window_returns_none():
    bars = _synthetic_day()[:-3]                       # 13/14/15Z missing
    assert compute_row(bars, "2026-08-07") is None
    assert compute_row(_synthetic_day(), "2026-08-08") is None   # wrong day
