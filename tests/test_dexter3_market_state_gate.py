"""Wiring tests for the broker market-state gate in run_symbol_cycle.

Proves the three invariants that make it safe on the live demo loop:
  * env OFF (default)  -> byte-identical behaviour (still enters).
  * env ON + stale feed -> observable ``market_closed_broker:stale_feed`` skip
    BEFORE any decision/journal (the weekend/holiday case).
  * env ON + feed within ceiling -> gate opens, trading proceeds.

Same fake-mcp / temp-sqlite conventions as tests/test_dexter3_price_action_eye.py.
Year-2020 bar timestamps make fetch_fresh_m5 return on the first read (no
retry/sleep) and are "stale" by construction for the default ceiling.
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


def _m5_bars(n: int = 65) -> list[dict[str, Any]]:
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


class _FakeMcp:
    def __init__(self, m5_bars: list[dict[str, Any]]) -> None:
        self._m5 = list(m5_bars)

    def get_trendbars(self, symbol: str, period: str, count: int) -> list[dict[str, Any]]:
        return list(self._m5) if period == "m5" else []

    def get_spot_price(self, symbol: str) -> dict[str, Any]:
        return {"bid": 2000.0, "ask": 2000.5}

    def get_symbol_details(self, symbol: str) -> dict[str, Any]:
        # No broker trading flag available in this fake -> trading_enabled None,
        # so the gate keys purely on feed freshness (the v1 primary signal).
        return {}


@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "market_state_gate.db")
    yield j
    j.close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    sr._SYMBOL_META_CACHE.clear()
    monkeypatch.setattr(sr, "log_line", lambda *_a, **_k: None)
    monkeypatch.setenv("DEXTER3_HUNT", "1")
    yield
    sr._SYMBOL_META_CACHE.clear()


def test_gate_off_is_unchanged(journal, monkeypatch):
    # Default (env unset) -> gate skipped -> normal hunt entry.
    monkeypatch.delenv("DEXTER3_MARKET_STATE_GATE", raising=False)
    status = sr.run_symbol_cycle(_FakeMcp(_m5_bars()), journal, {}, {}, "XAUUSD", executor=None)
    assert status.startswith("decided")
    assert "market_closed" not in status


def test_gate_on_stale_feed_blocks(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_MARKET_STATE_GATE", "1")
    status = sr.run_symbol_cycle(_FakeMcp(_m5_bars()), journal, {}, {}, "XAUUSD", executor=None)
    assert status == "market_closed_broker:stale_feed"
    # Blocked BEFORE any decision was journaled.
    assert journal.recent_decisions(limit=1) == []


def test_gate_on_fresh_feed_allows(journal, monkeypatch):
    # A ceiling wide enough that the year-2020 bar age is "within" it -> the
    # gate reports open and trading proceeds exactly as with the gate off.
    monkeypatch.setenv("DEXTER3_MARKET_STATE_GATE", "1")
    monkeypatch.setenv("DEXTER3_MARKET_STALE_SEC", "1e12")
    status = sr.run_symbol_cycle(_FakeMcp(_m5_bars()), journal, {}, {}, "XAUUSD", executor=None)
    assert status.startswith("decided")
    assert "market_closed" not in status
