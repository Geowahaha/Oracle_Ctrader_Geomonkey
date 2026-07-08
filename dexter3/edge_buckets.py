"""Dexter3 EDGE BUCKETS — anti-chase sizing gate (Layer 2, shadow-measurable).

Owner directive 2026-07-08: ``scripts/dexter3_edge_discovery.py`` (Layer 1,
historical backtest sweep) found the FIRST data-proven edge in the live
system: the "aligned x trending" bucket — the decision's side agrees with
the H1 trend AND H1 is in a strong directional (trending, not ranging)
regime, i.e. CHASING a mature trend — is the ENTIRE system loss (-116R over
6 days, 43% of entries, robust across hold periods), while the other 57% of
trades are +85R.

This module is the PURE classification + sizing logic that turns that
finding into a downsize (never a block — participation-first stays, per the
HUNT MODE blueprint: a chase entry is still taken, just at scout size). No
I/O, no MCP, no randomness — mirrors ``scripts/dexter3_edge_discovery.py``'s
``_h1_trend_sign``/``_regime`` definitions exactly (same n/thresh defaults),
duplicated on purpose so this module has zero import coupling to ``scripts/``
(never touched per the task's constraints).

Wiring: ``dexter3/shadow_runner.py::run_symbol_cycle`` calls
``anti_chase_risk_mult`` at the live-entry call site, AFTER the governor's
risk_for_entry and BEFORE ``_execute_live_entry``, and multiplies the result
into ``risk_usd_override``. The classification is ALWAYS journaled (even
when ``DEXTER3_ANTICHASE_ENABLED=0``) — that shadow record is the Layer-2
forward measurement the PM needs to confirm the edge live before betting
bigger downsizes/blocks on it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

Bar = dict[str, Any]

# ---------------------------------------------------------------------------
# EXACT mirror of scripts/dexter3_edge_discovery.py's bucket definitions —
# same n/thresh defaults, kept as module constants for auditability.
# ---------------------------------------------------------------------------

H1_TREND_SIGN_N = 6
REGIME_N = 8
REGIME_THRESH_DEFAULT = 0.35


def h1_trend_sign(h1_bars: list[Bar], n: int = H1_TREND_SIGN_N) -> int:
    """Direction of the last n completed H1 bars (net close change).

    Mirrors ``scripts/dexter3_edge_discovery.py::_h1_trend_sign`` verbatim.
    """
    if len(h1_bars) < 2:
        return 0
    seg = h1_bars[-n:]
    net = float(seg[-1].get("close", 0)) - float(seg[0].get("close", 0))
    if net > 0:
        return 1
    if net < 0:
        return -1
    return 0


def regime(h1_bars: list[Bar], n: int = REGIME_N, thresh: float = REGIME_THRESH_DEFAULT) -> str:
    """Directional efficiency = |net move| / sum(|bar-to-bar moves|) over the
    last n H1 bars. High = trending, low = ranging.

    Mirrors ``scripts/dexter3_edge_discovery.py::_regime`` verbatim.
    """
    seg = h1_bars[-n:] if len(h1_bars) >= 2 else []
    if len(seg) < 3:
        return "unknown"
    closes = [float(b.get("close", 0.0)) for b in seg]
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[k] - closes[k - 1]) for k in range(1, len(closes)))
    if path <= 0:
        return "unknown"
    eff = net / path
    return "trending" if eff >= thresh else "ranging"


# ---------------------------------------------------------------------------
# bucket classification
# ---------------------------------------------------------------------------


def classify_bucket(side: str | None, h1_bars: list[Bar], regime_thresh: float = REGIME_THRESH_DEFAULT) -> dict[str, Any]:
    """Classify one decision into the same (align, regime) space the edge
    discovery sweep bucketed on.

    align:
        'aligned'  — side matches h1_trend_sign
        'counter'  — side opposes a nonzero h1_trend_sign
        'no_trend' — h1_trend_sign == 0 (flat H1 net change)
    regime:
        'trending' | 'ranging' | 'unknown' (see ``regime`` above)
    is_chase:
        True only when align == 'aligned' AND regime == 'trending' — the
        exact bucket the edge-discovery sweep proved is the entire system
        loss (-116R / 6 days / 43% of entries).
    """
    trend_sign = h1_trend_sign(h1_bars)
    side_sign = 1 if side == "buy" else (-1 if side == "sell" else 0)

    if trend_sign == 0:
        align = "no_trend"
    elif side_sign == trend_sign:
        align = "aligned"
    else:
        align = "counter"

    regime_label = regime(h1_bars, thresh=regime_thresh)
    is_chase = align == "aligned" and regime_label == "trending"

    return {
        "align": align,
        "regime": regime_label,
        "is_chase": is_chase,
        "h1_trend_sign": trend_sign,
        "side": side,
    }


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeGateConfig:
    """Anti-chase sizing gate knobs — env-overridable via
    ``dexter3/shadow_runner.py::_edge_gate_config_from_env``.

    enabled:
        DEXTER3_ANTICHASE_ENABLED (default "1" -> True; "0" disables). When
        disabled, ``anti_chase_risk_mult`` always returns multiplier 1.0 —
        but the classification is STILL computed/returned/journaled (shadow
        mode: record what the gate WOULD have done).
    chase_size_mult:
        DEXTER3_ANTICHASE_MULT (default 0.15 — scout-size downsize factor
        applied to the proven-loss "aligned x trending" chase bucket).
    regime_thresh:
        DEXTER3_ANTICHASE_REGIME_THRESH (default 0.35 — same directional-
        efficiency cutoff as the edge-discovery sweep's ``_regime``).
    """

    enabled: bool = True
    chase_size_mult: float = 0.15
    regime_thresh: float = REGIME_THRESH_DEFAULT


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------


def anti_chase_risk_mult(
    side: str | None, h1_bars: list[Bar], cfg: EdgeGateConfig | None = None
) -> tuple[float, dict[str, Any]]:
    """Return (multiplier, reason_dict).

    multiplier = cfg.chase_size_mult when the bucket is_chase AND
    cfg.enabled, else 1.0. The classification dict is ALWAYS returned in
    full (even when disabled or not a chase) so the caller can journal it
    unconditionally for Layer-2 shadow measurement.
    """
    cfg = cfg or EdgeGateConfig()
    bucket = classify_bucket(side, h1_bars, regime_thresh=cfg.regime_thresh)

    would_downsize = bucket["is_chase"]
    applied = would_downsize and cfg.enabled
    multiplier = cfg.chase_size_mult if applied else 1.0

    reason = dict(bucket)
    reason.update(
        {
            "enabled": cfg.enabled,
            "would_downsize": would_downsize,
            "applied": applied,
            "multiplier": multiplier,
            "chase_size_mult_cfg": cfg.chase_size_mult,
        }
    )
    return multiplier, reason
