"""
learning/position_trailing_brain.py
Neural action-value trailing stop engine that learns to maximize profit retention without rigid rules.
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
    mode: str

class PositionTrailingBrain:
    def __init__(self, db_path: str | None = None, model_dir: str | None = None):
        data_dir = Path(__file__).resolve().parent.parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path or (data_dir / "signal_learning.db"))
        self.model_dir = Path(model_dir or (data_dir / "neural_models"))
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.ghost_mode = True  # Phase 1 constraint locked ON
        self._init_db()

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
                        
                        -- Output prediction decision
                        predicted_lock_r REAL,
                        
                        -- Retroactive audit labels (Y)
                        optimal_lock_r REAL,
                        final_trade_r REAL,
                        choked_runner INTEGER
                    )
                """)

    def _utc_now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def get_trailing_decision(self, state: dict) -> TrailingDecision:
        """
        Called by the Executor at every evaluation loop. Evaluates true market state 
        and returns the optimal SL lock. In Ghost Mode, it explicitly forces should_move=False,
        logs the live state, and generates a sample baseline prediction.
        """
        decision_id = str(uuid.uuid4())
        position_id = state.get("position_id", 0)
        symbol = str(state.get("symbol", "UNKNOWN"))
        family = str(state.get("family", "other"))
        
        # Telemetry Features (X)
        r_now = float(state.get("r_now", 0.0))
        time_in_trade_minutes = float(state.get("time_in_trade_minutes", 0.0))
        vwap_slope = float(state.get("vwap_slope_100t", 0.0))
        tick_velocity = float(state.get("tick_velocity", 0.0))
        depth_imbalance = float(state.get("depth_imbalance", 0.0))
        vol_regime_ratio = float(state.get("vol_regime_ratio", 1.0))
        session_overlap_flag = float(state.get("session_overlap_flag", 0.0))
        is_canary = 1.0 if "canary" in str(state.get("source_lane", "")) else 0.0

        if self.ghost_mode:
            mode = "ghost_logged"
            # Placeholder for testing DB schema integration prior to training activation
            predicted_lock_r = 0.10 if r_now >= 0.22 else 0.0
            should_move = False
        else:
            # Phase 3: Query active MLP here
            predicted_lock_r = 0.0
            should_move = True
            mode = "live_active"

        # Persist the feature state to DB immediately for continuous learning
        try:
            with self._lock:
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.execute("""
                        INSERT INTO trailing_decisions (
                            decision_id, position_id, symbol, family, created_at,
                            r_now, time_in_trade_minutes, vwap_slope, tick_velocity, 
                            depth_imbalance, vol_regime_ratio, session_overlap_flag, is_canary,
                            predicted_lock_r
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        decision_id, position_id, symbol, family, self._utc_now_iso(),
                        r_now, time_in_trade_minutes, vwap_slope, tick_velocity,
                        depth_imbalance, vol_regime_ratio, session_overlap_flag, is_canary,
                        predicted_lock_r
                    ))
        except Exception as e:
            logger.error(f"[PositionTrailingBrain] Database Insert failed: {e}")

        # Heavy Logging
        logger.info(
            f"[TRAIL DECISION] {mode.upper()} | symbol={symbol} | lane={'canary' if is_canary else 'winner'} | "
            f"r_now={r_now:.2f} | vwap_slope={vwap_slope:.4f} | predicted_lock_r={predicted_lock_r:.2f} | "
            f"active_sl={state.get('active_sl', 0.0):.4f}"
        )

        return TrailingDecision(
            decision_id=decision_id,
            should_move=should_move,
            trail_lock_r=predicted_lock_r,
            mode=mode
        )

    def update_from_closed_trade(self, position_id: int, final_trade_r: float, optimal_lock_r: float, choked: bool):
        """
        Post-Trade Audit: Retrospectively updates all decisions made for this position
        with their true deterministic deterministic labels to train the Neural Network.
        """
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
            logger.error(f"[PositionTrailingBrain] Database Update failed: {e}")

# Global singleton
trailing_brain = PositionTrailingBrain()
