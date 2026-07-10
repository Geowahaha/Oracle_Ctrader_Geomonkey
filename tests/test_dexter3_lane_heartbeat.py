"""Unit tests for scripts.dexter3_lane_heartbeat (lane decision-progress check).

Covers: fresh -> healthy, stale -> stale, missing file -> missing, malformed
JSON -> missing, and the threshold boundary. All timestamps are fixed/
injected (via the ``now`` kwarg) — no real-clock dependence, so these never
flake.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.dexter3_lane_heartbeat import (
    DEFAULT_STALE_SEC,
    check_lane_heartbeat,
    main,
)

NOW = datetime(2026, 7, 9, 19, 40, 0, tzinfo=timezone.utc)
THRESHOLD = 600.0


def _write_state(path: Path, last_seen_at: str | None, symbol: str = "XAUUSD") -> None:
    sym_block: dict = {}
    if last_seen_at is not None:
        sym_block["last_seen_at"] = last_seen_at
    path.write_text(
        json.dumps({"symbols": {symbol: sym_block}}),
        encoding="utf-8",
    )


def test_fresh_timestamp_is_healthy(tmp_path: Path) -> None:
    state_file = tmp_path / "dexter3_shadow_state.json"
    _write_state(state_file, "2026-07-09T19:39:30Z")  # 30s old

    result = check_lane_heartbeat(state_file, THRESHOLD, now=NOW)

    assert result.status == "healthy"
    assert result.healthy is True
    assert result.age_sec == 30.0
    assert result.last_seen_at == "2026-07-09T19:39:30Z"


def test_stale_timestamp_is_stale(tmp_path: Path) -> None:
    state_file = tmp_path / "dexter3_shadow_state.json"
    # 3h49m old, matching the real incident this fix targets.
    _write_state(state_file, "2026-07-09T15:51:00Z")

    result = check_lane_heartbeat(state_file, THRESHOLD, now=NOW)

    assert result.status == "stale"
    assert result.healthy is False
    assert result.age_sec == 13740.0
    assert result.reason == "heartbeat_older_than_threshold"


def test_missing_file_is_missing(tmp_path: Path) -> None:
    state_file = tmp_path / "does_not_exist.json"

    result = check_lane_heartbeat(state_file, THRESHOLD, now=NOW)

    assert result.status == "missing"
    assert result.healthy is False
    assert result.age_sec is None
    assert result.reason == "file_not_found"


def test_malformed_json_is_missing_not_raising(tmp_path: Path) -> None:
    state_file = tmp_path / "dexter3_shadow_state.json"
    state_file.write_text("{not valid json!!", encoding="utf-8")

    result = check_lane_heartbeat(state_file, THRESHOLD, now=NOW)

    assert result.status == "missing"
    assert result.reason == "invalid_json"


def test_json_array_root_is_missing_not_raising(tmp_path: Path) -> None:
    state_file = tmp_path / "dexter3_shadow_state.json"
    state_file.write_text("[1, 2, 3]", encoding="utf-8")

    result = check_lane_heartbeat(state_file, THRESHOLD, now=NOW)

    assert result.status == "missing"
    assert result.reason == "invalid_json"


def test_symbol_absent_is_missing(tmp_path: Path) -> None:
    state_file = tmp_path / "dexter3_shadow_state.json"
    _write_state(state_file, "2026-07-09T19:39:30Z", symbol="XAUUSD")

    result = check_lane_heartbeat(state_file, THRESHOLD, symbol="BTCUSD", now=NOW)

    assert result.status == "missing"
    assert result.reason == "no_symbol_state"


def test_last_seen_at_missing_key_is_missing(tmp_path: Path) -> None:
    state_file = tmp_path / "dexter3_shadow_state.json"
    _write_state(state_file, None)  # symbol block present but no last_seen_at

    result = check_lane_heartbeat(state_file, THRESHOLD, now=NOW)

    assert result.status == "missing"
    assert result.reason == "no_last_seen_at"


def test_unparsable_timestamp_is_missing_not_raising(tmp_path: Path) -> None:
    state_file = tmp_path / "dexter3_shadow_state.json"
    _write_state(state_file, "not-a-timestamp")

    result = check_lane_heartbeat(state_file, THRESHOLD, now=NOW)

    assert result.status == "missing"
    assert result.reason == "unparsable_timestamp"
    assert result.last_seen_at == "not-a-timestamp"


def test_threshold_boundary_exact_age_is_healthy(tmp_path: Path) -> None:
    state_file = tmp_path / "dexter3_shadow_state.json"
    # Exactly THRESHOLD seconds before NOW -> 19:40:00 - 600s = 19:30:00.
    _write_state(state_file, "2026-07-09T19:30:00Z")

    result = check_lane_heartbeat(state_file, THRESHOLD, now=NOW)

    assert result.age_sec == THRESHOLD
    assert result.status == "healthy"


def test_threshold_boundary_one_second_over_is_stale(tmp_path: Path) -> None:
    state_file = tmp_path / "dexter3_shadow_state.json"
    # One second past the threshold -> 19:29:59.
    _write_state(state_file, "2026-07-09T19:29:59Z")

    result = check_lane_heartbeat(state_file, THRESHOLD, now=NOW)

    assert result.age_sec == THRESHOLD + 1.0
    assert result.status == "stale"


def test_default_stale_sec_is_not_the_naive_180s_assumption() -> None:
    # Regression guard: the default must stay safely above the real M5 bar
    # cadence (300s) so a quiet market never looks stale. See module
    # docstring "CONFIRMATION" section for the empirical basis.
    assert DEFAULT_STALE_SEC >= 600.0


def _fresh_ts() -> str:
    """A 'just now' UTC timestamp for the CLI tests (which use real now
    internally). Must be computed at call time, NOT hardcoded — a fixed
    timestamp goes stale as wall-clock advances and flakes the test."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_cli_exit_code_healthy(tmp_path: Path, capsys) -> None:
    state_file = tmp_path / "dexter3_shadow_state.json"
    _write_state(state_file, _fresh_ts())

    # CLI uses real "now" internally, so give it a threshold large enough
    # that the freshly-written timestamp above is always healthy regardless
    # of tiny scheduling jitter during the test run.
    exit_code = main(["--state-file", str(state_file), "--threshold-sec", "3600", "--quiet"])

    assert exit_code == 0


def test_cli_exit_code_stale_or_missing(tmp_path: Path) -> None:
    state_file = tmp_path / "does_not_exist.json"

    exit_code = main(["--state-file", str(state_file), "--threshold-sec", "600", "--quiet"])

    assert exit_code == 1


def test_cli_prints_json_by_default(tmp_path: Path, capsys) -> None:
    state_file = tmp_path / "dexter3_shadow_state.json"
    _write_state(state_file, _fresh_ts())

    main(["--state-file", str(state_file), "--threshold-sec", "3600"])

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "healthy"
