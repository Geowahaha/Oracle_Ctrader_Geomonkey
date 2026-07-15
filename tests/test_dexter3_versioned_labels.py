"""Regression tests for the 2026-07-15 VERSIONED BROKER LABELS design.

Owner directive: the broker order label must carry the STRATEGY VERSION so
every trade is attributable to the code version that placed it. LABEL becomes
``f"{LABEL_FAMILY}:{VERSION}"`` (VERSION from env ``DEXTER3_FABLE_VERSION``,
sanitized, length-capped) instead of the frozen literal
``"dexter3:fable:m5h-v1"``. Every "is this mine" check switches from
full-label equality to FAMILY-PREFIX matching so a version bump never orphans
a position/journal row a PRIOR version of the SAME lane wrote. Grok/VP keep
their own frozen label constants unchanged; only their family roots
participate in the same matching mechanism.

Invariants under test (see the task brief's safety-invariant list a-g):
  (a) an OPEN position labeled the OLD "dexter3:fable:m5h-v1" is still
      owned/managed/vanish-reconciled by an executor whose CURRENT version
      label is "dexter3:fable:v1.7-selective-edge".
  (b) new entries journal + send the versioned label.
  (c) _lane_realized_today sums deals across BOTH old and new labels of the
      family, never grok's.
  (d) empirical_stats/skip_evaluator's family filter spans versions, still
      excludes grok + unlabeled rows.
  (e) grok's existing (frozen) label still matches family "dexter3:grok" and
      NOT "dexter3:fable".
  (f) version sanitization (spaces/colons/Thai chars -> '-'), length cap.
  (g) H4's account-wide open-risk cap still counts every dexter3 family
      (unaffected by the family-matching change — it was already a broad
      "dexter3*" prefix scan).

NO live MCP calls, NO touching data/runtime/* — every test drives a fake
transport / temp sqlite DB, same conventions as
tests/test_dexter3_cross_lane_incident_fixes.py and
tests/test_dexter3_lane_decoupling.py.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dexter3.executor as ex_mod
import dexter3.shadow_runner as sr
from dexter3 import empirical_stats as es
from dexter3 import skip_evaluator as se
from dexter3.decision_journal import DecisionJournal
from dexter3.executor import (
    Dexter3Executor,
    ExecutorConfig,
    build_versioned_label,
    ensure_exec_events_table,
    insert_exec_event,
    is_our_position,
    label_matches_family,
    position_id_of,
    recent_exec_events,
    sanitize_label_version,
)
from dexter3.grok_v10 import GROK_LABEL, GROK_LABEL_FAMILY
from dexter3.hunter_brain import Decision
from dexter3.volume_profile import VP_LABEL, VP_LABEL_FAMILY

OLD_FABLE_LABEL = "dexter3:fable:m5h-v1"
NEW_FABLE_LABEL = "dexter3:fable:v1.7-selective-edge"


@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "versioned_labels_journal.db")
    yield j
    j.close()


# ---------------------------------------------------------------------------
# fakes (mirrors tests/test_dexter3_cross_lane_incident_fixes.py)
# ---------------------------------------------------------------------------


class FakeMcp:
    def __init__(self) -> None:
        self.symbol_details = {
            "minVolume": 0.01, "maxVolume": 10.0, "volumeStep": 0.01, "lotSize": 1.0, "pipSize": 0.01,
        }
        self.spot = {"bid": 62000.0, "ask": 62006.0}
        self._positions: list[dict] = []
        self.order_response = {"dealStatus": "FILLED"}
        self.post_entry_position: dict | None = None
        self.balance = {"traderId": 9922808, "balance": 10000.0}
        self.deals: list[dict] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_symbol_details(self, symbol: str) -> dict:
        self.calls.append(("get_symbol_details", {}))
        return dict(self.symbol_details)

    def get_spot_price(self, symbol: str) -> dict:
        self.calls.append(("get_spot_price", {}))
        return dict(self.spot)

    def get_positions(self) -> list[dict]:
        self.calls.append(("get_positions", {}))
        if self.post_entry_position is not None and any(c[0] == "place_market_order" for c in self.calls):
            return list(self._positions) + [dict(self.post_entry_position)]
        return list(self._positions)

    def get_balance(self) -> dict:
        return dict(self.balance)

    def place_market_order(self, **kwargs: Any) -> dict:
        self.calls.append(("place_market_order", kwargs))
        return dict(self.order_response)

    def amend_position(self, position_id: int, stop_loss=None, take_profit=None) -> dict:
        return {"status": "ok"}

    def close_position(self, position_id: int) -> dict:
        return {"status": "closed"}

    def get_deals(self, count: int = 200, from_timestamp_ms: int | None = None) -> list[dict]:
        self.calls.append(("get_deals", {"count": count, "from_timestamp_ms": from_timestamp_ms}))
        return list(self.deals)


class FakeDecision(SimpleNamespace):
    def __init__(self, **kw: Any) -> None:
        base = dict(
            symbol="BTCUSD", action="enter", side="buy", entry=62000.0, sl=61900.0, tp=62150.0,
            setup="leader_continuation", reasons=["test_reason"], session="london",
        )
        base.update(kw)
        super().__init__(**base)


DEMO_ACCOUNT = {"traderId": 9922808}


# ---------------------------------------------------------------------------
# (b) new entries journal + send the CURRENT versioned label
# ---------------------------------------------------------------------------


def test_new_entry_sends_and_journals_the_current_versioned_label(journal, monkeypatch):
    monkeypatch.setattr(ex_mod, "LABEL", NEW_FABLE_LABEL)
    mcp = FakeMcp()
    mcp.post_entry_position = {
        "positionId": 801, "symbolName": "BTCUSD", "tradeSide": "Buy",
        "volumeInUnits": 0.01, "stopLoss": 61900.0, "takeProfit": 62150.0, "label": NEW_FABLE_LABEL,
    }
    ex = Dexter3Executor(mcp, journal, ExecutorConfig())
    result = ex.execute_entry(FakeDecision(), DEMO_ACCOUNT)

    assert result["action"] == "entered"
    assert result["label"] == NEW_FABLE_LABEL
    place_calls = [c for c in mcp.calls if c[0] == "place_market_order"]
    assert place_calls[0][1]["label"] == NEW_FABLE_LABEL

    events = recent_exec_events(journal._conn, symbol="BTCUSD")
    entered = next(e for e in events if e["event"] == "entry_executed")
    assert entered["payload"]["label"] == NEW_FABLE_LABEL


# ---------------------------------------------------------------------------
# (a) a version bump never orphans a PRIOR version's open position
# ---------------------------------------------------------------------------


def test_is_our_position_recognizes_prior_version_of_same_family(monkeypatch):
    monkeypatch.setattr(ex_mod, "LABEL", NEW_FABLE_LABEL)
    assert is_our_position({"label": OLD_FABLE_LABEL}) is True
    assert is_our_position({"label": NEW_FABLE_LABEL}) is True
    assert is_our_position({"label": GROK_LABEL}) is False
    assert is_our_position({"label": VP_LABEL}) is False


def test_reconcile_vanished_still_reconciles_a_prior_version_entry(journal, monkeypatch):
    """An executor running the CURRENT version must still vanish-reconcile a
    position ITS OWN LANE opened under an OLDER version's label — this is the
    exact scenario invariant (a) names."""
    # Enter under the OLD version first (simulating a position opened before
    # today's version bump).
    monkeypatch.setattr(ex_mod, "LABEL", OLD_FABLE_LABEL)
    mcp = FakeMcp()
    ex_old = Dexter3Executor(mcp, journal, ExecutorConfig())
    mcp.post_entry_position = {
        "positionId": 802, "symbolName": "BTCUSD", "tradeSide": "Buy",
        "volumeInUnits": 0.01, "stopLoss": 61900.0, "takeProfit": 62150.0, "label": OLD_FABLE_LABEL,
    }
    entered = ex_old.execute_entry(FakeDecision(), DEMO_ACCOUNT)
    assert entered["action"] == "entered" and entered["position_id"] == 802
    mcp.calls = [c for c in mcp.calls if c[0] != "place_market_order"]
    mcp.post_entry_position = None

    # NOW bump the version (a fresh process/deploy) and prove the NEW
    # executor still reconciles the OLD position's broker-side close.
    monkeypatch.setattr(ex_mod, "LABEL", NEW_FABLE_LABEL)
    ex_new = Dexter3Executor(mcp, journal, ExecutorConfig())
    mcp.deals = [{"positionId": 802, "netProfit": -3.5}]
    out = ex_new.reconcile_vanished_lane_positions("BTCUSD", [])
    assert len(out) == 1
    assert out[0]["position_id"] == 802
    assert out[0]["pnl"] == pytest.approx(-3.5)

    # Negative control: grok's executor must NOT pick this fable row up.
    monkeypatch.setattr(ex_mod, "LABEL", GROK_LABEL)
    ex_grok = Dexter3Executor(mcp, journal, ExecutorConfig())
    assert ex_grok.reconcile_vanished_lane_positions("BTCUSD", []) == []


# ---------------------------------------------------------------------------
# (c) _lane_realized_today sums across BOTH labels of the family, never grok's
# ---------------------------------------------------------------------------


def test_lane_realized_today_spans_both_fable_versions_excludes_grok(monkeypatch):
    monkeypatch.setattr(sr, "_LANE_REALIZED_CACHE", {})
    mcp = FakeMcp()
    today_ms = int(_dt.datetime.now(_dt.timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0).timestamp() * 1000)
    mcp.deals = [
        {"label": OLD_FABLE_LABEL, "netProfit": -4.0, "execution_timestamp_ms": today_ms},
        {"label": NEW_FABLE_LABEL, "netProfit": 6.5, "execution_timestamp_ms": today_ms},
        {"label": GROK_LABEL, "netProfit": 999.0, "execution_timestamp_ms": today_ms},
    ]
    realized, pnls = sr._lane_realized_today(mcp, label_filter="dexter3:fable")
    assert realized == pytest.approx(2.5)  # -4.0 + 6.5, grok's 999.0 excluded
    assert sorted(pnls) == [pytest.approx(-4.0), pytest.approx(6.5)]


def test_active_label_family_matches_active_order_label_family_per_mode(monkeypatch):
    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    assert sr._active_label_family("v16") == ex_mod.LABEL_FAMILY
    assert sr._active_label_family("grok") == GROK_LABEL_FAMILY
    assert sr._active_label_family("vp") == VP_LABEL_FAMILY
    # Every mode's own _active_order_label() must belong to the family
    # _active_label_family() reports for that SAME mode.
    for mode in ("v16", "grok", "vp"):
        assert label_matches_family(sr._active_order_label(mode), sr._active_label_family(mode))


# ---------------------------------------------------------------------------
# (d) empirical_stats / skip_evaluator family filter spans versions
# ---------------------------------------------------------------------------


def test_empirical_stats_family_filter_pools_both_fable_versions_excludes_grok(journal):
    ensure_exec_events_table(journal._conn)
    for pnl, label in (
        (2.0, OLD_FABLE_LABEL),
        (1.0, NEW_FABLE_LABEL),
        (-3.0, GROK_LABEL),
    ):
        insert_exec_event(
            journal._conn,
            symbol="XAUUSD",
            event="lane_position_closed",
            verified=True,
            payload={"setup": "hunt_h1_context", "session": "london", "pnl": pnl, "label": label},
        )

    fable = es.compute_from_journal(journal, "XAUUSD", label="dexter3:fable")
    grok = es.compute_from_journal(journal, "XAUUSD", label="dexter3:grok")

    assert fable[("hunt_h1_context", "london")]["wins"] == 2  # both fable versions pooled
    assert fable[("hunt_h1_context", "london")]["losses"] == 0
    assert grok[("hunt_h1_context", "london")]["wins"] == 0
    assert grok[("hunt_h1_context", "london")]["losses"] == 1


def _decision(*, action: str = "skip", symbol: str = "XAUUSD", ts_close: str = "2026-07-15T09:00:00Z") -> Decision:
    return Decision(
        ts_close=ts_close, symbol=symbol, action=action, side=None, entry_type=None, entry=None,
        sl=None, tp=None, size_class="none", leader_score=0.3, p_win_est=0.0, setup="none",
        reasons=["test"], features={},
    )


def test_skip_evaluator_family_filter_spans_versions_excludes_grok_and_unlabeled(journal):
    old_id = journal.insert_decision(_decision(ts_close="2026-07-15T09:00:00Z"), label=OLD_FABLE_LABEL)
    new_id = journal.insert_decision(_decision(ts_close="2026-07-15T09:01:00Z"), label=NEW_FABLE_LABEL)
    grok_id = journal.insert_decision(_decision(ts_close="2026-07-15T09:02:00Z"), label=GROK_LABEL)
    legacy_id = journal.insert_decision(_decision(ts_close="2026-07-15T09:03:00Z"), label=None)

    rows = se._fetch_pending_skip_rows(
        journal._conn,
        now=_dt.datetime(2026, 7, 15, 10, 0, tzinfo=_dt.timezone.utc),
        delay_min=30,
        limit=200,
        label="dexter3:fable",
    )
    ids = {r["id"] for r in rows}
    assert ids == {old_id, new_id}
    assert grok_id not in ids
    assert legacy_id not in ids


def test_fear_cost_summary_family_filter_pools_both_fable_versions(journal):
    old_id = journal.insert_decision(_decision(ts_close="2026-07-15T09:00:00Z"), label=OLD_FABLE_LABEL)
    new_id = journal.insert_decision(_decision(ts_close="2026-07-15T09:05:00Z"), label=NEW_FABLE_LABEL)
    grok_id = journal.insert_decision(_decision(ts_close="2026-07-15T09:06:00Z"), label=GROK_LABEL)

    import json as _json

    journal.insert_skip_outcome(old_id, would_have_result=_json.dumps({"hit": "win"}), would_have_pnl=1.0, label=OLD_FABLE_LABEL)
    journal.insert_skip_outcome(new_id, would_have_result=_json.dumps({"hit": "win"}), would_have_pnl=2.0, label=NEW_FABLE_LABEL)
    journal.insert_skip_outcome(grok_id, would_have_result=_json.dumps({"hit": "win"}), would_have_pnl=99.0, label=GROK_LABEL)

    summary = se.fear_cost_summary(
        journal, hours=24, now=_dt.datetime(2026, 7, 15, 10, 0, tzinfo=_dt.timezone.utc), label="dexter3:fable"
    )
    assert summary["skips_evaluated"] == 2
    assert summary["would_have_pnl_r"] == pytest.approx(3.0)  # grok's 99.0 excluded


# ---------------------------------------------------------------------------
# (e) grok's frozen label matches ITS family, not fable's
# ---------------------------------------------------------------------------


def test_grok_label_matches_own_family_not_fable(monkeypatch):
    assert label_matches_family(GROK_LABEL, GROK_LABEL_FAMILY) is True
    assert label_matches_family(GROK_LABEL, ex_mod.LABEL_FAMILY) is False
    assert label_matches_family(VP_LABEL, VP_LABEL_FAMILY) is True
    assert label_matches_family(VP_LABEL, ex_mod.LABEL_FAMILY) is False
    assert label_matches_family(NEW_FABLE_LABEL, ex_mod.LABEL_FAMILY) is True
    assert label_matches_family(NEW_FABLE_LABEL, GROK_LABEL_FAMILY) is False


# ---------------------------------------------------------------------------
# (f) version sanitization + length cap
# ---------------------------------------------------------------------------


def test_sanitize_label_version_collapses_invalid_characters():
    assert sanitize_label_version("v1.7-selective-edge") == "v1.7-selective-edge"
    assert sanitize_label_version("v1 8 size") == "v1-8-size"
    assert sanitize_label_version("v1:8:colon") == "v1-8-colon"
    # Thai characters (and other non-ASCII) are entirely outside
    # [A-Za-z0-9._-] and must collapse to a single separator, never raise
    # and never smuggle non-ASCII into a broker-facing label.
    assert sanitize_label_version("รุ่น1.8") == "1.8" or sanitize_label_version("รุ่น1.8").isascii()
    assert sanitize_label_version("").isascii()
    assert sanitize_label_version("   ") == "unknown"
    assert sanitize_label_version(None) == "unknown"


def test_build_versioned_label_caps_total_length():
    family = "dexter3:fable"
    long_version = "v" * 200
    label = build_versioned_label(family, long_version, max_len=60)
    assert len(label) <= 60
    assert label.startswith(family + ":")


def test_default_fable_label_is_under_the_openapi_truncation_ceiling():
    """dexter3.openapi_client truncates any incoming label to 64 chars before
    it reaches the broker (label=str(label or "")[:64]) — the assembled
    default LABEL must stay comfortably under that with real-world version
    strings (confirmed against the deployed ops/*.service values)."""
    for version in ("v1.7-selective-edge", "v1.8-size-the-edge"):
        label = build_versioned_label(ex_mod.LABEL_FAMILY, version)
        assert len(label) <= 64
        assert len(label) <= ex_mod.MAX_LABEL_LEN


# ---------------------------------------------------------------------------
# (g) H4's account-wide open-risk cap is unaffected (still counts every family)
# ---------------------------------------------------------------------------


class _H4FakeMcp(FakeMcp):
    def __init__(self, *, positions: list[dict] | None = None) -> None:
        super().__init__()
        self._positions = positions or []


def _foreign_position(*, volume: float, label: str) -> dict:
    return {
        "positionId": 900, "symbolName": "BTCUSD", "tradeSide": "Buy",
        "volumeInUnits": volume, "entryPrice": 62000.0, "stopLoss": 61900.0,
        "takeProfit": 62150.0, "label": label,
    }


def test_account_open_risk_cap_still_counts_a_new_version_fable_label(journal, monkeypatch):
    """H4's cap keys off label.startswith('dexter3') (untouched by this
    change) — prove a NEW-version fable label (not just the old m5h-v1
    literal) still counts toward the combined open-risk ceiling."""
    monkeypatch.delenv("DEXTER3_ACCOUNT_MAX_OPEN_RISK_USD", raising=False)
    mcp = _H4FakeMcp(positions=[_foreign_position(volume=0.3, label=NEW_FABLE_LABEL)])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=15.0))
    result = ex._account_open_risk_cap_refusal("BTCUSD", mcp.get_positions(), 15.0)
    # 100pt x 0.3 = $30 existing + $15 planned = $45 > default $40 cap.
    assert result is not None
    assert result["reason"] == "account_open_risk_cap"


def test_account_open_risk_cap_still_counts_a_vp_labeled_position(journal, monkeypatch):
    monkeypatch.delenv("DEXTER3_ACCOUNT_MAX_OPEN_RISK_USD", raising=False)
    mcp = _H4FakeMcp(positions=[_foreign_position(volume=0.3, label=VP_LABEL)])
    ex = Dexter3Executor(mcp, journal, ExecutorConfig(risk_usd=15.0))
    result = ex._account_open_risk_cap_refusal("BTCUSD", mcp.get_positions(), 15.0)
    assert result is not None
    assert result["reason"] == "account_open_risk_cap"
