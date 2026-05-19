"""Tests for the Breathing Room filter."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from execution.breathing_room import (
    BreathingRoomConfig,
    BreathingRoomFilter,
    FilterDecision,
    PositionRoomState,
)


_T0 = datetime(2026, 5, 18, 13, 0, 0, tzinfo=timezone.utc)


def _clock_factory(start: datetime):
    state = {"now": start}

    def now() -> datetime:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return now, advance


def _state(
    *,
    pid: int = 621704468,
    direction: str = "short",
    entry: float = 4571.40,
    current: float = 4571.91,
    sl: float = 4578.63,
    age_min: float = 5.0,
    base: datetime = _T0,
) -> PositionRoomState:
    first_seen = (base - timedelta(minutes=age_min)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return PositionRoomState(
        position_id=pid,
        direction=direction,
        entry_price=entry,
        current_price=current,
        original_stop_loss=sl,
        current_stop_loss=sl,
        first_seen_utc=first_seen,
    )


@dataclass
class _FakeDirective:
    action: str
    position_id: int = 0


def test_disabled_passes_everything():
    f = BreathingRoomFilter(config=BreathingRoomConfig(enabled=False))
    decision = f.evaluate_position(_state())
    assert decision.blocked is False
    assert decision.reason == "disabled"


def test_blocks_close_on_young_position_with_no_mae():
    """The smoking-gun pattern: short opened 5 min ago, 0.5pt against,
    guardian wants to close. Should be BLOCKED — needs breathing room."""
    clock, _ = _clock_factory(_T0)
    f = BreathingRoomFilter(
        config=BreathingRoomConfig(
            enabled=True, min_breathing_minutes=10.0, require_mae_r=0.6,
        ),
        clock=clock,
    )
    state = _state(age_min=5.0, entry=4571.40, current=4571.91, sl=4578.63)
    # MAE = (4571.91 - 4571.40) / (4578.63 - 4571.40) = 0.51/7.23 = 0.071R << 0.6
    # Age = 5min < 10min
    # Filter should BLOCK the close.
    d = f.evaluate_position(state)
    assert d.blocked is True
    assert "give_room" in d.reason


def test_allows_close_when_all_conditions_proven():
    """Old position + decisively losing + no MFE → allow close."""
    clock, _ = _clock_factory(_T0)
    f = BreathingRoomFilter(
        config=BreathingRoomConfig(
            enabled=True, min_breathing_minutes=10.0,
            require_mae_r=0.6, allow_mfe_r=0.2,
        ),
        clock=clock,
    )
    # 15 min old, 5pt against on 7pt risk = MAE 0.71R, no MFE → all proven
    state = _state(age_min=15.0, entry=4571.40, current=4576.55, sl=4578.63)
    d = f.evaluate_position(state)
    assert d.blocked is False
    assert d.reason == "all_proven_allow_close"


def test_blocks_when_position_has_positive_mfe_peak():
    """Even old trade with positive MFE peak should keep its room — let
    MFE Progressive Trail handle the lock-in instead of force-closing."""
    clock, advance = _clock_factory(_T0)
    f = BreathingRoomFilter(
        config=BreathingRoomConfig(
            enabled=True, min_breathing_minutes=5.0,
            require_mae_r=0.6, allow_mfe_r=0.2,
        ),
        clock=clock,
    )
    # First call: short up by 2pt favourable → records peak MFE 0.28R
    state_good = _state(age_min=6.0, entry=4571.40, current=4569.40, sl=4578.63)
    f.evaluate_position(state_good)
    # Second call (later): now back down to entry, MAE 0.6R reached.
    advance(120.0)
    state_now = _state(age_min=8.0, entry=4571.40, current=4575.74, sl=4578.63)
    d = f.evaluate_position(state_now)
    assert d.blocked is True  # peak MFE 0.28 > 0.20 threshold → blocked
    assert "mfe_proven=N" in d.reason


def test_directive_filter_drops_close_position_keeps_others():
    clock, _ = _clock_factory(_T0)
    f = BreathingRoomFilter(
        config=BreathingRoomConfig(
            enabled=True, min_breathing_minutes=10.0, require_mae_r=0.6,
        ),
        clock=clock,
    )
    state = _state()
    directives = [
        _FakeDirective(action="close_position", position_id=state.position_id),
        _FakeDirective(action="hold_position", position_id=state.position_id),
        _FakeDirective(action="partial_close", position_id=state.position_id),
        _FakeDirective(action="amend_position", position_id=state.position_id),
    ]
    kept, blocked = f.filter_directives(
        directives=directives,
        position_states={state.position_id: state},
    )
    actions_kept = sorted(d.action for d in kept)
    assert actions_kept == ["amend_position", "hold_position", "partial_close"]
    assert len(blocked) == 1
    assert blocked[0].blocked is True


def test_filter_passes_directives_when_disabled():
    f = BreathingRoomFilter(config=BreathingRoomConfig(enabled=False))
    directives = [
        _FakeDirective(action="close_position", position_id=1),
    ]
    kept, blocked = f.filter_directives(
        directives=directives, position_states={},
    )
    assert len(kept) == 1
    assert len(blocked) == 0


def test_filter_keeps_directive_when_position_state_missing():
    """Safety: if we don't have the position context, don't filter — let
    the original directive through. Better to over-close than to silently
    swallow a directive we can't evaluate."""
    clock, _ = _clock_factory(_T0)
    f = BreathingRoomFilter(
        config=BreathingRoomConfig(enabled=True), clock=clock,
    )
    directives = [_FakeDirective(action="close_position", position_id=999)]
    kept, blocked = f.filter_directives(directives=directives, position_states={})
    assert len(kept) == 1
    assert len(blocked) == 0


def test_filter_long_direction_uses_correct_signs():
    clock, _ = _clock_factory(_T0)
    f = BreathingRoomFilter(
        config=BreathingRoomConfig(
            enabled=True, min_breathing_minutes=10.0, require_mae_r=0.6,
        ),
        clock=clock,
    )
    # Long entry 4540, current 4539 → MAE 1pt on 7pt risk = 0.14R << 0.6
    state = PositionRoomState(
        position_id=42, direction="long",
        entry_price=4540.0, current_price=4539.0,
        original_stop_loss=4533.0, current_stop_loss=4533.0,
        first_seen_utc=(_T0 - timedelta(minutes=3)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    d = f.evaluate_position(state)
    assert d.blocked is True


def test_filter_allows_old_long_with_decisive_mae():
    clock, _ = _clock_factory(_T0)
    f = BreathingRoomFilter(
        config=BreathingRoomConfig(
            enabled=True, min_breathing_minutes=10.0,
            require_mae_r=0.6, allow_mfe_r=0.2,
        ),
        clock=clock,
    )
    # 30min old long, 5pt against on 7pt risk = MAE 0.71R, no MFE → allow close
    state = PositionRoomState(
        position_id=42, direction="long",
        entry_price=4540.0, current_price=4534.95,
        original_stop_loss=4533.0, current_stop_loss=4533.0,
        first_seen_utc=(_T0 - timedelta(minutes=30)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    d = f.evaluate_position(state)
    assert d.blocked is False
