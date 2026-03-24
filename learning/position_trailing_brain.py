"""
learning/position_trailing_brain.py
Neural action-value trailing stop engine.

MISSION: Use real-time microstructure (100-tick bars, order flow) to predict 
the optimal profit-lock R. 

PHASE 1: Stronger Stepped Heuristic (Active) + Neural Shadow.
PHASE 2: Neural Inference (Supervised Learning from X, Y pairs).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
import os
import time
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
    mode: str  # "heuristic_active" or "shadow_neural"
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
        
        # Phase 2: MLP Weights (7 features)
        # Sequence: [r_now, age, slope, velocity, imbalance, vol_regime, session]
        self.weights_path = self.model_dir / "trailing_mlp_v1.npy"
        self.weights = self._load_weights()
        
        # Active Heuristic: Stronger Stepped Rules for immediate profit protection.
        self._active_steps = [
            (1.80, 1.20, "runner_max"),
            (1.20, 0.80, "runner_major"),
            (0.80, 0.50, "profit_mid"),
            (0.22, 0.18, "be_plus"),
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

                        -- Output prediction / decision (Y')
                        predicted_lock_r REAL,
                        heuristic_lock_r REAL,
                        neural_lock_r REAL,
                        decision_mode TEXT,

                        -- Retroactive audit labels (Y)
                        optimal_lock_r REAL,
                        final_trade_r REAL,
                        choked_runner INTEGER
                    )
                """)

    def _load_weights(self) -> np.ndarray:
        if self.weights_path.exists():
            try:
                return np.load(str(self.weights_path))
            except Exception:
                pass
        # Initialize with neutral/biased weights towards r_now
        w = np.zeros(7)
        w[0] = 0.5  # Start with simple 50% r_now as prediction
        return w

    def _save_weights(self):
        try:
            np.save(str(self.weights_path), self.weights)
        except Exception as e:
            logger.error(f"[PositionTrailingBrain] Weights Save Error: {e}")

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _predict_heuristic_lock_r(self, r_now: float) -> tuple[float, str]:
        """Stronger stepped rules for immediate profit protection."""
        for thresh, lock, lbl in self._active_steps:
            if r_now >= thresh:
                return lock, lbl
        return 0.0, "hold"

    def _predict_neural_lock_r(self, features: dict) -> float:
        """Linear layer inference (Shadow)."""
        X = np.array([
            features["r_now"],
            features["time_in_trade_minutes"],
            features["vwap_slope"],
            features["tick_velocity"],
            features["depth_imbalance"],
            features["vol_regime_ratio"],
            features["session_overlap_flag"]
        ])
        return float(np.dot(X, self.weights))

    def get_trailing_decision(self, state: dict) -> TrailingDecision:
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

        # 1. Active: Stronger Stepped Heuristic
        heuristic_r, h_label = self._predict_heuristic_lock_r(r_now)
        
        # 2. Shadow: Neural Prediction
        neural_r = self._predict_neural_lock_r(features)
        
        mode = "heuristic_active"
        final_lock_r = heuristic_r
        
        should_move = bool(final_lock_r > 0)

        # 3. Persist
        try:
            with self._lock:
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.execute("""
                        INSERT INTO trailing_decisions (
                            decision_id, position_id, symbol, family, created_at,
                            r_now, time_in_trade_minutes, vwap_slope, tick_velocity,
                            depth_imbalance, vol_regime_ratio, session_overlap_flag, is_canary,
                            predicted_lock_r, heuristic_lock_r, neural_lock_r, decision_mode
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        decision_id, position_id, symbol, family, self._utc_now_iso(),
                        features["r_now"], features["time_in_trade_minutes"], 
                        features["vwap_slope"], features["tick_velocity"],
                        features["depth_imbalance"], features["vol_regime_ratio"],
                        features["session_overlap_flag"], features["is_canary"],
                        final_lock_r, heuristic_r, neural_r, mode
                    ))
        except Exception as e:
            logger.error(f"[PositionTrailingBrain] DB Log Error: {e}")

        # Heavy Transparency Logging (Audit trail)
        logger.info(
            f"[TRAIL DECISION] {mode.upper()} | r_now={r_now:.2f} -> lock_r={final_lock_r:.2f} | "
            f"neural_shadow={neural_r:.2f} | label={h_label}"
        )

        return TrailingDecision(
            decision_id=decision_id,
            should_move=should_move,
            trail_lock_r=final_lock_r,
            mode=mode,
            diagnostics={"h_label": h_label, "features": features}
        )

    def update_from_closed_trade(self, position_id: int, final_trade_r: float, optimal_lock_r: float, choked: bool):
        """FEEDBACK LOOP: Label historical decisions."""
        choked_int = 1 if choked else 0
        try:
            with self._lock:
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.execute("""
                        UPDATE trailing_decisions
                        SET optimal_lock_r = ?, final_trade_r = ?, choked_runner = ?
                        WHERE position_id = ? AND optimal_lock_r IS NULL
                    """, (float(optimal_lock_r), float(final_trade_r), int(choked_int), position_id))
            logger.info(f"[TRAIL FEEDBACK] Position {position_id} resolved: Final={final_trade_r:.2f}R | Optimal={optimal_lock_r:.2f}R | Choked={choked}")
        except Exception as e:
            logger.error(f"[PositionTrailingBrain] FB Update Error: {e}")

    def train_trailing_mlp(self):
        """Supervised Learning: Update weights using (Features X) -> (Optimal Y)."""
        try:
            with self._lock:
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    data = conn.execute("""
                        SELECT r_now, time_in_trade_minutes, vwap_slope, tick_velocity, 
                               depth_imbalance, vol_regime_ratio, session_overlap_flag,
                               optimal_lock_r, choked_runner
                        FROM trailing_decisions 
                        WHERE optimal_lock_r IS NOT NULL
                    """).fetchall()
            
            if len(data) < 20:
                logger.debug(f"[PositionTrailingBrain] Insufficient samples for training ({len(data)}/20)")
                return
                
            X = []
            Y = []
            weights = []
            for row in data:
                X.append([
                    row["r_now"], row["time_in_trade_minutes"], row["vwap_slope"],
                    row["tick_velocity"], row["depth_imbalance"], row["vol_regime_ratio"],
                    row["session_overlap_flag"]
                ])
                Y.append(row["optimal_lock_r"])
                # 3x penalty for choked runners (optimal_lock_r was much higher than trailed)
                weights.append(3.0 if row["choked_runner"] else 1.0)
            
            X = np.array(X)
            Y = np.array(Y)
            W = np.diag(weights)
            
            # Weighted Least Squares solution: w = (X^T W X)^-1 X^T W Y
            try:
                new_w = np.linalg.inv(X.T @ W @ X) @ X.T @ W @ Y
                self.weights = new_w
                self._save_weights()
                logger.info(f"[PositionTrailingBrain] Training complete: n={len(data)} | bias_weight[0]={new_w[0]:.4f}")
            except np.linalg.LinAlgError:
                logger.error("[PositionTrailingBrain] Matrix inversion failed (singular matrix)")
                
        except Exception as e:
            logger.error(f"[PositionTrailingBrain] Training Cycle Error: {e}")

# Singleton
trailing_brain = PositionTrailingBrain()
