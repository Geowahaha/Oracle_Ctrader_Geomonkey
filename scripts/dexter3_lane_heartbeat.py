#!/usr/bin/env python3
"""Heartbeat (decision-progress) check for a Dexter3 XAU lane.

Background (owner incident 2026-07-09): the parallel watchdog
(``ops/dexter3_xau_parallel_watchdog.ps1``) only checked that a lane's
tracked PID was alive. A hung-but-alive lane (MCP outage / cTrader desktop
crash) went undetected for 3h49m because "process exists" kept reporting
healthy while both lanes were blind. This module adds a second, additive
signal: is the lane still making decision-cycle progress, not just running.

Heartbeat source: each lane's state JSON (``data/runtime/dexter3_shadow_state.json``
for V1.6, ``data/runtime/dexter3_grok_shadow_state.json`` for Grok) carries
``symbols.<SYMBOL>.last_seen_at`` (UTC ISO, ``%Y-%m-%dT%H:%M:%SZ``).

CONFIRMATION — why this will not false-positive on a quiet market
-------------------------------------------------------------------
``last_seen_at`` is stamped by ``dexter3.shadow_runner.mark_m5_close_seen``
(dexter3/shadow_runner.py:986-990), which is called unconditionally at the
end of every pending-M5-bar iteration in ``run_symbol_cycle``
(dexter3/shadow_runner.py:1349, immediately before ``save_shadow_state`` on
line 1350) — regardless of ``decision.action`` (enter/hold/skip) and
regardless of whether a live order was placed. So a quiet market (no trades)
still advances ``last_seen_at`` on every M5 evaluation.

IMPORTANT correction to an earlier assumption: ``last_seen_at`` does NOT
update on every ~20s outer poll tick. ``run_symbol_cycle`` only calls
``mark_m5_close_seen`` when ``pending_m5_closes`` finds a bar the lane
hasn't decided yet (dexter3/shadow_runner.py:1202-1204: returns early with
"no_new_m5_close" otherwise) — and new M5 bars close once every
``M5_BAR_SEC`` = 300s (dexter3/shadow_runner.py:96). Verified empirically
against the live state files on 2026-07-09/10: both
``data/runtime/dexter3_shadow_state.json`` and
``data/runtime/dexter3_grok_shadow_state.json`` show
``last_seen_at`` landing ~25-30s after each 5-minute bar boundary (e.g.
last_m5_close_ts=2026-07-09T19:25:00Z, last_seen_at=2026-07-09T19:30:25Z),
i.e. the natural healthy cadence between heartbeat writes is ~300-330s, not
~20s. A naive 180s default (~9x the 20s poll) would therefore misfire on
almost every single healthy bar rollover. DEFAULT_STALE_SEC below is set to
2x the M5 bar interval (600s) instead — comfortable margin above the
normal ~300-330s cadence (tolerates one skipped/late cycle) while still
cutting a real outage from 3h49m down to a worst case of ~10 minutes.
Operators can override via the DEXTER3_HEARTBEAT_STALE_SEC env var.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STALE_SEC = 600.0
ENV_STALE_SEC = "DEXTER3_HEARTBEAT_STALE_SEC"
DEFAULT_SYMBOL = "XAUUSD"


@dataclass(frozen=True)
class HeartbeatResult:
    """Outcome of one heartbeat check.

    status: "healthy" | "stale" | "missing"
      - "missing": file absent/unreadable, malformed JSON, no entry for
        ``symbol``, or no (parsable) ``last_seen_at`` value. Callers should
        treat this the same as the existing "PID not alive" path — e.g. a
        lane that never started yet, or was just launched and hasn't
        completed its first M5 cycle — NOT as proof of a hang on its own.
      - "stale": a parsable ``last_seen_at`` exists but is older than
        ``threshold_sec``.
      - "healthy": within threshold.
    """

    status: str
    age_sec: float | None
    last_seen_at: str | None
    threshold_sec: float
    reason: str | None = None

    @property
    def healthy(self) -> bool:
        return self.status == "healthy"


def _parse_iso_utc(ts: str) -> float | None:
    """Parse a UTC ISO timestamp to a POSIX epoch; never raises.

    Mirrors dexter3.shadow_runner._iso_to_epoch's two accepted shapes
    (strict ``%Y-%m-%dT%H:%M:%SZ`` written by ``utc_now_iso()``, plus a
    general ``fromisoformat`` fallback) without importing shadow_runner,
    so this ops helper stays a lightweight, independent dependency.
    """
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def check_lane_heartbeat(
    state_path: str | Path,
    threshold_sec: float,
    *,
    symbol: str = DEFAULT_SYMBOL,
    now: datetime | None = None,
) -> HeartbeatResult:
    """Check one lane's state file for decision-cycle staleness.

    Pure given its inputs (only side effect is the one file read); never
    raises. Tests MUST pass an explicit ``now`` — omitting it uses real UTC
    time, which is only appropriate for the CLI/live path.
    """
    now_dt = now if now is not None else datetime.now(timezone.utc)

    path = Path(state_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return HeartbeatResult("missing", None, None, threshold_sec, reason="file_not_found")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return HeartbeatResult("missing", None, None, threshold_sec, reason="invalid_json")

    if not isinstance(data, dict):
        return HeartbeatResult("missing", None, None, threshold_sec, reason="invalid_json")

    symbols = data.get("symbols")
    sym_state = symbols.get(symbol) if isinstance(symbols, dict) else None
    if not isinstance(sym_state, dict):
        return HeartbeatResult("missing", None, None, threshold_sec, reason="no_symbol_state")

    last_seen_at = sym_state.get("last_seen_at")
    if not last_seen_at or not isinstance(last_seen_at, str):
        return HeartbeatResult("missing", None, None, threshold_sec, reason="no_last_seen_at")

    epoch = _parse_iso_utc(last_seen_at)
    if epoch is None:
        return HeartbeatResult("missing", None, last_seen_at, threshold_sec, reason="unparsable_timestamp")

    age_sec = max(0.0, now_dt.timestamp() - epoch)
    if age_sec > threshold_sec:
        return HeartbeatResult("stale", age_sec, last_seen_at, threshold_sec, reason="heartbeat_older_than_threshold")
    return HeartbeatResult("healthy", age_sec, last_seen_at, threshold_sec)


def _default_threshold_sec() -> float:
    raw = os.environ.get(ENV_STALE_SEC)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_STALE_SEC


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a Dexter3 lane's M5 decision heartbeat")
    parser.add_argument("--state-file", required=True, help="path to dexter3_*_shadow_state.json")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument(
        "--threshold-sec",
        type=float,
        default=_default_threshold_sec(),
        help=f"stale threshold in seconds (env {ENV_STALE_SEC}, default {DEFAULT_STALE_SEC:.0f})",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress stdout JSON (exit code still set)")
    args = parser.parse_args(argv)

    result = check_lane_heartbeat(args.state_file, args.threshold_sec, symbol=args.symbol)
    if not args.quiet:
        print(json.dumps(asdict(result), ensure_ascii=False))
    return 0 if result.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
