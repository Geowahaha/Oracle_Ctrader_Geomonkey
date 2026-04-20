"""
utils/atomic_write.py — Atomic JSON file operations.

Prevents corruption on crash/power loss by writing to a .tmp file
first, then atomically renaming to the target path.

Usage:
    from utils.atomic_write import atomic_json_write, atomic_json_read

    # Write (crash-safe)
    atomic_json_write(Path("data/runtime/state.json"), {"key": "value"})

    # Read (handles interrupted writes)
    data = atomic_json_read(Path("data/runtime/state.json"), default={})
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def atomic_json_write(path: Path, data: Any, indent: int = 2) -> None:
    """Write JSON atomically.

    1. Write to path.tmp
    2. Rename path.tmp → path (atomic on both Windows and Linux)

    If the process crashes between steps 1 and 2, the original file
    is untouched. If it crashes during step 2, the rename is atomic
    at the OS level.

    Args:
        path: Target file path.
        data: JSON-serializable data.
        indent: JSON indentation.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")

    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
        tmp.replace(path)  # Atomic rename — overwrites existing
        logger.debug("[atomic_write] Wrote %s (%d bytes)", path, path.stat().st_size)
    except Exception:
        # Clean up temp file on failure
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        logger.exception("[atomic_write] Failed to write %s", path)
        raise


def atomic_json_read(path: Path, default: Optional[Any] = None) -> Any:
    """Read JSON with recovery from interrupted writes.

    If a .tmp file exists alongside the target, the previous write
    was interrupted. Attempts to recover by promoting the .tmp file.

    Args:
        path: File path to read.
        default: Returned if file doesn't exist and no recovery possible.

    Returns:
        Parsed JSON data, or default.
    """
    path = Path(path)
    tmp = path.with_suffix(".tmp")

    # Check for interrupted write (.tmp exists)
    if tmp.exists():
        logger.warning("[atomic_write] Found orphaned .tmp for %s — attempting recovery", path)
        try:
            data = json.loads(tmp.read_text(encoding="utf-8"))
            tmp.replace(path)  # Promote .tmp to real file
            logger.info("[atomic_write] Recovered %s from .tmp", path)
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.error("[atomic_write] Recovery failed for %s: %s — using original", path, e)
            # Fall through to try reading the original

    if not path.exists():
        return default if default is not None else {}

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("[atomic_write] Failed to read %s: %s — returning default", path, e)
        return default if default is not None else {}


def atomic_json_update(path: Path, updater: callable, default: Optional[Any] = None) -> Any:
    """Read-modify-write with atomicity.

    Reads current data, applies updater function, writes atomically.
    Returns the updated data.

    Args:
        path: File path.
        updater: Callable(data) -> modified_data.
        default: Initial data if file doesn't exist.

    Returns:
        The updated data after write.
    """
    current = atomic_json_read(path, default=default)
    updated = updater(current)
    atomic_json_write(path, updated)
    return updated


# ---------------------------------------------------------------------------
# Registry: files that should use atomic writes
# ---------------------------------------------------------------------------

ATOMIC_WRITE_TARGETS = [
    "data/runtime/trading_manager_state.json",
    "data/runtime/neural_gate_canary_policy.json",
    "data/runtime/strategy_lab_team_state.json",
    "data/runtime/trading_team_state.json",
    "data/runtime/xau_direct_lane_tune_state.json",
    "data/runtime/mt5_repeat_error_guard.json",
    "data/runtime/hermes_loop_state.json",
    "data/runtime/auto_live_profile_state.json",
    "data/runtime/canary_post_trade_audit_state.json",
    "data/runtime/ct_only_watch_state.json",
    "data/runtime/openclaw_state.json",
    "data/runtime/parameter_trials.json",
    "data/runtime/token_budget.json",
    "data/runtime/neural_gate_loop_latest.json",
]
