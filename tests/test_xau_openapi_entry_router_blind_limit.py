from analysis.signals import TradeSignal
from scheduler import DexterScheduler


def _signal(raw_scores=None):
    return TradeSignal(
        symbol="XAUUSD",
        direction="short",
        confidence=82.0,
        entry=4690.0,
        stop_loss=4698.0,
        take_profit_1=4686.0,
        take_profit_2=4682.0,
        take_profit_3=4676.0,
        risk_reward=3.0,
        timeframe="M1",
        session="new_york",
        trend="bearish",
        rsi=45.0,
        atr=4.0,
        pattern="scalp_xauusd:winner",
        raw_scores=dict(raw_scores or {}),
        entry_type="limit",
    )


def _weak_snapshot():
    return {
        "ok": True,
        "run_id": "unit-router",
        "gate": {
            "features": {
                "spread_avg_pct": 0.0002,
                "spread_expansion": 1.09,
                "delta_proxy": 0.20,
                "depth_imbalance": 0.0,
                "depth_refill_shift": 0.10,
                "rejection_ratio": 0.0,
                "bar_volume_proxy": 0.10,
                "tick_up_ratio": 0.50,
            }
        },
    }


def _strong_short_signal_snapshot():
    return {
        "ok": True,
        "run_id": "unit-router-strong",
        "gate": {
            "features": {
                "spread_avg_pct": 0.00018,
                "spread_expansion": 1.01,
                "delta_proxy": -0.22,
                "depth_imbalance": -0.05,
                "depth_refill_shift": -0.04,
                "rejection_ratio": 0.04,
                "bar_volume_proxy": 0.82,
                "tick_up_ratio": 0.34,
            }
        },
    }


def _patch_router_deps(monkeypatch, *, snapshot=None, state_label="range_noise", continuation_bias=0.0):
    monkeypatch.setattr("scheduler.live_profile_autopilot.latest_capture_feature_snapshot", lambda **kwargs: snapshot or _weak_snapshot())
    monkeypatch.setattr(
        "scheduler.live_profile_classify_chart_state",
        lambda direction, context, capture_features=None: {
            "state_label": state_label,
            "day_type": "trend",
            "continuation_bias": continuation_bias,
        },
    )


def test_router_converts_unanchored_pullback_limit_to_probe_stop_not_block(monkeypatch):
    _patch_router_deps(monkeypatch)
    sched = DexterScheduler.__new__(DexterScheduler)

    routed = sched._xau_openapi_entry_router(
        _signal(), family="xau_scalp_pullback_limit", preferred_entry_type="limit"
    )

    assert routed["blocked"] is False
    assert routed["entry_type"] == "sell_stop"
    assert routed["mode"] == "wait_break_probe_stop"
    assert routed["reason"] == "no_midair_limit,wait_for_break,probe_risk"
    assert routed["risk_multiplier"] == 0.2625
    assert "no_midair_limit" in routed["reasons"]
    assert routed["continuation_score"] < 4
    assert routed["absorption_score"] < 4


def test_router_keeps_limit_when_fibo_impulse_zone_confirms(monkeypatch):
    _patch_router_deps(monkeypatch)
    sched = DexterScheduler.__new__(DexterScheduler)
    sig = _signal(
        {
            "fibo_reclaim_confirmed": True,
            "fibo_cluster_count": 2,
            "impulse_state_direction": "short",
            "ratio_zone": "0.618",
        }
    )

    routed = sched._xau_openapi_entry_router(
        sig, family="xau_scalp_pullback_limit", preferred_entry_type="limit"
    )

    assert routed["blocked"] is False
    assert routed["entry_type"] == "limit"
    assert routed["mode"] == "zone_anchored_limit"
    assert "fibo_impulse_zone:cluster=2" in routed["zone_confluence"]["reasons"]


def test_router_keeps_limit_when_kronos_path_aligned(monkeypatch):
    _patch_router_deps(monkeypatch)
    sched = DexterScheduler.__new__(DexterScheduler)
    sig = _signal(
        {
            "xau_ohlcv_forecast_shadow": {
                "status": "ok",
                "forecast_direction": "short",
                "aligned_with_signal": True,
                "uncertainty": 0.32,
            }
        }
    )

    routed = sched._xau_openapi_entry_router(
        sig, family="xau_scalp_pullback_limit", preferred_entry_type="limit"
    )

    assert routed["blocked"] is False
    assert routed["mode"] == "zone_anchored_limit"
    assert routed["zone_confluence"]["reasons"] == ["kronos_path_aligned:unc=0.32"]


def test_router_uses_market_entry_when_signal_is_immediate_and_strong(monkeypatch):
    _patch_router_deps(
        monkeypatch,
        snapshot=_strong_short_signal_snapshot(),
        state_label="continuation_drive",
        continuation_bias=0.82,
    )
    monkeypatch.setattr("scheduler.config.XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_ENABLED", True, raising=False)
    monkeypatch.setattr("scheduler.config.XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE", 7, raising=False)
    monkeypatch.setattr("scheduler.config.XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_BIAS", 0.70, raising=False)
    sched = DexterScheduler.__new__(DexterScheduler)

    routed = sched._xau_openapi_entry_router(
        _signal({"candle_rejection_confirmed": True, "structure_break": True}),
        family="xau_scalp_pullback_limit",
        preferred_entry_type="limit",
    )

    assert routed["blocked"] is False
    assert routed["entry_type"] == "market"
    assert routed["mode"] == "signal_market"
    assert "signal_now" in routed["reasons"]


def test_router_maps_618_rejection_to_zone_sell_limit_action(monkeypatch):
    _patch_router_deps(monkeypatch)
    sched = DexterScheduler.__new__(DexterScheduler)
    sig = _signal(
        {
            "ratio_zone": "0.618",
            "impulse_state_direction": "short",
            "candle_rejection_confirmed": True,
            "dema_reclaim_state": "fail",
        }
    )

    routed = sched._xau_openapi_entry_router(
        sig, family="xau_scalp_pullback_limit", preferred_entry_type="limit"
    )

    assert routed["blocked"] is False
    assert routed["entry_type"] == "limit"
    assert routed["fibo_action_zone"]["zone_role"] == "resistance_retest"
    assert routed["fibo_action_zone"]["entry_action"] == "sell_limit"
    assert routed["fibo_action_zone"]["pm_action"] == "runner_preserve_or_add_on_rejection"


def test_router_blocks_fresh_short_at_382_support_and_maps_pm_protect(monkeypatch):
    _patch_router_deps(monkeypatch)
    sched = DexterScheduler.__new__(DexterScheduler)
    sig = _signal(
        {
            "ratio_zone": "0.382",
            "impulse_state_direction": "short",
            "dema_reclaim_state": "support_touch",
            "candle_rejection_confirmed": True,
        }
    )

    routed = sched._xau_openapi_entry_router(
        sig, family="xau_scalp_pullback_limit", preferred_entry_type="limit"
    )

    assert routed["blocked"] is True
    assert routed["mode"] == "blocked_fibo_pm_zone"
    assert routed["reason"] == "fibo_382_support_is_pm_protect_not_fresh_short"
    assert routed["fibo_action_zone"]["zone_role"] == "support_profit_protect"
    assert routed["fibo_action_zone"]["pm_action"] == "partial_close_lock_profit_or_wait_breakdown"


def test_router_allows_382_breakdown_short_as_signal_entry_not_support_sell(monkeypatch):
    _patch_router_deps(
        monkeypatch,
        snapshot=_strong_short_signal_snapshot(),
        state_label="continuation_drive",
        continuation_bias=0.82,
    )
    sched = DexterScheduler.__new__(DexterScheduler)
    sig = _signal(
        {
            "ratio_zone": "0.382",
            "impulse_state_direction": "short",
            "structure_break": True,
            "dema_reclaim_state": "breakdown",
        }
    )

    routed = sched._xau_openapi_entry_router(
        sig, family="xau_scalp_pullback_limit", preferred_entry_type="limit"
    )

    assert routed["blocked"] is False
    assert routed["entry_type"] in {"market", "sell_stop"}
    assert routed["fibo_action_zone"]["zone_role"] == "breakdown_continuation"
    assert routed["fibo_action_zone"]["entry_action"] in {"sell_market", "sell_stop"}
