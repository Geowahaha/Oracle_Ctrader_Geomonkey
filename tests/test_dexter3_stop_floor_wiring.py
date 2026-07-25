"""Wiring tests for the volatility-normalized SL floor in run_symbol_cycle.

Pins the three properties the live deploy depends on:
  * env OFF (default) -> byte-identical decision geometry;
  * env ON  -> a too-tight stop is widened, RR preserved, and BOTH the new
    geometry and the proof-of-execution stamp reach the journal row;
  * a failure inside the floor can never change the decision or kill a cycle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import dexter3.shadow_runner as sr
from dexter3.decision_journal import DecisionJournal


def _bar(o: float, h: float, l: float, c: float, ts: str) -> dict[str, Any]:
    return {"open": o, "high": h, "low": l, "close": c, "ts": ts}


def _m5(n: int = 65) -> list[dict[str, Any]]:
    bars = []
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    price = 2000.0
    for i in range(n):
        ts = (base + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        c = price + (0.5 if i % 2 == 0 else -0.3)
        bars.append(_bar(price, max(price, c) + 0.8, min(price, c) - 0.8, c, ts))
        price = c
    return bars


class _FakeMcp:
    def __init__(self, bars): self._m5 = list(bars)
    def get_trendbars(self, symbol, period, count):
        return list(self._m5) if period == "m5" else []
    def get_spot_price(self, symbol):
        return {"bid": 2000.0, "ask": 2000.5}
    def get_symbol_details(self, symbol):
        return {}


@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "sl_floor.db")
    yield j
    j.close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(sr, "log_line", lambda *_a, **_k: None)
    monkeypatch.setenv("DEXTER3_HUNT", "1")
    monkeypatch.delenv("DEXTER3_MARKET_STATE_GATE", raising=False)
    yield


def _run(journal, mcp=None):
    sr.run_symbol_cycle(mcp or _FakeMcp(_m5()), journal, {}, {}, "XAUUSD", executor=None)
    rows = journal.recent_decisions(limit=1)
    return rows[0] if rows else None


def test_off_by_default_is_byte_identical(journal, monkeypatch, tmp_path):
    monkeypatch.delenv("DEXTER3_SL_FLOOR_TR_MULT", raising=False)
    row_off = _run(journal)
    assert row_off is not None and row_off["action"] == "enter"
    assert "sl_floor" not in row_off["features"]

    # same inputs, floor explicitly disabled -> identical geometry
    monkeypatch.setenv("DEXTER3_SL_FLOOR_TR_MULT", "0")
    j2 = DecisionJournal(tmp_path / "off2.db")
    try:
        row_zero = _run(j2)
    finally:
        j2.close()
    for k in ("side", "entry", "sl", "tp", "setup"):
        assert row_off[k] == row_zero[k]


def test_on_widens_tight_stop_and_journals_proof(journal, monkeypatch, tmp_path):
    monkeypatch.delenv("DEXTER3_SL_FLOOR_TR_MULT", raising=False)
    j0 = DecisionJournal(tmp_path / "base.db")
    try:
        base = _run(j0)
    finally:
        j0.close()

    # a large multiplier guarantees the floor binds on this fixture
    monkeypatch.setenv("DEXTER3_SL_FLOOR_TR_MULT", "3.0")
    row = _run(journal)

    assert row is not None and row["action"] == "enter"
    meta = row["features"]["sl_floor"]
    assert meta["applied"] is True, "floor must have engaged at mult=3.0"

    base_sl_dist = abs(float(base["entry"]) - float(base["sl"]))
    new_sl_dist = abs(float(row["entry"]) - float(row["sl"]))
    assert new_sl_dist > base_sl_dist              # widened
    assert float(row["entry"]) == float(base["entry"])  # entry untouched

    # RR preserved through the widening
    rr_base = abs(float(base["tp"]) - float(base["entry"])) / base_sl_dist
    rr_new = abs(float(row["tp"]) - float(row["entry"])) / new_sl_dist
    assert rr_new == pytest.approx(rr_base, rel=1e-3)


def test_stamp_present_even_when_floor_does_not_bind(journal, monkeypatch):
    # tiny multiplier -> never binds, but the proof-of-execution stamp must exist
    monkeypatch.setenv("DEXTER3_SL_FLOOR_TR_MULT", "0.01")
    row = _run(journal)
    assert row["features"]["sl_floor"]["applied"] is False
    assert "tr" in row["features"]["sl_floor"]


def test_max_abs_bounds_the_widening(journal, monkeypatch, tmp_path):
    # Establish the producer's own SL distance first, then set the cap just
    # ABOVE it — the cap must bind (widen to exactly the cap) rather than the
    # absurd 50xTR floor. A cap BELOW the current stop must not tighten it,
    # which is covered by test_max_abs_below_current_sl_leaves_untouched.
    monkeypatch.delenv("DEXTER3_SL_FLOOR_TR_MULT", raising=False)
    j0 = DecisionJournal(tmp_path / "base3.db")
    try:
        base = _run(j0)
    finally:
        j0.close()
    base_dist = abs(float(base["entry"]) - float(base["sl"]))
    cap = base_dist + 1.0

    monkeypatch.setenv("DEXTER3_SL_FLOOR_TR_MULT", "50")   # absurd floor
    monkeypatch.setenv("DEXTER3_SL_FLOOR_MAX_ABS", str(cap))
    row = _run(journal)
    meta = row["features"]["sl_floor"]
    assert meta["applied"] is True
    assert meta["capped_by_max_abs"] is True
    assert abs(float(row["entry"]) - float(row["sl"])) == pytest.approx(cap, abs=1e-3)


def test_internal_failure_leaves_decision_unchanged(journal, monkeypatch, tmp_path):
    monkeypatch.delenv("DEXTER3_SL_FLOOR_TR_MULT", raising=False)
    j0 = DecisionJournal(tmp_path / "base2.db")
    try:
        base = _run(j0)
    finally:
        j0.close()

    monkeypatch.setenv("DEXTER3_SL_FLOOR_TR_MULT", "3.0")

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom-floor")

    monkeypatch.setattr(sr.stop_floor, "apply_floor", _boom)
    row = _run(journal)
    assert row is not None and row["action"] == "enter"
    for k in ("side", "entry", "sl", "tp"):
        assert row[k] == base[k], f"{k} changed despite an internal failure"
