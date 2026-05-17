from types import SimpleNamespace
from unittest.mock import patch

from analysis.signals import TradeSignal
from scheduler import DexterScheduler


def _probe_signal() -> TradeSignal:
    return TradeSignal(
        symbol="XAUUSD",
        direction="long",
        confidence=82.0,
        entry=4700.0,
        stop_loss=4698.0,
        take_profit_1=4702.0,
        take_profit_2=4704.0,
        take_profit_3=4706.0,
        risk_reward=3.0,
        timeframe="M15",
        session="london",
        trend="bullish",
        rsi=55.0,
        atr=2.0,
        pattern="FIBO_MTF_SHADOW_RECLAIM",
        raw_scores={
            "fibo_mtf_shadow": True,
            "fibo_mtf_live_enabled": False,
            "fibo_mtf_planner_shadow_only": True,
            "fibo_reclaim_setup": "fibo_reclaim_long",
            "fibo_reclaim_score": 74.0,
            "fibo_cluster_count": 2,
            "impulse_state_name": "restart",
            "impulse_state_direction": "long",
            "tf_label": "M15",
            "setup_pattern": "FIBO_MTF_SHADOW_RECLAIM",
        },
    )


def test_micro_live_adapter_clones_probe_without_shadow_markers_and_executes_non_shadow_source():
    sched = DexterScheduler.__new__(DexterScheduler)
    sig = _probe_signal()
    decision = SimpleNamespace(route="probe")
    captured = {}

    def fake_execute(exec_sig, source):
        captured["source"] = source
        captured["signal"] = exec_sig
        return SimpleNamespace(ok=True, dry_run=False, status="accepted")

    with patch("scheduler.config.FIBO_MTF_MICRO_LIVE_ENABLED", True, create=True), \
         patch("scheduler.config.FIBO_MTF_MICRO_LIVE_REQUIRE_DEMO", True, create=True), \
         patch("scheduler.config.CTRADER_USE_DEMO", True, create=True), \
         patch("scheduler.config.FIBO_MTF_MICRO_LIVE_SOURCE", "fibo_xauusd", create=True), \
         patch.object(sched, "_maybe_execute_ctrader_signal", side_effect=fake_execute):
        report = sched._maybe_execute_fibo_mtf_micro_live_probe(sig, decision)

    assert report["executed"] is True
    assert captured["source"] == "fibo_xauusd"
    live_sig = captured["signal"]
    assert live_sig is not sig
    assert "FIBO_MTF_SHADOW" not in live_sig.pattern
    assert "FIBO_MTF_SHADOW" not in str(live_sig.raw_scores)
    assert live_sig.pattern == "Fibo MTF Micro Live Probe"
    assert live_sig.raw_scores["fibo_mtf_micro_live_adapter"] is True
    assert live_sig.raw_scores["fibo_mtf_live_enabled"] is True
    assert live_sig.raw_scores["fibo_mtf_planner_shadow_only"] is False
    assert live_sig.raw_scores["fibo_mtf_shadow"] is False
    assert sched._is_fibo_mtf_shadow_signal(live_sig, captured["source"], live_sig.raw_scores) is False


def test_micro_live_adapter_rejects_low_rr_even_when_calendar_gate_is_bypassed():
    sched = DexterScheduler.__new__(DexterScheduler)
    sig = _probe_signal()
    sig.risk_reward = 2.2
    decision = SimpleNamespace(route="probe")
    with patch("scheduler.config.FIBO_MTF_MICRO_LIVE_ENABLED", True, create=True), \
         patch("scheduler.config.FIBO_MTF_MICRO_LIVE_REQUIRE_DEMO", True, create=True), \
         patch("scheduler.config.CTRADER_USE_DEMO", True, create=True), \
         patch.object(sched, "_maybe_execute_ctrader_signal") as exec_mock:
        report = sched._maybe_execute_fibo_mtf_micro_live_probe(sig, decision)
    assert report["executed"] is False
    assert report["reason"].startswith("rr_below")
    exec_mock.assert_not_called()


def test_micro_live_adapter_rejects_w1_idle_absurd_sell_from_real_bad_case():
    sched = DexterScheduler.__new__(DexterScheduler)
    sig = TradeSignal(
        symbol="XAUUSD",
        direction="short",
        confidence=26.0,
        entry=4668.22,
        stop_loss=5657.41,
        take_profit_1=2689.85,
        take_profit_2=2689.85,
        take_profit_3=2689.85,
        risk_reward=3.0,
        timeframe="W1",
        session="asia",
        trend="bearish",
        rsi=45.0,
        atr=284.83,
        pattern="FIBO_MTF_SHADOW_RECLAIM",
        raw_scores={
            "fibo_mtf_shadow": False,
            "fibo_reclaim_setup": "fibo_reclaim_short",
            "fibo_reclaim_score": 100.0,
            "fibo_cluster_count": 5,
            "tf_label": "W1",
            "impulse_state_name": "idle",
            "impulse_state_direction": "",
        },
    )
    decision = SimpleNamespace(route="probe")
    with patch("scheduler.config.FIBO_MTF_MICRO_LIVE_ENABLED", True, create=True), \
         patch("scheduler.config.FIBO_MTF_MICRO_LIVE_REQUIRE_DEMO", True, create=True), \
         patch("scheduler.config.CTRADER_USE_DEMO", True, create=True), \
         patch.object(sched, "_maybe_execute_ctrader_signal") as exec_mock:
        report = sched._maybe_execute_fibo_mtf_micro_live_probe(sig, decision)
    assert report["executed"] is False
    assert report["reason"].startswith("tf_not_tactical")
    exec_mock.assert_not_called()


def test_micro_live_adapter_rejects_counter_impulse_even_with_good_geometry():
    sched = DexterScheduler.__new__(DexterScheduler)
    sig = _probe_signal()
    sig.direction = "short"
    sig.stop_loss = 4702.0
    sig.take_profit_1 = 4694.0
    sig.raw_scores["fibo_reclaim_setup"] = "fibo_reclaim_short"
    sig.raw_scores["impulse_state_direction"] = "long"
    decision = SimpleNamespace(route="probe")
    with patch("scheduler.config.FIBO_MTF_MICRO_LIVE_ENABLED", True, create=True), \
         patch("scheduler.config.FIBO_MTF_MICRO_LIVE_REQUIRE_DEMO", True, create=True), \
         patch("scheduler.config.CTRADER_USE_DEMO", True, create=True), \
         patch.object(sched, "_maybe_execute_ctrader_signal") as exec_mock:
        report = sched._maybe_execute_fibo_mtf_micro_live_probe(sig, decision)
    assert report["executed"] is False
    assert report["reason"] == "counter_impulse:short!=long"
    exec_mock.assert_not_called()
