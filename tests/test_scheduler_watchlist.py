import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import scheduler as scheduler_module
from analysis.signals import TradeSignal
from execution.mt5_executor import MT5ExecutionResult
from market.macro_news import MacroHeadline
from scanners.stock_scanner import StockOpportunity


def make_signal(symbol: str, confidence: float = 72.0) -> TradeSignal:
    return TradeSignal(
        symbol=symbol,
        direction="long",
        confidence=confidence,
        entry=100.0,
        stop_loss=99.0,
        take_profit_1=101.0,
        take_profit_2=102.0,
        take_profit_3=103.0,
        risk_reward=2.0,
        timeframe="1h",
        session="new_york",
        trend="bullish",
        rsi=56.0,
        atr=1.0,
        pattern="TEST",
        reasons=[],
        warnings=[],
        raw_scores={"edge": 20},
    )


def make_opp(symbol: str, vol: float = 0.8, quality_score: int = 1, confidence: float = 72.0) -> StockOpportunity:
    return StockOpportunity(
        signal=make_signal(symbol=symbol, confidence=confidence),
        market="US",
        setup_type="BULLISH_OB_BOUNCE",
        base_setup_type="OB_BOUNCE",
        vol_vs_avg=vol,
        quality_score=quality_score,
        quality_tag="LOW",
    )


class SchedulerWatchlistTests(unittest.TestCase):
    @staticmethod
    def _macro_headline(headline_id: str, score: int, themes: list[str], age_min: int = 15) -> MacroHeadline:
        return MacroHeadline(
            headline_id=headline_id,
            title=f"Headline {headline_id}",
            link=f"https://example.com/{headline_id}",
            source="Reuters",
            published_utc=datetime.now(timezone.utc) - timedelta(minutes=age_min),
            score=score,
            themes=themes,
            impact_hint="Macro-sensitive headline",
        )

    def test_scheduler_uses_filtered_watchlist_when_no_quality(self):
        dexter = scheduler_module.DexterScheduler()
        opps_all = [make_opp("A", 0.3), make_opp("B", 0.7)]
        watchlist = [make_opp("B", 0.7)]

        with patch.object(scheduler_module.stock_scanner, "scan_all_open_markets", return_value=opps_all), \
             patch.object(scheduler_module.stock_scanner, "filter_quality", return_value=[]), \
             patch.object(scheduler_module.stock_scanner, "filter_watchlist", return_value=watchlist), \
             patch.object(scheduler_module.notifier, "send_stock_scan_summary") as send_summary, \
             patch.object(scheduler_module.notifier, "send_stock_signal") as send_signal:
            dexter._run_stock_scan()

        self.assertTrue(send_summary.called)
        args, kwargs = send_summary.call_args
        self.assertEqual(args[0], watchlist)
        self.assertIn("WATCHLIST", kwargs.get("market_label", ""))
        self.assertFalse(send_signal.called)

    def test_scheduler_logs_quality_and_watchlist_counts(self):
        dexter = scheduler_module.DexterScheduler()
        opps_all = [make_opp("A", 0.3), make_opp("B", 0.7)]
        watchlist = [make_opp("B", 0.7)]

        with patch.object(scheduler_module.stock_scanner, "scan_all_open_markets", return_value=opps_all), \
             patch.object(scheduler_module.stock_scanner, "filter_quality", return_value=[]), \
             patch.object(scheduler_module.stock_scanner, "filter_watchlist", return_value=watchlist), \
             patch.object(scheduler_module.notifier, "send_stock_scan_summary"), \
             patch.object(scheduler_module.logger, "info") as info_log:
            dexter._run_stock_scan()

        messages = [str(call.args[0]) for call in info_log.call_args_list if call.args]
        self.assertTrue(any("Stocks quality filter:" in m for m in messages))
        self.assertTrue(any("Stocks watchlist filter:" in m for m in messages))

    def test_xauusd_scheduled_scan_respects_cooldown(self):
        dexter = scheduler_module.DexterScheduler()
        signal = make_signal("XAUUSD", confidence=85.0)
        dexter._last_xauusd_alert_ts = 9999999999.0
        dexter._last_xauusd_direction = signal.direction
        dexter._last_xauusd_entry = signal.entry
        dexter._last_xauusd_atr = signal.atr

        with patch.object(scheduler_module.xauusd_scanner, "scan", return_value=signal), \
             patch.object(scheduler_module.notifier, "send_signal") as send_signal:
            dexter._run_xauusd_scan(force_alert=False)

        self.assertFalse(send_signal.called)

    def test_xauusd_manual_scan_bypasses_cooldown(self):
        dexter = scheduler_module.DexterScheduler()
        signal = make_signal("XAUUSD", confidence=85.0)
        dexter._last_xauusd_alert_ts = 9999999999.0
        dexter._last_xauusd_direction = signal.direction
        dexter._last_xauusd_entry = signal.entry
        dexter._last_xauusd_atr = signal.atr

        with patch.object(scheduler_module.xauusd_scanner, "scan", return_value=signal), \
             patch.object(scheduler_module.notifier, "send_signal", return_value=True) as send_signal:
            dexter._run_xauusd_scan(force_alert=True)

        self.assertTrue(send_signal.called)

    def test_mt5_batch_stops_after_attempt_cap(self):
        dexter = scheduler_module.DexterScheduler()
        signals = [make_signal(f"S{i}") for i in range(6)]
        failed = MT5ExecutionResult(
            ok=False,
            status="skipped",
            message="margin guard",
            signal_symbol="S0",
            broker_symbol="S0USD",
        )

        with patch.object(scheduler_module.config, "MT5_ENABLED", True), \
             patch.object(scheduler_module.config, "MT5_AUTOPILOT_ENABLED", False), \
             patch.object(scheduler_module.config, "NEURAL_BRAIN_ENABLED", False), \
             patch.object(scheduler_module.config, "MT5_MICRO_MODE_ENABLED", False), \
             patch.object(scheduler_module.config, "MT5_MAX_SIGNALS_PER_SCAN", 1), \
             patch.object(scheduler_module.config, "MT5_MAX_ATTEMPTS_PER_SCAN", 2), \
             patch.object(scheduler_module.mt5_executor, "execute_signal", return_value=failed) as exec_call:
            dexter._maybe_execute_mt5_batch(signals, source="crypto")

        self.assertEqual(exec_call.call_count, 2)

    def test_mt5_filled_notifies_execution_update(self):
        dexter = scheduler_module.DexterScheduler()
        signal = make_signal("ETH/USDT", confidence=90.0)
        filled = MT5ExecutionResult(
            ok=True,
            status="filled",
            message="order accepted",
            signal_symbol="ETH/USDT",
            broker_symbol="ETHUSD",
            ticket=12345,
            position_id=12345,
        )
        with patch.object(scheduler_module.config, "NEURAL_BRAIN_ENABLED", False), \
             patch.object(scheduler_module.config, "MT5_NOTIFY_EXECUTED", True), \
             patch.object(scheduler_module.notifier, "send_mt5_execution_update") as send_exec:
            dexter._handle_mt5_result(signal, filled, source="crypto")
        self.assertTrue(send_exec.called)

    def test_neural_soft_adjustment_applies_confidence_without_block(self):
        dexter = scheduler_module.DexterScheduler()
        signal = make_signal("XAUUSD", confidence=74.0)

        with patch.object(
            scheduler_module.neural_brain,
            "confidence_adjustment",
            return_value={
                "applied": True,
                "reason": "applied",
                "prob": 0.78,
                "base_confidence": 74.0,
                "adjusted_confidence": 78.0,
                "delta": 4.0,
            },
        ):
            out = dexter._apply_neural_soft_adjustment(signal, source="xauusd")

        self.assertTrue(out.get("applied"))
        self.assertAlmostEqual(signal.confidence, 78.0, places=2)
        self.assertTrue(signal.raw_scores.get("neural_confidence_adjusted"))
        self.assertIn("neural_probability", signal.raw_scores)

    def test_mt5_filter_armed_but_not_ready_does_not_block_execution(self):
        dexter = scheduler_module.DexterScheduler()
        signal = make_signal("ETH/USDT", confidence=90.0)
        exec_result = MT5ExecutionResult(
            ok=False,
            status="skipped",
            message="margin guard",
            signal_symbol="ETH/USDT",
            broker_symbol="ETHUSD",
        )

        with patch.object(scheduler_module.config, "MT5_ENABLED", True), \
             patch.object(scheduler_module.config, "MT5_AUTOPILOT_ENABLED", False), \
             patch.object(scheduler_module.config, "NEURAL_BRAIN_ENABLED", True), \
             patch.object(scheduler_module.config, "NEURAL_BRAIN_EXECUTION_FILTER", True), \
             patch.object(scheduler_module.neural_brain, "execution_filter_status", return_value={"ready": False, "reason": "insufficient_samples"}), \
             patch.object(scheduler_module.mt5_executor, "execute_signal", return_value=exec_result) as exec_call, \
             patch.object(dexter, "_handle_mt5_result") as handle_result:
            dexter._maybe_execute_mt5_signal(signal, source="crypto")

        self.assertEqual(exec_call.call_count, 1)
        self.assertEqual(handle_result.call_count, 1)

    def test_mt5_filter_ready_blocks_low_probability(self):
        dexter = scheduler_module.DexterScheduler()
        signal = make_signal("ETH/USDT", confidence=90.0)

        with patch.object(scheduler_module.config, "MT5_ENABLED", True), \
             patch.object(scheduler_module.config, "NEURAL_BRAIN_ENABLED", True), \
             patch.object(scheduler_module.config, "NEURAL_BRAIN_EXECUTION_FILTER", True), \
             patch.object(scheduler_module.config, "NEURAL_BRAIN_MIN_PROB", 0.60), \
             patch.object(scheduler_module.neural_brain, "execution_filter_status", return_value={"ready": True, "reason": "ready"}), \
             patch.object(scheduler_module.neural_brain, "predict_probability", return_value=0.45), \
             patch.object(scheduler_module.mt5_executor, "execute_signal") as exec_call, \
             patch.object(dexter, "_handle_mt5_result") as handle_result:
            dexter._maybe_execute_mt5_signal(signal, source="crypto")

        self.assertFalse(exec_call.called)
        self.assertEqual(handle_result.call_count, 1)

    def test_neural_sync_train_uses_feedback_and_bootstrap_min_samples(self):
        dexter = scheduler_module.DexterScheduler()
        with patch.object(scheduler_module.config, "NEURAL_BRAIN_ENABLED", True), \
             patch.object(scheduler_module.config, "NEURAL_BRAIN_AUTO_TRAIN", True), \
             patch.object(scheduler_module.config, "SIGNAL_FEEDBACK_ENABLED", True), \
             patch.object(scheduler_module.config, "NEURAL_BRAIN_SYNC_DAYS", 120), \
             patch.object(scheduler_module.config, "NEURAL_BRAIN_MIN_SAMPLES", 30), \
             patch.object(scheduler_module.config, "NEURAL_BRAIN_BOOTSTRAP_MIN_SAMPLES", 10), \
             patch.object(scheduler_module.config, "NEURAL_BRAIN_SIGNAL_FEEDBACK_MAX_RECORDS", 400), \
             patch.object(scheduler_module.neural_brain, "sync_outcomes_from_mt5", return_value={"ok": True, "updated": 0, "closed_positions": 0}), \
             patch.object(scheduler_module.neural_brain, "sync_signal_outcomes_from_market", return_value={"ok": True, "reviewed": 20, "resolved": 12, "updated": 20}) as sync_feedback, \
             patch.object(scheduler_module.neural_brain, "model_status", return_value={"available": False}), \
             patch.object(scheduler_module.neural_brain, "train_backprop") as train_call:
            dexter._run_neural_sync_train()

        self.assertEqual(sync_feedback.call_count, 1)
        self.assertEqual(train_call.call_count, 1)
        self.assertEqual(train_call.call_args.kwargs.get("min_samples"), 10)

    def test_macro_adaptive_priority_drops_weak_theme_headline(self):
        dexter = scheduler_module.DexterScheduler()
        weak = self._macro_headline("weak1", 8, ["tariff_trade"])
        strong = self._macro_headline("strong1", 8, ["fed_policy"])
        weights = {
            "tariff_trade": {"weight_mult": 0.82, "sample_count": 8, "no_clear_rate": 82.0, "confirmed_rate": 5.0},
            "fed_policy": {"weight_mult": 1.08, "sample_count": 8, "no_clear_rate": 15.0, "confirmed_rate": 55.0},
        }
        with patch.object(scheduler_module.config, "MACRO_ALERT_ADAPTIVE_PRIORITY_ENABLED", True), \
             patch.object(scheduler_module.config, "MACRO_ALERT_ADAPTIVE_MIN_SAMPLES", 3), \
             patch.object(scheduler_module.config, "MACRO_ALERT_ADAPTIVE_MIN_THEME_MULT", 0.90), \
             patch.object(scheduler_module.config, "MACRO_ALERT_ADAPTIVE_SKIP_NO_CLEAR_RATE", 65.0), \
             patch.object(scheduler_module.config, "MACRO_ALERT_ADAPTIVE_ULTRA_SCORE_FLOOR", 10), \
             patch.object(scheduler_module.macro_news, "dynamic_theme_weights_snapshot", return_value=weights):
            ranked, meta = dexter._rank_macro_alert_candidates([weak, strong], now_utc=datetime.now(timezone.utc))

        self.assertEqual([h.headline_id for h in ranked], ["strong1"])
        self.assertEqual(meta["dropped"], 1)

    def test_macro_adaptive_priority_keeps_ultra_score_even_if_theme_weak(self):
        dexter = scheduler_module.DexterScheduler()
        ultra = self._macro_headline("ultra1", 12, ["tariff_trade"])
        weights = {
            "tariff_trade": {"weight_mult": 0.80, "sample_count": 9, "no_clear_rate": 90.0, "confirmed_rate": 5.0},
        }
        with patch.object(scheduler_module.config, "MACRO_ALERT_ADAPTIVE_PRIORITY_ENABLED", True), \
             patch.object(scheduler_module.config, "MACRO_ALERT_ADAPTIVE_MIN_SAMPLES", 3), \
             patch.object(scheduler_module.config, "MACRO_ALERT_ADAPTIVE_MIN_THEME_MULT", 0.90), \
             patch.object(scheduler_module.config, "MACRO_ALERT_ADAPTIVE_SKIP_NO_CLEAR_RATE", 65.0), \
             patch.object(scheduler_module.config, "MACRO_ALERT_ADAPTIVE_ULTRA_SCORE_FLOOR", 10), \
             patch.object(scheduler_module.macro_news, "dynamic_theme_weights_snapshot", return_value=weights):
            ranked, meta = dexter._rank_macro_alert_candidates([ultra], now_utc=datetime.now(timezone.utc))

        self.assertEqual([h.headline_id for h in ranked], ["ultra1"])
        self.assertEqual(meta["dropped"], 0)
        self.assertGreater(float(getattr(ranked[0], "_adaptive_priority", 0.0)), 0.0)

    def test_mt5_micro_mode_does_not_count_margin_guard_skips_against_attempt_cap(self):
        dexter = scheduler_module.DexterScheduler()
        signals = [make_signal(f"S{i}") for i in range(4)]
        skipped = MT5ExecutionResult(ok=False, status="skipped", message="margin guard: required=10 > allowed=1", signal_symbol="S0", broker_symbol="S0")
        filled = MT5ExecutionResult(ok=True, status="filled", message="ok", signal_symbol="S3", broker_symbol="S3")
        seq = [skipped, skipped, skipped, filled]

        with patch.object(scheduler_module.config, "MT5_ENABLED", True), \
             patch.object(scheduler_module.config, "MT5_AUTOPILOT_ENABLED", False), \
             patch.object(scheduler_module.config, "NEURAL_BRAIN_ENABLED", False), \
             patch.object(scheduler_module.config, "MT5_MICRO_MODE_ENABLED", True), \
             patch.object(scheduler_module.config, "MT5_MAX_SIGNALS_PER_SCAN", 1), \
             patch.object(scheduler_module.config, "MT5_MAX_ATTEMPTS_PER_SCAN", 1), \
             patch.object(scheduler_module.mt5_executor, "execute_signal", side_effect=seq) as exec_call:
            dexter._maybe_execute_mt5_batch(signals, source="crypto")

        # Micro-mode margin-guard skips should not consume the single attempt slot.
        self.assertEqual(exec_call.call_count, 4)


if __name__ == "__main__":
    unittest.main()
