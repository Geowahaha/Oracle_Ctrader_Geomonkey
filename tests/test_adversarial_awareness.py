"""Tests for AdversarialAwareness — psychology tagging + cool-down patterns.

Anchored to the 2026-05-18/19 incident data so each test reflects a real
loss pattern the system saw in live trading.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analysis.adversarial_awareness import (
    AdversarialAwareness,
    AdversarialAwarenessConfig,
    CloseEvent,
    PsychologyTag,
    PsychologyTagger,
)


_T0 = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)


def _clock_factory(start: datetime):
    state = {"now": start}

    def now():
        return state["now"]

    def advance(seconds: float):
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return now, advance


def _ev(pid: int, pnl: float, dir_: str = "short", source: str = "scalp_xauusd",
        realised_r: float = 0.0, mfe_r: float = 0.0, mae_r: float = 0.0,
        closed_at: datetime = _T0) -> CloseEvent:
    return CloseEvent(
        position_id=pid, direction=dir_, source=source,
        pnl_usd=pnl, realised_r=realised_r, mfe_r=mfe_r, mae_r=mae_r,
        closed_utc=closed_at,
    )


# ---- PsychologyTagger -----------------------------------------------------

def test_tagger_win_normal():
    t = PsychologyTagger()
    assert t.tag(_ev(1, pnl=24.46, realised_r=0.60, mfe_r=0.70)) == PsychologyTag.WIN_NORMAL


def test_tagger_win_cut_early_matches_incident():
    """pid=621794184: pnl=+9.47 realised 0.18R mfe 2.03R → WIN_CUT_EARLY."""
    t = PsychologyTagger()
    assert t.tag(_ev(621794184, pnl=9.47, realised_r=0.18, mfe_r=2.03)) == PsychologyTag.WIN_CUT_EARLY


def test_tagger_panic_close_noise_matches_incident():
    """pid=621704468: -$7.84, realised -0.07R, mae 0.10R → PANIC_CLOSE_NOISE."""
    t = PsychologyTagger()
    assert t.tag(_ev(621704468, pnl=-7.84, realised_r=-0.07, mae_r=0.10)) == PsychologyTag.PANIC_CLOSE_NOISE


def test_tagger_mfe_giveback_matches_incident():
    """pid=621968762: -$82.53, realised -1.01R (full SL hit), mfe 1.05R → MFE_GIVEBACK."""
    t = PsychologyTagger()
    # realised < -0.5 AND mfe > 1.0 triggers MFE_GIVEBACK before STOP_HUNT_FULL_SL.
    tag = t.tag(_ev(621968762, pnl=-82.53, realised_r=-0.95, mfe_r=1.05))
    assert tag == PsychologyTag.MFE_GIVEBACK


def test_tagger_stop_hunt_full_sl():
    t = PsychologyTagger()
    assert t.tag(_ev(99, pnl=-50.0, realised_r=-1.01, mae_r=1.01)) == PsychologyTag.STOP_HUNT_FULL_SL


def test_tagger_wrong_direction():
    t = PsychologyTagger()
    # realised -0.7R, mae 1.3R, mfe ~0 → wrong direction (went straight against)
    tag = t.tag(_ev(7, pnl=-50.0, realised_r=-0.70, mae_r=1.30, mfe_r=0.05))
    assert tag == PsychologyTag.WRONG_DIRECTION


def test_tagger_pre_tagged_passes_through():
    t = PsychologyTagger()
    ev = CloseEvent(
        position_id=1, direction="short", source="x", pnl_usd=-10.0,
        realised_r=-0.5, mfe_r=0.5, mae_r=0.5, closed_utc=_T0,
        pre_tagged="CUSTOM_TAG",
    )
    assert t.tag(ev) == "CUSTOM_TAG"


# ---- AdversarialAwareness — pattern detection -----------------------------

def test_disabled_returns_no_directives():
    a = AdversarialAwareness(config=AdversarialAwarenessConfig(enabled=False))
    a.record_close(_ev(1, pnl=-50.0, realised_r=-1.0))
    assert a.evaluate() == []


def test_revenge_pattern_blocks_same_direction():
    """3 consecutive short losses → block short for cooldown_minutes.

    Uses non-stop-hunt loss shape (realised_r -0.5, mae_r 0.6) so the
    revenge detector triggers in isolation.
    """
    clock, advance = _clock_factory(_T0)
    a = AdversarialAwareness(
        config=AdversarialAwarenessConfig(
            enabled=True, revenge_loss_count=3, revenge_window_minutes=60,
            revenge_cooldown_minutes=30,
            hunt_cluster_count=99, panic_cluster_count=99,
            drawdown_window_count=99,
        ),
        clock=clock,
    )
    base = _T0
    for i, t in enumerate([0, 600, 1200]):  # 0, 10, 20 min
        a.record_close(_ev(1000 + i, pnl=-30.0, realised_r=-0.5, mae_r=0.6,
                            closed_at=base + timedelta(seconds=t)))
    advance(1200)
    directives = a.evaluate()
    assert len(directives) >= 1
    revenge = next((d for d in directives if "direction:short" in d.scope), None)
    assert revenge is not None
    blocked, why = a.is_blocked(direction="short", source="scalp_xauusd")
    assert blocked is True
    blocked_long, _ = a.is_blocked(direction="long", source="scalp_xauusd")
    assert blocked_long is False


def test_winning_trade_clears_revenge_block():
    clock, advance = _clock_factory(_T0)
    a = AdversarialAwareness(
        config=AdversarialAwarenessConfig(
            enabled=True, revenge_loss_count=3, revenge_window_minutes=60,
            revenge_cooldown_minutes=30,
            hunt_cluster_count=99, panic_cluster_count=99,
            drawdown_window_count=99,
        ),
        clock=clock,
    )
    for i in range(3):
        a.record_close(_ev(2000 + i, pnl=-30.0, realised_r=-0.5, mae_r=0.6,
                            closed_at=_T0 + timedelta(minutes=i * 5)))
    a.evaluate()
    assert a.is_blocked(direction="short", source="x")[0] is True
    # Now a winner same direction clears it.
    advance(60)
    a.record_close(_ev(2999, pnl=+50.0, realised_r=1.0, closed_at=clock()))
    a.evaluate(now=clock())
    blocked, why = a.is_blocked(direction="short", source="x", now=clock())
    assert blocked is False


def test_hunt_cluster_blocks_global():
    """3 STOP_HUNT_FULL_SL within 90min → global cool-down."""
    clock, _ = _clock_factory(_T0)
    a = AdversarialAwareness(
        config=AdversarialAwarenessConfig(
            enabled=True, hunt_cluster_count=3, hunt_cluster_window_minutes=90,
            hunt_cooldown_minutes=20,
        ),
        clock=clock,
    )
    for i in range(3):
        # full SL hit: realised_r -1.0, mae 1.0
        a.record_close(_ev(3000 + i, pnl=-80.0, realised_r=-1.0, mae_r=1.0,
                            closed_at=_T0 + timedelta(minutes=i * 20)))
    a.evaluate(now=_T0 + timedelta(hours=1))
    blocked, why = a.is_blocked(direction="long", source="x", now=_T0 + timedelta(hours=1))
    assert blocked is True
    assert "global" in why or "hunt_cluster" in why


def test_panic_cluster_blocks_source():
    """4 consecutive PANIC_CLOSE_NOISE same source → block that source.

    Setting revenge_loss_count high so we isolate panic detection.
    """
    clock, _ = _clock_factory(_T0)
    a = AdversarialAwareness(
        config=AdversarialAwarenessConfig(
            enabled=True,
            revenge_loss_count=99, hunt_cluster_count=99,
            drawdown_window_count=99,
            panic_cluster_count=4, panic_cluster_window_minutes=60,
            panic_cooldown_minutes=15,
        ),
        clock=clock,
    )
    src = "scalp_xauusd"
    for i in range(4):
        a.record_close(_ev(4000 + i, pnl=-10.0, realised_r=-0.10, mae_r=0.20,
                            source=src,
                            closed_at=_T0 + timedelta(minutes=i * 8)))
    a.evaluate(now=_T0 + timedelta(minutes=40))
    blocked, why = a.is_blocked(direction="short", source=src,
                                now=_T0 + timedelta(minutes=40))
    assert blocked is True
    assert src in why
    # Other source is fine.
    ok_blocked, _ = a.is_blocked(direction="short", source="xauusd_scheduled",
                                  now=_T0 + timedelta(minutes=40))
    assert ok_blocked is False


def test_drawdown_burst_blocks_global():
    """Last 8 trades net -$250 → global pause."""
    clock, _ = _clock_factory(_T0)
    a = AdversarialAwareness(
        config=AdversarialAwarenessConfig(
            enabled=True, drawdown_window_count=8,
            drawdown_threshold_usd=-200.0, drawdown_cooldown_minutes=60,
        ),
        clock=clock,
    )
    for i in range(8):
        a.record_close(_ev(5000 + i, pnl=-40.0, realised_r=-0.8,
                            closed_at=_T0 + timedelta(minutes=i * 6)))
    a.evaluate(now=_T0 + timedelta(hours=1))
    blocked, why = a.is_blocked(direction="long", source="x",
                                 now=_T0 + timedelta(hours=1))
    assert blocked is True
    assert "drawdown" in why


def test_cooldown_expires_naturally():
    clock, advance = _clock_factory(_T0)
    a = AdversarialAwareness(
        config=AdversarialAwarenessConfig(
            enabled=True, revenge_loss_count=3, revenge_window_minutes=60,
            revenge_cooldown_minutes=10,
            hunt_cluster_count=99, panic_cluster_count=99,
            drawdown_window_count=99,
        ),
        clock=clock,
    )
    for i in range(3):
        a.record_close(_ev(6000 + i, pnl=-20.0, realised_r=-0.5, mae_r=0.6,
                            closed_at=_T0 + timedelta(minutes=i * 2)))
    a.evaluate()
    assert a.is_blocked(direction="short", source="x")[0] is True
    # Advance past both the cooldown (10min) AND the revenge window (60min)
    # so the historical events no longer match the pattern on re-eval.
    advance(75 * 60)
    blocked, _ = a.is_blocked(direction="short", source="x")
    assert blocked is False


def test_snapshot_returns_tag_distribution():
    a = AdversarialAwareness(config=AdversarialAwarenessConfig(enabled=True))
    a.record_close(_ev(1, pnl=10.0, realised_r=0.5))
    a.record_close(_ev(2, pnl=-5.0, realised_r=-0.1, mae_r=0.2))
    snap = a.snapshot()
    assert snap["events_total"] == 2
    assert PsychologyTag.WIN_NORMAL in snap["tag_counts"]
    assert PsychologyTag.PANIC_CLOSE_NOISE in snap["tag_counts"]
