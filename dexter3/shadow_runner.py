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
from dataclasses import dataclass
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
from dexter3 import market_state
from dexter3 import stop_floor
from dexter3 import vp_lane
from dexter3 import channelfade
from dexter3.executor import LABEL as LIVE_ORDER_LABEL
from dexter3.executor import VERSION as FABLE_VERSION
from dexter3.executor import LABEL_FAMILY as FABLE_LABEL_FAMILY
from dexter3.executor import Dexter3Executor, ExecutorConfig, label_matches_family, position_id_of
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
DAYTREND_STATE_FILE = RUNTIME / "dexter3_daytrend_shadow_state.json"
SCALP_STATE_FILE = RUNTIME / "dexter3_scalp_shadow_state.json"
DPULL_STATE_FILE = RUNTIME / "dexter3_dpull_shadow_state.json"
DPULL_CS_STATE_FILE = RUNTIME / "dexter3_dpull_cs_shadow_state.json"
CHF_STATE_FILE = RUNTIME / "dexter3_chf_shadow_state.json"
LOG_FILE = RUNTIME / "dexter3_shadow.log"
# H5 (2026-07-15 cross-lane entanglement audit): per-lane log files. Fable's
# path stays LOG_FILE unchanged (confirmed the only code reader,
# ops/dexter3_telegram_watcher.py, hardcodes exactly this fable path — see
# _active_log_file below), so keeping it as-is means zero breakage there.
GROK_LOG_FILE = RUNTIME / "dexter3_grok_shadow.log"
VP_LOG_FILE = RUNTIME / "dexter3_vp_shadow.log"
DAYTREND_LOG_FILE = RUNTIME / "dexter3_daytrend_shadow.log"
SCALP_LOG_FILE = RUNTIME / "dexter3_scalp_shadow.log"
DPULL_LOG_FILE = RUNTIME / "dexter3_dpull_shadow.log"
DPULL_CS_LOG_FILE = RUNTIME / "dexter3_dpull_cs_shadow.log"
CHF_LOG_FILE = RUNTIME / "dexter3_chf_shadow.log"
LOCK_FILE = RUNTIME / "dexter3_loop.lock"
GROK_LOCK_FILE = RUNTIME / "dexter3_grok_loop.lock"
VP_LOCK_FILE = RUNTIME / "dexter3_vp_loop.lock"
DAYTREND_LOCK_FILE = RUNTIME / "dexter3_daytrend_shadow.lock"
SCALP_LOCK_FILE = RUNTIME / "dexter3_scalp_shadow.lock"
DPULL_LOCK_FILE = RUNTIME / "dexter3_dpull_shadow.lock"
DPULL_CS_LOCK_FILE = RUNTIME / "dexter3_dpull_cs_shadow.lock"
CHF_LOCK_FILE = RUNTIME / "dexter3_chf_shadow.lock"

DEFAULT_SYMBOLS = ("XAUUSD", "BTCUSD")
DEFAULT_POLL_SEC = 20
M5_BAR_SEC = 300
MCP_ZOMBIE_SLEEP_SEC = 60
MIN_M5_BARS = 60
# dpull-cs broker-SL backstop: cap the self-heal amend retries per position.
# The broker order IS replaced within a couple of attempts even though the
# loop's stale position stream + uncertain amend response make it LOOK like it
# keeps failing (2026-07-23 investigation) — so a small cap stops an infinite
# every-8s re-amend without leaving a genuinely-failed one un-widened.
_CS_BACKSTOP_MAX_TRIES = 3
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


def _daytrend_producer_enabled() -> bool:
    """DAYTREND lane (owner deploy 2026-07-16): with-the-day pullback
    continuation — dexter3/daytrend.py carries the evidence block."""
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "daytrend":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "daytrend"


def _scalp_producer_enabled() -> bool:
    """SCALP lane (owner order 2026-07-17, grok's successor): sdzone x bias x
    bank-green — dexter3/sd_zones.py carries the evidence block."""
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "scalp":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "scalp"


def _dpull_producer_enabled() -> bool:
    """DPULL lane (owner sign-off 2026-07-22): decide_daytrend producer with
    the deep-pullback bypass stream x limit -0.5R x convex a2.0 h24 — the
    dtcap matrix's both-segments/3-window winner. Evidence block in
    dexter3/daytrend.py next to DPULL_LABEL."""
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "dpull":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "dpull"


def _dpull_cs_producer_enabled() -> bool:
    """DPULL-CS lane (owner choice ค, 2026-07-23): same decide_daytrend
    producer as dpull, run in parallel with the vol-gated close-stop OM exit
    for a forward head-to-head. Evidence block by DPULL_CS_LABEL in
    dexter3/daytrend.py."""
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "dpull-cs":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "dpull-cs"


def _channelfade_producer_enabled() -> bool:
    """CHANNELFADE lane (Edge A, owner 2026-07-24 "สร้าง อย่าตัด"): the
    range-phase edge-fade producer (dexter3/channelfade.py), forward-A/B
    canary — DEFAULT OFF (owner sign-off required). Two ways in:
    DEXTER3_PRODUCER=channelfade (producer-only override) or
    DEXTER3_MODE=channelfade (the full canary lane: own label
    dexter3:chf:canary + own state file + own lock, same isolation pattern as
    the vp/dpull lanes). replay UNDER-RATES range edges (vp is replay-marginal
    yet live +23.58) so this lane's promotion is judged on FORWARD realized
    PnL, not the in-sample fit."""
    if os.environ.get("DEXTER3_PRODUCER", "").strip().lower() == "channelfade":
        return True
    return os.environ.get("DEXTER3_MODE", "").strip().lower() == "channelfade"


def _alt_producer_enabled() -> bool:
    """Non-hunt producers (vp / daytrend / scalp / dpull / dpull-cs /
    channelfade) share the same runner posture: v16 gate bypass (no
    hunt-committee features), hunt sizing-selector bypass (their proofs sized
    flat), the VP entry gate (day-open bias + no-trade window; trivially true
    for daytrend/dpull whose signals are with-bias by construction, and
    bias-neutral for the channelfade range lane), and the deep 340-bar M5
    fetch."""
    return (_vp_producer_enabled() or _daytrend_producer_enabled()
            or _scalp_producer_enabled() or _dpull_producer_enabled()
            or _dpull_cs_producer_enabled() or _channelfade_producer_enabled())


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
    if current_mode == "daytrend":
        from dexter3.daytrend import DAYTREND_LABEL

        return DAYTREND_LABEL
    if current_mode == "scalp":
        from dexter3.sd_zones import SCALP_LABEL

        return SCALP_LABEL
    if current_mode == "dpull":
        from dexter3.daytrend import DPULL_LABEL

        return DPULL_LABEL
    if current_mode == "dpull-cs":
        from dexter3.daytrend import DPULL_CS_LABEL

        return DPULL_CS_LABEL
    if current_mode == "channelfade":
        from dexter3.channelfade import CHF_LABEL

        return CHF_LABEL
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
    if current_mode == "daytrend":
        from dexter3.daytrend import DAYTREND_LABEL_FAMILY

        return DAYTREND_LABEL_FAMILY
    if current_mode == "scalp":
        from dexter3.sd_zones import SCALP_LABEL_FAMILY

        return SCALP_LABEL_FAMILY
    if current_mode == "dpull":
        from dexter3.daytrend import DPULL_LABEL_FAMILY

        return DPULL_LABEL_FAMILY
    if current_mode == "dpull-cs":
        from dexter3.daytrend import DPULL_CS_LABEL_FAMILY

        return DPULL_CS_LABEL_FAMILY
    if current_mode == "channelfade":
        from dexter3.channelfade import CHF_LABEL_FAMILY

        return CHF_LABEL_FAMILY
    return FABLE_LABEL_FAMILY


def _active_state_file(mode: str | None = None) -> Path:
    current_mode = (mode or os.environ.get("DEXTER3_MODE", "v16")).lower().strip()
    if current_mode == "grok":
        return GROK_STATE_FILE
    if current_mode == "vp":
        return VP_STATE_FILE
    if current_mode == "daytrend":
        return DAYTREND_STATE_FILE
    if current_mode == "scalp":
        return SCALP_STATE_FILE
    if current_mode == "dpull":
        return DPULL_STATE_FILE
    if current_mode == "dpull-cs":
        return DPULL_CS_STATE_FILE
    if current_mode == "channelfade":
        return CHF_STATE_FILE
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
    if current_mode == "daytrend":
        return DAYTREND_LOG_FILE
    if current_mode == "scalp":
        return SCALP_LOG_FILE
    if current_mode == "dpull":
        return DPULL_LOG_FILE
    if current_mode == "dpull-cs":
        return DPULL_CS_LOG_FILE
    if current_mode == "channelfade":
        return CHF_LOG_FILE
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
    # VP lane (2026-07-16): the convex exit's proven hold is 240min (replay
    # h48) — longer than the 180min basket default, which would cap-stop the
    # basket before the trail's own time stop. Env-tunable, default untouched.
    raw_ts = os.environ.get("DEXTER3_BASKET_TIME_STOP_MIN")
    if raw_ts:
        try:
            kw["time_stop_min"] = int(raw_ts)
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
    DEXTER3_DAILY_LOSS_USD, DEXTER3_BASE_RISK_FRAC, DEXTER3_MAX_RISK_FRAC,
    DEXTER3_GOVERNOR_BYPASS_MIN_SCORE (2026-07-22 high-conviction bypass —
    unset/invalid = feature OFF, same posture as every knob above).
    """
    kw: dict[str, Any] = {}
    for env, field_name in (
        ("DEXTER3_CAPITAL_USD", "capital_usd"),
        ("DEXTER3_DAILY_TARGET_USD", "daily_target_usd"),
        ("DEXTER3_DAILY_LOSS_USD", "daily_loss_usd"),
        ("DEXTER3_BASE_RISK_FRAC", "base_risk_frac"),
        ("DEXTER3_MAX_RISK_FRAC", "max_risk_frac"),
        ("DEXTER3_GOVERNOR_BYPASS_MIN_SCORE", "bypass_min_score"),
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
        f"base_risk_frac={cfg.base_risk_frac} max_risk_frac={cfg.max_risk_frac} "
        f"bypass_min_score={cfg.bypass_min_score}"
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


# ---------------------------------------------------------------------------
# FIX 2b (2026-07-15 OM-blindspot fix #2) — per-position peak-R durability
# ledger, keyed by the broker's OWN position_id rather than
# (symbol, oldest_open_ts).
#
# ``basket_runtime`` above already persists peak_r to disk every tick
# (save_shadow_state runs unconditionally in run_om_tick, and
# load_shadow_state reloads the FULL state fresh at the top of every loop
# iteration) — a plain process restart does NOT lose it. The real gap docs/
# AGENT_SYNC_BOARD.md's 2026-07-15 ~07:30Z "OM ROUND-TRIP INCIDENT" (defect
# #2, "OM amnesia") points at: that key resets to a fresh peak the instant
# ``oldest_open_ts`` changes or ``_clear_basket_runtime`` fires — which
# happens on a transient empty-lane read (a momentary reconcile hiccup, NOT
# a real close) or a label-family boundary crossing (exactly what happened
# during today's versioned-labels cutover, 90239b5). This ledger tracks the
# SAME peak_r/floor_r by ``position_id`` instead — an identity that survives
# every one of those resets — so a resetting basket_runtime can re-seed its
# starting peak from real history instead of always restarting blind at
# live_r. Never LOWERS an existing recorded peak; purges only entries whose
# position_id the caller's ALREADY-FETCHED lane confirms is no longer open
# (no new broker call).
# ---------------------------------------------------------------------------


def _position_peak_ledger(state: dict[str, Any]) -> dict[str, Any]:
    return state.setdefault("position_peak_r", {})


def _position_ids_in_lane(lane: list[dict[str, Any]]) -> list[int]:
    out: list[int] = []
    for p in lane:
        try:
            pid = int(p.get("positionId") or p.get("id") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            out.append(pid)
    return out


def _update_position_peak_ledger(
    state: dict[str, Any],
    symbol: str,
    lane: list[dict[str, Any]],
    peak_r: float | None,
    floor_r: float | None,
) -> None:
    """Raise (never lower) every open lane position's recorded peak_r/
    floor_r, then purge any THIS-SYMBOL ledger entry whose position_id is no
    longer in ``lane``. Called with ``lane=[]`` (the flat-lane case) purges
    every remaining entry for ``symbol`` — the lane read itself IS the
    "position no longer open" evidence, no extra broker call needed."""
    ledger = _position_peak_ledger(state)
    live_ids = set(_position_ids_in_lane(lane))
    if peak_r is not None:
        for pid in live_ids:
            key = str(pid)
            entry = ledger.get(key) or {}
            prev_peak = _f(entry.get("peak_r"), peak_r)
            new_entry: dict[str, Any] = {
                "symbol": symbol,
                "peak_r": max(prev_peak, float(peak_r)),
                "updated_at": utc_now_iso(),
            }
            if floor_r is not None:
                new_entry["floor_r"] = float(floor_r)
            elif "floor_r" in entry:
                new_entry["floor_r"] = entry["floor_r"]
            ledger[key] = new_entry
    for key in list(ledger.keys()):
        entry = ledger.get(key) or {}
        if str(entry.get("symbol") or "") != symbol:
            continue  # a peer symbol's entries are untouched by this call
        try:
            pid = int(key)
        except ValueError:
            ledger.pop(key, None)
            continue
        if pid not in live_ids:
            ledger.pop(key, None)


def _seed_peak_r_from_ledger(
    state: dict[str, Any], lane: list[dict[str, Any]], reset_peak_r: float
) -> float:
    """When a fresh basket_runtime is about to start peak_r at
    ``reset_peak_r`` (``_update_peak_r``'s own "new basket" reset guard just
    fired), resume from the highest recorded peak any position_id CURRENTLY
    in ``lane`` already has in the durability ledger, if higher. Never
    returns less than ``reset_peak_r`` — this only ever raises the starting
    point, exactly like the ledger's own raise-only update rule."""
    ledger = _position_peak_ledger(state)
    best = float(reset_peak_r)
    for pid in _position_ids_in_lane(lane):
        entry = ledger.get(str(pid))
        if isinstance(entry, dict):
            best = max(best, _f(entry.get("peak_r"), reset_peak_r))
    return best


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


# --- broker market-state (2026-07-25, owner cTrader ‖-pause observation) -------
_SYMBOL_META_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_SYMBOL_META_TTL_SEC = 3600.0  # schedule/tradingMode change ~never; refresh hourly


def _cached_symbol_meta(mcp: Any, symbol: str) -> dict[str, Any] | None:
    """Broker symbol spec (tradingMode/trading_enabled/schedule/...) with a long
    TTL cache and FAIL-OPEN semantics: any error (no daemon, subprocess
    transport, transport failure, unrecognised proto) -> None, so the
    market-state gate degrades to feed-freshness only and never hard-blocks.
    Never raises."""
    now = datetime.now(timezone.utc).timestamp()
    hit = _SYMBOL_META_CACHE.get(symbol)
    if hit is not None and (now - hit[0]) < _SYMBOL_META_TTL_SEC:
        return hit[1]
    meta: dict[str, Any] | None = None
    try:
        meta = mcp.get_symbol_details(symbol)
    except Exception:
        meta = None
    _SYMBOL_META_CACHE[symbol] = (now, meta)
    return meta


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
    if _alt_producer_enabled():
        # VP/daytrend lanes (owner deploy 2026-07-16): the v16 gate scores
        # hunt-committee features (leader_score etc.) that VP decisions do
        # not carry — leader_score=0.0 would block EVERY VP entry on
        # min_leader_score. The 3-window replay proof ran gates=none; the
        # VP lane's own gate (day-open bias + no-trade window) is applied
        # by the caller right after this returns.
        return {
            "allow": True,
            "reason": "vp_bypass",
            "a_plus": False,
            "a_plus_reason": "",
            "cooldown_bypassed": False,
            "features": {"vp_bypass": True},
        }
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
# How often a PERSISTENT get_deals outage is re-logged (2026-07-25 audit fix).
# The old code logged exactly once ever, so an all-day outage — during which
# the daily loss cap is effectively disabled because the realized total can no
# longer grow — produced a single line nobody would notice.
LANE_REALIZED_FAILURE_RELOG_SEC = 300.0
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
        # 2026-07-25 audit fix: a persistent get_deals outage silently turned
        # the daily loss cap OFF. The cached total cannot grow while closes
        # keep landing, so `effective_pnl` freezes at its last good value and
        # NEITHER the close-all trigger NOR the pre-entry refusal can ever
        # fire — and the single `logged_failure` line meant an all-day outage
        # produced exactly ONE log entry. The cache is still served (a stale
        # total is better than a fabricated zero), but the failure is now
        # re-logged on a bounded interval and, when there has NEVER been a
        # successful read, it is escalated so "0.0 realized" is never mistaken
        # for "no losses today".
        first_ever = float(cache.get("epoch", 0.0) or 0.0) <= 0.0
        last_warn = float(cache.get("last_failure_log_epoch", 0.0) or 0.0)
        if not cache.get("logged_failure") or (now_epoch - last_warn) >= LANE_REALIZED_FAILURE_RELOG_SEC:
            severity = "NO CACHE EVER — daily cap is BLIND" if first_ever else "serving stale cache"
            log_line(
                f"{utc_now_iso()} governor lane_realized_today get_deals_failed "
                f"({severity}; label={label_filter} cached_sum={float(cache.get('sum', 0.0)):+.2f} "
                f"age={int(now_epoch - float(cache.get('epoch', 0.0) or now_epoch))}s): {exc}"
            )
            cache["logged_failure"] = True
            cache["last_failure_log_epoch"] = now_epoch
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
    elif mode == "daytrend":
        lock_file, lock_name = DAYTREND_LOCK_FILE, "daytrend-canary"
    elif mode == "scalp":
        lock_file, lock_name = SCALP_LOCK_FILE, "scalp-canary"
    elif mode == "dpull":
        lock_file, lock_name = DPULL_LOCK_FILE, "dpull-canary"
    elif mode == "dpull-cs":
        lock_file, lock_name = DPULL_CS_LOCK_FILE, "dpull-cs-canary"
    elif mode == "channelfade":
        lock_file, lock_name = CHF_LOCK_FILE, "channelfade-canary"
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
    elif mode == "daytrend":
        lock_file = DAYTREND_LOCK_FILE
    elif mode == "scalp":
        lock_file = SCALP_LOCK_FILE
    elif mode == "dpull":
        lock_file = DPULL_LOCK_FILE
    elif mode == "dpull-cs":
        lock_file = DPULL_CS_LOCK_FILE
    elif mode == "channelfade":
        lock_file = CHF_LOCK_FILE
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

    # VP mode needs a much deeper prefix than hunt: decide_vp's profile
    # window is 288 bars (+3 context = MIN_BARS 291, else it skips every
    # bar with "bars<291" — the exact latent gap that surfaced at first
    # enable 2026-07-16), and the day-open bias must see back to the 00Z
    # anchor bar (worst case 288 bars; 340 covers ~28h, > any anchor age).
    m5_bars = fetch_fresh_m5(mcp, symbol, count=340 if _alt_producer_enabled() else MIN_M5_BARS)
    if len(m5_bars) < MIN_M5_BARS:
        return f"insufficient_m5_bars({len(m5_bars)})"

    # --- broker market-state gate (2026-07-25, owner cTrader ‖-pause obs) ------
    # Additive + env-gated (default off => fail-open, byte-identical behaviour).
    # Reads the broker's OWN open/closed signal — feed freshness (newest M5 bar
    # age) plus tradingMode when the daemon surfaces it — instead of relying
    # only on the hardcoded weekly_close_policy time window, and turns "market
    # closed" into an OBSERVABLE skip reason rather than a silent
    # no_new_m5_close. weekly_close_policy stays as the time-based backstop.
    if _env_bool("DEXTER3_MARKET_STATE_GATE", False):
        _meta = _cached_symbol_meta(mcp, symbol)
        # 2026-07-25 audit fix (fail-OPEN, self-inflicted): _iso_to_epoch
        # returns the sentinel 0.0 for a missing/unparseable bar timestamp, and
        # feeding 0.0 + M5_BAR_SEC = 300 to the gate made bar_age ~1.7e9s, i.e.
        # a bogus "stale_feed" that HALTS entries on every lane running this
        # gate — inverting market_state's documented fail-OPEN contract. Pass
        # None (unknown age) instead so the verdict degrades to open.
        _newest_raw = _iso_to_epoch(str(m5_bars[-1].get("ts") or ""))
        _newest_epoch = (_newest_raw + M5_BAR_SEC) if _newest_raw > 0 else None
        _ms = market_state.evaluate_market_state(
            now_ts=datetime.now(timezone.utc).timestamp(),
            newest_bar_epoch=_newest_epoch,
            trading_enabled=(_meta or {}).get("trading_enabled"),
            schedule=(_meta or {}).get("schedule"),
            stale_sec=_env_float("DEXTER3_MARKET_STALE_SEC", market_state.DEFAULT_STALE_SEC),
        )
        if not _ms["open"]:
            log_line(
                f"{utc_now_iso()} {symbol} market_closed_broker "
                f"source={_ms['source']} reason={_ms['reason']} "
                f"bar_age={_ms.get('bar_age_sec')}s trading_enabled={_ms.get('trading_enabled')} "
                f"schedule_intervals={_ms.get('schedule_intervals')}"
            )
            return f"market_closed_broker:{_ms['source']}"

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
                # 2026-07-15 repair-scalp-harvest design: exclude ":rsh" scalp
                # legs from the M5-close basket-management lane too (same
                # rationale as run_om_tick's own lane fetch above) — a
                # harvester scalp is family-owned but must never be swept
                # into the PARENT basket's aggregate_r or close_all_* sweep.
                lane = basket_live.lane_positions(
                    executor.client.get_positions(),
                    _active_label_family(),
                    exclude_label_suffix=_repair_harvest_label_suffix(),
                )
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
            if vp_lane.trail_mode():
                # convex/plain trail lanes (vp/daytrend, 2026-07-16): the OM
                # fast tick + broker SL/TP + caps are the ONLY exits the
                # proofs measured. The hunt-era M5 campaign brain
                # (_manage_lane_basket: close_all_in_profit on level_lost,
                # repair legs, resolve targets) closed the VP lane's FIRST
                # live position at +1.71R via level_lost 16:15Z — profitable
                # that time, but an unproven second exit brain. HOLD only.
                decision = hunter_brain.Decision(
                    ts_close=utc_now_iso(), symbol=symbol, action="manage",
                    side=None, entry_type=None, entry=None, sl=None, tp=None,
                    size_class="small", leader_score=0.0, p_win_est=0.0,
                    setup="basket_hold",
                    reasons=[f"trail_mode={vp_lane.trail_mode()}: campaign brain deferred "
                             "(OM trail + broker SL/TP + caps are the only exits)"],
                    session="", features={},
                )
            else:
                # BASKET ACTIVE → this bar's action is campaign management
                decision, basket_action = _manage_lane_basket(
                    executor, symbol, bar_ts, prefix, lane, state, spread_abs
                )
        elif is_newest and _scalp_producer_enabled():
            # SCALP lane (owner order 2026-07-17): sdzone producer ONLY —
            # zone re-entry confirm; day-open bias via vp_entry_gate; exits
            # = OM bank mode (close-based +bank_r) + broker SL + 60min cap.
            from dexter3 import daytrend as _daytrend
            from dexter3 import market_lens as _ml

            sc_session = str(_ml.session_context(bar_ts).get("value") or "unknown")
            decision = _daytrend.decide_sdzone_live(symbol, prefix, spread_abs, session=sc_session)
        elif is_newest and (_dpull_producer_enabled() or _dpull_cs_producer_enabled()):
            # DPULL lane (owner sign-off 2026-07-22) + DPULL-CS parallel lane
            # (owner choice ค 2026-07-23): SAME decide_daytrend producer, cap12
            # + deep-pullback bypass 3xATR + limit -0.5R x convex a2.0 h24. The
            # ONLY difference between the two is the OM exit env: dpull uses
            # the intrabar convex stop, dpull-cs adds the vol-gated close-stop
            # (DEXTER3_OM_CONVEX_CLOSE_STOP=1). Identical decision path here.
            from dexter3 import daytrend as _daytrend
            from dexter3 import market_lens as _ml

            dp_session = str(_ml.session_context(bar_ts).get("value") or "unknown")
            decision = _daytrend.decide_daytrend(symbol, prefix, spread_abs, session=dp_session)
        elif is_newest and _channelfade_producer_enabled():
            # CHANNELFADE lane (Edge A, owner 2026-07-24): the range-phase
            # edge-fade producer — fade the box edges toward the vp POC. Exit
            # is PLAIN (DEXTER3_OM_TRAIL_MODE=plain: a fade has a fixed target
            # and must NOT ride). Off-by-default; forward-A/B canary — replay
            # under-rates range edges (vp replay-marginal yet live +23.58) so
            # the honest arbiter is forward realized PnL.
            from dexter3 import market_lens as _ml

            chf_session = str(_ml.session_context(bar_ts).get("value") or "unknown")
            decision = channelfade.decide_channelfade(symbol, prefix, spread_abs, session=chf_session)
        elif is_newest and _daytrend_producer_enabled():
            # DAYTREND lane (owner deploy 2026-07-16) — with-the-day pullback
            # continuation; evidence + parity notes in dexter3/daytrend.py.
            from dexter3 import daytrend as _daytrend
            from dexter3 import market_lens as _ml

            dt_session = str(_ml.session_context(bar_ts).get("value") or "unknown")
            decision = _daytrend.decide_daytrend(symbol, prefix, spread_abs, session=dt_session)
            if decision.action != "enter" and _daytrend.dayreversal_enabled():
                # DAYREVERSAL twin (owner order 2026-07-16 "เปิดเลย"): when
                # continuation has nothing (incl. the range_cap handoff), hunt
                # the DZ/SZ structure-flip. Rare by construction — precedence
                # is irrelevant in practice, continuation-first by design.
                rev = _daytrend.decide_dayreversal(symbol, prefix, spread_abs, session=dt_session)
                if rev.action == "enter":
                    decision = rev
            if decision.action != "enter" and _daytrend.sdzone_enabled():
                # SD-ZONE entries (owner order 2026-07-17 "โอกาสหายาก เปิดเลย"):
                # third producer — zone re-entry confirm from the shared
                # dexter3/sd_zones.py engine; the day-open bias gate applies
                # downstream (the proven x-bias combo).
                sdz = _daytrend.decide_sdzone_live(symbol, prefix, spread_abs, session=dt_session)
                if sdz.action == "enter":
                    decision = sdz
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
            # Volatility-normalized SL floor (2026-07-25 whole-project audit —
            # the #1 measured edge: SL<1.2xTR = -0.398R vs SL>=1.2xTR = +0.116R,
            # 4/4 refutation controls passed). Runs FIRST so every downstream
            # reader (pa_eye, gates, sizing, executor) and the journal row below
            # all see the FINAL geometry. Off by default; see _apply_sl_floor.
            _apply_sl_floor(decision, prefix)
            # Price Action Eye Phase A shadow (2026-07-15, additive): MUST
            # run before insert_decision just below so the journaled
            # features_json snapshot captures pa_eye automatically (see
            # _apply_pa_eye_shadow's docstring) -- placed here rather than
            # beside the v16 entry-quality gate further down because that
            # gate only evaluates on is_newest LIVE entries, while the Eye
            # shadow-journals EVERY decided enter (both lanes, catch-up
            # bars included) per the Phase A design doc.
            _apply_pa_eye_shadow(decision, prefix)
        elif decision.action == "skip":
            _stamp_skip_bias_fallback(decision, prefix)
        # H2 (2026-07-15 cross-lane entanglement audit): stamp this row with
        # the writing lane's own order label so per-lane journal queries
        # (empirical stats, skip fear-cost) never pool Fable/Grok/VP rows.
        decision_row_id = journal.insert_decision(decision, label=_active_order_label())
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
                gov_state = str(gov.get("state") or "")
                # Computed eagerly (even on the locked/unverified branches
                # below, where its result goes unused) so the pre-entry cap
                # check below has it without a second MCP-adjacent call —
                # _lane_realized_today is cache-backed, so this is cheap.
                risk_info = _governor_entry_risk(mcp, decision, state)
                # High-conviction bypass (2026-07-22 owner audit): checked
                # here too (not just inside _governor_entry_risk, which only
                # ever evaluates the LOSS-cap half of this gate) because this
                # cached gov_state also carries TARGET_LOCKED, which
                # _governor_entry_risk never checks at all -- this is the
                # ONLY gate standing between a locked/stopped day and a
                # candidate strong enough to clear the bypass threshold.
                gov_score = float(getattr(decision, "leader_score", 0.0) or 0.0)
                gov_bypassed = (
                    gov_state in ("TARGET_LOCKED", "LOSS_STOPPED")
                    and _get_governor().bypass_allowed(gov_score)
                )
                if gov_bypassed:
                    log_line(
                        f"{utc_now_iso()} {decision.symbol} governor_bypass "
                        f"state={gov_state} score={gov_score:.3f} (entry allowed)"
                    )
                if gov_state in ("TARGET_LOCKED", "LOSS_STOPPED") and not gov_bypassed:
                    # Owner's daily mission rule (2026-07-07) outranks
                    # participation-first for EXECUTION only — the decision above
                    # is still journaled; we just do not put money on it today.
                    status += f":governor_{gov_state.lower()}"
                elif lane is None:
                    status += ":live_skipped_lane_unverified"
                elif risk_info is not None and not risk_info.get("allow", True):
                    # Pre-entry loss-cap refusal (closes the race window
                    # between one tick's governor evaluation and the next
                    # tick's entry attempt — 2026-07-15).
                    status += f":live_blocked_{risk_info.get('reason', 'governor_cap')}"
                else:
                    if gov_bypassed:
                        status += f":governor_{gov_state.lower()}_bypassed"
                    daily = _daily_state(state)
                    base_risk_usd = (risk_info or {}).get("risk_usd")
                    if base_risk_usd is None:
                        base_risk_usd = executor.config.risk_usd
                    if _alt_producer_enabled():
                        # VP/daytrend lanes (2026-07-16): the hunt sizing selectors
                        # (anti-chase / pullback / v16 profit controls) read
                        # hunt-committee features VP decisions do not carry,
                        # and the 3-window proof sized every accepted VP
                        # trade at flat base risk — bypass, keep the
                        # governor's risk_for_entry result untouched.
                        risk_usd_override = float(base_risk_usd)
                    else:
                        risk_usd_override = _apply_anti_chase_gate(decision, h1_ctx, float(base_risk_usd))
                        risk_usd_override = _apply_pullback_gate(decision, prefix, float(risk_usd_override))
                        risk_usd_override = _apply_v16_profit_controls(
                            mcp, state, decision, float(risk_usd_override)
                        )
                        # day-open bias downsize (owner 2026-07-16) — the
                        # two-window-proven direction layer, applied to the
                        # hunt lanes as sizing, never a block (demo rule).
                        risk_usd_override = _apply_hunt_dayopen_bias(
                            decision, prefix, float(risk_usd_override)
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
                    # VP lane gate (owner deploy 2026-07-16, additive): the
                    # day-open bias + no-trade window from the 3-window replay
                    # proof, same result shape as the v16 gate so the blocked
                    # path below is shared. Only ever evaluated in VP mode.
                    if quality.get("allow", True):
                        if _alt_producer_enabled():
                            if str(getattr(decision, "setup", "")).startswith("dayreversal"):
                                # the reversal is counter-bias BY DESIGN (the
                                # DZ/SZ flip) — only the no-trade window
                                # applies, never the day-open bias gate.
                                if vp_lane.in_no_trade_window(utc_now_iso()):
                                    quality = {"allow": False, "reason": "vp_no_trade_window",
                                               "a_plus": False, "a_plus_reason": "",
                                               "cooldown_bypassed": False, "features": {}}
                            else:
                                quality = vp_lane.vp_entry_gate(str(decision.side), prefix, utc_now_iso())
                        elif (
                            os.environ.get("DEXTER3_HUNT_DAYOPEN_BIAS", "").strip().lower() == "skip"
                            and _hunt_bias_is_counter(decision, prefix)
                        ):
                            # owner order 2026-07-16 (option A, overriding the
                            # old no-hard-blockers rule): counter-bias hunt
                            # entries are BLOCKED outright — the exact filter
                            # the two-window proof measured. ต่ำเปิดห้าม buy.
                            quality = {"allow": False, "reason": "hunt_dayopen_bias",
                                       "a_plus": False, "a_plus_reason": "",
                                       "cooldown_bypassed": False, "features": {}}
                        elif vp_lane.in_no_trade_window(
                            utc_now_iso(), os.environ.get("DEXTER3_HUNT_NO_TRADE_UTC", "")
                        ):
                            # owner 2026-07-16 ("ควรมีเวลาที่ไม่ควรเทรด"):
                            # entry pause around the daily close/reopen —
                            # env-gated off by default; open positions are
                            # untouched (OM keeps managing them).
                            quality = {"allow": False, "reason": "hunt_no_trade_window",
                                       "a_plus": False, "a_plus_reason": "",
                                       "cooldown_bypassed": False, "features": {}}
                    if not quality.get("allow", True):
                        status += f":live_blocked_{quality.get('reason', 'quality')}"
                    else:
                        risk_usd_override = _apply_v18_size_levers(
                            quality, float(base_risk_usd), float(risk_usd_override)
                        )
                        if _lane_limit_entry_enabled():
                            # Synthetic LIMIT (the two-window/3-window proven
                            # entry layer, producer-agnostic): store the
                            # intent; the fast tick fires a market order when
                            # the touch-side quote reaches the level, or
                            # expires it at the TTL. Newest allowed signal
                            # replaces any pending intent.
                            intent = _lane_make_limit_intent(
                                decision, prefix, float(risk_usd_override)
                            )
                            prev_intent = state.get("vp_limit_intent")
                            replaced = bool(prev_intent)
                            # vp-funnel instrumentation (2026-07-25): carry the
                            # journal row id so the intent's final fate can be
                            # stamped back on the decision that produced it,
                            # and record the REPLACED fate of the intent this
                            # one evicts (a signal every M5 close can evict a
                            # pending intent before it ever fills — the
                            # suspected 72-for-0 mechanism on vp_poc_reversion).
                            intent["decision_row_id"] = decision_row_id
                            if replaced:
                                _stamp_intent_fate(
                                    journal, prev_intent, "replaced",
                                    {"by_row_id": decision_row_id, "at": utc_now_iso()},
                                )
                            state["vp_limit_intent"] = intent
                            log_line(
                                f"{utc_now_iso()} {symbol} vp_limit_intent_set side={intent['side']} "
                                f"level={intent['level']} sl={intent['sl']} ttl_min="
                                f"{(intent['deadline_epoch'] - intent['created_epoch']) / 60:.0f} "
                                f"replaced={replaced}"
                            )
                            status += ":vp_limit_intent_set"
                            exec_result = {"action": "limit_intent_set"}
                        else:
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

        if is_newest and decision.action == "enter" and isinstance(decision.features, dict):
            # 2026-07-22 B-tier verdict blocker: the row above was journaled
            # BEFORE the gate chain ran, but the gates stamp their evidence
            # (v16_entry_quality incl. b_tier, anti_chase, pullback_gate,
            # smart_exit meta) into decision.features AFTER — so the journal
            # never carried any of it and the B-tier verdict was reduced to
            # journalctl greps. One re-sync after the whole chain; never
            # allowed to break the loop.
            # 2026-07-25 vp-funnel instrumentation: the accumulated ``status``
            # already names EVERY downstream branch this enter took (governor
            # lock, lane unverified, risk-cap refusal, quality/vp gate block,
            # limit-intent set, live fill) but it only ever reached the log
            # line — so "155 enter decisions -> 6 broker fills" was not
            # answerable from the DB. Stamp it on the row itself, in ONE place
            # after the whole chain, so no branch can be missed.
            _stamp_entry_outcome(decision, status)
            try:
                journal.update_decision_features(decision_row_id, decision.features)
            except Exception as exc:  # noqa: BLE001 - observability must not kill the cycle
                log_line(f"{utc_now_iso()} {symbol} decision_features_resync_failed: {exc}")

        mark_m5_close_seen(state, symbol, bar_ts)
        save_shadow_state(state)
        if late_sec > 90:
            status += f":late{int(late_sec)}s"
        statuses.append(status)
    return ";".join(statuses)


def _apply_sl_floor(decision: hunter_brain.Decision, prefix: list[dict[str, Any]]) -> None:
    """Widen a too-tight stop to a volatility-normalized floor, preserving RR.

    THE #1 finding of the 2026-07-25 whole-project audit and the only pre-trade
    discriminator that survived refutation (4/4 controls): trades entered with
    ``SL < 1.2 x true range`` averaged **-0.398R** (N=30) while ``SL >= 1.2x``
    averaged **+0.116R** (N=25). The mechanism is measured, not fitted — the
    tight cohort reached its take-profit only 6-8% of the time vs 27-29% for
    the wide cohort *at comparable planned RR*, the opposite of what a random
    walk implies, i.e. the tight stops were being taken out by noise before the
    thesis could resolve (38 of 105 trades died inside 15 minutes at -1.110R
    with a median peak MFE of just 0.23R).

    This is ONE root cause the project previously fixed four separate times as
    four per-lane diseases (dpull intrabar stop, fable sweep wick-SL, dpull-cs
    close-stop, the 07-25 -9.56 h1_context loss). It is therefore implemented
    in ONE central place on the shared run loop rather than in each of the four
    producers — deliberately avoiding the "wiring lives in N places, one was
    missed" defect class that has bitten this repo repeatedly.

    Env (default OFF => byte-identical behaviour):
      DEXTER3_SL_FLOOR_TR_MULT   floor = mult x median true range (0 = off)
      DEXTER3_SL_FLOOR_MAX_ABS   hard ceiling on the widened stop distance.
                                 At the XAU 1-ounce volume floor dollar risk ==
                                 stop distance, and the executor REFUSES when
                                 that exceeds DEXTER3_MIN_VOLUME_RISK_ABS_CAP_USD
                                 (9-12 on the live units) — so this bound keeps
                                 a volatile bar widening the stop as far as is
                                 affordable instead of silently converting the
                                 trade into a refusal, which would confound
                                 "better stops" with "fewer trades" in the
                                 forward measurement.
      DEXTER3_SL_FLOOR_LOOKBACK  bars for the median TR (default 14)

    Always stamps ``features["sl_floor"]`` — including when it does nothing —
    so the journal can PROVE the path executed on live data (the audit's #1
    defect class was flags that were set but never actually ran). Never raises.
    """
    try:
        mult = _env_float("DEXTER3_SL_FLOOR_TR_MULT", 0.0)
        if mult <= 0.0:
            return
        if not isinstance(getattr(decision, "features", None), dict):
            return
        lookback = int(_env_float("DEXTER3_SL_FLOOR_LOOKBACK", 14.0))
        tr = stop_floor.median_true_range(prefix, lookback=lookback)
        out = stop_floor.apply_floor(
            side=str(getattr(decision, "side", "") or ""),
            entry=_f(getattr(decision, "entry", 0.0)),
            sl=_f(getattr(decision, "sl", 0.0)),
            tp=_f(getattr(decision, "tp", 0.0)),
            tr=tr,
            mult=mult,
            max_sl_abs=_env_float("DEXTER3_SL_FLOOR_MAX_ABS", 0.0),
        )
        if out is None:
            decision.features["sl_floor"] = {"applied": False, "tr": round(tr, 5), "mult": mult}
            return
        before_sl, before_tp = decision.sl, decision.tp
        decision.sl = out["sl"]
        decision.tp = out["tp"]
        decision.features["sl_floor"] = out["meta"]
        decision.reasons = list(getattr(decision, "reasons", []) or []) + [
            f"sl_floor: {out['meta']['sl_dist_before']:.2f}->{out['meta']['sl_dist_after']:.2f} "
            f"({out['meta']['sl_over_tr_before']:.2f}->{out['meta']['sl_over_tr_after']:.2f} xTR), RR preserved"
        ]
        log_line(
            f"{utc_now_iso()} {decision.symbol} sl_floor_applied side={decision.side} "
            f"tr={tr:.3f} sl {before_sl}->{decision.sl} tp {before_tp}->{decision.tp} "
            f"dist {out['meta']['sl_dist_before']:.2f}->{out['meta']['sl_dist_after']:.2f} "
            f"capped={out['meta']['capped_by_max_abs']}"
        )
    except Exception as exc:  # noqa: BLE001 - geometry tuning must never kill a cycle
        log_line(f"{utc_now_iso()} sl_floor_failed (decision left unchanged): {exc}")


def _classify_entry_outcome(status: str) -> tuple[str, str]:
    """Map an accumulated cycle ``status`` string to (outcome, reason).

    Outcomes: ``filled`` (a real order exists), ``limit_intent`` (pending —
    its final fate is stamped later by the fast tick), ``blocked`` (a gate
    refused it), ``no_exec`` (nothing downstream ran, e.g. shadow mode).
    Pure/table-driven so the funnel query has stable buckets.
    """
    s = str(status or "")
    if ":live_entered" in s:
        return "filled", "entered"
    if ":vp_limit_intent_set" in s:
        return "limit_intent", "intent_set"
    for marker, outcome in (
        (":live_blocked_", "blocked"),
        (":governor_", "blocked"),
        (":live_skipped_", "blocked"),
        (":live_", "no_exec"),
    ):
        idx = s.find(marker)
        if idx >= 0:
            reason = s[idx + len(marker):].split(":")[0] or marker.strip(":_")
            if marker == ":governor_" and reason.endswith("_bypassed"):
                continue  # bypassed = allowed through; a later marker decides
            return outcome, reason
    return "no_exec", "no_downstream_branch"


def _stamp_entry_outcome(decision: hunter_brain.Decision, status: str) -> None:
    """Record how an enter decision actually ended downstream (2026-07-25).

    Pure observability — writes only into ``decision.features`` (which the
    caller's post-gate resync persists); never changes sizing, gating or any
    order. Wrapped by the caller's try/except at the journal boundary; kept
    exception-safe here too so a malformed status can never kill a cycle."""
    try:
        if not isinstance(getattr(decision, "features", None), dict):
            return
        outcome, reason = _classify_entry_outcome(status)
        decision.features["entry_outcome"] = {
            "outcome": outcome,
            "reason": reason,
            "status": str(status or "")[:200],
            "setup": str(getattr(decision, "setup", "") or ""),
            "side": str(getattr(decision, "side", "") or ""),
        }
    except Exception:  # noqa: BLE001 - observability must never break the loop
        return


def _stamp_intent_fate(
    journal: Any,
    intent: dict[str, Any] | None,
    fate: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Stamp a synthetic-limit intent's FINAL fate onto its originating
    decision row (2026-07-25 vp-funnel instrumentation).

    The fast tick settles an intent (fill/expire/kill) long after the decision
    was journaled, and a newer signal can REPLACE a pending intent outright —
    the suspected reason vp booked 72 ``vp_poc_reversion`` enter decisions and
    0 broker fills. Uses ``merge_decision_features`` so the gate evidence
    written by the post-gate resync is preserved. Fully exception-safe and a
    no-op without a journal / row id: pure observability."""
    try:
        if journal is None or not isinstance(intent, dict):
            return
        row_id = intent.get("decision_row_id")
        if row_id is None:
            return
        patch = {"intent_fate": {"fate": str(fate), **(extra or {})}}
        journal.merge_decision_features(int(row_id), patch)
    except Exception:  # noqa: BLE001 - observability must never break the loop
        return


def _stamp_skip_bias_fallback(decision: hunter_brain.Decision, prefix: list[dict[str, Any]]) -> None:
    """fear-cost bias fallback (2026-07-22, owner audit): vp/daytrend/scalp
    skip decisions never carry the hunt-lens component keys
    (liquidity_sweep/displacement/compression_release/close_location_pressure/
    swing_structure) that market_lens.leader_score() scores against -- vp
    stamps features={} outright, daytrend/scalp stamp their own
    producer-specific shape. Every skip from those 3 lanes was therefore
    GUARANTEED score=0 -> side=None -> permanently "no_determinable_side" in
    skip_evaluator (measured: 398/400 unevaluable skip_outcomes rows). Stamp
    the SAME day-open-bias direction these lanes' own entry gates already
    gate on, into ``decision.features["skip_bias_side"]``, as a fallback
    candidate side ``skip_evaluator.determine_candidate_side`` can fall back
    to when the hunt-lens score comes up empty (it always tries the hunt-lens
    score FIRST, so this never overrides a real hunt-derived side). A neutral
    bias (0, no clear anchor yet) is left unstamped on purpose -- there is
    genuinely no directional lean to simulate. Mutates ``decision.features``
    in place; never raises -- a bad/short prefix just leaves the skip
    unevaluable, same as before this fallback existed."""
    try:
        anchor_hour = int(os.environ.get("DEXTER3_SKIP_BIAS_ANCHOR_HOUR", "0"))
        bias, _hours = vp_lane.dayopen_bias(prefix, anchor_hour)
        if bias > 0:
            decision.features["skip_bias_side"] = "buy"
        elif bias < 0:
            decision.features["skip_bias_side"] = "sell"
    except Exception:
        pass


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
    immediately closed by the governor's loss-stop (2026-07-15). Unless the
    decision's own ``leader_score`` clears the configured high-conviction
    bypass (``DailyGovernor.bypass_allowed``, 2026-07-22 owner audit — OFF by
    default), in which case the refusal is skipped and normal sizing below
    still runs; the bypass is logged either way so it's auditable.

    Never raises — a failure (sizing OR the cap check above) falls back to
    the executor's static config risk (returns None), same fail-open posture
    as every other entry-sizing gate in this file."""
    try:
        realized, pnls = _lane_realized_today(mcp, label_filter=_active_label_family())
        governor = _get_governor()
        cfg = governor.config
        floating = 0.0
        if state is not None:
            floating_by_symbol = (state.get("governor") or {}).get("floating_by_symbol") or {}
            floating = sum(_f(v, 0.0) for v in floating_by_symbol.values())
        effective = realized + floating
        candidate_score = float(getattr(decision, "leader_score", 0.0) or 0.0)
        if effective <= -abs(cfg.daily_loss_usd):
            if governor.bypass_allowed(candidate_score):
                log_line(
                    f"{utc_now_iso()} {decision.symbol} governor_bypass_pre_entry "
                    f"effective={effective:.2f} cap=-{abs(cfg.daily_loss_usd):.2f} "
                    f"score={candidate_score:.3f} >= min={cfg.bypass_min_score:.3f} (entry allowed)"
                )
            else:
                log_line(
                    f"{utc_now_iso()} {decision.symbol} governor_loss_stop_pre_entry "
                    f"effective={effective:.2f} cap=-{abs(cfg.daily_loss_usd):.2f} "
                    f"score={candidate_score:.3f} (entry refused)"
                )
                return {
                    "allow": False,
                    "reason": "governor_loss_stop_pre_entry",
                    "effective_pnl": round(effective, 4),
                }
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


def _hunt_bias_is_counter(
    decision: hunter_brain.Decision, m5_prefix: list[dict[str, Any]]
) -> bool:
    """True when the decision's side runs AGAINST an established day-open
    bias (shared classification for both the skip and downsize modes).
    Always journals onto decision.features; never raises (fail-open)."""
    try:
        anchor_hour = int(_env_float("DEXTER3_HUNT_BIAS_ANCHOR_HOUR", 0))
        min_hours = _env_float("DEXTER3_HUNT_BIAS_MIN_HOURS", 1.0)
        bias, hours = vp_lane.dayopen_bias(m5_prefix, anchor_hour)
        counter = (
            bias != 0 and hours >= min_hours
            and ((str(decision.side) == "buy") != (bias > 0))
        )
        if isinstance(decision.features, dict):
            decision.features["hunt_dayopen_bias"] = {
                "bias": bias, "hours": round(hours, 2), "counter": counter,
            }
        return counter
    except Exception:
        return False


def _apply_hunt_dayopen_bias(
    decision: hunter_brain.Decision, m5_prefix: list[dict[str, Any]], risk_usd: float
) -> float:
    """Day-open bias DOWNSIZE for the hunt lanes (owner 2026-07-16: "เอาสิ่งที่
    เราเจอวันนี้ไปปรับปรุง [fable/grok]"). Two-window proven layer on the hunt
    producer at the live ladder (6k −148→−93/−66→−44, 10k −187→−105/−127→−82:
    bleed roughly halved, still net-negative — a loss-reducer, not an edge).
    DOWNSIZE, never block, per the standing demo rule: a counter-bias entry
    still fires at DEXTER3_HUNT_BIAS_DOWNSIZE_MULT x risk. Env-gated
    DEXTER3_HUNT_DAYOPEN_BIAS=downsize, default off; classification always
    journaled onto decision.features."""
    if os.environ.get("DEXTER3_HUNT_DAYOPEN_BIAS", "").strip().lower() != "downsize":
        return risk_usd
    try:
        mult = _env_float("DEXTER3_HUNT_BIAS_DOWNSIZE_MULT", 0.25)
        counter = _hunt_bias_is_counter(decision, m5_prefix)
        if isinstance(decision.features, dict) and "hunt_dayopen_bias" in decision.features:
            decision.features["hunt_dayopen_bias"]["mult"] = mult if counter else 1.0
        return risk_usd * mult if counter else risk_usd
    except Exception:
        return risk_usd


def _lane_limit_entry_enabled() -> bool:
    """Trough-limit entry gating per producer: VP lanes read the VP env;
    hunt lanes opt in via DEXTER3_HUNT_LIMIT_DIP_R>0 (two-window proven
    entry layer on the hunt producer — moves ONLY the entry; ladder/TP
    exits stay the live config)."""
    if _vp_producer_enabled():
        return vp_lane.limit_entry_enabled()
    return _env_float("DEXTER3_HUNT_LIMIT_DIP_R", 0.0) > 0.0


def _lane_make_limit_intent(
    decision: hunter_brain.Decision, prefix: list[dict[str, Any]], risk_usd: float
) -> dict[str, Any]:
    if _vp_producer_enabled():
        return vp_lane.make_limit_intent(decision, prefix, risk_usd, utc_now_iso())
    # dpull lane (owner sign-off 2026-07-22): SAME hunt-limit env knobs for
    # the entry (dip/ttl), but the CONVEX trail is the exit — so it needs the
    # FAR protective TP (prefer_signal_tp=False), exactly like the VP lane,
    # NOT the daytrend producer's signal TP (the day extreme). Keeping the
    # signal TP would cap the +100R convex ride the dtcap matrix measured for
    # "limit -0.5R x convex a2.0 h24" at the day extreme. Hunt lanes (limit x
    # ladder, TP is structural) keep prefer_signal_tp=True unchanged.
    # 2026-07-25 audit fix: this omitted _dpull_cs_producer_enabled(), so the
    # dpull-cs lane kept decide_daytrend's SIGNAL tp (the day extreme) instead
    # of the far protective cap its own DEXTER3_VP_FAR_TP_R=12 configures —
    # truncating the convex ride the lane exists to capture, and making the
    # dpull vs dpull-cs A/B differ in TP PLACEMENT as well as the close-stop it
    # was designed to isolate. The producer dispatch already pairs both
    # predicates; this line was the missed second wiring site (the same defect
    # class as the chf label bug).
    prefer_signal_tp = not (_dpull_producer_enabled() or _dpull_cs_producer_enabled())
    return vp_lane.make_limit_intent(
        decision, prefix, risk_usd, utc_now_iso(),
        dip_r=_env_float("DEXTER3_HUNT_LIMIT_DIP_R", 0.4),
        ttl_min=_env_float("DEXTER3_HUNT_LIMIT_TTL_MIN", 30.0),
        prefer_signal_tp=prefer_signal_tp,
    )


def _service_vp_limit_intent(
    mcp: Dexter3McpClient,
    state: dict[str, Any],
    symbol: str,
    executor: Dexter3Executor,
    journal: Any | None = None,
) -> dict[str, Any] | None:
    """Fast-tick servicing of the VP lane's synthetic limit intent (owner
    deploy 2026-07-16). Fires a MARKET entry when the touch-side quote
    reaches the level (>= one 8s tick of slippage, journaled as fill-vs-level
    delta so the synthetic's cost is measured); expires the intent past its
    TTL. Never raises — the caller wraps it like run_om_tick. Returns the
    executor result on a fill, else None."""
    intent = state.get("vp_limit_intent")
    if not isinstance(intent, dict) or str(intent.get("symbol")) != symbol:
        return None
    fill_px: float | None = None
    confirm_tf = vp_lane.confirm_mode()
    if confirm_tf:
        # owner order 2026-07-16 ("เบรคและกลับตัวเท่านั้น ไม่รับมีด"): never
        # fill on a bare touch — drive the zone-confirm state machine on
        # CLOSED confirm-TF bars. A close beyond the structural SL kills the
        # setup with NO trade (the knife the blind limit would have caught).
        now_e = _iso_to_epoch(utc_now_iso())
        if now_e > _f(intent.get("deadline_epoch")):
            state.pop("vp_limit_intent", None)
            log_line(f"{utc_now_iso()} {symbol} vp_limit_intent_expired level={intent.get('level')} "
                     f"side={intent.get('side')} confirm_tf={confirm_tf}")
            _stamp_intent_fate(journal, intent, "expired",
                               {"confirm_tf": confirm_tf, "at": utc_now_iso()})
            save_shadow_state(state)
            return None
        tf_sec = 60 if confirm_tf == "m1" else 300
        try:
            bars = mcp.get_trendbars(symbol, confirm_tf, 8)
        except Exception as exc:
            log_line(f"{utc_now_iso()} {symbol} vp_intent_confirm_bars_failed: {exc}")
            return None
        closed = [b for b in bars if _iso_to_epoch(str(b.get("ts") or "")) + tf_sec <= now_e + 1]
        verdict, fill_px = vp_lane.advance_confirm_intent(intent, closed)
        if verdict == "killed":
            state.pop("vp_limit_intent", None)
            log_line(f"{utc_now_iso()} {symbol} vp_limit_intent_killed_break level={intent.get('level')} "
                     f"sl={intent.get('sl')} side={intent.get('side')} confirm_tf={confirm_tf}")
            _stamp_intent_fate(journal, intent, "killed_break",
                               {"confirm_tf": confirm_tf, "at": utc_now_iso()})
            save_shadow_state(state)
            return None
        if verdict != "fill":
            save_shadow_state(state)   # persist confirm_touched/confirm_last_ts
            return None
        touch_px = _f(fill_px)
    else:
        try:
            spot = mcp.get_spot_price(symbol)
        except Exception as exc:
            log_line(f"{utc_now_iso()} {symbol} vp_intent_spot_read_failed: {exc}")
            return None
        bid = _f(spot.get("bid"), 0.0)
        ask = _f(spot.get("ask"), 0.0)
        verdict = vp_lane.check_intent_fill(intent, bid=bid, ask=ask, now_iso=utc_now_iso())
        if verdict == "expired":
            state.pop("vp_limit_intent", None)
            log_line(f"{utc_now_iso()} {symbol} vp_limit_intent_expired level={intent.get('level')} "
                     f"side={intent.get('side')}")
            _stamp_intent_fate(journal, intent, "expired", {"at": utc_now_iso()})
            save_shadow_state(state)
            return None
        if verdict != "fill":
            return None
        touch_px = ask if str(intent.get("side")) == "buy" else bid
    state.pop("vp_limit_intent", None)
    decision = vp_lane.intent_to_decision(intent, hunter_brain.Decision, utc_now_iso(),
                                          entry_px=fill_px)
    daily = _daily_state(state)
    exec_result = _execute_live_entry(
        executor,
        decision,
        today_entry_count=int(daily.get("entries", 0)),
        today_losing_count=int(daily.get("loss_baskets", 0)),
        risk_usd_override=_f(intent.get("risk_usd"), None),
    )
    _stamp_intent_fate(journal, intent, str(exec_result.get("action") or "unknown"),
                       {"at": utc_now_iso(), "fill_px": fill_px, "touch_px": touch_px})
    if exec_result.get("action") == "entered":
        daily["entries"] = int(daily.get("entries", 0)) + 1
        if not _is_grok_mode():
            # same cool-down clear a direct market fill performs
            state.pop("v16_entry_cooldown", None)
        # Stamp the convex-exit inputs for run_om_tick (atr measured at
        # signal time, stop distance of the FILLED geometry — the confirm
        # close for reversal-confirmed fills, the level for touch fills).
        # Inert for ladder lanes — only read when DEXTER3_OM_TRAIL_MODE=convex.
        soft_stop_pts = abs(_f(decision.entry) - _f(decision.sl)) or _f(intent.get("stop_pts"), 0.0)
        state["vp_convex"] = {
            "atr_pts": _f(intent.get("atr_pts"), 0.0),
            "stop_pts": soft_stop_pts,
        }
        # dpull-cs (2026-07-23): the order was SIZED to the soft SL (soft-stop
        # = -1R, matching the replay). The BROKER SL is widened to a far
        # backstop by _ensure_dpull_cs_backstop on the OM fast tick, NOT here
        # -- an entry-time amend fires ~1s after the fill, before get_positions
        # propagates the new position, and was refused (position_not_found)
        # every time, leaving dpull-cs a clone of base dpull. The OM tick reads
        # fresh positions and self-heals (retries until it sticks).
    log_line(
        f"{utc_now_iso()} {symbol} vp_limit_intent_{exec_result.get('action', 'unknown')} "
        f"level={intent.get('level')} touch_px={touch_px:.5f} "
        f"fill_vs_level={touch_px - _f(intent.get('level')):+.5f} side={intent.get('side')}"
    )
    save_shadow_state(state)
    return exec_result


def _execute_live_entry(
    executor: Dexter3Executor,
    decision: hunter_brain.Decision,
    *,
    today_entry_count: int = 0,
    today_losing_count: int = 0,
    repair: bool = False,
    risk_usd_override: float | None = None,
    smart_exit: dict[str, Any] | None = None,
    repair_context: dict[str, Any] | None = None,
    basket_authorized: bool = False,
    label_override: str | None = None,
) -> dict[str, Any]:
    """Resolve account state and place a live micro-entry. Never raises.

    ``repair_context`` (additive, default None): forwarded to
    ``executor.execute_repair_leg`` unchanged — see
    ``Dexter3Executor._build_repair_lineage`` for the expected shape. On the
    non-repair path it is now ALSO forwarded (2026-07-15 repair-scalp-harvest
    design — previously ignored there; the Repair-Scalp Harvester needs
    lineage on a plain ``execute_entry`` call, not a ``execute_repair_leg``
    one, since it opens a NEW-labeled mirror scalp rather than a same-basket
    repair leg).

    ``basket_authorized`` (additive, default False -> unchanged behavior):
    forwarded to ``executor.execute_entry`` on the non-repair path only —
    lets a caller (the harvester) bypass the duplicate-label pre-flight gate
    the same way a repair leg does, without going through
    ``execute_repair_leg``'s own basket_repair_leg journaling.

    ``label_override`` (additive, default None): forwarded to
    ``executor.execute_entry`` on the non-repair path only — see that
    method's own docstring.
    """
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
                repair_context=repair_context,
            )
        else:
            result = executor.execute_entry(
                decision,
                account_state,
                today_entry_count=today_entry_count,
                today_losing_count=today_losing_count,
                risk_usd_override=risk_usd_override,
                smart_exit=smart_exit,
                basket_authorized=basket_authorized,
                repair_context=repair_context,
                label_override=label_override,
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


def _parent_position_ids_oldest_first(lane: list[dict[str, Any]]) -> list[int]:
    """Open lane position ids for repair-lineage journaling, OLDEST leg
    first (index 0) so ``Dexter3Executor._build_repair_lineage`` resolves
    ``parent_setup`` from the ORIGINAL leg's own entry_executed row, not an
    arbitrary/later one (2026-07-15 repair-lineage-enrichment fix). Mirrors
    ``basket_live._position_open_ts``'s key-fallback chain (duplicated per
    this module's own no-cross-import-of-a-peer-module's-private-helpers
    convention — see basket_live.py's docstring) purely to sort; never
    raises on malformed entries."""

    def _open_ts(p: dict[str, Any]) -> str:
        return str(
            p.get("openTime") or p.get("openTimestamp") or p.get("open_ts")
            or p.get("openedAt") or p.get("ts") or ""
        ).strip()

    def _pid(p: dict[str, Any]) -> int:
        try:
            return int(p.get("positionId") or p.get("id") or 0)
        except (TypeError, ValueError):
            return 0

    ordered = sorted((p for p in lane if _pid(p) > 0), key=_open_ts)
    return [_pid(p) for p in ordered]


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

    agg = basket_live.aggregate_lane(
        lane, base_risk_usd=_lane_actual_risk_usd(lane, executor.config.risk_usd, _soft_stop_pts(state))
    )
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
        # Same source normal entries use (hunter_brain.decide()) — 2026-07-15
        # repair-lineage-enrichment fix: previously omitted entirely, which
        # left every repair entry_executed row stamped session="".
        session_label = str(lens.get("session_context", {}).get("value") or "unknown")
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
            session=session_label,
        )
        repair_context = {
            "parent_position_ids": _parent_position_ids_oldest_first(lane),
            "basket_id": agg.get("oldest_open_ts"),
            "basket_side": basket_side,
            "basket_agg_r_at_repair": agg.get("aggregate_r"),
            "basket_pnl_at_repair": agg.get("aggregate_pnl_usd"),
            "level_lost": evidence.get("level_lost"),
            "close_beyond": evidence.get("m5_close_beyond"),
        }
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
            repair_context=repair_context,
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


# ---------------------------------------------------------------------------
# REPAIR-SCALP HARVESTER (owner hypothesis, 2026-07-15 — replay-validated on
# BOTH segments, see docs/AGENT_SYNC_BOARD.md 2026-07-15 ~08:15Z and
# scripts/dexter3_repair_scalp_replay.py for the exact semantics this engine
# keeps identical).
#
# CONCEPT: when a lane's basket is TRAPPED (a single open leg whose
# aggregate_r has fallen to/below ``-trigger_r``), harvest the MIRROR side
# with v1.0-style bank-green scalps: enter at each M5 close with no scalp
# currently open, bank the WHOLE scalp at the first M5 close where its own
# floating R >= ``bank_target_r`` (no TP — banking is the exit), re-enter at
# the M5 close AFTER the one that resolved it, repeat until the PARENT basket
# resolves (closes/vanishes) — bounded by a per-episode scalp cap and a
# cumulative loss-stop.
#
# MODES (env ``DEXTER3_REPAIR_HARVEST``): off (default, zero engine
# activity) | shadow (journals every would-be scalp and resolves its
# counterfactual outcome against subsequent REAL M5 bars — zero orders) |
# live (real market orders). An unrecognized value degrades to shadow with a
# one-time log note — nothing can go live by typo (mirrors
# ``_apply_pa_eye_shadow``'s mode handling).
#
# CRITICAL ISOLATION: every scalp's broker label is
# ``f"{_active_order_label()}:{suffix}"`` (env
# ``DEXTER3_REPAIR_HARVEST_LABEL_SUFFIX``, default "rsh"). This label is
# EXCLUDED from ``basket_live.lane_positions`` (see ``run_om_tick`` /
# ``run_symbol_cycle``'s own lane fetches above) so a harvester scalp's
# floating PnL never distorts the PARENT basket's aggregate_r or gets swept
# by a basket close_all — while STILL matching the lane's FAMILY prefix
# everywhere ownership is family-based: vanish reconcile
# (``Dexter3Executor.reconcile_vanished_lane_positions`` matches via
# ``label_matches_family``, unaffected by a trailing suffix), the H4
# account-risk cap (``Dexter3Executor._account_open_risk_cap_refusal`` scans
# every position whose label starts with "dexter3", suffix included), and
# the Daily Mission Governor's realized PnL (``_lane_realized_today`` also
# matches via ``label_matches_family``). The duplicate-label pre-flight gate
# is bypassed the same way a repair leg bypasses it — ``basket_authorized``.
# ---------------------------------------------------------------------------

DEXTER3_REPAIR_HARVEST_ENV_VAR = "DEXTER3_REPAIR_HARVEST"
_RSH_MODE_WARNED: set[str] = set()


@dataclass(frozen=True)
class RepairHarvestConfig:
    """Replay-validated defaults (2026-07-15 owner hypothesis — see
    ``scripts/dexter3_repair_scalp_replay.py``'s CLI defaults, which this
    mirrors exactly): trigger 1.2R, bank 0.2R, scalp SL = 1.0x parent risk
    distance, scalp max-hold 12 M5 bars."""

    trigger_r: float = 1.2
    bank_target_r: float = 0.2
    scalp_sl_frac: float = 1.0
    scalp_max_hold_bars: int = 12
    max_scalps_per_episode: int = 8
    episode_loss_stop_r: float = 1.5


def _repair_harvest_mode_from_env() -> str:
    """off (zero engine activity) | shadow (journal-only counterfactual) |
    live (real orders). Any other value degrades to shadow with a ONE-TIME
    log note per distinct value — nothing can go live by typo."""
    raw = os.environ.get(DEXTER3_REPAIR_HARVEST_ENV_VAR, "off").strip().lower()
    if raw == "off":
        return "off"
    if raw in ("shadow", "live"):
        return raw
    if raw not in _RSH_MODE_WARNED:
        _RSH_MODE_WARNED.add(raw)
        log_line(
            f"{utc_now_iso()} repair-harvest: unrecognized {DEXTER3_REPAIR_HARVEST_ENV_VAR}={raw!r} "
            "-- mode not recognized, running shadow"
        )
    return "shadow"


def _repair_harvest_label_suffix() -> str:
    raw = str(os.environ.get("DEXTER3_REPAIR_HARVEST_LABEL_SUFFIX", "rsh") or "").strip()
    return raw or "rsh"


def _repair_harvest_config_from_env() -> RepairHarvestConfig:
    """Same "ignored invalid falls back to default" posture as every other
    ``_*_config_from_env`` builder in this file — a typo'd env var must
    never crash the live loop, only leave that one knob at its default."""
    kw: dict[str, Any] = {}
    for env, field_name in (
        ("DEXTER3_REPAIR_HARVEST_TRIGGER_R", "trigger_r"),
        ("DEXTER3_REPAIR_HARVEST_BANK_TARGET_R", "bank_target_r"),
        ("DEXTER3_REPAIR_HARVEST_SCALP_SL_FRAC", "scalp_sl_frac"),
        ("DEXTER3_REPAIR_HARVEST_EPISODE_LOSS_STOP_R", "episode_loss_stop_r"),
    ):
        raw = os.environ.get(env)
        if raw:
            try:
                kw[field_name] = float(raw)
            except ValueError:
                log_line(f"{utc_now_iso()} ignored invalid {env}={raw!r}")
    for env, field_name in (
        ("DEXTER3_REPAIR_HARVEST_SCALP_MAX_HOLD_BARS", "scalp_max_hold_bars"),
        ("DEXTER3_REPAIR_HARVEST_MAX_SCALPS_PER_EPISODE", "max_scalps_per_episode"),
    ):
        raw = os.environ.get(env)
        if raw:
            try:
                kw[field_name] = int(raw)
            except ValueError:
                log_line(f"{utc_now_iso()} ignored invalid {env}={raw!r}")
    return RepairHarvestConfig(**kw)


# -- small local position-field helpers (duplicated small pure parsers — same
# "no cross-import of a peer module's private helpers" convention
# basket_live.py/executor.py each document for themselves) -------------------


def _rsh_position_id(p: dict[str, Any]) -> int:
    try:
        return int(p.get("positionId") or p.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _rsh_position_side(p: dict[str, Any]) -> str:
    raw = str(p.get("tradeSide") or p.get("side") or "").strip().lower()
    if raw.startswith("buy"):
        return "buy"
    if raw.startswith("sell"):
        return "sell"
    return raw


def _rsh_position_entry(p: dict[str, Any]) -> float:
    return _f(p.get("entryPrice", p.get("price", 0.0)))


def _rsh_position_sl(p: dict[str, Any]) -> float:
    return _f(p.get("stopLoss", p.get("stopLossPrice", 0.0)))


def _rsh_position_volume(p: dict[str, Any]) -> float:
    for key in ("volumeInUnits", "volume", "volumeUnits"):
        try:
            value = float(p.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def _rsh_opposite(side: str) -> str:
    return "sell" if side == "buy" else "buy"


# -- multi-leg shadow observation (DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE) --
# 2026-07-16: the harvester's single-leg-only episode-open requirement
# starves it of shadow data whenever the basket's own repair-leg engine has
# already added a second leg BEFORE aggregate_r reaches -trigger_r (the
# normal, expected case for a basket that eventually becomes multi-leg) --
# every one of those baskets was silently skipped (``rsh_skip_multileg``,
# never even observed). This flag lets a SHADOW-only episode open on a
# multi-leg basket using its NET direction instead of a single parent leg;
# see ``_maybe_open_multileg_shadow_episode``. Default 0 (off, byte-identical
# legacy skip behavior).


def _repair_harvest_multileg_observe_enabled() -> bool:
    raw = str(os.environ.get("DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE", "0") or "0").strip()
    return raw == "1"


def _rsh_basket_net_direction(lane: list[dict[str, Any]]) -> tuple[str | None, float]:
    """Net directional bias of a MULTI-leg basket: sum of signed leg volumes
    (buy legs positive, sell legs negative). A flat/tied net (exactly 0.0) is
    directionless -- returns (None, 0.0), never guessed as buy or sell."""
    net = 0.0
    for p in lane:
        side = _rsh_position_side(p)
        vol = _rsh_position_volume(p)
        if side == "buy":
            net += vol
        elif side == "sell":
            net -= vol
    if net > 0:
        return "buy", round(net, 8)
    if net < 0:
        return "sell", round(net, 8)
    return None, 0.0


def _shadow_scalp_bar_outcome(
    open_scalp: dict[str, Any], bar: dict[str, Any], bank_target_r: float
) -> tuple[str | None, float]:
    """Mirrors ``scripts/dexter3_geometry_optimizer.py::_simulate_bank``'s
    PER-BAR step exactly: conservative SL-first (a bar whose low/high
    breaches the scalp's own SL is a loss at -1.0R, ties go to the stop),
    else close-based bank check (floating R at THIS bar's close >=
    ``bank_target_r``). Returns ``(outcome, r)`` where outcome is
    "loss"/"bank"/None (still open — ``r`` is this bar's close-based R,
    kept for the caller's own max-hold timeout bookkeeping) or "skip" (a
    degenerate zero-risk scalp — mirrors ``_simulate_bank``'s own "skip"
    return for ``risk <= 0``)."""
    side = str(open_scalp.get("side"))
    entry = _f(open_scalp.get("entry"))
    sl = _f(open_scalp.get("sl"))
    risk = abs(entry - sl)
    if risk <= 0:
        return "skip", 0.0
    hi = _f(bar.get("high"))
    lo = _f(bar.get("low"))
    cl = _f(bar.get("close"))
    if side == "buy":
        if lo <= sl:
            return "loss", -1.0
        r = (cl - entry) / risk
    else:
        if hi >= sl:
            return "loss", -1.0
        r = (entry - cl) / risk
    if r >= bank_target_r:
        return "bank", r
    return None, r


def _journal_repair_harvest_scalp_cap_once(
    journal: DecisionJournal, state: dict[str, Any], symbol: str, episode: dict[str, Any]
) -> None:
    """Journal ``rsh_scalp_cap_reached`` at most once per distinct scalp
    count (the cap check runs at most once per M5 close, so this cannot
    spam even without the guard — the guard just keeps a re-read of an
    unchanged episode from re-journaling identical rows)."""
    flagged = state.setdefault("repair_harvest_cap_warned", {})
    n = len(episode.get("scalps", []))
    if flagged.get(symbol) == n:
        return
    flagged[symbol] = n
    journal.insert_basket_event(
        0,
        "rsh_scalp_cap_reached",
        {"symbol": symbol, "n_scalps": n, "parent_position_id": episode.get("parent_position_id")},
        label=_active_order_label(),
    )


def _maybe_open_repair_harvest_episode(
    journal: DecisionJournal,
    state: dict[str, Any],
    symbol: str,
    lane: list[dict[str, Any]],
    cfg: RepairHarvestConfig,
    base_risk_usd: float,
    governor_locked: bool,
) -> None:
    """Open a new episode iff: mode != off (checked by the caller), the lane
    basket has EXACTLY ONE leg (multi-leg -> log ``rsh_skip_multileg``, no
    episode — matches the replay's single-parent model), its aggregate_r <=
    ``-trigger_r``, AND the aggregate pnl is RELIABLE (fail-closed — a blind
    OM must never harvest). Never opens new exposure once the governor has
    locked/stopped the day (mirrors the existing add_repair_leg suppression
    in ``run_om_tick``/``_manage_lane_basket``)."""
    if governor_locked:
        return
    n = len(lane)
    if n == 0:
        return
    if n > 1:
        ids = sorted(_rsh_position_id(p) for p in lane)
        # DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE=1 (default 0, off):
        # unblock SHADOW-only data collection on a multi-leg basket instead
        # of starving forever on the single-leg requirement below. Mode is
        # re-checked here (never trusted from the caller) so this can NEVER
        # take effect in "live" mode -- see _maybe_open_multileg_shadow_episode's
        # own belt-and-suspenders guard for the second, independent check.
        if _repair_harvest_multileg_observe_enabled():
            mode = _repair_harvest_mode_from_env()
            if mode == "shadow":
                _maybe_open_multileg_shadow_episode(journal, state, symbol, lane, ids, cfg, base_risk_usd, mode)
                return
        warned = state.setdefault("repair_harvest_multileg_warned", {})
        if warned.get(symbol) != ids:
            warned[symbol] = ids
            journal.insert_basket_event(
                0,
                "rsh_skip_multileg",
                {"symbol": symbol, "legs": n, "position_ids": ids},
                label=_active_order_label(),
            )
        return

    parent = lane[0]
    parent_pid = _rsh_position_id(parent)
    parent_side = _rsh_position_side(parent)
    if parent_pid <= 0 or parent_side not in ("buy", "sell"):
        return

    agg = basket_live.aggregate_lane(lane, base_risk_usd=base_risk_usd)
    if bool(agg.get("unreliable")):
        return  # fail-closed -- never harvest on an unreliable pnl snapshot
    aggregate_r = _f(agg.get("aggregate_r"), 0.0)
    if aggregate_r > -abs(cfg.trigger_r):
        return  # not trapped yet

    mode = _repair_harvest_mode_from_env()
    episode = {
        "parent_position_id": parent_pid,
        "parent_side": parent_side,
        "parent_entry": _rsh_position_entry(parent),
        "parent_sl": _rsh_position_sl(parent),
        "started_ts": utc_now_iso(),
        "trigger_r_at_open": round(aggregate_r, 4),
        "mode": mode,
        "cum_scalp_r": 0.0,
        "scalps": [],
        "open_scalp": None,
        "last_m5_close_ts": None,
        "loss_stopped": False,
    }
    state.setdefault("repair_harvest", {})[symbol] = episode
    journal.insert_basket_event(
        0,
        "rsh_episode_opened",
        {
            "symbol": symbol,
            "parent_position_id": parent_pid,
            "parent_side": parent_side,
            "aggregate_r": round(aggregate_r, 4),
            "trigger_r": cfg.trigger_r,
            "mode": mode,
        },
        label=_active_order_label(),
    )
    log_line(
        f"{utc_now_iso()} {symbol} rsh_episode_opened parent={parent_pid} side={parent_side} "
        f"aggregate_r={aggregate_r:.4f} mode={mode}"
    )


def _maybe_open_multileg_shadow_episode(
    journal: DecisionJournal,
    state: dict[str, Any],
    symbol: str,
    lane: list[dict[str, Any]],
    ids: list[int],
    cfg: RepairHarvestConfig,
    base_risk_usd: float,
    mode: str,
) -> None:
    """MULTILEG SHADOW OBSERVATION (``DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE=1``,
    shadow mode ONLY): a trapped multi-leg basket may still open a SHADOW
    episode using the basket's NET direction (sum of signed leg volumes)
    instead of requiring a single parent leg. Purpose: unblock shadow data
    collection for baskets the harvester currently starves on forever
    (2026-07-16 observation: the basket's own repair-leg engine routinely
    adds a second leg on structural evidence BEFORE aggregate_r reaches
    -trigger_r, so the single-leg episode-open requirement never fires).

    Second, independent guard (belt-and-suspenders on top of the caller's
    own ``mode == "shadow"`` check): this function refuses to open anything
    unless ``mode == "shadow"`` -- a future refactor of the caller cannot
    silently start live multi-leg episodes by accident.
    """
    if mode != "shadow":
        journal.insert_basket_event(
            0,
            "rsh_multileg_observe_blocked_non_shadow",
            {"symbol": symbol, "mode": mode, "legs": len(lane), "position_ids": ids},
            label=_active_order_label(),
        )
        return

    net_side, net_volume = _rsh_basket_net_direction(lane)
    if net_side is None:
        warned = state.setdefault("repair_harvest_flat_net_warned", {})
        if warned.get(symbol) != ids:
            warned[symbol] = ids
            journal.insert_basket_event(
                0,
                "rsh_skip_flat_net",
                {"symbol": symbol, "legs": len(lane), "position_ids": ids},
                label=_active_order_label(),
            )
        return

    agg = basket_live.aggregate_lane(lane, base_risk_usd=base_risk_usd)
    if bool(agg.get("unreliable")):
        return  # fail-closed -- never harvest on an unreliable pnl snapshot
    aggregate_r = _f(agg.get("aggregate_r"), 0.0)
    if aggregate_r > -abs(cfg.trigger_r):
        return  # not trapped yet

    # Representative "parent" entry/sl for scalp sizing: the oldest leg on
    # the net side (closest analog to the single-leg "parent" -- never a
    # synthetic/invented position). Falls back to the basket's first leg
    # only if, degenerately, no leg actually matches the net side (shouldn't
    # happen given net_side is derived FROM these same legs' signed volumes).
    same_side_legs = [p for p in lane if _rsh_position_side(p) == net_side]
    reference_leg = same_side_legs[0] if same_side_legs else lane[0]

    episode = {
        "parent_position_id": _rsh_position_id(reference_leg),
        "parent_position_ids": list(ids),
        "is_multileg_shadow": True,
        "parent_side": net_side,
        "parent_entry": _rsh_position_entry(reference_leg),
        "parent_sl": _rsh_position_sl(reference_leg),
        "started_ts": utc_now_iso(),
        "trigger_r_at_open": round(aggregate_r, 4),
        "mode": "shadow",
        "cum_scalp_r": 0.0,
        "scalps": [],
        "open_scalp": None,
        "last_m5_close_ts": None,
        "loss_stopped": False,
        "net_volume_at_open": net_volume,
    }
    state.setdefault("repair_harvest", {})[symbol] = episode
    journal.insert_basket_event(
        0,
        "rsh_episode_opened",
        {
            "symbol": symbol,
            "parent_position_id": episode["parent_position_id"],
            "parent_position_ids": list(ids),
            "parent_side": net_side,
            "aggregate_r": round(aggregate_r, 4),
            "trigger_r": cfg.trigger_r,
            "mode": "shadow",
            "multileg_shadow": True,
            "net_volume": net_volume,
        },
        label=_active_order_label(),
    )
    log_line(
        f"{utc_now_iso()} {symbol} rsh_episode_opened(multileg_shadow) parent_ids={ids} side={net_side} "
        f"aggregate_r={aggregate_r:.4f} net_volume={net_volume:.4f}"
    )


def _finalize_repair_harvest_scalp(
    journal: DecisionJournal,
    symbol: str,
    episode: dict[str, Any],
    open_scalp: dict[str, Any],
    *,
    outcome: str,
    r: float,
    mode: str,
    cfg: RepairHarvestConfig,
) -> None:
    record = {
        "position_id": open_scalp.get("position_id"),
        "side": open_scalp.get("side"),
        "entry": open_scalp.get("entry"),
        "entry_ts": open_scalp.get("opened_bar_ts"),
        "outcome": outcome,
        "r": round(float(r), 6),
    }
    episode.setdefault("scalps", []).append(record)
    episode["cum_scalp_r"] = round(_f(episode.get("cum_scalp_r"), 0.0) + float(r), 6)
    episode["open_scalp"] = None
    event = "rsh_bank" if outcome == "bank" else "rsh_timeout" if outcome == "timeout" else "rsh_scalp_resolved"
    journal.insert_basket_event(
        0,
        event,
        {
            "symbol": symbol,
            "mode": mode,
            "parent_position_id": episode.get("parent_position_id"),
            "cum_scalp_r": episode["cum_scalp_r"],
            **record,
        },
        label=_active_order_label(),
    )
    if not episode.get("loss_stopped") and episode["cum_scalp_r"] <= -abs(cfg.episode_loss_stop_r):
        episode["loss_stopped"] = True
        journal.insert_basket_event(
            0,
            "rsh_loss_stopped",
            {
                "symbol": symbol,
                "parent_position_id": episode.get("parent_position_id"),
                "cum_scalp_r": episode["cum_scalp_r"],
                "threshold_r": -abs(cfg.episode_loss_stop_r),
            },
            label=_active_order_label(),
        )
        log_line(
            f"{utc_now_iso()} {symbol} rsh_loss_stopped cum_scalp_r={episode['cum_scalp_r']:.4f} "
            f"threshold={-abs(cfg.episode_loss_stop_r):.4f}"
        )


def _resolve_open_scalp_at_close(
    journal: DecisionJournal,
    symbol: str,
    executor: Dexter3Executor | None,
    episode: dict[str, Any],
    open_scalp: dict[str, Any],
    scalp_lane: list[dict[str, Any]],
    bar: dict[str, Any],
    cfg: RepairHarvestConfig,
    mode: str,
) -> bool:
    """Evaluate the currently-open scalp at THIS M5 close. Returns True iff
    it resolved (bank/timeout/loss/vanished) this tick — the caller must
    never open a fresh scalp on the SAME tick a resolution happened (the
    replay's own index math always opens the NEXT scalp one bar AFTER the
    resolution bar, never on it)."""
    if mode == "live":
        pid = int(open_scalp.get("position_id") or 0)
        pos = next((p for p in scalp_lane if _rsh_position_id(p) == pid), None) if pid > 0 else None
        if pos is None:
            # Broker already closed it (SL fill, external close, weekly
            # flatten, ...) -- NEVER guess its R; vanish reconcile
            # (Dexter3Executor.reconcile_vanished_lane_positions, called from
            # run_symbol_cycle) owns journaling the REAL realized pnl for
            # this position_id since it is still family-owned.
            episode["open_scalp"] = None
            episode.setdefault("scalps", []).append(
                {
                    "position_id": pid,
                    "side": open_scalp.get("side"),
                    "entry": open_scalp.get("entry"),
                    "entry_ts": open_scalp.get("opened_bar_ts"),
                    "outcome": "vanished_unresolved",
                    "r": None,
                }
            )
            journal.insert_basket_event(
                0,
                "rsh_scalp_vanished",
                {"symbol": symbol, "position_id": pid, "parent_position_id": episode.get("parent_position_id")},
                label=_active_order_label(),
            )
            return True
        scalp_risk_usd = _f(open_scalp.get("scalp_risk_usd"), 0.0) or 1.0
        agg = basket_live.aggregate_lane([pos], base_risk_usd=scalp_risk_usd)
        if bool(agg.get("unreliable")):
            return False  # stale/missing pnl this tick -- never guess, retry at the next close
        live_r = _f(agg.get("aggregate_r"), 0.0)
        bars_held = int(_f(open_scalp.get("bars_evaluated"), 0)) + 1
        if live_r >= cfg.bank_target_r:
            if executor is not None:
                executor.close_lane_position(pid, reason="rsh_bank")
            _finalize_repair_harvest_scalp(journal, symbol, episode, open_scalp, outcome="bank", r=live_r, mode=mode, cfg=cfg)
            return True
        if bars_held >= cfg.scalp_max_hold_bars:
            if executor is not None:
                executor.close_lane_position(pid, reason="rsh_timeout")
            _finalize_repair_harvest_scalp(journal, symbol, episode, open_scalp, outcome="timeout", r=live_r, mode=mode, cfg=cfg)
            return True
        open_scalp["bars_evaluated"] = bars_held
        return False

    # -- shadow: pure counterfactual against this REAL completed M5 bar -----
    outcome, r = _shadow_scalp_bar_outcome(open_scalp, bar, cfg.bank_target_r)
    if outcome == "skip":
        _finalize_repair_harvest_scalp(journal, symbol, episode, open_scalp, outcome="skip", r=0.0, mode=mode, cfg=cfg)
        return True
    bars_held = int(_f(open_scalp.get("bars_evaluated"), 0)) + 1
    if outcome is None:
        if bars_held >= cfg.scalp_max_hold_bars:
            _finalize_repair_harvest_scalp(journal, symbol, episode, open_scalp, outcome="timeout", r=r, mode=mode, cfg=cfg)
            return True
        open_scalp["bars_evaluated"] = bars_held
        return False
    _finalize_repair_harvest_scalp(journal, symbol, episode, open_scalp, outcome=outcome, r=r, mode=mode, cfg=cfg)
    return True


def _open_new_repair_harvest_scalp(
    journal: DecisionJournal,
    symbol: str,
    executor: Dexter3Executor | None,
    episode: dict[str, Any],
    m5_bars: list[dict[str, Any]],
    cfg: RepairHarvestConfig,
    suffix: str,
    daily: dict[str, Any],
    mode: str,
) -> None:
    parent_side = str(episode.get("parent_side") or "")
    if parent_side not in ("buy", "sell"):
        return
    mirror_side = _rsh_opposite(parent_side)
    entry_price = _f(m5_bars[-1].get("close"), 0.0)
    parent_risk = abs(_f(episode.get("parent_entry")) - _f(episode.get("parent_sl")))
    scalp_risk_price = max(0.0, float(cfg.scalp_sl_frac)) * parent_risk
    if entry_price <= 0 or scalp_risk_price <= 0:
        return  # degenerate geometry -- retry at the next M5 close rather than fabricate a trade
    sl_price = entry_price - scalp_risk_price if mirror_side == "buy" else entry_price + scalp_risk_price
    # No real TP -- banking (rsh_bank) / timeout (rsh_timeout) is the exit.
    # execute_entry's own pre-flight hard-requires a non-None, correctly-
    # sided tp, so a far ceiling (never expected to bind) satisfies that
    # contract without becoming a real profit-taking mechanism.
    tp_far_mult = 50.0
    tp_price = (
        entry_price + tp_far_mult * scalp_risk_price
        if mirror_side == "buy"
        else entry_price - tp_far_mult * scalp_risk_price
    )
    bar_ts = str(m5_bars[-1].get("ts") or "")
    ts_close = hunter_brain._bar_close_ts(bar_ts)
    session_label = str(market_lens.session_context(ts_close).get("value") or "unknown")
    scalp_index = len(episode.get("scalps", [])) + 1
    decision = hunter_brain.Decision(
        ts_close=ts_close,
        symbol=symbol,
        action="enter",
        side=mirror_side,
        entry_type="market",
        entry=entry_price,
        sl=sl_price,
        tp=tp_price,
        size_class="scout",
        leader_score=0.0,
        p_win_est=0.5,
        setup="repair_harvest",
        reasons=[
            f"Repair-Scalp Harvester: bank-green scalp #{scalp_index} mirror={mirror_side} "
            f"while parent {episode.get('parent_position_id')} trapped",
            f"cum_scalp_r_before={episode.get('cum_scalp_r')}",
        ],
        features={
            "repair_harvest": {
                "parent_position_id": episode.get("parent_position_id"),
                "episode_started_ts": episode.get("started_ts"),
                "scalp_index": scalp_index,
            }
        },
        session=session_label,
    )

    if mode != "live" or executor is None:
        episode["open_scalp"] = {
            "position_id": 0,
            "side": mirror_side,
            "entry": entry_price,
            "sl": sl_price,
            "opened_bar_ts": bar_ts,
            "bars_evaluated": 0,
            "scalp_risk_usd": 1.0,
        }
        journal.insert_basket_event(
            0,
            "rsh_scalp_opened",
            {
                "symbol": symbol,
                "mode": "shadow",
                "side": mirror_side,
                "entry": entry_price,
                "sl": sl_price,
                "scalp_index": scalp_index,
                "parent_position_id": episode.get("parent_position_id"),
            },
            label=_active_order_label(),
        )
        return

    scalp_risk_usd = _om_base_risk_usd(executor)
    lineage = {
        "parent_position_id": episode.get("parent_position_id"),
        "episode_started_ts": episode.get("started_ts"),
        "scalp_index": scalp_index,
        "cum_scalp_r_before": episode.get("cum_scalp_r"),
        "engine": "repair_scalp_harvest",
    }
    label_override = f"{_active_order_label()}:{suffix}"
    result = _execute_live_entry(
        executor,
        decision,
        today_entry_count=int(daily.get("entries", 0)),
        today_losing_count=int(daily.get("loss_baskets", 0)),
        risk_usd_override=scalp_risk_usd,
        repair_context=lineage,
        basket_authorized=True,
        label_override=label_override,
    )
    journal.insert_basket_event(
        0,
        "rsh_scalp_open_attempt",
        {
            "symbol": symbol,
            "mode": "live",
            "side": mirror_side,
            "entry": entry_price,
            "sl": sl_price,
            "scalp_index": scalp_index,
            "parent_position_id": episode.get("parent_position_id"),
            "result_action": result.get("action"),
        },
        label=_active_order_label(),
    )
    if result.get("action") == "entered":
        daily["entries"] = int(daily.get("entries", 0)) + 1
        episode["open_scalp"] = {
            "position_id": int(result.get("position_id") or 0),
            "side": mirror_side,
            "entry": _f(result.get("entry"), entry_price),
            "sl": sl_price,
            "opened_bar_ts": bar_ts,
            "bars_evaluated": 0,
            "scalp_risk_usd": scalp_risk_usd,
        }
        journal.insert_basket_event(
            0,
            "rsh_scalp_opened",
            {
                "symbol": symbol,
                "mode": "live",
                "position_id": result.get("position_id"),
                "side": mirror_side,
                "entry": entry_price,
                "sl": sl_price,
                "scalp_index": scalp_index,
                "parent_position_id": episode.get("parent_position_id"),
            },
            label=_active_order_label(),
        )


def _manage_repair_harvest_episode(
    journal: DecisionJournal,
    state: dict[str, Any],
    symbol: str,
    executor: Dexter3Executor | None,
    episode: dict[str, Any],
    scalp_lane: list[dict[str, Any]],
    m5_bars: list[dict[str, Any]],
    cfg: RepairHarvestConfig,
    suffix: str,
    daily: dict[str, Any],
    governor_locked: bool,
) -> None:
    """Per-M5-close scalp management: resolve any open scalp (bank/timeout),
    then open a fresh one iff none is open, the episode is not loss-stopped,
    caps allow it, and the day is not governor-locked. Runs every fast tick
    but only ACTS on a genuinely new M5 close (``last_m5_close_ts`` dedup —
    same one-decision-per-close discipline as ``is_new_m5_close`` elsewhere
    in this file), tracked independently of the main decision path's own
    dedup key so the two can never interfere with each other."""
    episode_mode = str(episode.get("mode") or "shadow")
    if not m5_bars:
        return
    last_bar = m5_bars[-1]
    last_ts = str(last_bar.get("ts") or "")
    if not last_ts:
        return
    if last_ts == str(episode.get("last_m5_close_ts") or ""):
        return  # no new M5 close since we last acted -- nothing to do

    just_resolved = False
    open_scalp = episode.get("open_scalp")
    if open_scalp is not None:
        just_resolved = _resolve_open_scalp_at_close(
            journal, symbol, executor, episode, open_scalp, scalp_lane, last_bar, cfg, episode_mode
        )

    if (
        not just_resolved
        and episode.get("open_scalp") is None
        and not episode.get("loss_stopped")
        and not governor_locked
    ):
        if len(episode.get("scalps", [])) >= cfg.max_scalps_per_episode:
            _journal_repair_harvest_scalp_cap_once(journal, state, symbol, episode)
        else:
            _open_new_repair_harvest_scalp(journal, symbol, executor, episode, m5_bars, cfg, suffix, daily, episode_mode)

    episode["last_m5_close_ts"] = last_ts


def _close_repair_harvest_episode(
    journal: DecisionJournal,
    symbol: str,
    executor: Dexter3Executor | None,
    episode: dict[str, Any],
    m5_bars: list[dict[str, Any]],
    reason: str,
) -> None:
    """Episode ENDS when the parent position leaves the lane
    (closed/vanished): close any open LIVE scalp at market immediately;
    journal a final episode summary (parent id, parent outcome if knowable,
    n_scalps, cum_scalp_r, mode)."""
    mode = str(episode.get("mode") or "shadow")
    open_scalp = episode.get("open_scalp")
    if open_scalp is not None:
        pid = int(open_scalp.get("position_id") or 0)
        if mode == "live" and executor is not None and pid > 0:
            executor.close_lane_position(pid, reason="rsh_episode_close")
            episode.setdefault("scalps", []).append(
                {
                    "position_id": pid,
                    "side": open_scalp.get("side"),
                    "entry": open_scalp.get("entry"),
                    "entry_ts": open_scalp.get("opened_bar_ts"),
                    "outcome": "episode_end_closed",
                    "r": None,
                }
            )
        else:
            # Shadow (or no executor bound): nothing real to close. Mark the
            # virtual scalp to the LAST known bar's close (best-effort,
            # honest — never fabricate a resolved outcome for a still-open
            # counterfactual position).
            r = None
            if m5_bars:
                side = str(open_scalp.get("side"))
                entry = _f(open_scalp.get("entry"))
                sl = _f(open_scalp.get("sl"))
                risk = abs(entry - sl)
                if risk > 0:
                    cl = _f(m5_bars[-1].get("close"))
                    r = round(((cl - entry) / risk if side == "buy" else (entry - cl) / risk), 6)
            episode.setdefault("scalps", []).append(
                {
                    "position_id": 0,
                    "side": open_scalp.get("side"),
                    "entry": open_scalp.get("entry"),
                    "entry_ts": open_scalp.get("opened_bar_ts"),
                    "outcome": "episode_end_mark",
                    "r": r,
                }
            )
            if r is not None:
                episode["cum_scalp_r"] = round(_f(episode.get("cum_scalp_r"), 0.0) + r, 6)
        episode["open_scalp"] = None
    journal.insert_basket_event(
        0,
        "rsh_episode_closed",
        {
            "symbol": symbol,
            "parent_position_id": episode.get("parent_position_id"),
            "parent_last_known_agg_r": episode.get("parent_last_known_agg_r"),
            "parent_last_known_pnl_usd": episode.get("parent_last_known_pnl_usd"),
            "n_scalps": len(episode.get("scalps", [])),
            "cum_scalp_r": episode.get("cum_scalp_r"),
            "mode": mode,
            "reason": reason,
        },
        label=_active_order_label(),
    )
    log_line(
        f"{utc_now_iso()} {symbol} rsh_episode_closed parent={episode.get('parent_position_id')} "
        f"n_scalps={len(episode.get('scalps', []))} cum_scalp_r={episode.get('cum_scalp_r')} mode={mode}"
    )
    # State cleanup (popping this symbol's episode out of state["repair_harvest"])
    # is the caller's responsibility (_run_repair_harvest_tick) — this function
    # only journals the summary and closes/marks the open scalp.


def _run_repair_harvest_tick(
    mcp: Dexter3McpClient,
    journal: DecisionJournal,
    state: dict[str, Any],
    symbol: str,
    executor: Dexter3Executor | None,
    lane: list[dict[str, Any]],
    all_positions: list[dict[str, Any]],
) -> None:
    """Entry point called once per fast tick from ``run_om_tick`` (BEFORE its
    own ``if not lane:`` early return — the harvester must still detect a
    parent-left-the-lane episode-end even when the OM's own lane just went
    empty). ``off`` mode is a pure no-op: no MCP reads beyond what the caller
    already fetched, no journal writes, no state mutation."""
    mode = _repair_harvest_mode_from_env()
    if mode == "off":
        return

    cfg = _repair_harvest_config_from_env()
    suffix = _repair_harvest_label_suffix()
    family = _active_label_family()
    scalp_lane = basket_live.repair_harvest_legs(all_positions, family, suffix)
    scalp_lane = [p for p in scalp_lane if str(p.get("symbolName") or p.get("symbol") or "") in ("", symbol)]

    store = state.setdefault("repair_harvest", {})
    episode = store.get(symbol)
    governor_locked = (state.get("governor") or {}).get("state") in ("TARGET_LOCKED", "LOSS_STOPPED")
    m5_bars, _m15, _h1 = _om_bars_for(mcp, symbol)

    if episode is None:
        base_risk_usd = _lane_actual_risk_usd(lane, _om_base_risk_usd(executor), _soft_stop_pts(state))
        _maybe_open_repair_harvest_episode(journal, state, symbol, lane, cfg, base_risk_usd, governor_locked)
        episode = store.get(symbol)
        if episode is None:
            return
    else:
        if episode.get("is_multileg_shadow"):
            # Multi-leg shadow episode (DEXTER3_REPAIR_HARVEST_MULTILEG_OBSERVE):
            # parent tracking is a SET of leg ids from episode-open time --
            # the episode ends when the basket EMPTIES of all of them (any
            # single leg's own close/vanish does not end it, unlike the
            # legacy single-parent path below).
            tracked_ids = {int(x) for x in (episode.get("parent_position_ids") or [])}
            parent_lane = [p for p in lane if _rsh_position_id(p) in tracked_ids]
            if not parent_lane:
                _close_repair_harvest_episode(journal, symbol, executor, episode, m5_bars, reason="parent_resolved")
                store.pop(symbol, None)
                return
        else:
            parent_pid = int(episode.get("parent_position_id") or 0)
            parent_lane = [p for p in lane if _rsh_position_id(p) == parent_pid]
            if not parent_lane:
                _close_repair_harvest_episode(journal, symbol, executor, episode, m5_bars, reason="parent_resolved")
                store.pop(symbol, None)
                return
        # Best-effort proxy for "parent outcome if knowable" at episode-close
        # time — the last RELIABLE aggregate observed for the parent leg
        # while it was still open (never a broker-verified realized pnl,
        # which would need an extra get_deals() call this fast-tick path
        # deliberately avoids; documented as a best-effort field).
        parent_agg = basket_live.aggregate_lane(
            parent_lane,
            base_risk_usd=_lane_actual_risk_usd(parent_lane, _om_base_risk_usd(executor), _soft_stop_pts(state)),
        )
        if not bool(parent_agg.get("unreliable")):
            episode["parent_last_known_agg_r"] = parent_agg.get("aggregate_r")
            episode["parent_last_known_pnl_usd"] = parent_agg.get("aggregate_pnl_usd")

    daily = _daily_state(state)
    _manage_repair_harvest_episode(
        journal, state, symbol, executor, episode, scalp_lane, m5_bars, cfg, suffix, daily, governor_locked
    )


def _ensure_dpull_cs_backstop(
    executor: Dexter3Executor | None, state: dict[str, Any],
    lane: list[dict[str, Any]] | None, symbol: str,
) -> None:
    """dpull-cs (2026-07-23 fix): SELF-HEALING broker-SL backstop widen.

    The old entry-time amend (fired ~1s after the fill) was refused every
    time because get_positions had not yet propagated the new position
    (position_not_found race) — so the broker SL stayed at the SOFT -1R level,
    fired on wicks (broker_side_close), and the OM software close-stop never
    engaged (convex_close_stop=0). Result: dpull-cs was a byte-for-byte clone
    of base dpull and the whole forward A/B was measuring nothing.

    This runs on EVERY OM fast tick with a FRESH position read, so it retries
    until the broker sees the position and the amend sticks. Idempotent: once
    the broker SL is at/near the backstop, the distance check skips it — no
    repeated amends. Only touches close-stop lanes (env-gated); a failed amend
    just leaves the SL tighter (degrades to base-dpull, safe) and retries next
    tick."""
    if (executor is None or not vp_lane.convex_close_stop_enabled()
            or not lane or len(lane) != 1):
        # position closed / none open -> forget the per-pid retry counters so a
        # later reused pid starts fresh (the map only ever holds the live lane's).
        if state.get("dpull_cs_bs_tries"):
            state["dpull_cs_bs_tries"] = {}
        return
    cvx = state.get("vp_convex") or {}
    stop_pts = _f(cvx.get("stop_pts"), 0.0)
    if stop_pts <= 0:
        return
    pos = lane[0]
    entry = _f(pos.get("entryPrice") or pos.get("entry_price"), 0.0)
    cur_sl = _f(pos.get("stopLoss") or pos.get("stop_loss"), 0.0)
    side = str(pos.get("tradeSide") or pos.get("side") or "").lower()
    if entry <= 0 or cur_sl <= 0:
        return
    backstop_dist = stop_pts * (1.0 + vp_lane.convex_close_stop_backstop_mult())
    # already at/near the backstop (within 2%)? -> nothing to do (idempotent)
    if abs(entry - cur_sl) >= backstop_dist * 0.98:
        return
    pid = int(position_id_of(pos))
    # BOUNDED retry (2026-07-23): the daemon's position STREAM the loop reads
    # is stale after a mutation (a fresh get_positions confirms the amend DID
    # apply on the broker, but the loop still sees the old SL), and the amend
    # response is often classified McpClientError even when the broker
    # replaced the order (a false-negative "amend_failed"). Without a cap the
    # self-heal would re-amend every 8s forever. So: attempt at most
    # _CS_BACKSTOP_MAX_TRIES times per pid (the broker order IS replaced by
    # then), then stop; the per-pid counter is cleared when the position
    # closes (lane empties) below.
    tries = state.setdefault("dpull_cs_bs_tries", {})
    n = int(tries.get(str(pid), 0))
    if n >= _CS_BACKSTOP_MAX_TRIES:
        return
    backstop = entry - backstop_dist if side.startswith("buy") else entry + backstop_dist
    tp = _f(pos.get("takeProfit") or pos.get("take_profit"), 0.0) or None
    res = executor.amend_lane_sl_tp(pid, sl=round(backstop, 5), tp=tp)
    tries[str(pid)] = n + 1
    log_line(
        f"{utc_now_iso()} {symbol} dpull_cs_backstop_ensure pid={pid} try={n + 1} "
        f"cur_sl={cur_sl:.5f} -> backstop={backstop:.5f} "
        f"result={res.get('action') or res.get('status')} reason={res.get('reason', '-')} "
        f"err={str(res.get('error', '-'))[:120]}"
    )


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
        # 2026-07-15 repair-scalp-harvest design: exclude the harvester's own
        # ":rsh" scalp legs from the OM/basket lane — a harvester scalp's
        # floating PnL must never distort the PARENT basket's aggregate_r
        # (else the OM would "repair" a basket that is actually fine, or the
        # vanish/duplicate gates would fight the harvester). The excluded
        # legs remain family-owned everywhere else (vanish reconcile, H4 cap,
        # governor realized) via plain prefix matching — see
        # basket_live.lane_positions' own docstring.
        lane = basket_live.lane_positions(
            positions, active_label_family, exclude_label_suffix=_repair_harvest_label_suffix()
        )
        lane = [p for p in lane if str(p.get("symbolName") or p.get("symbol") or "") in ("", symbol)]
    except (McpClientError, McpZombieError) as exc:
        _note_mcp_error(state, ok=False)
        log_line(f"{utc_now_iso()} {symbol} om_lane_read_failed (no OM action this tick): {exc}")
        return "om_lane_read_failed"

    _note_mcp_error(state, ok=True)

    try:
        _ensure_dpull_cs_backstop(executor, state, lane, symbol)
    except Exception as exc:  # noqa: BLE001 - never crash the OM tick over a safety-net amend
        log_line(f"{utc_now_iso()} {symbol} dpull_cs_backstop_ensure_failed: {exc}")

    try:
        _run_repair_harvest_tick(mcp, journal, state, symbol, executor, lane, positions)
    except Exception as exc:  # noqa: BLE001 - the harvester must never crash the OM fast tick
        log_line(f"{utc_now_iso()} {symbol} repair_harvest_tick_failed (fail-open, no harvest action): {exc}")

    if not lane:
        is_grok = bool((state.get("grok_v10_flags") or {}).get("is_grok_scalp"))
        _clear_basket_runtime(state, symbol, grok=is_grok)
        # 2026-07-15 OM-blindspot fix #2: this IS the "position no longer
        # open" evidence for the durability ledger too — purge every
        # remaining entry for this symbol using the lane read already in
        # hand (empty), no extra broker call.
        _update_position_peak_ledger(state, symbol, [], None, None)
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

    agg_probe = basket_live.aggregate_lane(lane, base_risk_usd=_lane_actual_risk_usd(lane, _om_base_risk_usd(executor), _soft_stop_pts(state)))
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

    # 2026-07-15 OM-blindspot fix #2: basket_runtime is ABOUT to reset (a
    # brand-new basket, or amnesia from a transient empty-lane read / a
    # label-family boundary crossing — see _update_position_peak_ledger's
    # docstring) whenever its own oldest_open_ts disagrees with what THIS
    # tick's lane will produce. Re-seed the starting peak from the
    # position_id-keyed durability ledger (never below what a normal reset
    # would give) BEFORE opening_manager.evaluate() ever sees it, so a reset
    # never silently drops real history for a position that never actually
    # closed.
    existing_runtime = (state.get(runtime_key) or {}).get(symbol)
    if lane_oldest_ts and (
        not isinstance(existing_runtime, dict) or existing_runtime.get("oldest_open_ts") != lane_oldest_ts
    ):
        seeded_peak = _seed_peak_r_from_ledger(state, lane, _f(agg_probe.get("aggregate_r"), 0.0))
        existing_runtime = {
            "oldest_open_ts": lane_oldest_ts,
            "peak_r": seeded_peak,
            "ticks_since_peak": 0,
            "ticks_open": 0,
            "last_pyramid_peak": None,
        }

    om_state = {
        "basket_runtime": existing_runtime,
        "now_utc_iso": utc_now_iso(),
        "daily_state": {"daily_loss_baskets": int(_daily_state(state).get("loss_baskets", 0))},
        "basket_cfg": _basket_config_from_env(),
        "base_risk_usd": _lane_actual_risk_usd(lane, _om_base_risk_usd(executor), _soft_stop_pts(state)),
        "spread_abs": spread_abs,
        "smart_exit_regime": regime_map,
        # VP convex-exit inputs (stamped at intent fill — see
        # _service_vp_limit_intent; only read when DEXTER3_OM_TRAIL_MODE=convex)
        "vp_convex": state.get("vp_convex"),
        # Grok_v1.0 flags (populated at entry time)
        **(state.get("grok_v10_flags") or {}),
    }
    action = om.evaluate(symbol, lane, spot, m5_bars, m15_bars, h1_bars, om_state)

    new_runtime = action.get("basket_runtime")
    if new_runtime is not None:
        state.setdefault(runtime_key, {})[symbol] = new_runtime

    # Raise (never lower) the durability ledger from whatever peak_r/floor_r
    # this tick's OM decision computed, and purge any of THIS symbol's
    # entries no longer in ``lane`` (2026-07-15 OM-blindspot fix #2).
    _update_position_peak_ledger(state, symbol, lane, action.get("peak_r"), action.get("floor_r"))

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
            # Same source normal entries use (hunter_brain.decide()) —
            # 2026-07-15 repair-lineage-enrichment fix: previously omitted
            # entirely, which left every fast-tick repair entry_executed row
            # stamped session="".
            session_label = str(market_lens.session_context(ts_close).get("value") or "unknown")
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
                session=session_label,
            )
            repair_context = {
                "parent_position_ids": _parent_position_ids_oldest_first(lane),
                "basket_id": agg_probe.get("oldest_open_ts"),
                "basket_side": action.get("basket_side"),
                "basket_agg_r_at_repair": action.get("basket_agg_r_at_repair"),
                "basket_pnl_at_repair": action.get("basket_pnl_at_repair"),
                "level_lost": action.get("level_lost"),
                "close_beyond": action.get("close_beyond"),
            }
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
                    _lane_actual_risk_usd(lane, _om_base_risk_usd(executor), _soft_stop_pts(state)),
                ),
                repair_context=repair_context,
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
            # 2026-07-15 OM-blindspot fix #1 observability: per-position spot
            # age at enrichment time, journaled forever alongside pnl_sources.
            "pnl_spot_ages": agg_probe.get("pnl_spot_ages"),
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

        # 2026-07-25 audit fix — the close-all was FIRE-ONCE: the latch is set
        # before the close loop runs, so `newly_triggered` could never be true
        # again that UTC day, while inside the loop a single get_positions
        # failure only `continue`d and execute_close_all's
        # "close_all_partial" result was journaled but never checked. One
        # transient daemon hiccup at exactly the wrong moment therefore turned
        # the daily loss cap into an ADVISORY: entries stayed blocked (correct)
        # but the open basket kept running past the cap on broker SL alone.
        # The latch still fires once (the mission is decided once, not
        # re-litigated); only the CLOSE is retried, until it verifiably has
        # nothing left to close.
        retry_close_all = (
            not newly_triggered
            and effective_state in ("TARGET_LOCKED", "LOSS_STOPPED")
            and not bool(gov_state.get("close_all_done"))
        )
        if retry_close_all:
            log_line(
                f"{utc_now_iso()} governor close_all RETRY state={effective_state} "
                f"(previous attempt did not complete)"
            )

        if newly_triggered or retry_close_all:
            gov_state["state"] = effective_state
            if newly_triggered:
                gov_state["locked_pnl"] = status["effective_pnl"]
                gov_state["triggered_at"] = utc_now_iso()

            # Close every lane position across every symbol.
            # ``close_all_ok`` tracks whether this pass verifiably finished:
            # any read failure or non-clean close result leaves it False so the
            # retry above fires on the next tick (2026-07-25 audit fix).
            close_all_ok = True
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
                        close_all_ok = False  # unknown broker state -> must retry
                        continue
                    lane = basket_live.lane_positions(positions, active_label_family)
                    lane = [p for p in lane if str(p.get("symbolName") or p.get("symbol") or "") in ("", symbol)]
                    if not lane:
                        continue
                    ids = [int(p.get("positionId") or p.get("id") or 0) for p in lane]
                    ids = [x for x in ids if x > 0]
                    reason = "governor_target_lock" if effective_state == "TARGET_LOCKED" else "governor_loss_stop"
                    result = executor.execute_close_all(ids, reason=reason)
                    closed_summary[symbol] = result
                    # execute_close_all returns "close_all_partial" when ANY leg
                    # was refused. Previously that was journaled and ignored.
                    if str((result or {}).get("action") or "") != "closed_all":
                        close_all_ok = False
                        log_line(
                            f"{utc_now_iso()} governor close_all INCOMPLETE {symbol} "
                            f"action={(result or {}).get('action')} ids={ids} — will retry"
                        )
                    _clear_basket_runtime(state, symbol, grok=active_label == GROK_LABEL)

            # Only mark the capital-protection close as COMPLETE when this pass
            # verifiably closed everything (or found nothing to close). While
            # False, the retry branch above re-attempts on every governor tick
            # instead of the cap silently degrading to advisory.
            gov_state["close_all_done"] = bool(close_all_ok)
            if not close_all_ok:
                log_line(f"{utc_now_iso()} governor close_all NOT COMPLETE — retrying next tick")

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


def _soft_stop_pts(state: dict[str, Any] | None) -> float:
    """The SOFT (entry-time) stop distance for a close-stop lane, else 0.0.

    2026-07-25 audit fix, see ``_lane_actual_risk_usd``. Only meaningful while
    ``DEXTER3_OM_CONVEX_CLOSE_STOP=1`` (dpull-cs), where the broker SL is
    deliberately widened away from the risk the lane actually runs."""
    try:
        if not vp_lane.convex_close_stop_enabled():
            return 0.0
        return max(0.0, _f(((state or {}).get("vp_convex") or {}).get("stop_pts"), 0.0))
    except Exception:  # noqa: BLE001 - never let the R-base lookup kill a tick
        return 0.0


def _lane_actual_risk_usd(lane: list[dict[str, Any]] | None, fallback: float,
                          soft_stop_pts: float = 0.0) -> float:
    """ACTUAL dollar risk of the open lane legs: sum(|entry−SL| × volume).

    The R-base for aggregate_r/trail math. Using the static config risk was
    a live bug (2026-07-07): governor sized entries at ~$14.4 while the base
    stayed $0.50 → aggregate_r inflated ~29× → OM 'take' fired at +$0.60 and
    banked 11 straight winners at ~0.09R of their true risk.

    ``soft_stop_pts`` (2026-07-25 audit fix): on a CLOSE-STOP lane the broker
    SL is intentionally amended out to a far backstop (2.5× by default) while
    the software close-stop owns the real −1R. Deriving the R-base from that
    widened broker SL inflated the denominator ~3.5×, so ``peak_r`` read 0.57
    on a genuine +2.0R move and the convex trail could NEVER arm at
    ``DEXTER3_OM_CONVEX_ARM_R=2.0`` — it would have needed a 7.0 soft-R move
    inside the 120-minute cap. The deployed dpull-cs lane therefore had no
    profit trail at all and every winner rode to the time stop. When the caller
    supplies the soft distance, it is used as the per-unit risk instead.
    """
    total = 0.0
    soft = max(0.0, float(soft_stop_pts or 0.0))
    for p in lane or []:
        try:
            entry = float(p.get("entryPrice") or p.get("price") or 0.0)
            sl = float(p.get("stopLoss") or p.get("stopLossPrice") or 0.0)
            vol = float(p.get("volumeInUnits") or p.get("volume") or 0.0)
        except (TypeError, ValueError):
            continue
        if entry > 0 and sl > 0 and vol > 0:
            dist = soft if soft > 0 else abs(entry - sl)
            total += dist * vol
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
    is_daytrend = mode == "daytrend"
    is_scalp = mode == "scalp"
    is_dpull = mode == "dpull"
    is_dpull_cs = mode == "dpull-cs"
    is_channelfade = mode == "channelfade"

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

    if is_daytrend:
        from dexter3.daytrend import DAYTREND_LABEL as _DT_LABEL

        import dexter3.executor as _ex
        _ex.LABEL = _DT_LABEL
        print(f"[DAYTREND] Forced executor LABEL to {_DT_LABEL}", flush=True)

    if is_scalp:
        from dexter3.sd_zones import SCALP_LABEL as _SC_LABEL

        import dexter3.executor as _ex
        _ex.LABEL = _SC_LABEL
        print(f"[SCALP] Forced executor LABEL to {_SC_LABEL}", flush=True)

    if is_dpull:
        from dexter3.daytrend import DPULL_LABEL as _DP_LABEL

        import dexter3.executor as _ex
        _ex.LABEL = _DP_LABEL
        print(f"[DPULL] Forced executor LABEL to {_DP_LABEL}", flush=True)

    if is_dpull_cs:
        from dexter3.daytrend import DPULL_CS_LABEL as _DPC_LABEL

        import dexter3.executor as _ex
        _ex.LABEL = _DPC_LABEL
        print(f"[DPULL-CS] Forced executor LABEL to {_DPC_LABEL}", flush=True)

    # CHANNELFADE canary lane (Edge A, 2026-07-24): own broker label so no
    # other lane (esp. fable) ever touches its positions and vice versa.
    if is_channelfade:
        from dexter3.channelfade import CHF_LABEL as _CHF_LABEL

        import dexter3.executor as _ex
        _ex.LABEL = _CHF_LABEL
        print(f"[CHANNELFADE] Forced executor LABEL to {_CHF_LABEL}", flush=True)

    if is_grok and GROK_LABEL:
        active_label = GROK_LABEL
    elif is_vp:
        from dexter3.volume_profile import VP_LABEL as _VP_LABEL

        active_label = _VP_LABEL
    elif is_daytrend:
        from dexter3.daytrend import DAYTREND_LABEL as _DT_LABEL

        active_label = _DT_LABEL
    elif is_scalp:
        from dexter3.sd_zones import SCALP_LABEL as _SC_LABEL

        active_label = _SC_LABEL
    elif is_dpull:
        from dexter3.daytrend import DPULL_LABEL as _DP_LABEL

        active_label = _DP_LABEL
    elif is_dpull_cs:
        from dexter3.daytrend import DPULL_CS_LABEL as _DPC_LABEL

        active_label = _DPC_LABEL
    elif is_channelfade:
        from dexter3.channelfade import CHF_LABEL as _CHF_LABEL

        active_label = _CHF_LABEL
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
                        if executor is not None:
                            # no-ops without a pending intent; serves VP and
                            # hunt lanes alike (owner 2026-07-16 layer port)
                            _service_vp_limit_intent(mcp, state, symbol, executor, journal=journal)
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

    if os.environ.get("DEXTER3_MODE", "v16").lower().strip() == "daytrend":
        from dexter3.daytrend import DAYTREND_LABEL as _DT_LABEL

        import dexter3.executor as _ex
        _ex.LABEL = _DT_LABEL
        print(f"[DAYTREND] Forced executor LABEL to {_DT_LABEL}", flush=True)

    # Same H6 class of fix for the scalp (2026-07-17) and dpull (2026-07-22)
    # lanes — --once with the mode env set must place entries under the
    # lane's own label, mirroring run_loop's patches exactly.
    if os.environ.get("DEXTER3_MODE", "v16").lower().strip() == "scalp":
        from dexter3.sd_zones import SCALP_LABEL as _SC_LABEL

        import dexter3.executor as _ex
        _ex.LABEL = _SC_LABEL
        print(f"[SCALP] Forced executor LABEL to {_SC_LABEL}", flush=True)

    if os.environ.get("DEXTER3_MODE", "v16").lower().strip() == "dpull":
        from dexter3.daytrend import DPULL_LABEL as _DP_LABEL

        import dexter3.executor as _ex
        _ex.LABEL = _DP_LABEL
        print(f"[DPULL] Forced executor LABEL to {_DP_LABEL}", flush=True)

    if os.environ.get("DEXTER3_MODE", "v16").lower().strip() == "dpull-cs":
        from dexter3.daytrend import DPULL_CS_LABEL as _DPC_LABEL

        import dexter3.executor as _ex
        _ex.LABEL = _DPC_LABEL
        print(f"[DPULL-CS] Forced executor LABEL to {_DPC_LABEL}", flush=True)

    if os.environ.get("DEXTER3_MODE", "v16").lower().strip() == "channelfade":
        from dexter3.channelfade import CHF_LABEL as _CHF_LABEL

        import dexter3.executor as _ex
        _ex.LABEL = _CHF_LABEL
        print(f"[CHANNELFADE] Forced executor LABEL to {_CHF_LABEL}", flush=True)

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
