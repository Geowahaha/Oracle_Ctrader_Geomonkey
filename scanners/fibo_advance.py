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
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from market.data_fetcher import xauusd_provider, session_manager
from analysis.technical import TechnicalAnalysis
from analysis.smc import SMCAnalyzer
from analysis.signals import SignalGenerator, TradeSignal
from analysis.fibonacci import FibonacciAnalyzer
from config import config
from learning.live_profile_autopilot import LiveProfileAutopilot

logger = logging.getLogger(__name__)

ta   = TechnicalAnalysis()
smc  = SMCAnalyzer()
sig  = SignalGenerator(min_confidence=config.MIN_SIGNAL_CONFIDENCE)
fibo = FibonacciAnalyzer(
    swing_lookback=int(getattr(config, "FIBO_ADVANCE_SWING_LOOKBACK", 5) or 5),
    min_impulse_atr_mult=float(getattr(config, "FIBO_ADVANCE_MIN_IMPULSE_ATR", 1.2) or 1.2),
)
autopilot = LiveProfileAutopilot()

# ── Config shortcuts (with safe fallbacks) ────────────────────────────────────
def _cfg(key: str, default):
    return type(default)(getattr(config, key, default) or default)


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

    def get_last_scan_diagnostics(self) -> dict:
        return dict(self._last_scan_diagnostics or {})

    def _set_diag(self, **kwargs) -> None:
        try:
            self._last_scan_diagnostics = dict(kwargs)
        except Exception:
            self._last_scan_diagnostics = {}

    # ── Fibonacci Killer Detection ─────────────────────────────────────────────

    def _fibonacci_killer_check(self, snapshot: dict, atr: float,
                                df_entry: pd.DataFrame) -> tuple[bool, str]:
        """
        Detect conditions that invalidate Fibonacci levels ("Fibonacci killers").
        Returns (is_killer, reason).

        Killers identified from years of XAUUSD observation:
        1. ATR expansion: current bar ATR >> rolling average → trending/news day
        2. Strong delta: momentum too strong for retracement to hold
        3. Volume spike: anomalous volume = institutional repricing event
        4. Day type: panic_spread / fast_expansion / repricing
        5. State label: continuation_drive (price will not reverse at Fibo)
        6. Spread expansion: wide spread = low liquidity at level
        7. Retracement velocity: price falling too fast through Fib levels = stop hunt
        """
        features = snapshot.get("features", {}) if snapshot else {}

        # ── 1. ATR expansion guard ────────────────────────────────────────────
        if len(df_entry) >= 20 and atr > 0:
            recent_atr = float(df_entry["atr_14"].iloc[-1]) if "atr_14" in df_entry.columns else atr
            avg_atr    = float(df_entry["atr_14"].rolling(20).mean().iloc[-1]) if "atr_14" in df_entry.columns else atr
            if avg_atr > 0 and recent_atr > avg_atr * float(_cfg("FIBO_ADVANCE_KILLER_ATR_MULT", 1.8)):
                return True, f"atr_expansion_killer:{recent_atr:.2f}vs{avg_atr:.2f}"

        # ── 2. Delta proxy: too much momentum in one direction ────────────────
        delta = float(features.get("delta_proxy", 0.0))
        delta_kill = float(_cfg("FIBO_ADVANCE_KILLER_DELTA_THRESHOLD", 0.40))
        if abs(delta) > delta_kill:
            return True, f"delta_momentum_killer:{delta:.3f}"

        # ── 3. Volume spike: > 2.5x normal = institutional repricing ─────────
        bar_vol   = float(features.get("bar_volume_proxy", 0.0))
        vol_spike = float(_cfg("FIBO_ADVANCE_KILLER_VOL_SPIKE", 2.5))
        if bar_vol > vol_spike:
            return True, f"volume_spike_killer:{bar_vol:.2f}"

        # ── 4. Day type check ─────────────────────────────────────────────────
        day_type    = str(snapshot.get("day_type", "") or "")
        state_label = str(snapshot.get("state_label", "") or "")
        if day_type in self.KILLER_DAY_TYPES:
            return True, f"day_type_killer:{day_type}"
        if state_label in self.KILLER_STATE_LABELS:
            return True, f"state_label_killer:{state_label}"

        # ── 5. Spread expansion: wide spread = low liquidity at level ─────────
        spread_expansion = float(features.get("spread_expansion_ratio", 1.0))
        max_spread_exp   = float(_cfg("FIBO_ADVANCE_KILLER_MAX_SPREAD_EXP", 1.25))
        if spread_expansion > max_spread_exp:
            return True, f"spread_expansion_killer:{spread_expansion:.2f}"

        # ── 6. Retracement velocity: price dropping too fast through levels ───
        if len(df_entry) >= 5 and "atr_14" in df_entry.columns:
            retrace_vel = self._retracement_velocity(df_entry, atr)
            vel_kill = float(_cfg("FIBO_ADVANCE_KILLER_RETRACE_VEL", 2.0))
            if retrace_vel > vel_kill:
                return True, f"retrace_velocity_killer:{retrace_vel:.2f}x_atr"

        return False, "no_killer"

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
        features = snapshot.get("features", {}) if snapshot else {}
        delta       = float(features.get("delta_proxy", 0.0))
        imbalance   = float(features.get("depth_imbalance", 0.0))
        tick_vel    = float(features.get("bar_volume_proxy", 0.0))

        delta_thr = float(_cfg("FIBO_ADVANCE_MICRO_DELTA_THR", 0.30))
        imb_thr   = float(_cfg("FIBO_ADVANCE_MICRO_IMB_THR",   0.35))
        vel_thr   = float(_cfg("FIBO_ADVANCE_MICRO_VEL_THR",   0.05))

        if tick_vel < vel_thr:
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
            if bool(getattr(config, "FIBO_REQUIRE_CAPTURE_FEATURES", True)):
                return False, "capture_features_required", {"sharpness_score": 0, "sharpness_band": "blocked"}
            return True, "no_features_available", {"sharpness_score": 50, "sharpness_band": "normal"}

        try:
            from analysis.entry_sharpness import compute_entry_sharpness_score
            sharpness = compute_entry_sharpness_score(features, direction)
        except Exception as e:
            logger.debug("[FiboAdvance] sharpness error: %s", e)
            return True, "sharpness_error_passthrough", {"sharpness_score": 50, "sharpness_band": "normal"}

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

    def _fib_entry_quality_gate(
        self,
        direction: str,
        fibo_ctx,
        snapshot: dict,
        df_entry: pd.DataFrame,
        atr: float,
        *,
        mode: str,
        sharpness: dict | None = None,
        mtf_stacking: bool = False,
    ) -> tuple[bool, str, dict]:
        """Require golden-pocket depth plus enough directional evidence before a Fib signal."""
        prefix = "FIBO_SCOUT" if mode == "scout" else "FIBO_ADVANCE"
        ratio = float(getattr(fibo_ctx, "nearest_level_ratio", 0.0) or 0.0)
        depth = float(getattr(fibo_ctx, "retracement_depth", 0.0) or 0.0)
        tol = max(0.0, float(_cfg("FIBO_GOLDEN_GATE_TOLERANCE", 0.012)))
        min_ratio = float(_cfg(f"{prefix}_MIN_ENTRY_LEVEL_RATIO", 0.618))
        min_depth = float(_cfg(f"{prefix}_MIN_RETRACEMENT_DEPTH", 0.618))
        max_depth = float(_cfg(f"{prefix}_MAX_RETRACEMENT_DEPTH", 0.786))
        require_gp = bool(getattr(config, f"{prefix}_REQUIRE_GOLDEN_POCKET", True))
        in_gp = bool(getattr(fibo_ctx, "in_golden_pocket", False))
        details = {
            "mode": mode,
            "direction": str(direction or ""),
            "nearest_level_ratio": round(ratio, 4),
            "retracement_depth": round(depth, 4),
            "in_golden_pocket": in_gp,
            "min_entry_level_ratio": round(min_ratio, 4),
            "min_retracement_depth": round(min_depth, 4),
            "max_retracement_depth": round(max_depth, 4),
        }

        if ratio + tol < min_ratio:
            return False, f"pre_golden_level:{ratio:.3f}<{min_ratio:.3f}", details
        if depth + tol < min_depth:
            return False, f"pre_golden_depth:{depth:.3f}<{min_depth:.3f}", details
        if max_depth > 0 and depth - tol > max_depth:
            return False, f"overdeep_retracement:{depth:.3f}>{max_depth:.3f}", details
        if require_gp and not in_gp:
            return False, f"not_golden_pocket:ratio={ratio:.3f}:depth={depth:.3f}", details

        score = 0
        reasons: list[str] = []
        missing: list[str] = []
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
            details.update(
                {
                    "delta_proxy": round(delta, 4),
                    "depth_imbalance": round(imbalance, 4),
                    "mid_drift_pct": round(drift, 5),
                    "micro_support_count": int(support_count),
                }
            )
        except Exception:
            missing.append("micro_support_unavailable")

        min_score = max(1, int(_cfg(f"{prefix}_MIN_MOMENTUM_SCORE", 4)))
        details.update(
            {
                "momentum_score": int(score),
                "min_momentum_score": int(min_score),
                "momentum_reasons": reasons,
                "momentum_missing": missing,
            }
        )
        if score < min_score:
            return False, f"momentum_score_low:{score}<{min_score}", details
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
    ) -> Optional[tuple[float, float, float, float]]:
        """Cap TP1 distance in R; keep tp1,tp2,tp3 ordered; recompute RR from tp2."""
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

        # SL: place beyond swing start (the 100% level) with ATR buffer
        sl_buffer  = atr * float(_cfg("FIBO_ADVANCE_SL_ATR_BUFFER", 0.25))
        fib_100    = fib.levels.get(1.0, fib.swing_start)

        if direction == "long":
            stop_loss = fib_100 - sl_buffer
        else:
            stop_loss = fib_100 + sl_buffer

        risk = abs(entry - stop_loss)
        if risk < atr * 0.1:
            return None  # degenerate SL
        _wide = self._reject_wide_structure_stop(risk, atr, entry, scout=False)
        if _wide:
            logger.debug("[FiboAdvance:Sniper] skip %s", _wide)
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

        _ladder = self._finalize_fib_tp_ladder(
            direction, entry, risk, tp1, tp2, tp3, scout=False, min_rr_tp2=float(_cfg("FIBO_ADVANCE_MIN_RR", 1.2))
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

        confidence = round(min(base_conf + smc_boost + rsi_boost + vp_adj + mtf_bonus, 96.0), 1)
        min_conf   = float(_cfg("FIBO_ADVANCE_MIN_CONFIDENCE", 62.0))
        if confidence < min_conf:
            return None

        # ── Sharpness-based risk adjustment ───────────────────────────────
        sharpness_info = sharpness or {}
        sharpness_band = str(sharpness_info.get("sharpness_band", "normal") or "normal")
        quality_info = dict(quality or {})

        # Pattern label
        zone = "GoldenPocket" if fibo_ctx.in_golden_pocket else f"Fib{fibo_ctx.nearest_level_ratio:.3f}"
        wave = f"_EW{fibo_ctx.elliott_wave_count}" if fibo_ctx.elliott_wave_count > 0 else ""
        pattern = f"FIBO_{zone}{wave}"

        reasons  = list(fibo_ctx.reasons)
        warnings = list(fibo_ctx.warnings)

        if vp_reason:
            reasons.append(vp_reason)
        if mtf_reason:
            reasons.append(mtf_reason)

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
                "sharpness_score": int(sharpness_info.get("sharpness_score", 0) or 0),
                "sharpness_band": sharpness_band,
                "fib_entry_quality_score": int(quality_info.get("momentum_score", 0) or 0),
                "fib_entry_quality": quality_info,
                "ctrader_risk_usd_override": float(_cfg("FIBO_ADVANCE_CTRADER_RISK_USD", 1.0)),
            },
            entry_type="limit",
            sl_type="structure",
            sl_reason=f"beyond_fib_100pct_swing_origin atr_buf:{sl_buffer:.1f}",
            tp_type="structure",
            tp_reason=f"fibo_extensions_1272_{tp2:.1f}_1618_{tp3:.1f}",
            sl_liquidity_mapped=False,
            liquidity_pools_count=len(smc_context.liquidity_pools) if smc_context else 0,
        )

    # ── Scout Mode: H1 impulse → M15 Fibonacci entry ──────────────────────────

    def _scout_scan(self, df_h1: pd.DataFrame, df_m15: pd.DataFrame,
                    current_price: float, atr_h1: float, rsi: float,
                    session_info: dict, snapshot: dict,
                    smc_context, h4_bias: str) -> Optional[TradeSignal]:
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
        if scout_direction == "short" and bool(getattr(config, "FIBO_ADVANCE_SHORT_QUARANTINE_ENABLED", True)):
            logger.info("[FiboAdvance:Scout] blocked: fibo_xauusd short quarantine")
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
        if bool(getattr(config, "FIBO_SCOUT_REQUIRE_MTF_STACKING", True)) and not mtf_stacking:
            logger.debug("[FiboAdvance:Scout] blocked: %s", mtf_reason)
            return None

        # ── Entry Sharpness Score ─────────────────────────────────────────
        sharp_ok, sharp_reason, sharpness = self._check_entry_sharpness(
            scout_direction, snapshot, mode="scout"
        )
        if not sharp_ok:
            logger.debug("[FiboAdvance:Scout] blocked: %s", sharp_reason)
            return None

        # ── Volume Profile confluence ─────────────────────────────────────
        vp_adj, vp_reason, vp_check = self._check_volume_profile(
            fibo_ctx.nearest_level_price, scout_direction
        )

        # ── Microstructure gate ───────────────────────────────────────────
        micro_ok, micro_reason = self._check_microstructure(scout_direction, 65.0, snapshot)
        if not micro_ok:
            return None

        quality_ok, quality_reason, quality = self._fib_entry_quality_gate(
            scout_direction,
            fibo_ctx,
            snapshot,
            df_m15,
            atr_m15,
            mode="scout",
            sharpness=sharpness,
            mtf_stacking=mtf_stacking,
        )
        if not quality_ok:
            logger.info("[FiboAdvance:Scout] blocked: %s", quality_reason)
            return None

        # Build signal with scout-specific targets (shorter, quicker)
        entry     = fibo_ctx.nearest_level_price
        sl_buffer = atr_m15 * float(_cfg("FIBO_SCOUT_SL_ATR_BUFFER", 0.20))
        fib_100   = fib.levels.get(1.0, fib.swing_start)

        if scout_direction == "long":
            stop_loss = fib_100 - sl_buffer
        else:
            stop_loss = fib_100 + sl_buffer

        risk = abs(entry - stop_loss)
        if risk < atr_m15 * 0.08:
            return None
        _wide_sc = self._reject_wide_structure_stop(risk, atr_m15, entry, scout=True)
        if _wide_sc:
            logger.debug("[FiboAdvance:Scout] skip %s", _wide_sc)
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

        _ladder_sc = self._finalize_fib_tp_ladder(
            scout_direction, entry, risk, tp1, tp2, tp3, scout=True, min_rr_tp2=float(_cfg("FIBO_SCOUT_MIN_RR", 1.0))
        )
        if _ladder_sc is None:
            return None
        tp1, tp2, tp3, rr = _ladder_sc

        smc_boost  = min(smc_context.confidence * 0.20, 10.0) if smc_context else 0.0
        confidence = round(min(
            fibo_ctx.fibo_confluence_score + smc_boost + 5.0 + vp_adj + mtf_bonus,
            88.0,
        ), 1)
        min_conf   = float(_cfg("FIBO_SCOUT_MIN_CONFIDENCE", 55.0))
        if confidence < min_conf:
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
                "vp_adjustment": vp_adj,
                "vp_reason": vp_reason,
                "mtf_stacking": mtf_stacking,
                "mtf_bonus": mtf_bonus,
                "mtf_reason": mtf_reason,
                "sharpness_score": sharpness_score,
                "sharpness_band": sharpness_band,
                "fib_entry_quality_score": int(quality.get("momentum_score", 0) or 0),
                "fib_entry_quality": dict(quality or {}),
                "ctrader_risk_usd_override": float(_cfg("FIBO_SCOUT_CTRADER_RISK_USD", 0.5)),
            },
            entry_type="limit",
            sl_type="structure",
            sl_reason=f"scout_fib100_origin atr_buf:{sl_buffer:.1f}",
            tp_type="structure",
            tp_reason=f"scout_ext_1272:{tp2:.1f}",
            sl_liquidity_mapped=False,
            liquidity_pools_count=len(smc_context.liquidity_pools) if smc_context else 0,
        )

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

        # Session gate: London and NY only
        active_sessions = set(session_info.get("active_sessions", []) or [])
        if not active_sessions.intersection({"london", "new_york"}):
            self._set_diag(status="session_skip", unmet=["non_london_ny_session"])
            logger.debug("[FiboAdvance] Skipping — outside London/NY session")
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
        is_killer, killer_reason = self._fibonacci_killer_check(snapshot, atr_h1, df_h1)
        if is_killer:
            self._set_diag(
                status="fibonacci_killer_blocked",
                killer_reason=killer_reason,
                unmet=["fibonacci_killer"],
                notes=[f"Fibonacci levels not safe: {killer_reason}"],
            )
            logger.info("[FiboAdvance] KILLER DETECTED — skipping: %s", killer_reason)
            return None

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
            min_fibo_score = float(_cfg("FIBO_ADVANCE_MIN_FIBO_SCORE", 38.0))
            fib            = fibo_ctx.fib_levels
            direction      = "long" if fib.direction == "bullish" else "short"
            smc_aligned    = not smc_context or smc_context.bias in ("neutral", direction)
            dist_ok        = fibo_ctx.nearest_level_dist_pct <= float(_cfg("FIBO_ADVANCE_MAX_LEVEL_DIST_PCT", 0.25))
            score_ok       = fibo_ctx.fibo_confluence_score >= min_fibo_score

            if direction == "short" and bool(getattr(config, "FIBO_ADVANCE_SHORT_QUARANTINE_ENABLED", True)):
                logger.info("[FiboAdvance:Sniper] blocked: fibo_xauusd short quarantine")
            elif score_ok and smc_aligned and dist_ok:
                # ── Gate: Impulse freshness ────────────────────────────────
                fresh_ok, fresh_reason = self._check_impulse_freshness(fib, df_h1, mode="sniper")
                if not fresh_ok:
                    logger.debug("[FiboAdvance:Sniper] %s", fresh_reason)
                else:
                    # ── Gate: Entry Sharpness Score ────────────────────────
                    sharp_ok, sharp_reason, sharpness = self._check_entry_sharpness(
                        direction, snapshot, mode="sniper"
                    )
                    if not sharp_ok:
                        logger.info("[FiboAdvance:Sniper] blocked: %s", sharp_reason)
                    else:
                        # ── Gate: Volume Profile confluence ───────────────
                        vp_adj, vp_reason, vp_check = self._check_volume_profile(
                            fibo_ctx.nearest_level_price, direction
                        )

                        # ── Gate: Microstructure ──────────────────────────
                        micro_ok, micro_reason = self._check_microstructure(direction, 70.0, snapshot)
                        if micro_ok:
                            quality_ok, quality_reason, quality = self._fib_entry_quality_gate(
                                direction,
                                fibo_ctx,
                                snapshot,
                                df_h1,
                                atr_h1,
                                mode="sniper",
                                sharpness=sharpness,
                            )
                            if not quality_ok:
                                logger.info("[FiboAdvance:Sniper] blocked: %s", quality_reason)
                            else:
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
                                    # Mark as Sniper for pattern
                                    signal.pattern = signal.pattern.replace("FIBO_", "FIBO_SNIPER_")
                                    signal.raw_scores["mode"] = "sniper"
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
                                        killer_cleared=True,
                                        sharpness_score=int(sharpness.get("sharpness_score", 0) or 0),
                                        sharpness_band=str(sharpness.get("sharpness_band", "") or ""),
                                        vp_reason=vp_reason,
                                        notes=signal.reasons,
                                    )
                                    logger.info(
                                        "[FiboAdvance:Sniper] SIGNAL #%d | %s | Conf:%.1f | "
                                        "Fib:%.3f | GP:%s | Entry:%.2f | SL:%.2f | TP2:%.2f | RR:%.2f | "
                                        "Sharpness:%d(%s) | VP:%s",
                                        self.signal_count, direction.upper(), signal.confidence,
                                        fibo_ctx.nearest_level_ratio, fibo_ctx.in_golden_pocket,
                                        signal.entry, signal.stop_loss, signal.take_profit_2, signal.risk_reward,
                                        int(sharpness.get("sharpness_score", 0) or 0),
                                        str(sharpness.get("sharpness_band", "") or ""),
                                        vp_reason,
                                    )
                                    return signal

        # ══ SCOUT MODE: H1 impulse → M15 entry (fires while waiting for Sniper) ══
        if not sniper_fired and bool(_cfg("FIBO_SCOUT_ENABLED", True)):
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
            )
            if scout_signal is not None:
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
