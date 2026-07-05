"""Unit tests for dexter3/decision_journal.py and the M5-close state tracker
in dexter3/shadow_runner.py.

Critical invariants under test (per Dexter3 Phase 1 spec):
  (d) M5-close detection fires exactly once per new bar (state-tracking
      function tested directly, not via the live loop).
  (e) journal roundtrip with Thai text.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dexter3.decision_journal import DecisionJournal
from dexter3.hunter_brain import Decision
from dexter3.shadow_runner import is_new_m5_close, mark_m5_close_seen


@pytest.fixture()
def journal(tmp_path: Path) -> DecisionJournal:
    db_path = tmp_path / "dexter3_journal_test.db"
    j = DecisionJournal(db_path)
    yield j
    j.close()


def _decision(
    action="skip",
    symbol="XAUUSD",
    reasons=None,
    features=None,
    **overrides,
) -> Decision:
    base = dict(
        ts_close="2026-07-05T09:35:00Z",
        symbol=symbol,
        action=action,
        side=None,
        entry_type=None,
        entry=None,
        sl=None,
        tp=None,
        size_class="none",
        leader_score=0.41,
        p_win_est=0.0,
        setup="none",
        reasons=reasons if reasons is not None else ["insufficient bars"],
        features=features if features is not None else {"note": "test"},
    )
    base.update(overrides)
    return Decision(**base)


# -- auto-create dir/schema --------------------------------------------------------------


def test_journal_auto_creates_db_directory_and_schema(tmp_path: Path):
    nested = tmp_path / "nested" / "runtime" / "dexter3_journal.db"
    assert not nested.parent.exists()
    j = DecisionJournal(nested)
    try:
        assert nested.exists()
        assert nested.parent.exists()
        # schema present: insert must not raise
        j.insert_decision(_decision())
    finally:
        j.close()


def test_journal_uses_wal_mode(journal: DecisionJournal):
    mode = journal._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# -- decisions roundtrip -----------------------------------------------------------------


def test_insert_and_recent_decisions_roundtrip(journal: DecisionJournal):
    rid = journal.insert_decision(_decision())
    assert rid > 0
    rows = journal.recent_decisions(limit=5)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "XAUUSD"
    assert rows[0]["action"] == "skip"
    assert rows[0]["reasons"] == ["insufficient bars"]
    assert rows[0]["features"] == {"note": "test"}


def test_recent_decisions_filters_by_symbol(journal: DecisionJournal):
    journal.insert_decision(_decision(symbol="XAUUSD"))
    journal.insert_decision(_decision(symbol="BTCUSD"))
    journal.insert_decision(_decision(symbol="XAUUSD"))

    xau_rows = journal.recent_decisions(limit=10, symbol="XAUUSD")
    assert len(xau_rows) == 2
    assert all(r["symbol"] == "XAUUSD" for r in xau_rows)

    btc_rows = journal.recent_decisions(limit=10, symbol="BTCUSD")
    assert len(btc_rows) == 1


def test_recent_decisions_respects_limit_and_order(journal: DecisionJournal):
    for i in range(5):
        journal.insert_decision(_decision(ts_close=f"2026-07-05T09:{30+i}:00Z"))
    rows = journal.recent_decisions(limit=3)
    assert len(rows) == 3
    # most recent (highest id) first
    assert rows[0]["ts_close"] == "2026-07-05T09:34:00Z"


def test_insert_decision_accepts_plain_dict_not_just_dataclass(journal: DecisionJournal):
    payload = _decision().to_dict()
    rid = journal.insert_decision(payload)
    assert rid > 0


def test_enter_decision_roundtrip_preserves_numeric_fields(journal: DecisionJournal):
    d = _decision(
        action="enter", side="sell", entry_type="stop",
        entry=2020.5, sl=2023.2, tp=2015.0, size_class="small",
        leader_score=0.71, p_win_est=0.55, setup="dragon_shelf_short",
        reasons=["upper_shelf_rejection confirmed"],
    )
    journal.insert_decision(d)
    row = journal.recent_decisions(limit=1)[0]
    assert row["entry"] == pytest.approx(2020.5)
    assert row["sl"] == pytest.approx(2023.2)
    assert row["tp"] == pytest.approx(2015.0)
    assert row["side"] == "sell"
    assert row["setup"] == "dragon_shelf_short"


# -- (e) Thai text roundtrip --------------------------------------------------------------


def test_journal_roundtrip_with_thai_text_in_reasons_and_features(journal: DecisionJournal):
    thai_reason = "leader score ต่ำกว่าเกณฑ์ / leader score below floor"
    thai_feature_note = "ทดสอบข้อความภาษาไทยในฟีเจอร์"
    d = _decision(reasons=[thai_reason, "second reason 123"], features={"note": thai_feature_note, "n": 42})
    rid = journal.insert_decision(d)
    rows = journal.recent_decisions(limit=1)
    assert rows[0]["reasons"][0] == thai_reason
    assert rows[0]["features"]["note"] == thai_feature_note
    assert rows[0]["features"]["n"] == 42
    assert rid > 0


def test_basket_event_roundtrip_with_thai_text(journal: DecisionJournal):
    thai_payload = {"note": "เปิด basket ใหม่", "leg_count": 1}
    event_id = journal.insert_basket_event(basket_id=7, event="on_entry", payload=thai_payload)
    assert event_id > 0
    events = journal.recent_basket_events(basket_id=7)
    assert len(events) == 1
    assert events[0]["payload"]["note"] == "เปิด basket ใหม่"
    assert events[0]["event"] == "on_entry"


# -- basket_events ------------------------------------------------------------------------


def test_basket_events_roundtrip_and_filter(journal: DecisionJournal):
    journal.insert_basket_event(1, "on_entry", {"leg": "a"})
    journal.insert_basket_event(2, "on_entry", {"leg": "b"})
    journal.insert_basket_event(1, "resolve", {"result": "profit"})

    basket1_events = journal.recent_basket_events(basket_id=1)
    assert len(basket1_events) == 2
    assert {e["event"] for e in basket1_events} == {"on_entry", "resolve"}

    all_events = journal.recent_basket_events()
    assert len(all_events) == 3


# -- skip_outcomes ------------------------------------------------------------------------


def test_skip_outcome_starts_unfilled_and_can_be_backfilled(journal: DecisionJournal):
    rid = journal.insert_decision(_decision(action="skip"))
    outcome_id = journal.insert_skip_outcome(rid)
    assert outcome_id > 0

    row = journal._conn.execute(
        "SELECT would_have_result, would_have_pnl FROM skip_outcomes WHERE id = ?", (outcome_id,)
    ).fetchone()
    assert row == (None, None)

    filled_id = journal.insert_skip_outcome(rid, would_have_result="would_have_won", would_have_pnl=15.5)
    row2 = journal._conn.execute(
        "SELECT would_have_result, would_have_pnl FROM skip_outcomes WHERE id = ?", (filled_id,)
    ).fetchone()
    assert row2 == ("would_have_won", 15.5)


# -- context manager ------------------------------------------------------------------------


def test_journal_context_manager_closes_connection(tmp_path: Path):
    db_path = tmp_path / "ctx_journal.db"
    with DecisionJournal(db_path) as j:
        j.insert_decision(_decision())
    # connection should be closed; a further op must raise
    with pytest.raises(Exception):
        j._conn.execute("SELECT 1")


# -- (d) M5-close detection fires exactly once per new bar -----------------------------------


def _m5_bars(ts_list: list[str]) -> list[dict]:
    return [{"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "ts": ts} for ts in ts_list]


def test_is_new_m5_close_true_on_first_observation():
    state = {"symbols": {}}
    bars = _m5_bars(["2026-07-05T09:00:00Z", "2026-07-05T09:05:00Z"])
    is_new, close_ts = is_new_m5_close(state, "XAUUSD", bars)
    assert is_new is True
    assert close_ts == "2026-07-05T09:05:00Z"


def test_is_new_m5_close_false_after_marking_seen():
    state = {"symbols": {}}
    bars = _m5_bars(["2026-07-05T09:00:00Z", "2026-07-05T09:05:00Z"])
    is_new, close_ts = is_new_m5_close(state, "XAUUSD", bars)
    assert is_new is True
    mark_m5_close_seen(state, "XAUUSD", close_ts)

    # same bars fetched again (no new close yet) -> must NOT fire again
    is_new2, close_ts2 = is_new_m5_close(state, "XAUUSD", bars)
    assert is_new2 is False
    assert close_ts2 == "2026-07-05T09:05:00Z"


def test_is_new_m5_close_fires_exactly_once_across_repeated_polls():
    """Simulate a poll loop hitting the same M5 close 10 times before a new
    bar arrives — is_new_m5_close must be True exactly once."""
    state = {"symbols": {}}
    bars = _m5_bars(["2026-07-05T09:00:00Z", "2026-07-05T09:05:00Z"])
    fire_count = 0
    for _poll in range(10):
        is_new, close_ts = is_new_m5_close(state, "XAUUSD", bars)
        if is_new:
            fire_count += 1
            mark_m5_close_seen(state, "XAUUSD", close_ts)
    assert fire_count == 1, f"expected exactly one fire across repeated polls, got {fire_count}"


def test_is_new_m5_close_fires_again_when_a_genuinely_new_bar_closes():
    state = {"symbols": {}}
    bars1 = _m5_bars(["2026-07-05T09:00:00Z", "2026-07-05T09:05:00Z"])
    is_new1, ts1 = is_new_m5_close(state, "XAUUSD", bars1)
    assert is_new1 is True
    mark_m5_close_seen(state, "XAUUSD", ts1)

    bars2 = _m5_bars(["2026-07-05T09:00:00Z", "2026-07-05T09:05:00Z", "2026-07-05T09:10:00Z"])
    is_new2, ts2 = is_new_m5_close(state, "XAUUSD", bars2)
    assert is_new2 is True
    assert ts2 == "2026-07-05T09:10:00Z"


def test_is_new_m5_close_tracks_symbols_independently():
    state = {"symbols": {}}
    xau_bars = _m5_bars(["2026-07-05T09:00:00Z", "2026-07-05T09:05:00Z"])
    btc_bars = _m5_bars(["2026-07-05T09:00:00Z", "2026-07-05T09:05:00Z"])

    is_new_xau, ts_xau = is_new_m5_close(state, "XAUUSD", xau_bars)
    mark_m5_close_seen(state, "XAUUSD", ts_xau)

    # BTCUSD has never been marked -> must still be new even though the
    # timestamp coincidentally matches XAUUSD's
    is_new_btc, ts_btc = is_new_m5_close(state, "BTCUSD", btc_bars)
    assert is_new_btc is True


def test_is_new_m5_close_handles_empty_bars():
    state = {"symbols": {}}
    is_new, close_ts = is_new_m5_close(state, "XAUUSD", [])
    assert is_new is False
    assert close_ts is None
