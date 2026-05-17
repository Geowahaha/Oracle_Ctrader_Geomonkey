"""Tests for the Confluence Ledger + Booster."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analysis.confluence import ConfluenceBooster, ConfluenceLedger


def _clock(start: datetime):
    state = {"now": start}

    def fn() -> datetime:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return fn, advance


def test_ledger_dedupes_repeat_vote_within_window():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, advance = _clock(base)
    ledger = ConfluenceLedger(window_minutes=10.0, dedupe_seconds=60.0, clock=clock)
    assert ledger.record_vote(family="A", symbol="XAUUSD", side="long") is True
    # Same family/side again within dedupe → rejected.
    assert ledger.record_vote(family="A", symbol="XAUUSD", side="long") is False
    counts = ledger.count_for(symbol="XAUUSD")
    assert counts.long == 1
    assert counts.short == 0
    # Different family within dedupe → accepted.
    assert ledger.record_vote(family="B", symbol="XAUUSD", side="long") is True
    assert ledger.count_for(symbol="XAUUSD").long == 2


def test_ledger_evicts_after_window_expires():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, advance = _clock(base)
    ledger = ConfluenceLedger(window_minutes=5.0, clock=clock)
    ledger.record_vote(family="A", symbol="XAUUSD", side="long")
    advance(10 * 60)  # 10 minutes later
    counts = ledger.count_for(symbol="XAUUSD")
    assert counts.long == 0
    assert counts.short == 0


def test_booster_strong_consensus_doubles_size():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _clock(base)
    ledger = ConfluenceLedger(window_minutes=10.0, clock=clock)
    for family in ("A", "B", "C"):
        ledger.record_vote(family=family, symbol="XAUUSD", side="long")
    booster = ConfluenceBooster(ledger=ledger)
    result = booster.multiplier_for(symbol="XAUUSD", side="long")
    assert result.state == "strong_consensus"
    assert result.multiplier == 2.0
    assert result.agree_count == 3
    assert result.disagree_count == 0


def test_booster_friction_returns_mid_multiplier():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _clock(base)
    ledger = ConfluenceLedger(window_minutes=10.0, clock=clock)
    for f in ("A", "B", "C"):
        ledger.record_vote(family=f, symbol="XAUUSD", side="long")
    ledger.record_vote(family="D", symbol="XAUUSD", side="short")
    booster = ConfluenceBooster(ledger=ledger)
    r = booster.multiplier_for(symbol="XAUUSD", side="long")
    assert r.state == "consensus_with_friction"
    assert r.multiplier == 1.30


def test_booster_split_returns_defensive_size():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _clock(base)
    ledger = ConfluenceLedger(window_minutes=10.0, clock=clock)
    for f in ("A", "B"):
        ledger.record_vote(family=f, symbol="XAUUSD", side="long")
    for f in ("C", "D"):
        ledger.record_vote(family=f, symbol="XAUUSD", side="short")
    booster = ConfluenceBooster(ledger=ledger)
    long_view = booster.multiplier_for(symbol="XAUUSD", side="long")
    short_view = booster.multiplier_for(symbol="XAUUSD", side="short")
    assert long_view.state == "split"
    assert short_view.state == "split"
    assert long_view.multiplier == 0.70


def test_booster_opposite_consensus_returns_fight_minimum():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _clock(base)
    ledger = ConfluenceLedger(window_minutes=10.0, clock=clock)
    for f in ("A", "B", "C"):
        ledger.record_vote(family=f, symbol="XAUUSD", side="short")
    booster = ConfluenceBooster(ledger=ledger)
    r = booster.multiplier_for(symbol="XAUUSD", side="long")
    assert r.state == "opposite_consensus"
    assert r.multiplier == 0.50


def test_booster_neutral_when_only_one_vote():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _clock(base)
    ledger = ConfluenceLedger(window_minutes=10.0, clock=clock)
    ledger.record_vote(family="A", symbol="XAUUSD", side="long")
    booster = ConfluenceBooster(ledger=ledger)
    r = booster.multiplier_for(symbol="XAUUSD", side="long")
    assert r.state == "neutral"
    assert r.multiplier == 1.0


def test_booster_normalizes_buy_to_long_and_sell_to_short():
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    clock, _ = _clock(base)
    ledger = ConfluenceLedger(window_minutes=10.0, clock=clock)
    ledger.record_vote(family="A", symbol="XAUUSD", side="buy")
    ledger.record_vote(family="B", symbol="XAUUSD", side="BUY")
    counts = ledger.count_for(symbol="XAUUSD")
    assert counts.long == 2
    booster = ConfluenceBooster(ledger=ledger)
    r = booster.multiplier_for(symbol="XAUUSD", side="long")
    assert r.agree_count == 2
