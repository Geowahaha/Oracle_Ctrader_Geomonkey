from analysis.signals import TradeSignal
from scheduler import DexterScheduler


def _signal(raw_scores=None, direction="short"):
    sign = -1 if direction == "short" else 1
    return TradeSignal(
        symbol="XAUUSD",
        direction=direction,
        confidence=84.0,
        entry=4690.0,
        stop_loss=4698.0 if direction == "short" else 4682.0,
        take_profit_1=4686.0 if direction == "short" else 4694.0,
        take_profit_2=4682.0 if direction == "short" else 4698.0,
        take_profit_3=4676.0 if direction == "short" else 4704.0,
        risk_reward=3.0,
        timeframe="M1",
        session="new_york",
        trend="bearish" if direction == "short" else "bullish",
        rsi=45.0,
        atr=4.0,
        pattern="scalp_xauusd:winner",
        raw_scores=dict(raw_scores or {}),
        entry_type="limit",
    )


def _strong_continuation_snapshot(direction="short"):
    if direction == "short":
        delta, imb, refill, tick = -0.24, -0.055, -0.045, 0.32
    else:
        delta, imb, refill, tick = 0.24, 0.055, 0.045, 0.68
    return {
        "ok": True,
        "run_id": "unit-entry-advantage",
        "gate": {
            "features": {
                "spread_avg_pct": 0.00018,
                "spread_expansion": 1.01,
                "delta_proxy": delta,
                "depth_imbalance": imb,
                "depth_refill_shift": refill,
                "rejection_ratio": 0.04,
                "bar_volume_proxy": 0.84,
                "tick_up_ratio": tick,
            }
        },
    }


def _patch_router_deps(monkeypatch, *, direction="short", state_label="continuation_drive", continuation_bias=0.84):
    monkeypatch.setattr(
        "scheduler.live_profile_autopilot.latest_capture_feature_snapshot",
        lambda **kwargs: _strong_continuation_snapshot(direction),
    )
    monkeypatch.setattr(
        "scheduler.live_profile_classify_chart_state",
        lambda direction, context, capture_features=None: {
            "state_label": state_label,
            "day_type": "trend",
            "continuation_bias": continuation_bias,
        },
    )
    monkeypatch.setattr("scheduler.config.XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_ENABLED", True, raising=False)
    monkeypatch.setattr("scheduler.config.XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE", 7, raising=False)
    monkeypatch.setattr("scheduler.config.XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_BIAS", 0.70, raising=False)
    monkeypatch.setattr("scheduler.config.XAU_ENTRY_ADVANTAGE_GUARD_ENABLED", True, raising=False)


def test_entry_advantage_guard_prevents_market_chase_without_structure_break(monkeypatch):
    _patch_router_deps(monkeypatch)
    sched = DexterScheduler.__new__(DexterScheduler)

    routed = sched._xau_openapi_entry_router(
        _signal({"candle_rejection_confirmed": True}),
        family="xau_scalp_pullback_limit",
        preferred_entry_type="limit",
    )

    assert routed["blocked"] is False
    assert routed["entry_type"] == "sell_stop"
    assert routed["mode"] == "entry_advantage_wait_break"
    assert "no_structure_break_no_market_chase" in routed["reasons"]


def test_entry_advantage_guard_allows_market_only_after_real_break(monkeypatch):
    _patch_router_deps(monkeypatch)
    sched = DexterScheduler.__new__(DexterScheduler)

    routed = sched._xau_openapi_entry_router(
        _signal({"structure_break": True, "candle_rejection_confirmed": True}),
        family="xau_scalp_pullback_limit",
        preferred_entry_type="limit",
    )

    assert routed["blocked"] is False
    assert routed["entry_type"] == "market"
    assert routed["mode"] == "signal_market"
    assert "signal_now" in routed["reasons"]
