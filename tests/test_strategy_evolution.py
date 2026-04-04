"""
Tests for learning/strategy_evolution.py

Covers: entry creation, log persistence, querying, stats, formatting.
"""
import json
import pytest
from unittest.mock import patch, mock_open
from pathlib import Path

from learning.strategy_evolution import (
    make_evolution_entry,
    log_change,
    get_recent_entries,
    compute_evolution_stats,
    update_entry_impact,
    format_evolution_summary,
    _load_log,
    _save_log,
    _LOG_PATH,
)


# ---------------------------------------------------------------------------
# 1. Entry Creation
# ---------------------------------------------------------------------------

class TestMakeEntry:
    def test_basic_entry(self):
        entry = make_evolution_entry(
            change_type="weight_calibration",
            description="Adjusted momentum weight 1.0 -> 1.15",
            component="analysis/entry_sharpness.py",
            metric_before={"composite_r": 0.25},
            impact="pending",
            auto=True,
            source="sharpness_feedback",
        )
        assert entry["change_type"] == "weight_calibration"
        assert entry["auto"] is True
        assert "timestamp" in entry
        assert entry["metric_before"]["composite_r"] == 0.25

    def test_minimal_entry(self):
        entry = make_evolution_entry(change_type="bug_fix", description="Fixed session filter")
        assert entry["change_type"] == "bug_fix"
        assert entry["auto"] is False
        assert entry["impact"] == "unknown"
        assert entry["metric_before"] == {}

    def test_all_fields_populated(self):
        entry = make_evolution_entry(
            change_type="feature_added",
            description="Volume Profile module",
            component="analysis/volume_profile.py",
            metric_before={"win_rate": 0.55},
            metric_after={"win_rate": 0.62},
            impact="positive",
            auto=False,
            source="manual",
            metadata={"commit": "abc123"},
        )
        assert entry["metric_after"]["win_rate"] == 0.62
        assert entry["metadata"]["commit"] == "abc123"


# ---------------------------------------------------------------------------
# 2. Persistence (mocked)
# ---------------------------------------------------------------------------

class TestPersistence:
    @patch("learning.strategy_evolution._LOG_PATH")
    def test_save_and_load(self, mock_path, tmp_path):
        test_file = tmp_path / "test_evolution.json"
        mock_path.__class__ = type(test_file)
        # Use real file operations via patching the module-level path
        import learning.strategy_evolution as mod
        original_path = mod._LOG_PATH
        mod._LOG_PATH = test_file

        try:
            entries = [
                make_evolution_entry(change_type="test", description="entry 1"),
                make_evolution_entry(change_type="test", description="entry 2"),
            ]
            assert _save_log(entries) is True
            loaded = _load_log()
            assert len(loaded) == 2
            assert loaded[0]["description"] == "entry 1"
        finally:
            mod._LOG_PATH = original_path

    @patch("learning.strategy_evolution._LOG_PATH")
    def test_load_nonexistent(self, mock_path, tmp_path):
        import learning.strategy_evolution as mod
        original_path = mod._LOG_PATH
        mod._LOG_PATH = tmp_path / "nonexistent.json"
        try:
            assert _load_log() == []
        finally:
            mod._LOG_PATH = original_path


# ---------------------------------------------------------------------------
# 3. Stats Computation
# ---------------------------------------------------------------------------

class TestEvolutionStats:
    @patch("learning.strategy_evolution._load_log")
    def test_stats_with_data(self, mock_load):
        mock_load.return_value = [
            {"change_type": "weight_calibration", "impact": "positive", "auto": True},
            {"change_type": "weight_calibration", "impact": "positive", "auto": True},
            {"change_type": "bug_fix", "impact": "positive", "auto": False},
            {"change_type": "feature_added", "impact": "unknown", "auto": False},
        ]
        stats = compute_evolution_stats()
        assert stats["total_entries"] == 4
        assert stats["auto_changes"] == 2
        assert stats["manual_changes"] == 2
        assert stats["by_type"]["weight_calibration"] == 2
        assert stats["by_impact"]["positive"] == 3

    @patch("learning.strategy_evolution._load_log")
    def test_stats_empty(self, mock_load):
        mock_load.return_value = []
        stats = compute_evolution_stats()
        assert stats["total_entries"] == 0
        assert stats["recent_trend"] == "unknown"

    @patch("learning.strategy_evolution._load_log")
    def test_improving_trend(self, mock_load):
        mock_load.return_value = [{"impact": "positive", "auto": True, "change_type": "x"} for _ in range(10)]
        stats = compute_evolution_stats()
        assert stats["recent_trend"] == "improving"


# ---------------------------------------------------------------------------
# 4. Query / Filter
# ---------------------------------------------------------------------------

class TestGetRecentEntries:
    @patch("learning.strategy_evolution._load_log")
    def test_filter_by_type(self, mock_load):
        mock_load.return_value = [
            {"change_type": "weight_calibration", "timestamp": "2026-04-04"},
            {"change_type": "bug_fix", "timestamp": "2026-04-04"},
            {"change_type": "weight_calibration", "timestamp": "2026-04-04"},
        ]
        result = get_recent_entries(change_type="weight_calibration")
        assert len(result) == 2

    @patch("learning.strategy_evolution._load_log")
    def test_filter_by_component(self, mock_load):
        mock_load.return_value = [
            {"component": "analysis/entry_sharpness.py", "timestamp": "2026-04-04"},
            {"component": "scheduler.py", "timestamp": "2026-04-04"},
        ]
        result = get_recent_entries(component="entry_sharpness")
        assert len(result) == 1

    @patch("learning.strategy_evolution._load_log")
    def test_limit_n(self, mock_load):
        mock_load.return_value = [{"change_type": "x", "timestamp": f"2026-04-0{i}"} for i in range(10)]
        result = get_recent_entries(n=3)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# 5. Telegram Formatting
# ---------------------------------------------------------------------------

class TestFormatEvolution:
    @patch("learning.strategy_evolution._load_log")
    def test_format_with_data(self, mock_load):
        mock_load.return_value = [
            {
                "timestamp": "2026-04-04T10:00:00Z",
                "change_type": "weight_calibration",
                "description": "Adjusted momentum weight",
                "impact": "positive",
                "auto": True,
            },
        ]
        text = format_evolution_summary(n=5)
        assert "Strategy Evolution" in text
        assert "weight_calibration" in text
        assert "[auto]" in text

    @patch("learning.strategy_evolution._load_log")
    def test_format_empty(self, mock_load):
        mock_load.return_value = []
        text = format_evolution_summary()
        assert "No recent entries" in text
