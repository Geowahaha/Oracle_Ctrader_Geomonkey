#!/usr/bin/env python3
"""Dexter3 M5 shadow/live loop — lens -> brain -> journal (+ demo micro-entries in Phase 2).

Without ``--live``, this file behaves EXACTLY as Phase 1: a pure observation
loop that, on every NEW M5 close per symbol, fetches M5/M15/H1 trendbars +
spot via ``dexter3.mcp_client`` (read-only), runs ``market_lens`` ->
``hunter_brain.decide()``, journals the decision, and drives a paper
(simulated-fill) ``BasketManager`` so basket-management telemetry
accumulates before any money is at risk.

Phase 2 adds an OPT-IN live micro-entry path: passing ``--live`` AND setting
the environment variable ``DEXTER3_LIVE=1`` (double opt-in — either alone is
not enough) constructs a ``dexter3.executor.Dexter3Executor`` and invokes it
for the newest bar's ``action == 'enter'`` decisions only (never for
historical catch-up bars). The executor itself refuses to trade unless the
bound account is confirmed demo (see ``dexter3/executor.py``), so a live
loop pointed at the wrong account still cannot place an order.

Every ``LEARNING_REFRESH_MINUTES`` (default 15), the loop also refreshes
empirical p_win_est stats (``dexter3.empirical_stats``) and evaluates
pending skip decisions for the "fear cost" KPI
(``dexter3.skip_evaluator``) — both additive, both no-ops until enough
closed trades/skips exist in the journal.

CLI:
    python -m dexter3.shadow_runner --symbols XAUUSD,BTCUSD --once
    python -m dexter3.shadow_runner --symbols XAUUSD,BTCUSD --loop --poll-sec 20
    DEXTER3_LIVE=1 python -m dexter3.shadow_runner --symbols BTCUSD --loop --live

Single-instance lock: ``data/runtime/dexter3_loop.lock`` (own lock, separate
from the live scalp loops' locks — blueprint "Non-negotiables" #2). Stale
lock (pid no longer running) is taken over automatically.

State: ``data/runtime/dexter3_shadow_state.json`` tracks the last-seen M5
close id per symbol so each M5 close triggers exactly one decision.

Logging: one line per decision to stdout AND
``data/runtime/dexter3_shadow.log`` (UTF-8, Thai-safe).

Failure handling:
  - ``McpZombieError``: log loudly, sleep 60s, continue the loop.
  - any other exception: log full traceback, continue the loop (never die).
  - ``KeyboardInterrupt``: release the lock and exit cleanly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dexter3 import (
    basket_live,
    edge_buckets,
    empirical_stats,
    hunt_mode,
    hunter_brain,
    market_lens,
    price_action_eye,
    skip_evaluator,
)
from dexter3.basket_manager import BasketConfig, BasketManager, Leg
from dexter3.daily_governor import DailyGovernor, GovernorConfig
from dexter3.decision_journal import DecisionJournal
from dexter3.edge_buckets import EdgeGateConfig, anti_chase_risk_mult
from dexter3.weekly_risk import weekly_close_policy
from dexter3.executor import LABEL as LIVE_ORDER_LABEL
from dexter3.executor import VERSION as FABLE_VERSION
from dexter3.executor import LABEL_FAMILY as FABLE_LABEL_FAMILY
from dexter3.executor import Dexter3Executor, ExecutorConfig, label_matches_family
from dexter3.mcp_client import Dexter3McpClient, McpClientError, McpZombieError
from dexter3.opening_manager import OMConfig, OpeningManager
from dexter3.transport import make_client

# Grok_v1.0 (optional import for docs / direct use)
try:
    from dexter3 import grok_v10 as grok_v10  # independent parallel scalping
    from dexter3.grok_v10 import GROK_LABEL, GROK_LABEL_FAMILY, GrokV10OpeningManager
except Exception:
    grok_v10 = None  # type: ignore
    GROK_LABEL = None
    GROK_LABEL_FAMILY = None
    GrokV10OpeningManager = None  # type: ignore
from dexter3.smart_exit import SmartExitConfig, resolve_stop_regime
from dexter3.v16_entry_quality import (
    V16EntryQualityConfig,
    evaluate_v16_entry_gate,
    record_noise_close,
)

RUNTIME = ROOT / "data" / "runtime"
STATE_FILE = RUNTIME / "dexter3_shadow_state.json"
GROK_STATE_FILE = RUNTIME / "dexter3_grok_shadow_state.json"
VP_STATE_FILE = RUNTIME / "dexter3_vp_shadow_state.json"
LOG_FILE = RUNTIME / "dexter3_shadow.log"
# H5 (2026-07-15 cross-lane entanglement audit): per-lane log files. Fable's
# path stays LOG_FILE unchanged (confirmed the only code reader,
# ops/dexter3_telegram_watcher.py, hardcodes exactly this fable path — see
# _active_log_file below), so keeping it as-is means zero breakage there.
GROK_LOG_FILE = RUNTIME / "dexter3_grok_shadow.log"
VP_LOG_FILE = RUNTIME / "dexter3_vp_shadow.log"
LOCK_FILE = RUNTIME / "dexter3_loop.lock"
GROK_LOCK_FILE = RUNTIME / "dexter3_grok_loop.lock"
VP_LOCK_FILE = RUNTIME / "dexter3_vp_loop.lock"

DEFAULT_SYMBOLS = ("XAUUSD", "BTCUSD")
DEFAULT_POLL_SEC = 20
M5_BAR_SEC = 300
MCP_ZOMBIE_SLEEP_SEC = 60
MIN_M5_BARS = 60
M15_BARS_NEEDED = 60
H1_BARS_NEEDED = 60

# -- OPENING MANAGER (owner directive 2026-07-07): fast intrabar defense ----
# The main loop ticks every DEXTER3_FAST_TICK_SEC (default 4s) instead of
# sleeping poll_sec as one block. Every fast tick, OM (dexter3.opening_manager)
# runs on any open lane — Profit Hunter (ratcheting trail) + Basket Doctor
# (edge-measured repair) — independent of the M5 cadence; the M5 entry/
# decision path (run_once) still fires only once per ~poll_sec worth of fast
# ticks. See docs/DEXTER3_M5_HUNTER_BLUEPRINT.md "OPENING MANAGER (OM)".
DEFAULT_FAST_TICK_SEC = 4
OM_BAR_REFRESH_SEC = 30  # refetch M5/M15/H1 bars at most this often; spot+positions refetch EVERY tick

# -- Phase 2: live micro-entry double opt-in ---------------------------------
# Placing a real (demo) order requires BOTH the --live CLI flag AND this env
# var set to "1" — blueprint P2 "double opt-in" so a stray --live in a test
# harness or a copy-pasted command can never place an order by accident.
DEXTER3_LIVE_ENV_VAR = "DEXTER3_LIVE"

# -- HUNT MODE (owner directive 2026-07-05): participation-first ------------
# When DEXTER3_HUNT=1, the NEWEST bar's decision comes from
# hunt_mode.decide_hunt (always-enter direction committee) instead of the
# selective sniper brain, and an open lane basket routes the bar to basket
# management (basket_live) instead of a fresh entry. Catch-up (historical)
# bars keep sniper decisions — they are journal-only telemetry either way.
DEXTER3_HUNT_ENV_VAR = "DEXTER3_HUNT"


def _hunt_enabled() -> bool:
    return os.environ.get(DEXTER3_HUNT_ENV_VAR) == "1"


def _vp_producer_enabled() -> bool:
    """Volume-profile producer canary flag — DEFAULT OFF (owner sign-off
    required before enabling; see the 2026-07-11 promotion-gate results on
    AGENT_SYNC_BOARD). Takes precedence over hunt when enabled. Two ways in:
    DEXTER3_PRODUCER=vp (producer-only override) or DEXTER3_MODE=vp (the full
    VP canary lane: own label dexter3:vp:canary + own state file + own lock,
    same isolation pattern as the grok lane)."""
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "vp":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "vp"


def _active_order_label(mode: str | None = None) -> str:
    """Broker label owned by the current Dexter3 process.

    V1.6 and Grok v1.0 must never share lane reads or close-all operations.
    The process mode selects exactly one label.
    """
    current_mode = (mode or os.environ.get("DEXTER3_MODE", "v16")).lower().strip()
    if current_mode == "grok" and GROK_LABEL:
        return GROK_LABEL
    if current_mode == "vp":
        from dexter3.volume_profile import VP_LABEL

        return VP_LABEL
    return LIVE_ORDER_LABEL


def _active_label_family(mode: str | None = None) -> str:
    """FAMILY counterpart of ``_active_order_label`` (2026-07-15
    versioned-labels design): spans every version of the current lane's
    label (fable's family never changes across a ``DEXTER3_FABLE_VERSION``
    bump), rather than pinning to today's exact versioned string.

    Use this wherever a caller needs to MATCH rows/positions written under
    ANY past version of this lane (``lane_positions``, ``_lane_realized_today``,
    ``empirical_stats.compute_from_journal``, ``skip_evaluator.*``). Use
    ``_active_order_label`` (unchanged) wherever a caller needs to WRITE
    today's exact label (journal stamps, order placement) — a version bump
    must still be attributable in the journal/broker, only MATCHING spans
    versions.
    """
    current_mode = (mode or os.environ.get("DEXTER3_MODE", "v16")).lower().strip()
    if current_mode == "grok" and GROK_LABEL_FAMILY:
        return GROK_LABEL_FAMILY
    if current_mode == "vp":
        from dexter3.volume_profile import VP_LABEL_FAMILY

        return VP_LABEL_FAMILY
    return FABLE_LABEL_FAMILY


def _active_state_file(mode: str | None = None) -> Path:
    current_mode = (mode or os.environ.get("DEXTER3_MODE", "v16")).lower().strip()
    if current_mode == "grok":
        return GROK_STATE_FILE
    if current_mode == "vp":
        return VP_STATE_FILE
    return STATE_FILE


def _active_log_file(mode: str | None = None) -> Path:
    """H5 (2026-07-15 cross-lane entanglement audit): mode-selected log path,
    same pattern as ``_active_state_file`` above. Fable (default/"v16") keeps
    ``LOG_FILE`` (``dexter3_shadow.log``) unchanged — ops/dexter3_telegram_watcher.py
    is the only code that reads this path today and it only ever reads the
    fable path, so this default is a strict no-op for that reader."""
    current_mode = (mode or os.environ.get("DEXTER3_MODE", "v16")).lower().strip()
    if current_mode == "grok":
        return GROK_LOG_FILE
    if current_mode == "vp":
        return VP_LOG_FILE
    return LOG_FILE


def _executor_config_from_env() -> "ExecutorConfig":
    """Operator knobs without code edits — set env before launching the loop.

    XAUUSD REQUIRES DEXTER3_MAX_VOLUME_UNITS>=1: its minVolume is 1 unit
    (1 oz, lotSize=100 measured 2026-07-06), so the BTC-scale default cap
    (0.05) refuses every XAU entry via min_volume_exceeds_max_volume_units_cap.
    """
    kw: dict[str, Any] = {}
    for env, field, cast in (
        ("DEXTER3_RISK_USD", "risk_usd", float),
        ("DEXTER3_MAX_VOLUME_UNITS", "max_volume_units", float),
        ("DEXTER3_MAX_ENTRIES_PER_DAY", "max_live_entries_per_day", int),
        ("DEXTER3_DAILY_LOSS_BASKETS", "stop_after_daily_losses", int),
        ("DEXTER3_MAX_SPREAD_BPS", "max_spread_bps", float),
    ):
        raw = os.environ.get(env)
        if raw:
            try:
                kw[field] = cast(raw)
            except ValueError:
                log_line(f"{utc_now_iso()} ignored invalid {env}={raw!r}")
    cfg = ExecutorConfig(**kw)
    log_line(
        f"{utc_now_iso()} executor config: risk_usd={cfg.risk_usd} "
        f"max_volume_units={cfg.max_volume_units} entries/day={cfg.max_live_entries_per_day} "
        f"daily_loss_stop={cfg.stop_after_daily_losses} spread_cap_bps={cfg.max_spread_bps}"
    )
    return cfg


def _basket_config_from_env() -> BasketConfig:
    """Same DEXTER3_DAILY_LOSS_BASKETS knob drives the basket engine's daily
    cap so the two layers can never disagree about when the day is over.

    FIX 2 (2026-07-07): also reads the peak-R trailing knobs so the PM can
    tune the fix live without a code edit — DEXTER3_ARM_TRAIL_R /
    DEXTER3_TRAIL_KEEP_FRAC / DEXTER3_TAKE_R / DEXTER3_RESOLVE_TARGET_R.
    Any missing/invalid env var silently falls back to the BasketConfig
    dataclass default (same "ignored invalid" posture as
    _executor_config_from_env below) — a typo'd env var must never crash the
    live loop, only leave that one knob at its default.
    """
    kw: dict[str, Any] = {}
    raw = os.environ.get("DEXTER3_DAILY_LOSS_BASKETS")
    if raw:
        try:
            kw["daily_loss_baskets"] = int(raw)
        except ValueError:
            pass
    for env, field in (
        ("DEXTER3_ARM_TRAIL_R", "arm_trail_r"),
        ("DEXTER3_TRAIL_KEEP_FRAC", "trail_keep_frac"),
        ("DEXTER3_TAKE_R", "take_r"),
        ("DEXTER3_RESOLVE_TARGET_R", "resolve_target_r"),
    ):
        raw_val = os.environ.get(env)
        if raw_val:
            try:
                kw[field] = float(raw_val)
            except ValueError:
                log_line(f"{utc_now_iso()} ignored invalid {env}={raw_val!r}")
    return BasketConfig(**kw)


def _parse_ladder_csv(raw: str) -> tuple[tuple[float, float], ...] | None:
    """Parse ``DEXTER3_OM_LADDER_CSV`` ("peak:floor,peak:floor,...") into the
    ``OMConfig.ladder_points`` tuple shape. Returns ``None`` (caller keeps the
    default) on ANY malformed input — a typo'd override must never crash the
    live loop or silently install a broken (non-monotonic) ladder.
    """
    points: list[tuple[float, float]] = []
    try:
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            peak_txt, floor_txt = chunk.split(":")
            points.append((float(peak_txt), float(floor_txt)))
    except (ValueError, TypeError):
        return None
    if len(points) < 2:
        return None
    # Must be strictly ascending in peak and non-decreasing in floor, and
    # every floor must stay <= its own peak (a floor above its own peak
    # would violate "never give back more than the peak reached").
    for (p_prev, f_prev), (p_next, f_next) in zip(points, points[1:]):
        if p_next <= p_prev or f_next < f_prev or f_prev > p_prev or f_next > p_next:
            return None
    return tuple(points)


def _om_config_from_env() -> OMConfig:
    """OpeningManager knobs, same "ignored invalid falls back to default"
    posture as ``_basket_config_from_env``/``_executor_config_from_env``
    above — a typo'd env var must never crash the live loop.

    Legacy flat-trail env vars (kept for A/B / override use, no longer the
    default profit exit — see the DRAGON LADDER section of
    ``docs/DEXTER3_M5_HUNTER_BLUEPRINT.md``): DEXTER3_OM_ARM_R,
    DEXTER3_OM_TRAIL_KEEP, DEXTER3_OM_TAKE_R, DEXTER3_OM_SPIKE_R,
    DEXTER3_OM_REPAIR_TRIGGER_R, DEXTER3_OM_REPAIR_MIN_CONV.

    DRAGON LADDER env vars (owner directive 2026-07-08):
    DEXTER3_OM_LADDER_CSV ("peak:floor,peak:floor,..." optional override of
    the default ladder_points table), DEXTER3_OM_STALL_TICKS,
    DEXTER3_OM_STALL_MAX_PEAK_R, DEXTER3_OM_PYRAMID_MIN_R,
    DEXTER3_OM_PYRAMID_MIN_CONV, DEXTER3_OM_PYRAMID_TIER_STEP, and the master
    switch DEXTER3_OM_PYRAMID_ENABLED (default "1"; "0" disables ONLY the
    pyramid-add path — ladder + stall-take stay on).
    """
    kw: dict[str, Any] = {}
    for env, field in (
        ("DEXTER3_OM_ARM_R", "arm_trail_r"),
        ("DEXTER3_OM_TRAIL_KEEP", "trail_keep_frac"),
        ("DEXTER3_OM_TAKE_R", "take_r"),
        ("DEXTER3_OM_SPIKE_R", "spike_take_r"),
        ("DEXTER3_OM_REPAIR_TRIGGER_R", "repair_trigger_r"),
        ("DEXTER3_OM_REPAIR_MIN_CONV", "repair_min_conviction"),
        ("DEXTER3_OM_STALL_MAX_PEAK_R", "stall_max_peak_r"),
        ("DEXTER3_OM_STALL_DECAY_FRAC", "stall_decay_frac"),
        ("DEXTER3_OM_STALL_MIN_PEAK_R", "stall_min_peak_r"),
        ("DEXTER3_OM_PYRAMID_MIN_R", "pyramid_min_live_r"),
        ("DEXTER3_OM_PYRAMID_MIN_CONV", "pyramid_min_conv"),
        ("DEXTER3_OM_PYRAMID_TIER_STEP", "pyramid_tier_step"),
    ):
        raw_val = os.environ.get(env)
        if raw_val:
            try:
                kw[field] = float(raw_val)
            except ValueError:
                log_line(f"{utc_now_iso()} ignored invalid {env}={raw_val!r}")

    raw_stall_ticks = os.environ.get("DEXTER3_OM_STALL_TICKS")
    if raw_stall_ticks:
        try:
            kw["stall_ticks"] = int(raw_stall_ticks)
        except ValueError:
            log_line(f"{utc_now_iso()} ignored invalid DEXTER3_OM_STALL_TICKS={raw_stall_ticks!r}")

    raw_stall_min_hold = os.environ.get("DEXTER3_OM_STALL_MIN_HOLD_TICKS")
    if raw_stall_min_hold:
        try:
            kw["stall_min_hold_ticks"] = int(raw_stall_min_hold)
        except ValueError:
            log_line(f"{utc_now_iso()} ignored invalid DEXTER3_OM_STALL_MIN_HOLD_TICKS={raw_stall_min_hold!r}")

    raw_pyramid_enabled = os.environ.get("DEXTER3_OM_PYRAMID_ENABLED")
    if raw_pyramid_enabled is not None:
        kw["pyramid_enabled"] = raw_pyramid_enabled.strip() not in ("0", "false", "False", "")

    raw_ladder_csv = os.environ.get("DEXTER3_OM_LADDER_CSV")
    if raw_ladder_csv:
        parsed = _parse_ladder_csv(raw_ladder_csv)
        if parsed is not None:
            kw["ladder_points"] = parsed
        else:
            log_line(f"{utc_now_iso()} ignored invalid DEXTER3_OM_LADDER_CSV={raw_ladder_csv!r}")

    return OMConfig(**kw)


def _governor_config_from_env() -> GovernorConfig:
    """Daily Mission Governor knobs (owner directive 2026-07-07): chase
    $100/day on a $1000 virtual capital base, aggressively but survivably.
    Same "ignored invalid falls back to default" posture as every other
    ``_*_config_from_env`` builder in this file — a typo'd env var must
    never crash the live loop, only leave that one knob at its default.

    Env vars: DEXTER3_CAPITAL_USD, DEXTER3_DAILY_TARGET_USD,
    DEXTER3_DAILY_LOSS_USD, DEXTER3_BASE_RISK_FRAC, DEXTER3_MAX_RISK_FRAC.
    """
    kw: dict[str, Any] = {}
    for env, field_name in (
        ("DEXTER3_CAPITAL_USD", "capital_usd"),
        ("DEXTER3_DAILY_TARGET_USD", "daily_target_usd"),
        ("DEXTER3_DAILY_LOSS_USD", "daily_loss_usd"),
        ("DEXTER3_BASE_RISK_FRAC", "base_risk_frac"),
        ("DEXTER3_MAX_RISK_FRAC", "max_risk_frac"),
    ):
        raw_val = os.environ.get(env)
        if raw_val:
            try:
                kw[field_name] = float(raw_val)
            except ValueError:
                log_line(f"{utc_now_iso()} ignored invalid {env}={raw_val!r}")
    cfg = GovernorConfig(**kw)
    log_line(
        f"{utc_now_iso()} governor config: capital_usd={cfg.capital_usd} "
        f"daily_target_usd={cfg.daily_target_usd} daily_loss_usd={cfg.daily_loss_usd} "
        f"base_risk_frac={cfg.base_risk_frac} max_risk_frac={cfg.max_risk_frac}"
    )
    return cfg


def _edge_gate_config_from_env() -> EdgeGateConfig:
    """ANTI-CHASE sizing gate knobs (owner directive 2026-07-08): the edge-
    discovery sweep proved the "aligned x trending" bucket (chasing a mature
    H1 trend) is the ENTIRE system loss (-116R/6d, 43% of entries) while the
    rest is +85R. Same "ignored invalid falls back to default" posture as
    every other ``_*_config_from_env`` builder in this file.

    Env vars: DEXTER3_ANTICHASE_ENABLED ("0" disables — multiplier always
    1.0, classification still journaled for shadow measurement),
    DEXTER3_ANTICHASE_MULT (downsize factor for the chase bucket, default
    0.15), DEXTER3_ANTICHASE_REGIME_THRESH (directional-efficiency cutoff,
    default 0.35, same as the edge-discovery sweep's ``_regime``).
    """
    kw: dict[str, Any] = {}
    raw_enabled = os.environ.get("DEXTER3_ANTICHASE_ENABLED")
    if raw_enabled is not None:
        kw["enabled"] = raw_enabled.strip() not in ("0", "false", "False", "")
    for env, field_name in (
        ("DEXTER3_ANTICHASE_MULT", "chase_size_mult"),
        ("DEXTER3_ANTICHASE_REGIME_THRESH", "regime_thresh"),
        ("DEXTER3_PULLBACK_MULT", "non_pullback_mult"),
    ):
        raw_val = os.environ.get(env)
        if raw_val:
            try:
                kw[field_name] = float(raw_val)
            except ValueError:
                log_line(f"{utc_now_iso()} ignored invalid {env}={raw_val!r}")
    # Pullback-resumption selector (DEXTER3_PULLBACK_ENABLED "0" disables →
    # non-pullback entries keep full size; classification still journaled).
    raw_pb = os.environ.get("DEXTER3_PULLBACK_ENABLED")
    if raw_pb is not None:
        kw["pullback_enabled"] = raw_pb.strip() not in ("0", "false", "False", "")
    return EdgeGateConfig(**kw)


def _smart_exit_config_from_env() -> SmartExitConfig:
    """SMART ADAPTIVE EXIT knobs (owner directive 2026-07-08): the backtest
    (``scripts/dexter3_edge_discovery.py --smart-exit``, 938 decisions)
    proved a close-confirmed exit (survive noise wicks, cut only on a bar
    CLOSE beyond the structural invalidation, wide disaster hard-stop for
    tail risk) amplifies edge on non-chase buckets but amplifies the LOSS on
    the chase bucket — so it is gated OFF for chase entries (see
    ``dexter3/smart_exit.py``, reuses ``edge_buckets.classify_bucket``'s
    ``is_chase``). Same "ignored invalid falls back to default" posture as
    every other ``_*_config_from_env`` builder in this file.

    Env vars: DEXTER3_SMART_EXIT_ENABLED ("0" disables — every entry keeps
    the tight hard SL regardless of chase classification, but the
    would-have-applied classification is still journaled for shadow
    measurement), DEXTER3_SMART_EXIT_DISASTER_MULT (wide hard-stop
    multiplier on the structural SL distance, default 2.0).
    """
    kw: dict[str, Any] = {}
    raw_enabled = os.environ.get("DEXTER3_SMART_EXIT_ENABLED")
    if raw_enabled is not None:
        kw["enabled"] = raw_enabled.strip() not in ("0", "false", "False", "")
    raw_mult = os.environ.get("DEXTER3_SMART_EXIT_DISASTER_MULT")
    if raw_mult:
        try:
            kw["disaster_mult"] = float(raw_mult)
        except ValueError:
            log_line(f"{utc_now_iso()} ignored invalid DEXTER3_SMART_EXIT_DISASTER_MULT={raw_mult!r}")
    return SmartExitConfig(**kw)


# ---------------------------------------------------------------------------
# PRICE ACTION EYE — Layer 1 bar anatomy (Phase A, 2026-07-15, additive,
# shadow-only). See dexter3/price_action_eye.py for the full detector
# catalog + verdict contract, and docs/DEXTER3_PRICE_ACTION_EYE_DESIGN.md for
# the design. Governing philosophy: detectors are born POWERLESS -- Phase A
# only journals features + a verdict, it NEVER blocks or resizes a trade.
# ---------------------------------------------------------------------------

DEXTER3_PA_EYE_ENV_VAR = "DEXTER3_PA_EYE"
# One-time-per-distinct-value warning cache (process lifetime; mirrors the
# "logged_failure" one-shot pattern already used by _lane_realized_today
# above) so an unrecognized mode does not spam the log every M5 close.
_PA_EYE_MODE_WARNED: set[str] = set()


def _pa_eye_config_from_env() -> "price_action_eye.PriceActionEyeConfig":
    """Price Action Eye Layer-1 threshold knobs — env prefix
    DEXTER3_PA_EYE_* (see price_action_eye.PriceActionEyeConfig's docstring
    for the full list + documented default rationale). Same "ignored
    invalid falls back to default" posture as every other
    ``_*_config_from_env`` builder in this file."""
    kw: dict[str, Any] = {}
    for env, field_name, cast in (
        ("DEXTER3_PA_EYE_WICK_FRAC_MIN", "wick_frac_min", float),
        ("DEXTER3_PA_EYE_TREND_BODY_FRAC_MIN", "trend_body_frac_min", float),
        ("DEXTER3_PA_EYE_DOJI_BODY_FRAC_MAX", "doji_body_frac_max", float),
        ("DEXTER3_PA_EYE_EXHAUSTION_ATR_MULT", "exhaustion_atr_mult", float),
        ("DEXTER3_PA_EYE_ATR_WINDOW", "atr_window", int),
        ("DEXTER3_PA_EYE_SWING_WINDOW", "swing_window", int),
        ("DEXTER3_PA_EYE_SWING_PIVOT_SPAN", "swing_pivot_span", int),
        ("DEXTER3_PA_EYE_NEAR_LEVEL_ATR_DIST", "near_level_atr_dist", float),
        ("DEXTER3_PA_EYE_ROUND_NUMBER_GRID", "round_number_grid", float),
        ("DEXTER3_PA_EYE_ROUND_NUMBER_ATR_FRAC", "round_number_atr_frac", float),
        ("DEXTER3_PA_EYE_ALWAYS_IN_WINDOW", "always_in_window", int),
        ("DEXTER3_PA_EYE_MICROCHANNEL_MIN_BARS", "microchannel_min_bars", int),
        ("DEXTER3_PA_EYE_REJECTION_CLUSTER_MIN", "rejection_cluster_min", int),
        ("DEXTER3_PA_EYE_REJECTION_CLUSTER_LOOKBACK", "rejection_cluster_lookback", int),
        ("DEXTER3_PA_EYE_TRADING_RANGE_OVERLAP_MIN", "trading_range_overlap_min", float),
    ):
        raw = os.environ.get(env)
        if raw:
            try:
                kw[field_name] = cast(raw)
            except ValueError:
                log_line(f"{utc_now_iso()} ignored invalid {env}={raw!r}")
    return price_action_eye.PriceActionEyeConfig(**kw)


def _apply_pa_eye_shadow(decision: "hunter_brain.Decision", prefix: list[dict[str, Any]]) -> None:
    """Price Action Eye Phase A wiring (2026-07-15, additive, both lanes).

    Called for EVERY decided ``enter`` (fable/v16, grok, hunt-mode, and the
    VP producer all route through the single decision-source block in
    ``run_symbol_cycle`` this is invoked from -- one call site covers both
    lanes since the lane identity only changes ``DEXTER3_MODE``, not this
    code path) -- BEFORE the caller's ``journal.insert_decision`` so the
    existing journaling captures ``pa_eye`` with ZERO DecisionJournal
    changes (``features_json`` is a one-time snapshot taken at insert time;
    see ``decision_journal.py``'s ``insert_decision`` -- stamping the
    feature any later, e.g. beside the v16 entry-quality gate further down
    in this function, would silently miss the journal row).

    ``DEXTER3_PA_EYE`` modes:
      off (default) -- does nothing, zero cost, no evaluate() call.
      shadow        -- journals features + verdict; NEVER affects the
                       decision, sizing, or execution.
      anything else (typo, or a future 'veto'/'lens' value not yet wired)
                    -- treated as shadow (fail-safe default: an
                       unrecognized mode must never silently gate a live
                       decision before the design doc's replay-gate
                       promotes it) with a ONE-TIME log note per distinct
                       value.

    Fail-open: ANY exception out of ``price_action_eye.evaluate`` is caught
    here, stamps ``{"error": str(exc)}`` in its place, and NEVER raises --
    a bug in the Eye must never block or crash a decision (same posture as
    every other ``_apply_*`` gate in this file, e.g.
    ``_apply_v18_size_levers``'s governor-cap fallback).
    """
    mode = os.environ.get(DEXTER3_PA_EYE_ENV_VAR, "off").strip().lower()
    if mode == "off":
        return
    if mode != "shadow" and mode not in _PA_EYE_MODE_WARNED:
        _PA_EYE_MODE_WARNED.add(mode)
        log_line(
            f"{utc_now_iso()} pa-eye: unrecognized {DEXTER3_PA_EYE_ENV_VAR}={mode!r} "
            "-- mode not yet promoted, running shadow"
        )
    try:
        cfg = _pa_eye_config_from_env()
        result = price_action_eye.evaluate(prefix, decision.side, cfg=cfg)
        if isinstance(decision.features, dict):
            decision.features["pa_eye"] = result
        log_line(
            f"{utc_now_iso()} {decision.symbol} pa-eye: verdict={result.get('verdict')} "
            f"side={decision.side} reasons={result.get('verdict_reasons')}"
        )
    except Exception as exc:  # noqa: BLE001 - the Eye must never block/crash a decision
        if isinstance(decision.features, dict):
            decision.features["pa_eye"] = {"error": str(exc)}
        log_line(f"{utc_now_iso()} {decision.symbol} pa-eye_failed (fail-open, no trade impact): {exc}")


def _fast_tick_sec_from_env() -> int:
    raw = os.environ.get("DEXTER3_FAST_TICK_SEC")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            log_line(f"{utc_now_iso()} ignored invalid DEXTER3_FAST_TICK_SEC={raw!r}")
    return DEFAULT_FAST_TICK_SEC


# ---------------------------------------------------------------------------
# FIX 2 (2026-07-07) — per-basket peak-R runtime state (persisted in the
# shadow state file, keyed by the basket's oldest_open_ts so it survives a
# loop restart and naturally resets when a brand-new basket opens under a
# different oldest leg).
# ---------------------------------------------------------------------------


def _basket_runtime_for(
    state: dict[str, Any], symbol: str, oldest_open_ts: str | None, aggregate_r: float
) -> dict[str, Any]:
    """Return the up-to-date basket_runtime dict for ``symbol``'s current
    lane basket, updating peak_r = max(previous_peak_r, aggregate_r) BEFORE
    returning it (basket_live._resolve_profit_action expects the caller to
    have already folded this bar's aggregate_r into peak_r — see its
    docstring). Persists into ``state`` (caller must still call
    ``save_shadow_state``); resets to a fresh peak_r=aggregate_r whenever the
    basket's oldest_open_ts changes (a new basket opened — the previous
    basket's peak must never leak into a new one) or is absent (lane just
    went flat then reopened).
    """
    runtimes = state.setdefault("basket_runtime", {})
    existing = runtimes.get(symbol) or {}
    if not oldest_open_ts or existing.get("oldest_open_ts") != oldest_open_ts:
        # New basket (or first observation) — fresh peak starting at this bar.
        runtime = {"oldest_open_ts": oldest_open_ts, "peak_r": aggregate_r}
    else:
        runtime = dict(existing)
        runtime["peak_r"] = max(_f(existing.get("peak_r"), aggregate_r), aggregate_r)
    runtimes[symbol] = runtime
    return runtime


def _clear_basket_runtime(state: dict[str, Any], symbol: str, grok: bool = False) -> None:
    """Reset a symbol's basket_runtime once its lane goes flat.

    Supports independent state for Grok v1.0 vs V1.6.
    """
    key = "grok_v10_basket_runtime" if grok else "basket_runtime"
    runtimes = state.setdefault(key, {})
    runtimes.pop(symbol, None)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _daily_state(state: dict[str, Any]) -> dict[str, Any]:
    """Per-UTC-day counters persisted in the shadow state file: live entries
    placed + resolved losing baskets. Feeds the executor's daily caps (which
    were silently ineffective before — the runner always passed 0)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = state.setdefault("daily", {})
    if daily.get("date") != today:
        daily.clear()
        daily.update({"date": today, "entries": 0, "loss_baskets": 0})
    return daily


def _governor_state_for(state: dict[str, Any]) -> dict[str, Any]:
    """Per-UTC-day Daily Mission Governor state persisted in the shadow
    state file: ``{date, state, locked_pnl}``. Once TARGET_LOCKED or
    LOSS_STOPPED fires it STAYS for the rest of the UTC day even if a later
    tick's effective PnL would otherwise read back as HUNTING (e.g. floating
    PnL wobbles back under the target after a close-all) — the day is over,
    full stop. Automatically re-arms (resets to HUNTING) the moment the UTC
    date rolls over, same rollover mechanics as ``_daily_state`` above.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    gov = state.setdefault("governor", {})
    if gov.get("date") != today:
        gov.clear()
        gov.update({"date": today, "state": "HUNTING", "locked_pnl": None})
    return gov


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        log_line(f"{utc_now_iso()} ignored invalid {name}={raw!r}")
        return default


def _env_csv_set(name: str, default: set[str]) -> set[str]:
    raw = os.environ.get(name)
    if raw is None:
        return set(default)
    return {part.strip() for part in raw.split(",") if part.strip()}


def _is_grok_mode() -> bool:
    return os.environ.get("DEXTER3_MODE", "v16").lower().strip() == "grok"


V16_WINNER_SETUPS_DEFAULT: set[str] = {
    "hunt_h1_context",
    "hunt_swing_structure",
    "basket_repair",
    "opening_manager_repair",
}
V16_WEAK_SETUPS_DEFAULT: set[str] = {
    "hunt_m15_drift",
    "hunt_day_range_tilt",
    "hunt_sweep_reclaim",
}


def _effective_lane_pnl_today(mcp: Dexter3McpClient, state: dict[str, Any]) -> tuple[float, float, float]:
    realized, _ = _lane_realized_today(mcp, label_filter=_active_label_family())
    floating_by_symbol = (state.get("governor") or {}).get("floating_by_symbol") or {}
    floating = sum(_f(v, 0.0) for v in floating_by_symbol.values())
    return realized + floating, realized, floating


def _v16_entry_quality_config_from_env() -> V16EntryQualityConfig:
    """V1.6 entry-quality pro-pack knobs. Grok mode never consults this."""
    kw: dict[str, Any] = {}
    raw_enabled = os.environ.get("DEXTER3_V16_ENTRY_QUALITY_ENABLED")
    if raw_enabled is not None:
        kw["enabled"] = raw_enabled.strip() not in ("0", "false", "False", "")
    raw_chase = os.environ.get("DEXTER3_V16_CHASE_HARD_BLOCK")
    if raw_chase is not None:
        kw["chase_hard_block"] = raw_chase.strip() not in ("0", "false", "False", "")
    raw_chase_bucket = os.environ.get("DEXTER3_V17_BLOCK_ALIGNED_TRENDING_CHASE_BYPASS")
    if raw_chase_bucket is not None:
        kw["block_chase_bypass_on_aligned_trending"] = raw_chase_bucket.strip() not in (
            "0",
            "false",
            "False",
        )
    raw_weak = os.environ.get("DEXTER3_V16_WEAK_HARD_SKIP")
    if raw_weak is not None:
        kw["weak_hard_skip"] = raw_weak.strip() not in ("0", "false", "False", "")
    raw_cd = os.environ.get("DEXTER3_V16_COOLDOWN_ENABLED")
    if raw_cd is not None:
        kw["cooldown_enabled"] = raw_cd.strip() not in ("0", "false", "False", "")
    for env, field_name in (
        ("DEXTER3_V18_WINNER_BOOST_ENABLED", "winner_boost_enabled"),
        ("DEXTER3_V18_CHASE_RESCUE_ENABLED", "chase_rescue_enabled"),
        ("DEXTER3_V18_B_TIER_ENABLED", "b_tier_enabled"),
    ):
        raw_v18 = os.environ.get(env)
        if raw_v18 is not None:
            kw[field_name] = raw_v18.strip() not in ("0", "false", "False", "")
    for env, field_name in (
        ("DEXTER3_V16_MIN_LEADER_SCORE", "min_leader_score"),
        ("DEXTER3_V16_CHASE_BYPASS_SCORE", "chase_bypass_score"),
        ("DEXTER3_V16_WEAK_MIN_SCORE", "weak_min_score"),
        ("DEXTER3_V16_COOLDOWN_BYPASS_SCORE", "bypass_score"),
        ("DEXTER3_V16_COOLDOWN_BYPASS_WINNER_SCORE", "bypass_winner_score"),
        ("DEXTER3_V16_COOLDOWN_BYPASS_EXCEPTIONAL", "bypass_exceptional_score"),
        ("DEXTER3_V16_COOLDOWN_NOISE_LIVE_R", "cooldown_noise_live_r"),
        ("DEXTER3_V18_WINNER_BOOST_MULT", "winner_boost_mult"),
        ("DEXTER3_V18_CHASE_RESCUE_FLOOR", "chase_rescue_floor_frac"),
        ("DEXTER3_V18_B_TIER_MIN_SCORE", "b_tier_min_score"),
        ("DEXTER3_V18_B_TIER_MULT", "b_tier_mult"),
    ):
        raw_val = os.environ.get(env)
        if raw_val:
            try:
                kw[field_name] = float(raw_val)
            except ValueError:
                log_line(f"{utc_now_iso()} ignored invalid {env}={raw_val!r}")
    for env, field_name in (
        ("DEXTER3_V16_COOLDOWN_SEC", "cooldown_sec"),
        ("DEXTER3_V16_MCP_MAX_CONSEC_ERRORS", "mcp_max_consec_errors"),
    ):
        raw_val = os.environ.get(env)
        if raw_val:
            try:
                kw[field_name] = int(raw_val)
            except ValueError:
                log_line(f"{utc_now_iso()} ignored invalid {env}={raw_val!r}")
    return V16EntryQualityConfig(**kw)


def _apply_v18_size_levers(
    quality: dict[str, Any], governor_risk_usd: float, chain_risk_usd: float
) -> float:
    """V1.8 size-the-edge: apply the gate's size_mult / size_floor_frac to the
    post-chain risk, hard-capped at the governor's max risk fraction of
    capital. Returns chain_risk_usd unchanged when the levers are off (their
    defaults) — byte-identical V1.7 sizing."""
    mult = float(quality.get("size_mult") or 1.0)
    floor_frac = float(quality.get("size_floor_frac") or 0.0)
    risk = float(chain_risk_usd)
    if mult == 1.0 and floor_frac <= 0.0:
        return risk
    risk = risk * mult
    if floor_frac > 0.0:
        risk = max(risk, float(governor_risk_usd) * floor_frac)
    try:
        gcfg = _get_governor().config
        cap = float(gcfg.capital_usd) * float(gcfg.max_risk_frac)
    except Exception:  # noqa: BLE001 - cap fallback must never block sizing
        cap = 25.0
    risk = min(risk, cap)
    if risk != float(chain_risk_usd):
        log_line(
            f"{utc_now_iso()} v18-size: mult={mult} floor_frac={floor_frac} "
            f"risk_usd {chain_risk_usd:.2f}->{risk:.2f} (cap={cap:.2f})"
        )
    return risk


def _note_mcp_error(state: dict[str, Any], *, ok: bool = False) -> int:
    """Track consecutive MCP failures for entry pause. Returns current count."""
    if ok:
        state["mcp_consec_errors"] = 0
        return 0
    n = int(state.get("mcp_consec_errors") or 0) + 1
    state["mcp_consec_errors"] = n
    return n


def _apply_v16_entry_quality_gate(
    state: dict[str, Any],
    decision: hunter_brain.Decision,
) -> dict[str, Any]:
    """V1.6-only entry gate. Grok always passes above its own leader_score
    floor (DEXTER3_GROK_MIN_LEADER_SCORE — grok bypasses the v16 gate
    entirely otherwise, so a near-zero leader_score signal was reaching
    live entry unfiltered, e.g. leader_score=0.056 on 2026-07-15). Journals
    features on decision."""
    if _is_grok_mode():
        leader_score = float(getattr(decision, "leader_score", 0.0) or 0.0)
        min_leader_score = _env_float("DEXTER3_GROK_MIN_LEADER_SCORE", 0.10)
        if min_leader_score > 0.0 and leader_score < min_leader_score:
            return {
                "allow": False,
                "reason": "grok_min_leader_score",
                "a_plus": False,
                "a_plus_reason": "",
                "cooldown_bypassed": False,
                "features": {
                    "grok_bypass": True,
                    "leader_score": leader_score,
                    "min_leader_score": min_leader_score,
                },
            }
        return {
            "allow": True,
            "reason": "grok_bypass",
            "a_plus": False,
            "a_plus_reason": "",
            "cooldown_bypassed": False,
            "features": {
                "grok_bypass": True,
                "leader_score": leader_score,
                "min_leader_score": min_leader_score,
            },
        }
    cfg = _v16_entry_quality_config_from_env()
    result = evaluate_v16_entry_gate(
        decision=decision,
        state=state,
        mcp_consec_errors=int(state.get("mcp_consec_errors") or 0),
        now_iso=utc_now_iso(),
        cfg=cfg,
    )
    if isinstance(decision.features, dict):
        decision.features["v16_entry_quality"] = result.get("features") or {}
        decision.features["v16_entry_quality"]["gate_reason"] = result.get("reason")
        decision.features["v16_entry_quality"]["allow"] = result.get("allow")
        decision.features["v16_entry_quality"]["cooldown_bypassed"] = result.get("cooldown_bypassed")
    log_line(
        f"{utc_now_iso()} {decision.symbol} v16-entry-quality: allow={result.get('allow')} "
        f"reason={result.get('reason')} a_plus={result.get('a_plus')} "
        f"a_plus_reason={result.get('a_plus_reason') or '-'} "
        f"cooldown_bypassed={result.get('cooldown_bypassed')} "
        f"score={getattr(decision, 'leader_score', 0):.3f} setup={getattr(decision, 'setup', '')}"
    )
    return result


def _apply_v16_profit_controls(
    mcp: Dexter3McpClient,
    state: dict[str, Any],
    decision: hunter_brain.Decision,
    base_risk_usd: float,
) -> float:
    """V1.6-only profit controls.

    Rules, from the 2026-07-09 owner audit:
    - weak buckets are scout-sized immediately;
    - winner buckets scale only after the lane's realized+floating day PnL is green;
    - house-money mode presses winners harder only after a larger green cushion;
    - Grok mode bypasses this layer completely.
    """
    if _is_grok_mode() or not _env_bool("DEXTER3_V16_PROFIT_CONTROLS_ENABLED", True):
        return base_risk_usd
    try:
        setup = str(getattr(decision, "setup", "") or "")
        winner_setups = _env_csv_set("DEXTER3_V16_WINNER_SETUPS", V16_WINNER_SETUPS_DEFAULT)
        weak_setups = _env_csv_set("DEXTER3_V16_WEAK_SETUPS", V16_WEAK_SETUPS_DEFAULT)
        green_threshold = _env_float("DEXTER3_V16_GREEN_THRESHOLD_USD", 20.0)
        house_threshold = _env_float("DEXTER3_V16_HOUSE_THRESHOLD_USD", 30.0)
        weak_mult = max(0.0, _env_float("DEXTER3_V16_WEAK_MULT", 0.25))
        winner_mult = max(0.0, _env_float("DEXTER3_V16_WINNER_MULT", 2.0))
        house_mult = max(0.0, _env_float("DEXTER3_V16_HOUSE_MULT", 3.0))
        max_mult = max(0.0, _env_float("DEXTER3_V16_MAX_EDGE_MULT", 3.0))
        effective, realized, floating = _effective_lane_pnl_today(mcp, state)

        reason = "neutral_no_scale"
        mult = 1.0
        if setup in weak_setups:
            mult = weak_mult
            reason = "weak_bucket_downsize"
        elif setup in winner_setups:
            if effective >= house_threshold:
                mult = house_mult
                reason = "house_money_winner_scale"
            elif effective >= green_threshold:
                mult = winner_mult
                reason = "green_day_winner_scale"
            else:
                reason = "winner_waiting_for_green_day"

        mult = min(mult, max_mult)
        risk = round(float(base_risk_usd) * mult, 4)
        meta = {
            "setup": setup,
            "reason": reason,
            "effective_pnl": round(effective, 4),
            "realized_pnl": round(realized, 4),
            "floating_pnl": round(floating, 4),
            "green_threshold": green_threshold,
            "house_threshold": house_threshold,
            "mult": mult,
            "base_risk_usd": base_risk_usd,
            "gated_risk_usd": risk,
        }
        if isinstance(decision.features, dict):
            decision.features["v16_profit_control"] = meta
        log_line(
            f"{utc_now_iso()} {decision.symbol} v16-profit-control: setup={setup} "
            f"reason={reason} effective={effective:.2f} mult={mult} "
            f"risk_usd {base_risk_usd:.2f}->{risk:.2f}"
        )
        return risk
    except Exception as exc:  # noqa: BLE001 - risk shaping must never block an entry
        log_line(f"{utc_now_iso()} {decision.symbol} v16_profit_controls_failed (ungated risk used): {exc}")
        return base_risk_usd


def _apply_v16_house_money_status(gov_state: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    """Arm a green-day floor and lock the day if giveback reaches it.

    This is deliberately outside DailyGovernor's pure $100 target / $50 loss
    contract so V1.6 can run the house-money experiment without changing Grok
    or the base governor semantics.
    """
    if _is_grok_mode() or not _env_bool("DEXTER3_V16_HOUSE_MONEY_ENABLED", True):
        return status
    effective = float(status.get("effective_pnl") or 0.0)
    house_threshold = _env_float("DEXTER3_V16_HOUSE_THRESHOLD_USD", 30.0)
    floor_usd = _env_float("DEXTER3_V16_HOUSE_FLOOR_USD", 20.0)
    if effective >= house_threshold:
        if not gov_state.get("house_money_armed"):
            log_line(
                f"{utc_now_iso()} governor house-money armed: effective={effective:.2f} "
                f"threshold={house_threshold:.2f} floor={floor_usd:.2f}"
            )
        gov_state["house_money_armed"] = True
        gov_state["house_money_floor_usd"] = max(float(gov_state.get("house_money_floor_usd") or 0.0), floor_usd)
    if (
        gov_state.get("house_money_armed")
        and str(status.get("state")) == "HUNTING"
        and effective <= float(gov_state.get("house_money_floor_usd") or floor_usd)
    ):
        floor = float(gov_state.get("house_money_floor_usd") or floor_usd)
        locked = dict(status)
        locked.update(
            {
                "state": "TARGET_LOCKED",
                "target": floor,
                "house_money_floor_triggered": True,
                "house_money_floor_usd": floor,
                "reason": (
                    f"effective_pnl {effective:.2f} <= house_money_floor {floor:.2f} "
                    "-> locking green day floor"
                ),
            }
        )
        return locked
    return status

# ---------------------------------------------------------------------------
# Daily Mission Governor (owner directive 2026-07-07) — realized PnL cache
# ---------------------------------------------------------------------------
# get_deals is a real MCP read (network round-trip) and must never be called
# every ~4s fast tick — it is cached for LANE_REALIZED_CACHE_SEC and reused
# across ticks, same "cheap by default, refetch only when stale" posture as
# _OM_BAR_CACHE below. Keyed by nothing (single account) — module-level like
# every other cross-tick cache in this file.
LANE_REALIZED_CACHE_SEC = 60
_LANE_REALIZED_CACHE: dict[str, dict[str, Any]] = {}


def _lane_realized_today(mcp: Dexter3McpClient, label_filter: str = FABLE_LABEL_FAMILY) -> tuple[float, list[float]]:
    """Sum of today's (UTC) realized netProfit for our lane + the ordered
    list of those close PnLs (oldest -> newest), both derived from
    ``get_deals``. Cached for ``LANE_REALIZED_CACHE_SEC`` — callers on the
    fast (~4s) tick path must never trigger a fresh MCP read every tick. The
    cache is ALSO invalidated the instant the UTC calendar date rolls over,
    even inside the 60s window: a fetch made at 23:59:5xZ used to keep
    serving yesterday's already-computed "today" sum for up to 60s into the
    new UTC day, and the governor read that stale total as the NEW day's
    realized PnL — LOSS_STOPPED fired on a fresh 19-second-old position at
    00:00:54Z on 2026-07-15 counting the PREVIOUS day's losses (cross-
    midnight cache staleness, not a real loss on the new day).

    On an MCP failure, returns the last cached value; if there has never
    been a successful read, returns (0.0, []) and logs the failure exactly
    once (not every tick) so a persistent outage does not spam the log.

    ``label_filter`` (2026-07-15 versioned-labels design): a FAMILY prefix
    (e.g. "dexter3:fable"/pass ``_active_label_family()``), matched via
    ``label_matches_family`` — NOT the exact current versioned label. Passing
    the exact versioned label here was the bug: a mid-day
    ``DEXTER3_FABLE_VERSION`` bump would make the governor stop matching the
    SAME lane's own earlier-today deals (closed under the OLD version
    string), silently forgetting real realized PnL — the same shape of bug as
    the cross-midnight cache staleness this function already guards against.
    """
    now_epoch = datetime.now(timezone.utc).timestamp()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache = _LANE_REALIZED_CACHE.setdefault(
        label_filter, {"epoch": 0.0, "date": "", "sum": 0.0, "pnls": [], "logged_failure": False}
    )
    if (
        cache.get("date") == today
        and now_epoch - float(cache.get("epoch", 0.0)) < LANE_REALIZED_CACHE_SEC
        and cache.get("epoch", 0.0) > 0
    ):
        return float(cache["sum"]), list(cache["pnls"])

    # Strict UTC-midnight clamp for the numeric-timestamp path below (used
    # when a deal carries execution_timestamp_ms/executionTimestamp) — a
    # precise ">= today 00:00:00Z" boundary the string-prefix check further
    # down cannot express and which is immune to timestamp-format drift.
    today_start_ms = int(
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )
    try:
        # H1 fix (2026-07-15 cross-lane entanglement audit): thread the
        # already-computed UTC-day-start floor into get_deals so a deal-heavy
        # lane can never push the OTHER lane's earlier-today closes out of
        # this shared count=500 window before the per-deal filters below ever
        # see them. The local-MCP transport accepts and drops this param (see
        # Dexter3McpClient.get_deals); the per-deal date/label filters below
        # remain the correctness backstop on that path.
        deals = mcp.get_deals(count=500, from_timestamp_ms=today_start_ms)
    except (McpClientError, McpZombieError) as exc:
        if not cache.get("logged_failure"):
            log_line(f"{utc_now_iso()} governor lane_realized_today get_deals_failed (using cached/zero): {exc}")
            cache["logged_failure"] = True
        return float(cache.get("sum", 0.0)), list(cache.get("pnls", []))

    dated: list[tuple[str, float]] = []
    for deal in deals:
        if not isinstance(deal, dict):
            continue
        label = str(deal.get("label") or deal.get("comment") or "")
        if not label_matches_family(label, label_filter):
            continue
        ts = str(
            deal.get("execution_utc")  # normalized client shape (openapi daemon path)
            or deal.get("time")  # the Local MCP's actual field (live-verified 2026-07-07)
            or deal.get("executionTimestamp")
            or deal.get("closeTimestamp")
            or deal.get("closingTimestamp")
            or deal.get("utcLastUpdateTimestamp")
            or ""
        )
        # Prefer a numeric UTC epoch-ms clamp over the ts string-prefix
        # check when available (execution_timestamp_ms / executionTimestamp)
        # — see today_start_ms above.
        exec_ms = None
        for ms_key in ("execution_timestamp_ms", "executionTimestamp"):
            raw_ms = deal.get(ms_key)
            if raw_ms:
                try:
                    exec_ms = int(raw_ms)
                    break
                except (TypeError, ValueError):
                    continue
        if exec_ms is not None:
            if exec_ms < today_start_ms:
                continue
        elif not ts.startswith(today):
            continue
        pnl = None
        for key in ("netProfit", "profit", "grossProfit", "pnl", "closedNetProfit"):
            raw = deal.get(key)
            if raw is None:
                continue
            try:
                pnl = float(raw)
                break
            except (TypeError, ValueError):
                continue
        if pnl is None or pnl == 0.0:
            # zero-pnl rows are the OPEN side of a deal — including them left
            # a trailing 0 that zeroed the win streak (live 2026-07-07:
            # streak=0 through an 11-0 run, ladder never pressed)
            continue
        dated.append((ts, pnl))

    dated.sort(key=lambda pair: pair[0])
    pnls = [p for _, p in dated]
    total = sum(pnls)

    cache["epoch"] = now_epoch
    cache["date"] = today
    cache["sum"] = total
    cache["pnls"] = pnls
    cache["logged_failure"] = False
    return float(total), list(pnls)


# -- Phase 2: periodic learning-loop refresh ---------------------------------
# "every N cycles (default every 15 min)" per spec section 5; expressed in
# cycles (not minutes) because run_once has no wall-clock of its own — the
# caller's poll interval determines how many cycles fit in 15 minutes.
LEARNING_REFRESH_MINUTES = 15


def _learning_refresh_every_cycles(poll_sec: int) -> int:
    return max(1, int((LEARNING_REFRESH_MINUTES * 60) / max(1, poll_sec)))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# single-instance lock (own lock file — never the live loops' lock)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_loop_lock(mode: str = "v16") -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if mode == "grok":
        lock_file, lock_name = GROK_LOCK_FILE, "grok-v1.0"
    elif mode == "vp":
        lock_file, lock_name = VP_LOCK_FILE, "vp-canary"
    else:
        lock_file, lock_name = LOCK_FILE, "dexter3"
    if lock_file.exists():
        try:
            old_pid = int(lock_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old_pid = 0
        if old_pid > 0 and _pid_alive(old_pid):
            raise SystemExit(f"another {lock_name} shadow loop is running (pid={old_pid})")
        # stale lock (pid dead or unreadable) — take it over
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass
    lock_file.write_text(str(os.getpid()), encoding="utf-8")


def release_loop_lock(mode: str = "v16") -> None:
    if mode == "grok":
        lock_file = GROK_LOCK_FILE
    elif mode == "vp":
        lock_file = VP_LOCK_FILE
    else:
        lock_file = LOCK_FILE
    try:
        if lock_file.exists() and lock_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock_file.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# shadow state (last-seen M5 close id per symbol) — M5-close detection
# ---------------------------------------------------------------------------


def load_shadow_state() -> dict[str, Any]:
    state_file = _active_state_file()
    if not state_file.exists():
        return {"symbols": {}}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"symbols": {}}
        data.setdefault("symbols", {})
        return data
    except (OSError, json.JSONDecodeError):
        return {"symbols": {}}


def save_shadow_state(state: dict[str, Any]) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    state_file = _active_state_file()
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(state_file)


def is_new_m5_close(state: dict[str, Any], symbol: str, bars_m5: list[dict[str, Any]]) -> tuple[bool, str | None]:
    """Return (is_new, close_ts) for the last bar in bars_m5 vs. the tracked state.

    Pure w.r.t. its inputs except for reading ``state`` — does not mutate
    it. Callers must call ``mark_m5_close_seen`` after successfully
    processing the close, so a crash mid-processing does not silently skip
    the bar on the next tick.
    """
    if not bars_m5:
        return False, None
    last_ts = str(bars_m5[-1].get("ts") or "")
    if not last_ts:
        return False, None
    sym_state = state.get("symbols", {}).get(symbol, {})
    last_seen = str(sym_state.get("last_m5_close_ts") or "")
    return last_ts != last_seen, last_ts


def mark_m5_close_seen(state: dict[str, Any], symbol: str, close_ts: str) -> None:
    symbols = state.setdefault("symbols", {})
    sym_state = symbols.setdefault(symbol, {})
    sym_state["last_m5_close_ts"] = close_ts
    sym_state["last_seen_at"] = utc_now_iso()


def _iso_to_epoch(ts: str) -> float:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0


FRESHNESS_GRACE_SEC = 10
FRESHNESS_RETRIES = 3
FRESHNESS_RETRY_SLEEP_SEC = 7
FRESHNESS_MAX_GAP_SEC = 1800  # newest bar older than this = market closed/idle, no retries
MAX_CATCHUP_BARS = 6


def pending_m5_closes(
    state: dict[str, Any], symbol: str, bars_m5: list[dict[str, Any]], max_catchup: int = MAX_CATCHUP_BARS
) -> list[int]:
    """Indices (oldest→newest) of completed bars not yet decided.

    The blueprint contract is one decision per M5 close with no silent gaps,
    so a data gap must yield ALL missed bars, not only the newest. First run
    for a symbol decides only the newest bar (no history replay); gaps larger
    than ``max_catchup`` are truncated to the newest bars so a long MCP outage
    cannot trigger a replay storm.
    """
    if not bars_m5:
        return []
    sym_state = state.get("symbols", {}).get(symbol, {})
    last_seen = str(sym_state.get("last_m5_close_ts") or "")
    if not last_seen:
        return [len(bars_m5) - 1]
    pending = [i for i, b in enumerate(bars_m5) if str(b.get("ts") or "") > last_seen]
    return pending[-max_catchup:]


def fetch_fresh_m5(mcp: Dexter3McpClient, symbol: str, count: int = MIN_M5_BARS) -> list[dict[str, Any]]:
    """Fetch M5 bars, retrying briefly when the newest completed bar is missing.

    The local MCP can serve a snapshot one full bar stale (observed
    2026-07-05: the 08:15 bar stayed absent until 08:25 while the 08:20 bar
    then appeared within 19s). When ``now`` is past a boundary plus grace and
    the response lacks that bar, refetch with a varied ``count`` so any
    request-shaped cache is bypassed. Idle markets (weekend XAU) are exempt:
    retries fire only when the newest bar is within FRESHNESS_MAX_GAP_SEC of
    the expected one.
    """
    bars = mcp.get_trendbars(symbol, "m5", count)
    for attempt in range(1, FRESHNESS_RETRIES + 1):
        if not bars:
            return bars
        now_epoch = datetime.now(timezone.utc).timestamp()
        expected_open = (int(now_epoch - FRESHNESS_GRACE_SEC) // M5_BAR_SEC * M5_BAR_SEC) - M5_BAR_SEC
        gap = expected_open - _iso_to_epoch(str(bars[-1].get("ts") or ""))
        if gap <= 0 or gap > FRESHNESS_MAX_GAP_SEC:
            return bars
        time.sleep(FRESHNESS_RETRY_SLEEP_SEC)
        bars = mcp.get_trendbars(symbol, "m5", count + attempt)
    if bars:
        log_line(
            f"{utc_now_iso()} {symbol} data_stale newest_m5={bars[-1].get('ts')} "
            f"(expected newer bar; will catch up on a later poll)"
        )
    return bars[-count:] if len(bars) > count else bars


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------


def log_line(text: str) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    print(text)
    with _active_log_file().open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def log_decision_line(decision: hunter_brain.Decision, late_sec: float = 0.0) -> None:
    reasons_short = "; ".join(decision.reasons)[:200]
    late_txt = f" late={int(late_sec)}s" if late_sec > 90 else ""
    text = (
        f"{utc_now_iso()} {decision.symbol} action={decision.action} setup={decision.setup} "
        f"leader_score={decision.leader_score:.3f} p_win={decision.p_win_est:.3f}{late_txt} "
        f"reasons={reasons_short}"
    )
    log_line(text)


def log_error(context: str, exc: BaseException) -> None:
    log_line(f"{utc_now_iso()} ERROR {context}: {exc!r}")
    tb = traceback.format_exc()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with _active_log_file().open("a", encoding="utf-8") as fh:
        fh.write(tb + "\n")


# ---------------------------------------------------------------------------
# paper basket per symbol (simulated fills — NO real orders, ever)
# ---------------------------------------------------------------------------


class PaperBasket:
    """Wraps a BasketManager with simple simulated-fill bookkeeping.

    Phase 1 only needs the basket state machine to be EXERCISED so its
    telemetry accumulates in the journal; fills are simulated at the
    decision's entry price and PnL is tracked in R-multiples using the
    decision's own sl distance as the risk unit.
    """

    def __init__(self, symbol: str, journal: DecisionJournal) -> None:
        self.symbol = symbol
        self.journal = journal
        self.manager = BasketManager(BasketConfig())
        self._now_min = 0.0

    def advance_clock(self, minutes: float) -> None:
        self._now_min += minutes

    def on_decision(self, decision: hunter_brain.Decision) -> None:
        if decision.action != "enter" or decision.entry is None or decision.sl is None:
            return
        risk_usd = abs(decision.entry - decision.sl)
        if self.manager.state.state == "FLAT":
            grok_active = os.environ.get("DEXTER3_MODE", "v16").lower().strip() == "grok"
            leg = Leg(
                side=str(decision.side),
                entry=float(decision.entry),
                sl=float(decision.sl),
                risk_usd=risk_usd,
                opened_at_min=self._now_min,
                label=GROK_LABEL if (grok_active and GROK_LABEL) else f"{LIVE_ORDER_LABEL}:{decision.setup}",
            )
            result = self.manager.on_entry(leg)
            self.journal.insert_basket_event(
                self.manager.state.basket_id or 0,
                "on_entry",
                {"decision_action": decision.action, "result": result.to_dict()},
                label=_active_order_label(),
            )

    def on_m5_close_tick(self, current_price: float | None) -> None:
        if self.manager.state.state == "FLAT" or current_price is None:
            return
        legs = self.manager.state.legs
        if not legs:
            return
        base_risk = self.manager.state.base_risk_usd or 1.0
        # Simple aggregate PnL proxy: mean directional distance across legs,
        # expressed in R relative to the basket's base risk. This is a
        # shadow-only approximation — Phase 2 replaces it with broker PnL.
        total_r = 0.0
        for leg in legs:
            direction = 1.0 if leg.side == "buy" else -1.0
            pnl = direction * (current_price - leg.entry)
            total_r += pnl / base_risk if base_risk else 0.0
        aggregate_r = total_r / len(legs)
        result = self.manager.on_m5_close(
            bars=[],
            features={},
            positions_pnl={"now_min": self._now_min, "aggregate_r": aggregate_r, "structure_evidence": {}},
        )
        if result.action != "none":
            self.journal.insert_basket_event(
                self.manager.state.basket_id or 0,
                "on_m5_close",
                {"result": result.to_dict(), "aggregate_r": aggregate_r},
                label=_active_order_label(),
            )


# ---------------------------------------------------------------------------
# one cycle per symbol
# ---------------------------------------------------------------------------


def _completed_by(bar_ts: str, close_epoch: float, tf_min: int) -> bool:
    """True when a bar (labeled by open ts) has fully closed by ``close_epoch``."""
    ts_epoch = _iso_to_epoch(bar_ts)
    return ts_epoch > 0 and (ts_epoch + tf_min * 60) <= close_epoch + 1e-6


def run_symbol_cycle(
    mcp: Dexter3McpClient,
    journal: DecisionJournal,
    baskets: dict[str, PaperBasket],
    state: dict[str, Any],
    symbol: str,
    *,
    executor: Dexter3Executor | None = None,
    journal_stats: dict[str, Any] | None = None,
) -> str:
    """Run one evaluation cycle for ``symbol``. Returns a short status string.

    Decides EVERY pending completed M5 bar (catch-up), not only the newest.
    Catch-up decisions use only bars completed by that close (no lookahead in
    M5 prefix or M15/H1 context) and carry their lateness in the log line.

    ``executor`` is optional (None in Phase 1 shadow mode and whenever
    --live is absent — see ``main``/``run_loop``). When provided, it is
    invoked ONLY for the newest bar's ``enter`` decisions — historical
    catch-up bars never place a live order (that would be a backfill/
    lookahead order against a price that has already moved on).
    """
    if _env_bool("DEXTER3_WEEKEND_FLATTEN_ENABLED", False):
        weekly = weekly_close_policy(datetime.now(timezone.utc))
        if bool(weekly["block_entries"]):
            return f"weekly_entry_blocked:{weekly['reason']}"

    m5_bars = fetch_fresh_m5(mcp, symbol)
    if len(m5_bars) < MIN_M5_BARS:
        return f"insufficient_m5_bars({len(m5_bars)})"

    pending = pending_m5_closes(state, symbol, m5_bars)
    if not pending:
        return "no_new_m5_close"

    m15_bars = mcp.get_trendbars(symbol, "m15", M15_BARS_NEEDED)
    h1_bars = mcp.get_trendbars(symbol, "h1", H1_BARS_NEEDED)

    statuses: list[str] = []
    for i in pending:
        bar_ts = str(m5_bars[i].get("ts") or "")
        close_epoch = _iso_to_epoch(bar_ts) + M5_BAR_SEC
        late_sec = max(0.0, datetime.now(timezone.utc).timestamp() - close_epoch)
        prefix = m5_bars[: i + 1]
        m15_ctx = [b for b in m15_bars if _completed_by(str(b.get("ts") or ""), close_epoch, 15)]
        h1_ctx = [b for b in h1_bars if _completed_by(str(b.get("ts") or ""), close_epoch, 60)]
        is_newest = i == len(m5_bars) - 1

        # -- newest-bar context: spot quote + open lane basket (live only) ---
        spot: dict[str, Any] | None = None
        spread_abs = 0.0
        if is_newest:
            try:
                spot = mcp.get_spot_price(symbol)
                spread_abs = max(0.0, float(spot.get("ask", 0.0) or 0.0) - float(spot.get("bid", 0.0) or 0.0))
            except (McpClientError, McpZombieError) as exc:
                # never silent — a broken quote path hid the wrong-tool-name
                # bug for hours on 2026-07-05
                log_line(f"{utc_now_iso()} {symbol} spot_read_failed: {exc}")
                spot = None

        lane: list[dict[str, Any]] | None = []
        if is_newest and executor is not None:
            try:
                lane = basket_live.lane_positions(executor.client.get_positions(), _active_label_family())
                lane = [p for p in lane if str(p.get("symbolName") or p.get("symbol") or "") in ("", symbol)]
            except (McpClientError, McpZombieError) as exc:
                log_line(f"{utc_now_iso()} {symbol} lane_read_failed (no live action this bar): {exc}")
                lane = None  # unknown broker state → hard veto on live actions
            # Broker-side SL/TP closes bypass close_lane_position — reconcile
            # them into the learner here (journal-deduped; None lane = no-op;
            # never raises). See executor.reconcile_vanished_lane_positions.
            reconciled = executor.reconcile_vanished_lane_positions(symbol, lane)
            for rec in reconciled:
                log_line(
                    f"{utc_now_iso()} {symbol} vanish_reconciled position_id={rec.get('position_id')} "
                    f"setup={rec.get('setup')} session={rec.get('session')} pnl={rec.get('pnl')}"
                )

        # -- decision source routing -----------------------------------------
        basket_action: dict[str, Any] | None = None
        if is_newest and executor is not None and lane:
            # BASKET ACTIVE → this bar's action is campaign management
            decision, basket_action = _manage_lane_basket(
                executor, symbol, bar_ts, prefix, lane, state, spread_abs
            )
        elif is_newest and _vp_producer_enabled():
            # Volume-profile producer canary (2026-07-11): the FIRST candidate
            # to pass the promotion gate on BOTH hold-out splits (60/40:
            # validate +76.4R PF 1.57; 50/50: +67.2R PF 1.36 — board 22:15Z).
            # Env-gated DEFAULT OFF (DEXTER3_PRODUCER=vp to enable, owner
            # sign-off required); bars without volume make decide_vp skip
            # gracefully, so a mis-set flag can never crash the loop.
            from dexter3 import market_lens, volume_profile

            vp_session = str(market_lens.session_context(bar_ts).get("value") or "unknown")
            decision = volume_profile.decide_vp(symbol, prefix, spread_abs, session=vp_session)
        elif is_newest and _hunt_enabled():
            lens = hunter_brain._run_lens(prefix, hunter_brain._bar_close_ts(bar_ts))
            decision = hunt_mode.decide_hunt(
                symbol, prefix, m15_ctx, h1_ctx, lens, spread_abs,
                journal_stats=journal_stats if is_newest else None,
            )
        else:
            decision = hunter_brain.decide(
                symbol, None, prefix, m15_ctx, h1_ctx, journal_stats=journal_stats if is_newest else None
            )
        if decision.action == "enter":
            # Price Action Eye Phase A shadow (2026-07-15, additive): MUST
            # run before insert_decision just below so the journaled
            # features_json snapshot captures pa_eye automatically (see
            # _apply_pa_eye_shadow's docstring) -- placed here rather than
            # beside the v16 entry-quality gate further down because that
            # gate only evaluates on is_newest LIVE entries, while the Eye
            # shadow-journals EVERY decided enter (both lanes, catch-up
            # bars included) per the Phase A design doc.
            _apply_pa_eye_shadow(decision, prefix)
        # H2 (2026-07-15 cross-lane entanglement audit): stamp this row with
        # the writing lane's own order label so per-lane journal queries
        # (empirical stats, skip fear-cost) never pool Fable/Grok/VP rows.
        journal.insert_decision(decision, label=_active_order_label())
        log_decision_line(decision, late_sec=late_sec)

        basket = baskets.setdefault(symbol, PaperBasket(symbol, journal))
        basket.advance_clock(M5_BAR_SEC / 60.0)
        basket.on_decision(decision)
        if is_newest and spot is not None:
            mid = (float(spot.get("bid", 0.0) or 0.0) + float(spot.get("ask", 0.0) or 0.0)) / 2.0
        else:
            # historical catch-up bar (or failed quote): that bar's close — no lookahead
            mid = float(m5_bars[i].get("close") or 0.0)
        basket.on_m5_close_tick(mid)

        status = f"decided:{decision.action}:{decision.setup}"
        if basket_action is not None:
            status += f":basket_{basket_action.get('action', 'unknown')}"
        elif is_newest and decision.action == "enter":
            # Journal the smart-exit stop-regime classification for EVERY
            # fresh-entry decision on the newest bar, live or shadow (owner
            # directive 2026-07-08 "always journal, even when disabled" —
            # this is the Layer-2 shadow record the PM needs regardless of
            # whether --live/DEXTER3_LIVE is even on).
            smart_exit_meta = _apply_smart_exit_gate(decision, h1_ctx, state)
            if executor is not None:
                gov = state.get("governor") or {}
                # Computed eagerly (even on the locked/unverified branches
                # below, where its result goes unused) so the pre-entry cap
                # check below has it without a second MCP-adjacent call —
                # _lane_realized_today is cache-backed, so this is cheap.
                risk_info = _governor_entry_risk(mcp, decision, state)
                if gov.get("state") in ("TARGET_LOCKED", "LOSS_STOPPED"):
                    # Owner's daily mission rule (2026-07-07) outranks
                    # participation-first for EXECUTION only — the decision above
                    # is still journaled; we just do not put money on it today.
                    status += f":governor_{str(gov.get('state')).lower()}"
                elif lane is None:
                    status += ":live_skipped_lane_unverified"
                elif risk_info is not None and not risk_info.get("allow", True):
                    # Pre-entry loss-cap refusal (closes the race window
                    # between one tick's governor evaluation and the next
                    # tick's entry attempt — 2026-07-15).
                    status += f":live_blocked_{risk_info.get('reason', 'governor_cap')}"
                else:
                    daily = _daily_state(state)
                    base_risk_usd = (risk_info or {}).get("risk_usd")
                    if base_risk_usd is None:
                        base_risk_usd = executor.config.risk_usd
                    risk_usd_override = _apply_anti_chase_gate(decision, h1_ctx, float(base_risk_usd))
                    risk_usd_override = _apply_pullback_gate(decision, prefix, float(risk_usd_override))
                    risk_usd_override = _apply_v16_profit_controls(
                        mcp, state, decision, float(risk_usd_override)
                    )

                    # Stamp Grok_v1.0 scalping mode for this entry (independent parallel path)
                    # leader_score high is good. This entry will use fast Grok small-lock (0.25-0.45R)
                    # while V1.6 logic is bypassed for its profit exits.
                    grok_cfg = grok_v10.get_grok_v10_config_from_env() if grok_v10 else None
                    ls = float(getattr(decision, "leader_score", 0.0) or 0.0)
                    ch = bool((getattr(decision, "features", {}) or {}).get("anti_chase", {}).get("is_chase", False))
                    pb = bool((getattr(decision, "features", {}) or {}).get("pullback_gate", {}).get("is_pullback", True))
                    grok_mode = os.environ.get("DEXTER3_MODE", "v16").lower().strip() == "grok"
                    is_grok = (
                        bool(grok_mode and grok_v10 and grok_v10.is_grok_scalp_candidate(ls, ch, pb, grok_cfg))
                        if grok_v10
                        else False
                    )
                    state.setdefault("grok_v10_flags", {}).update({
                        "leader_score": ls,
                        "is_chase": ch,
                        "is_pullback": pb,
                        "is_grok_scalp": is_grok,
                    })

                    # V1.6 entry-quality pro-pack (Grok bypasses). A+ setups
                    # bypass cool-down; MCP pause / min score still apply.
                    quality = _apply_v16_entry_quality_gate(state, decision)
                    if not quality.get("allow", True):
                        status += f":live_blocked_{quality.get('reason', 'quality')}"
                    else:
                        risk_usd_override = _apply_v18_size_levers(
                            quality, float(base_risk_usd), float(risk_usd_override)
                        )
                        exec_result = _execute_live_entry(
                            executor,
                            decision,
                            today_entry_count=int(daily.get("entries", 0)),
                            today_losing_count=int(daily.get("loss_baskets", 0)),
                            risk_usd_override=risk_usd_override,
                            smart_exit=smart_exit_meta,
                        )
                        if exec_result.get("action") == "entered":
                            daily["entries"] = int(daily.get("entries", 0)) + 1
                            # Clear cool-down after a real fill so A+ / allowed
                            # entries do not leave a stale same-side stamp.
                            if not _is_grok_mode():
                                state.pop("v16_entry_cooldown", None)
                            # oldest_open_ts stays None here (the position was
                            # just placed; its broker-side open timestamp is not
                            # yet known) — run_om_tick backfills it with the real
                            # lane timestamp on the first fast tick that observes
                            # this basket (see its own smart-exit backfill block).
                        status += f":live_{exec_result.get('action', 'unknown')}"

        mark_m5_close_seen(state, symbol, bar_ts)
        save_shadow_state(state)
        if late_sec > 90:
            status += f":late{int(late_sec)}s"
        statuses.append(status)
    return ";".join(statuses)


def _governor_entry_risk(
    mcp: Dexter3McpClient, decision: hunter_brain.Decision, state: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Daily Mission Governor dynamic sizing for one entry: ladder by today's
    win streak (stateless, derived from today's closed lane deals) × session
    multiplier, on the owner's virtual capital base.

    Also refuses the entry outright — ``allow: False``, reason
    ``governor_loss_stop_pre_entry`` — when today's realized (+ floating, if
    ``state["governor"]["floating_by_symbol"]`` already has a fresh snapshot)
    PnL is already at/through the SAME ``daily_loss_usd`` cap
    ``run_governor_tick`` uses for its close-all trigger (reused via
    ``_get_governor().config``, never duplicated). This closes the race
    window between one fast tick's governor evaluation and the NEXT tick's
    entry attempt — a fresh 19-second-old position was let through and then
    immediately closed by the governor's loss-stop (2026-07-15).

    Never raises — a failure (sizing OR the cap check above) falls back to
    the executor's static config risk (returns None), same fail-open posture
    as every other entry-sizing gate in this file."""
    try:
        realized, pnls = _lane_realized_today(mcp, label_filter=_active_label_family())
        cfg = _get_governor().config
        floating = 0.0
        if state is not None:
            floating_by_symbol = (state.get("governor") or {}).get("floating_by_symbol") or {}
            floating = sum(_f(v, 0.0) for v in floating_by_symbol.values())
        effective = realized + floating
        if effective <= -abs(cfg.daily_loss_usd):
            log_line(
                f"{utc_now_iso()} {decision.symbol} governor_loss_stop_pre_entry "
                f"effective={effective:.2f} cap=-{abs(cfg.daily_loss_usd):.2f} (entry refused)"
            )
            return {
                "allow": False,
                "reason": "governor_loss_stop_pre_entry",
                "effective_pnl": round(effective, 4),
            }
        governor = _get_governor()
        session = str(((decision.features or {}).get("session_context") or {}).get("value") or "unknown")
        streak = governor.win_streak_from_closes(pnls)
        info = governor.risk_for_entry(session, streak)
        info["allow"] = True
        log_line(
            f"{utc_now_iso()} {decision.symbol} governor sizing risk_usd={info.get('risk_usd')} "
            f"streak={streak} ladder={info.get('ladder_mult')} session={session}x{info.get('session_mult')}"
        )
        return info
    except Exception as exc:  # noqa: BLE001 - sizing must never block an entry
        log_line(f"{utc_now_iso()} {decision.symbol} governor_sizing_failed (static risk used): {exc}")
        return None


def _apply_anti_chase_gate(
    decision: hunter_brain.Decision, h1_ctx: list[dict[str, Any]], base_risk_usd: float
) -> float:
    """ANTI-CHASE sizing gate (owner directive 2026-07-08 — edge-discovery
    Layer 1 finding): downsize (never block — participation-first stays) the
    "aligned x trending" bucket, the ONE bucket the sweep proved is the
    entire system loss (-116R/6d, 43% of entries) while the rest is +85R.

    Computes the multiplier from the SAME h1_ctx already in scope for this
    bar (no extra MCP read), multiplies it into ``base_risk_usd`` (the
    governor's risk_for_entry result, or the executor's static config risk
    when the governor path failed), and ALWAYS journals the classification —
    into the decision's own ``features`` dict (so it lands in the journal
    row DecisionJournal already persists) AND a dedicated log line — even
    when DEXTER3_ANTICHASE_ENABLED=0, so the shadow record of what the gate
    WOULD have done exists regardless of the flag. Never raises: any failure
    here must fall back to the ungated base_risk_usd, not block the entry.
    """
    try:
        cfg = _edge_gate_config_from_env()
        mult, reason = anti_chase_risk_mult(decision.side, h1_ctx, cfg)
        gated_risk_usd = round(base_risk_usd * mult, 4)
        # Journal on the decision itself — DecisionJournal.insert_decision
        # (called just above this bar's dispatch) already persisted the
        # Decision's features dict, but that decision object is the SAME
        # instance the caller holds, so mutating it here before it is
        # inspected downstream (e.g. by tests or later log lines) still
        # carries the anti-chase verdict for anyone reading decision.features.
        if isinstance(decision.features, dict):
            decision.features["anti_chase"] = {**reason, "base_risk_usd": base_risk_usd, "gated_risk_usd": gated_risk_usd}
        log_line(
            f"{utc_now_iso()} {decision.symbol} anti-chase: bucket={reason.get('align')}/{reason.get('regime')} "
            f"is_chase={reason.get('is_chase')} enabled={reason.get('enabled')} mult={mult} "
            f"risk_usd {base_risk_usd:.2f}->{gated_risk_usd:.2f}"
        )
        return gated_risk_usd
    except Exception as exc:  # noqa: BLE001 - sizing gate must never block an entry
        log_line(f"{utc_now_iso()} {decision.symbol} anti_chase_gate_failed (ungated risk used): {exc}")
        return base_risk_usd


def _apply_pullback_gate(
    decision: hunter_brain.Decision, m5_prefix: list[dict[str, Any]], base_risk_usd: float
) -> float:
    """PULLBACK-RESUMPTION sizing selector (owner directive 2026-07-08 —
    edge-discovery: pullback entries = +0.055R vs +0.003R baseline, 18x).
    Full size on a pullback-resumption entry; scout (``non_pullback_mult``)
    otherwise — so real money concentrates on the proven-edge setups while
    participation-first holds (non-pullback M5s are still entered, scout-sized).
    Multiplies the ALREADY anti-chase-gated risk (the two entry selectors
    compound). Always journals the classification; never raises."""
    try:
        cfg = _edge_gate_config_from_env()
        mult, reason = edge_buckets.pullback_size_mult(decision.side, m5_prefix, cfg)
        gated = round(base_risk_usd * mult, 4)
        if isinstance(decision.features, dict):
            decision.features["pullback_gate"] = {**reason, "base_risk_usd": base_risk_usd, "gated_risk_usd": gated}
        log_line(
            f"{utc_now_iso()} {decision.symbol} pullback-gate: is_pullback={reason.get('is_pullback')} "
            f"mult={mult} risk_usd {base_risk_usd:.2f}->{gated:.2f}"
        )
        return gated
    except Exception as exc:  # noqa: BLE001 - sizing gate must never block an entry
        log_line(f"{utc_now_iso()} {decision.symbol} pullback_gate_failed (ungated risk used): {exc}")
        return base_risk_usd


def _apply_smart_exit_gate(
    decision: hunter_brain.Decision, h1_ctx: list[dict[str, Any]], state: dict[str, Any]
) -> dict[str, Any]:
    """SMART ADAPTIVE EXIT stop-regime classification (owner directive
    2026-07-08 — see ``dexter3/smart_exit.py`` for the full design and the
    backtest that proved it). Classifies this entry's stop regime ('tight'
    vs 'disaster') from the SAME ``h1_ctx`` already in scope for this bar
    (no extra MCP read, and — critically — the SAME chase classification the
    anti-chase gate just computed, via ``edge_buckets.classify_bucket``, so
    the two gates can never disagree about which bucket this entry is in).

    ALWAYS journals the classification (decision.features + a dedicated log
    line), even when ``DEXTER3_SMART_EXIT_ENABLED=0`` or the entry is a
    chase (regime stays 'tight' either way) — shadow measurement per the
    module's own contract. Also persists the regime into
    ``state['smart_exit_regime'][symbol]`` so the OM fast-tick path
    (``run_om_tick``) can look it up once this entry becomes an open lane
    position; the caller is responsible for calling ``save_shadow_state``
    afterward (same ownership pattern as ``basket_runtime``). Never raises:
    any failure here degrades to the 'tight' regime (today's unchanged
    behavior), never blocks the entry.
    """
    try:
        cfg = _smart_exit_config_from_env()
        classification = resolve_stop_regime(decision.side, h1_ctx, cfg)
        if isinstance(decision.features, dict):
            decision.features["smart_exit"] = dict(classification)
        log_line(
            f"{utc_now_iso()} {decision.symbol} smart-exit: bucket={classification.get('align')}/"
            f"{classification.get('h1_regime')} "
            f"is_chase={classification.get('is_chase')} enabled={classification.get('enabled')} "
            f"stop_regime={classification.get('regime')} disaster_mult={classification.get('disaster_mult')}"
        )
        # Persist keyed by symbol (mirrors state['basket_runtime'][symbol]) so
        # the OM tick can look this up once the entry becomes an open lane —
        # oldest_open_ts is stamped once the position actually opens (this
        # function runs BEFORE the order is placed, so we stamp None here and
        # let the OM tick's own basket-reset guard tolerate the first read;
        # run_om_tick backfills oldest_open_ts on its first observation of
        # this lane via the same regime dict object).
        state.setdefault("smart_exit_regime", {})[decision.symbol] = {
            "regime": classification.get("regime"),
            "disaster_mult": classification.get("disaster_mult"),
            "oldest_open_ts": None,
            "classified_at": utc_now_iso(),
        }
        return classification
    except Exception as exc:  # noqa: BLE001 - classification must never block an entry
        log_line(f"{utc_now_iso()} {decision.symbol} smart_exit_gate_failed (tight regime used): {exc}")
        return {"regime": "tight", "disaster_mult": 1.0, "enabled": False, "is_chase": None}


# -- account guard alarm (owner directive 2026-07-09: force demo 9922808) ----
# The executor's demo gate (ExecutorConfig.demo_trader_ids, fail-closed on
# get_balance().traderId) is what BLOCKS entries on a wrong/unverified
# account. This alarm makes that state loud instead of silent: without it a
# wrong active account in cTrader just looks like an idle loop.
_ACCOUNT_GUARD_NOTIFY_THROTTLE_SEC = 300.0
_account_guard_last_notify = 0.0


def _maybe_alert_account_guard(client: Any, result: dict[str, Any] | None) -> bool:
    """In-app popup + loud log when an entry was refused by the demo gate.

    Best-effort and throttled (one popup per 5 min); NEVER raises — the live
    loop must not die because a notification failed. Returns True when a
    notification was sent (test hook)."""
    global _account_guard_last_notify
    try:
        if not isinstance(result, dict):
            return False
        if str(result.get("reason") or "") != "account_not_confirmed_demo":
            return False
        log_line(
            f"{utc_now_iso()} ACCOUNT GUARD: entry refused — active cTrader account is not "
            f"the required demo (trader_id={result.get('trader_id')}, "
            f"expected demo_trader_ids={result.get('demo_trader_ids')}). "
            f"Switch cTrader to demo 9922808; entries stay blocked until then."
        )
        now = time.time()
        if now - _account_guard_last_notify < _ACCOUNT_GUARD_NOTIFY_THROTTLE_SEC:
            return False
        client.call(
            "show_notification",
            {
                "caption": "DEXTER3 ACCOUNT GUARD",
                "description": (
                    "Wrong/unverified trading account active. Switch cTrader to "
                    "demo 9922808 — all Dexter3 entries are blocked until then."
                ),
                "type": "error",
            },
        )
        _account_guard_last_notify = now
        return True
    except Exception:  # noqa: BLE001 - alarm must never break the loop
        return False


def _execute_live_entry(
    executor: Dexter3Executor,
    decision: hunter_brain.Decision,
    *,
    today_entry_count: int = 0,
    today_losing_count: int = 0,
    repair: bool = False,
    risk_usd_override: float | None = None,
    smart_exit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve account state and place a live micro-entry. Never raises."""
    account_state: dict[str, Any] = {}
    # get_balance() intermittently returns without traderId (observed live
    # 2026-07-05 11:00:21Z → demo gate refused a valid entry). One short
    # retry before giving the executor a state it will refuse.
    for attempt in range(2):
        try:
            balance = executor.client.get_balance()
            account_state = {"traderId": balance.get("traderId")}
        except (McpClientError, McpZombieError) as exc:
            log_line(f"{utc_now_iso()} {decision.symbol} live_entry_balance_read_failed: {exc}")
            account_state = {}
        if account_state.get("traderId") is not None:
            break
        if attempt == 0:
            time.sleep(2)
    try:
        if repair:
            result = executor.execute_repair_leg(
                decision,
                account_state,
                today_entry_count=today_entry_count,
                today_losing_count=today_losing_count,
                risk_usd_override=risk_usd_override,
            )
        else:
            result = executor.execute_entry(
                decision,
                account_state,
                today_entry_count=today_entry_count,
                today_losing_count=today_losing_count,
                risk_usd_override=risk_usd_override,
                smart_exit=smart_exit,
            )
    except Exception as exc:  # noqa: BLE001 - live path must never crash the loop
        log_error(f"execute_entry({decision.symbol})", exc)
        return {"action": "exception"}
    _maybe_alert_account_guard(executor.client, result)
    log_line(
        f"{utc_now_iso()} {decision.symbol} LIVE_ENTRY action={result.get('action')} "
        f"position_id={result.get('position_id')} verified={result.get('verified')}"
    )
    return result


def _repair_geometry(
    prefix: list[dict[str, Any]], side: str, spread_abs: float
) -> tuple[float, float, float]:
    """Entry/SL/TP for a repair or hedge leg: TR-quantile distances with the
    same cost guards as hunt mode (SL >= max(6*spread, TR_q50), TP >= both
    8*spread and 1.2*SL). Returns (entry, sl, tp)."""
    entry = float(prefix[-1].get("close") or 0.0)
    trs = sorted(
        max(1e-9, float(b.get("high", 0.0)) - float(b.get("low", 0.0)))
        for b in prefix[-40:]
    )
    q50 = trs[len(trs) // 2] if trs else 0.0
    q90 = trs[int(len(trs) * 0.9) - 1] if len(trs) >= 10 else (trs[-1] if trs else 0.0)
    sl_dist = min(max(6.0 * spread_abs, q50 * 1.2), max(2.0 * q90, 6.0 * spread_abs))
    tp_dist = max(8.0 * spread_abs, 1.2 * sl_dist)
    if side == "buy":
        return entry, round(entry - sl_dist, 5), round(entry + tp_dist, 5)
    return entry, round(entry + sl_dist, 5), round(entry - tp_dist, 5)


def _manage_lane_basket(
    executor: Dexter3Executor,
    symbol: str,
    bar_ts: str,
    prefix: list[dict[str, Any]],
    lane: list[dict[str, Any]],
    state: dict[str, Any],
    spread_abs: float,
) -> tuple[hunter_brain.Decision, dict[str, Any]]:
    """A lane basket is open → this M5 close is a campaign-management action
    (HUNT MODE contract v2): hold / repair / hedge / close-all. Returns the
    journal Decision (action='manage') and the executed basket action."""
    ts_close = hunter_brain._bar_close_ts(bar_ts)
    lens = hunter_brain._run_lens(prefix, ts_close)
    daily = _daily_state(state)

    agg = basket_live.aggregate_lane(lane, base_risk_usd=_lane_actual_risk_usd(lane, executor.config.risk_usd))
    agg["lens_liquidity_sweep"] = lens.get("liquidity_sweep")  # sweep-vs-break repair distinction
    sides = agg.get("sides", {}) or {}
    basket_side = "buy" if int(sides.get("buy", 0)) >= int(sides.get("sell", 0)) else "sell"
    evidence = basket_live.structure_evidence(lens, prefix, basket_side, agg.get("weighted_entry"))

    # FIX 2 (2026-07-07): maintain per-basket peak-R runtime state (keyed by
    # oldest_open_ts) BEFORE calling decide_basket_action, so the peak-R
    # trailing path in basket_live._resolve_profit_action activates on the
    # REAL lane basket (not just in unit tests). aggregate_r is already
    # broker-derived PnL (aggregate_lane's aggregate_r), so peak_r here
    # tracks real R, not a simulated proxy.
    basket_runtime = _basket_runtime_for(
        state, symbol, agg.get("oldest_open_ts"), _f(agg.get("aggregate_r"), 0.0)
    )
    action = basket_live.decide_basket_action(
        _basket_config_from_env(),
        agg,
        evidence,
        now_utc_iso=utc_now_iso(),
        daily_state={"daily_loss_baskets": int(daily.get("loss_baskets", 0))},
        basket_runtime=basket_runtime,
    )
    act = str(action.get("action") or "hold")
    executed: dict[str, Any] = dict(action)

    if act in ("close_all_in_profit", "close_all_cap_stop"):
        ids = [int(p.get("positionId") or p.get("id") or 0) for p in lane]
        ids = [x for x in ids if x > 0]
        executed["exec"] = executor.execute_close_all(ids, reason=act)
        if act == "close_all_cap_stop" and float(agg.get("aggregate_pnl_usd") or 0.0) < 0:
            daily["loss_baskets"] = int(daily.get("loss_baskets", 0)) + 1
        # Basket resolved (either path) — the lane is now flat; clear the
        # peak-R runtime so the NEXT basket starts tracking from zero
        # instead of inheriting this basket's peak.
        is_grok_close = "grok" in str(action.get("reason", "")).lower()
        _clear_basket_runtime(state, symbol, grok=is_grok_close)
    elif act == "add_repair_leg" and (state.get("governor") or {}).get("state") in ("TARGET_LOCKED", "LOSS_STOPPED"):
        # governor lock/stop: never ADD exposure after the day is decided —
        # existing legs are being closed by run_governor_tick anyway.
        executed["governor_suppressed"] = True
        act = "hold"
    elif act == "add_repair_leg":
        repair_side = str(action.get("side") or ("sell" if basket_side == "buy" else "buy"))
        entry, sl, tp = _repair_geometry(prefix, repair_side, spread_abs)
        repair_decision = hunter_brain.Decision(
            ts_close=ts_close,
            symbol=symbol,
            action="enter",
            side=repair_side,
            entry_type="market",
            entry=entry,
            sl=sl,
            tp=tp,
            size_class="scout",
            leader_score=0.0,
            p_win_est=0.5,
            setup="basket_repair",
            reasons=[
                f"เติมไม้ซ่อมตะกร้า / basket repair leg ({action.get('note') or act})",
                f"aggregate_r={agg.get('aggregate_r')}, legs={agg.get('legs')}",
            ],
            features={"basket": {k: agg.get(k) for k in ("legs", "aggregate_pnl_usd", "aggregate_r", "sides")}},
        )
        executed["exec"] = _execute_live_entry(
            executor,
            repair_decision,
            today_entry_count=int(daily.get("entries", 0)),
            today_losing_count=int(daily.get("loss_baskets", 0)),
            repair=True,
            risk_usd_override=_apply_v16_profit_controls(
                executor.client,
                state,
                repair_decision,
                _f(agg.get("base_risk_usd"), executor.config.risk_usd),
            ),
        )
        if executed["exec"].get("action") == "entered":
            daily["entries"] = int(daily.get("entries", 0)) + 1

    manage_decision = hunter_brain.Decision(
        ts_close=ts_close,
        symbol=symbol,
        action="manage",
        side=None,
        entry_type=None,
        entry=None,
        sl=None,
        tp=None,
        size_class="none",
        leader_score=0.0,
        p_win_est=0.0,
        setup=f"basket_{act}",
        reasons=[
            f"บริหารตะกร้า / campaign management: {act}",
            f"legs={agg.get('legs')} aggregate_r={agg.get('aggregate_r')} pnl_usd={agg.get('aggregate_pnl_usd')}",
            f"evidence: level_lost={evidence.get('level_lost')} close_beyond={evidence.get('m5_close_beyond')}",
        ],
        features={"basket": agg, "evidence": evidence, "basket_action": {k: v for k, v in executed.items() if k != 'exec'}},
    )
    log_line(
        f"{utc_now_iso()} {symbol} BASKET action={act} legs={agg.get('legs')} "
        f"agg_r={agg.get('aggregate_r')} pnl={agg.get('aggregate_pnl_usd')}"
    )
    return manage_decision, executed


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------


_LAST_STATUS: dict[str, str] = {}
_STATUS_REPEATS: dict[str, int] = {}
STATUS_HEARTBEAT_EVERY = 90  # re-log an unchanged status every ~30 min at 20s poll

# per-symbol empirical stats cache, refreshed every _learning_refresh_every_cycles
# cycles (see run_once). Module-level so --once callers (which run a single
# cycle) simply never populate it — hunter_brain.decide() then falls back to
# its own base p_win priors, byte-identical to Phase 1.
_JOURNAL_STATS_CACHE: dict[str, dict[str, Any]] = {}
_CYCLE_COUNTER = 0


def _refresh_learning_loops(symbols: list[str], journal: DecisionJournal, mcp: Dexter3McpClient) -> None:
    """Every N cycles: refresh empirical p_win stats + evaluate pending skips.

    Never raises — a failure here must not interrupt the decision loop, it
    only means stats stay stale for another refresh interval.
    """
    for symbol in symbols:
        try:
            _JOURNAL_STATS_CACHE[symbol] = empirical_stats.stats_to_journal_stats_arg(
                empirical_stats.compute_from_journal(journal, symbol, label=_active_label_family())
            ) or None
        except Exception as exc:  # noqa: BLE001 - stats refresh must never break the loop
            log_error(f"empirical_stats.compute_from_journal({symbol})", exc)
    try:
        # H2 (2026-07-15 cross-lane entanglement audit): each lane's learner
        # loop only ever evaluates/summarizes ITS OWN decisions — see
        # skip_evaluator.evaluate_pending_skips/fear_cost_summary's label
        # exclusion convention. FAMILY (not the exact versioned label, see
        # 2026-07-15 versioned-labels design) so a version bump never drops
        # this lane's own earlier rows from its learner.
        active_label_family = _active_label_family()
        result = skip_evaluator.evaluate_pending_skips(journal, mcp, label=active_label_family)
        log_line(
            f"{utc_now_iso()} skip_evaluator checked={result['checked']} "
            f"evaluated={result['evaluated']} unevaluable={result['unevaluable']}"
        )
        fear_cost = skip_evaluator.fear_cost_summary(journal, label=active_label_family)
        log_line(
            f"{utc_now_iso()} fear_cost hours={fear_cost['hours']} "
            f"skips_evaluated={fear_cost['skips_evaluated']} "
            f"invalid_lookahead={fear_cost['invalid_lookahead']} "
            f"would_have_wins={fear_cost['would_have_wins']} "
            f"would_have_pnl_r={fear_cost['would_have_pnl_r']}"
        )
    except Exception as exc:  # noqa: BLE001 - loop must never die
        log_error("skip_evaluator.evaluate_pending_skips", exc)


def run_once(
    symbols: list[str],
    mcp: Dexter3McpClient,
    journal: DecisionJournal,
    baskets: dict[str, PaperBasket],
    *,
    executor: Dexter3Executor | None = None,
    refresh_every_cycles: int | None = None,
) -> None:
    global _CYCLE_COUNTER
    state = load_shadow_state()
    for symbol in symbols:
        try:
            status = run_symbol_cycle(
                mcp,
                journal,
                baskets,
                state,
                symbol,
                executor=executor,
                journal_stats=_JOURNAL_STATS_CACHE.get(symbol),
            )
            repeats = _STATUS_REPEATS.get(symbol, 0) + 1 if _LAST_STATUS.get(symbol) == status else 0
            _STATUS_REPEATS[symbol] = repeats
            _LAST_STATUS[symbol] = status
            if repeats == 0 or repeats % STATUS_HEARTBEAT_EVERY == 0:
                suffix = f" (x{repeats + 1})" if repeats else ""
                log_line(f"{utc_now_iso()} {symbol} cycle_status={status}{suffix}")
        except McpZombieError as exc:
            log_line(f"{utc_now_iso()} {symbol} MCP_ZOMBIE: {exc}")
        except Exception as exc:  # noqa: BLE001 - loop must never die on a per-symbol error
            log_error(f"run_symbol_cycle({symbol})", exc)

    if refresh_every_cycles:
        _CYCLE_COUNTER += 1
        if _CYCLE_COUNTER % refresh_every_cycles == 0:
            _refresh_learning_loops(symbols, journal, mcp)


# ---------------------------------------------------------------------------
# OPENING MANAGER (OM) — fast-tick scheduling + per-tick execution
# ---------------------------------------------------------------------------


def m5_entry_tick_due(tick_count: int, poll_sec: int, fast_tick_sec: int) -> bool:
    """Pure scheduling decision: should this fast tick also run the M5
    entry/decision path (``run_once``)?

    ``tick_count`` is the 1-based count of fast ticks since the loop
    started. The M5 path fires once every ``ceil(poll_sec / fast_tick_sec)``
    ticks (and always on the very first tick, ``tick_count == 1``, so entries
    do not wait a full poll_sec before the loop's first M5 evaluation) — this
    keeps the M5 entry cadence unchanged from the pre-OM behavior (still
    roughly every ``poll_sec`` seconds) while every OTHER tick in between
    runs ONLY the OM fast defense path.
    """
    if fast_tick_sec <= 0:
        return True
    ticks_per_poll = max(1, -(-int(poll_sec) // int(fast_tick_sec)))  # ceil division
    return tick_count <= 1 or tick_count % ticks_per_poll == 0


def om_bars_refresh_due(last_bar_fetch_epoch: float, now_epoch: float, refresh_sec: int = OM_BAR_REFRESH_SEC) -> bool:
    """Pure: should OM refetch M5/M15/H1 bars this tick?

    Spot + positions are refetched EVERY tick (they drive the trail and must
    never be stale), but bars (needed only for structure_evidence/hunt_mode
    in the Basket Doctor path) are refetched at most every ``refresh_sec``
    seconds to keep MCP read load bounded at a 4s tick cadence.
    """
    return last_bar_fetch_epoch <= 0 or (now_epoch - last_bar_fetch_epoch) >= refresh_sec


_OM_INSTANCE: OpeningManager | None = None
_GROK_OM_INSTANCE: "GrokV10OpeningManager | None" = None
_OM_BAR_CACHE: dict[str, dict[str, Any]] = {}  # symbol -> {"epoch":, "m5":, "m15":, "h1":}
_OM_HOLD_TICKS: dict[str, int] = {}  # symbol -> consecutive hold ticks (heartbeat throttle)
OM_HOLD_HEARTBEAT_TICKS = 15  # ~1 min at 4s ticks


def _get_opening_manager(executor: Dexter3Executor | None, journal: DecisionJournal) -> OpeningManager:
    global _OM_INSTANCE
    if _OM_INSTANCE is None:
        _OM_INSTANCE = OpeningManager(executor, journal, _om_config_from_env())
    return _OM_INSTANCE


def _get_grok_opening_manager(executor: Dexter3Executor | None, journal: DecisionJournal) -> "GrokV10OpeningManager":
    """Separate OM instance for Grok v1.0 for 100% independent monitoring."""
    global _GROK_OM_INSTANCE
    if _GROK_OM_INSTANCE is None and GrokV10OpeningManager is not None:
        _GROK_OM_INSTANCE = GrokV10OpeningManager(executor, journal, _om_config_from_env())
    return _GROK_OM_INSTANCE if _GROK_OM_INSTANCE is not None else _get_opening_manager(executor, journal)


def _om_bars_for(mcp: Dexter3McpClient, symbol: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Cached M5/M15/H1 bars for the OM fast tick — refetched at most every
    ``OM_BAR_REFRESH_SEC`` seconds (see ``om_bars_refresh_due``). Never
    raises: an MCP read failure leaves the previous cache entry in place (or
    empty lists on the very first tick) rather than crashing the fast loop.
    """
    now_epoch = datetime.now(timezone.utc).timestamp()
    cached = _OM_BAR_CACHE.get(symbol)
    if cached is not None and not om_bars_refresh_due(cached.get("epoch", 0.0), now_epoch):
        return cached["m5"], cached["m15"], cached["h1"]
    try:
        m5 = mcp.get_trendbars(symbol, "m5", MIN_M5_BARS)
        m15 = mcp.get_trendbars(symbol, "m15", M15_BARS_NEEDED)
        h1 = mcp.get_trendbars(symbol, "h1", H1_BARS_NEEDED)
        _OM_BAR_CACHE[symbol] = {"epoch": now_epoch, "m5": m5, "m15": m15, "h1": h1}
        return m5, m15, h1
    except (McpClientError, McpZombieError) as exc:
        log_line(f"{utc_now_iso()} {symbol} om_bars_refresh_failed (using stale cache if any): {exc}")
        if cached is not None:
            return cached["m5"], cached["m15"], cached["h1"]
        return [], [], []


def run_weekly_flatten_tick(executor: Dexter3Executor | None, symbol: str) -> str:
    """Close only this lane's positions in the still-open Friday buffer.

    Default-off at the environment boundary; no shadow-mode mutation and no
    close attempt once the weekly market is already shut.
    """
    if executor is None or not _env_bool("DEXTER3_WEEKEND_FLATTEN_ENABLED", False):
        return "weekly_flatten_disabled"
    weekly = weekly_close_policy(datetime.now(timezone.utc))
    if not bool(weekly["flatten"]):
        return str(weekly["reason"])
    try:
        positions = executor.client.get_positions()
    except (McpClientError, McpZombieError) as exc:
        log_line(f"{utc_now_iso()} {symbol} weekly_flatten_read_failed: {exc}")
        return "weekly_flatten_read_failed"
    lane = basket_live.lane_positions(positions, _active_label_family())
    ids = [position_id_of(p) for p in lane if position_id_of(p) > 0 and str(p.get("symbolName") or p.get("symbol") or "") in ("", symbol)]
    if not ids:
        return "weekly_flatten_no_lane"
    result = executor.execute_close_all(ids, reason="weekly_flatten")
    log_line(f"{utc_now_iso()} {symbol} weekly_flatten attempted ids={ids} result={result}")
    return "weekly_flatten_attempted"


def run_om_tick(
    mcp: Dexter3McpClient,
    journal: DecisionJournal,
    state: dict[str, Any],
    symbol: str,
    *,
    executor: Dexter3Executor | None,
) -> str:
    """One fast-tick OM evaluation for ``symbol``. Never raises — any
    failure is logged and treated as a no-op tick (the OM must never crash
    the fast loop, per the blueprint's "never trades blind" / "never dies"
    posture shared with every other dexter3 loop path).

    Without a live executor (shadow mode), OM still evaluates and journals
    the would-be action but places/closes NOTHING — gated on
    ``executor is not None``, identical to every other live-mutation gate in
    this file.
    """
    try:
        positions = executor.client.get_positions() if executor is not None else mcp.get_positions()
        # Manage exactly one lane per process. Combining V1.6 and Grok labels
        # would merge PnL/runtime state and let one manager close the other.
        # FAMILY (not the exact versioned label — see 2026-07-15
        # versioned-labels design) so a version bump never drops this lane's
        # own already-open positions from OM management mid-day.
        active_label_family = _active_label_family()
        lane = basket_live.lane_positions(positions, active_label_family)
        lane = [p for p in lane if str(p.get("symbolName") or p.get("symbol") or "") in ("", symbol)]
    except (McpClientError, McpZombieError) as exc:
        _note_mcp_error(state, ok=False)
        log_line(f"{utc_now_iso()} {symbol} om_lane_read_failed (no OM action this tick): {exc}")
        return "om_lane_read_failed"

    _note_mcp_error(state, ok=True)

    if not lane:
        is_grok = bool((state.get("grok_v10_flags") or {}).get("is_grok_scalp"))
        _clear_basket_runtime(state, symbol, grok=is_grok)
        state.setdefault("governor", {}).setdefault("floating_by_symbol", {})[symbol] = 0.0
        return "om_no_lane"

    m5_bars, m15_bars, h1_bars = _om_bars_for(mcp, symbol)

    spread_abs = 0.0
    try:
        spot = mcp.get_spot_price(symbol)
        spread_abs = max(0.0, _f(spot.get("ask"), 0.0) - _f(spot.get("bid"), 0.0))
    except (McpClientError, McpZombieError) as exc:
        log_line(f"{utc_now_iso()} {symbol} om_spot_read_failed: {exc}")
        spot = None

    # Determine mode from lane label for 100% independent monitoring
    lane_labels = [str(p.get("label") or p.get("comment") or "") for p in lane]
    is_grok_lane = any("grok-v1.0" in lbl.lower() for lbl in lane_labels) or bool((state.get("grok_v10_flags") or {}).get("is_grok_scalp"))

    if is_grok_lane and GrokV10OpeningManager is not None:
        om = _get_grok_opening_manager(executor, journal)
        runtime_key = "grok_v10_basket_runtime"
        regime_key = "grok_v10_smart_exit_regime"
    else:
        om = _get_opening_manager(executor, journal)
        runtime_key = "basket_runtime"
        regime_key = "smart_exit_regime"

    agg_probe = basket_live.aggregate_lane(lane, base_risk_usd=_lane_actual_risk_usd(lane, _om_base_risk_usd(executor)))
    # Daily Mission Governor (owner directive 2026-07-07): record this
    # symbol's floating PnL for the governor's account-wide floating
    # aggregate — reuses this same aggregate_lane read, no extra MCP calls.
    state.setdefault("governor", {}).setdefault("floating_by_symbol", {})[symbol] = _f(
        agg_probe.get("aggregate_pnl_usd"), 0.0
    )
    # SMART EXIT (owner directive 2026-07-08): backfill the regime record's
    # oldest_open_ts with the REAL lane timestamp the first tick that
    # observes this basket (it is stamped None/placeholder at classification
    # time in _apply_smart_exit_gate, before the position exists) so
    # OpeningManager._smart_loss_exit's stale-basket guard can match it
    # against agg['oldest_open_ts'] on every subsequent tick.
    regime_map = state.setdefault(regime_key, {})
    regime_entry = regime_map.get(symbol)
    lane_oldest_ts = agg_probe.get("oldest_open_ts")
    if isinstance(regime_entry, dict) and lane_oldest_ts and regime_entry.get("oldest_open_ts") != lane_oldest_ts:
        regime_entry["oldest_open_ts"] = lane_oldest_ts
    om_state = {
        "basket_runtime": (state.get(runtime_key) or {}).get(symbol),
        "now_utc_iso": utc_now_iso(),
        "daily_state": {"daily_loss_baskets": int(_daily_state(state).get("loss_baskets", 0))},
        "basket_cfg": _basket_config_from_env(),
        "base_risk_usd": _lane_actual_risk_usd(lane, _om_base_risk_usd(executor)),
        "spread_abs": spread_abs,
        "smart_exit_regime": regime_map,
        # Grok_v1.0 flags (populated at entry time)
        **(state.get("grok_v10_flags") or {}),
    }
    action = om.evaluate(symbol, lane, spot, m5_bars, m15_bars, h1_bars, om_state)

    new_runtime = action.get("basket_runtime")
    if new_runtime is not None:
        state.setdefault(runtime_key, {})[symbol] = new_runtime

    act = str(action.get("action") or "hold")
    if act == "add_repair_leg" and (state.get("governor") or {}).get("state") in ("TARGET_LOCKED", "LOSS_STOPPED"):
        # governor lock/stop: never ADD exposure after the day is decided
        # (close_all actions still pass — they reduce exposure).
        act = "hold"
        action = {**action, "action": "hold", "governor_suppressed": True}
    dry = executor is None
    executed: dict[str, Any] = {}
    if act == "close_all":
        ids = [int(p.get("positionId") or p.get("id") or 0) for p in lane]
        ids = [x for x in ids if x > 0]
        # Side for smart cool-down (noise serial re-entry filter, V1.6 only).
        close_side = ""
        for p in lane:
            t = str(p.get("tradeSide") or p.get("side") or "").upper()
            if t in ("BUY", "LONG"):
                close_side = "buy"
                break
            if t in ("SELL", "SHORT"):
                close_side = "sell"
                break
        if not dry:
            executed = executor.execute_close_all(ids, reason=f"om_{action.get('reason')}")
            if action.get("reason") == "cap_stop" and float(agg_probe.get("aggregate_pnl_usd") or 0.0) < 0:
                daily = _daily_state(state)
                daily["loss_baskets"] = int(daily.get("loss_baskets", 0)) + 1
        is_grok_close = "grok" in str(action.get("reason", "")).lower() or is_grok_lane
        if not is_grok_close and not _is_grok_mode():
            stamp = record_noise_close(
                state,
                symbol=symbol,
                side=close_side or str((state.get("grok_v10_flags") or {}).get("side") or ""),
                reason=str(action.get("reason") or ""),
                peak_r=_f(action.get("peak_r"), 0.0),
                live_r=_f(action.get("live_r"), _f(agg_probe.get("aggregate_r"), 0.0)),
                now_iso=utc_now_iso(),
                cfg=_v16_entry_quality_config_from_env(),
            )
            if stamp:
                log_line(
                    f"{utc_now_iso()} {symbol} v16-noise-cooldown armed side={stamp.get('side')} "
                    f"reason={stamp.get('reason')} peak_r={stamp.get('peak_r')} live_r={stamp.get('live_r')}"
                )
        _clear_basket_runtime(state, symbol, grok=is_grok_close)
    elif act == "add_repair_leg" and not dry:
        repair_side = str(action.get("side") or "buy")
        prefix = list(m5_bars or [])
        if prefix:
            entry, sl, tp = _repair_geometry(prefix, repair_side, spread_abs)
            ts_close = hunter_brain._bar_close_ts(str(prefix[-1].get("ts") or ""))
            repair_decision = hunter_brain.Decision(
                ts_close=ts_close,
                symbol=symbol,
                action="enter",
                side=repair_side,
                entry_type="market",
                entry=entry,
                sl=sl,
                tp=tp,
                size_class="scout",
                leader_score=_f(action.get("conviction"), 0.0),
                p_win_est=0.5,
                setup="opening_manager_repair",
                reasons=[
                    f"OM Basket Doctor: เติมไม้ซ่อมตะกร้า (fast tick) / {action.get('note')}",
                    f"conviction={action.get('conviction')}",
                ],
                features={"om_action": {k: v for k, v in action.items() if k != "basket_runtime"}},
            )
            daily = _daily_state(state)
            executed = _execute_live_entry(
                executor,
                repair_decision,
                today_entry_count=int(daily.get("entries", 0)),
                today_losing_count=int(daily.get("loss_baskets", 0)),
                repair=True,
                risk_usd_override=_apply_v16_profit_controls(
                    executor.client,
                    state,
                    repair_decision,
                    _lane_actual_risk_usd(lane, _om_base_risk_usd(executor)),
                ),
            )
            if executed.get("action") == "entered":
                daily["entries"] = int(daily.get("entries", 0)) + 1

    journal.insert_basket_event(
        0,
        "om_action",
        {
            "symbol": symbol,
            "dry_run": dry,
            "action": act,
            "reason": action.get("reason"),
            "peak_r": action.get("peak_r"),
            "floor_r": action.get("floor_r"),
            "live_r": action.get("live_r"),
            "side": action.get("side"),
            "note": action.get("note"),
            "conviction": action.get("conviction"),
            "aggregate_pnl_usd": agg_probe.get("aggregate_pnl_usd"),
            "aggregate_unreliable": bool(agg_probe.get("unreliable")),
            "pnl_sources": agg_probe.get("pnl_sources"),
            "unreliable_pnl_position_ids": agg_probe.get("unreliable_pnl_position_ids"),
            "executed": executed,
        },
        label=_active_order_label(),
    )
    save_shadow_state(state)
    if act != "hold":
        log_line(
            f"{utc_now_iso()} {symbol} OM action={act} reason={action.get('reason')} "
            f"peak_r={action.get('peak_r')} live_r={action.get('live_r')} dry={dry}"
        )
    else:
        # Throttled heartbeat so the owner can watch the profit hunt live
        # without spamming (every OM_HOLD_HEARTBEAT_TICKS ticks per symbol).
        global _OM_HOLD_TICKS
        n = _OM_HOLD_TICKS.get(symbol, 0) + 1
        _OM_HOLD_TICKS[symbol] = n
        if action.get("live_r") is not None and n % OM_HOLD_HEARTBEAT_TICKS == 0:
            floor = action.get("floor_r")
            armed = " ARMED" if floor is not None else ""
            log_line(
                f"{utc_now_iso()} {symbol} OM hunting live_r={action.get('live_r')} "
                f"peak_r={action.get('peak_r')} floor_r={floor}{armed}"
            )
    return f"om_{act}"


_GOVERNOR_INSTANCE: DailyGovernor | None = None


def _get_governor() -> DailyGovernor:
    global _GOVERNOR_INSTANCE
    if _GOVERNOR_INSTANCE is None:
        _GOVERNOR_INSTANCE = DailyGovernor(_governor_config_from_env())
    return _GOVERNOR_INSTANCE


def governor_state_snapshot(state: dict[str, Any]) -> str:
    """Read-only: the governor's persisted state for TODAY (UTC) without
    mutating anything. Used by the entry-gate check in ``run_symbol_cycle``
    so the M5 entry path can refuse to trade a locked/stopped day without
    itself owning governor evaluation (that only happens in
    ``run_governor_tick``, once per fast tick, before entries are considered
    on that same tick)."""
    gov = state.get("governor") or {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if gov.get("date") != today:
        return "HUNTING"  # not yet evaluated today (or stale) -> default open
    return str(gov.get("state") or "HUNTING")


def run_governor_tick(
    mcp: Dexter3McpClient,
    journal: DecisionJournal,
    state: dict[str, Any],
    symbols: list[str],
    *,
    executor: Dexter3Executor | None,
) -> str:
    """Daily Mission Governor evaluation — once per fast tick, AFTER the
    per-symbol OM loop has populated ``state['governor']['floating_by_symbol']``
    (see ``run_om_tick``). Never raises: any failure is logged and treated as
    a no-op tick, same posture as every other fast-tick path in this file.

    On TARGET_LOCKED/LOSS_STOPPED with an open lane on ANY symbol: close
    every lane position across every symbol and journal a 'governor' basket
    event. The locked/stopped state then persists in shadow state for the
    rest of the UTC day (``_governor_state_for``) regardless of what a later
    tick's effective PnL would read back as — the mission is decided once
    per day, not re-litigated every 4 seconds.
    """
    try:
        gov_state = _governor_state_for(state)
        realized, pnls = _lane_realized_today(mcp, label_filter=_active_label_family())
        floating_by_symbol = (state.get("governor") or {}).get("floating_by_symbol") or {}
        floating = sum(_f(v, 0.0) for v in floating_by_symbol.values())

        governor = _get_governor()
        status = governor.status(realized, floating)
        status = _apply_v16_house_money_status(gov_state, status)
        new_state = status["state"]

        # Once locked/stopped, it STAYS locked/stopped for the rest of the
        # UTC day even if this tick's fresh status would read HUNTING again
        # (e.g. floating PnL wobbled back under target after the close-all
        # already fired) — the mission is decided once, not re-litigated.
        if gov_state.get("state") in ("TARGET_LOCKED", "LOSS_STOPPED"):
            effective_state = gov_state["state"]
            newly_triggered = False
        else:
            effective_state = new_state
            newly_triggered = new_state in ("TARGET_LOCKED", "LOSS_STOPPED")

        if newly_triggered:
            gov_state["state"] = effective_state
            gov_state["locked_pnl"] = status["effective_pnl"]
            gov_state["triggered_at"] = utc_now_iso()

            # Close every lane position across every symbol.
            closed_summary: dict[str, Any] = {}
            if executor is not None:
                # active_label stays the exact/full current label (needed
                # for the active_label == GROK_LABEL identity check below);
                # active_label_family is the FAMILY used for the lane match
                # itself so an already-open PRIOR-version position of this
                # same lane still gets closed on lock/stop.
                active_label = _active_order_label()
                active_label_family = _active_label_family()
                for symbol in symbols:
                    try:
                        positions = executor.client.get_positions()
                    except (McpClientError, McpZombieError) as exc:
                        log_line(f"{utc_now_iso()} governor close_all read_failed {symbol}: {exc}")
                        continue
                    lane = basket_live.lane_positions(positions, active_label_family)
                    lane = [p for p in lane if str(p.get("symbolName") or p.get("symbol") or "") in ("", symbol)]
                    if not lane:
                        continue
                    ids = [int(p.get("positionId") or p.get("id") or 0) for p in lane]
                    ids = [x for x in ids if x > 0]
                    reason = "governor_target_lock" if effective_state == "TARGET_LOCKED" else "governor_loss_stop"
                    closed_summary[symbol] = executor.execute_close_all(ids, reason=reason)
                    _clear_basket_runtime(state, symbol, grok=active_label == GROK_LABEL)

            if effective_state == "TARGET_LOCKED" and status.get("house_money_floor_triggered"):
                log_line(
                    f"{utc_now_iso()} house-money floor locked +${status['effective_pnl']:.2f} "
                    f"(floor=${status.get('house_money_floor_usd', status.get('target')):.2f})"
                )
            elif effective_state == "TARGET_LOCKED":
                log_line(
                    f"{utc_now_iso()} \U0001F3AF MISSION COMPLETE +${status['effective_pnl']:.2f} locked "
                    f"(target=${status['target']:.2f})"
                )
            else:
                log_line(
                    f"{utc_now_iso()} \U0001F6D1 daily loss cap — protecting capital "
                    f"(effective=${status['effective_pnl']:.2f}, cap=-${status['loss_cap']:.2f})"
                )

            journal.insert_basket_event(
                0,
                "governor",
                {
                    "state": effective_state,
                    "status": status,
                    "win_streak": DailyGovernor.win_streak_from_closes(pnls),
                    "closed": closed_summary,
                },
                label=_active_order_label(),
            )

        save_shadow_state(state)
        return f"governor_{gov_state.get('state', 'HUNTING').lower()}"
    except Exception as exc:  # noqa: BLE001 - governor tick must never crash the fast loop
        log_error("run_governor_tick", exc)
        return "governor_error"


def _lane_actual_risk_usd(lane: list[dict[str, Any]] | None, fallback: float) -> float:
    """ACTUAL dollar risk of the open lane legs: sum(|entry−SL| × volume).

    The R-base for aggregate_r/trail math. Using the static config risk was
    a live bug (2026-07-07): governor sized entries at ~$14.4 while the base
    stayed $0.50 → aggregate_r inflated ~29× → OM 'take' fired at +$0.60 and
    banked 11 straight winners at ~0.09R of their true risk."""
    total = 0.0
    for p in lane or []:
        try:
            entry = float(p.get("entryPrice") or p.get("price") or 0.0)
            sl = float(p.get("stopLoss") or p.get("stopLossPrice") or 0.0)
            vol = float(p.get("volumeInUnits") or p.get("volume") or 0.0)
        except (TypeError, ValueError):
            continue
        if entry > 0 and sl > 0 and vol > 0:
            total += abs(entry - sl) * vol
    return total if total > 0 else float(fallback)


def _om_base_risk_usd(executor: Dexter3Executor | None) -> float:
    if executor is not None:
        return _f(getattr(executor.config, "risk_usd", None), 0.5)
    return 0.5


def _resolve_live_executor(mcp: Dexter3McpClient, journal: DecisionJournal, live_flag: bool) -> Dexter3Executor | None:
    """Double opt-in: --live CLI flag AND DEXTER3_LIVE=1 env var, both required.

    Returns None (shadow-only, byte-identical to Phase 1) unless both gates
    pass — this is deliberately redundant with the demo-refusal gate inside
    Dexter3Executor itself; the two gates protect against different mistakes
    (a stray --live vs. a misconfigured/live-bound MCP).
    """
    if not live_flag:
        return None
    if os.environ.get(DEXTER3_LIVE_ENV_VAR) != "1":
        log_line(
            f"{utc_now_iso()} --live requested but {DEXTER3_LIVE_ENV_VAR}=1 env var not set — "
            "staying in shadow mode (double opt-in required, see blueprint P2)"
        )
        return None
    log_line(
        f"{utc_now_iso()} DEXTER3 LIVE MODE ENABLED — demo micro-entries may be placed "
        f"(label={_active_order_label()})"
    )
    return Dexter3Executor(mcp, journal, _executor_config_from_env())


def run_loop(symbols: list[str], poll_sec: int, live: bool = False) -> None:
    """Main loop — OPENING MANAGER fast tick (owner directive 2026-07-07).

    Supports DEXTER3_MODE=grok for independent Grok_v1.0 scalping process.
    Use separate processes + different MODE for full isolation (recommended).
    """
    mode = os.environ.get("DEXTER3_MODE", "v16").lower().strip()
    is_grok = mode == "grok"
    is_vp = mode == "vp"

    # Force Grok label early for order creation (live entries)
    if is_grok and GROK_LABEL:
        import dexter3.executor as _ex
        _ex.LABEL = GROK_LABEL
        print(f"[Grok] Forced executor LABEL to {GROK_LABEL}", flush=True)

    # VP canary lane (2026-07-11): same isolation pattern as grok — its own
    # broker label so fable/grok loops never touch VP positions and vice versa.
    if is_vp:
        from dexter3.volume_profile import VP_LABEL as _VP_LABEL

        import dexter3.executor as _ex
        _ex.LABEL = _VP_LABEL
        print(f"[VP] Forced executor LABEL to {_VP_LABEL}", flush=True)

    if is_grok and GROK_LABEL:
        active_label = GROK_LABEL
    elif is_vp:
        from dexter3.volume_profile import VP_LABEL as _VP_LABEL

        active_label = _VP_LABEL
    else:
        active_label = LIVE_ORDER_LABEL
    active_lock_name = "grok-v1.0" if is_grok else "dexter3"
    # Single source of truth for the version string (2026-07-15
    # versioned-labels design): dexter3.executor.VERSION already reads +
    # sanitizes the SAME DEXTER3_FABLE_VERSION env var (default
    # "v1.7-selective-edge") at its own module-load time — re-reading the
    # raw env var here would drift from what actually got baked into LABEL.
    fable_version = FABLE_VERSION

    # The loop ticks every fast_tick_sec. Every fast tick runs OM on the
    # active lane; the M5 entry path still runs only on poll cadence.
    mode = os.environ.get("DEXTER3_MODE", "v16").lower()
    acquire_loop_lock(mode)
    mcp = make_client()
    baskets: dict[str, PaperBasket] = {}
    fast_tick_sec = _fast_tick_sec_from_env()
    try:
        with DecisionJournal() as journal:
            executor = _resolve_live_executor(mcp, journal, live)
            refresh_every_cycles = _learning_refresh_every_cycles(poll_sec)
            log_line(
                f"{utc_now_iso()} dexter3 shadow loop started symbols={symbols} poll_sec={poll_sec} "
                f"fast_tick_sec={fast_tick_sec} mode={mode} label={active_label} "
                f"version={fable_version if not is_grok else 'grok-v1.0'} "
                f"live={'ON' if executor is not None else 'off'}"
            )
            if not is_grok:
                entry_cfg = _v16_entry_quality_config_from_env()
                log_line(
                    f"{utc_now_iso()} v16 entry-quality config: enabled={entry_cfg.enabled} "
                    f"cooldown_enabled={entry_cfg.cooldown_enabled} "
                    f"chase_hard_block={entry_cfg.chase_hard_block} "
                    f"block_chase_bypass_on_aligned_trending={entry_cfg.block_chase_bypass_on_aligned_trending} "
                    f"weak_hard_skip={entry_cfg.weak_hard_skip} "
                    f"min_leader_score={entry_cfg.min_leader_score}"
                )
            tick_count = 0
            while True:
                tick_count += 1
                state = load_shadow_state()
                for symbol in symbols:
                    try:
                        run_weekly_flatten_tick(executor, symbol)
                        run_om_tick(mcp, journal, state, symbol, executor=executor)
                    except McpZombieError as exc:
                        log_line(f"{utc_now_iso()} {symbol} OM MCP_ZOMBIE: {exc}")
                    except Exception as exc:  # noqa: BLE001 - OM tick must never crash the loop
                        log_error(f"run_om_tick({symbol})", exc)

                # Daily Mission Governor (owner directive 2026-07-07): runs
                # AFTER the OM ticks (floating_by_symbol is fresh) and BEFORE
                # the M5 entry path so a lock/stop suppresses entries on this
                # very tick. Guards its own exceptions internally.
                run_governor_tick(mcp, journal, state, symbols, executor=executor)

                if m5_entry_tick_due(tick_count, poll_sec, fast_tick_sec):
                    try:
                        run_once(
                            symbols, mcp, journal, baskets, executor=executor, refresh_every_cycles=refresh_every_cycles
                        )
                    except McpZombieError as exc:
                        log_line(f"{utc_now_iso()} MCP_ZOMBIE (loop-level): {exc}")
                        time.sleep(MCP_ZOMBIE_SLEEP_SEC)
                        continue
                    except Exception as exc:  # noqa: BLE001 - loop must never die
                        log_error("run_loop", exc)
                time.sleep(max(1, fast_tick_sec))
    except KeyboardInterrupt:
        log_line(f"{utc_now_iso()} dexter3 shadow loop stopped (KeyboardInterrupt)")
    finally:
        mode = os.environ.get("DEXTER3_MODE", "v16").lower()
        release_loop_lock(mode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dexter3 M5 shadow/live loop")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="comma-separated symbol list")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run a single evaluation cycle per symbol and exit")
    mode.add_argument("--loop", action="store_true", help="run continuously until interrupted")
    parser.add_argument("--poll-sec", type=int, default=DEFAULT_POLL_SEC, help="seconds between loop iterations")
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Enable demo micro-entries for 'enter' decisions. Also requires the "
            f"{DEXTER3_LIVE_ENV_VAR}=1 environment variable (double opt-in) — "
            "default is OFF/shadow-only."
        ),
    )
    parser.add_argument(
        "--grok",
        action="store_true",
        help="Run in Grok_v1.0 scalping mode (forces GROK_LABEL when creating orders, uses separate OM + state keys + lock). Recommended: run in a separate terminal for 100%% independence from V1.6.",
    )
    args = parser.parse_args(argv)

    if args.grok:
        os.environ.setdefault("DEXTER3_MODE", "grok")
        if GROK_LABEL:
            import dexter3.executor as _ex
            _ex.LABEL = GROK_LABEL

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("no symbols provided", file=sys.stderr)
        return 2

    if args.loop:
        if args.grok:
            os.environ["DEXTER3_MODE"] = "grok"
        run_loop(symbols, args.poll_sec, live=args.live)
        return 0

    if args.grok:
        os.environ["DEXTER3_MODE"] = "grok"

    # H6 fix (2026-07-15 cross-lane entanglement audit): --once previously
    # only ever patched executor.LABEL for --grok, never for the VP canary
    # lane (DEXTER3_MODE=vp is env-var-driven only — there is no --vp CLI
    # flag, same as run_loop's own is_vp check above). Without this, a --once
    # invocation launched with DEXTER3_MODE=vp already set in the environment
    # would place any live entry under the DEFAULT (Fable) LABEL instead of
    # VP_LABEL — mirrors run_loop's own VP patch exactly.
    if os.environ.get("DEXTER3_MODE", "v16").lower().strip() == "vp":
        from dexter3.volume_profile import VP_LABEL as _VP_LABEL

        import dexter3.executor as _ex
        _ex.LABEL = _VP_LABEL
        print(f"[VP] Forced executor LABEL to {_VP_LABEL}", flush=True)

    # --once: no lock required for a single pass, but still respect an
    # already-running loop's lock to avoid racing its state file.
    once_lock_file = GROK_LOCK_FILE if args.grok else LOCK_FILE
    if once_lock_file.exists():
        try:
            old_pid = int(once_lock_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old_pid = 0
        if old_pid > 0 and _pid_alive(old_pid):
            print(f"dexter3 shadow loop already running (pid={old_pid}) — skipping --once", file=sys.stderr)
            return 1

    mcp = make_client()
    baskets: dict[str, PaperBasket] = {}
    try:
        with DecisionJournal() as journal:
            executor = _resolve_live_executor(mcp, journal, args.live)
            run_once(symbols, mcp, journal, baskets, executor=executor)
    except McpZombieError as exc:
        log_line(f"{utc_now_iso()} MCP_ZOMBIE (--once): {exc}")
        return 3
    except Exception as exc:  # noqa: BLE001 - report but do not crash ugly
        log_error("main(--once)", exc)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
