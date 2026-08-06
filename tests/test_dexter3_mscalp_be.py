"""Pins the MSCALP-BE twin #3 (owner order 2026-08-06): identical entries,
BE-amend at the deadline instead of the T15 flatten. Label isolation
(hyphen-family must NOT collide with dexter3:mscalp), off-by-default flags,
and the pure BE-amend decision."""
from __future__ import annotations

from dexter3 import mscalp as ms


def _pos(side="BUY", entry=29800.0, sl=29760.0, open_ts="2026-08-06T01:00:00Z", pid=9):
    return {"positionId": pid, "tradeSide": side, "entryPrice": entry,
            "stopLoss": sl, "openTimestamp": open_ts}


def _epoch(ts: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def test_mode_flags_independent(monkeypatch):
    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert ms.mscalp_be_mode_enabled() is False
    monkeypatch.setenv("DEXTER3_MODE", "mscalp-be")
    assert ms.mscalp_be_mode_enabled() is True
    assert ms.mscalp_mode_enabled() is False
    assert ms.mscalp2_mode_enabled() is False


def test_be_deadline_env_gated_off_by_default(monkeypatch):
    monkeypatch.delenv(ms.ENV_MSCALP_BE_SEC, raising=False)
    assert ms.mscalp_be_deadline_sec() == 0.0
    monkeypatch.setenv(ms.ENV_MSCALP_BE_SEC, "900")
    assert ms.mscalp_be_deadline_sec() == 900.0


def test_label_isolation_hyphen_family_never_collides():
    from dexter3.executor import label_matches_family

    assert ms.MSCALP_BE_LABEL == "dexter3:mscalp-be:canary"
    assert label_matches_family(ms.MSCALP_BE_LABEL, "dexter3:mscalp-be") is True
    # '-be' is not a '-v<digit>' version token -> the ORIGINAL family must
    # NOT claim the twin's positions (the dpull vs dpull-cs 2026-07-25 bug)
    assert label_matches_family(ms.MSCALP_BE_LABEL, "dexter3:mscalp") is False
    assert label_matches_family(ms.MSCALP_LABEL, "dexter3:mscalp-be") is False
    assert label_matches_family(ms.MSCALP2_LABEL, "dexter3:mscalp-be") is False


def test_runner_resolvers(monkeypatch):
    import dexter3.shadow_runner as sr

    monkeypatch.setenv("DEXTER3_MODE", "mscalp-be")
    assert sr._active_order_label() == "dexter3:mscalp-be:canary"
    assert sr._active_label_family() == "dexter3:mscalp-be"
    assert sr._active_state_file().name == "dexter3_mscalp_be_shadow_state.json"
    assert sr._active_log_file().name == "dexter3_mscalp_be_shadow.log"
    assert sr._mscalp_be_producer_enabled() is True
    assert sr._mscalp_producer_enabled() is False
    assert sr._mscalp2_producer_enabled() is False
    assert sr._alt_producer_enabled() is True


def test_lane_tally_family_split():
    from ops.dexter3_lane_tally import _lane_family

    assert _lane_family("dexter3:mscalp-be:canary") == "mscalp-be"
    assert _lane_family("dexter3:mscalp:canary") == "mscalp"
    assert _lane_family("dexter3:mscalp2:canary") == "mscalp2"


def test_be_amend_waits_for_deadline():
    pos = _pos()
    before = ms.mscalp_be_amend_needed(pos, _epoch("2026-08-06T01:14:59Z"), 900.0)
    assert before is None
    at = ms.mscalp_be_amend_needed(pos, _epoch("2026-08-06T01:15:00Z"), 900.0)
    assert at == 29800.0  # amend SL -> entry


def test_be_amend_idempotent_once_at_breakeven():
    # buy already BE (or better) -> no re-amend, ever
    assert ms.mscalp_be_amend_needed(_pos(sl=29800.0), _epoch("2026-08-06T01:30:00Z"), 900.0) is None
    assert ms.mscalp_be_amend_needed(_pos(sl=29810.0), _epoch("2026-08-06T01:30:00Z"), 900.0) is None
    # sell: BE means sl <= entry
    sell = _pos(side="SELL", entry=29800.0, sl=29840.0)
    assert ms.mscalp_be_amend_needed(sell, _epoch("2026-08-06T01:30:00Z"), 900.0) == 29800.0
    sell_be = _pos(side="SELL", entry=29800.0, sl=29800.0)
    assert ms.mscalp_be_amend_needed(sell_be, _epoch("2026-08-06T01:30:00Z"), 900.0) is None


def test_be_amend_guards():
    # disabled deadline / unknown open time / missing entry / unknown side
    assert ms.mscalp_be_amend_needed(_pos(), _epoch("2026-08-06T02:00:00Z"), 0.0) is None
    no_ts = _pos(); no_ts["openTimestamp"] = ""
    assert ms.mscalp_be_amend_needed(no_ts, _epoch("2026-08-06T02:00:00Z"), 900.0) is None
    no_entry = _pos(entry=0.0)
    assert ms.mscalp_be_amend_needed(no_entry, _epoch("2026-08-06T02:00:00Z"), 900.0) is None
    odd = _pos(); odd["tradeSide"] = ""
    assert ms.mscalp_be_amend_needed(odd, _epoch("2026-08-06T02:00:00Z"), 900.0) is None
    # missing SL (0.0) after deadline still amends -> BE is safer than naked
    naked = _pos(sl=0.0)
    assert ms.mscalp_be_amend_needed(naked, _epoch("2026-08-06T02:00:00Z"), 900.0) == 29800.0
