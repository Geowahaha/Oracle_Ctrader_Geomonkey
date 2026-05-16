from execution.xau_profit_guardian import (
    BasketState,
    GuardianConfig,
    OrderState,
    PMDirective,
    PositionState,
    XAUProfitGuardian,
    XAUProfitGuardianDB,
    classify_trend_phase,
    direction_from_deal_and_position,
    snowball_capital_multiplier,
    validate_tp_sl_coherence,
)


def test_realized_direction_prefers_opening_position_direction_over_deal_direction():
    assert direction_from_deal_and_position(
        {"direction": "short"}, {"direction": "long"}
    ) == "long"
    assert direction_from_deal_and_position({"direction": "short"}, None) == "unknown"


def test_tp_sl_coherence_rejects_absurd_blind_sell_geometry():
    result = validate_tp_sl_coherence(
        direction="short",
        entry=4709.0,
        stop_loss=5670.62,
        take_profit=2663.43,
        atr=5.0,
    )
    assert not result.ok
    assert result.reason == "tp_distance_atr_out_of_range"


def test_tp_sl_coherence_allows_reasonable_xau_long_geometry():
    result = validate_tp_sl_coherence(
        direction="long",
        entry=4700.0,
        stop_loss=4688.0,
        take_profit=4736.0,
        atr=6.0,
    )
    assert result.ok
    assert result.rr == 3.0


def test_equity_ratchet_keeps_peak_floor_monotonic_and_triggers_harvest():
    guard = XAUProfitGuardian(GuardianConfig(mode="shadow", base_giveback_pct=0.10, hard_giveback_pct=0.25))
    previous = BasketState(symbol="XAUUSD", realized_today=224.0, unrealized_now=26.0, combined_pnl=250.0)
    previous = guard.update_ratchet(previous, previous=None, atr_percentile=0.5)
    assert previous.combined_peak == 250.0
    assert previous.ratchet_floor > 200.0

    current = BasketState(symbol="XAUUSD", realized_today=224.0, unrealized_now=-241.0, combined_pnl=-17.0)
    current = guard.update_ratchet(current, previous=previous, atr_percentile=0.5)
    assert current.combined_peak == 250.0
    assert current.ratchet_floor == previous.ratchet_floor
    assert current.tier_state == "T3_HARVEST"


def test_guardian_prunes_weak_late_cycle_adds_before_runner_core():
    guard = XAUProfitGuardian(GuardianConfig(mode="shadow", max_prune_positions=2))
    basket = BasketState(
        symbol="XAUUSD",
        realized_today=224.0,
        unrealized_now=-80.0,
        combined_pnl=144.0,
        combined_peak=250.0,
        ratchet_floor=220.0,
        tier_state="T1_PRUNE",
        trend_phase="DISTRIBUTION",
        hazard_score=45.0,
        positions=[
            PositionState(position_id=1, direction="long", entry=4647.5, current=4685.0, stop_loss=4620.0, take_profit=4790.0, volume=100, source="fibo_xauusd", mfe=67.0, mae=0.0, age_sec=7200),
            PositionState(position_id=2, direction="long", entry=4719.8, current=4685.0, stop_loss=4681.4, take_profit=4797.0, volume=100, source="fibo_xauusd", mfe=0.0, mae=34.8, age_sec=1800),
            PositionState(position_id=3, direction="long", entry=4713.0, current=4685.0, stop_loss=4681.3, take_profit=4797.0, volume=100, source="fibo_xauusd", mfe=0.0, mae=28.0, age_sec=1800),
        ],
    )
    directives = guard.evaluate(basket, orders=[])
    close_ids = [d.position_id for d in directives if d.action == "close_position"]
    assert close_ids == [2, 3]
    assert all(d.reason.startswith("weak_add_prune") for d in directives if d.action == "close_position")


def test_winner_long_reservoir_hour_permissions_preserve_runner_without_size_or_live_risk():
    guard = XAUProfitGuardian(
        GuardianConfig(
            mode="micro_live",
            max_prune_positions=2,
            winner_long_reservoir_hours=("h22",),
            winner_long_reservoir_min_r=-0.25,
        )
    )
    basket = BasketState(
        symbol="XAUUSD",
        tier_state="T1_PRUNE",
        trend_phase="DISTRIBUTION",
        positions=[
            PositionState(
                position_id=22,
                direction="long",
                entry=4700,
                current=4698,
                stop_loss=4690,
                take_profit=4730,
                volume=100,
                source="scalp_xauusd:winner",
                first_seen_utc="2026-05-16T22:15:00Z",
                mae=2,
                age_sec=1200,
            ),
            PositionState(
                position_id=16,
                direction="long",
                entry=4700,
                current=4698,
                stop_loss=4690,
                take_profit=4730,
                volume=100,
                source="scalp_xauusd:winner",
                first_seen_utc="2026-05-16T16:15:00Z",
                mae=2,
                age_sec=1200,
            ),
        ],
    )
    directives = guard.evaluate(basket, orders=[])
    assert [d.position_id for d in directives if d.action == "hold_position"] == [22]
    assert [d.position_id for d in directives if d.action == "close_position"] == [16]
    hold = next(d for d in directives if d.action == "hold_position")
    assert hold.live_allowed is False
    assert hold.metadata["size_multiplier"] == 1.0
    assert hold.metadata["risk_usd_delta"] == 0.0
    assert hold.metadata["execution_enabled"] is False


def test_t3_harvest_does_not_emit_partial_and_full_close_for_same_position():
    guard = XAUProfitGuardian(GuardianConfig(mode="full"))
    basket = BasketState(
        symbol="XAUUSD",
        tier_state="T3_HARVEST",
        positions=[PositionState(position_id=7, direction="long", entry=4600, current=4700, stop_loss=4590, take_profit=4800, volume=100, mfe=100)],
    )
    directives = guard.evaluate(basket, orders=[])
    position_actions = [(d.action, d.position_id) for d in directives if d.position_id == 7]
    assert position_actions == [("close_position", 7)]


def test_guardian_detects_blind_sell_order_without_broad_short_ban():
    guard = XAUProfitGuardian(GuardianConfig(mode="shadow"))
    basket = BasketState(symbol="XAUUSD", trend_phase="IMPULSE", trend_direction="long", hazard_score=25.0)
    orders = [
        OrderState(order_id=99, direction="short", entry=4709.0, stop_loss=5670.62, take_profit=2663.43, volume=100, source="scalp_xauusd"),
        OrderState(order_id=100, direction="short", entry=4709.0, stop_loss=4714.0, take_profit=4694.0, volume=100, source="scalp_xauusd"),
    ]
    directives = guard.evaluate(basket, orders=orders, atr=5.0)
    cancel_ids = [d.order_id for d in directives if d.action == "cancel_order"]
    assert cancel_ids == [99]


def test_stale_broker_truth_forces_hold_no_live_action():
    guard = XAUProfitGuardian(GuardianConfig(mode="full"))
    basket = BasketState(symbol="XAUUSD", data_fresh=False, data_status="stale_tick:999s", tier_state="T3_HARVEST")
    directives = guard.evaluate(basket, orders=[])
    assert [(d.action, d.live_allowed) for d in directives] == [("hold", False)]
    assert directives[0].reason.startswith("broker_truth_stale")


def test_snowball_capital_multiplier_uses_realized_profit_but_suppresses_hazard():
    calm = snowball_capital_multiplier(realized_today=220, combined_peak=250, hazard_score_value=10, regime_quality=0.8)
    hazard = snowball_capital_multiplier(realized_today=220, combined_peak=250, hazard_score_value=90, regime_quality=0.8)
    assert calm > 1.1
    assert hazard < calm


def test_trend_phase_classifier_separates_impulse_from_distribution():
    assert classify_trend_phase({"hh_hl_streak": 5, "delta_slope": 0.8, "atr_jump_percentile": 0.4, "volume_z": 0.7}) == "IMPULSE"
    assert classify_trend_phase({"hh_hl_streak": 5, "delta_slope": -0.6, "atr_jump_percentile": 0.6, "volume_z": -1.2}) == "DISTRIBUTION"


def test_guardian_db_persists_snapshot_action_journal_and_daily_equity_peak(tmp_path):
    db_path = tmp_path / "ctrader.db"
    runtime_path = tmp_path / "runtime" / "xau_basket_truth.json"
    guardian_db = XAUProfitGuardianDB(db_path, runtime_path, GuardianConfig(mode="shadow"))
    basket = BasketState(symbol="XAUUSD", realized_today=224, unrealized_now=26, combined_pnl=250, combined_peak=250, ratchet_floor=225, tier_state="NORMAL", trend_phase="IMPULSE")
    directives = [PMDirective(action="cancel_order", order_id=99, reason="blind_signal_schema:tp_distance_atr_out_of_range", live_allowed=False)]
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        XAUProfitGuardianDB.ensure_schema(conn)
    guardian_db.persist(basket, directives)
    assert runtime_path.exists()
    with sqlite3.connect(db_path) as conn:
        snap_count = conn.execute("SELECT COUNT(*) FROM xau_basket_snapshots").fetchone()[0]
        action_count = conn.execute("SELECT COUNT(*) FROM pm_action_journal").fetchone()[0]
        peak_row = conn.execute("SELECT scope, combined_peak FROM equity_peaks").fetchone()
    assert snap_count == 1
    assert action_count == 1
    assert peak_row[0].startswith("xau_session:")
    assert peak_row[1] == 250


def test_shadow_apply_live_directives_executes_nothing(tmp_path):
    db_path = tmp_path / "ctrader.db"
    guardian_db = XAUProfitGuardianDB(db_path, tmp_path / "rt.json", GuardianConfig(mode="shadow"))
    directive = PMDirective(action="close_position", position_id=1, volume=100, reason="test", live_allowed=True)
    class Executor:
        def close_position(self, **kwargs):
            raise AssertionError("shadow mode must not call executor")
    assert guardian_db.apply_live_directives(Executor(), [directive]) == []
