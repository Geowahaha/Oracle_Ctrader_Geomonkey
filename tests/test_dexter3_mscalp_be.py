"""Pins the MSCALP-BE twin #3 (owner order 2026-08-06): identical entries,
BE-amend at the deadline instead of the T15 flatten. Label isolation
(hyphen-family must NOT collide with dexter3:mscalp), off-by-default flags,
and the pure BE-amend decision."""
from __future__ import annotations

from dexter3 import mscalp as ms


def _pos(side="BUY", entry=29800.0, sl=29760.0, open_ts="2026-08-06T01:00:00Z",
         pid=9, floating=4.0):
    return {"positionId": pid, "tradeSide": side, "entryPrice": entry,
            "stopLoss": sl, "openTimestamp": open_ts, "netProfit": floating}


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


def test_be_action_waits_for_deadline():
    pos = _pos()
    assert ms.mscalp_be_action(pos, _epoch("2026-08-06T01:14:59Z"), 900.0) is None
    assert ms.mscalp_be_action(pos, _epoch("2026-08-06T01:15:00Z"), 900.0) == ("amend", 29800.0)


def test_be_action_underwater_closes_instead_of_amending():
    # measured live 2026-08-06 01:39Z: the broker REJECTS an entry-level SL
    # on the losing side — the honest deadline action for a loser is the
    # market close (the corrected sweep's BEW cell).
    loser = _pos(floating=-12.1)
    assert ms.mscalp_be_action(loser, _epoch("2026-08-06T01:30:00Z"), 900.0) == ("close", -12.1)
    # epoch-millis openTimestamp (the broker's real payload shape) parses too
    millis = _pos(floating=-3.0)
    millis["openTimestamp"] = 1785979437172  # 2026-08-06T01:23:57Z
    assert ms.mscalp_be_action(millis, 1785979437.172 + 900.0, 900.0) == ("close", -3.0)


def test_be_action_idempotent_once_at_breakeven():
    # buy already BE (or better) -> None forever
    assert ms.mscalp_be_action(_pos(sl=29800.0), _epoch("2026-08-06T01:30:00Z"), 900.0) is None
    assert ms.mscalp_be_action(_pos(sl=29810.0), _epoch("2026-08-06T01:30:00Z"), 900.0) is None
    # sell: BE means sl <= entry
    sell = _pos(side="SELL", entry=29800.0, sl=29840.0)
    assert ms.mscalp_be_action(sell, _epoch("2026-08-06T01:30:00Z"), 900.0) == ("amend", 29800.0)
    sell_be = _pos(side="SELL", entry=29800.0, sl=29800.0)
    assert ms.mscalp_be_action(sell_be, _epoch("2026-08-06T01:30:00Z"), 900.0) is None


def test_be_action_guards():
    # disabled deadline / unknown open time / missing entry / unknown side /
    # unreadable PnL -> never act blind
    assert ms.mscalp_be_action(_pos(), _epoch("2026-08-06T02:00:00Z"), 0.0) is None
    no_ts = _pos(); no_ts["openTimestamp"] = ""
    assert ms.mscalp_be_action(no_ts, _epoch("2026-08-06T02:00:00Z"), 900.0) is None
    no_entry = _pos(entry=0.0)
    assert ms.mscalp_be_action(no_entry, _epoch("2026-08-06T02:00:00Z"), 900.0) is None
    odd = _pos(); odd["tradeSide"] = ""
    assert ms.mscalp_be_action(odd, _epoch("2026-08-06T02:00:00Z"), 900.0) is None
    blind = _pos(); blind.pop("netProfit")
    assert ms.mscalp_be_action(blind, _epoch("2026-08-06T02:00:00Z"), 900.0) is None
    # missing SL (0.0) on a WINNER after deadline still amends -> BE beats naked
    naked = _pos(sl=0.0)
    assert ms.mscalp_be_action(naked, _epoch("2026-08-06T02:00:00Z"), 900.0) == ("amend", 29800.0)


def test_be_suffix_fork_families_stay_disjoint():
    # 2026-08-07: the BASE+BEW unit forks the family with "-base"; that is NOT
    # a "-v<digit>" version token, so neither family may claim the other's
    # positions (the dpull vs dpull-cs contamination class).
    from dexter3.executor import label_matches_family

    assert label_matches_family("dexter3:mscalp-be-base:canary", "dexter3:mscalp-be-base") is True
    assert label_matches_family("dexter3:mscalp-be-base:canary", "dexter3:mscalp-be") is False
    assert label_matches_family("dexter3:mscalp-be:canary", "dexter3:mscalp-be-base") is False
    assert label_matches_family("dexter3:mscalp-be-base:canary", "dexter3:mscalp") is False


def test_be_suffix_env_forks_label_state_lock_log(monkeypatch):
    import importlib

    import dexter3.mscalp as m
    import dexter3.shadow_runner as sr

    monkeypatch.setenv("DEXTER3_MSCALP_BE_SUFFIX", "base")
    try:
        importlib.reload(m)
        importlib.reload(sr)
        assert m.MSCALP_BE_LABEL_FAMILY == "dexter3:mscalp-be-base"
        assert m.MSCALP_BE_LABEL == "dexter3:mscalp-be-base:canary"
        assert sr.MSCALP_BE_STATE_FILE.name == "dexter3_mscalp_be_base_shadow_state.json"
        assert sr.MSCALP_BE_LOG_FILE.name == "dexter3_mscalp_be_base_shadow.log"
        assert sr.MSCALP_BE_LOCK_FILE.name == "dexter3_mscalp_be_base_shadow.lock"
    finally:
        monkeypatch.delenv("DEXTER3_MSCALP_BE_SUFFIX", raising=False)
        importlib.reload(m)
        importlib.reload(sr)
    # absent suffix restores the original lane's names byte-for-byte
    assert m.MSCALP_BE_LABEL == "dexter3:mscalp-be:canary"
    assert sr.MSCALP_BE_STATE_FILE.name == "dexter3_mscalp_be_shadow_state.json"


def test_lane_tally_maps_the_be_fork():
    from ops.dexter3_lane_tally import _lane_family

    assert _lane_family("dexter3:mscalp-be-base:canary") == "mscalp-be-base"
    assert _lane_family("dexter3:mscalp-be:canary") == "mscalp-be"
