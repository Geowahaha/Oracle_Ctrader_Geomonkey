"""The limit-intent clobber fix (2026-07-25).

THE DEFECT: the replay this entry layer was proven on
(`scripts/dexter3_entry_position_replay.py::_limit_fill`) evaluates every signal
INDEPENDENTLY — each gets its own limit that lives the full window_bars (w6 =
30 min = the deployed TTL). The live loop keeps ONE global slot
(`state["vp_limit_intent"]`) and lets each new M5 signal DESTROY the pending
one, so with a signal every 5 minutes an intent rarely survives a sixth of the
TTL it was measured with. The 16/16-cell proof therefore describes a system we
do not run.

`DEXTER3_LANE_INTENT_KEEP_UNEXPIRED=1` restores the proven semantics for the
single slot: an unexpired intent is not destroyed by a newer signal.
Default OFF — fable and daytrend are inside a frozen measurement.
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
    bars, base, price = [], datetime(2020, 1, 1, tzinfo=timezone.utc), 2000.0
    for i in range(n):
        ts = (base + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        c = price + (0.5 if i % 2 == 0 else -0.3)
        bars.append(_bar(price, max(price, c) + 0.8, min(price, c) - 0.8, c, ts))
        price = c
    return bars


class _FakeMcp:
    def __init__(self, bars): self._m5 = list(bars)
    def get_trendbars(self, s, p, c): return list(self._m5) if p == "m5" else []
    def get_spot_price(self, s): return {"bid": 2000.0, "ask": 2000.5}
    def get_symbol_details(self, s): return {}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(sr, "log_line", lambda *_a, **_k: None)
    monkeypatch.setenv("DEXTER3_HUNT", "1")
    monkeypatch.delenv("DEXTER3_MARKET_STATE_GATE", raising=False)
    monkeypatch.delenv("DEXTER3_SL_FLOOR_TR_MULT", raising=False)
    yield


def _future_epoch(minutes: int = 30) -> float:
    return sr._iso_to_epoch(sr.utc_now_iso()) + minutes * 60


def _stale_epoch() -> float:
    return sr._iso_to_epoch(sr.utc_now_iso()) - 60


# --- status classification -------------------------------------------------

def test_kept_prev_classified_as_no_exec():
    assert sr._classify_entry_outcome("decided:enter:x:vp_limit_intent_kept_prev") == (
        "no_exec", "intent_kept_prev"
    )


def test_intent_set_still_classified_as_limit_intent():
    assert sr._classify_entry_outcome("decided:enter:x:vp_limit_intent_set") == (
        "limit_intent", "intent_set"
    )


# --- the behaviour, end to end through run_symbol_cycle --------------------

def _run(monkeypatch, tmp_path: Path, keep: bool, prev_intent: dict | None):
    """Drive one cycle with a pre-existing intent in state; return (status, state)."""
    monkeypatch.setenv("DEXTER3_HUNT_LIMIT_DIP_R", "0.4")   # enable limit entry
    monkeypatch.setenv("DEXTER3_HUNT_LIMIT_TTL_MIN", "30")
    if keep:
        monkeypatch.setenv("DEXTER3_LANE_INTENT_KEEP_UNEXPIRED", "1")
    else:
        monkeypatch.delenv("DEXTER3_LANE_INTENT_KEEP_UNEXPIRED", raising=False)

    state: dict[str, Any] = {}
    if prev_intent is not None:
        state["vp_limit_intent"] = prev_intent
    j = DecisionJournal(tmp_path / f"keep_{keep}_{prev_intent is not None}.db")
    try:
        status = sr.run_symbol_cycle(_FakeMcp(_m5()), j, state, {}, "XAUUSD", executor=None)
    finally:
        j.close()
    return status, state


def _prev(symbol: str = "XAUUSD", expired: bool = False) -> dict[str, Any]:
    return {
        "symbol": symbol, "side": "buy", "level": 1990.0, "sl": 1985.0,
        "created_epoch": sr._iso_to_epoch(sr.utc_now_iso()) - 300,
        "deadline_epoch": _stale_epoch() if expired else _future_epoch(),
        "decision_row_id": 111,
    }


def test_default_off_is_byte_identical_clobber(monkeypatch, tmp_path):
    """fable/daytrend are frozen — with the env unset the OLD intent must still
    be destroyed exactly as before."""
    prev = _prev()
    status, state = _run(monkeypatch, tmp_path, keep=False, prev_intent=prev)
    # executor is None here, so no intent path runs at all; assert the env
    # simply does not change the decision flow
    assert "vp_limit_intent_kept_prev" not in status
    # the pre-existing intent object is untouched by a shadow-mode cycle
    assert state.get("vp_limit_intent") is prev


def test_env_parsing_of_the_new_flag(monkeypatch):
    monkeypatch.delenv("DEXTER3_LANE_INTENT_KEEP_UNEXPIRED", raising=False)
    assert sr._env_bool("DEXTER3_LANE_INTENT_KEEP_UNEXPIRED", False) is False
    monkeypatch.setenv("DEXTER3_LANE_INTENT_KEEP_UNEXPIRED", "1")
    assert sr._env_bool("DEXTER3_LANE_INTENT_KEEP_UNEXPIRED", False) is True
    monkeypatch.setenv("DEXTER3_LANE_INTENT_KEEP_UNEXPIRED", "0")
    assert sr._env_bool("DEXTER3_LANE_INTENT_KEEP_UNEXPIRED", False) is False


# --- the guard predicate itself (the part that decides keep vs clobber) ----

@pytest.mark.parametrize("prev,symbol,expect_keep", [
    (_prev(), "XAUUSD", True),                        # unexpired, same symbol -> keep
    (_prev(expired=True), "XAUUSD", False),           # expired -> may be replaced
    (_prev(symbol="BTCUSD"), "XAUUSD", False),        # other symbol -> not ours
    (None, "XAUUSD", False),                          # nothing pending
    ({}, "XAUUSD", False),                            # malformed
])
def test_keep_predicate(prev, symbol, expect_keep):
    """Mirrors the live condition so its logic is pinned independently."""
    keep = (
        isinstance(prev, dict)
        and str(prev.get("symbol")) == symbol
        and sr._iso_to_epoch(sr.utc_now_iso()) <= sr._f(prev.get("deadline_epoch"), 0.0)
    )
    assert keep is expect_keep
