"""
scheduler.py - Background Task Scheduler
Runs XAUUSD and crypto scans on configured intervals.
Sends Telegram alerts when signals are found.
Respects session timing - more aggressive scans during active sessions.
"""
import time
import logging
import threading
from datetime import datetime, timezone, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import schedule

from config import config
from scanners.xauusd import xauusd_scanner
from scanners.crypto_sniper import crypto_sniper
from scanners.fx_major_scanner import fx_major_scanner
from scanners.stock_scanner import stock_scanner
from notifier.telegram_bot import notifier
from market.data_fetcher import session_manager
from market.economic_calendar import economic_calendar
from market.macro_news import macro_news
from market.macro_impact_tracker import macro_impact_tracker
from execution.mt5_executor import mt5_executor, MT5ExecutionResult
from learning.neural_brain import neural_brain
from learning.mt5_autopilot_core import mt5_autopilot_core
from learning.mt5_orchestrator import mt5_orchestrator
from learning.mt5_position_manager import mt5_position_manager

logger = logging.getLogger(__name__)


class DexterScheduler:
    """
    Background scheduler that runs scans at configured intervals
    and dispatches Telegram alerts for qualified signals.
    """

    def __init__(self):
        self.running = False
        self._thread: threading.Thread = None
        self._last_signal_symbols: set = set()
        self._last_us_open_plan_date: str = ""
        self._us_open_last_symbols: list[str] = []
        self._us_open_last_sent_ts: float = 0.0
        self._us_open_last_noopp_sent_ts: float = 0.0
        self._us_open_session_checkin_day: str = ""
        self._us_open_quality_last_sent_ts: float = 0.0
        self._us_open_quality_last_key: str = ""
        self._us_open_mood_day: str = ""
        self._us_open_mood_weak_cycles: int = 0
        self._us_open_mood_stop_triggered: bool = False
        self._us_open_mood_stop_reason: str = ""
        self._last_xauusd_alert_ts: float = 0.0
        self._last_xauusd_direction: str = ""
        self._last_xauusd_entry: float = 0.0
        self._last_xauusd_atr: float = 0.0
        self._last_xauusd_signal_snapshot: dict = {}
        self._last_signal_feedback_report_date: str = ""
        self._last_neural_filter_not_ready_log_ts: float = 0.0
        self._last_neural_soft_adjust_skip_log_ts: float = 0.0
        self._econ_alert_sent: dict[str, float] = {}
        self._macro_alert_sent: dict[str, float] = {}
        self._last_macro_impact_sync_log_ts: float = 0.0
        self._last_crypto_focus_no_signal_ts: float = 0.0
        self._us_open_symbol_alert_ts: dict[str, float] = {}
        self._us_open_circuit_day: str = ""
        self._us_open_circuit_triggered: bool = False
        self._us_open_circuit_reason: str = ""
        self._us_open_quality_guard_cache_ts: float = 0.0
        self._us_open_quality_guard_cache: dict = {}
        self._us_open_quality_guard_last_diag: dict[str, dict] = {"plan": {}, "monitor": {}}
        self._us_open_quality_guard_last_diag_ts: float = 0.0
        self._us_open_symbol_recovery_state: dict[str, dict] = {}

    def _raw_confidence(self, signal) -> float:
        try:
            raw_scores = dict(getattr(signal, "raw_scores", {}) or {})
            return float(raw_scores.get("confidence_pre_neural", float(getattr(signal, "confidence", 0.0))))
        except Exception:
            return float(getattr(signal, "confidence", 0.0))

    def _apply_neural_soft_adjustment(self, signal, source: str) -> dict:
        """
        Soft-adjust confidence before sending a signal.
        This never blocks the signal path.
        """
        if signal is None:
            return {"applied": False, "reason": "no_signal"}
        try:
            raw_scores = dict(getattr(signal, "raw_scores", {}) or {})
            if raw_scores.get("neural_confidence_adjusted"):
                return {"applied": False, "reason": "already_adjusted"}

            base_conf = float(getattr(signal, "confidence", 0.0))
            adjust = neural_brain.confidence_adjustment(signal, source=source)
            prob = adjust.get("prob")
            raw_scores["confidence_pre_neural"] = round(base_conf, 3)
            if prob is not None:
                raw_scores["neural_probability"] = round(float(prob), 4)
            raw_scores["neural_adjust_reason"] = str(adjust.get("reason", "unknown"))

            if adjust.get("applied"):
                adjusted = float(adjust.get("adjusted_confidence", base_conf))
                delta = float(adjust.get("delta", adjusted - base_conf))
                signal.confidence = round(adjusted, 1)
                raw_scores["confidence_post_neural"] = round(signal.confidence, 3)
                raw_scores["neural_adjust_delta"] = round(delta, 3)
                if prob is not None:
                    msg = f"🧠 Neural prob: {float(prob) * 100:.1f}%"
                    if delta >= 0.15 and msg not in signal.reasons:
                        signal.reasons.append(f"{msg} (confidence boosted)")
                    elif delta <= -0.15 and msg not in signal.warnings:
                        signal.warnings.append(f"{msg} (confidence tempered)")
            else:
                raw_scores["confidence_post_neural"] = round(base_conf, 3)
                now_ts = time.time()
                if (now_ts - self._last_neural_soft_adjust_skip_log_ts) >= 900:
                    logger.info(
                        "[NeuralBrain] soft-adjust skipped (%s)",
                        str(adjust.get("reason", "unknown")),
                    )
                    self._last_neural_soft_adjust_skip_log_ts = now_ts

            raw_scores["neural_confidence_adjusted"] = True
            signal.raw_scores = raw_scores
            return adjust
        except Exception as e:
            logger.debug("[NeuralBrain] soft-adjust error: %s", e)
            return {"applied": False, "reason": "exception"}

    def _neural_execution_filter_ready(self) -> tuple[bool, dict]:
        if not (config.NEURAL_BRAIN_ENABLED and config.NEURAL_BRAIN_EXECUTION_FILTER):
            return False, {"ready": False, "reason": "execution_filter_disabled"}
        try:
            state = neural_brain.execution_filter_status()
        except Exception as e:
            return False, {"ready": False, "reason": f"status_error:{e}"}

        ready = bool(state.get("ready", False))
        if not ready:
            now_ts = time.time()
            if (now_ts - self._last_neural_filter_not_ready_log_ts) >= 600:
                logger.info(
                    "[MT5] Neural execution filter armed but not ready (%s): "
                    "samples=%s/%s val_acc=%.3f/%.3f age_h=%s",
                    str(state.get("reason", "unknown")),
                    int(state.get("samples", 0) or 0),
                    int(state.get("required_samples", 0) or 0),
                    float(state.get("val_accuracy", 0.0) or 0.0),
                    float(state.get("required_val_accuracy", 0.0) or 0.0),
                    str(state.get("age_hours", "-")),
                )
                self._last_neural_filter_not_ready_log_ts = now_ts
        return ready, state

    def _neural_min_prob_for_signal(self, signal, source: str) -> tuple[float, str]:
        base = float(getattr(config, "NEURAL_BRAIN_MIN_PROB", 0.55) or 0.55)
        reason = "global"
        src = str(source or "").strip().lower()
        sym = ""
        if src == "fx":
            fx_min = float(getattr(config, "NEURAL_BRAIN_MIN_PROB_FX", base) or base)
            base = fx_min
            reason = "fx_default"
        try:
            sym = str(getattr(signal, "symbol", "") or "").strip().upper()
            overrides = config.get_neural_min_prob_symbol_overrides()
            if sym and sym in overrides:
                base = float(overrides[sym])
                reason = f"symbol_override:{sym}"
        except Exception:
            pass
        if src == 'fx' and sym:
            try:
                learned = mt5_autopilot_core.fx_learned_neural_threshold(sym, base_threshold=float(base))
                if bool(learned.get('applied')):
                    base = float(learned.get('threshold', base) or base)
                    reason = f"{reason}+learned:{int(learned.get('samples',0) or 0)}"
            except Exception:
                pass
        # Clamp to sane probability range
        base = max(0.0, min(0.99, float(base)))
        return base, reason

    def _attach_neural_filter_meta(self, signal, prob: float | None, min_prob: float, min_prob_reason: str, extra: dict | None = None) -> None:
        try:
            raw_scores = dict(getattr(signal, "raw_scores", {}) or {})
            if prob is not None:
                raw_scores["neural_probability"] = round(float(prob), 4)
            raw_scores["mt5_neural_min_prob"] = round(float(min_prob), 4)
            raw_scores["mt5_neural_min_prob_reason"] = str(min_prob_reason or "")
            if isinstance(extra, dict):
                raw_scores.update({k: v for k, v in extra.items()})
            signal.raw_scores = raw_scores
        except Exception:
            pass


    def _maybe_apply_fx_neural_soft_filter(self, signal, source: str, prob: float | None, min_prob: float) -> tuple[bool, dict]:
        info = {"applied": False, "hard_block": False, "reason": "n/a", "penalty": 0.0}
        src = str(source or "").strip().lower()
        if src != 'fx':
            info['reason'] = 'not_fx'
            return False, info
        if prob is None:
            info['reason'] = 'no_prob'
            return False, info
        if not bool(getattr(config, 'NEURAL_BRAIN_FX_SOFT_FILTER_ENABLED', False)):
            info['reason'] = 'disabled'
            return False, info
        p = float(prob)
        base_low = float(getattr(config, 'NEURAL_BRAIN_FX_SOFT_FILTER_BAND_LOW', 0.43) or 0.43)
        base_high = float(getattr(config, 'NEURAL_BRAIN_FX_SOFT_FILTER_BAND_HIGH', 0.48) or 0.48)
        low = float(base_low)
        high = float(base_high)
        if high < low:
            low, high = high, low
            base_low, base_high = low, high
        sym = str(getattr(signal, 'symbol', '') or '').strip().upper()
        if sym and bool(getattr(config, 'NEURAL_BRAIN_FX_SOFT_FILTER_LEARNED_BAND_ENABLED', False)):
            try:
                learned = mt5_autopilot_core.fx_learned_neural_soft_band(
                    sym,
                    base_low=float(low),
                    base_high=float(high),
                    ref_threshold=float(min_prob),
                )
                info['learned_band_reason'] = str(learned.get('reason', ''))
                info['learned_band_samples'] = int(learned.get('samples', 0) or 0)
                if bool(learned.get('applied')):
                    low = float(learned.get('low', low) or low)
                    high = float(learned.get('high', high) or high)
                    info['learned_band_applied'] = True
                else:
                    info['learned_band_applied'] = False
            except Exception as e:
                info['learned_band_applied'] = False
                info['learned_band_reason'] = f'exception:{e}'
        if p < low:
            info['hard_block'] = True
            info['reason'] = f'below_soft_band:{low:.2f}'
            info['band_low'] = round(float(low), 4)
            info['band_high'] = round(float(high), 4)
            return False, info
        if p >= min_prob:
            info['reason'] = 'above_min'
            info['band_low'] = round(float(low), 4)
            info['band_high'] = round(float(high), 4)
            return False, info
        if p > high:
            info['reason'] = 'above_soft_band'
            info['band_low'] = round(float(low), 4)
            info['band_high'] = round(float(high), 4)
            return False, info
        # Soft band fallback: degrade confidence instead of hard blocking.
        try:
            max_penalty = float(getattr(config, 'NEURAL_BRAIN_FX_SOFT_FILTER_MAX_CONF_PENALTY', 4.0) or 4.0)
            span = max(0.0001, float(min_prob) - low)
            ratio = max(0.0, min(1.0, (float(min_prob) - p) / span))
            penalty = round(max_penalty * ratio, 2)
            old_conf = float(getattr(signal, 'confidence', 0.0) or 0.0)
            new_conf = max(0.0, round(old_conf - penalty, 1))
            signal.confidence = new_conf
            try:
                if hasattr(signal, 'warnings') and isinstance(signal.warnings, list):
                    signal.warnings.append(f'FX neural soft-filter: p={p:.2f}, conf {old_conf:.1f}->{new_conf:.1f}')
            except Exception:
                pass
            info.update({
                'applied': True,
                'reason': 'soft_penalty',
                'penalty': penalty,
                'old_conf': round(old_conf, 3),
                'new_conf': round(new_conf, 3),
                'band_low': round(low, 3),
                'band_high': round(high, 3),
                'base_band_low': round(base_low, 3),
                'base_band_high': round(base_high, 3),
            })
            logger.info('[MT5] FX neural soft filter %s p=%.2f min=%.2f conf %.1f->%.1f penalty=%.2f band=[%.2f,%.2f]%s',
                        str(getattr(signal, 'symbol', '') or '-'), p, float(min_prob), old_conf, new_conf, penalty,
                        float(low), float(high),
                        (f" learned(n={int(info.get('learned_band_samples',0) or 0)})" if info.get('learned_band_applied') else ''))
            return True, info
        except Exception as e:
            info['reason'] = f'exception:{e}'
            return False, info

    def _maybe_execute_mt5_signal(self, signal, source: str) -> None:
        if not config.MT5_ENABLED:
            return
        apply_filter, _filter_state = self._neural_execution_filter_ready()
        if apply_filter:
            prob = neural_brain.predict_probability(signal, source=source)
            min_prob, min_prob_reason = self._neural_min_prob_for_signal(signal, source)
            soft_applied, soft_info = self._maybe_apply_fx_neural_soft_filter(signal, source, prob, min_prob)
            self._attach_neural_filter_meta(signal, prob, min_prob, min_prob_reason, extra={
                'mt5_fx_soft_filter_applied': bool(soft_info.get('applied')),
                'mt5_fx_soft_filter_reason': str(soft_info.get('reason','')),
                'mt5_fx_soft_filter_penalty': float(soft_info.get('penalty',0.0) or 0.0),
            })
            if (prob is not None) and (prob < float(min_prob)) and (not bool(soft_info.get('applied'))):
                skipped = MT5ExecutionResult(
                    ok=False,
                    status="skipped",
                    message=(
                        f"neural filter: predicted win prob {prob:.2f} "
                        f"< min {float(min_prob):.2f}"
                        f" ({min_prob_reason})"
                    ),
                    signal_symbol=str(getattr(signal, "symbol", "") or ""),
                )
                self._handle_mt5_result(signal, skipped, source=source)
                return
        volume_multiplier = None
        if getattr(config, "MT5_AUTOPILOT_ENABLED", True):
            plan = mt5_orchestrator.pre_trade_plan(signal, source=source)
            if not plan.allow:
                blocked = MT5ExecutionResult(
                    ok=False,
                    status="guard_blocked",
                    message=str(plan.reason or "risk governor blocked"),
                    signal_symbol=str(getattr(signal, "symbol", "") or ""),
                )
                self._handle_mt5_result(signal, blocked, source=source)
                return
            volume_multiplier = float(plan.risk_multiplier or 1.0)
            try:
                raw_scores = dict(getattr(signal, "raw_scores", {}) or {})
                raw_scores["mt5_risk_multiplier"] = round(volume_multiplier, 4)
                raw_scores["mt5_canary_mode"] = bool(plan.canary_mode)
                raw_scores["mt5_walkforward_reason"] = str((plan.walkforward or {}).get("reason", plan.reason))
                signal.raw_scores = raw_scores
            except Exception:
                pass
        result = mt5_executor.execute_signal(signal, source=source, volume_multiplier=volume_multiplier)
        self._handle_mt5_result(signal, result, source=source)

    def _handle_mt5_result(self, signal, result: MT5ExecutionResult, source: str) -> None:
        logger.info(
            "[MT5] %s %s -> %s (%s) %s",
            result.status,
            result.signal_symbol,
            result.broker_symbol or "-",
            source,
            result.message,
        )
        try:
            neural_brain.record_execution(signal, result, source=source)
        except Exception as e:
            logger.warning("[NeuralBrain] record_execution failed: %s", e)
        try:
            if getattr(config, "MT5_AUTOPILOT_ENABLED", True):
                mt5_autopilot_core.record_execution(signal, result, source=source)
        except Exception as e:
            logger.warning("[MT5Autopilot] record_execution failed: %s", e)

        status = str(result.status or "").lower()
        if result.ok and config.MT5_NOTIFY_EXECUTED:
            notifier.send_mt5_execution_update(signal, result, source=source)
        elif (not result.ok) and config.MT5_NOTIFY_FAILED and status in {"rejected", "error", "invalid_stops", "blocked"}:
            notifier.send_mt5_execution_update(signal, result, source=source)

    def _maybe_execute_mt5_batch(self, signals: list, source: str) -> None:
        if not config.MT5_ENABLED:
            return
        max_count = max(1, int(config.MT5_MAX_SIGNALS_PER_SCAN))
        max_attempts = max(max_count, int(config.MT5_MAX_ATTEMPTS_PER_SCAN))
        executed = 0
        attempted = 0
        apply_filter, _filter_state = self._neural_execution_filter_ready()
        for signal in signals:
            if executed >= max_count or attempted >= max_attempts:
                break
            if apply_filter:
                prob = neural_brain.predict_probability(signal, source=source)
                min_prob, min_prob_reason = self._neural_min_prob_for_signal(signal, source)
                soft_applied, soft_info = self._maybe_apply_fx_neural_soft_filter(signal, source, prob, min_prob)
                self._attach_neural_filter_meta(signal, prob, min_prob, min_prob_reason, extra={
                    'mt5_fx_soft_filter_applied': bool(soft_info.get('applied')),
                    'mt5_fx_soft_filter_reason': str(soft_info.get('reason','')),
                    'mt5_fx_soft_filter_penalty': float(soft_info.get('penalty',0.0) or 0.0),
                })
                if (prob is not None) and (prob < float(min_prob)) and (not bool(soft_info.get('applied'))):
                    attempted += 1
                    skipped = MT5ExecutionResult(
                        ok=False,
                        status="skipped",
                        message=(
                            f"neural filter: predicted win prob {prob:.2f} "
                            f"< min {float(min_prob):.2f}"
                            f" ({min_prob_reason})"
                        ),
                        signal_symbol=str(getattr(signal, "symbol", "") or ""),
                    )
                    self._handle_mt5_result(signal, skipped, source=source)
                    continue
            volume_multiplier = None
            if getattr(config, "MT5_AUTOPILOT_ENABLED", True):
                plan = mt5_orchestrator.pre_trade_plan(signal, source=source)
                if not plan.allow:
                    skipped = MT5ExecutionResult(
                        ok=False,
                        status="guard_blocked",
                        message=str(plan.reason or "risk governor blocked"),
                        signal_symbol=str(getattr(signal, "symbol", "") or ""),
                    )
                    self._handle_mt5_result(signal, skipped, source=source)
                    attempted += 1
                    continue
                volume_multiplier = float(plan.risk_multiplier or 1.0)
                try:
                    raw_scores = dict(getattr(signal, "raw_scores", {}) or {})
                    raw_scores["mt5_risk_multiplier"] = round(volume_multiplier, 4)
                    raw_scores["mt5_canary_mode"] = bool(plan.canary_mode)
                    raw_scores["mt5_walkforward_reason"] = str((plan.walkforward or {}).get("reason", plan.reason))
                    signal.raw_scores = raw_scores
                except Exception:
                    pass
            result = mt5_executor.execute_signal(signal, source=source, volume_multiplier=volume_multiplier)
            self._handle_mt5_result(signal, result, source=source)
            consume_attempt = True
            if bool(getattr(config, "MT5_MICRO_MODE_ENABLED", False)):
                status = str(getattr(result, "status", "") or "").lower()
                msg = str(getattr(result, "message", "") or "").lower()
                if status in {"micro_filtered", "unmapped"}:
                    consume_attempt = False
                elif status == "skipped" and "margin guard" in msg:
                    consume_attempt = False
            if consume_attempt:
                attempted += 1
            if result.ok:
                executed += 1
        if attempted >= max_attempts and executed < max_count:
            logger.info(
                "[MT5] %s attempts capped: executed=%d attempted=%d max_signals=%d",
                source,
                executed,
                attempted,
                max_count,
            )

    def _run_neural_sync_train(self):
        """Sync outcomes into learning DB and optionally auto-train."""
        if not config.NEURAL_BRAIN_ENABLED:
            return
        try:
            mt5_labels = 0
            market_labels = 0

            sync = neural_brain.sync_outcomes_from_mt5(days=config.NEURAL_BRAIN_SYNC_DAYS)
            if sync.get("ok"):
                mt5_labels = int(sync.get("updated", 0) or 0)
                logger.info(
                    "[NeuralBrain] sync updated=%s closed_positions=%s",
                    sync.get("updated", 0),
                    sync.get("closed_positions", 0),
                )
            else:
                logger.warning("[NeuralBrain] mt5 sync failed: %s", sync.get("message", "unknown"))

            if config.SIGNAL_FEEDBACK_ENABLED:
                feedback = neural_brain.sync_signal_outcomes_from_market(
                    days=config.NEURAL_BRAIN_SYNC_DAYS,
                    max_records=config.NEURAL_BRAIN_SIGNAL_FEEDBACK_MAX_RECORDS,
                )
                if feedback.get("ok"):
                    market_labels = int(feedback.get("resolved", 0) or 0)
                    logger.info(
                        "[NeuralBrain] feedback reviewed=%s resolved=%s pseudo=%s updated=%s",
                        feedback.get("reviewed", 0),
                        feedback.get("resolved", 0),
                        feedback.get("pseudo_labeled", 0),
                        feedback.get("updated", 0),
                    )
                else:
                    logger.warning("[NeuralBrain] feedback sync failed: %s", feedback.get("message", "unknown"))

            if config.NEURAL_BRAIN_AUTO_TRAIN:
                model = neural_brain.model_status()
                new_labels = int(mt5_labels + market_labels)
                should_train = (not model.get("available")) or (new_labels > 0)
                if should_train:
                    train_min_samples = int(config.NEURAL_BRAIN_MIN_SAMPLES)
                    if not model.get("available"):
                        bootstrap_min = max(10, int(config.NEURAL_BRAIN_BOOTSTRAP_MIN_SAMPLES))
                        train_min_samples = min(train_min_samples, bootstrap_min)
                    train = neural_brain.train_backprop(
                        days=config.NEURAL_BRAIN_SYNC_DAYS,
                        min_samples=train_min_samples,
                    )
                    if train.ok:
                        logger.info(
                            "[NeuralBrain] trained samples=%d train_acc=%.2f val_acc=%.2f win_rate=%.2f (min_samples=%d)",
                            train.samples,
                            train.train_accuracy,
                            train.val_accuracy,
                            train.win_rate,
                            train_min_samples,
                        )
                    else:
                        logger.info("[NeuralBrain] train skipped: %s", train.message)
                else:
                    logger.info("[NeuralBrain] no new labels; training not required")
        except Exception as e:
            logger.warning("[NeuralBrain] sync/train error: %s", e)

    def _run_mt5_autopilot_sync(self):
        """Sync MT5 closed outcomes into forward-test journal + calibration stats."""
        if not (config.MT5_ENABLED and getattr(config, "MT5_AUTOPILOT_ENABLED", True)):
            return
        try:
            report = mt5_autopilot_core.sync_outcomes_from_mt5(
                hours=max(24, int(getattr(config, "NEURAL_BRAIN_SYNC_DAYS", 120)) * 24)
            )
            if report.get("ok"):
                logger.info(
                    "[MT5Autopilot] sync closed=%s updated=%s q=%s labeled7d=%s win_rate=%.2f mae=%s",
                    report.get("closed_rows_seen", 0),
                    report.get("updated", 0),
                    report.get("history_query_mode", "-"),
                    report.get("labeled_7d", 0),
                    float(report.get("win_rate_7d", 0.0) or 0.0),
                    (f"{float(report.get('mae_7d')):.3f}" if report.get("mae_7d") is not None else "-"),
                )
            else:
                logger.info("[MT5Autopilot] sync skipped: %s", report.get("message", "unknown"))
            try:
                orch = mt5_orchestrator.sync_current_account()
                if orch.get("ok"):
                    logger.info("[MT5Orchestrator] synced current account: %s", orch.get("account_key"))
            except Exception as e:
                logger.debug("[MT5Orchestrator] sync error: %s", e)
            try:
                pm_learn = mt5_position_manager.sync_learning_outcomes(
                    hours=max(24, int(getattr(config, "MT5_PM_LEARNING_SYNC_HOURS", 168)))
                )
                if pm_learn.get("ok"):
                    logger.info(
                        "[MT5PM-Learn] closed=%s updated=%s unresolved=%s q=%s",
                        pm_learn.get("closed_rows_seen", 0),
                        pm_learn.get("updated", 0),
                        pm_learn.get("still_unresolved", 0),
                        pm_learn.get("history_query_mode", "-"),
                    )
                elif str(pm_learn.get("error") or "") not in {"disabled"}:
                    logger.debug("[MT5PM-Learn] sync skipped: %s", pm_learn.get("error", "unknown"))
            except Exception as e:
                logger.debug("[MT5PM-Learn] sync error: %s", e)
        except Exception as e:
            logger.warning("[MT5Autopilot] sync error: %s", e)

    def _run_mt5_position_manager(self):
        """Autonomous MT5 position management cycle (BE / trail / partial / time-stop)."""
        if not (config.MT5_ENABLED and getattr(config, "MT5_POSITION_MANAGER_ENABLED", True)):
            return
        try:
            report = mt5_position_manager.run_cycle(source="scheduler")
            if report.get("ok"):
                actions = list(report.get("actions", []) or [])
                if actions:
                    def _pm_act_label(a: dict) -> str:
                        label = f"{a.get('symbol')}:{a.get('action')}:{a.get('status')}"
                        try:
                            rc = a.get('retcode')
                            if rc is not None:
                                label += f"(retcode={int(rc)})"
                        except Exception:
                            pass
                        return label

                    logger.info(
                        "[MT5PM] positions=%s checked=%s managed=%s actions=%s",
                        report.get("positions", 0),
                        report.get("checked", 0),
                        report.get("managed", 0),
                        ", ".join(_pm_act_label(a) for a in actions[:5]),
                    )
                    if bool(getattr(config, "MT5_PM_NOTIFY_ACTIONS", True)):
                        try:
                            notifier.send_mt5_position_manager_update(report, source="scheduler")
                        except Exception as e:
                            logger.warning("[MT5PM] notify failed: %s", e)
            else:
                logger.debug("[MT5PM] cycle skipped: %s", report.get("error", "unknown"))
        except Exception as e:
            logger.warning("[MT5PM] cycle error: %s", e)

    def _evaluate_xauusd_cooldown(self, signal) -> tuple[bool, dict]:
        """
        Evaluate duplicate-alert cooldown state for XAUUSD.
        """
        data: dict = {"reason": "ok", "elapsed_sec": 0.0, "remaining_sec": 0.0}
        now_ts = time.time()
        if self._last_xauusd_alert_ts <= 0:
            data["reason"] = "first_signal"
            return True, data

        elapsed = now_ts - self._last_xauusd_alert_ts
        data["elapsed_sec"] = float(elapsed)
        cooldown = max(60, int(config.XAUUSD_ALERT_COOLDOWN_SEC))
        data["cooldown_sec"] = float(cooldown)
        if elapsed >= cooldown:
            data["reason"] = "cooldown_elapsed"
            return True, data

        # Direction flip should always be alerted even inside cooldown.
        if signal.direction != self._last_xauusd_direction:
            data["reason"] = "direction_flip"
            return True, data

        atr_ref = max(0.01, float(signal.atr or 0), float(self._last_xauusd_atr or 0))
        move = abs(float(signal.entry) - float(self._last_xauusd_entry))
        data["atr_ref"] = float(atr_ref)
        data["price_delta"] = float(move)
        data["move_threshold"] = float(0.6 * atr_ref)
        if move >= 0.6 * atr_ref:
            data["reason"] = "meaningful_price_move"
            return True, data

        data["reason"] = "cooldown_active"
        data["remaining_sec"] = float(max(0.0, cooldown - elapsed))
        logger.info(
            "[Scheduler] XAUUSD signal suppressed by cooldown "
            f"({elapsed:.0f}s < {cooldown}s, delta={move:.2f}, atr_ref={atr_ref:.2f})"
        )
        return False, data

    def _mark_xauusd_alert_sent(self, signal) -> None:
        ts = time.time()
        self._last_xauusd_alert_ts = ts
        self._last_xauusd_direction = str(signal.direction)
        self._last_xauusd_entry = float(signal.entry)
        self._last_xauusd_atr = float(signal.atr or 0)
        self._last_xauusd_signal_snapshot = {
            'ts': ts,
            'symbol': str(getattr(signal, 'symbol', 'XAUUSD') or 'XAUUSD'),
            'direction': str(getattr(signal, 'direction', '') or ''),
            'entry': float(getattr(signal, 'entry', 0.0) or 0.0),
            'confidence': float(getattr(signal, 'confidence', 0.0) or 0.0),
        }

    def _attach_xau_previous_signal_context(self, result: dict) -> None:
        try:
            snap = dict(self._last_xauusd_signal_snapshot or {})
            if not snap:
                return
            age_sec = max(0.0, time.time() - float(snap.get('ts', 0.0) or 0.0))
            if age_sec > 3600:
                return
            out = dict(snap)
            out['age_sec'] = round(age_sec, 1)
            result['previous_signal'] = out
        except Exception:
            return

    def _run_xauusd_scan(self, force_alert: bool = False, source: str = "scheduled"):
        """Execute XAUUSD scan and send alert if signal found."""
        session_info = session_manager.get_session_info()
        now_utc = datetime.now(timezone.utc)
        result = {
            "task": "xauusd",
            "source": source,
            "forced": bool(force_alert),
            "status": "unknown",
            "signal_sent": False,
            "session_info": session_info,
            "weekend": bool(now_utc.weekday() >= 5),
            "confidence_threshold": float(config.MIN_SIGNAL_CONFIDENCE),
            "cooldown": {},
            "error": "",
        }
        try:
            logger.info("[Scheduler] Running XAUUSD scan...")
            signal = xauusd_scanner.scan()
            if signal is None:
                result["status"] = "no_signal"
                try:
                    result["diagnostics"] = xauusd_scanner.get_last_scan_diagnostics()
                except Exception:
                    result["diagnostics"] = {}
                logger.info("[Scheduler] XAUUSD: No qualifying signal")
                try:
                    self._attach_xau_previous_signal_context(result)
                    notifier.send_xauusd_scan_status(result)
                except Exception:
                    logger.debug("[Scheduler] XAUUSD no-signal status send failed", exc_info=True)
                return result

            result["signal"] = {
                "symbol": str(signal.symbol),
                "direction": str(signal.direction),
                "confidence": float(signal.confidence),
                "entry": float(signal.entry),
                "stop_loss": float(signal.stop_loss),
                "take_profit_2": float(signal.take_profit_2),
                "atr": float(signal.atr or 0),
            }

            if signal.confidence < config.MIN_SIGNAL_CONFIDENCE:
                result["status"] = "below_confidence"
                try:
                    result["diagnostics"] = xauusd_scanner.get_last_scan_diagnostics()
                except Exception:
                    result["diagnostics"] = {}
                logger.info(
                    "[Scheduler] XAUUSD signal below confidence threshold (%.1f < %.1f)",
                    float(signal.confidence),
                    float(config.MIN_SIGNAL_CONFIDENCE),
                )
                try:
                    self._attach_xau_previous_signal_context(result)
                    notifier.send_xauusd_scan_status(result)
                except Exception:
                    logger.debug("[Scheduler] XAUUSD below-confidence status send failed", exc_info=True)
                return result

            self._apply_neural_soft_adjustment(signal, source=f"xauusd_{source}")
            result["signal"]["confidence_raw"] = self._raw_confidence(signal)
            result["signal"]["confidence_adjusted"] = float(signal.confidence)

            allow_by_cooldown, cooldown_data = self._evaluate_xauusd_cooldown(signal)
            result["cooldown"] = cooldown_data
            should_send = force_alert or allow_by_cooldown
            if should_send:
                if force_alert and not allow_by_cooldown:
                    result["status"] = "sent_manual_bypass_cooldown"
                    logger.info("[Scheduler] XAUUSD manual scan: bypassing cooldown")
                else:
                    result["status"] = "sent"
                logger.info("[Scheduler] XAUUSD signal found! Sending alert...")
                sent = notifier.send_signal(signal)
                if sent:
                    result["signal_sent"] = True
                    self._mark_xauusd_alert_sent(signal)
                    if config.SIGNAL_FEEDBACK_ENABLED:
                        neural_brain.record_signal_sent(signal, source=f"xauusd_{source}")
                    if config.MT5_EXECUTE_XAUUSD:
                        self._maybe_execute_mt5_signal(signal, source="xauusd")
                else:
                    result["status"] = "send_failed"
            else:
                result["status"] = "cooldown_suppressed"
                result["signal_sent"] = False
                try:
                    result["diagnostics"] = xauusd_scanner.get_last_scan_diagnostics()
                except Exception:
                    result["diagnostics"] = {}
                try:
                    self._attach_xau_previous_signal_context(result)
                    notifier.send_xauusd_scan_status(result)
                except Exception:
                    logger.debug("[Scheduler] XAUUSD cooldown status send failed", exc_info=True)

            return result
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            logger.error(f"[Scheduler] XAUUSD scan error: {e}", exc_info=True)
            notifier.send_error(f"XAUUSD scan failed: {str(e)[:200]}")
            return result

    def _run_crypto_scan(self, force: bool = False):
        """Execute crypto scan and send alerts for top opportunities."""
        try:
            logger.info("[Scheduler] Running Crypto Sniper scan...")
            opps = crypto_sniper.get_top_n(5)

            # Manual /scan_crypto keeps original full-report behavior.
            if force or (not config.CRYPTO_AUTO_FOCUS_ONLY):
                if not opps:
                    logger.info("[Scheduler] Crypto: No qualifying signals")
                    if force:
                        notifier.send_crypto_scan_summary([])
                    return

                new_opps = [opp for opp in opps if opp.signal.symbol not in self._last_signal_symbols]
                if new_opps:
                    for opp in new_opps:
                        self._apply_neural_soft_adjustment(opp.signal, source="crypto")

                    sent_summary = notifier.send_crypto_scan_summary(new_opps)
                    if sent_summary and config.SIGNAL_FEEDBACK_ENABLED:
                        for opp in new_opps:
                            neural_brain.record_signal_sent(opp.signal, source="crypto_summary")
                    top = new_opps[0]
                    if self._raw_confidence(top.signal) >= config.MIN_SIGNAL_CONFIDENCE + 5:
                        notifier.send_signal(top.signal)

                    if config.MT5_EXECUTE_CRYPTO:
                        self._maybe_execute_mt5_batch([opp.signal for opp in new_opps], source="crypto")

                    for opp in new_opps:
                        self._last_signal_symbols.add(opp.signal.symbol)
                else:
                    logger.info("[Scheduler] Crypto: All signals already alerted recently")
                return

            focus_symbols = {s.upper() for s in config.get_crypto_auto_focus_symbols()}
            focus_alias_map = {
                "BTCUSD": {"BTCUSD", "BTC/USDT"},
                "ETHUSD": {"ETHUSD", "ETH/USDT"},
            }

            def _is_focus_symbol(sym: str) -> bool:
                su = str(sym or "").upper()
                if su in focus_symbols:
                    return True
                for aliases in focus_alias_map.values():
                    if su in aliases and (aliases & focus_symbols):
                        return True
                return False

            focus_opps = [opp for opp in (opps or []) if _is_focus_symbol(getattr(opp.signal, "symbol", ""))]
            new_focus = [opp for opp in focus_opps if opp.signal.symbol not in self._last_signal_symbols]

            if new_focus:
                for opp in new_focus:
                    self._apply_neural_soft_adjustment(opp.signal, source="crypto")

                sent_summary = notifier.send_crypto_focus_status(sorted(focus_symbols), new_focus)
                if sent_summary and config.SIGNAL_FEEDBACK_ENABLED:
                    for opp in new_focus:
                        neural_brain.record_signal_sent(opp.signal, source="crypto_focus")

                for opp in new_focus:
                    if self._raw_confidence(opp.signal) >= config.MIN_SIGNAL_CONFIDENCE + 5:
                        notifier.send_signal(opp.signal)

                if config.MT5_EXECUTE_CRYPTO:
                    self._maybe_execute_mt5_batch([opp.signal for opp in new_focus], source="crypto")

                for opp in new_focus:
                    self._last_signal_symbols.add(opp.signal.symbol)
                return

            if focus_opps:
                logger.info("[Scheduler] Crypto focus: signals exist but already alerted recently")
                return

            logger.info("[Scheduler] Crypto focus: BTC/ETH no signal")
            if config.CRYPTO_AUTO_FOCUS_NO_SIGNAL_REPORT:
                now_ts = time.time()
                min_gap = max(1, int(config.CRYPTO_AUTO_FOCUS_NO_SIGNAL_INTERVAL_MIN)) * 60
                if (now_ts - float(self._last_crypto_focus_no_signal_ts)) >= min_gap:
                    if notifier.send_crypto_focus_status(sorted(focus_symbols), []):
                        self._last_crypto_focus_no_signal_ts = now_ts

        except Exception as e:
            logger.error(f"[Scheduler] Crypto scan error: {e}", exc_info=True)

    def _run_fx_scan(self, force: bool = False):
        """Execute FX major scan and send alerts for top opportunities."""
        try:
            logger.info("[Scheduler] Running FX Major scan...")
            opps = fx_major_scanner.get_top_n(max(1, int(getattr(config, "FX_TOP_N", 5))))
            if not opps:
                diag = fx_major_scanner.get_last_scan_diagnostics()
                if diag:
                    rr = dict(diag.get("reject_reasons", {}) or {})
                    pf = dict(diag.get("prefilter", {}) or {})
                    logger.info(
                        "[Scheduler] FX diagnostics: prefilter kept=%s/%s unmapped=%s | market_closed=%s no_entry=%s no_trend=%s no_signal=%s guard_blocked=%s exception=%s",
                        pf.get("kept", diag.get("symbols", 0)),
                        pf.get("input", diag.get("symbols_input", diag.get("symbols", 0))),
                        pf.get("unmapped", 0),
                        rr.get("market_closed", 0),
                        rr.get("no_entry_data", 0),
                        rr.get("no_trend_data", 0),
                        rr.get("no_signal", 0),
                        rr.get("guard_blocked", 0),
                        rr.get("exception", 0),
                    )
                logger.info("[Scheduler] FX: No qualifying signals")
                if force:
                    notifier.send_fx_scan_summary([])
                return

            new_opps = [opp for opp in opps if opp.signal.symbol not in self._last_signal_symbols]
            send_list = new_opps if new_opps else ([] if not force else opps)
            if not send_list:
                logger.info("[Scheduler] FX: All signals already alerted recently")
                return

            for opp in send_list:
                self._apply_neural_soft_adjustment(opp.signal, source="fx")

            sent_summary = notifier.send_fx_scan_summary(send_list)
            if sent_summary and config.SIGNAL_FEEDBACK_ENABLED:
                for opp in send_list:
                    neural_brain.record_signal_sent(opp.signal, source="fx_summary")

            top = send_list[0]
            if self._raw_confidence(top.signal) >= max(int(getattr(config, "FX_MIN_CONFIDENCE", config.MIN_SIGNAL_CONFIDENCE)), int(config.MIN_SIGNAL_CONFIDENCE)) + 5:
                notifier.send_signal(top.signal)

            if bool(getattr(config, "MT5_EXECUTE_FX", False)):
                self._maybe_execute_mt5_batch([opp.signal for opp in send_list], source="fx")

            for opp in send_list:
                self._last_signal_symbols.add(opp.signal.symbol)
        except Exception as e:
            logger.error(f"[Scheduler] FX scan error: {e}", exc_info=True)

    def _run_gold_overview(self):
        """Send XAUUSD market overview (morning briefing)."""
        try:
            overview = xauusd_scanner.get_market_overview()
            notifier.send_xauusd_overview(overview)
        except Exception as e:
            logger.error(f"[Scheduler] Gold overview error: {e}")

    def _run_stock_scan(self):
        """Scan all currently open stock markets and send alerts."""
        try:
            logger.info("[Scheduler] Running Global Stock scan...")
            opps_all = stock_scanner.scan_all_open_markets()
            if opps_all:
                opps = stock_scanner.filter_quality(opps_all, min_score=2)
                logger.info("[Scheduler] Stocks quality filter: %d/%d passed", len(opps), len(opps_all))
                if opps:
                    send_list = [o for o in opps if o.signal.symbol not in self._last_signal_symbols]
                    for opp in (send_list or opps):
                        self._apply_neural_soft_adjustment(opp.signal, source="stocks")
                    if send_list:
                        sent_summary = notifier.send_stock_scan_summary(send_list, market_label="OPEN MARKETS (QUALITY)")
                        if sent_summary and config.SIGNAL_FEEDBACK_ENABLED:
                            for opp in send_list:
                                neural_brain.record_signal_sent(opp.signal, source="stocks_quality")
                    else:
                        logger.info("[Scheduler] Stocks: quality signals already alerted recently")
                    # Detailed signal for #1 pick
                    top = opps[0]
                    if self._raw_confidence(top.signal) >= config.STOCK_MIN_CONFIDENCE + 5:
                        notifier.send_stock_signal(top)
                    if config.MT5_EXECUTE_STOCKS:
                        self._maybe_execute_mt5_batch(
                            [opp.signal for opp in send_list or opps],
                            source="stocks",
                        )
                    for o in opps:
                        self._last_signal_symbols.add(o.signal.symbol)
                else:
                    watchlist = stock_scanner.filter_watchlist(opps_all)[:config.WATCHLIST_MAX_RESULTS]
                    logger.info(
                        "[Scheduler] Stocks watchlist filter: %d/%d passed",
                        len(watchlist),
                        len(opps_all),
                    )
                    if watchlist:
                        for opp in watchlist:
                            self._apply_neural_soft_adjustment(opp.signal, source="stocks_watchlist")
                        logger.info("[Scheduler] Stocks: no quality signals, sending filtered watchlist snapshot")
                        sent_summary = notifier.send_stock_scan_summary(
                            watchlist,
                            market_label=f"OPEN MARKETS (WATCHLIST, vol>={config.WATCHLIST_MIN_VOL_RATIO:.1f}x)",
                        )
                        if sent_summary and config.SIGNAL_FEEDBACK_ENABLED:
                            for opp in watchlist:
                                neural_brain.record_signal_sent(opp.signal, source="stocks_watchlist")
                    else:
                        logger.info("[Scheduler] Stocks: no quality/watchlist signals passed filters; skipping alert")
            else:
                logger.info("[Scheduler] Stocks: No qualifying signals")
        except Exception as e:
            logger.error(f"[Scheduler] Stock scan error: {e}", exc_info=True)

    def _run_thai_scan(self):
        """Dedicated Thailand SET50 market scan — triggered at Thai open."""
        try:
            logger.info("[Scheduler] Running Thailand SET50 scan...")
            opps = stock_scanner.scan_thailand()
            if opps:
                for opp in opps:
                    self._apply_neural_soft_adjustment(opp.signal, source="stocks_thailand")
                sent_summary = notifier.send_stock_scan_summary(opps, market_label="🇹🇭 THAILAND SET50")
                if sent_summary and config.SIGNAL_FEEDBACK_ENABLED:
                    for opp in opps:
                        neural_brain.record_signal_sent(opp.signal, source="stocks_thailand")
                if self._raw_confidence(opps[0].signal) >= config.STOCK_MIN_CONFIDENCE + 5:
                    notifier.send_stock_signal(opps[0])
            else:
                diag = stock_scanner.get_last_scan_diagnostics("SET50_TH")
                if diag:
                    up = (diag.get("reject_reasons", {}) or {})
                    logger.info(
                        "[Scheduler] Thailand diagnostics: symbols=%s market_closed=%s no_data=%s no_signal=%s exception=%s",
                        diag.get("symbols", 0),
                        up.get("market_closed", 0),
                        up.get("no_entry_data", 0),
                        up.get("no_signal", 0),
                        up.get("exception", 0),
                    )
                logger.info("[Scheduler] Thailand: No qualifying signals")
        except Exception as e:
            logger.error(f"[Scheduler] Thai scan error: {e}")

    def _run_thai_vi_stock_scan(self, force: bool = False):
        """VI-style Thailand SET50 value + trend scan (off-hours friendly)."""
        try:
            logger.info("[Scheduler] Running THAILAND VALUE+TREND scan...")
            top_n = max(3, int(getattr(config, "VI_TOP_N", 10)))
            opps = stock_scanner.scan_thailand_value_trend(top_n=top_n)
            if not opps:
                diag = stock_scanner.get_last_scan_diagnostics("TH_VI")
                if diag:
                    up = (diag.get("reject_reasons", {}) or {})
                    vf = (diag.get("vi_filter", {}) or {})
                    logger.info(
                        "[Scheduler] Thailand VI diagnostics: symbols=%s market_closed=%s no_data=%s no_signal=%s exception=%s",
                        diag.get("symbols", 0),
                        up.get("market_closed", 0),
                        up.get("no_entry_data", 0),
                        up.get("no_signal", 0),
                        up.get("exception", 0),
                    )
                    if vf:
                        logger.info(
                            "[Scheduler] Thailand VI filter diagnostics: raw=%s pass=%s long=%s fail_conf=%s fail_vol=%s fail_dv=%s fail_q=%s fail_wr=%s fail_rsi=%s fail_trend=%s fail_dir=%s",
                            vf.get("raw_opportunities", 0),
                            vf.get("base_passed", 0),
                            vf.get("after_direction", 0),
                            vf.get("fail_confidence", 0),
                            vf.get("fail_volume", 0),
                            vf.get("fail_dollar_volume", 0),
                            vf.get("fail_quality", 0),
                            vf.get("fail_setup_wr", 0),
                            vf.get("fail_rsi", 0),
                            vf.get("fail_trend", 0),
                            vf.get("fail_direction", 0),
                        )
                logger.info("[Scheduler] Thailand VI scan: No qualifying candidates")
                if force:
                    notifier.send_vi_stock_summary([], region_label="🇹🇭 THAILAND", feature_override="scan_thai_vi")
                    try:
                        diag = stock_scanner.get_last_scan_diagnostics("TH_VI")
                        vf = (diag.get("vi_filter", {}) or {}) if diag else {}
                        if vf:
                            th = vf.get("thresholds", {}) or {}
                            msg = (
                                "TH VI filter diagnostics\n"
                                f"raw={vf.get('raw_opportunities',0)} pass={vf.get('base_passed',0)} long={vf.get('after_direction',0)}\n"
                                f"fail conf={vf.get('fail_confidence',0)} q={vf.get('fail_quality',0)} vol={vf.get('fail_volume',0)} dv={vf.get('fail_dollar_volume',0)} wr={vf.get('fail_setup_wr',0)} rsi={vf.get('fail_rsi',0)} trend={vf.get('fail_trend',0)} dir={vf.get('fail_direction',0)}\n"
                                f"thresholds: conf>={th.get('min_confidence')} vol>={th.get('min_vol_ratio')} dv>={th.get('min_dollar_volume')} wr>={th.get('min_setup_win_rate')} rsi={th.get('rsi_min')}-{th.get('rsi_max')} q>={th.get('min_quality_score')}"
                            )
                            notifier._send(notifier._escape(msg), feature="scan_thai_vi")
                    except Exception:
                        pass
                return

            for opp in opps:
                self._apply_neural_soft_adjustment(opp.signal, source="stocks_thai_vi")
            sent_summary = notifier.send_vi_stock_summary(opps, region_label="🇹🇭 THAILAND")
            if sent_summary and config.SIGNAL_FEEDBACK_ENABLED:
                for opp in opps:
                    neural_brain.record_signal_sent(opp.signal, source="stocks_thai_vi")

            top = opps[0]
            if self._raw_confidence(top.signal) >= config.STOCK_MIN_CONFIDENCE + 5:
                notifier.send_stock_signal(top, feature_override="scan_thai_vi")
        except Exception as e:
            logger.error(f"[Scheduler] Thailand VI scan error: {e}", exc_info=True)

    def _run_us_scan(self):
        """US market scan — triggered at NYSE open."""
        try:
            logger.info("[Scheduler] Running US market scan...")
            opps_all = stock_scanner.scan_us()
            opps = stock_scanner.filter_quality(opps_all, min_score=2)
            logger.info("[Scheduler] US quality filter: %d/%d passed", len(opps), len(opps_all))
            if opps:
                for opp in opps:
                    self._apply_neural_soft_adjustment(opp.signal, source="stocks_us")
                sent_summary = notifier.send_stock_scan_summary(opps, market_label="🇺🇸 US MARKETS")
                if sent_summary and config.SIGNAL_FEEDBACK_ENABLED:
                    for opp in opps:
                        neural_brain.record_signal_sent(opp.signal, source="stocks_us")
                if self._raw_confidence(opps[0].signal) >= config.STOCK_MIN_CONFIDENCE + 5:
                    notifier.send_stock_signal(opps[0], feature_override="scan_us_open")
                if config.MT5_EXECUTE_STOCKS:
                    self._maybe_execute_mt5_batch([opp.signal for opp in opps], source="stocks_us")
            elif opps_all:
                watchlist = stock_scanner.filter_watchlist(opps_all)[:config.WATCHLIST_MAX_RESULTS]
                logger.info(
                    "[Scheduler] US watchlist filter: %d/%d passed",
                    len(watchlist),
                    len(opps_all),
                )
                if watchlist:
                    for opp in watchlist:
                        self._apply_neural_soft_adjustment(opp.signal, source="stocks_us_watchlist")
                    sent_summary = notifier.send_stock_scan_summary(
                        watchlist,
                        market_label=f"🇺🇸 US WATCHLIST (vol>={config.WATCHLIST_MIN_VOL_RATIO:.1f}x)",
                    )
                    if sent_summary and config.SIGNAL_FEEDBACK_ENABLED:
                        for opp in watchlist:
                            neural_brain.record_signal_sent(opp.signal, source="stocks_us_watchlist")
                else:
                    logger.info("[Scheduler] US: no watchlist candidates passed filters; skipping alert")
        except Exception as e:
            logger.error(f"[Scheduler] US scan error: {e}")

    def _run_us_open_daytrade(self, force: bool = False):
        """Run US open day-trade selector (top 10) in first 1-2h after NY open."""
        try:
            ny_now = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
            if ny_now.weekday() >= 5:
                return
            is_premarket = ny_now.time() < dt_time(9, 30)

            # Gate by local NY open window to support DST automatically.
            if (not force) and (not ((ny_now.hour == 9 and ny_now.minute >= 30) or (ny_now.hour == 10))):
                return

            day_key = ny_now.strftime("%Y-%m-%d")
            if (not force) and self._last_us_open_plan_date == day_key:
                return

            logger.info("[Scheduler] Running US OPEN day-trade selector...")
            if force and is_premarket:
                logger.info("[Scheduler] US OPEN plan forced during pre-market; using prep mode")
            opps = stock_scanner.scan_us_open_daytrade(top_n=10, allow_premarket=bool(force and is_premarket))
            if (not force):
                macro_freeze, macro_reason = self._check_us_open_macro_freeze()
                if macro_freeze:
                    logger.info("[Scheduler] US OPEN plan: macro-freeze engaged (%s)", macro_reason)
                    return
                cb_stop, cb_reason = self._check_us_open_quality_circuit_breaker(ny_now)
                if cb_stop:
                    logger.info("[Scheduler] US OPEN plan: circuit-breaker engaged (%s)", cb_reason)
                    return
            if opps:
                for opp in opps:
                    self._apply_neural_soft_adjustment(opp.signal, source="us_open")
                opps, usq_diag = self._apply_us_open_quality_filters(opps, stage="plan")
                self._log_us_open_quality_guard_diag(usq_diag, stage="plan")
                if not opps:
                    logger.info("[Scheduler] US OPEN plan: all candidates filtered by setup/symbol quality guard")
                    return
                sent_summary = notifier.send_us_open_daytrade_summary(opps)
                if sent_summary and config.SIGNAL_FEEDBACK_ENABLED:
                    for opp in opps:
                        neural_brain.record_signal_sent(opp.signal, source="us_open_plan")
                # Keep one detailed alert for the best candidate.
                if self._raw_confidence(opps[0].signal) >= config.STOCK_MIN_CONFIDENCE + 5:
                    notifier.send_stock_signal(opps[0], feature_override="scan_us_open")
                if config.MT5_EXECUTE_STOCKS:
                    self._maybe_execute_mt5_batch([opp.signal for opp in opps], source="us_open")
                self._last_us_open_plan_date = day_key
            else:
                diag = stock_scanner.get_last_us_open_diagnostics()
                if diag:
                    strict = diag.get("strict_filter", {}) or {}
                    upstream = diag.get("upstream", {}) or {}
                    up = (upstream.get("reject_reasons", {}) or {})
                    pf = (upstream.get("prefilter", {}) or {})
                    logger.info(
                        "[Scheduler] US OPEN plan diagnostics (%s): "
                        "prefilter kept=%s/%s unmapped=%s | "
                        "upstream market_closed=%s no_data=%s no_signal=%s | "
                        "strict raw=%s pass=%s fail_conf=%s fail_vol=%s fail_dv=%s",
                        diag.get("mode", "-"),
                        pf.get("kept", upstream.get("symbols", 0)),
                        pf.get("input", upstream.get("symbols_input", upstream.get("symbols", 0))),
                        pf.get("unmapped", 0),
                        up.get("market_closed", 0),
                        up.get("no_entry_data", 0),
                        up.get("no_signal", 0),
                        strict.get("total_opportunities", 0),
                        strict.get("passed", 0),
                        strict.get("fail_confidence", 0),
                        strict.get("fail_volume", 0),
                        strict.get("fail_dollar_volume", 0),
                    )
                logger.info("[Scheduler] US OPEN plan: No qualifying signals")
        except Exception as e:
            logger.error(f"[Scheduler] US OPEN plan error: {e}", exc_info=True)

    def _in_us_open_window(self, ny_now: datetime) -> bool:
        """
        Focused monitoring window:
        - Starts before NY cash open (pre-market lead time)
        - Continues through the first configured minutes after open
        """
        try:
            lead_min = max(0, int(getattr(config, "US_OPEN_SMART_PREMARKET_LEAD_MIN", 60) or 60))
        except Exception:
            lead_min = 60
        try:
            post_open_max_min = max(30, int(getattr(config, "US_OPEN_SMART_POST_OPEN_MAX_MIN", 120) or 120))
        except Exception:
            post_open_max_min = 120
        open_dt = ny_now.replace(hour=9, minute=30, second=0, microsecond=0)
        start_dt = open_dt - timedelta(minutes=lead_min)
        end_dt = open_dt + timedelta(minutes=post_open_max_min, seconds=59)
        return start_dt <= ny_now <= end_dt

    def _reset_us_open_mood_state_if_new_day(self, ny_now: datetime) -> None:
        day_key = ny_now.strftime("%Y-%m-%d")
        if self._us_open_mood_day != day_key:
            self._us_open_mood_day = day_key
            self._us_open_mood_weak_cycles = 0
            self._us_open_mood_stop_triggered = False
            self._us_open_mood_stop_reason = ""
            self._us_open_symbol_alert_ts = {}
            self._us_open_circuit_day = day_key
            self._us_open_circuit_triggered = False
            self._us_open_circuit_reason = ""
            self._us_open_symbol_recovery_state = {}
            self._us_open_quality_guard_last_diag = {"plan": {}, "monitor": {}}
            self._us_open_quality_guard_last_diag_ts = 0.0

    def _us_open_elapsed_after_open_min(self, ny_now: datetime) -> float:
        open_dt = ny_now.replace(hour=9, minute=30, second=0, microsecond=0)
        return max(0.0, (ny_now - open_dt).total_seconds() / 60.0)

    @staticmethod
    def _median_float(values: list[float]) -> float:
        vals = sorted(float(v) for v in values if v is not None)
        if not vals:
            return 0.0
        n = len(vals)
        mid = n // 2
        if n % 2 == 1:
            return vals[mid]
        return (vals[mid - 1] + vals[mid]) / 2.0

    def _update_us_open_mood_stop(self, ny_now: datetime, opps: list) -> tuple[bool, str]:
        self._reset_us_open_mood_state_if_new_day(ny_now)
        if self._us_open_mood_stop_triggered:
            return True, (self._us_open_mood_stop_reason or "mood_stop_active")
        if not bool(getattr(config, "US_OPEN_MOOD_STOP_ENABLED", True)):
            return False, "disabled"
        if ny_now.time() < dt_time(9, 30):
            return False, "premarket"

        elapsed_min = self._us_open_elapsed_after_open_min(ny_now)
        check_start_min = max(15, int(getattr(config, "US_OPEN_MOOD_CHECK_START_MIN", 45) or 45))
        if elapsed_min < check_start_min:
            self._us_open_mood_weak_cycles = 0
            return False, "warmup"

        weak_cycles_to_stop = max(2, int(getattr(config, "US_OPEN_MOOD_WEAK_CYCLES_TO_STOP", 3) or 3))
        count = len(opps or [])
        top_conf = float(getattr(opps[0].signal, "confidence", 0.0)) if count else 0.0
        median_vol = self._median_float([getattr(o, "vol_vs_avg", 0.0) for o in (opps or [])]) if count else 0.0
        min_conf = int(getattr(config, "STOCK_MIN_CONFIDENCE", 70))
        min_vol = float(getattr(config, "US_OPEN_MIN_VOL_RATIO", 1.0))

        weak = False
        if count == 0:
            weak = True
        elif count == 1 and (top_conf < (min_conf + 3) or median_vol < min_vol):
            weak = True
        elif count <= 2 and median_vol < max(0.8, min_vol * 0.9) and top_conf < (min_conf + 2):
            weak = True

        self._us_open_mood_weak_cycles = (self._us_open_mood_weak_cycles + 1) if weak else 0
        if self._us_open_mood_weak_cycles >= weak_cycles_to_stop:
            self._us_open_mood_stop_triggered = True
            self._us_open_mood_stop_reason = (
                f"weak_breadth x{self._us_open_mood_weak_cycles} "
                f"(elapsed={elapsed_min:.0f}m count={count} top_conf={top_conf:.1f} med_vol={median_vol:.2f})"
            )
            return True, self._us_open_mood_stop_reason

        return False, (
            f"monitoring(elapsed={elapsed_min:.0f}m weak={self._us_open_mood_weak_cycles}/{weak_cycles_to_stop} "
            f"count={count} top_conf={top_conf:.1f} med_vol={median_vol:.2f})"
        )

    def _check_us_open_macro_freeze(self) -> tuple[bool, str]:
        if not bool(getattr(config, "US_OPEN_MACRO_FREEZE_ENABLED", True)):
            return False, "disabled"
        try:
            min_score = max(1, int(getattr(config, "US_OPEN_MACRO_FREEZE_MIN_SCORE", 8) or 8))
            max_age_min = max(5, int(getattr(config, "US_OPEN_MACRO_FREEZE_MAX_AGE_MIN", 45) or 45))
            priority_only = bool(getattr(config, "US_OPEN_MACRO_FREEZE_PRIORITY_ONLY", True))
            heads = macro_news.high_impact_headlines(hours=4, min_score=min_score, limit=8)
            now_utc = datetime.now(timezone.utc)
            fresh = []
            for h in heads:
                age_min = max(0.0, (now_utc - h.published_utc).total_seconds() / 60.0)
                if age_min > max_age_min:
                    continue
                if priority_only and (not macro_news.is_priority_theme(h)):
                    continue
                fresh.append((h, age_min))
            if not fresh:
                return False, "clear"
            h, age = sorted(fresh, key=lambda x: (x[1], -int(getattr(x[0], 'score', 0))))[0]
            themes = ",".join(list(getattr(h, "themes", []) or [])[:3]) or "-"
            return True, f"macro_freeze score={int(getattr(h,'score',0))} age={age:.0f}m themes={themes}"
        except Exception as e:
            logger.debug("[Scheduler] US open macro freeze check skipped: %s", e)
            return False, "error"

    def _check_us_open_quality_circuit_breaker(self, ny_now: datetime) -> tuple[bool, str]:
        self._reset_us_open_mood_state_if_new_day(ny_now)
        if self._us_open_circuit_triggered:
            return True, (self._us_open_circuit_reason or "circuit_breaker_active")
        if not bool(getattr(config, "US_OPEN_CIRCUIT_BREAKER_ENABLED", True)):
            return False, "disabled"
        if ny_now.time() < dt_time(9, 30):
            return False, "premarket"
        elapsed_min = self._us_open_elapsed_after_open_min(ny_now)
        check_start_min = max(10, int(getattr(config, "US_OPEN_CIRCUIT_BREAKER_CHECK_START_MIN", 30) or 30))
        if elapsed_min < check_start_min:
            return False, "warmup"
        try:
            rpt = neural_brain.signal_feedback_report(days=1, source_contains="us_open")
            resolved = int(rpt.get("resolved", 0) or 0)
            wins = int(rpt.get("wins", 0) or 0)
            sl = int(rpt.get("sl", 0) or 0)
            win_rate = float(rpt.get("win_rate", 0.0) or 0.0)
            avg_r = float(rpt.get("avg_r_resolved", 0.0) or 0.0)
            min_resolved = max(3, int(getattr(config, "US_OPEN_CIRCUIT_BREAKER_MIN_RESOLVED", 8) or 8))
            max_wr = float(getattr(config, "US_OPEN_CIRCUIT_BREAKER_MAX_WIN_RATE", 25) or 25)
            min_sl = max(1, int(getattr(config, "US_OPEN_CIRCUIT_BREAKER_MIN_SL", 4) or 4))
            max_avg_r = float(getattr(config, "US_OPEN_CIRCUIT_BREAKER_MAX_AVG_R", -0.50) or -0.50)
            if resolved < min_resolved:
                return False, f"insufficient_resolved={resolved}/{min_resolved}"
            catastrophic = (wins == 0 and sl >= min_sl)
            weak = (win_rate <= max_wr and avg_r <= max_avg_r and sl >= min_sl)
            if catastrophic or weak:
                self._us_open_circuit_triggered = True
                self._us_open_circuit_reason = (
                    f"cb resolved={resolved} wins={wins} sl={sl} wr={win_rate:.1f}% avgR={avg_r:.3f}"
                )
                return True, self._us_open_circuit_reason
            return False, f"ok resolved={resolved} wr={win_rate:.1f}% avgR={avg_r:.3f}"
        except Exception as e:
            logger.debug("[Scheduler] US open circuit-breaker check skipped: %s", e)
            return False, "error"

    def _get_us_open_quality_guard_stats(self, force_refresh: bool = False) -> dict:
        """Cached US-open session stats derived from dashboard payload for runtime guard decisions."""
        try:
            now_ts = time.time()
            ttl_sec = 30.0
            if (not force_refresh) and self._us_open_quality_guard_cache and (now_ts - self._us_open_quality_guard_cache_ts) <= ttl_sec:
                return dict(self._us_open_quality_guard_cache)

            dash = neural_brain.us_open_trader_dashboard(risk_pct=1.0, start_balance=1000.0)

            def _rows_to_stats(rows: list) -> dict:
                out = {}
                for row in list(rows or []):
                    try:
                        key = str((row or {}).get("setup") or "").upper().strip()
                        if not key:
                            continue
                        out[key] = {
                            "sent": int((row or {}).get("sent", 0) or 0),
                            "resolved": int((row or {}).get("resolved", 0) or 0),
                            "wins": int((row or {}).get("wins", 0) or 0),
                            "losses": int((row or {}).get("losses", 0) or 0),
                            "win_rate": float((row or {}).get("win_rate", 0.0) or 0.0),
                            "net_r": float((row or {}).get("net_r", 0.0) or 0.0),
                        }
                    except Exception:
                        continue
                return out

            if not isinstance(dash, dict) or not bool(dash.get("ok")) or str(dash.get("status")) not in {"ok", "no_data"}:
                snap = {
                    "ok": False,
                    "status": str((dash or {}).get("status") or "error"),
                    "setup_stats": {},
                    "setup_stats_by_segment": {"core": {}, "late": {}},
                    "symbol_stats": {},
                    "segments": {},
                }
            else:
                setup_rows = list(dash.get("setup_stats_all") or dash.get("win_rate_by_setup") or [])
                setup_stats = _rows_to_stats(setup_rows)
                seg_rows = dict(dash.get("setup_stats_by_segment") or {})
                setup_stats_by_segment = {
                    "core": _rows_to_stats(seg_rows.get("core") or []),
                    "late": _rows_to_stats(seg_rows.get("late") or []),
                }

                symbol_rows = list(dash.get("symbol_stats_all") or [])
                symbol_stats = {}
                for row in symbol_rows:
                    try:
                        sym = str((row or {}).get("symbol") or "").upper().strip()
                        if not sym:
                            continue
                        symbol_stats[sym] = {
                            "sent": int((row or {}).get("sent", 0) or 0),
                            "resolved": int((row or {}).get("resolved", 0) or 0),
                            "wins": int((row or {}).get("wins", 0) or 0),
                            "losses": int((row or {}).get("losses", 0) or 0),
                            "win_rate": float((row or {}).get("win_rate", 0.0) or 0.0),
                            "net_r": float((row or {}).get("net_r", 0.0) or 0.0),
                            "pending_mark_r": float((row or {}).get("pending_mark_r", 0.0) or 0.0),
                            "session_r": float((row or {}).get("session_r", 0.0) or 0.0),
                        }
                    except Exception:
                        continue

                segs = dict(dash.get("segments") or {})
                snap = {
                    "ok": True,
                    "status": str(dash.get("status") or "ok"),
                    "ny_date": str(dash.get("ny_date") or ""),
                    "summary": dict(dash.get("summary") or {}),
                    "segments": {
                        "core": dict(segs.get("core") or {}),
                        "late": dict(segs.get("late") or {}),
                        "verdict": str(segs.get("verdict") or ""),
                    },
                    "setup_stats": setup_stats,
                    "setup_stats_by_segment": setup_stats_by_segment,
                    "symbol_stats": symbol_stats,
                }
            self._us_open_quality_guard_cache = dict(snap)
            self._us_open_quality_guard_cache_ts = now_ts
            return snap
        except Exception as e:
            return {
                "ok": False,
                "status": "error",
                "error": str(e),
                "setup_stats": {},
                "setup_stats_by_segment": {"core": {}, "late": {}},
                "symbol_stats": {},
                "segments": {},
            }

    def _base_setup_name_for_us_open(self, opp) -> str:
        try:
            setup = str(getattr(opp, "base_setup_type", "") or getattr(opp, "setup_type", "") or "").upper().strip()
            if setup.startswith("BULLISH_"):
                setup = setup[len("BULLISH_"):]
            elif setup.startswith("BEARISH_"):
                setup = setup[len("BEARISH_"):]
            return setup or "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    def _current_us_open_segment_for_guard(self) -> str:
        try:
            ny_now = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
            elapsed = self._us_open_elapsed_after_open_min(ny_now)
            hard_stop_min = max(30, int(getattr(config, "US_OPEN_SMART_POST_OPEN_MAX_MIN", 90) or 90))
            return "core" if elapsed <= hard_stop_min else "late"
        except Exception:
            return "core"

    def _maybe_recover_us_open_symbol(self, opp, sig, sym: str, setup: str, raw_scores: dict, blocked_reason: str) -> tuple[bool, str]:
        if not bool(getattr(config, "US_OPEN_SYMBOL_RECOVERY_ENABLED", True)):
            return False, blocked_reason
        if not str(blocked_reason or "").startswith("us_open_symbol_loss_cap"):
            return False, blocked_reason
        try:
            now_ts = time.time()
            state = dict(self._us_open_symbol_recovery_state.get(sym) or {})
            max_recovers = max(1, int(getattr(config, "US_OPEN_SYMBOL_RECOVERY_MAX_PER_SYMBOL", 1) or 1))
            cooldown_sec = max(0, int(getattr(config, "US_OPEN_SYMBOL_RECOVERY_COOLDOWN_MIN", 25) or 25)) * 60
            used = int(state.get("count", 0) or 0)
            last_ts = float(state.get("ts", 0.0) or 0.0)
            if used >= max_recovers:
                return False, blocked_reason
            if cooldown_sec > 0 and (now_ts - last_ts) < cooldown_sec:
                return False, blocked_reason

            conf = float(getattr(sig, "confidence", 0.0) or 0.0)
            vol_ratio = float(getattr(opp, "vol_vs_avg", 0.0) or 0.0)
            setup_wr = float(getattr(opp, "setup_win_rate", 0.0) or 0.0)
            rank_score = float(getattr(opp, "us_open_rank_score", 0.0) or 0.0)
            min_conf = float(getattr(config, "US_OPEN_SYMBOL_RECOVERY_MIN_CONFIDENCE", 72) or 72)
            min_vol = float(getattr(config, "US_OPEN_SYMBOL_RECOVERY_MIN_VOL_RATIO", 1.15) or 1.15)
            min_setup_wr = float(getattr(config, "US_OPEN_SYMBOL_RECOVERY_MIN_SETUP_WR", 0.58) or 0.58)
            min_rank = float(getattr(config, "US_OPEN_SYMBOL_RECOVERY_MIN_RANK_SCORE", 55) or 55)
            if conf < min_conf or vol_ratio < min_vol or setup_wr < min_setup_wr or rank_score < min_rank:
                return False, blocked_reason

            self._us_open_symbol_recovery_state[sym] = {
                "count": used + 1,
                "ts": now_ts,
                "reason": "quality_recovery",
                "setup": setup,
                "conf": round(conf, 2),
                "vol": round(vol_ratio, 3),
                "setup_wr": round(setup_wr, 3),
                "rank": round(rank_score, 2),
            }
            raw_scores["us_open_symbol_recovery"] = True
            raw_scores["us_open_symbol_recovery_prev_block"] = str(blocked_reason)
            if f"🟢 US-open symbol recovery: {sym}" not in getattr(sig, "reasons", []):
                sig.reasons.append(
                    f"🟢 US-open symbol recovery: {sym} quality override (conf {conf:.1f}%, vol {vol_ratio:.2f}x, WR {setup_wr*100:.1f}%)"
                )
            return True, ""
        except Exception:
            return False, blocked_reason

    def _log_us_open_quality_guard_diag(self, diag: dict, stage: str) -> None:
        try:
            diag_copy = dict(diag or {})
            if diag_copy:
                self._us_open_quality_guard_last_diag[str(stage or "monitor")] = diag_copy
                self._us_open_quality_guard_last_diag_ts = time.time()
            if not diag_copy:
                return
            if not any(int(diag_copy.get(k, 0) or 0) for k in ("symbol_loss_cap_blocked", "symbol_recovered", "setup_hard_blocked", "setup_post_conf_cutoff", "setup_penalized", "setup_boosted")):
                return
            logger.info(
                "[Scheduler] US OPEN quality guard (%s): symbols_blocked=%s symbol_recovered=%s setup_blocked=%s post_conf_cutoff=%s setup_penalized=%s avg_penalty=%.2f setup_boosted=%s avg_boost=%.2f source=%s seg=%s",
                stage,
                int(diag_copy.get("symbol_loss_cap_blocked", 0) or 0),
                int(diag_copy.get("symbol_recovered", 0) or 0),
                int(diag_copy.get("setup_hard_blocked", 0) or 0),
                int(diag_copy.get("setup_post_conf_cutoff", 0) or 0),
                int(diag_copy.get("setup_penalized", 0) or 0),
                float(diag_copy.get("avg_penalty", 0.0) or 0.0),
                int(diag_copy.get("setup_boosted", 0) or 0),
                float(diag_copy.get("avg_boost", 0.0) or 0.0),
                str(diag_copy.get("stats_status") or "-"),
                str(diag_copy.get("segment") or "-"),
            )
        except Exception:
            pass

    def _apply_us_open_quality_filters(self, opps: list, stage: str = "monitor") -> tuple[list, dict]:
        diag = {
            "input": len(opps or []),
            "output": len(opps or []),
            "stats_status": "disabled",
            "segment": "core",
            "symbol_loss_cap_blocked": 0,
            "symbol_recovered": 0,
            "setup_hard_blocked": 0,
            "setup_post_conf_cutoff": 0,
            "setup_penalized": 0,
            "setup_boosted": 0,
            "penalty_total": 0.0,
            "boost_total": 0.0,
        }
        if not opps:
            return [], diag

        stats = self._get_us_open_quality_guard_stats()
        diag["stats_status"] = str(stats.get("status") or ("ok" if stats.get("ok") else "error"))
        if not bool(stats.get("ok")):
            return list(opps), diag

        setup_stats = dict(stats.get("setup_stats") or {})
        setup_stats_by_segment = dict(stats.get("setup_stats_by_segment") or {})
        symbol_stats = dict(stats.get("symbol_stats") or {})
        segment = self._current_us_open_segment_for_guard()
        diag["segment"] = segment
        seg_setup_stats = dict((setup_stats_by_segment.get(segment) or {}))

        use_setup = bool(getattr(config, "US_OPEN_SETUP_WEIGHTING_ENABLED", True))
        use_symbol_cap = bool(getattr(config, "US_OPEN_SYMBOL_SESSION_LOSS_CAP_ENABLED", True))
        min_conf_cut = float(getattr(config, "STOCK_MIN_CONFIDENCE", 70) or 70)

        setup_min_resolved = max(1, int(getattr(config, "US_OPEN_SETUP_STATS_MIN_RESOLVED", 6) or 6))
        poor_wr = float(getattr(config, f"US_OPEN_SETUP_POOR_WR_{segment.upper()}", getattr(config, "US_OPEN_SETUP_POOR_WR", 35)) or getattr(config, "US_OPEN_SETUP_POOR_WR", 35))
        poor_net_r = float(getattr(config, f"US_OPEN_SETUP_POOR_NET_R_{segment.upper()}", getattr(config, "US_OPEN_SETUP_POOR_NET_R", -1.0)) or getattr(config, "US_OPEN_SETUP_POOR_NET_R", -1.0))

        hard_block_enabled = bool(getattr(config, "US_OPEN_SETUP_HARD_BLOCK_ENABLED", True))
        hard_block_min_resolved = max(setup_min_resolved, int(getattr(config, "US_OPEN_SETUP_HARD_BLOCK_MIN_RESOLVED", 10) or 10))
        hard_block_max_wr = float(getattr(config, f"US_OPEN_SETUP_HARD_BLOCK_MAX_WR_{segment.upper()}", getattr(config, "US_OPEN_SETUP_HARD_BLOCK_MAX_WR", 8)) or getattr(config, "US_OPEN_SETUP_HARD_BLOCK_MAX_WR", 8))
        hard_block_max_net_r = float(getattr(config, f"US_OPEN_SETUP_HARD_BLOCK_MAX_NET_R_{segment.upper()}", getattr(config, "US_OPEN_SETUP_HARD_BLOCK_MAX_NET_R", -2.5)) or getattr(config, "US_OPEN_SETUP_HARD_BLOCK_MAX_NET_R", -2.5))

        boost_enabled = bool(getattr(config, "US_OPEN_SETUP_BOOST_ENABLED", True))
        boost_min_resolved = max(1, int(getattr(config, "US_OPEN_SETUP_BOOST_MIN_RESOLVED", 8) or 8))
        boost_min_wr = float(getattr(config, f"US_OPEN_SETUP_BOOST_MIN_WR_{segment.upper()}", getattr(config, "US_OPEN_SETUP_BOOST_MIN_WR", 58)) or getattr(config, "US_OPEN_SETUP_BOOST_MIN_WR", 58))
        boost_min_net_r = float(getattr(config, f"US_OPEN_SETUP_BOOST_MIN_NET_R_{segment.upper()}", getattr(config, "US_OPEN_SETUP_BOOST_MIN_NET_R", 0.8)) or getattr(config, "US_OPEN_SETUP_BOOST_MIN_NET_R", 0.8))

        max_penalty_map = {
            "CHOCH": float(getattr(config, "US_OPEN_SETUP_MAX_PENALTY_CHOCH", 8.0) or 8.0),
            "BB_SQUEEZE": float(getattr(config, "US_OPEN_SETUP_MAX_PENALTY_BB_SQUEEZE", 6.0) or 6.0),
            "OB_BOUNCE": float(getattr(config, "US_OPEN_SETUP_MAX_PENALTY_OB_BOUNCE", 3.0) or 3.0),
        }
        max_boost_ob = float(getattr(config, "US_OPEN_SETUP_MAX_BOOST_OB_BOUNCE", 2.0) or 2.0)

        sym_cap_min_resolved = max(1, int(getattr(config, "US_OPEN_SYMBOL_SESSION_LOSS_CAP_MIN_RESOLVED", 4) or 4))
        sym_cap_max_neg_r = float(getattr(config, "US_OPEN_SYMBOL_SESSION_LOSS_CAP_MAX_NEG_R", -2.0) or -2.0)
        sym_cap_max_losses = max(1, int(getattr(config, "US_OPEN_SYMBOL_SESSION_LOSS_CAP_MAX_LOSSES", 4) or 4))

        filtered: list = []
        for opp in list(opps):
            sig = getattr(opp, "signal", None)
            if sig is None:
                continue
            raw_scores = dict(getattr(sig, "raw_scores", {}) or {})
            sym = str(getattr(sig, "symbol", "") or "").upper().strip()
            setup = self._base_setup_name_for_us_open(opp)

            blocked_reason = ""
            # Per-symbol session loss cap (blocks repeated weak names in same session)
            if use_symbol_cap and sym:
                srec = dict(symbol_stats.get(sym) or {})
                if srec:
                    s_res = int(srec.get("resolved", 0) or 0)
                    s_losses = int(srec.get("losses", 0) or 0)
                    s_net_r = float(srec.get("net_r", 0.0) or 0.0)
                    if s_res >= sym_cap_min_resolved and (s_losses >= sym_cap_max_losses or s_net_r <= sym_cap_max_neg_r):
                        blocked_reason = (
                            f"us_open_symbol_loss_cap {sym} res={s_res} losses={s_losses} netR={s_net_r:.3f}"
                        )
                        diag["symbol_loss_cap_blocked"] += 1
                        if blocked_reason not in sig.warnings:
                            sig.warnings.append(f"🛑 US-open symbol cap: {sym} underperforming this session")
                        recovered, blocked_reason = self._maybe_recover_us_open_symbol(opp, sig, sym, setup, raw_scores, blocked_reason)
                        if recovered:
                            diag["symbol_recovered"] += 1
                            diag["symbol_loss_cap_blocked"] = max(0, int(diag.get("symbol_loss_cap_blocked", 0) or 0) - 1)

            penalty = 0.0
            boost = 0.0
            if (not blocked_reason) and use_setup and setup in max_penalty_map:
                rec = dict(seg_setup_stats.get(setup) or setup_stats.get(setup) or {})
                if rec and int(rec.get("resolved", 0) or 0) >= setup_min_resolved:
                    resolved_n = int(rec.get("resolved", 0) or 0)
                    wr = float(rec.get("win_rate", 0.0) or 0.0)
                    net_r = float(rec.get("net_r", 0.0) or 0.0)
                    if hard_block_enabled and resolved_n >= hard_block_min_resolved and wr <= hard_block_max_wr and net_r <= hard_block_max_net_r:
                        blocked_reason = f"us_open_setup_block {setup} wr={wr:.1f}% netR={net_r:.3f} res={resolved_n}"
                        diag["setup_hard_blocked"] += 1
                        if f"🛑 US-open setup blocked: {setup}" not in sig.warnings:
                            sig.warnings.append(f"🛑 US-open setup blocked: {setup} weak today ({wr:.1f}% / {net_r:.2f}R)")
                    else:
                        if wr <= poor_wr or net_r <= poor_net_r:
                            wr_sev = max(0.0, (poor_wr - wr) / max(1.0, abs(poor_wr))) if wr <= poor_wr else 0.0
                            nr_sev = max(0.0, (poor_net_r - net_r) / max(0.25, abs(poor_net_r))) if net_r <= poor_net_r else 0.0
                            severity = max(wr_sev, nr_sev)
                            max_pen = max_penalty_map.get(setup, 0.0)
                            penalty = min(max_pen, max_pen * max(0.2, min(1.0, severity))) if max_pen > 0 else 0.0
                        elif boost_enabled and setup == "OB_BOUNCE" and resolved_n >= boost_min_resolved and wr >= boost_min_wr and net_r >= boost_min_net_r:
                            wr_gain = max(0.0, (wr - boost_min_wr) / max(5.0, 100.0 - boost_min_wr))
                            nr_gain = max(0.0, (net_r - boost_min_net_r) / max(0.5, abs(boost_min_net_r)))
                            gain = min(1.0, max(wr_gain, nr_gain))
                            boost = min(max_boost_ob, max_boost_ob * max(0.2, gain)) if max_boost_ob > 0 else 0.0

            if (not blocked_reason) and penalty > 0.0:
                before = float(getattr(sig, "confidence", 0.0) or 0.0)
                sig.confidence = round(max(0.0, before - penalty), 1)
                diag["setup_penalized"] += 1
                diag["penalty_total"] += float(penalty)
                raw_scores["us_open_setup_penalty"] = round(float(penalty), 3)
                raw_scores["us_open_setup_penalty_setup"] = setup
                raw_scores["us_open_conf_before_setup_penalty"] = round(before, 3)
                if f"⚠️ US-open setup penalty: {setup}" not in sig.warnings:
                    sig.warnings.append(f"⚠️ US-open setup penalty: {setup} underperforming today ({before:.1f}%→{sig.confidence:.1f}%)")

            if (not blocked_reason) and boost > 0.0:
                before = float(getattr(sig, "confidence", 0.0) or 0.0)
                sig.confidence = round(min(99.9, before + boost), 1)
                diag["setup_boosted"] += 1
                diag["boost_total"] += float(boost)
                raw_scores["us_open_setup_boost"] = round(float(boost), 3)
                raw_scores["us_open_setup_boost_setup"] = setup
                raw_scores["us_open_conf_before_setup_boost"] = round(before, 3)
                if f"🧠 US-open setup boost: {setup}" not in sig.reasons:
                    sig.reasons.append(f"🧠 US-open setup boost: {setup} strong today ({before:.1f}%→{sig.confidence:.1f}%)")

            if (not blocked_reason) and float(getattr(sig, "confidence", 0.0) or 0.0) < min_conf_cut:
                blocked_reason = f"us_open_post_setup_conf_cutoff conf={float(getattr(sig, 'confidence', 0.0) or 0.0):.1f}<{min_conf_cut:.1f}"
                diag["setup_post_conf_cutoff"] += 1

            if blocked_reason:
                raw_scores["us_open_quality_guard_segment"] = segment
                raw_scores["us_open_quality_guard_blocked"] = True
                raw_scores["us_open_quality_guard_reason"] = blocked_reason
                setattr(sig, "raw_scores", raw_scores)
                continue

            raw_scores["us_open_quality_guard_segment"] = segment
            raw_scores["us_open_quality_guard_blocked"] = False
            setattr(sig, "raw_scores", raw_scores)
            filtered.append(opp)

        if filtered and (diag["setup_penalized"] or diag["setup_boosted"]):
            try:
                filtered.sort(
                    key=lambda o: (o.us_open_rank_score, o.dollar_volume, o.setup_win_rate, o.signal.confidence),
                    reverse=True,
                )
            except Exception:
                pass

        diag["output"] = len(filtered)
        if diag["setup_penalized"]:
            diag["avg_penalty"] = round(diag["penalty_total"] / max(1, diag["setup_penalized"]), 2)
        else:
            diag["avg_penalty"] = 0.0
        if diag["setup_boosted"]:
            diag["avg_boost"] = round(diag["boost_total"] / max(1, diag["setup_boosted"]), 2)
        else:
            diag["avg_boost"] = 0.0
        return filtered, diag

    def get_us_open_guard_status(self) -> dict:
        """Live status snapshot for US-open guardrails (macro freeze / circuit breaker / mood-stop)."""
        try:
            ny_tz = ZoneInfo("America/New_York")
            now_utc = datetime.now(timezone.utc)
            ny_now = now_utc.astimezone(ny_tz)
            self._reset_us_open_mood_state_if_new_day(ny_now)

            open_dt = ny_now.replace(hour=9, minute=30, second=0, microsecond=0)
            lead_min = max(0, int(getattr(config, "US_OPEN_SMART_PREMARKET_LEAD_MIN", 60) or 60))
            post_open_max = max(30, int(getattr(config, "US_OPEN_SMART_POST_OPEN_MAX_MIN", 120) or 120))
            window_start = open_dt - timedelta(minutes=lead_min)
            window_end = open_dt + timedelta(minutes=post_open_max, seconds=59)
            in_window = self._in_us_open_window(ny_now)
            premarket = ny_now < open_dt
            elapsed_after_open_min = self._us_open_elapsed_after_open_min(ny_now)

            # Macro freeze details (with ETA until oldest relevant headline ages out).
            macro_enabled = bool(getattr(config, "US_OPEN_MACRO_FREEZE_ENABLED", True))
            macro_min_score = max(1, int(getattr(config, "US_OPEN_MACRO_FREEZE_MIN_SCORE", 8) or 8))
            macro_max_age_min = max(5, int(getattr(config, "US_OPEN_MACRO_FREEZE_MAX_AGE_MIN", 45) or 45))
            macro_priority_only = bool(getattr(config, "US_OPEN_MACRO_FREEZE_PRIORITY_ONLY", True))
            macro_active, macro_reason = self._check_us_open_macro_freeze()
            macro_release_eta_min = None
            macro_headline = ""
            try:
                heads = macro_news.high_impact_headlines(hours=4, min_score=macro_min_score, limit=8)
                fresh = []
                for h in heads:
                    age_min = max(0.0, (now_utc - h.published_utc).total_seconds() / 60.0)
                    if age_min > macro_max_age_min:
                        continue
                    if macro_priority_only and (not macro_news.is_priority_theme(h)):
                        continue
                    fresh.append((h, age_min))
                if fresh:
                    # Freeze releases when all relevant headlines age out; nearest release is oldest threshold among current fresh set.
                    rems = [max(0.0, macro_max_age_min - age) for _, age in fresh]
                    macro_release_eta_min = round(min(rems), 1)
                    h, age = sorted(fresh, key=lambda x: (x[1], -int(getattr(x[0], 'score', 0))))[0]
                    macro_headline = str(getattr(h, 'title', '') or '')[:180]
            except Exception:
                pass

            # Circuit breaker status + next release (next NY business day open, because reset is day-based).
            cb_active, cb_reason = self._check_us_open_quality_circuit_breaker(ny_now)
            cb_check_start_min = max(10, int(getattr(config, "US_OPEN_CIRCUIT_BREAKER_CHECK_START_MIN", 30) or 30))
            cb_release_eta_min = None
            cb_release_at_ny = ""
            if cb_active and str(cb_reason or '').startswith('cb '):
                nxt = open_dt + timedelta(days=1)
                while nxt.weekday() >= 5:
                    nxt += timedelta(days=1)
                nxt = nxt.replace(hour=9, minute=30, second=0, microsecond=0)
                cb_release_at_ny = nxt.strftime('%Y-%m-%d %H:%M NY')
                cb_release_eta_min = round(max(0.0, (nxt - ny_now).total_seconds() / 60.0), 1)
            elif (not cb_active) and str(cb_reason or '').lower() == 'warmup':
                cb_release_eta_min = round(max(0.0, cb_check_start_min - elapsed_after_open_min), 1)
                cb_release_at_ny = f'after +{cb_check_start_min}m from open'

            # Mood-stop status (already day-scoped, reset next session day).
            mood_active = bool(self._us_open_mood_stop_triggered)
            mood_reason = str(self._us_open_mood_stop_reason or '')
            mood_release_at_ny = ""
            if mood_active:
                nxt = open_dt + timedelta(days=1)
                while nxt.weekday() >= 5:
                    nxt += timedelta(days=1)
                nxt = nxt.replace(hour=9, minute=30, second=0, microsecond=0)
                mood_release_at_ny = nxt.strftime('%Y-%m-%d %H:%M NY')

            qg_stats = self._get_us_open_quality_guard_stats(force_refresh=False)
            qg_last_plan = dict((self._us_open_quality_guard_last_diag or {}).get('plan') or {})
            qg_last_monitor = dict((self._us_open_quality_guard_last_diag or {}).get('monitor') or {})
            qg_segment = self._current_us_open_segment_for_guard()
            qg_symbol_stats = dict((qg_stats or {}).get('symbol_stats') or {})
            cap_min_res = max(1, int(getattr(config, 'US_OPEN_SYMBOL_SESSION_LOSS_CAP_MIN_RESOLVED', 4) or 4))
            cap_max_neg_r = float(getattr(config, 'US_OPEN_SYMBOL_SESSION_LOSS_CAP_MAX_NEG_R', -2.0) or -2.0)
            cap_max_losses = max(1, int(getattr(config, 'US_OPEN_SYMBOL_SESSION_LOSS_CAP_MAX_LOSSES', 4) or 4))
            capped_symbols = []
            for sym, rec in qg_symbol_stats.items():
                try:
                    s_res = int((rec or {}).get('resolved', 0) or 0)
                    s_losses = int((rec or {}).get('losses', 0) or 0)
                    s_net_r = float((rec or {}).get('net_r', 0.0) or 0.0)
                    if s_res >= cap_min_res and (s_losses >= cap_max_losses or s_net_r <= cap_max_neg_r):
                        capped_symbols.append({'symbol': sym, 'resolved': s_res, 'losses': s_losses, 'net_r': round(s_net_r, 3)})
                except Exception:
                    continue
            capped_symbols = sorted(capped_symbols, key=lambda x: (x['net_r'], -x['losses']))[:10]
            qg_summary = {
                'stats_ok': bool((qg_stats or {}).get('ok')),
                'stats_status': str((qg_stats or {}).get('status') or '-'),
                'segment': qg_segment,
                'segments_verdict': str(((qg_stats or {}).get('segments') or {}).get('verdict') or ''),
                'cache_age_sec': round(max(0.0, time.time() - float(self._us_open_quality_guard_cache_ts or 0.0)), 1) if self._us_open_quality_guard_cache_ts else None,
                'last_diag_age_sec': round(max(0.0, time.time() - float(self._us_open_quality_guard_last_diag_ts or 0.0)), 1) if self._us_open_quality_guard_last_diag_ts else None,
                'last_plan': qg_last_plan,
                'last_monitor': qg_last_monitor,
                'capped_symbols': capped_symbols,
                'recovery_state': dict(self._us_open_symbol_recovery_state or {}),
            }

            return {
                'ok': True,
                'now_utc': now_utc.strftime('%Y-%m-%d %H:%M UTC'),
                'now_ny': ny_now.strftime('%Y-%m-%d %H:%M NY'),
                'weekday_ny': ny_now.weekday(),
                'in_us_open_window': bool(in_window),
                'premarket': bool(premarket),
                'elapsed_after_open_min': round(float(elapsed_after_open_min), 1),
                'window_start_ny': window_start.strftime('%Y-%m-%d %H:%M NY'),
                'window_end_ny': window_end.strftime('%Y-%m-%d %H:%M NY'),
                'macro_freeze': {
                    'enabled': macro_enabled,
                    'active': bool(macro_active),
                    'reason': str(macro_reason or ''),
                    'min_score': macro_min_score,
                    'max_age_min': macro_max_age_min,
                    'priority_only': macro_priority_only,
                    'release_eta_min': macro_release_eta_min,
                    'headline': macro_headline,
                },
                'circuit_breaker': {
                    'enabled': bool(getattr(config, 'US_OPEN_CIRCUIT_BREAKER_ENABLED', True)),
                    'active': bool(cb_active),
                    'reason': str(cb_reason or ''),
                    'check_start_min': cb_check_start_min,
                    'release_eta_min': cb_release_eta_min,
                    'release_at_ny': cb_release_at_ny,
                },
                'mood_stop': {
                    'enabled': bool(getattr(config, 'US_OPEN_MOOD_STOP_ENABLED', True)),
                    'active': mood_active,
                    'weak_cycles': int(self._us_open_mood_weak_cycles or 0),
                    'weak_cycles_to_stop': max(2, int(getattr(config, 'US_OPEN_MOOD_WEAK_CYCLES_TO_STOP', 3) or 3)),
                    'reason': mood_reason,
                    'release_at_ny': mood_release_at_ny,
                },
                'symbol_cooldown': {
                    'enabled': int(getattr(config, 'US_OPEN_SYMBOL_ALERT_COOLDOWN_MIN', 20) or 20) > 0,
                    'cooldown_min': int(getattr(config, 'US_OPEN_SYMBOL_ALERT_COOLDOWN_MIN', 20) or 20),
                    'tracked_symbols': len(self._us_open_symbol_alert_ts or {}),
                    'record_new_only': bool(getattr(config, 'US_OPEN_RECORD_NEW_SYMBOLS_ONLY', True)),
                },
                'quality_guard': qg_summary,
            }
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _maybe_us_open_session_checkin(self, ny_now: datetime, force: bool = False) -> None:
        if not bool(getattr(config, "US_OPEN_SESSION_CHECKIN_ENABLED", True)):
            return
        day_key = ny_now.strftime("%Y-%m-%d")
        if (not force) and self._us_open_session_checkin_day == day_key:
            return
        notifier.send_us_open_session_checkin(
            interval_min=max(3, int(config.US_OPEN_SMART_INTERVAL_MIN)),
            premarket_lead_min=max(0, int(getattr(config, "US_OPEN_SMART_PREMARKET_LEAD_MIN", 60))),
            no_opp_ping_min=max(3, int(getattr(config, "US_OPEN_SMART_NO_OPP_PING_MIN", 15))),
        )
        self._us_open_session_checkin_day = day_key

    def _maybe_send_us_open_quality_recap(self, force: bool = False) -> None:
        if not bool(getattr(config, "SIGNAL_FEEDBACK_ENABLED", True)):
            return
        try:
            interval_min = max(5, int(getattr(config, "US_OPEN_QUALITY_REPORT_INTERVAL_MIN", 15) or 15))
            now_ts = time.time()
            if (not force) and (now_ts - self._us_open_quality_last_sent_ts) < (interval_min * 60):
                return
            report = neural_brain.signal_feedback_report(days=1, source_contains="us_open")
            key = "|".join(
                str(report.get(k, 0))
                for k in ("sent", "resolved", "pending", "tp1", "tp2", "tp3", "sl")
            )
            if (not force) and self._us_open_quality_last_key == key and (now_ts - self._us_open_quality_last_sent_ts) < (interval_min * 60 * 2):
                return
            notifier.send_us_open_signal_quality_recap(report)
            self._us_open_quality_last_key = key
            self._us_open_quality_last_sent_ts = now_ts
        except Exception as e:
            logger.debug("[Scheduler] US open quality recap skipped: %s", e)

    def _run_us_open_smart_monitor(self, force: bool = False):
        """Close monitoring during US open: send focused updates when leadership changes."""
        if not config.US_OPEN_SMART_MONITOR:
            return
        try:
            ny_now = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
            if ny_now.weekday() >= 5:
                return
            if (not force) and (not self._in_us_open_window(ny_now)):
                return
            self._reset_us_open_mood_state_if_new_day(ny_now)

            self._maybe_us_open_session_checkin(ny_now, force=force)
            logger.info("[Scheduler] Running US OPEN smart monitor...")
            premarket_mode = ny_now.time() < dt_time(9, 30)
            opps = stock_scanner.scan_us_open_daytrade(top_n=10, allow_premarket=premarket_mode)
            now_ts = time.time()
            realtime_mode = bool(getattr(config, "US_OPEN_SMART_ALWAYS_REPORT", False))
            no_opp_ping_min = max(3, int(getattr(config, "US_OPEN_SMART_NO_OPP_PING_MIN", 15) or 15))

            macro_freeze, macro_reason = self._check_us_open_macro_freeze()
            if macro_freeze and (not force):
                logger.info("[Scheduler] US OPEN smart monitor: macro-freeze engaged (%s)", macro_reason)
                if force or realtime_mode:
                    if force or (now_ts - self._us_open_last_noopp_sent_ts) >= (no_opp_ping_min * 60):
                        notifier.send_us_open_monitor_update([], periodic_ping=True)
                        self._us_open_last_noopp_sent_ts = now_ts
                self._maybe_send_us_open_quality_recap(force=force)
                return

            cb_stop, cb_reason = self._check_us_open_quality_circuit_breaker(ny_now)
            if cb_stop and (not force):
                logger.info("[Scheduler] US OPEN smart monitor: circuit-breaker engaged (%s)", cb_reason)
                if force or realtime_mode:
                    if force or (now_ts - self._us_open_last_noopp_sent_ts) >= (no_opp_ping_min * 60):
                        notifier.send_us_open_monitor_update([], periodic_ping=True)
                        self._us_open_last_noopp_sent_ts = now_ts
                self._maybe_send_us_open_quality_recap(force=force)
                return

            if not opps:
                mood_stop, mood_reason = self._update_us_open_mood_stop(ny_now, opps)
                diag = stock_scanner.get_last_us_open_diagnostics()
                if diag:
                    strict = diag.get("strict_filter", {}) or {}
                    upstream = diag.get("upstream", {}) or {}
                    up = (upstream.get("reject_reasons", {}) or {})
                    pf = (upstream.get("prefilter", {}) or {})
                    logger.info(
                        "[Scheduler] US OPEN smart monitor diagnostics (%s): "
                        "prefilter kept=%s/%s unmapped=%s | "
                        "upstream market_closed=%s no_data=%s no_signal=%s | "
                        "strict raw=%s pass=%s fail_conf=%s fail_vol=%s fail_dv=%s",
                        diag.get("mode", "-"),
                        pf.get("kept", upstream.get("symbols", 0)),
                        pf.get("input", upstream.get("symbols_input", upstream.get("symbols", 0))),
                        pf.get("unmapped", 0),
                        up.get("market_closed", 0),
                        up.get("no_entry_data", 0),
                        up.get("no_signal", 0),
                        strict.get("total_opportunities", 0),
                        strict.get("passed", 0),
                        strict.get("fail_confidence", 0),
                        strict.get("fail_volume", 0),
                        strict.get("fail_dollar_volume", 0),
                    )
                if mood_stop and (not force):
                    logger.info("[Scheduler] US OPEN smart monitor: mood-stop engaged (%s)", mood_reason)
                    self._maybe_send_us_open_quality_recap(force=force)
                    return
                logger.info("[Scheduler] US OPEN smart monitor: no opportunities")
                if force or realtime_mode:
                    if force or (now_ts - self._us_open_last_noopp_sent_ts) >= (no_opp_ping_min * 60):
                        notifier.send_us_open_monitor_update(
                            [],
                            periodic_ping=True,
                        )
                        self._us_open_last_noopp_sent_ts = now_ts
                self._maybe_send_us_open_quality_recap(force=force)
                return

            for opp in opps:
                self._apply_neural_soft_adjustment(opp.signal, source="us_open_monitor")
            opps, usq_diag = self._apply_us_open_quality_filters(opps, stage="monitor")
            self._log_us_open_quality_guard_diag(usq_diag, stage="monitor")
            if not opps:
                mood_stop, mood_reason = self._update_us_open_mood_stop(ny_now, [])
                if mood_stop and (not force):
                    logger.info("[Scheduler] US OPEN smart monitor: mood-stop engaged (%s)", mood_reason)
                    self._maybe_send_us_open_quality_recap(force=force)
                    return
                logger.info("[Scheduler] US OPEN smart monitor: all opportunities filtered by setup/symbol quality guard")
                if force or realtime_mode:
                    if force or (now_ts - self._us_open_last_noopp_sent_ts) >= (no_opp_ping_min * 60):
                        notifier.send_us_open_monitor_update([], periodic_ping=True)
                        self._us_open_last_noopp_sent_ts = now_ts
                self._maybe_send_us_open_quality_recap(force=force)
                return

            mood_stop, mood_reason = self._update_us_open_mood_stop(ny_now, opps)
            if mood_stop and (not force):
                logger.info("[Scheduler] US OPEN smart monitor: mood-stop engaged (%s)", mood_reason)
                self._maybe_send_us_open_quality_recap(force=force)
                return

            symbols = [o.signal.symbol for o in opps[:10]]
            previous = self._us_open_last_symbols
            top_changed = bool(previous and symbols and symbols[0] != previous[0])
            new_symbols = [s for s in symbols if s not in previous]
            periodic_ping_sec = (max(3, int(config.US_OPEN_SMART_INTERVAL_MIN)) * 60) if realtime_mode else (45 * 60)
            periodic_ping = (now_ts - self._us_open_last_sent_ts) >= periodic_ping_sec

            cooldown_min = max(0, int(getattr(config, "US_OPEN_SYMBOL_ALERT_COOLDOWN_MIN", 20) or 20))
            cooldown_sec = cooldown_min * 60
            fresh_opps = list(opps)
            if cooldown_sec > 0 and (not force):
                tmp = []
                for opp in opps:
                    sym = str(getattr(opp.signal, "symbol", "") or "")
                    last_ts = float(self._us_open_symbol_alert_ts.get(sym, 0.0) or 0.0)
                    if (now_ts - last_ts) >= cooldown_sec:
                        tmp.append(opp)
                fresh_opps = tmp
            fresh_symbols = [str(getattr(o.signal, "symbol", "") or "") for o in fresh_opps]
            sent_any = False
            sent_opps = []
            if not previous:
                if fresh_opps:
                    sent_any = notifier.send_us_open_daytrade_summary(fresh_opps)
                    sent_opps = list(fresh_opps)
                elif realtime_mode:
                    sent_any = notifier.send_us_open_monitor_update([], periodic_ping=True)
                self._us_open_last_sent_ts = now_ts
            elif realtime_mode or top_changed or len(new_symbols) >= 2 or periodic_ping:
                if fresh_opps:
                    filtered_new_symbols = [s for s in new_symbols if s in set(fresh_symbols)]
                    sent_any = notifier.send_us_open_monitor_update(
                        fresh_opps,
                        new_symbols=filtered_new_symbols,
                        top_changed=(top_changed and bool(fresh_opps)),
                        periodic_ping=periodic_ping,
                    )
                    sent_opps = list(fresh_opps)
                elif realtime_mode or periodic_ping:
                    sent_any = notifier.send_us_open_monitor_update([], periodic_ping=True)
                self._us_open_last_sent_ts = now_ts
            if sent_any:
                for opp in sent_opps:
                    sym = str(getattr(opp.signal, "symbol", "") or "")
                    if sym:
                        self._us_open_symbol_alert_ts[sym] = now_ts
            if sent_any and config.SIGNAL_FEEDBACK_ENABLED:
                record_new_only = bool(getattr(config, "US_OPEN_RECORD_NEW_SYMBOLS_ONLY", True))
                rec_opps = sent_opps if record_new_only else opps
                for opp in rec_opps:
                    neural_brain.record_signal_sent(opp.signal, source="us_open_monitor")

            self._us_open_last_symbols = symbols
            self._maybe_send_us_open_quality_recap(force=force)
        except Exception as e:
            logger.error(f"[Scheduler] US OPEN smart monitor error: {e}", exc_info=True)

    def _run_vi_stock_scan(self):
        """VI-style US value + trend scan."""
        self._run_vi_profile_stock_scan(profile=None)

    def _run_vi_buffett_stock_scan(self):
        """Buffett-inspired VI scan (US)."""
        self._run_vi_profile_stock_scan(profile="BUFFETT")

    def _run_vi_turnaround_stock_scan(self):
        """Turnaround VI scan (US)."""
        self._run_vi_profile_stock_scan(profile="TURNAROUND")

    def _run_vi_profile_stock_scan(self, profile: str | None = None):
        try:
            prof = str(profile or "").strip().upper()
            if prof in {"BUFFETT", "TURNAROUND"}:
                logger.info("[Scheduler] Running US VI %s scan...", prof)
            else:
                logger.info("[Scheduler] Running US VALUE+TREND scan...")
            top_n = max(3, int(getattr(config, "VI_TOP_N", 10)))
            if prof == "BUFFETT":
                opps = stock_scanner.scan_us_value_trend_profile("BUFFETT", top_n=top_n)
                source_tag = "stocks_vi_buffett"
                feature_name = "scan_vi_buffett"
            elif prof == "TURNAROUND":
                opps = stock_scanner.scan_us_value_trend_profile("TURNAROUND", top_n=top_n)
                source_tag = "stocks_vi_turnaround"
                feature_name = "scan_vi_turnaround"
            else:
                opps = stock_scanner.scan_us_value_trend(top_n=top_n)
                source_tag = "stocks_vi"
                feature_name = "scan_vi"
            if not opps:
                logger.info("[Scheduler] %s: No qualifying candidates", (f"VI {prof}" if prof else "VI scan"))
                return

            for opp in opps:
                self._apply_neural_soft_adjustment(opp.signal, source=source_tag)
            sent_summary = notifier.send_vi_stock_summary(opps, feature_override=feature_name)
            if sent_summary and config.SIGNAL_FEEDBACK_ENABLED:
                for opp in opps:
                    neural_brain.record_signal_sent(opp.signal, source=source_tag)

            top = opps[0]
            if self._raw_confidence(top.signal) >= config.STOCK_MIN_CONFIDENCE + 5:
                notifier.send_stock_signal(top, feature_override=feature_name)
        except Exception as e:
            logger.error(f"[Scheduler] VI profile scan error: {e}", exc_info=True)

    def _run_economic_calendar_alerts(self, force: bool = False):
        """Alert upcoming economic events on configured lead-time windows."""
        if (not config.ECON_CALENDAR_ENABLED) and (not force):
            return
        try:
            windows = config.get_econ_alert_windows()
            tol = max(1, int(config.ECON_ALERT_TOLERANCE_MIN))
            lookahead_min = max(windows) + tol + 2
            ccy = config.get_econ_alert_currencies()
            events = economic_calendar.upcoming_events(
                within_minutes=lookahead_min,
                min_impact=config.ECON_CALENDAR_MIN_IMPACT,
                currencies=ccy,
            )
            if not events:
                return

            window_buckets: dict[int, list] = {w: [] for w in windows}
            now_ts = time.time()

            for ev in events:
                mins_left = max(0, int(getattr(ev, "minutes_to_event", 0)))
                matches = [int(w) for w in windows if abs(mins_left - int(w)) <= tol]
                if not matches:
                    continue
                chosen = sorted(matches, key=lambda w: (abs(mins_left - w), -w))[0]
                key = f"{ev.event_id}:{int(chosen)}"
                if (not force) and (key in self._econ_alert_sent):
                    continue
                window_buckets[int(chosen)].append(ev)
                self._econ_alert_sent[key] = now_ts

            # Keep cache bounded.
            cutoff = now_ts - (72 * 3600)
            self._econ_alert_sent = {k: v for k, v in self._econ_alert_sent.items() if v >= cutoff}

            sent_total = 0
            for w in sorted(window_buckets.keys(), reverse=True):
                batch = window_buckets[w]
                if not batch:
                    continue
                if notifier.send_economic_calendar_alert(batch, window_minutes=w):
                    sent_total += len(batch)
            if sent_total:
                logger.info("[Scheduler] Economic calendar alerts sent: %d events", sent_total)
        except Exception as e:
            logger.error("[Scheduler] Economic calendar alert error: %s", e, exc_info=True)

    def _run_economic_calendar_snapshot(self):
        """Manual snapshot of upcoming calendar events."""
        try:
            hours = max(6, int(getattr(config, "ECON_CALENDAR_LOOKAHEAD_HOURS", 24)))
            events = economic_calendar.next_events(
                hours=hours,
                limit=10,
                min_impact="medium",
                currencies=config.get_econ_alert_currencies(),
            )
            notifier.send_economic_calendar_snapshot(events, lookahead_hours=hours)
        except Exception as e:
            logger.error("[Scheduler] Economic calendar snapshot error: %s", e, exc_info=True)

    def _run_macro_news_watch(self, force: bool = False):
        """Watch macro/policy headlines and send deduped high-impact alerts."""
        if (not config.MACRO_NEWS_ENABLED) and (not force):
            return
        try:
            lookback_h = max(1, int(config.MACRO_NEWS_LOOKBACK_HOURS))
            min_score = max(1, int(config.MACRO_NEWS_MIN_SCORE))
            max_age_min = max(30, int(getattr(config, "MACRO_NEWS_ALERT_MAX_AGE_MIN", 240)))
            max_per_run = max(1, int(getattr(config, "MACRO_NEWS_MAX_ALERTS_PER_RUN", 2)))
            require_priority = bool(getattr(config, "MACRO_NEWS_REQUIRE_PRIORITY_THEME", True))

            heads = macro_news.high_impact_headlines(hours=lookback_h, min_score=min_score, limit=20)
            if not heads:
                return
            fresh = []
            now_ts = time.time()
            now_utc = datetime.now(timezone.utc)
            for h in heads:
                hid = str(getattr(h, "headline_id", "") or "")
                if not hid:
                    continue
                age_min = max(0.0, (now_utc - h.published_utc).total_seconds() / 60.0)
                if age_min > float(max_age_min):
                    continue
                if require_priority and (not macro_news.is_priority_theme(h)):
                    continue
                if (not force) and (hid in self._macro_alert_sent):
                    continue
                fresh.append(h)

            if fresh:
                ranked, adapt_meta = self._rank_macro_alert_candidates(fresh, now_utc=now_utc, force=force)
                if not ranked:
                    logger.info(
                        "[Scheduler] Macro adaptive priority filtered all fresh headlines (dropped=%s)",
                        adapt_meta.get("dropped", len(fresh)),
                    )
                    return
                batch = ranked[:max_per_run]
                if not batch:
                    return
                for h in batch:
                    hid = str(getattr(h, "headline_id", "") or "")
                    if hid:
                        self._macro_alert_sent[hid] = now_ts
                # Keep dedupe store bounded.
                cutoff = now_ts - (72 * 3600)
                self._macro_alert_sent = {k: v for k, v in self._macro_alert_sent.items() if v >= cutoff}
                notifier.send_macro_news_alert(batch)
                logger.info(
                    "[Scheduler] Macro news alerts sent: %d headlines (adaptive kept=%s dropped=%s)",
                    len(batch),
                    adapt_meta.get("kept", len(batch)),
                    adapt_meta.get("dropped", 0),
                )
        except Exception as e:
            logger.error("[Scheduler] Macro news watch error: %s", e, exc_info=True)

    def _rank_macro_alert_candidates(self, headlines: list, now_utc: datetime | None = None, force: bool = False):
        """
        Phase 3: adaptive macro alert prioritization using observed theme effectiveness.
        Fail-safe: when disabled or unavailable, returns normal score/time sort.
        """
        items = list(headlines or [])
        if not items:
            return [], {"kept": 0, "dropped": 0, "adaptive": False}

        base_sorted = sorted(items, key=lambda x: (getattr(x, "score", 0), getattr(x, "published_utc", datetime.now(timezone.utc))), reverse=True)
        if (not bool(getattr(config, "MACRO_ALERT_ADAPTIVE_PRIORITY_ENABLED", True))) or force:
            return base_sorted, {"kept": len(base_sorted), "dropped": 0, "adaptive": False}

        try:
            weights = macro_news.dynamic_theme_weights_snapshot()
        except Exception:
            return base_sorted, {"kept": len(base_sorted), "dropped": 0, "adaptive": False}

        if not weights:
            return base_sorted, {"kept": len(base_sorted), "dropped": 0, "adaptive": False}

        now = now_utc or datetime.now(timezone.utc)
        min_samples = max(1, int(getattr(config, "MACRO_ALERT_ADAPTIVE_MIN_SAMPLES", getattr(config, "MACRO_ADAPTIVE_WEIGHT_MIN_SAMPLES", 3))))
        min_mult = float(getattr(config, "MACRO_ALERT_ADAPTIVE_MIN_THEME_MULT", "0.90"))
        skip_no_clear = float(getattr(config, "MACRO_ALERT_ADAPTIVE_SKIP_NO_CLEAR_RATE", "65"))
        ultra_floor = int(getattr(config, "MACRO_ALERT_ADAPTIVE_ULTRA_SCORE_FLOOR", "10"))

        kept = []
        dropped = []
        for h in base_sorted:
            themes = list(getattr(h, "themes", []) or [])
            theme_meta = [weights.get(t) for t in themes if t in weights]
            eligible = [
                m for m in theme_meta
                if int((m or {}).get("sample_count", 0) or 0) >= min_samples
            ]

            avg_mult = 1.0
            avg_no_clear = None
            avg_confirmed = None
            if eligible:
                avg_mult = sum(float((m or {}).get("weight_mult", 1.0) or 1.0) for m in eligible) / len(eligible)
                avg_no_clear = sum(float((m or {}).get("no_clear_rate", 0.0) or 0.0) for m in eligible) / len(eligible)
                avg_confirmed = sum(float((m or {}).get("confirmed_rate", 0.0) or 0.0) for m in eligible) / len(eligible)

            age_min = max(0.0, (now - getattr(h, "published_utc", now)).total_seconds() / 60.0)
            freshness_bonus = 0.35 if age_min <= 30 else (0.15 if age_min <= 90 else 0.0)
            confirm_bonus = (float(avg_confirmed) / 100.0) * 0.75 if avg_confirmed is not None else 0.0
            no_clear_penalty = (float(avg_no_clear) / 100.0) * 0.55 if avg_no_clear is not None else 0.0
            adaptive_priority = (float(getattr(h, "score", 0) or 0) * float(avg_mult)) + freshness_bonus + confirm_bonus - no_clear_penalty

            setattr(h, "_adaptive_priority", round(adaptive_priority, 4))
            setattr(h, "_adaptive_theme_mult", round(float(avg_mult), 4))
            if avg_no_clear is not None:
                setattr(h, "_adaptive_no_clear_rate", round(float(avg_no_clear), 1))

            weak_theme = (
                bool(eligible)
                and float(avg_mult) < min_mult
                and float(avg_no_clear or 0.0) >= skip_no_clear
                and int(getattr(h, "score", 0) or 0) < ultra_floor
            )
            if weak_theme:
                dropped.append(h)
                continue
            kept.append(h)

        ranked = sorted(
            kept if kept else base_sorted,
            key=lambda x: (
                float(getattr(x, "_adaptive_priority", getattr(x, "score", 0) or 0)),
                float(getattr(x, "score", 0) or 0),
                getattr(x, "published_utc", now),
            ),
            reverse=True,
        )
        if dropped:
            logger.info(
                "[Scheduler] Macro adaptive priority filtered %d headline(s): %s",
                len(dropped),
                ", ".join(str(getattr(x, "headline_id", "") or "") for x in dropped[:5]),
            )
        return ranked, {"kept": len(kept), "dropped": len(dropped), "adaptive": True}

    def _run_macro_news_snapshot(self):
        """Manual macro risk snapshot."""
        try:
            lookback_h = max(1, int(config.MACRO_NEWS_LOOKBACK_HOURS))
            min_score = max(1, int(config.MACRO_NEWS_MIN_SCORE))
            heads = macro_news.high_impact_headlines(hours=lookback_h, min_score=min_score, limit=8)
            notifier.send_macro_news_snapshot(heads, lookback_hours=lookback_h)
        except Exception as e:
            logger.error("[Scheduler] Macro news snapshot error: %s", e, exc_info=True)

    def _run_macro_impact_tracker_sync(self):
        """Refresh post-news impact tracker samples for recent headlines."""
        if not bool(getattr(config, "MACRO_IMPACT_TRACKER_ENABLED", True)):
            return
        try:
            report = macro_impact_tracker.sync()
            logger.info(
                "[Scheduler] Macro impact tracker sync: headlines=%s ingested=%s sampled=%s weights_updated=%s status=%s",
                report.get("headlines", 0),
                report.get("ingested", 0),
                report.get("sampled", 0),
                report.get("weights_updated", 0),
                report.get("status", "ok"),
            )
        except Exception as e:
            logger.error("[Scheduler] Macro impact tracker sync error: %s", e, exc_info=True)

    def _run_macro_impact_report_snapshot(self):
        """Manual post-news impact report snapshot."""
        try:
            macro_impact_tracker.sync()
            hours = max(1, int(getattr(config, "MACRO_REPORT_DEFAULT_HOURS", 24)))
            min_score = max(1, int(getattr(config, "MACRO_NEWS_MIN_SCORE", 6)))
            report = macro_impact_tracker.build_report(hours=hours, min_score=min_score, limit=max(1, int(getattr(config, "MACRO_REPORT_MAX_HEADLINES", 5))))
            notifier.send_macro_impact_report(report)
        except Exception as e:
            logger.error("[Scheduler] Macro impact report snapshot error: %s", e, exc_info=True)

    def _run_macro_weights_snapshot(self, refresh: bool = False):
        """Manual snapshot of adaptive macro theme weights."""
        try:
            if refresh:
                macro_impact_tracker.refresh_adaptive_weights()
            report = macro_impact_tracker.build_weights_report(limit=max(1, int(getattr(config, "MACRO_WEIGHTS_DEFAULT_TOP", 8))))
            notifier.send_macro_weights_report(report)
        except Exception as e:
            logger.error("[Scheduler] Macro weights snapshot error: %s", e, exc_info=True)

    def _clear_signal_cache(self):
        """Clear the recently alerted symbols cache."""
        self._last_signal_symbols.clear()
        logger.info("[Scheduler] Signal cache cleared")

    def _adapt_intervals(self):
        """Dynamically adapt scan intervals based on session."""
        session_info = session_manager.get_session_info()
        if session_info["high_volatility"]:
            # More frequent during active sessions
            return {
                "xauusd": max(5 * 60, config.XAUUSD_SCAN_INTERVAL // 2),
                "crypto": max(2 * 60, config.CRYPTO_SCAN_INTERVAL // 2),
            }
        return {
            "xauusd": config.XAUUSD_SCAN_INTERVAL,
            "crypto": config.CRYPTO_SCAN_INTERVAL,
        }

    def setup_schedule(self):
        """Configure all scheduled tasks."""
        xauusd_mins = config.XAUUSD_SCAN_INTERVAL // 60
        crypto_mins = config.CRYPTO_SCAN_INTERVAL // 60
        fx_mins     = config.FX_SCAN_INTERVAL // 60
        stock_mins  = config.STOCK_SCAN_INTERVAL  // 60

        # ── Continuous scanners ──────────────────────────────────────────────
        schedule.every(xauusd_mins).minutes.do(self._run_xauusd_scan)
        schedule.every(crypto_mins).minutes.do(self._run_crypto_scan)
        schedule.every(max(1, fx_mins)).minutes.do(self._run_fx_scan)
        schedule.every(stock_mins).minutes.do(self._run_stock_scan)
        schedule.every(max(3, config.US_OPEN_SMART_INTERVAL_MIN)).minutes.do(self._run_us_open_smart_monitor)
        schedule.every(max(2, int(config.ECON_CALENDAR_CHECK_INTERVAL_MIN))).minutes.do(self._run_economic_calendar_alerts)
        schedule.every(max(5, int(config.MACRO_NEWS_CHECK_INTERVAL_MIN))).minutes.do(self._run_macro_news_watch)
        if bool(getattr(config, "MACRO_IMPACT_TRACKER_ENABLED", True)):
            schedule.every(max(5, int(getattr(config, "MACRO_IMPACT_TRACKER_SYNC_INTERVAL_MIN", 15)))).minutes.do(self._run_macro_impact_tracker_sync)

        # ── Market-open triggered scans (UTC times) ──────────────────────────
        # Thailand SET50 opens 03:30 UTC
        schedule.every().day.at("03:35").do(self._run_thai_scan)
        # Japan/HK/SG/IN open ~00:00-01:30 UTC
        schedule.every().day.at("01:35").do(self._run_stock_scan)
        # London/EU open 08:00 UTC
        schedule.every().day.at("08:05").do(self._run_stock_scan)
        # Gold overview at London open
        schedule.every().day.at("07:00").do(self._run_gold_overview)
        # US NYSE open 13:30 UTC
        schedule.every().day.at("13:35").do(self._run_us_open_daytrade)  # DST period
        schedule.every().day.at("14:35").do(self._run_us_open_daytrade)  # Standard time period
        # Gold overview at NY open
        schedule.every().day.at("13:00").do(self._run_gold_overview)
        # US mid-session scan 16:00 UTC
        schedule.every().day.at("16:00").do(self._run_stock_scan)
        # US close scan 20:00 UTC
        schedule.every().day.at("19:55").do(self._run_us_scan)

        # ── Maintenance ──────────────────────────────────────────────────────
        schedule.every(3).hours.do(self._clear_signal_cache)
        neural_mins = max(5, int(config.NEURAL_BRAIN_SYNC_INTERVAL_MIN))
        schedule.every(neural_mins).minutes.do(self._run_neural_sync_train)
        mt5_autopilot_line = ""
        if bool(getattr(config, "MT5_AUTOPILOT_ENABLED", True)) and bool(getattr(config, "MT5_ENABLED", False)):
            mt5_auto_mins = max(5, int(getattr(config, "MT5_AUTOPILOT_SYNC_INTERVAL_MIN", 15)))
            schedule.every(mt5_auto_mins).minutes.do(self._run_mt5_autopilot_sync)
            mt5_autopilot_line = f"  MT5 autopilot sync: every {mt5_auto_mins}m\n"
        mt5_pm_line = ""
        if bool(getattr(config, "MT5_POSITION_MANAGER_ENABLED", True)) and bool(getattr(config, "MT5_ENABLED", False)):
            mt5_pm_mins = max(1, int(getattr(config, "MT5_POSITION_MANAGER_INTERVAL_MIN", 1)))
            schedule.every(mt5_pm_mins).minutes.do(self._run_mt5_position_manager)
            mt5_pm_line = f"  MT5 position manager: every {mt5_pm_mins}m\n"
        macro_impact_line = (
            f"  Macro impact tracker sync: every {max(5, int(getattr(config, 'MACRO_IMPACT_TRACKER_SYNC_INTERVAL_MIN', 15)))}m\n"
            if bool(getattr(config, "MACRO_IMPACT_TRACKER_ENABLED", True)) else ""
        )

        logger.info(
            f"[Scheduler] Jobs configured:\n"
            f"  XAUUSD:  every {xauusd_mins}m\n"
            f"  Crypto:  every {crypto_mins}m\n"
            f"  FX Majors: every {max(1, fx_mins)}m\n"
            f"  Stocks:  every {stock_mins}m + market-open triggers\n"
            f"  US Open Smart Monitor: every {max(3, config.US_OPEN_SMART_INTERVAL_MIN)}m "
            f"(pre-open {max(0, int(getattr(config, 'US_OPEN_SMART_PREMARKET_LEAD_MIN', 60)))}m "
            f"+ post-open {max(30, int(getattr(config, 'US_OPEN_SMART_POST_OPEN_MAX_MIN', 120)))}m"
            f"{' + mood-stop' if bool(getattr(config, 'US_OPEN_MOOD_STOP_ENABLED', True)) else ''})\n"
            f"  Economic calendar: every {max(2, int(config.ECON_CALENDAR_CHECK_INTERVAL_MIN))}m\n"
            f"  Macro headline watch: every {max(5, int(config.MACRO_NEWS_CHECK_INTERVAL_MIN))}m\n"
            f"{macro_impact_line}"
            f"{mt5_autopilot_line}"
            f"{mt5_pm_line}"
            f"  Thai SET50: 03:35 UTC daily\n"
            f"  US Open Plan: 13:35 & 14:35 UTC daily (DST-safe)\n"
            f"  Gold overviews: 07:00 & 13:00 UTC daily\n"
            f"  Neural sync/train: every {neural_mins}m\n"
        )

    def _run_loop(self):
        """Main scheduler loop (runs in background thread)."""
        self.setup_schedule()
        logger.info("[Scheduler] Background loop started")

        # Run initial scans on startup
        time.sleep(5)
        self._run_gold_overview()
        time.sleep(10)
        self._run_xauusd_scan()
        time.sleep(5)
        self._run_crypto_scan()
        time.sleep(5)
        self._run_fx_scan()
        time.sleep(5)
        self._run_stock_scan()      # Initial stock scan on startup
        self._run_economic_calendar_alerts()
        self._run_macro_impact_tracker_sync()
        self._run_mt5_autopilot_sync()
        self._run_mt5_position_manager()
        self._run_neural_sync_train()

        while self.running:
            schedule.run_pending()
            time.sleep(30)

        logger.info("[Scheduler] Background loop stopped")

    def start(self):
        """Start the background scheduler thread."""
        if self.running:
            logger.warning("[Scheduler] Already running")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="DexterScheduler")
        self._thread.start()
        logger.info("[Scheduler] Started in background thread")

    def stop(self):
        """Stop the background scheduler."""
        self.running = False
        schedule.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("[Scheduler] Stopped")

    def run_once(self, task: str = "all"):
        """Manually trigger a single scan run (for CLI usage)."""
        results: dict = {}
        if task in ("xauusd", "gold", "all"):
            results["xauusd"] = self._run_xauusd_scan(force_alert=True, source="manual")
        if task in ("crypto", "all"):
            self._run_crypto_scan(force=True)
        if task in ("fx", "forex"):
            self._run_fx_scan(force=True)
        if task in ("stocks", "all"):
            self._run_stock_scan()
        if task in ("thai", "thailand"):
            self._run_thai_scan()
        if task in ("thai_vi", "th_vi", "thailand_vi"):
            self._run_thai_vi_stock_scan(force=True)
        if task in ("us",):
            self._run_us_scan()
        if task in ("us_open", "us_open_plan"):
            self._run_us_open_daytrade(force=True)
        if task in ("us_open_monitor", "monitor_us"):
            self._run_us_open_smart_monitor(force=True)
        if task in ("overview", "all"):
            self._run_gold_overview()
        if task in ("calendar", "eco", "economic"):
            self._run_economic_calendar_snapshot()
        if task in ("macro", "macro_news"):
            self._run_macro_news_snapshot()
        if task in ("macro_report", "macro_impact", "macro_impact_report"):
            self._run_macro_impact_report_snapshot()
        if task in ("macro_weights", "macro_weight", "macro_weights_report"):
            self._run_macro_weights_snapshot()
        if task in ("vi", "value", "value_trend"):
            self._run_vi_stock_scan()
        if task in ("vi_buffett", "buffett", "value_buffett"):
            self._run_vi_buffett_stock_scan()
        if task in ("vi_turnaround", "turnaround", "value_turnaround"):
            self._run_vi_turnaround_stock_scan()
        return results


scheduler = DexterScheduler()
