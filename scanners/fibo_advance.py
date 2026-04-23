"""
scanners/fibo_advance.py - Fibonacci Advance Sniper Scanner (Dual-Speed, Institution-Grade)

Two operating modes running in the same scanner:

  SNIPER MODE (H4 + H1 confluence)
  ─────────────────────────────────
  - H4 impulse → H1 Golden Pocket alignment
  - Less frequent, RR 1.618–2.618 extension targets
  - Full Elliott Wave context validation (3 structural rules)
  - Pattern: FIBO_SNIPER_*

  SCOUT MODE (H1 + M15 intermediate)
  ────────────────────────────────────
  - Fires WHILE waiting for Sniper setup
  - H1 bias direction → M15 Fibonacci entry
  - More frequent (3–5x per day), TP = 1.0–1.272 extension
  - Must trade in SAME direction as H4 bias (never against big picture)
  - MTF Fib zone stacking: M15 Fib must align with H1 Fib level
  - Pattern: FIBO_SCOUT_*

"Fibonacci Killer" protection (both modes):
  - ATR expansion guard: trending/news days blow through Fibonacci levels
  - Delta proxy guard: strong momentum = no respect, price will continue
  - Day type guard: panic_spread / fast_expansion = Fibonacci invalidated
  - Volume spike guard: anomalous volume = institutional repricing, not bounce
  - Spread expansion guard: thin liquidity = no bounce at level
  - Retracement velocity guard: price falling too fast through levels = stop hunt

Institution-grade gates (v2):
  - Entry Sharpness Score: 8 microstructure features, knife/caution/sharp
  - Volume Profile (POC/HVN/LVN): structural confluence at Fib level
  - H4 bias: swing structure (HH/HL vs LH/LL) not just EMA
  - Impulse freshness: reject stale impulses
  - Elliott Wave: validated with 3 structural rules
  - Scout MTF Fib stacking: M15 Fib ∩ H1 Fib zone = true confluence
"""
import logging
import sqlite3
from typing import Optional
from datetime import datetime, timezone, timedelta, date

import numpy as np
import pandas as pd

from market.data_fetcher import xauusd_provider, session_manager
from analysis.technical import TechnicalAnalysis
from analysis.smc import SMCAnalyzer
from analysis.signals import SignalGenerator, TradeSignal
from analysis.fibonacci import FibonacciAnalyzer
from config import config
from learning.reversal_training_dataset import evaluate_reversal_template_fit
from learning.live_profile_autopilot import LiveProfileAutopilot

logger = logging.getLogger(__name__)

ta   = TechnicalAnalysis()
smc  = SMCAnalyzer()
sig  = SignalGenerator(min_confidence=config.MIN_SIGNAL_CONFIDENCE)
fibo = FibonacciAnalyzer(
    swing_lookback=int(getattr(config, "FIBO_ADVANCE_SWING_LOOKBACK", 5) or 5),
    min_impulse_atr_mult=float(getattr(config, "FIBO_ADVANCE_MIN_IMPULSE_ATR", 1.2) or 1.2),
    impulse_birth_enabled=bool(getattr(config, "FIBO_IMPULSE_BIRTH_ENABLED", True)),
    impulse_birth_base_bars=int(getattr(config, "FIBO_IMPULSE_BIRTH_BASE_BARS", 8) or 8),
    impulse_birth_max_base_atr=float(getattr(config, "FIBO_IMPULSE_BIRTH_MAX_BASE_ATR", 1.2) or 1.2),
    impulse_birth_min_break_atr=float(getattr(config, "FIBO_IMPULSE_BIRTH_MIN_BREAK_ATR", 0.5) or 0.5),
    impulse_birth_min_body_pct=float(getattr(config, "FIBO_IMPULSE_BIRTH_MIN_BODY_PCT", 0.55) or 0.55),
    impulse_birth_max_breakout_age=int(getattr(config, "FIBO_IMPULSE_BIRTH_MAX_BREAKOUT_AGE", 3) or 3),
    impulse_birth_max_chase_atr=float(getattr(config, "FIBO_IMPULSE_BIRTH_MAX_CHASE_ATR", 1.5) or 1.5),
    impulse_birth_whipsaw_lookback=int(getattr(config, "FIBO_IMPULSE_BIRTH_WHIPSAW_LOOKBACK", 10) or 10),
    impulse_birth_min_confidence=float(getattr(config, "FIBO_IMPULSE_BIRTH_MIN_CONFIDENCE", 0.55) or 0.55),
    impulse_birth_score_bonus=float(getattr(config, "FIBO_IMPULSE_BIRTH_SCORE_BONUS", 10.0) or 10.0),
    impulse_birth_stale_age_bars=int(getattr(config, "FIBO_IMPULSE_BIRTH_STALE_AGE_BARS", 25) or 25),
)
autopilot = LiveProfileAutopilot()

# ── Config shortcuts (with safe fallbacks) ────────────────────────────────────
def _cfg(key: str, default):
    val = getattr(config, key, default)
    if val is None:
        return default
    return type(default)(val)


class FiboAdvanceScanner:
    """
    Fibonacci Advance Sniper for XAUUSD — Institution Grade.

    Lane  : fibo_advance
    Family: xau_fibo_advance
    Source: fibo_xauusd

    Signal flow:
      H4 swings → impulse detection → freshness gate → Fibonacci levels
      → zone check → SMC confluence → Volume Profile confluence
      → Fibonacci Killer guards (incl. retracement velocity)
      → Entry Sharpness Score (knife/caution/sharp)
      → microstructure gate → TradeSignal (limit order at Fibonacci level)
    """

    # ── Fibonacci Killer day types: price will NOT respect Fibonacci ───────────
    KILLER_DAY_TYPES  = {"panic_spread", "fast_expansion", "repricing"}
    KILLER_STATE_LABELS = {"failed_fade_risk", "panic_dislocation", "continuation_drive"}

    def __init__(self):
        self.last_signal: Optional[TradeSignal] = None
        self.scan_count  = 0
        self.signal_count = 0
        self._last_scan_diagnostics: dict = {}
        # ── Circuit breaker state ──────────────────────────────────────────
        self._consecutive_losses: int = 0
        self._daily_losses_usd: float = 0.0
        self._daily_trades: int = 0
        self._pause_until: Optional[datetime] = None
        self._last_reset_date: Optional[date] = None

    def get_last_scan_diagnostics(self) -> dict:
        return dict(self._last_scan_diagnostics or {})

    def _set_diag(self, **kwargs) -> None:
        try:
            self._last_scan_diagnostics = dict(kwargs)
        except Exception:
            self._last_scan_diagnostics = {}

    # ── Soft Circuit Breaker + Trade Result Reporting ───────────────────────────

    def report_trade_result(self, pnl_usd: float) -> None:
        """
        Called after each fibo_advance trade closes.
        Feeds the consecutive loss tracker + daily loss cap.
        """
        if pnl_usd < 0:
            self._consecutive_losses += 1
            self._daily_losses_usd += pnl_usd
            logger.info("[FiboAdvance:CB] Loss #%d | daily=$%.2f | consec=%d",
                        self._daily_trades + 1, self._daily_losses_usd, self._consecutive_losses)
        else:
            if self._consecutive_losses > 0:
                logger.info("[FiboAdvance:CB] Win resets consecutive counter (was %d)",
                            self._consecutive_losses)
            self._consecutive_losses = 0
        self._daily_trades += 1

    def _check_circuit_breaker(self) -> tuple[bool, str, float]:
        """
        Soft circuit breaker: 3 levels — warning, caution, emergency.
        Only emergency (level 3) actually blocks. Levels 1-2 reduce confidence.
        Returns (allowed, reason, confidence_modifier).
        """
        today = date.today()
        if self._last_reset_date != today:
            self._daily_losses_usd = 0.0
            self._daily_trades = 0
            self._consecutive_losses = 0
            self._last_reset_date = today

        if self._pause_until and datetime.now(timezone.utc) < self._pause_until:
            return False, f"paused_until_{self._pause_until.isoformat()}", 0.0

        # Level 3 — Emergency: hard stop
        max_consec_hard = int(_cfg("FIBO_ADVANCE_HARD_CONSEC_LOSSES", 10))
        daily_cap_hard = float(_cfg("FIBO_ADVANCE_HARD_DAILY_LOSS_USD", 150.0))

        if self._consecutive_losses >= max_consec_hard:
            pause_min = int(_cfg("FIBO_ADVANCE_HARD_PAUSE_MIN", 120))
            self._pause_until = datetime.now(timezone.utc) + timedelta(minutes=pause_min)
            self._consecutive_losses = 0
            return False, f"emergency_consec:{max_consec_hard}→paused_{pause_min}min", 0.0

        if self._daily_losses_usd <= -daily_cap_hard:
            self._pause_until = datetime.now(timezone.utc).replace(
                hour=23, minute=59, second=59)
            return False, f"emergency_daily_loss:${abs(self._daily_losses_usd):.2f}", 0.0

        # Level 2.5 — Soft pause at 5 consecutive losses (prevent April 7 disaster)
        # After 5 straight losses, pause 30min to let market settle
        soft_pause_threshold = int(_cfg("FIBO_ADVANCE_SOFT_PAUSE_CONSEC", 5))
        if self._consecutive_losses >= soft_pause_threshold:
            soft_pause_min = int(_cfg("FIBO_ADVANCE_SOFT_PAUSE_MIN", 30))
            self._pause_until = datetime.now(timezone.utc) + timedelta(minutes=soft_pause_min)
            logger.info("[FiboAdvance:CB] Soft pause: %d consec losses → pause %dmin",
                        self._consecutive_losses, soft_pause_min)
            return False, f"soft_pause_consec:{self._consecutive_losses}→{soft_pause_min}min", 0.0

        # Level 2 — Caution: significant confidence reduction
        if self._consecutive_losses >= 4:
            logger.info("[FiboAdvance:CB] Caution: %d consec losses → conf -25", self._consecutive_losses)
            return True, "caution_consec_4", -25.0

        if self._daily_losses_usd <= -75.0:
            logger.info("[FiboAdvance:CB] Caution: daily loss $%.2f → conf -30", abs(self._daily_losses_usd))
            return True, "caution_daily_75", -30.0

        # Level 1 — Warning: mild confidence reduction
        if self._consecutive_losses >= 3:
            logger.info("[FiboAdvance:CB] Warning: %d consec losses → conf -15", self._consecutive_losses)
            return True, "warning_consec_3", -15.0

        if self._daily_losses_usd <= -30.0:
            logger.info("[FiboAdvance:CB] Warning: daily loss $%.2f → conf -20", abs(self._daily_losses_usd))
            return True, "warning_daily_30", -20.0

        return True, "circuit_ok", 0.0

    # ── Fibonacci Killer Detection ─────────────────────────────────────────────

    def _fibonacci_killer_check(self, snapshot: dict, atr: float,
                                df_entry: pd.DataFrame) -> tuple[bool, str, float]:
        """
        Weighted Fibonacci Killer — confidence modifier, NOT binary gate.

        Returns (allowed, reason, confidence_modifier).

        Killer score system (cumulative):
          ATR expansion:        1-3 points (proportional to ratio)
          Delta momentum:       1-2 points
          Volume spike:         1-2 points
          Day type:             5 points (hard block at >= 8 with others)
          State label:          7 points (hard block)
          Spread expansion:     1-2 points
          Retracement velocity: 2-4 points

        Decision:
          score >= 8  → HARD BLOCK (allowed=False, truly dangerous)
          score 5-7   → conf -20 to -35 (severe degradation)
          score 3-4   → conf -10 to -18 (moderate degradation)
          score 1-2   → conf -3 to -8  (mild degradation)
          score 0     → no impact
        """
        features = snapshot.get("features", {}) if snapshot else {}
        score = 0
        reasons: list[str] = []

        # ── 1. ATR expansion (1-3 points) ─────────────────────────────────────
        atr_ratio = 0.0
        if len(df_entry) >= 20 and atr > 0:
            # NaN guard: rolling(20).mean() produces NaN when any of the last
            # 20 atr_14 values is NaN; iloc[-1] can also be NaN. Silent NaN
            # would make all downstream comparisons False and the killer would
            # miss ATR expansion. Fall back to the passed-in atr (current).
            if "atr_14" in df_entry.columns:
                _r = df_entry["atr_14"].iloc[-1]
                _a = df_entry["atr_14"].rolling(20).mean().iloc[-1]
                recent_atr = float(_r) if pd.notna(_r) else atr
                avg_atr    = float(_a) if pd.notna(_a) else atr
            else:
                recent_atr = atr
                avg_atr    = atr
            if avg_atr > 0:
                atr_ratio = recent_atr / avg_atr
                atr_kill_mult = float(_cfg("FIBO_ADVANCE_KILLER_ATR_MULT", 1.8))
                if atr_ratio > atr_kill_mult * 1.3:
                    score += 3
                    reasons.append(f"atr_extreme:{atr_ratio:.2f}x")
                elif atr_ratio > atr_kill_mult:
                    score += 2
                    reasons.append(f"atr_high:{atr_ratio:.2f}x")
                elif atr_ratio > atr_kill_mult * 0.8:
                    score += 1
                    reasons.append(f"atr_elevated:{atr_ratio:.2f}x")

        # ── 2. Delta proxy momentum (1-2 points) ─────────────────────────────
        delta = float(features.get("delta_proxy", 0.0))
        delta_kill = float(_cfg("FIBO_ADVANCE_KILLER_DELTA_THRESHOLD", 0.40))
        abs_delta = abs(delta)
        if abs_delta > delta_kill * 1.5:
            score += 2
            reasons.append(f"delta_extreme:{delta:.3f}")
        elif abs_delta > delta_kill:
            score += 1
            reasons.append(f"delta_high:{delta:.3f}")

        # ── 3. Volume spike (1-2 points) ──────────────────────────────────────
        bar_vol   = float(features.get("bar_volume_proxy", 0.0))
        vol_spike = float(_cfg("FIBO_ADVANCE_KILLER_VOL_SPIKE", 2.5))
        if bar_vol > vol_spike * 1.5:
            score += 2
            reasons.append(f"volume_extreme:{bar_vol:.2f}")
        elif bar_vol > vol_spike:
            score += 1
            reasons.append(f"volume_spike:{bar_vol:.2f}")

        # ── 4. Day type check (5 points — hard block with state_label) ────────
        day_type    = str(snapshot.get("day_type", "") or "")
        state_label = str(snapshot.get("state_label", "") or "")
        if day_type in self.KILLER_DAY_TYPES:
            score += 5
            reasons.append(f"day_type:{day_type}")
        if state_label in self.KILLER_STATE_LABELS:
            score += 7
            reasons.append(f"state_label:{state_label}")

        # ── 5. Spread expansion (1-2 points) ──────────────────────────────────
        spread_expansion = float(features.get("spread_expansion_ratio", 1.0))
        max_spread_exp   = float(_cfg("FIBO_ADVANCE_KILLER_MAX_SPREAD_EXP", 1.25))
        if spread_expansion > max_spread_exp * 1.5:
            score += 2
            reasons.append(f"spread_extreme:{spread_expansion:.2f}")
        elif spread_expansion > max_spread_exp:
            score += 1
            reasons.append(f"spread_wide:{spread_expansion:.2f}")

        # ── 6. Retracement velocity (2-4 points) ─────────────────────────────
        retrace_vel = 0.0
        if len(df_entry) >= 5 and "atr_14" in df_entry.columns:
            retrace_vel = self._retracement_velocity(df_entry, atr)
            vel_kill = float(_cfg("FIBO_ADVANCE_KILLER_RETRACE_VEL", 2.0))
            if retrace_vel > vel_kill * 1.5:
                score += 4
                reasons.append(f"retrace_extreme:{retrace_vel:.2f}x_atr")
            elif retrace_vel > vel_kill:
                score += 2
                reasons.append(f"retrace_fast:{retrace_vel:.2f}x_atr")
            elif retrace_vel > vel_kill * 0.8:
                score += 1
                reasons.append(f"retrace_elevated:{retrace_vel:.2f}x_atr")

        # ── Decision: hard block vs confidence degradation ─────────────────────
        reason_str = "+".join(reasons) if reasons else "no_killer"

        if score >= 8:
            # Hard block: genuinely dangerous (state_label panic + other severe)
            return False, f"killer_hard_block(score={score}):{reason_str}", 0.0

        if score >= 5:
            conf_mod = -20.0 - (score - 5) * 5.0  # -20 to -35
            return True, f"killer_severe(score={score}):{reason_str}", conf_mod

        if score >= 3:
            conf_mod = -10.0 - (score - 3) * 4.0  # -10 to -18
            return True, f"killer_moderate(score={score}):{reason_str}", conf_mod

        if score >= 1:
            conf_mod = -3.0 - (score - 1) * 2.5  # -3 to -8
            return True, f"killer_mild(score={score}):{reason_str}", conf_mod

        return True, "no_killer", 0.0

    # ── Retracement Velocity (Killer #6) ──────────────────────────────────────

    def _retracement_velocity(self, df_entry: pd.DataFrame, atr: float) -> float:
        """
        Measure how fast price is moving through recent bars relative to ATR.
        Fast retrace = stop hunt / liquidity sweep, not genuine support.

        Returns velocity as multiple of ATR. Values > 2.0 = dangerously fast.
        """
        if atr <= 0 or len(df_entry) < 3:
            return 0.0

        lookback = min(5, len(df_entry) - 1)
        recent = df_entry.iloc[-lookback:]
        total_range = 0.0
        for i in range(len(recent)):
            total_range += abs(float(recent["high"].iloc[i]) - float(recent["low"].iloc[i]))

        avg_bar_range = total_range / max(lookback, 1)
        return avg_bar_range / atr if atr > 0 else 0.0

    # ── Microstructure Gate ──────────────────────────────────────────────────

    def _check_microstructure(self, direction: str, confidence: float,
                              snapshot: dict) -> tuple[bool, str]:
        """
        Validate delta and DOM imbalance support the Fibonacci entry direction.
        For Fibonacci entries we're more lenient because price IS retracing —
        delta may be slightly adverse before reversing.
        """
        if not bool((snapshot or {}).get("ok")):
            status = str((snapshot or {}).get("status") or "capture_unavailable").strip().lower() or "capture_unavailable"
            return True, f"micro_capture_unavailable:{status}"
        features = snapshot.get("features", {}) if snapshot else {}
        if not features:
            return True, "micro_capture_unavailable:no_features"
        spots_count = int(features.get("spots_count", 0) or 0)
        depth_count = int(features.get("depth_count", 0) or 0)
        if spots_count < 3 and depth_count < 3:
            return True, f"micro_capture_insufficient:{spots_count}s_{depth_count}d"
        delta       = float(features.get("delta_proxy", 0.0))
        imbalance   = float(features.get("depth_imbalance", 0.0))
        tick_vel    = float(features.get("bar_volume_proxy", 0.0))

        delta_thr = float(_cfg("FIBO_ADVANCE_MICRO_DELTA_THR", 0.30))
        imb_thr   = float(_cfg("FIBO_ADVANCE_MICRO_IMB_THR",   0.35))
        vel_thr   = float(_cfg("FIBO_ADVANCE_MICRO_VEL_THR",   0.05))

        if tick_vel < vel_thr:
            if tick_vel == 0.0 and delta == 0.0 and imbalance == 0.0:
                return True, "micro_stale_data_passthrough:all_zero"
            return False, f"low_tick_velocity:{tick_vel:.3f}"

        if direction == "long":
            if delta < -delta_thr:
                return False, f"adverse_delta_long:{delta:.3f}"
            if imbalance < -imb_thr:
                return False, f"adverse_imbalance_long:{imbalance:.3f}"
        else:
            if delta > delta_thr:
                return False, f"adverse_delta_short:{delta:.3f}"
            if imbalance > imb_thr:
                return False, f"adverse_imbalance_short:{imbalance:.3f}"

        return True, "micro_aligned"

    # ── Entry Sharpness Score Gate ────────────────────────────────────────────

    def _check_entry_sharpness(self, direction: str, snapshot: dict,
                               mode: str = "sniper") -> tuple[bool, str, dict]:
        """
        Compute Entry Sharpness Score from microstructure features.
        Returns (passed, reason, sharpness_result).

        Knife band = block entry (institutional traps).
        Caution band = reduce risk.
        Sharp band = full confidence.
        """
        features = snapshot.get("features", {}) if snapshot else {}
        if not features:
            return True, "no_features_available", {"sharpness_score": 50, "sharpness_band": "normal"}

        try:
            from analysis.entry_sharpness import compute_entry_sharpness_score
            sharpness = compute_entry_sharpness_score(features, direction)
        except Exception as e:
            logger.warning("[FiboAdvance] sharpness check error — degrading: %s", e)
            return True, "sharpness_error_degraded", {"sharpness_score": 25, "sharpness_band": "caution"}

        score = int(sharpness.get("sharpness_score", 50) or 50)
        band  = str(sharpness.get("sharpness_band", "normal") or "normal")

        # Sniper mode: stricter knife threshold (we want the best entries)
        # Scout mode: slightly more lenient (we accept moderate quality)
        if mode == "sniper":
            knife_thr = int(_cfg("FIBO_ADVANCE_SHARPNESS_KNIFE_THR", 30))
        else:
            knife_thr = int(_cfg("FIBO_SCOUT_SHARPNESS_KNIFE_THR", 25))

        if band == "knife" and score < knife_thr:
            return False, f"sharpness_knife:{score}<{knife_thr}", sharpness

        return True, f"sharpness_{band}:{score}", sharpness

    # ── Volume Profile Confluence ─────────────────────────────────────────────

    def _check_volume_profile(self, entry_price: float, direction: str,
                              symbol: str = "XAUUSD") -> tuple[float, str, dict]:
        """
        Check if Fib entry level has Volume Profile structural support.

        Returns (score_adjustment, reason, vp_check_result).
          HVN/POC at level = +10 score (institutional support/resistance)
          LVN at level = -8 score (thin liquidity, price will slice through)
          Inside VA = +3 (fair value area)
        """
        try:
            from api.report_store import report_store
            from analysis.volume_profile import check_entry_vs_profile, get_tick_config

            vp_report = dict(
                report_store.get_report(f"volume_profile_{symbol.lower()}")
                or report_store.get_report("volume_profile")
                or {}
            )
            vp_data = dict(vp_report.get("vp") or {})
            if not vp_data.get("poc"):
                return 0.0, "no_vp_data", {}

            tc = get_tick_config(symbol)
            vp_check = check_entry_vs_profile(
                entry_price, direction, vp_data,
                tick_size=float(tc.get("tick_size", 0.01)),
                bucket_ticks=int(tc.get("bucket_ticks", 10)),
            )

            confirmation = str(vp_check.get("vp_confirmation", "neutral") or "neutral")
            near_lvn = bool(vp_check.get("near_lvn", False))

            score_adj = 0.0
            if confirmation == "strong":
                score_adj = 10.0
            elif confirmation == "moderate":
                score_adj = 5.0
            elif confirmation == "weak" or near_lvn:
                score_adj = -8.0
            elif bool(vp_check.get("in_value_area", False)):
                score_adj = 3.0

            reason = f"vp_{confirmation}"
            if near_lvn:
                reason += "_LVN_WARNING"

            return score_adj, reason, vp_check

        except Exception as e:
            logger.debug("[FiboAdvance] volume_profile check error: %s", e)
            return 0.0, "vp_error", {}

    # ── Impulse Freshness Gate ────────────────────────────────────────────────

    def _check_impulse_freshness(self, fib_levels, df_entry: pd.DataFrame,
                                 mode: str = "sniper") -> tuple[bool, str]:
        """
        Reject stale impulses. Institution desks don't trade Fib levels from
        moves that happened weeks ago — liquidity has shifted.

        Sniper (H4 structure): max 40 bars on H1 since swing end (~40 hours)
        Scout (H1 structure): max 30 bars on M15 since swing end (~7.5 hours)
        """
        if fib_levels is None:
            return False, "no_fib_levels"

        bars_since_swing_end = max(0, len(df_entry) - 1 - fib_levels.swing_end_idx)

        if mode == "sniper":
            max_bars = int(_cfg("FIBO_ADVANCE_MAX_IMPULSE_AGE_BARS", 40))
        else:
            max_bars = int(_cfg("FIBO_SCOUT_MAX_IMPULSE_AGE_BARS", 30))

        if bars_since_swing_end > max_bars:
            return False, f"stale_impulse:{bars_since_swing_end}bars>{max_bars}max"

        return True, f"fresh_impulse:{bars_since_swing_end}bars"

    # ── Trend Confidence Modifier (Enhanced — D1 Strong Trend Filter) ───────

    def _trend_confidence_modifier(self, df_d1: Optional[pd.DataFrame],
                                   df_h4: pd.DataFrame, direction: str) -> tuple[float, str]:
        """
        Returns confidence adjustment based on trend alignment.
        Negative = counter-trend penalty, Positive = aligned bonus.
        Does NOT block signals — lets brain learn from all setups.

        Enhanced: D1 strong trend alone triggers -25 penalty (prevents April 7 type
        disasters where bot kept buying in clear daily downtrend).
        """
        try:
            df_h4c = ta.add_ema(df_h4.copy(), periods=[21, 50])
            h4_ema21 = float(df_h4c["ema_21"].iloc[-1])
            h4_ema50 = float(df_h4c["ema_50"].iloc[-1])
            h4_close = float(df_h4c["close"].iloc[-1])
            h4_bearish = h4_close < h4_ema21 < h4_ema50
            h4_bullish = h4_close > h4_ema21 > h4_ema50

            d1_bearish = False
            d1_bullish = False
            d1_strong_bearish = False
            d1_strong_bullish = False
            if df_d1 is not None and not df_d1.empty and len(df_d1) >= 50:
                df_d1c = ta.add_ema(df_d1.copy(), periods=[21, 50])
                d1_ema21 = float(df_d1c["ema_21"].iloc[-1])
                d1_ema50 = float(df_d1c["ema_50"].iloc[-1])
                d1_close = float(df_d1c["close"].iloc[-1])
                d1_bearish = d1_close < d1_ema21 < d1_ema50
                d1_bullish = d1_close > d1_ema21 > d1_ema50

                # Strong trend: EMA spread > 0.5% of price (clear directional bias)
                ema_spread_pct = abs(d1_ema21 - d1_ema50) / d1_close * 100 if d1_close > 0 else 0
                strong_threshold = float(_cfg("FIBO_TREND_STRONG_EMA_SPREAD_PCT", 0.5))
                if d1_bearish and ema_spread_pct >= strong_threshold:
                    d1_strong_bearish = True
                if d1_bullish and ema_spread_pct >= strong_threshold:
                    d1_strong_bullish = True

            # ══ STRONG COUNTER-TREND (D1 alone — prevents April 7 disaster) ══
            # If D1 is in a STRONG bearish trend and we want to go long → heavy penalty
            if direction == "long" and d1_strong_bearish:
                return -25.0, "d1_strong_bearish_vs_long"
            if direction == "short" and d1_strong_bullish:
                return -25.0, "d1_strong_bullish_vs_short"

            # ══ COMBINED COUNTER-TREND (D1+H4 both oppose) ══════════════════
            if direction == "long" and d1_bearish and h4_bearish:
                return -20.0, "counter_trend_penalty:d1=bearish,h4=bearish"
            if direction == "short" and d1_bullish and h4_bullish:
                return -20.0, "counter_trend_penalty:d1=bullish,h4=bullish"

            # ══ D1-ONLY COUNTER-TREND (D1 opposes, H4 neutral) ══════════════
            if direction == "long" and d1_bearish and not h4_bullish:
                return -12.0, "d1_bearish_h4_neutral_vs_long"
            if direction == "short" and d1_bullish and not h4_bearish:
                return -12.0, "d1_bullish_h4_neutral_vs_short"

            # ══ ALIGNED BONUS (D1+H4 both support direction) ════════════════
            if (d1_bullish and h4_bullish and direction == "long") or \
               (d1_bearish and h4_bearish and direction == "short"):
                return +5.0, "trend_aligned_bonus"

            # ══ MIXED: D1 weakly opposes, H4 agrees — small penalty ═════════
            # D1 directionality opposes but is not strong enough to trip the
            # -25 gate. Do NOT reward H4-only alignment with a bonus; the
            # higher-timeframe is still against us.
            if direction == "long" and d1_bearish and h4_bullish:
                return -3.0, "d1_weak_bearish_h4_bullish_mixed_long"
            if direction == "short" and d1_bullish and h4_bearish:
                return -3.0, "d1_weak_bullish_h4_bearish_mixed_short"

            # ══ H4-ONLY ALIGNED (H4 supports, D1 neutral) ═══════════════════
            if (h4_bullish and direction == "long") or (h4_bearish and direction == "short"):
                return +2.0, "h4_aligned"

            return 0.0, "trend_neutral"
        except Exception as e:
            logger.debug("[FiboAdvance] trend check error: %s", e)
            return 0.0, "trend_check_error_passthrough"

    # ── H4 Bias — Structure-Based (Institution Grade) ─────────────────────────

    def _get_h4_bias(self, df_h4: pd.DataFrame, current_price: float,
                     atr_h4: float) -> str:
        """
        Determine H4 directional bias using market structure analysis.

        Institution-grade: uses swing structure (HH/HL vs LH/LL) + BOS,
        NOT just EMA crossover (which is retail-grade and lags).

        Returns 'long' | 'short' | 'neutral'
        """
        try:
            # ── Primary: Swing structure (HH/HL vs LH/LL) ────────────────
            swings = fibo.detect_swings(df_h4, left_bars=5, right_bars=3)
            if len(swings) >= 4:
                recent = swings[-4:]
                highs = [s for s in recent if s.swing_type == "high"]
                lows  = [s for s in recent if s.swing_type == "low"]

                hh = len(highs) >= 2 and highs[-1].price > highs[-2].price
                hl = len(lows) >= 2 and lows[-1].price > lows[-2].price
                lh = len(highs) >= 2 and highs[-1].price < highs[-2].price
                ll = len(lows) >= 2 and lows[-1].price < lows[-2].price

                if hh and hl:
                    return "long"
                if lh and ll:
                    return "short"

            # ── Secondary: SMC bias on H4 ─────────────────────────────────
            try:
                smc_h4 = smc.analyze(df_h4, current_price=current_price)
                if smc_h4 and smc_h4.bias in ("long", "short"):
                    return smc_h4.bias
            except Exception:
                pass

            # ── Tertiary fallback: EMA alignment ──────────────────────────
            df = ta.add_ema(df_h4.copy(), periods=[21, 50])
            ema21 = float(df["ema_21"].iloc[-1])
            ema50 = float(df["ema_50"].iloc[-1])
            close = float(df["close"].iloc[-1])

            if close > ema21 > ema50:
                return "long"
            if close < ema21 < ema50:
                return "short"

            return "neutral"
        except Exception:
            return "neutral"

    # ── Scout MTF Fib Zone Stacking ───────────────────────────────────────────

    def _check_mtf_fib_stacking(self, m15_fib_price: float, df_h1: pd.DataFrame,
                                atr_h1: float, current_price: float,
                                smc_context) -> tuple[bool, float, str]:
        """
        Check if M15 Fibonacci level aligns with any H1 Fibonacci level.
        True multi-timeframe confluence = institution-grade precision.

        Returns (has_stacking, bonus_score, reason).
          Stacking found = +12 score bonus
          No stacking = 0 (still allowed, just less confluence)
        """
        try:
            h1_fibo_ctx = fibo.analyze(
                df_structure=df_h1,
                df_entry=df_h1,
                current_price=current_price,
                atr=atr_h1,
                smc_context=smc_context,
            )
            if h1_fibo_ctx.fib_levels is None:
                return False, 0.0, "no_h1_fib_levels"

            h1_fib = h1_fibo_ctx.fib_levels
            zone_tol = atr_h1 * 0.4

            # Check proximity to each H1 Fib level
            for ratio, level_price in h1_fib.levels.items():
                if 0.2 <= ratio <= 0.8 and abs(m15_fib_price - level_price) <= zone_tol:
                    # Golden Pocket stacking = higher bonus
                    if 0.618 <= ratio <= 0.65:
                        return True, 15.0, f"MTF_STACK_GP_H1:{ratio:.3f}@{level_price:.2f}"
                    return True, 12.0, f"MTF_STACK_H1:{ratio:.3f}@{level_price:.2f}"

            return False, 0.0, "no_h1_fib_alignment"

        except Exception as e:
            logger.debug("[FiboAdvance] MTF stacking error: %s", e)
            return False, 0.0, "mtf_stacking_error"

    # ── Golden Pocket Quality Gate ─────────────────────────────────────────────

    def _fib_entry_quality_gate(
        self,
        direction: str,
        fibo_ctx,
        snapshot: dict,
        df_entry: pd.DataFrame,
        atr: float,
        *,
        mode: str,
        sharpness: dict = None,
        mtf_stacking: bool = False,
    ) -> tuple[bool, str, dict]:
        """Require golden-pocket depth plus enough directional evidence before a Fib signal.

        Blocks signals at 0.382 / 0.5 retracement levels — only fires at 61.8 Golden Pocket.
        Ensures impulse quality + microstructure agreement before entry.
        """
        prefix = "FIBO_SCOUT" if mode == "scout" else "FIBO_ADVANCE"
        ratio = float(getattr(fibo_ctx, "nearest_level_ratio", 0.0) or 0.0)
        depth = float(getattr(fibo_ctx, "retracement_depth", 0.0) or 0.0)
        tol = max(0.0, float(_cfg("FIBO_GOLDEN_GATE_TOLERANCE", 0.012)))
        min_ratio = float(_cfg(f"{prefix}_MIN_ENTRY_LEVEL_RATIO", 0.618))
        min_depth = float(_cfg(f"{prefix}_MIN_RETRACEMENT_DEPTH", 0.618))
        max_depth = float(_cfg(f"{prefix}_MAX_RETRACEMENT_DEPTH", 0.786))
        require_gp = bool(getattr(config, f"{prefix}_REQUIRE_GOLDEN_POCKET", True))
        in_gp = bool(getattr(fibo_ctx, "in_golden_pocket", False))
        wave_phase = str(getattr(fibo_ctx, "wave_phase", "unknown") or "unknown")
        wave_confidence = float(getattr(fibo_ctx, "wave_confidence", 0.0) or 0.0)
        correction_end_confirmed = bool(getattr(fibo_ctx, "correction_end_confirmed", False))
        correction_end_score = float(getattr(fibo_ctx, "correction_end_score", 0.0) or 0.0)
        correction_end_override_enabled = bool(getattr(config, f"{prefix}_ALLOW_CONFIRMED_CORRECTION_END_OUTSIDE_GP", True))
        correction_end_min_score = float(getattr(config, f"{prefix}_CORRECTION_END_MIN_SCORE", 5.0) or 5.0)
        correction_end_min_wave_conf = float(getattr(config, f"{prefix}_CORRECTION_END_MIN_WAVE_CONFIDENCE", 0.5) or 0.5)
        correction_end_min_ratio = float(getattr(config, f"{prefix}_CORRECTION_END_MIN_ENTRY_RATIO", 0.55) or 0.55)
        correction_end_max_ratio = float(getattr(config, f"{prefix}_CORRECTION_END_MAX_ENTRY_RATIO", 0.82) or 0.82)
        correction_end_override = bool(
            correction_end_override_enabled
            and correction_end_confirmed
            and wave_phase in {"correction_end", "impulse_restart"}
            and correction_end_score >= correction_end_min_score
            and wave_confidence >= correction_end_min_wave_conf
            and correction_end_min_ratio <= ratio <= correction_end_max_ratio
        )
        details = {
            "mode": mode,
            "direction": str(direction or ""),
            "nearest_level_ratio": round(ratio, 4),
            "retracement_depth": round(depth, 4),
            "in_golden_pocket": in_gp,
            "min_entry_level_ratio": round(min_ratio, 4),
            "min_retracement_depth": round(min_depth, 4),
            "max_retracement_depth": round(max_depth, 4),
            "wave_phase": wave_phase,
            "wave_confidence": round(wave_confidence, 3),
            "correction_end_confirmed": correction_end_confirmed,
            "correction_end_score": round(correction_end_score, 2),
            "correction_end_override": correction_end_override,
            "correction_end_min_entry_ratio": round(correction_end_min_ratio, 4),
            "correction_end_max_entry_ratio": round(correction_end_max_ratio, 4),
        }

        # Default rule: require 61.8+ / GP depth. Exception: confirmed correction-end
        # / impulse-restart phase with additional evidence may enter slightly above
        # or below the golden pocket when the reversal is already proving itself.
        if ratio + tol < min_ratio and not correction_end_override:
            return False, f"pre_golden_level:{ratio:.3f}<{min_ratio:.3f}", details
        if depth + tol < min_depth and not correction_end_override:
            return False, f"pre_golden_depth:{depth:.3f}<{min_depth:.3f}", details
        if max_depth > 0 and depth - tol > max_depth:
            return False, f"overdeep_retracement:{depth:.3f}>{max_depth:.3f}", details
        if require_gp and not in_gp and not correction_end_override:
            return False, f"not_golden_pocket:ratio={ratio:.3f}:depth={depth:.3f}", details

        template_enabled = bool(getattr(config, "FIBO_REVERSAL_TEMPLATE_ENABLED", True))
        template_level_tol = max(0.0, float(getattr(config, "FIBO_REVERSAL_TEMPLATE_LEVEL_TOLERANCE", 0.020) or 0.020))
        correction_end_min_template_score = int(getattr(config, f"{prefix}_CORRECTION_END_MIN_TEMPLATE_SCORE", 4) or 4)
        near_reversal_level = (
            in_gp
            or correction_end_override
            or abs(ratio - 0.618) <= template_level_tol
            or abs(ratio - 0.650) <= template_level_tol
        )
        template_info = {
            "ok": True,
            "applied": False,
            "score": 0,
            "min_score": int(getattr(config, "FIBO_REVERSAL_TEMPLATE_MIN_SCORE", 4) or 4),
            "reason": "not_near_618",
            "reasons": [],
            "missing": [],
        }
        if template_enabled and near_reversal_level:
            template_info = evaluate_reversal_template_fit(
                direction,
                snapshot.get("features", {}) if snapshot else {},
                sharpness=sharpness,
                profile="golden_pocket",
                min_score=int(getattr(config, "FIBO_REVERSAL_TEMPLATE_MIN_SCORE", 4) or 4),
                min_spots=int(getattr(config, "FIBO_REVERSAL_TEMPLATE_MIN_SPOTS", 6) or 6),
                min_depth=int(getattr(config, "FIBO_REVERSAL_TEMPLATE_MIN_DEPTH", 24) or 24),
                allow_unavailable=not bool(getattr(config, "FIBO_REVERSAL_TEMPLATE_STRICT_REQUIRE_CAPTURE", False)),
            )
            if not bool(template_info.get("ok", True)):
                details.update({
                    "reversal_template_enabled": True,
                    "reversal_template_near_level": True,
                    "reversal_template_applied": bool(template_info.get("applied", False)),
                    "reversal_template_score": int(template_info.get("score", 0) or 0),
                    "reversal_template_min_score": int(template_info.get("min_score", 0) or 0),
                    "reversal_template_reason": str(template_info.get("reason") or ""),
                    "reversal_template_reasons": list(template_info.get("reasons") or []),
                    "reversal_template_missing": list(template_info.get("missing") or []),
                })
                return False, f"reversal_template:{template_info.get('reason') or 'failed'}", details
        details.update({
            "reversal_template_enabled": bool(template_enabled),
            "reversal_template_near_level": bool(near_reversal_level),
            "reversal_template_applied": bool(template_info.get("applied", False)),
            "reversal_template_score": int(template_info.get("score", 0) or 0),
            "reversal_template_min_score": int(template_info.get("min_score", 0) or 0),
            "reversal_template_reason": str(template_info.get("reason") or ""),
            "reversal_template_reasons": list(template_info.get("reasons") or []),
            "reversal_template_missing": list(template_info.get("missing") or []),
        })
        if correction_end_override and int(template_info.get("score", 0) or 0) < correction_end_min_template_score:
            details["correction_end_override"] = False
            return False, f"correction_end_template_weak:{int(template_info.get('score', 0) or 0)}<{correction_end_min_template_score}", details

        # Momentum quality score (must have enough confluence)
        score = 0
        reasons: list = []
        missing: list = []
        fib = getattr(fibo_ctx, "fib_levels", None)
        impulse_strength = float(getattr(fib, "impulse_strength", 0.0) or 0.0)
        min_impulse = float(_cfg(f"{prefix}_MIN_IMPULSE_STRENGTH_SCORE", 0.55 if mode != "scout" else 0.50))
        if impulse_strength >= min_impulse:
            score += 1
            reasons.append(f"impulse_strength:{impulse_strength:.2f}")
        else:
            missing.append(f"impulse_strength:{impulse_strength:.2f}<{min_impulse:.2f}")
        if bool(getattr(fibo_ctx, "impulse_confirmed", False)):
            score += 1
            reasons.append("impulse_confirmed")
        else:
            missing.append("impulse_not_confirmed")
        if in_gp:
            score += 1
            reasons.append("golden_pocket")
        if bool(getattr(fibo_ctx, "retracement_healthy", False)):
            score += 1
            reasons.append("retracement_healthy")
        else:
            missing.append("retracement_not_healthy")
        if bool(getattr(fibo_ctx, "volume_diminishing", False)):
            score += 1
            reasons.append("retracement_volume_contracting")
        else:
            missing.append("retracement_volume_not_contracting")
        if bool(mtf_stacking):
            score += 1
            reasons.append("mtf_fib_stack")
        if bool(template_info.get("applied")) and bool(template_info.get("ok", False)):
            score += 1
            reasons.append(
                f"reversal_template:{int(template_info.get('score', 0) or 0)}/{int(template_info.get('min_score', 0) or 0)}"
            )

        sharp = dict(sharpness or {})
        sharp_score = int(sharp.get("sharpness_score", 0) or 0)
        sharp_band = str(sharp.get("sharpness_band", "") or "").lower()
        if sharp_score >= 45 and sharp_band != "knife":
            score += 1
            reasons.append(f"sharpness:{sharp_score}:{sharp_band or 'unknown'}")
        else:
            missing.append(f"sharpness_weak:{sharp_score}:{sharp_band or 'unknown'}")

        try:
            if df_entry is not None and len(df_entry) >= 3 and atr > 0:
                close_now = float(df_entry["close"].iloc[-1])
                close_prev = float(df_entry["close"].iloc[-3])
                move_atr = (close_now - close_prev) / max(float(atr), 1e-8)
                if (direction == "long" and move_atr >= 0.02) or (direction == "short" and move_atr <= -0.02):
                    score += 1
                    reasons.append(f"entry_tf_momentum:{move_atr:.3f}atr")
                else:
                    missing.append(f"entry_tf_momentum_not_confirmed:{move_atr:.3f}atr")
                details["entry_tf_momentum_atr"] = round(move_atr, 4)
        except Exception:
            missing.append("entry_tf_momentum_unavailable")

        features = snapshot.get("features", {}) if snapshot else {}
        try:
            delta = float(features.get("delta_proxy", 0.0) or 0.0)
            imbalance = float(features.get("depth_imbalance", 0.0) or 0.0)
            drift = float(features.get("mid_drift_pct", 0.0) or 0.0)
            if direction == "long":
                support_parts = [delta >= 0.02, imbalance >= 0.02, drift >= 0.002]
            else:
                support_parts = [delta <= -0.02, imbalance <= -0.02, drift <= -0.002]
            support_count = sum(1 for ok in support_parts if ok)
            if support_count >= 1:
                score += 1
                reasons.append(f"micro_support:{support_count}/3")
            else:
                missing.append("micro_support_missing")
            details.update({
                "delta_proxy": round(delta, 4),
                "depth_imbalance": round(imbalance, 4),
                "mid_drift_pct": round(drift, 5),
                "micro_support_count": int(support_count),
            })
        except Exception:
            missing.append("micro_support_unavailable")

        min_score = max(1, int(_cfg(f"{prefix}_MIN_MOMENTUM_SCORE", 4)))
        details.update({
            "momentum_score": int(score),
            "min_momentum_score": int(min_score),
            "momentum_reasons": reasons,
            "momentum_missing": missing,
        })
        if score < min_score:
            return False, f"momentum_score:{score}/{min_score}|missing:{','.join(missing[:3])}", details
        return True, f"fib_quality_ok:{score}>={min_score}", details

    def _reject_wide_structure_stop(self, risk: float, atr: float, entry: float, *, scout: bool) -> Optional[str]:
        """Optional max |entry−SL| caps (Fib-only). Returns skip reason or None."""
        if entry <= 0 or atr <= 0 or risk <= 0:
            return None
        if scout:
            mult = float(_cfg("FIBO_SCOUT_MAX_RISK_ATR_MULT", 0.0))
            pct = float(_cfg("FIBO_SCOUT_MAX_RISK_ENTRY_PCT", 0.0))
        else:
            mult = float(_cfg("FIBO_ADVANCE_MAX_RISK_ATR_MULT", 0.0))
            pct = float(_cfg("FIBO_ADVANCE_MAX_RISK_ENTRY_PCT", 0.0))
        if mult > 0 and risk > atr * mult:
            return f"risk_cap_atr:{risk:.2f}>{atr * mult:.2f}"
        if pct > 0 and risk > abs(entry) * pct:
            return f"risk_cap_pct:{risk:.4f}>{abs(entry) * pct:.4f}"
        return None

    def _finalize_fib_tp_ladder(
        self,
        direction: str,
        entry: float,
        risk: float,
        tp1: float,
        tp2: float,
        tp3: float,
        *,
        scout: bool,
        min_rr_tp2: float,
    ) -> Optional[tuple]:
        """Cap TP1 distance in R; keep tp1,tp2,tp3 ordered; recompute RR from tp2.

        Returns (tp1, tp2, tp3, rr) or None if RR is below minimum.
        """
        if risk <= 0 or entry <= 0:
            return None
        max_r = float(_cfg("FIBO_SCOUT_TP1_MAX_R", 0.0) if scout else _cfg("FIBO_ADVANCE_TP1_MAX_R", 0.0))
        if max_r > 0:
            if direction == "long":
                tp1 = min(tp1, entry + risk * max_r)
            else:
                tp1 = max(tp1, entry - risk * max_r)
        step = max(risk * 0.05, 0.2)
        if direction == "long":
            if tp1 <= entry:
                tp1 = entry + step
            if tp2 <= tp1 + step:
                tp2 = tp1 + step
            if tp3 <= tp2 + step:
                tp3 = tp2 + step
        else:
            if tp1 >= entry:
                tp1 = entry - step
            if tp2 >= tp1 - step:
                tp2 = tp1 - step
            if tp3 >= tp2 - step:
                tp3 = tp2 - step
        rr = round(abs(tp2 - entry) / risk, 2) if risk > 0 else 0.0
        if rr < min_rr_tp2:
            return None
        return round(tp1, 2), round(tp2, 2), round(tp3, 2), rr

    # ── Birth-anchor override for early_origin SL/TP ─────────────────────────

    def _resolve_birth_override(self, fibo_ctx, direction: str,
                                current_price: float) -> Optional[dict]:
        """Return a dict of (fib, entry, sl_anchor, confidence) to use in
        place of the completed-swing fib when entry_mode=="early_origin"
        and the birth fib is valid; otherwise None (caller keeps the
        default late-retrace path).

        Gated by FIBO_IMPULSE_BIRTH_SLTP_ENABLED. Never raises.
        """
        try:
            if not bool(getattr(config, "FIBO_IMPULSE_BIRTH_SLTP_ENABLED", False)):
                return None
            if getattr(fibo_ctx, "entry_mode", "") != "early_origin":
                return None
            birth_fib = getattr(fibo_ctx, "impulse_birth_fib_levels", None)
            if birth_fib is None:
                return None
            conf = float(getattr(fibo_ctx, "impulse_birth_confidence", 0.0) or 0.0)
            min_conf = float(getattr(config, "FIBO_IMPULSE_BIRTH_SLTP_MIN_CONFIDENCE", 0.60))
            if conf < min_conf:
                return None
            birth_dir = str(getattr(fibo_ctx, "impulse_birth_direction", "") or "")
            dir_ok = (
                (direction == "long" and birth_dir == "bullish")
                or (direction == "short" and birth_dir == "bearish")
            )
            if not dir_ok:
                return None

            anchor_price = float(getattr(fibo_ctx, "impulse_birth_anchor_price", 0.0) or 0.0)
            if anchor_price <= 0:
                return None

            # Entry: a limit at the chosen birth-fib retracement level
            # (default 0.618 — golden pocket of the birth impulse). This
            # keeps the "enter the origin on a shallow pullback" logic
            # consistent with the rest of the fibo lane.
            entry_ratio = float(getattr(config, "FIBO_IMPULSE_BIRTH_ENTRY_RATIO", 0.618))
            entry_price = birth_fib.levels.get(entry_ratio, 0.0) or 0.0
            # Degenerate birth fib (range 0) → skip override.
            if entry_price <= 0 or birth_fib.swing_range <= 0:
                return None
            # If current_price has already blown past the entry ratio
            # (pullback never came) we still prefer market at current to
            # not miss the move; downstream RR check guards the trade.
            if direction == "long" and current_price < entry_price:
                entry_price = current_price
            if direction == "short" and current_price > entry_price:
                entry_price = current_price

            return {
                "fib": birth_fib,
                "entry": float(entry_price),
                "sl_anchor": float(anchor_price),
                "confidence": conf,
            }
        except Exception as exc:
            logger.debug("[FiboAdvance] birth override error: %s", exc)
            return None

    # ── Entry / SL / TP Construction ──────────────────────────────────────────

    def _build_signal(self, direction: str, fibo_ctx, current_price: float,
                      atr: float, rsi: float, session_info: dict,
                      smc_context, df_entry: pd.DataFrame,
                      vp_adj: float = 0.0, vp_reason: str = "",
                      sharpness: dict = None,
                      quality: dict = None,
                      mtf_bonus: float = 0.0, mtf_reason: str = "",
                      mode: str = "sniper") -> Optional[TradeSignal]:
        """
        Construct a TradeSignal for the Fibonacci sniper setup.
        Entry = limit at nearest Fibonacci level.
        SL    = beyond the 1.0 retracement (swing origin) with ATR buffer.
        TP    = Fibonacci extensions (1.0, 1.272, 1.618).
        """
        fib = fibo_ctx.fib_levels
        if fib is None:
            return None

        entry = fibo_ctx.nearest_level_price
        if entry <= 0:
            return None

        # ── Early-origin override: swap fib + SL anchor when birth is fresh ──
        birth_override = self._resolve_birth_override(fibo_ctx, direction, current_price)
        anchor_mode = "late_retrace"
        if birth_override is not None:
            fib = birth_override["fib"]
            entry = birth_override["entry"]
            sl_anchor = birth_override["sl_anchor"]
            sl_buffer = atr * float(_cfg("FIBO_IMPULSE_BIRTH_SL_ATR_BUFFER", 0.25))
            anchor_mode = "early_origin"
        else:
            # SL: place beyond swing start (the 100% level) with ATR buffer
            sl_buffer = atr * float(_cfg("FIBO_ADVANCE_SL_ATR_BUFFER", 0.25))
            sl_anchor = fib.levels.get(1.0, fib.swing_start)

        if direction == "long":
            stop_loss = sl_anchor - sl_buffer
        else:
            stop_loss = sl_anchor + sl_buffer

        risk = abs(entry - stop_loss)
        if risk < atr * 0.1:
            return None  # degenerate SL

        # ── Reject oversized structure stops ────────────────────────────
        _wide = self._reject_wide_structure_stop(risk, atr, entry, scout=(mode == "scout"))
        if _wide:
            logger.debug("[FiboAdvance:%s] skip wide stop: %s", mode, _wide)
            return None

        # TPs at Fibonacci extensions
        ext_1272 = fib.extensions.get(1.272, 0.0)
        ext_1618 = fib.extensions.get(1.618, 0.0)
        ext_200  = fib.extensions.get(2.0,   0.0)
        ext_100  = fib.extensions.get(1.0,   0.0)  # 1:1 swing target

        if direction == "long":
            tp1 = ext_100  if ext_100  > entry else entry + risk * 1.0
            tp2 = ext_1272 if ext_1272 > entry else entry + risk * 1.618
            tp3 = ext_1618 if ext_1618 > entry else entry + risk * 2.618
        else:
            tp1 = ext_100  if ext_100  < entry else entry - risk * 1.0
            tp2 = ext_1272 if ext_1272 < entry else entry - risk * 1.618
            tp3 = ext_1618 if ext_1618 < entry else entry - risk * 2.618

        # ── Validate and finalize TP ladder (enforces minimum RR) ───────
        _ladder = self._finalize_fib_tp_ladder(
            direction, entry, risk, tp1, tp2, tp3,
            scout=(mode == "scout"),
            min_rr_tp2=float(_cfg("FIBO_SCOUT_MIN_RR" if mode == "scout" else "FIBO_ADVANCE_MIN_RR", 1.0 if mode == "scout" else 1.2)),
        )
        if _ladder is None:
            return None
        tp1, tp2, tp3, rr = _ladder

        # ── Confidence = Fib confluence + SMC + RSI + VP + MTF stacking ───
        base_conf = fibo_ctx.fibo_confluence_score
        smc_boost = 0.0
        if smc_context:
            smc_boost = min(smc_context.confidence * 0.25, 15.0)

        rsi_boost = 0.0
        if direction == "long" and rsi < 40:
            rsi_boost = 6.0
        elif direction == "short" and rsi > 60:
            rsi_boost = 6.0

        # Uncapped base — cap is applied post-modifier in scan() so that
        # high-confluence signals retain their headroom to absorb downstream
        # trend/killer/session penalties. (FIBO_ADVANCE_MAX_CONFIDENCE=96.)
        confidence = round(base_conf + smc_boost + rsi_boost + vp_adj + mtf_bonus, 1)
        min_conf   = float(_cfg("FIBO_ADVANCE_MIN_CONFIDENCE", 62.0))
        # Pre-mod budget: trend/killer/session/cb modifiers are applied AFTER this
        # build. A borderline base that would pass with +5 aligned trend bonus
        # should survive the internal gate. Caller enforces the final threshold
        # implicitly via the same min_conf after modifiers.
        _pre_mod_budget = float(_cfg("FIBO_ADVANCE_PRE_MOD_BUDGET", 5.0))
        if confidence < (min_conf - _pre_mod_budget):
            return None

        # ── Sharpness-based risk adjustment ───────────────────────────────
        sharpness_info = sharpness or {}
        sharpness_band = str(sharpness_info.get("sharpness_band", "normal") or "normal")

        # Pattern label
        zone = "GoldenPocket" if fibo_ctx.in_golden_pocket else f"Fib{fibo_ctx.nearest_level_ratio:.3f}"
        wave = f"_EW{fibo_ctx.elliott_wave_count}" if fibo_ctx.elliott_wave_count > 0 else ""
        pattern = f"FIBO_{zone}{wave}"

        reasons  = list(fibo_ctx.reasons)
        warnings = list(fibo_ctx.warnings)
        quality_info = dict(quality or {})

        if vp_reason:
            reasons.append(vp_reason)
        if mtf_reason:
            reasons.append(mtf_reason)
        if bool(quality_info.get("reversal_template_applied", False)):
            template_reason = str(quality_info.get("reversal_template_reason") or "")
            reasons.append(f"reversal_template:{template_reason or 'confirmed'}")

        session_str = ",".join(session_info.get("active_sessions", []) or [])
        trend_str   = (smc_context.current_trend if smc_context else "ranging") or "ranging"

        return TradeSignal(
            symbol="XAUUSD",
            direction=direction,
            confidence=confidence,
            entry=round(entry, 2),
            stop_loss=round(stop_loss, 2),
            take_profit_1=round(tp1, 2),
            take_profit_2=round(tp2, 2),
            take_profit_3=round(tp3, 2),
            risk_reward=rr,
            timeframe="1h",
            session=session_str,
            trend=trend_str,
            rsi=round(rsi, 1),
            atr=round(atr, 2),
            pattern=pattern,
            reasons=reasons,
            warnings=warnings,
            smc_context=smc_context,
            raw_scores={
                "fibo_confluence": fibo_ctx.fibo_confluence_score,
                "smc_boost": smc_boost,
                "rsi_boost": rsi_boost,
                "vp_adjustment": vp_adj,
                "vp_reason": vp_reason,
                "mtf_stacking_bonus": mtf_bonus,
                "mtf_reason": mtf_reason,
                "retracement_depth": round(fibo_ctx.retracement_depth, 3),
                "impulse_strength": round(fib.impulse_strength, 3),
                "elliott_wave": fibo_ctx.elliott_wave_count,
                "wave_phase": str(getattr(fibo_ctx, "wave_phase", "unknown") or "unknown"),
                "wave_confidence": round(float(getattr(fibo_ctx, "wave_confidence", 0.0) or 0.0), 3),
                "correction_end_score": round(float(getattr(fibo_ctx, "correction_end_score", 0.0) or 0.0), 2),
                "correction_end_confirmed": bool(getattr(fibo_ctx, "correction_end_confirmed", False)),
                "sharpness_score": int(sharpness_info.get("sharpness_score", 0) or 0),
                "sharpness_band": sharpness_band,
                "fibo_reversal_template_applied": bool(quality_info.get("reversal_template_applied", False)),
                "fibo_reversal_template_score": int(quality_info.get("reversal_template_score", 0) or 0),
                "fibo_reversal_template_min_score": int(quality_info.get("reversal_template_min_score", 0) or 0),
                "fibo_reversal_template_near_618": bool(quality_info.get("reversal_template_near_level", False)),
                "fibo_reversal_template_reason": str(quality_info.get("reversal_template_reason") or ""),
                "fibo_entry_mode": anchor_mode,
                "impulse_birth_confidence": round(
                    float(getattr(fibo_ctx, "impulse_birth_confidence", 0.0) or 0.0), 3
                ),
                "impulse_birth_direction": str(
                    getattr(fibo_ctx, "impulse_birth_direction", "") or ""
                ),
                "impulse_age_bars": int(getattr(fibo_ctx, "impulse_age_bars", 0) or 0),
            },
            entry_type="limit",
            sl_type="structure",
            sl_reason=(
                f"early_origin:base_anchor_{sl_anchor:.2f} atr_buf:{sl_buffer:.1f}"
                if anchor_mode == "early_origin"
                else f"beyond_fib_100pct_swing_origin atr_buf:{sl_buffer:.1f}"
            ),
            tp_type="structure",
            tp_reason=(
                f"birth_extensions_1272_{tp2:.1f}_1618_{tp3:.1f}"
                if anchor_mode == "early_origin"
                else f"fibo_extensions_1272_{tp2:.1f}_1618_{tp3:.1f}"
            ),
            sl_liquidity_mapped=False,
            liquidity_pools_count=len(smc_context.liquidity_pools) if smc_context else 0,
        )

    # ── Scout Mode: H1 impulse → M15 Fibonacci entry ──────────────────────────

    def _scout_scan(self, df_h1: pd.DataFrame, df_m15: pd.DataFrame,
                    current_price: float, atr_h1: float, rsi: float,
                    session_info: dict, snapshot: dict,
                    smc_context, h4_bias: str,
                    d1_bias: str = "neutral") -> Optional[TradeSignal]:
        """
        Scout mode: catch intermediate Fibonacci setups on M15 while waiting
        for the big Sniper setup. Only fires in H4 bias direction.

        Institution-grade additions:
        - Impulse freshness gate
        - MTF Fib zone stacking (M15 ∩ H1)
        - Entry Sharpness Score
        - Volume Profile confluence
        """
        if h4_bias == "neutral":
            logger.debug("[FiboAdvance:Scout] H4 neutral — skipping scout")
            return None

        if df_m15 is None or df_m15.empty or len(df_m15) < 30:
            return None

        df_m15 = ta.add_atr(df_m15, period=14)
        atr_m15 = float(df_m15["atr_14"].iloc[-1]) if "atr_14" in df_m15.columns else atr_h1 * 0.4

        # Fibonacci on H1 as structure, M15 for precision
        fibo_ctx = fibo.analyze(
            df_structure=df_h1,
            df_entry=df_m15,
            current_price=current_price,
            atr=atr_m15,
            smc_context=smc_context,
        )

        if fibo_ctx.fib_levels is None:
            return None

        fib = fibo_ctx.fib_levels
        scout_direction = "long" if fib.direction == "bullish" else "short"

        # Scout must align with H4 bias — this is the key guard
        if scout_direction != h4_bias:
            logger.debug("[FiboAdvance:Scout] Direction %s conflicts with H4 bias %s",
                         scout_direction, h4_bias)
            return None

        # ── Impulse freshness gate ────────────────────────────────────────
        fresh_ok, fresh_reason = self._check_impulse_freshness(fib, df_m15, mode="scout")
        if not fresh_ok:
            logger.debug("[FiboAdvance:Scout] %s", fresh_reason)
            return None

        # Lower score threshold for scout (more opportunities)
        scout_min_score = float(_cfg("FIBO_SCOUT_MIN_FIBO_SCORE", 28.0))
        if fibo_ctx.fibo_confluence_score < scout_min_score:
            return None

        # Distance gate — scout needs to be closer to level (more precise)
        max_dist = float(_cfg("FIBO_SCOUT_MAX_LEVEL_DIST_PCT", 0.15))
        if fibo_ctx.nearest_level_dist_pct > max_dist:
            return None

        # ── MTF Fib zone stacking (M15 ∩ H1) ─────────────────────────────
        mtf_stacking, mtf_bonus, mtf_reason = self._check_mtf_fib_stacking(
            fibo_ctx.nearest_level_price, df_h1, atr_h1, current_price, smc_context
        )

        # ── Trend Alignment Gate — SYMMETRIC ────────────────────────────
        # Block counter-trend entries only when D1+H4 BOTH agree against us.
        # Neutral/mixed H4 allows both sides (opportunity-first).
        _ta_gate_enabled = bool(getattr(config, "FIBO_TREND_ALIGNMENT_GATE_ENABLED", True))
        if _ta_gate_enabled:
            if scout_direction == "long" and d1_bias == "short" and h4_bias == "short":
                logger.info("[FiboAdvance:Scout] blocked: trend_alignment_gate long vs D1+H4 bearish")
                return None
            if scout_direction == "short" and d1_bias == "long" and h4_bias == "long":
                logger.info("[FiboAdvance:Scout] blocked: trend_alignment_gate short vs D1+H4 bullish")
                return None

        # ── MTF stacking required gate ────────────────────────────────────
        if bool(getattr(config, "FIBO_SCOUT_REQUIRE_MTF_STACKING", True)) and not mtf_stacking:
            logger.debug("[FiboAdvance:Scout] blocked: mtf_stacking_required (%s)", mtf_reason)
            return None

        # ── Entry Sharpness Score ─────────────────────────────────────────
        sharp_ok, sharp_reason, sharpness = self._check_entry_sharpness(
            scout_direction, snapshot, mode="scout"
        )
        if not sharp_ok:
            logger.debug("[FiboAdvance:Scout] blocked: %s", sharp_reason)
            return None

        # ── Gate: Golden Pocket Quality (61.8 required) ──────────────────
        quality_ok, quality_reason, quality = self._fib_entry_quality_gate(
            scout_direction, fibo_ctx, snapshot, df_m15, atr_m15,
            mode="scout", sharpness=sharpness, mtf_stacking=mtf_stacking,
        )
        if not quality_ok:
            logger.info("[FiboAdvance:Scout] blocked: %s", quality_reason)
            return None

        # ── Volume Profile confluence ─────────────────────────────────────
        vp_adj, vp_reason, vp_check = self._check_volume_profile(
            fibo_ctx.nearest_level_price, scout_direction
        )

        # ── Microstructure gate ───────────────────────────────────────────
        micro_ok, micro_reason = self._check_microstructure(scout_direction, 65.0, snapshot)
        if not micro_ok:
            return None

        # Build signal with scout-specific targets (shorter, quicker)
        entry     = fibo_ctx.nearest_level_price
        # ── Early-origin override: swap fib + SL anchor when birth is fresh ──
        scout_birth_override = self._resolve_birth_override(
            fibo_ctx, scout_direction, current_price
        )
        scout_anchor_mode = "late_retrace"
        if scout_birth_override is not None:
            fib = scout_birth_override["fib"]
            entry = scout_birth_override["entry"]
            sl_anchor = scout_birth_override["sl_anchor"]
            sl_buffer = atr_m15 * float(_cfg("FIBO_IMPULSE_BIRTH_SL_ATR_BUFFER", 0.25))
            scout_anchor_mode = "early_origin"
        else:
            sl_buffer = atr_m15 * float(_cfg("FIBO_SCOUT_SL_ATR_BUFFER", 0.20))
            sl_anchor = fib.levels.get(1.0, fib.swing_start)

        if scout_direction == "long":
            stop_loss = sl_anchor - sl_buffer
        else:
            stop_loss = sl_anchor + sl_buffer

        risk = abs(entry - stop_loss)
        if risk < atr_m15 * 0.08:
            return None

        # ── Reject oversized structure stops ────────────────────────────
        _wide_sc = self._reject_wide_structure_stop(risk, atr_m15, entry, scout=True)
        if _wide_sc:
            logger.debug("[FiboAdvance:Scout] skip wide stop: %s", _wide_sc)
            return None

        # Scout TPs: 1.0 and 1.272 extension only (not waiting for 1.618)
        ext_100  = fib.extensions.get(1.0,   0.0)
        ext_1272 = fib.extensions.get(1.272, 0.0)
        ext_1618 = fib.extensions.get(1.618, 0.0)

        if scout_direction == "long":
            tp1 = ext_100  if ext_100  > entry else entry + risk * 0.8
            tp2 = ext_1272 if ext_1272 > entry else entry + risk * 1.272
            tp3 = ext_1618 if ext_1618 > entry else entry + risk * 1.618
        else:
            tp1 = ext_100  if ext_100  < entry else entry - risk * 0.8
            tp2 = ext_1272 if ext_1272 < entry else entry - risk * 1.272
            tp3 = ext_1618 if ext_1618 < entry else entry - risk * 1.618

        # ── Validate and finalize TP ladder (enforces minimum RR) ───────
        _ladder_sc = self._finalize_fib_tp_ladder(
            scout_direction, entry, risk, tp1, tp2, tp3,
            scout=True, min_rr_tp2=float(_cfg("FIBO_SCOUT_MIN_RR", 1.0)),
        )
        if _ladder_sc is None:
            return None
        tp1, tp2, tp3, rr = _ladder_sc

        smc_boost  = min(smc_context.confidence * 0.20, 10.0) if smc_context else 0.0
        # Uncapped base — cap is applied post-modifier in scan() so that
        # high-confluence scout setups retain headroom to absorb penalties.
        # (FIBO_SCOUT_MAX_CONFIDENCE=88.)
        confidence = round(
            fibo_ctx.fibo_confluence_score + smc_boost + 5.0 + vp_adj + mtf_bonus,
            1,
        )
        min_conf   = float(_cfg("FIBO_SCOUT_MIN_CONFIDENCE", 55.0))
        # Pre-mod budget: scout_trend_mod/killer/session modifiers are applied
        # by the caller AFTER this returns. Let borderline signals survive so
        # positive modifiers can rescue them; penalties remain non-blocking
        # by design (see _trend_confidence_modifier docstring).
        _scout_pre_mod_budget = float(_cfg("FIBO_SCOUT_PRE_MOD_BUDGET", 5.0))
        if confidence < (min_conf - _scout_pre_mod_budget):
            return None

        zone = "GP" if fibo_ctx.in_golden_pocket else f"F{fibo_ctx.nearest_level_ratio:.3f}"
        mtf_tag = "_MTF" if mtf_stacking else ""
        pattern = f"FIBO_SCOUT_{zone}_H4{h4_bias.upper()}{mtf_tag}"

        reasons  = [f"scout_h4_bias_{h4_bias}"] + list(fibo_ctx.reasons)
        warnings = list(fibo_ctx.warnings)

        if vp_reason:
            reasons.append(vp_reason)
        if mtf_reason:
            reasons.append(mtf_reason)
        reasons.append(fresh_reason)
        if bool(quality.get("reversal_template_applied", False)):
            reasons.append(
                f"reversal_template:{str(quality.get('reversal_template_reason') or 'confirmed')}"
            )

        session_str = ",".join(session_info.get("active_sessions", []) or [])
        trend_str   = (smc_context.current_trend if smc_context else h4_bias) or h4_bias

        sharpness_score = int(sharpness.get("sharpness_score", 0) or 0)
        sharpness_band  = str(sharpness.get("sharpness_band", "normal") or "normal")

        logger.info("[FiboAdvance:Scout] SIGNAL | %s | Conf:%.1f | Fib:%.3f | "
                    "Entry:%.2f | SL:%.2f | TP2:%.2f | RR:%.2f | "
                    "Sharpness:%d(%s) | MTF:%s | VP:%s",
                    scout_direction.upper(), confidence,
                    fibo_ctx.nearest_level_ratio,
                    entry, stop_loss, tp2, rr,
                    sharpness_score, sharpness_band,
                    mtf_stacking, vp_reason)

        return TradeSignal(
            symbol="XAUUSD",
            direction=scout_direction,
            confidence=confidence,
            entry=round(entry, 2),
            stop_loss=round(stop_loss, 2),
            take_profit_1=round(tp1, 2),
            take_profit_2=round(tp2, 2),
            take_profit_3=round(tp3, 2),
            risk_reward=rr,
            timeframe="15m",
            session=session_str,
            trend=trend_str,
            rsi=round(rsi, 1),
            atr=round(atr_m15, 2),
            pattern=pattern,
            reasons=reasons,
            warnings=warnings,
            smc_context=smc_context,
            raw_scores={
                "mode": "scout",
                "fibo_confluence": fibo_ctx.fibo_confluence_score,
                "h4_bias": h4_bias,
                "retracement_depth": round(fibo_ctx.retracement_depth, 3),
                "impulse_strength": round(fib.impulse_strength, 3),
                "wave_phase": str(getattr(fibo_ctx, "wave_phase", "unknown") or "unknown"),
                "wave_confidence": round(float(getattr(fibo_ctx, "wave_confidence", 0.0) or 0.0), 3),
                "correction_end_score": round(float(getattr(fibo_ctx, "correction_end_score", 0.0) or 0.0), 2),
                "correction_end_confirmed": bool(getattr(fibo_ctx, "correction_end_confirmed", False)),
                "vp_adjustment": vp_adj,
                "vp_reason": vp_reason,
                "mtf_stacking": mtf_stacking,
                "mtf_bonus": mtf_bonus,
                "mtf_reason": mtf_reason,
                "sharpness_score": sharpness_score,
                "sharpness_band": sharpness_band,
                "fibo_reversal_template_applied": bool(quality.get("reversal_template_applied", False)),
                "fibo_reversal_template_score": int(quality.get("reversal_template_score", 0) or 0),
                "fibo_reversal_template_min_score": int(quality.get("reversal_template_min_score", 0) or 0),
                "fibo_reversal_template_near_618": bool(quality.get("reversal_template_near_level", False)),
                "fibo_reversal_template_reason": str(quality.get("reversal_template_reason") or ""),
                "fibo_entry_mode": scout_anchor_mode,
                "impulse_birth_confidence": round(
                    float(getattr(fibo_ctx, "impulse_birth_confidence", 0.0) or 0.0), 3
                ),
                "impulse_birth_direction": str(
                    getattr(fibo_ctx, "impulse_birth_direction", "") or ""
                ),
                "impulse_age_bars": int(getattr(fibo_ctx, "impulse_age_bars", 0) or 0),
            },
            entry_type="limit",
            sl_type="structure",
            sl_reason=(
                f"scout_early_origin_base_anchor_{sl_anchor:.2f} atr_buf:{sl_buffer:.1f}"
                if scout_anchor_mode == "early_origin"
                else f"scout_fib100_origin atr_buf:{sl_buffer:.1f}"
            ),
            tp_type="structure",
            tp_reason=(
                f"scout_birth_ext_1272:{tp2:.1f}"
                if scout_anchor_mode == "early_origin"
                else f"scout_ext_1272:{tp2:.1f}"
            ),
            sl_liquidity_mapped=False,
            liquidity_pools_count=len(smc_context.liquidity_pools) if smc_context else 0,
        )

    # ── Session Confidence Modifier ────────────────────────────────────────

    @staticmethod
    def _session_confidence_modifier(active_sessions: set[str]) -> tuple[float, str]:
        """
        Compute session-based confidence modifier (weight, not gate).

        London/NY/overlap → no penalty (0.0)
        Asian session     → conf -8 to -12 (configurable via FIBO_ASIAN_CONF_PENALTY)
        Off hours         → conf -15
        """
        has_london_or_ny = bool(active_sessions.intersection({"london", "new_york"}))
        if has_london_or_ny:
            return 0.0, "london_ny"
        elif "off_hours" in active_sessions:
            return -15.0, "off_hours"
        else:
            penalty = float(_cfg("FIBO_ASIAN_CONF_PENALTY", -10.0))
            return penalty, "asian_session"

    @staticmethod
    def _session_direction_bias(direction: str, d1_bias: str,
                                active_sessions: set[str]) -> tuple[float, str]:
        """
        During low-liquidity sessions (Asian/off_hours), apply extra penalty
        for trading counter to D1 trend.

        Rationale: In thin liquidity, price tends to follow the dominant trend.
        Counter-trend setups during Asian session have much lower win rate.
        (April 7 disaster: bot kept buying in Asian/off-hours during D1 downtrend)

        Returns (confidence_modifier, reason).
        """
        has_london_or_ny = bool(active_sessions.intersection({"london", "new_york"}))
        if has_london_or_ny:
            return 0.0, "session_bias_nylon"

        # Low liquidity session — check if direction aligns with D1
        if d1_bias in ("long", "short") and d1_bias != direction:
            penalty = float(_cfg("FIBO_SESSION_DIRECTION_BIAS_PENALTY", -12.0))
            return penalty, f"low_liq_counter_d1:{d1_bias}"

        return 0.0, "session_bias_ok"

    # ── Main Scan ──────────────────────────────────────────────────────────────

    def scan(self) -> Optional[TradeSignal]:
        """
        Dual-speed Fibonacci Advance scan — Institution Grade.

        Priority order:
          1. SNIPER (H4+H1 confluence) — high confidence, large targets
          2. SCOUT  (H1+M15, H4-aligned) — intermediate, fires while waiting

        Both modes share the same Fibonacci Killer guards.
        Institution-grade gates: Entry Sharpness, Volume Profile, impulse
        freshness, retracement velocity, Elliott Wave validation, MTF stacking.
        """
        self.scan_count += 1
        session_info = session_manager.get_session_info()
        self._set_diag(
            status="scan_started",
            utc_time=str(session_info.get("utc_time", "-")),
            active_sessions=list(session_info.get("active_sessions", []) or []),
            unmet=[],
            notes=[],
        )

        logger.info("[FiboAdvance] Scan #%d | %s | Sessions: %s",
                    self.scan_count,
                    session_info.get("utc_time", "-"),
                    session_info.get("active_sessions", []))

        # Market hours gate
        if not bool(session_info.get("xauusd_market_open", True)):
            self._set_diag(status="market_closed", unmet=["market_closed"])
            return None

        # Session confidence modifier (weight, not gate)
        # London/NY → no penalty; Asian → conf -8 to -12; off_hours → conf -15
        active_sessions = set(session_info.get("active_sessions", []) or [])
        session_conf_mod, session_reason = self._session_confidence_modifier(active_sessions)
        if session_conf_mod < 0:
            logger.debug("[FiboAdvance] Session weight: %+.0f (%s)", session_conf_mod, session_reason)

        # ── Circuit breaker check (soft — only emergency blocks) ─────────────
        cb_ok, cb_reason, cb_conf_mod = self._check_circuit_breaker()
        if not cb_ok:
            self._set_diag(status="circuit_breaker", unmet=["circuit_breaker"],
                           notes=[cb_reason])
            logger.info("[FiboAdvance] CIRCUIT BREAKER: %s", cb_reason)
            return None

        # ── Fetch data (all timeframes) ────────────────────────────────────────
        df_h4  = xauusd_provider.fetch("4h",  bars=120)
        df_h1  = xauusd_provider.fetch("1h",  bars=200)
        df_m15 = xauusd_provider.fetch("15m", bars=160)
        df_m5  = xauusd_provider.fetch("5m",  bars=120)

        if df_h4 is None or df_h4.empty or df_h1 is None or df_h1.empty:
            self._set_diag(status="no_data", unmet=["h4_or_h1_data"])
            logger.warning("[FiboAdvance] Failed to fetch H4/H1 data")
            return None

        # ── Technical indicators ───────────────────────────────────────────────
        df_h4 = ta.add_atr(df_h4, period=14)
        df_h1 = ta.add_rsi(df_h1, period=14)
        df_h1 = ta.add_atr(df_h1, period=14)
        df_h1 = ta.add_ema(df_h1, periods=[21, 50, 200])

        current_price = float(xauusd_provider.get_current_price() or df_h1["close"].iloc[-1])
        atr_h1  = float(df_h1["atr_14"].iloc[-1]) if "atr_14" in df_h1.columns else 1.0
        rsi     = float(df_h1["rsi_14"].iloc[-1]) if "rsi_14" in df_h1.columns else 50.0

        logger.info("[FiboAdvance] Price: %.2f | ATR(H1): %.2f | RSI: %.1f",
                    current_price, atr_h1, rsi)

        # ── Get microstructure snapshot ────────────────────────────────────────
        snapshot: dict = {}
        try:
            for direction_probe in ("long", "short"):
                snap = autopilot.latest_capture_feature_snapshot(
                    symbol="XAUUSD",
                    direction=direction_probe,
                    confidence=70.0,
                )
                if snap:
                    snapshot = snap
                    break
        except Exception as e:
            logger.debug("[FiboAdvance] snapshot error: %s", e)

        # ── Fibonacci Killer check (before expensive analysis) ─────────────────
        # ── Fibonacci Killer check (weighted — not binary gate) ─────────────
        killer_allowed, killer_reason, killer_weight = self._fibonacci_killer_check(snapshot, atr_h1, df_h1)
        if not killer_allowed:
            self._set_diag(
                status="fibonacci_killer_blocked",
                killer_reason=killer_reason,
                unmet=["fibonacci_killer"],
                notes=[f"Fibonacci levels hard-blocked: {killer_reason}"],
            )
            logger.info("[FiboAdvance] KILLER HARD BLOCK — skipping: %s", killer_reason)
            return None
        if killer_weight < 0:
            logger.info("[FiboAdvance] KILLER WEIGHT: %s (%+.0f conf)", killer_reason, killer_weight)

        # ── SMC analysis ───────────────────────────────────────────────────────
        smc_context = None
        try:
            smc_context = smc.analyze(df_h1, current_price=current_price)
        except Exception as e:
            logger.debug("[FiboAdvance] smc error: %s", e)

        # ── H4 bias — structure-based (institution grade) ──────────────────────
        df_h4_ind = ta.add_atr(df_h4.copy(), period=14)
        atr_h4    = float(df_h4_ind["atr_14"].iloc[-1]) if "atr_14" in df_h4_ind.columns else atr_h1 * 2
        h4_bias   = self._get_h4_bias(df_h4, current_price, atr_h4)

        # Fetch D1 data for trend alignment gate
        df_d1 = xauusd_provider.fetch("1d", bars=60)

        # Determine D1 bias for session direction bias check
        d1_bias = "neutral"
        if df_d1 is not None and not df_d1.empty and len(df_d1) >= 50:
            try:
                df_d1c = ta.add_ema(df_d1.copy(), periods=[21, 50])
                d1_ema21 = float(df_d1c["ema_21"].iloc[-1])
                d1_ema50 = float(df_d1c["ema_50"].iloc[-1])
                d1_close = float(df_d1c["close"].iloc[-1])
                if d1_close > d1_ema21 > d1_ema50:
                    d1_bias = "long"
                elif d1_close < d1_ema21 < d1_ema50:
                    d1_bias = "short"
            except Exception:
                pass

        # ══ SNIPER MODE: H4 impulse → H1 Golden Pocket ═══════════════════════
        logger.debug("[FiboAdvance] Trying SNIPER mode (H4→H1)")
        fibo_ctx = fibo.analyze(
            df_structure=df_h4,
            df_entry=df_h1,
            current_price=current_price,
            atr=atr_h1,
            smc_context=smc_context,
        )

        sniper_fired = False
        if fibo_ctx.fib_levels is not None:
            min_fibo_score = float(_cfg("FIBO_ADVANCE_MIN_FIBO_SCORE", 45.0))
            fib            = fibo_ctx.fib_levels
            direction      = "long" if fib.direction == "bullish" else "short"
            smc_aligned    = not smc_context or smc_context.bias in ("neutral", direction)
            dist_ok        = fibo_ctx.nearest_level_dist_pct <= float(_cfg("FIBO_ADVANCE_MAX_LEVEL_DIST_PCT", 0.25))
            score_ok       = fibo_ctx.fibo_confluence_score >= min_fibo_score

            if score_ok and smc_aligned and dist_ok:
                # ── Trend Alignment Gate (replaces crude short quarantine) ─
                # Block counter-trend entries when D1+H4 agree on direction.
                _ta_gate = bool(getattr(config, "FIBO_TREND_ALIGNMENT_GATE_ENABLED", True))
                _ta_blocked = False
                if _ta_gate:
                    # Symmetric: block only when D1+H4 BOTH agree against direction.
                    # Neutral/mixed H4 allows both sides (opportunity-first).
                    if direction == "long" and d1_bias == "short" and h4_bias == "short":
                        logger.info("[FiboAdvance:Sniper] blocked: trend_alignment_gate long vs D1+H4 bearish")
                        _ta_blocked = True
                    elif direction == "short" and d1_bias == "long" and h4_bias == "long":
                        logger.info("[FiboAdvance:Sniper] blocked: trend_alignment_gate short vs D1+H4 bullish")
                        _ta_blocked = True
                if not _ta_blocked:
                    # ── Gate: Impulse freshness ────────────────────────────
                    fresh_ok, fresh_reason = self._check_impulse_freshness(fib, df_h1, mode="sniper")
                    if not fresh_ok:
                        logger.debug("[FiboAdvance:Sniper] %s", fresh_reason)
                    else:
                        # ── Trend confidence modifier (D1 + H4) ───────────
                        trend_mod, trend_reason = self._trend_confidence_modifier(df_d1, df_h4, direction)
                        if trend_mod != 0:
                            logger.info("[FiboAdvance:Sniper] trend %s: %+.0f conf", trend_reason, trend_mod)
                        # ── Gate: Entry Sharpness Score ────────────────────
                        sharp_ok, sharp_reason, sharpness = self._check_entry_sharpness(
                            direction, snapshot, mode="sniper"
                        )
                        if not sharp_ok:
                            logger.info("[FiboAdvance:Sniper] blocked: %s", sharp_reason)
                        else:
                            # ── Gate: Golden Pocket Quality (61.8 required) ─
                            quality_ok, quality_reason, quality = self._fib_entry_quality_gate(
                                direction, fibo_ctx, snapshot, df_h1, atr_h1,
                                mode="sniper", sharpness=sharpness,
                            )
                            if not quality_ok:
                                logger.info("[FiboAdvance:Sniper] blocked: %s", quality_reason)
                            else:
                                # ── Gate: Volume Profile confluence ──────────
                                vp_adj, vp_reason, vp_check = self._check_volume_profile(
                                    fibo_ctx.nearest_level_price, direction
                                )
                                # ── Gate: Microstructure ─────────────────────
                                micro_ok, micro_reason = self._check_microstructure(direction, 70.0, snapshot)
                                if micro_ok:
                                    signal = self._build_signal(
                                        direction=direction,
                                        fibo_ctx=fibo_ctx,
                                        current_price=current_price,
                                        atr=atr_h1,
                                        rsi=rsi,
                                        session_info=session_info,
                                        smc_context=smc_context,
                                        df_entry=df_h1,
                                        vp_adj=vp_adj,
                                        vp_reason=vp_reason,
                                        sharpness=sharpness,
                                        quality=quality,
                                        mode="sniper",
                                    )
                                    if signal is not None:
                                        sess_dir_bias, sess_dir_reason = self._session_direction_bias(
                                            direction, d1_bias, active_sessions)
                                        # Post-modifier: floor at 10 (display safety),
                                        # cap at FIBO_ADVANCE_MAX_CONFIDENCE (design ceiling).
                                        _sniper_max_conf = float(_cfg("FIBO_ADVANCE_MAX_CONFIDENCE", 96.0))
                                        signal.confidence = round(
                                            min(
                                                max(
                                                    signal.confidence + trend_mod + cb_conf_mod + killer_weight + session_conf_mod + sess_dir_bias,
                                                    10.0,
                                                ),
                                                _sniper_max_conf,
                                            ),
                                            1,
                                        )
                                        signal.pattern = signal.pattern.replace("FIBO_", "FIBO_SNIPER_")
                                        signal.raw_scores["mode"] = "sniper"
                                        signal.raw_scores["trend_mod"] = trend_mod
                                        signal.raw_scores["trend_reason"] = trend_reason
                                        signal.raw_scores["cb_conf_mod"] = cb_conf_mod
                                        signal.raw_scores["killer_weight"] = killer_weight
                                        signal.raw_scores["killer_reason"] = killer_reason
                                        signal.raw_scores["session_weight"] = session_conf_mod
                                        signal.raw_scores["session_reason"] = session_reason
                                        signal.raw_scores["session_dir_bias"] = sess_dir_bias
                                        signal.raw_scores["session_dir_reason"] = sess_dir_reason
                                        signal.raw_scores["fib_quality"] = quality
                                        signal.reasons.append(fresh_reason)
                                        sniper_fired = True
                                        self.signal_count += 1
                                        self.last_signal = signal
                                        self._set_diag(
                                            status="sniper_signal_generated",
                                            mode="sniper",
                                            direction=direction,
                                            confidence=signal.confidence,
                                            entry=signal.entry,
                                            stop_loss=signal.stop_loss,
                                            fib_level=fibo_ctx.nearest_level_ratio,
                                            fib_score=fibo_ctx.fibo_confluence_score,
                                            in_golden_pocket=fibo_ctx.in_golden_pocket,
                                            impulse_strength=fib.impulse_strength,
                                            killer_weight=killer_weight,
                                            killer_reason=killer_reason,
                                            sharpness_score=int(sharpness.get("sharpness_score", 0) or 0),
                                            sharpness_band=str(sharpness.get("sharpness_band", "") or ""),
                                            vp_reason=vp_reason,
                                            quality_reason=quality_reason,
                                            notes=signal.reasons,
                                        )
                                        logger.info(
                                            "[FiboAdvance:Sniper] SIGNAL #%d | %s | Conf:%.1f | "
                                            "Fib:%.3f | GP:%s | Entry:%.2f | SL:%.2f | TP2:%.2f | RR:%.2f | "
                                            "Sharpness:%d(%s) | VP:%s | Quality:%s",
                                            self.signal_count, direction.upper(), signal.confidence,
                                            fibo_ctx.nearest_level_ratio, fibo_ctx.in_golden_pocket,
                                            signal.entry, signal.stop_loss, signal.take_profit_2, signal.risk_reward,
                                            int(sharpness.get("sharpness_score", 0) or 0),
                                            str(sharpness.get("sharpness_band", "") or ""),
                                            vp_reason, quality_reason,
                                        )
                                        return signal

        # ══ SCOUT MODE: H1 impulse → M15 entry (fires while waiting for Sniper) ══
        if not sniper_fired and bool(_cfg("FIBO_SCOUT_ENABLED", True)):
            # Soft penalty when Sniper has consecutive losses (don't hard-block)
            scout_conf_penalty = 0.0
            min_consec_warn = int(_cfg("FIBO_SCOUT_CONSEC_LOSS_WARN", 2))
            if self._consecutive_losses >= min_consec_warn:
                scout_conf_penalty = -10.0 * (self._consecutive_losses - min_consec_warn + 1)
                logger.debug("[FiboAdvance] Scout conf penalty: %.0f (%d consec losses)",
                             scout_conf_penalty, self._consecutive_losses)
            logger.debug("[FiboAdvance] Sniper not ready — trying SCOUT mode (H1→M15)")
            scout_signal = self._scout_scan(
                df_h1=df_h1,
                df_m15=df_m15,
                current_price=current_price,
                atr_h1=atr_h1,
                rsi=rsi,
                session_info=session_info,
                snapshot=snapshot,
                smc_context=smc_context,
                h4_bias=h4_bias,
                d1_bias=d1_bias,
            )
            if scout_signal is not None:
                # Trend modifier for scout direction
                scout_trend_mod, scout_trend_reason = self._trend_confidence_modifier(
                    df_d1, df_h4, scout_signal.direction)
                # Session direction bias for scout too
                scout_sess_bias, scout_sess_reason = self._session_direction_bias(
                    scout_signal.direction, d1_bias, active_sessions)
                # Apply soft penalty for consecutive losses
                # Apply scout conf penalty + trend + killer weight + session + direction bias (all weight)
                total_scout_mod = (scout_conf_penalty + scout_trend_mod + killer_weight
                                   + session_conf_mod + scout_sess_bias + cb_conf_mod)
                # Scout design: apply penalties only (positive mods discarded to
                # prevent inflating a lower-conviction class).
                if total_scout_mod < 0:
                    scout_signal.confidence = round(max(scout_signal.confidence + total_scout_mod, 10.0), 1)
                    if scout_conf_penalty < 0:
                        scout_signal.reasons.append(f"scout_conf_penalty:{scout_conf_penalty:.0f}")
                    if scout_trend_mod < 0:
                        scout_signal.reasons.append(f"trend:{scout_trend_reason}")
                    if killer_weight < 0:
                        scout_signal.reasons.append(f"killer_weight:{killer_weight:.0f}")
                    if session_conf_mod < 0:
                        scout_signal.reasons.append(f"session_weight:{session_conf_mod:.0f}")
                    if scout_sess_bias < 0:
                        scout_signal.reasons.append(f"session_dir_bias:{scout_sess_bias:.0f}")
                    if cb_conf_mod < 0:
                        scout_signal.reasons.append(f"circuit_breaker:{cb_conf_mod:.0f}")
                # Apply scout ceiling regardless — the base may exceed 88 since
                # the internal cap was removed to preserve penalty-absorption
                # headroom (fix: move cap to post-modifier stage).
                _scout_max_conf = float(_cfg("FIBO_SCOUT_MAX_CONFIDENCE", 88.0))
                scout_signal.confidence = round(min(scout_signal.confidence, _scout_max_conf), 1)
                # Add all modifiers to scout raw_scores
                scout_signal.raw_scores["trend_mod"] = scout_trend_mod
                scout_signal.raw_scores["trend_reason"] = scout_trend_reason
                scout_signal.raw_scores["killer_weight"] = killer_weight
                scout_signal.raw_scores["killer_reason"] = killer_reason
                scout_signal.raw_scores["session_weight"] = session_conf_mod
                scout_signal.raw_scores["session_reason"] = session_reason
                scout_signal.raw_scores["session_dir_bias"] = scout_sess_bias
                scout_signal.raw_scores["session_dir_reason"] = scout_sess_reason
                scout_signal.raw_scores["cb_conf_mod"] = cb_conf_mod
                self.signal_count += 1
                self.last_signal = scout_signal
                self._set_diag(
                    status="scout_signal_generated",
                    mode="scout",
                    h4_bias=h4_bias,
                    direction=scout_signal.direction,
                    confidence=scout_signal.confidence,
                    entry=scout_signal.entry,
                    sharpness_score=int(scout_signal.raw_scores.get("sharpness_score", 0) or 0),
                    sharpness_band=str(scout_signal.raw_scores.get("sharpness_band", "") or ""),
                    mtf_stacking=bool(scout_signal.raw_scores.get("mtf_stacking", False)),
                    vp_reason=str(scout_signal.raw_scores.get("vp_reason", "") or ""),
                    notes=scout_signal.reasons,
                )
                return scout_signal

        self._set_diag(
            status="no_signal",
            h4_bias=h4_bias,
            sniper_fired=sniper_fired,
            notes=["No Sniper or Scout setup found this scan"],
        )
        return None
