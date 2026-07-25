"""vp-funnel instrumentation tests (2026-07-25).

Proves the observability added to answer "155 enter decisions -> 6 broker
fills, vp_poc_reversion 72-for-0":
  * every downstream branch maps to a stable (outcome, reason) bucket;
  * the intent-fate stamp MERGES (never destroys the gate evidence the
    post-gate resync wrote) — the one real corruption risk here;
  * stamping is pure observability: no sizing/gating/order field is touched.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import dexter3.shadow_runner as sr
from dexter3.decision_journal import DecisionJournal
from dexter3.hunter_brain import Decision


def _decision(**kw: Any) -> Decision:
    base = dict(
        ts_close="2026-07-25T12:00:00Z", symbol="XAUUSD", action="enter", side="buy",
        entry_type="market", entry=4000.0, sl=3990.0, tp=4020.0, size_class="small",
        leader_score=0.5, p_win_est=0.5, setup="vp_poc_reversion", reasons=[],
        features={}, session="ny",
    )
    base.update(kw)
    return Decision(**base)


# --- outcome classification (every live branch) ----------------------------

@pytest.mark.parametrize("status,expected", [
    ("decided:enter:vp_lvn_rejection:live_entered", ("filled", "entered")),
    ("decided:enter:vp_poc_reversion:vp_limit_intent_set", ("limit_intent", "intent_set")),
    ("decided:enter:x:live_blocked_vp_dayopen_bias", ("blocked", "vp_dayopen_bias")),
    ("decided:enter:x:live_blocked_vp_no_trade_window", ("blocked", "vp_no_trade_window")),
    ("decided:enter:x:live_blocked_governor_cap", ("blocked", "governor_cap")),
    ("decided:enter:x:governor_target_locked", ("blocked", "target_locked")),
    ("decided:enter:x:governor_loss_stopped", ("blocked", "loss_stopped")),
    ("decided:enter:x:live_skipped_lane_unverified", ("blocked", "lane_unverified")),
    ("decided:enter:x", ("no_exec", "no_downstream_branch")),
])
def test_classify_entry_outcome(status: str, expected: tuple[str, str]):
    assert sr._classify_entry_outcome(status) == expected


def test_bypassed_governor_is_not_counted_as_blocked():
    # ':governor_target_locked_bypassed' means the entry was ALLOWED through;
    # the later marker (the real fill) must win.
    status = "decided:enter:x:governor_target_locked_bypassed:live_entered"
    assert sr._classify_entry_outcome(status) == ("filled", "entered")


def test_late_suffix_does_not_corrupt_reason():
    status = "decided:enter:x:live_blocked_vp_dayopen_bias:late120s"
    assert sr._classify_entry_outcome(status) == ("blocked", "vp_dayopen_bias")


# --- stamping is additive + safe -------------------------------------------

def test_stamp_entry_outcome_preserves_existing_features():
    d = _decision(features={"v16_entry_quality": {"b_tier": True}, "vp_poc": 4001.0})
    sr._stamp_entry_outcome(d, "decided:enter:vp_poc_reversion:vp_limit_intent_set")
    assert d.features["v16_entry_quality"] == {"b_tier": True}
    assert d.features["vp_poc"] == 4001.0
    assert d.features["entry_outcome"]["outcome"] == "limit_intent"
    assert d.features["entry_outcome"]["setup"] == "vp_poc_reversion"


def test_stamp_entry_outcome_never_touches_trade_fields():
    d = _decision()
    before = (d.side, d.entry, d.sl, d.tp, d.size_class, d.leader_score, d.entry_type)
    sr._stamp_entry_outcome(d, "decided:enter:x:live_entered")
    assert (d.side, d.entry, d.sl, d.tp, d.size_class, d.leader_score, d.entry_type) == before


def test_stamp_entry_outcome_survives_garbage():
    d = _decision(features=None)  # not a dict -> must be a silent no-op
    sr._stamp_entry_outcome(d, "whatever")  # must not raise


# --- the corruption risk: fate stamp must MERGE ----------------------------

@pytest.fixture()
def journal(tmp_path: Path):
    j = DecisionJournal(tmp_path / "funnel.db")
    yield j
    j.close()


def test_merge_decision_features_preserves_gate_evidence(journal):
    d = _decision(features={"v16_entry_quality": {"b_tier": True}, "entry_outcome": {"outcome": "limit_intent"}})
    row_id = journal.insert_decision(d, label="dexter3:vp:canary")
    journal.merge_decision_features(row_id, {"intent_fate": {"fate": "replaced"}})
    got = journal.recent_decisions(limit=1)[0]["features"]
    assert got["v16_entry_quality"] == {"b_tier": True}      # NOT destroyed
    assert got["entry_outcome"]["outcome"] == "limit_intent"  # NOT destroyed
    assert got["intent_fate"]["fate"] == "replaced"           # patch landed


def test_merge_decision_features_unknown_id_is_noop(journal):
    journal.merge_decision_features(999999, {"intent_fate": {"fate": "expired"}})  # must not raise


def test_stamp_intent_fate_writes_through_journal(journal):
    d = _decision(features={"vp_poc": 4001.0})
    row_id = journal.insert_decision(d, label="dexter3:vp:canary")
    sr._stamp_intent_fate(journal, {"decision_row_id": row_id}, "expired", {"at": "2026-07-25T12:30:00Z"})
    got = journal.recent_decisions(limit=1)[0]["features"]
    assert got["intent_fate"]["fate"] == "expired"
    assert got["intent_fate"]["at"] == "2026-07-25T12:30:00Z"
    assert got["vp_poc"] == 4001.0


def test_stamp_intent_fate_is_noop_without_journal_or_row_id(journal):
    sr._stamp_intent_fate(None, {"decision_row_id": 1}, "expired")       # no journal
    sr._stamp_intent_fate(journal, {}, "expired")                        # no row id
    sr._stamp_intent_fate(journal, None, "expired")                      # no intent


def test_stamp_intent_fate_swallows_journal_errors():
    class _Boom:
        def merge_decision_features(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("db gone")
    sr._stamp_intent_fate(_Boom(), {"decision_row_id": 1}, "expired")  # must not raise
