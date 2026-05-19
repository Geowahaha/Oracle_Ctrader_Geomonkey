"""
learning/position_trailing_brain.py
Neural action-value trailing stop engine.

MISSION: Use real-time microstructure (100-tick bars, order flow) to predict 
the optimal profit-lock R. 

PHASE 1: Aggressive Stepped Heuristic (Forced Live Improvement) + Neural Shadow.
PHASE 2: Online Supervised Learning (Transitioning to Neural Active).
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
    mode: str  # "heuristic_active" or "neural_shadow"
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
        
        # Neural Model: Linear Regressor (8 features)
        self.weights_path = self.model_dir / "trailing_weights_v1.npy"
        self.weights = self._load_weights()

        # ACTIVE LOGIC v2 (2026-05-19): operator-tunable MFE-peak heuristic.
        # The legacy steps used r_now (CURRENT R) which downgraded the lock
        # whenever price retraced from a peak. The 2026-05-18 loss cluster
        # was caused by that bug — a 1R MFE peak got retraced and the brain
        # decided lock=0 (held) instead of locking 55-70% of the peak.
        #
        # Now the brain tracks per-position peak_r and locks a FRACTION of
        # the peak. Once locked, peak only ratchets up — never down.
        try:
            from config import config as _cfg
            steps_csv = str(getattr(_cfg, "TRAILING_BRAIN_PEAK_STEPS_CSV", "") or "")
        except Exception:
            steps_csv = ""
        if steps_csv:
            self._active_steps = self._parse_steps_csv(steps_csv)
        if not steps_csv or not self._active_steps:
            # MFE-peak progressive ladder — see project_breathing_room_2026_05_18 lesson.
            self._active_steps = [
                (4.00, 3.20, "runner_max_lock_85pct"),    # peak 4R → lock 80%
                (3.00, 2.20, "runner_lock_73pct"),
                (2.00, 1.30, "runner_lock_65pct"),
                (1.50, 0.90, "major_lock_60pct"),
                (1.00, 0.55, "mid_lock_55pct"),
                (0.50, 0.25, "early_lock_50pct"),         # NEW — lock half from 0.5R peak
                (0.22, 0.10, "be_plus_minimal"),          # gentler at micro-peak
            ]

        # Per-position peak-MFE cache so a single excursion locks the floor.
        # In-process memory; resets on restart (acceptable — open positions get
        # re-seeded from the next tick's r_now if it's >= 0).
        self._peak_r_by_pid: dict[int, float] = {}

    @staticmethod
    def _parse_steps_csv(csv: str) -> list:
        """Parse 'threshold:lock:label,...' into the stepped list."""
        out = []
        for chunk in csv.split(","):
            parts = chunk.split(":")
            if len(parts) < 2:
                continue
            try:
                thr = float(parts[0].strip())
                lock = float(parts[1].strip())
                lbl = parts[2].strip() if len(parts) >= 3 else f"step_{thr}_{lock}"
            except (TypeError, ValueError):
                continue
            out.append((thr, lock, lbl))
        # Descending order so we pick the highest-applicable step first.
        out.sort(key=lambda t: t[0], reverse=True)
        return out

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
            try: return np.load(str(self.weights_path))
            except Exception: pass
        w = np.zeros(8)
        w[0] = 0.5 # Default bias: 50% R trailing
        w[7] = 0.01 
        return w

    def _save_weights(self):
        try: np.save(str(self.weights_path), self.weights)
        except Exception as e: logger.error(f"[PositionTrailingBrain] weights save error: {e}")

    def _get_feature_vector(self, features: dict) -> np.ndarray:
        return np.array([
            features["r_now"],
            min(features["time_in_trade_minutes"] / 120.0, 1.0),
            features["vwap_slope"] * 100.0,
            features["tick_velocity"],
            features["depth_imbalance"],
            features["vol_regime_ratio"],
            features["session_overlap_flag"],
            1.0 # Bias
        ])

    def _predict_heuristic_lock_r(self, r_value: float) -> tuple[float, str]:
        """AGGRESSIVE STEPPED RULES — applied to PEAK r (not current r_now).

        Walking the ladder from largest threshold down so the strictest step
        wins; once a peak crosses a level we stay at that lock or higher
        even if subsequent ticks show retracement.
        """
        for thresh, lock, lbl in self._active_steps:
            if r_value >= thresh:
                return lock, lbl
        return 0.0, "hold"

    def _predict_neural_lock_r(self, features: dict) -> float:
        """Linear inference (Shadow)."""
        X = self._get_feature_vector(features)
        return float(np.dot(X, self.weights))

    def get_trailing_decision(self, state: dict) -> TrailingDecision:
        decision_id = str(uuid.uuid4())
        position_id = state.get("position_id", 0)
        symbol = str(state.get("symbol", "UNKNOWN"))
        family = str(state.get("family", "other"))

        r_now = float(state.get("r_now", 0.0))
        # PEAK R tracking — once a position reaches a peak, the floor is
        # set there. Retracement does NOT erase the peak. This is the
        # cure for 2026-05-18 trade 2 (pid=621968762): MFE peaked at 1R+
        # then retraced, brain saw r_now=0.24 and downgraded to lock=0.20
        # — leaving SL untouched at the original 4557.60 which then hit.
        try:
            from config import config as _cfg
            _use_peak = bool(getattr(_cfg, "TRAILING_BRAIN_USE_PEAK_R", True))
        except Exception:
            _use_peak = True
        peak_r = r_now
        if _use_peak and position_id:
            with self._lock:
                prev_peak = float(self._peak_r_by_pid.get(int(position_id), 0.0))
                peak_r = max(prev_peak, r_now)
                self._peak_r_by_pid[int(position_id)] = peak_r

        features = {
            "r_now": r_now,
            "peak_r": peak_r,
            "time_in_trade_minutes": float(state.get("time_in_trade_minutes", 0.0)),
            "vwap_slope": float(state.get("vwap_slope_100t", 0.0)),
            "tick_velocity": float(state.get("tick_velocity", 0.0)),
            "depth_imbalance": float(state.get("depth_imbalance", 0.0)),
            "vol_regime_ratio": float(state.get("vol_regime_ratio", 1.0)),
            "session_overlap_flag": float(state.get("session_overlap_flag", 0.0)),
            "is_canary": 1.0 if "canary" in str(state.get("source_lane", "")) else 0.0
        }

        # 1. Active: Heuristic on PEAK R (operator-tunable via env)
        heuristic_r, h_label = self._predict_heuristic_lock_r(peak_r)
        
        # 2. Shadow: Neural Prediction
        neural_r = self._predict_neural_lock_r(features)
        
        mode = "heuristic_active"
        final_lock_r = heuristic_r
        should_move = bool(final_lock_r > 0)

        # 3. Log Features + Decision
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
            logger.error(f"[PositionTrailingBrain] DB log error: {e}")

        # Heavy Audit Log — shows peak vs r_now so operators can verify the
        # MFE-peak logic is doing its job.
        logger.info(
            f"[TRAIL DECISION] {mode.upper()} | r_now={r_now:.2f} peak_r={peak_r:.2f} -> "
            f"lock_r={final_lock_r:.2f} | neural_pred={neural_r:.2f} | h_label={h_label}"
        )

        return TrailingDecision(
            decision_id=decision_id,
            should_move=should_move,
            trail_lock_r=final_lock_r,
            mode=mode,
            diagnostics={"h_label": h_label, "features": features}
        )

    def update_from_closed_trade(self, position_id: int, final_trade_r: float, optimal_lock_r: float, choked: bool):
        """FEEDBACK LOOP: Updates historical decisions with TRUE labels."""
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
            logger.error(f"[PositionTrailingBrain] FB update error: {e}")

    def train_trailing_mlp(self, learning_rate: float = 0.01):
        """Supervised Online Learning Cycle (SGD)."""
        try:
            with self._lock:
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute("""
                        SELECT r_now, time_in_trade_minutes, vwap_slope, tick_velocity, 
                               depth_imbalance, vol_regime_ratio, session_overlap_flag,
                               optimal_lock_r, choked_runner
                        FROM trailing_decisions WHERE optimal_lock_r IS NOT NULL
                    """).fetchall()
            
            if len(rows) < 10: return
            for row in rows:
                features = {
                    "r_now": row["r_now"], "time_in_trade_minutes": row["time_in_trade_minutes"],
                    "vwap_slope": row["vwap_slope"], "tick_velocity": row["tick_velocity"],
                    "depth_imbalance": row["depth_imbalance"], "vol_regime_ratio": row["vol_regime_ratio"],
                    "session_overlap_flag": row["session_overlap_flag"]
                }
                X = self._get_feature_vector(features)
                y_pred = np.dot(X, self.weights)
                penalty = 3.0 if row["choked_runner"] else 1.0 # Force aggressive capture
                self.weights += learning_rate * penalty * (row["optimal_lock_r"] - y_pred) * X
            self._save_weights()
            logger.info(f"[PositionTrailingBrain] Training complete: n={len(rows)}")
        except Exception as e: logger.error(f"[PositionTrailingBrain] train error: {e}")

# Singleton
trailing_brain = PositionTrailingBrain()
