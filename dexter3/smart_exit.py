"""Dexter3 SMART EXIT — gated smart adaptive exit (owner directive 2026-07-08).

``scripts/dexter3_edge_discovery.py --smart-exit`` PROVED (938 decisions,
``_simulate_smart``): replacing the tight wick-triggered hard SL with (a) a
WIDE "disaster" hard-stop (``disaster_mult`` x the structural SL distance)
and (b) a soft cut that only fires on a bar CLOSE beyond the (tight)
structural invalidation — surviving noise wicks in between — AMPLIFIES edge
on the good "non-chase" buckets (aligned-ranging +0.254R -> +0.320R, WR
52->57%) but AMPLIFIES the loss on the no-edge "chase" bucket
(aligned-trending -0.285R -> -0.469R, see ``dexter3/edge_buckets.py``'s
``is_chase`` classification, same "aligned x trending" bucket the anti-chase
sizing gate already downsizes).

So this MUST be gated: smart exit applies ONLY to NON-chase entries. Chase
entries keep today's behavior exactly — tight hard SL, no wide disaster
stop, smart exit OFF (they are already downsized by ``edge_buckets``'s
anti-chase gate; stacking a wider stop on top of an already-proven-losing
bucket would compound the very effect the backtest measured).

This module is PURE classification + math — no I/O, no MCP, no broker
calls, no mutation. It reuses ``dexter3.edge_buckets.classify_bucket`` for
the chase gate (never reimplemented) and mirrors
``scripts/dexter3_edge_discovery.py::_simulate_smart``'s confirmed-break
definition, which itself is exactly what
``dexter3.basket_live.structure_evidence`` already computes for the M5 path
(``level_lost`` = a close crossed the defended structural level;
``m5_close_beyond`` = the most recent close is STILL beyond it, i.e. not a
wick that has since reclaimed) — this module does not reimplement that
either, it just interprets the evidence dict the OM already builds.

Wiring:
  - ``dexter3/shadow_runner.py::run_symbol_cycle`` classifies the entry's
    stop regime (tight vs disaster) via ``resolve_stop_regime`` right next
    to the existing anti-chase call (same ``h1_ctx`` in scope, no extra MCP
    read), journals it unconditionally (shadow mode), and — when the regime
    is 'disaster' — passes the widened stop distance + scaled sizing into
    ``dexter3.executor.Dexter3Executor.execute_entry`` via its new
    ``smart_exit`` kwarg. The regime is ALSO persisted into
    ``state['smart_exit_regime'][symbol]`` (same ownership pattern as
    ``basket_runtime``) so the OM fast-tick path
    (``shadow_runner.run_om_tick``) knows which open lanes are under the
    disaster-stop regime and may fire the soft cut.
  - ``dexter3/opening_manager.py::OpeningManager.evaluate`` gains a new
    branch (``_smart_loss_exit``, checked after cap-stop, before the DRAGON
    LADDER's profit path — the two are naturally disjoint since the ladder
    only ever engages a positive peak_r) that calls ``should_smart_exit``
    with the basket's own ``structure_evidence`` read and returns
    ``close_all``/``reason='smart_confirmed_break'`` when a bar has CLOSED
    beyond the tight invalidation. Otherwise it holds through the noise —
    the wide disaster stop already resting at the broker is the only hard
    floor, exactly mirroring the backtest's proven logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

Bar = dict[str, Any]

# Stop-regime tokens (persisted on the entry decision + in shadow state).
REGIME_TIGHT = "tight"
REGIME_DISASTER = "disaster"


@dataclass(frozen=True)
class SmartExitConfig:
    """Smart-exit knobs — env-overridable via
    ``dexter3/shadow_runner.py::_smart_exit_config_from_env``.

    enabled:
        DEXTER3_SMART_EXIT_ENABLED (default "1" -> True; "0" disables). When
        disabled, EVERY entry keeps the tight hard SL (regime is always
        'tight') — but the classification (what smart-exit WOULD have
        applied) is STILL computed and journaled, mirroring the anti-chase
        gate's shadow-mode posture.
    disaster_mult:
        DEXTER3_SMART_EXIT_DISASTER_MULT (default 2.0 — the wide hard-stop
        multiplier on the structural SL distance; mirrors
        ``scripts/dexter3_edge_discovery.py``'s ``--disaster-mult`` proven
        default region, see its own default of 2.5 for the sweep CLI — 2.0
        is this module's own conservative default per the owner directive's
        "disaster_mult default 2.0" instruction).
    """

    enabled: bool = True
    disaster_mult: float = 2.0


# ---------------------------------------------------------------------------
# entry-side: stop-regime classification (chase gate)
# ---------------------------------------------------------------------------


def resolve_stop_regime(
    side: str | None, h1_bars: list[Bar], cfg: SmartExitConfig | None = None, regime_thresh: float | None = None
) -> dict[str, Any]:
    """Classify one entry's stop regime: 'disaster' (smart exit ON) for
    NON-chase entries, 'tight' (today's behavior, unchanged) for chase
    entries OR whenever ``cfg.enabled`` is False.

    Reuses ``dexter3.edge_buckets.classify_bucket`` for the chase read —
    never reimplemented (module docstring). ``regime_thresh`` lets the
    caller pass the SAME threshold the anti-chase gate is using (so the two
    gates can never classify the same entry into disagreeing buckets); a
    caller that omits it gets ``edge_buckets``'s own default.

    ALWAYS returns the full classification dict, even when disabled — the
    caller journals this unconditionally (shadow mode, mirrors
    ``anti_chase_risk_mult``'s ``reason`` contract exactly).
    """
    from dexter3.edge_buckets import REGIME_THRESH_DEFAULT, classify_bucket

    cfg = cfg or SmartExitConfig()
    thresh = REGIME_THRESH_DEFAULT if regime_thresh is None else regime_thresh
    bucket = classify_bucket(side, h1_bars, regime_thresh=thresh)
    is_chase = bool(bucket.get("is_chase"))

    would_apply = not is_chase
    applied = would_apply and cfg.enabled
    stop_regime = REGIME_DISASTER if applied else REGIME_TIGHT

    # NOTE: classify_bucket's own 'regime' key ('trending'/'ranging'/
    # 'unknown', the H1 directional-efficiency read) is renamed to
    # 'h1_regime' here to avoid colliding with THIS function's 'regime' key
    # (the stop regime, 'tight'/'disaster') — callers that want the raw H1
    # regime label (e.g. for a log line) should read 'h1_regime'.
    result = dict(bucket)
    result["h1_regime"] = result.pop("regime", None)
    result.update(
        {
            "enabled": cfg.enabled,
            "would_apply_smart_exit": would_apply,
            "applied": applied,
            "regime": stop_regime,
            "disaster_mult": cfg.disaster_mult,
        }
    )
    return result


# ---------------------------------------------------------------------------
# entry-side: disaster-stop distance + size-down math
# ---------------------------------------------------------------------------


def disaster_stop_distance(sl_distance: float, disaster_mult: float) -> float:
    """The WIDE broker stop distance for the disaster regime: disaster_mult
    x the original (tight) structural SL distance. Never negative/zero for a
    positive input (clamped to the input itself if disaster_mult < 1, which
    should never happen with a sane config but must never narrow the stop)."""
    sl_distance = max(0.0, float(sl_distance))
    disaster_mult = max(1.0, float(disaster_mult))
    return sl_distance * disaster_mult


def size_scale_for_disaster_stop(disaster_mult: float) -> float:
    """The sizing scale-down factor so $ risk to the WIDE disaster stop stays
    EQUAL to the intended risk against the original tight distance:

        risk_usd = size x tight_distance  (today's sizing)
        size_disaster x (disaster_mult x tight_distance) == risk_usd
        => size_disaster == size x (1 / disaster_mult)

    This function returns that ``1 / disaster_mult`` scale. The caller
    (``executor.planned_volume_units``) achieves the equal-risk property
    simply by sizing against ``disaster_stop_distance(...)`` directly with
    the SAME (unscaled) ``risk_usd`` — this helper exists for callers/tests
    that want the explicit ratio without re-deriving it from the distance
    math (e.g. asserting volume shrank by ~1/disaster_mult)."""
    disaster_mult = max(1.0, float(disaster_mult))
    return 1.0 / disaster_mult


# ---------------------------------------------------------------------------
# exit-side: soft cut on confirmed structural break (OM branch)
# ---------------------------------------------------------------------------


def should_smart_exit(regime: str, evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Decide whether the OM's soft smart-loss-exit should fire THIS tick.

    Mirrors ``scripts/dexter3_edge_discovery.py::_simulate_smart``'s
    confirmed-break definition exactly, expressed in terms of the evidence
    dict ``dexter3.basket_live.structure_evidence`` already computes:
      - a wick beyond the tight invalidation that has since closed back
        inside (``level_lost`` True but ``m5_close_beyond`` False, or the
        most recent close no longer beyond) is NOISE — survived, hold.
      - a bar that CLOSED beyond the invalidation and remains beyond on the
        latest close (``level_lost`` AND ``m5_close_beyond`` both True) is a
        CONFIRMED break — the thesis is dead, cut now (soft cut; the broker
        disaster stop is only the tail-risk backstop, not the intended exit
        mechanism).

    Only applies to lanes classified into the 'disaster' regime — a 'tight'
    regime lane (chase entry, or smart exit disabled) never fires this path;
    its own broker-side tight hard SL is the entire exit mechanism, exactly
    as today. Returns ``{'fire': bool, 'reason': str}``; never raises on a
    malformed/missing evidence dict (degrades to ``fire=False``, i.e. hold —
    "never trade blind" applies to a cut decision just as much as to a
    repair decision)."""
    if regime != REGIME_DISASTER:
        return {"fire": False, "reason": "not_disaster_regime"}
    ev = evidence if isinstance(evidence, dict) else {}
    level_lost = bool(ev.get("level_lost"))
    m5_close_beyond = bool(ev.get("m5_close_beyond"))
    if level_lost and m5_close_beyond:
        return {"fire": True, "reason": "smart_confirmed_break"}
    if level_lost and not m5_close_beyond:
        return {"fire": False, "reason": "noise_wick_reclaimed"}
    return {"fire": False, "reason": "no_confirmed_break"}
