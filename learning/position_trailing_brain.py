"""
learning/position_trailing_brain.py
Neural action-value trailing stop engine.

MISSION: Use real-time microstructure (100-tick bars, order flow) to predict 
the optimal profit-lock R. 

PHASE 1: Heuristic Model (Linear Weights) + Stepped Backstop (Training Seed).
PHASE 2: MLP-based continuous prediction (Supervised Learning).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from config import config

logger = logging.getLogger(__name__)


@dataclass
class TrailingDecision:
    decision_id: str
    should_move: bool
    trail_lock_r: float
    mode: str  # "heuristic_active" or "bridge_active" or "neural_active"
    diagnostics: dict


class PositionTrailingBrain:
    """
    Centralized trailing stop brain.
    Moving away from hardcoded if/else toward learned continuous prediction.
    """

    def __init__(self, db_path: str | None = None, model_dir: str | None = None):
        data_dir = Path(__file__).resolve().parent.parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path or (data_dir / "signal_learning.db"))
        self.model_dir = Path(model_dir or (data_dir / "neural_models"))
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        
        # Temporary Bridge Constraint: 
        # This table acts as a deterministic "Training Seed" and safety backstop.
        # It is NOT the primary logic; the heuristic model sits on top of it.
        self._bridge_backstop = [
            (1.80, 1.20, "runner_protection"),
            (1.20, 0.80, "profit_lock_major"),
            (0.80, 0.50, "profit_lock_mid"),
            (0.22, 0.18, "breakeven_plus"),
        ]

    def _init_db(self):
        with self._lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS trailing_decisions (
                        decision_id TEXT PRIMARY KEY,
                        position_id INTEGER,
                        symbol TEXT,
                        family TEXT,
                        created_at TEXT,

                        -- Real-time context features (X)
                        r_now REAL,
                        time_in_trade_minutes REAL,
                        vwap_slope REAL,
                        tick_velocity REAL,
                        depth_imbalance REAL,
                        vol_regime_ratio REAL,
                        session_overlap_flag REAL,
                        is_canary REAL,

                        -- Output prediction / decision
                        predicted_lock_r REAL,
                        decision_mode TEXT,

                        -- Retroactive audit labels (Y)
                        optimal_lock_r REAL,
                        final_trade_r REAL,
                        choked_runner INTEGER
                    )
                """)

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _predict_optimal_lock_r(self, features: dict) -> tuple[float, str, dict]:
        """
        The 'Brain' logic:
        Uses a weighted heuristic (Simple Model) that prioritizes momentum (VWAP) 
        and volatility (Tick Velocity) over flat thresholds.
        """
        r_now = float(features.get("r_now", 0.0))
        vwap_slope = float(features.get("vwap_slope", 0.0))
        tick_velocity = float(features.get("tick_velocity", 0.0))
        vol_regime = float(features.get("vol_regime_ratio", 1.0))
        
        # 1. Start with the deterministic Bridge Backstop as a seed
        base_lock_r = 0.0
        reason = "hold"
        for thresh, lock, lbl in self._bridge_backstop:
            if r_now >= thresh:
                base_lock_r = lock
                reason = lbl
                break
        
        if r_now < 0.22:
            return 0.0, "waiting_for_min_r", {"base": 0.0}

        # 2. Heuristic Adjustments (The 'Simple Model' requested)
        # Weighting: we want to let it run if momentum is strong (vwap_slope > 0 for longs)
        # and tighten if things are getting choppy (tick_velocity high)
        
        # Momentum adjustment: If slope is strong, loosen the trail slightly (up to -0.05R) to avoid noise-stops
        momentum_adj = 0.0
        if abs(vwap_slope) > 0.0005: 
            momentum_adj = -0.05  # Loosen: "Let it breathe"
            
        # Volatility adjustment: If velocity is high relative to regime, tighten (+0.03R)
        vol_adj = 0.0
        if tick_velocity > 0.60 * vol_regime:
            vol_adj = 0.03  # Tighten: "Secure the bag"
            
        predicted_r = max(0.0, base_lock_r + momentum_adj + vol_adj)
        
        # Ensure we never lock MORE than r_now - 0.02R (stop distance)
        predicted_r = min(predicted_r, r_now - 0.02)
        
        diag = {
            "base_r": round(base_lock_r, 4),
            "momentum_adj": round(momentum_adj, 4),
            "vol_adj": round(vol_adj, 4),
            "final_predicted": round(predicted_r, 4),
            "reason": reason
        }
        
        return predicted_r, "heuristic_active", diag

    def get_trailing_decision(self, state: dict) -> TrailingDecision:
        """
        Core entry point for the Executor.
        Logs every feature state to signal_learning.db for eventually replacing 
        the heuristic with an MLP.
        """
        decision_id = str(uuid.uuid4())
        position_id = state.get("position_id", 0)
        symbol = str(state.get("symbol", "UNKNOWN"))
        family = str(state.get("family", "other"))

        # Features (X)
        r_now = float(state.get("r_now", 0.0))
        features = {
            "r_now": r_now,
            "time_in_trade_minutes": float(state.get("time_in_trade_minutes", 0.0)),
            "vwap_slope": float(state.get("vwap_slope_100t", 0.0)),
            "tick_velocity": float(state.get("tick_velocity", 0.0)),
            "depth_imbalance": float(state.get("depth_imbalance", 0.0)),
            "vol_regime_ratio": float(state.get("vol_regime_ratio", 1.0)),
            "session_overlap_flag": float(state.get("session_overlap_flag", 0.0)),
            "is_canary": 1.0 if "canary" in str(state.get("source_lane", "")) else 0.0
        }

        # Predict (Y')
        predicted_lock_r, mode, diag = self._predict_optimal_lock_r(features)
        
        # Decision logic
        # We only move if predicted_lock_r is significantly better than current lock
        # or if the base backstop is triggered.
        active_sl_r = 0.0 # simplified for comparison
        # (The executor handles the actual compare against current SL, 
        # but the brain decides IF we should even try)
        should_move = bool(predicted_lock_r > 0)

        # Persist features for model training
        try:
            with self._lock:
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.execute("""
                        INSERT INTO trailing_decisions (
                            decision_id, position_id, symbol, family, created_at,
                            r_now, time_in_trade_minutes, vwap_slope, tick_velocity,
                            depth_imbalance, vol_regime_ratio, session_overlap_flag, is_canary,
                            predicted_lock_r, decision_mode
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        decision_id, position_id, symbol, family, self._utc_now_iso(),
                        features["r_now"], features["time_in_trade_minutes"], 
                        features["vwap_slope"], features["tick_velocity"],
                        features["depth_imbalance"], features["vol_regime_ratio"],
                        features["session_overlap_flag"], features["is_canary"],
                        predicted_lock_r, mode
                    ))
        except Exception as e:
            logger.error(f"[PositionTrailingBrain] DB Log Error: {e}")

        # Heavy Transparency Logging (Audit trail)
        logger.info(
            f"[TRAIL DECISION] {mode.upper()} | id={decision_id[:8]} | pos={position_id} | "
            f"r_now={r_now:.2f} -> lock_r={predicted_lock_r:.2f} | "
            f"momentum={features['vwap_slope']:.5f} | velocity={features['tick_velocity']:.3f} | "
            f"diag={diag}"
        )

        return TrailingDecision(
            decision_id=decision_id,
            should_move=should_move,
            trail_lock_r=predicted_lock_r,
            mode=mode,
            diagnostics=diag
        )

    def update_from_closed_trade(self, position_id: int, final_trade_r: float, optimal_lock_r: float, choked: bool):
        choked_int = 1 if choked else 0
        try:
            with self._lock:
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.execute("""
                        UPDATE trailing_decisions
                        SET optimal_lock_r = ?, final_trade_r = ?, choked_runner = ?
                        WHERE position_id = ? AND optimal_lock_r IS NULL
                    """, (float(optimal_lock_r), float(final_trade_r), int(choked_int), position_id))
        except Exception as e:
            logger.error(f"[PositionTrailingBrain] FB Update Error: {e}")

# Singleton
trailing_brain = PositionTrailingBrain()
