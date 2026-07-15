"""Regression tests for the 2026-07-15 CROSS-LANE ENTANGLEMENT AUDIT holes
(docs/AGENT_SYNC_BOARD.md, entry "~03:20Z — CROSS-LANE ENTANGLEMENT AUDIT"):

  H1 — ``get_deals`` from_timestamp_ms threading (openapi daemon transport
       honors it via the reconcile payload; local-MCP transport accepts and
       drops it).
  H2 — journal lane identity: nullable ``label`` column on
       decisions/basket_events/skip_outcomes, idempotent migration for
       pre-existing DB files, and skip_evaluator's label-filtered
       evaluate_pending_skips/fear_cost_summary (H7 is the same basket_events
       label column, no separate test needed).
  H4 — combined real-account open-risk ceiling
       (DEXTER3_ACCOUNT_MAX_OPEN_RISK_USD) in Dexter3Executor's pre-flight.
  H5 — per-lane log file selection (_active_log_file).
  H6 — ``main()``'s ``--once`` path applying the VP label patch (previously
       only ``--grok`` was handled).

H3 (daemon slow-call observability log) is intentionally NOT covered here —
it is a pure logging addition with no behavior to assert, per the task brief.

NO live MCP calls, NO touching data/runtime/* — every test drives a fake
transport / temp sqlite DB, same conventions as
tests/test_dexter3_cross_lane_incident_fixes.py and
tests/test_dexter3_openapi_client.py.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dexter3.executor as ex_mod
import dexter3.shadow_runner as sr
from dexter3 import skip_evaluator as se
from dexter3.decision_journal import DecisionJournal
from dexter3.executor import Dexter3Executor, ExecutorConfig
from dexter3.hunter_brain import Decision
from dexter3.mcp_client import Dexter3McpClient
from dexter3.openapi_client import Dexter3OpenApiClient

FABLE_LABEL = "dexter3:fable:m5h-v1"
GROK_LABEL = "dexter3:grok-v1.0:scalper"
VP_LABEL = "dexter3:vp:canary"


# ---------------------------------------------------------------------------
# H1 -- from_timestamp_ms threading
# ---------------------------------------------------------------------------


class _CapturingInvoke:
    """Fake ``Dexter3OpenApiClient._invoke`` — records every call, always
    answers with the same canned response (payload assertions are what these
    tests care about, not response shaping)."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, mode, payload, *, mutating=False, timeout_sec=None):
        self.calls.append((mode, dict(payload)))
        return self.response


def _openapi_client() -> Dexter3OpenApiClient:
    c = Dexter3OpenApiClient()
    c._pin_verified = True  # bypass the account-pin round trip for these tests
    return c


def test_openapi_get_deals_threads_from_timestamp_ms_into_reconcile_payload(monkeypatch):
    c = _openapi_client()
    router = _CapturingInvoke({"ok": True, "positions": [], "orders": [], "deals": []})
    monkeypatch.setattr(c, "_invoke", router)

    c.get_deals(count=50, from_timestamp_ms=1_752_364_800_000)

    reconcile_calls = [call for call in router.calls if call[0] == "reconcile"]
    assert len(reconcile_calls) == 1
    assert reconcile_calls[0][1]["from_timestamp"] == 1_752_364_800_000


def test_openapi_get_deals_omits_from_timestamp_when_not_given(monkeypatch):
    c = _openapi_client()
    router = _CapturingInvoke({"ok": True, "positions": [], "orders": [], "deals": []})
    monkeypatch.setattr(c, "_invoke", router)

    c.get_deals(count=50)

    reconcile_calls = [call for call in router.calls if call[0] == "reconcile"]
    assert "from_timestamp" not in reconcile_calls[0][1]


def test_local_mcp_get_deals_accepts_and_drops_from_timestamp_ms(monkeypatch):
    """The local-MCP transport has no from/to filtering support at all — the
    param must be accepted (never raise) and never reach the underlying MCP
    tool-call payload."""
    client = Dexter3McpClient()
    seen: dict[str, Any] = {}

    def _fake_call(name, args=None):
        seen["name"] = name
        seen["args"] = dict(args or {})
        return {"deals": []}

    monkeypatch.setattr(client, "call", _fake_call)

    result = client.get_deals(count=50, from_timestamp_ms=1_752_364_800_000)

    assert result == []
    assert seen["name"] == "get_deals"
    assert seen["args"] == {"count": 50}


# ---------------------------------------------------------------------------
# H2 -- journal lane identity
# ---------------------------------------------------------------------------


@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "lane_decoupling_journal.db")
    yield j
    j.close()


def _decision(*, action: str = "skip", symbol: str = "XAUUSD", ts_close: str = "2026-07-15T09:00:00Z", features=None) -> Decision:
    return Decision(
        ts_close=ts_close,
        symbol=symbol,
        action=action,
        side=None,
        entry_type=None,
        entry=None,
        sl=None,
        tp=None,
        size_class="none",
        leader_score=0.3,
        p_win_est=0.0,
        setup="none",
        reasons=["test"],
        features=features if features is not None else {},
    )


# Pre-H2 schema (no label column anywhere) -- a real snapshot of a DB file
# created before this fix, used to prove the migration is safe against it.
_PRE_MIGRATION_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_close TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT,
    entry_type TEXT,
    entry REAL,
    sl REAL,
    tp REAL,
    size_class TEXT,
    leader_score REAL,
    p_win_est REAL,
    setup TEXT,
    reasons_json TEXT NOT NULL,
    features_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE basket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE skip_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    would_have_result TEXT,
    would_have_pnl REAL,
    evaluated_at TEXT,
    FOREIGN KEY(decision_id) REFERENCES decisions(id)
);
"""


def test_decision_journal_label_migration_is_idempotent_on_pre_migration_db(tmp_path: Path):
    db_path = tmp_path / "pre_migration.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_PRE_MIGRATION_SCHEMA)
    conn.execute(
        "INSERT INTO decisions (ts_close, symbol, action, reasons_json, features_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-07-14T09:00:00Z", "XAUUSD", "skip", "[]", "{}", "2026-07-14T09:00:00Z"),
    )
    conn.execute(
        "INSERT INTO basket_events (basket_id, event, payload_json, ts) VALUES (?, ?, ?, ?)",
        (1, "on_entry", "{}", "2026-07-14T09:00:00Z"),
    )
    conn.execute(
        "INSERT INTO skip_outcomes (decision_id, would_have_result, would_have_pnl, evaluated_at) VALUES (?, ?, ?, ?)",
        (1, None, None, None),
    )
    conn.commit()
    conn.close()

    # First connect: must not raise, and must add the label column to every
    # pre-existing table without disturbing existing rows.
    j = DecisionJournal(db_path)
    try:
        for table in ("decisions", "basket_events", "skip_outcomes"):
            cols = {row[1] for row in j._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            assert "label" in cols
        pre_existing = j._conn.execute("SELECT symbol, label FROM decisions WHERE id = 1").fetchone()
        assert pre_existing == ("XAUUSD", None)
    finally:
        j.close()

    # Second connect against the SAME (now-migrated) file (simulating a lane
    # restart): the ALTER TABLE must never re-fire / must not raise.
    j2 = DecisionJournal(db_path)
    try:
        cols2 = {row[1] for row in j2._conn.execute("PRAGMA table_info(decisions)").fetchall()}
        assert "label" in cols2
        rid = j2.insert_decision(_decision(), label=FABLE_LABEL)
        row = j2._conn.execute("SELECT label FROM decisions WHERE id = ?", (rid,)).fetchone()
        assert row[0] == FABLE_LABEL
    finally:
        j2.close()


def test_label_round_trips_on_decisions_basket_events_and_skip_outcomes(journal: DecisionJournal):
    rid = journal.insert_decision(_decision(action="skip"), label=FABLE_LABEL)
    row = journal.recent_decisions(limit=1)[0]
    assert row["label"] == FABLE_LABEL

    journal.insert_basket_event(1, "on_entry", {"leg": "a"}, label=GROK_LABEL)
    events = journal.recent_basket_events(basket_id=1)
    assert len(events) == 1
    assert events[0]["label"] == GROK_LABEL

    outcome_id = journal.insert_skip_outcome(rid, would_have_result="would_have_won", would_have_pnl=1.0, label=VP_LABEL)
    outcome_label = journal._conn.execute(
        "SELECT label FROM skip_outcomes WHERE id = ?", (outcome_id,)
    ).fetchone()[0]
    assert outcome_label == VP_LABEL

    # None (default, unlabeled) still round-trips as NULL -- pre-fix callers
    # (and any legacy code path that never passes label=) keep working.
    rid2 = journal.insert_decision(_decision(action="skip", ts_close="2026-07-15T09:05:00Z"))
    row2 = next(r for r in journal.recent_decisions(limit=5) if r["id"] == rid2)
    assert row2["label"] is None


_STRONG_BUY_FEATURES = {
    "liquidity_sweep": {"value": True, "side": "buy", "evidence": "x"},
    "displacement": {"value": True, "direction": "buy"},
    "compression_release": {"value": False},
    "close_location_pressure": {"bias": "buy"},
    "swing_structure": {"value": "uptrend"},
}


class _SkipEvalFakeMcp:
    def __init__(self, bars: list[dict]) -> None:
        self._bars = bars

    def get_trendbars(self, symbol: str, period: str, count: int) -> list[dict]:
        return list(self._bars)


def _bar(o: float, h: float, l: float, c: float, ts: str) -> dict:
    return {"open": o, "high": h, "low": l, "close": c, "ts": ts}


def _insert_labeled_skip(journal: DecisionJournal, *, symbol: str, ts_close: str, label: str | None) -> int:
    d = _decision(action="skip", symbol=symbol, ts_close=ts_close, features=_STRONG_BUY_FEATURES)
    return journal.insert_decision(d, label=label)


def test_skip_evaluator_label_filter_excludes_foreign_and_unlabeled_rows(journal: DecisionJournal):
    now = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
    fable_id = _insert_labeled_skip(journal, symbol="XAUUSD", ts_close="2026-07-15T09:00:00Z", label=FABLE_LABEL)
    grok_id = _insert_labeled_skip(journal, symbol="XAUUSD", ts_close="2026-07-15T09:00:00Z", label=GROK_LABEL)
    legacy_id = _insert_labeled_skip(journal, symbol="XAUUSD", ts_close="2026-07-15T09:00:00Z", label=None)

    bars = [
        _bar(2000.0, 2001.0, 1999.0, 2000.0, "2026-07-15T08:55:00Z"),
        _bar(2000.0, 2001.0, 1999.5, 2000.0, "2026-07-15T09:00:00Z"),
        _bar(2000.0, 2010.0, 1999.5, 2008.0, "2026-07-15T09:05:00Z"),
    ]
    mcp = _SkipEvalFakeMcp(bars)

    result = se.evaluate_pending_skips(journal, mcp, now=now, delay_min=30, label=FABLE_LABEL)
    assert result["checked"] == 1  # only the fable-labeled row is eligible

    rows = journal._conn.execute("SELECT decision_id FROM skip_outcomes").fetchall()
    decision_ids = {r[0] for r in rows}
    assert decision_ids == {fable_id}
    assert grok_id not in decision_ids
    assert legacy_id not in decision_ids


def test_fear_cost_summary_label_filter_excludes_foreign_rows(journal: DecisionJournal):
    fable_id = _insert_labeled_skip(journal, symbol="XAUUSD", ts_close="2026-07-15T09:00:00Z", label=FABLE_LABEL)
    grok_id = _insert_labeled_skip(journal, symbol="XAUUSD", ts_close="2026-07-15T09:05:00Z", label=GROK_LABEL)

    journal.insert_skip_outcome(
        fable_id, would_have_result=json.dumps({"hit": "win"}), would_have_pnl=1.5, label=FABLE_LABEL
    )
    journal.insert_skip_outcome(
        grok_id, would_have_result=json.dumps({"hit": "win"}), would_have_pnl=99.0, label=GROK_LABEL
    )

    summary = se.fear_cost_summary(
        journal, hours=24, now=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc), label=FABLE_LABEL
    )
    assert summary["skips_evaluated"] == 1
    assert summary["would_have_pnl_r"] == pytest.approx(1.5)  # grok's 99.0 excluded

    unfiltered = se.fear_cost_summary(journal, hours=24, now=datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc))
    assert unfiltered["skips_evaluated"] == 2  # label=None keeps the legacy pooled behavior


# ---------------------------------------------------------------------------
# H4 -- combined real-account open-risk ceiling
# ---------------------------------------------------------------------------


class _H4FakeMcp:
    def __init__(self, *, positions: list[dict] | None = None) -> None:
        self.symbol_details = {
            "minVolume": 0.01, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0, "pipSize": 0.01,
        }
        self.spot = {"bid": 62000.0, "ask": 62006.0}
        self._positions = positions or []
        self.balance = {"traderId": 9922808, "balance": 10000.0}
        self.order_response = {"dealStatus": "FILLED"}
        self.post_entry_position: dict | None = None
        self.calls: list[str] = []

    def get_symbol_details(self, symbol: str) -> dict:
        return dict(self.symbol_details)

    def get_spot_price(self, symbol: str) -> dict:
        return dict(self.spot)

    def get_positions(self) -> list[dict]:
        self.calls.append("get_positions")
        if self.post_entry_position is not None and "place_market_order" in self.calls:
            return list(self._positions) + [dict(self.post_entry_position)]
        return list(self._positions)

    def get_balance(self) -> dict:
        return dict(self.balance)

    def place_market_order(self, **kwargs: Any) -> dict:
        self.calls.append("place_market_order")
        return dict(self.order_response)

    def amend_position(self, position_id: int, stop_loss=None, take_profit=None) -> dict:
        return {"status": "ok"}

    def close_position(self, position_id: int) -> dict:
        return {"status": "closed"}

    def get_deals(self, count: int = 200, from_timestamp_ms: int | None = None) -> list[dict]:
        return []


class _FakeDecision(SimpleNamespace):
    def __init__(self, **kw: Any) -> None:
        base = dict(
            symbol="BTCUSD", action="enter", side="buy", entry=62000.0, sl=61900.0, tp=62150.0,
            setup="leader_continuation", reasons=["test_reason"], session="london",
        )
        base.update(kw)
        super().__init__(**base)


def _foreign_position(*, volume: float, label: str = GROK_LABEL) -> dict:
    return {
        "positionId": 900,
        "symbolName": "BTCUSD",
        "tradeSide": "Buy",
        "volumeInUnits": volume,
        "entryPrice": 62000.0,
        "stopLoss": 61900.0,  # 100pt distance
        "takeProfit": 62150.0,
        "label": label,
    }


DEMO_ACCOUNT = {"traderId": 9922808}


def test_account_open_risk_cap_blocks_when_combined_dexter3_labeled_risk_exceeds(journal, monkeypatch):
    monkeypatch.delenv("DEXTER3_ACCOUNT_MAX_OPEN_RISK_USD", raising=False)
    # existing peer-lane risk: 100pt x 0.3 units = $30; this entry's own
    # planned risk_usd=15 -> combined $45 > default $40 cap.
    mcp = _H4FakeMcp(positions=[_foreign_position(volume=0.3)])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=15.0))
    result = ex.execute_entry(_FakeDecision(), DEMO_ACCOUNT)
    assert result["action"] == "refused"
    assert result["reason"] == "account_open_risk_cap"
    assert "place_market_order" not in mcp.calls


def test_account_open_risk_cap_allows_when_combined_risk_under_cap(journal, monkeypatch):
    monkeypatch.delenv("DEXTER3_ACCOUNT_MAX_OPEN_RISK_USD", raising=False)
    # existing peer-lane risk: 100pt x 0.05 units = $5; +$15 planned = $20 <= $40 cap.
    mcp = _H4FakeMcp(positions=[_foreign_position(volume=0.05)])
    mcp.post_entry_position = {
        "positionId": 901, "symbolName": "BTCUSD", "tradeSide": "Buy",
        "volumeInUnits": 0.05, "entryPrice": 62000.0, "stopLoss": 61900.0,
        "takeProfit": 62150.0, "label": ex_mod.LABEL,
    }
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=15.0))
    result = ex.execute_entry(_FakeDecision(), DEMO_ACCOUNT)
    assert result["action"] == "entered"
    assert "place_market_order" in mcp.calls


def test_account_open_risk_cap_disabled_at_non_positive_env(journal, monkeypatch):
    monkeypatch.setenv("DEXTER3_ACCOUNT_MAX_OPEN_RISK_USD", "0")
    # same setup that BLOCKS with the default cap above -- disabled cap must allow it.
    mcp = _H4FakeMcp(positions=[_foreign_position(volume=0.3)])
    mcp.post_entry_position = {
        "positionId": 902, "symbolName": "BTCUSD", "tradeSide": "Buy",
        "volumeInUnits": 0.05, "entryPrice": 62000.0, "stopLoss": 61900.0,
        "takeProfit": 62150.0, "label": ex_mod.LABEL,
    }
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=15.0))
    result = ex.execute_entry(_FakeDecision(), DEMO_ACCOUNT)
    assert result["action"] == "entered"


def test_account_open_risk_cap_fails_open_on_malformed_position_data(journal, monkeypatch):
    monkeypatch.delenv("DEXTER3_ACCOUNT_MAX_OPEN_RISK_USD", raising=False)
    mcp = _H4FakeMcp()
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    # a malformed "position" (not a dict) must never raise out of the gate --
    # it must be treated as a compute error and fail OPEN (allow).
    refusal = ex._account_open_risk_cap_refusal("BTCUSD", ["not-a-dict-position"], 0.5)
    assert refusal is None


# ---------------------------------------------------------------------------
# H5 -- per-lane log file selection
# ---------------------------------------------------------------------------


def test_active_log_file_selects_by_mode(monkeypatch):
    assert sr._active_log_file("v16").name == "dexter3_shadow.log"
    assert sr._active_log_file("grok").name == "dexter3_grok_shadow.log"
    assert sr._active_log_file("vp").name == "dexter3_vp_shadow.log"

    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    assert sr._active_log_file().name == "dexter3_shadow.log"
    monkeypatch.setenv("DEXTER3_MODE", "grok")
    assert sr._active_log_file().name == "dexter3_grok_shadow.log"
    monkeypatch.setenv("DEXTER3_MODE", "vp")
    assert sr._active_log_file().name == "dexter3_vp_shadow.log"


# ---------------------------------------------------------------------------
# H6 -- main() --once VP label patch
# ---------------------------------------------------------------------------


def test_once_mode_vp_patches_executor_label(tmp_path: Path, monkeypatch):
    from dexter3.volume_profile import VP_LABEL as _VP_LABEL

    monkeypatch.setenv("DEXTER3_MODE", "vp")
    # Isolate from any real MCP/journal/lock-file state (data/runtime/* must
    # never be touched by this test).
    monkeypatch.setattr(sr, "make_client", lambda: object())
    monkeypatch.setattr(sr, "DecisionJournal", lambda *a, **kw: DecisionJournal(tmp_path / "once_vp_journal.db"))
    monkeypatch.setattr(sr, "run_once", lambda *a, **kw: None)
    monkeypatch.setattr(sr, "LOCK_FILE", tmp_path / "dexter3_loop.lock")
    monkeypatch.setattr(sr, "GROK_LOCK_FILE", tmp_path / "dexter3_grok_loop.lock")
    # dexter3.executor.LABEL is mutated by main() as a bare module-global
    # assignment (not via monkeypatch) -- pre-register a snapshot so pytest
    # restores the ORIGINAL value at teardown regardless of that mutation.
    monkeypatch.setattr(ex_mod, "LABEL", ex_mod.LABEL)

    rc = sr.main(["--once", "--symbols", "XAUUSD"])

    assert rc == 0
    assert ex_mod.LABEL == _VP_LABEL


def test_once_mode_fable_default_leaves_executor_label_unchanged(tmp_path: Path, monkeypatch):
    """Negative control: without DEXTER3_MODE=vp, --once must NOT touch LABEL."""
    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    monkeypatch.setattr(sr, "make_client", lambda: object())
    monkeypatch.setattr(sr, "DecisionJournal", lambda *a, **kw: DecisionJournal(tmp_path / "once_fable_journal.db"))
    monkeypatch.setattr(sr, "run_once", lambda *a, **kw: None)
    monkeypatch.setattr(sr, "LOCK_FILE", tmp_path / "dexter3_loop.lock")
    monkeypatch.setattr(sr, "GROK_LOCK_FILE", tmp_path / "dexter3_grok_loop.lock")
    monkeypatch.setattr(ex_mod, "LABEL", FABLE_LABEL)

    rc = sr.main(["--once", "--symbols", "XAUUSD"])

    assert rc == 0
    assert ex_mod.LABEL == FABLE_LABEL
