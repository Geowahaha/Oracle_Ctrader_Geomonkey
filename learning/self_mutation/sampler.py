"""Mutation sampler — generates 3 candidate config deltas from a loss event.

Design choices:
- **Whitelist-only.** Only knobs in `MUTABLE_KNOBS` are mutable. Everything else
  is treated as load-bearing proven config and never touched. Adding a knob to
  the whitelist is a deliberate, reviewable code change.
- **Sanity bands.** Every knob declares `(min, max, step)`. The proposer never
  produces a value outside the band, even if the rationale is strong.
- **Direction is data-driven.** The proposer asks the knob's narrative how a
  given loss context maps to a directional change (e.g. "loss was a market
  chase" → `XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE` should *increase*
  so the bar is higher).
- **Cooldown-aware.** Knobs touched in the last `cooldown_hours` are skipped.
- **Deterministic given fixed inputs.** Tests can rely on outputs.
- **No randomness on hot path.** Three candidates per call: a conservative tweak,
  a moderate tweak, and an aggressive tweak. Operator can scale step coefficients.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from learning.self_mutation.types import LossEvent, Mutation

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _short_id(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class KnobSpec:
    """Declares the contract of a mutable knob.

    `direction_hint` returns "increase" / "decrease" / "" given a LossEvent. An
    empty return means the knob is not relevant for this loss context — the
    sampler skips it.
    """

    name: str
    min_value: float
    max_value: float
    step: float
    narrative: str
    direction_hint: Callable[[LossEvent], str]


def _is_xau_short(event: LossEvent) -> bool:
    return event.symbol.upper().startswith("XAU") and event.direction.lower() == "short"


def _is_xau(event: LossEvent) -> bool:
    return event.symbol.upper().startswith("XAU")


def _hint_signal_market_min_score(event: LossEvent) -> str:
    if not _is_xau(event):
        return ""
    meta = dict(event.raw_meta or {})
    mode = str(meta.get("mode") or "").lower()
    entry_type = str(meta.get("entry_type") or "").lower()
    if mode == "signal_market" or entry_type == "market":
        return "increase"  # bar was too low → demand more confidence
    return ""


def _hint_signal_market_min_bias(event: LossEvent) -> str:
    if not _is_xau(event):
        return ""
    meta = dict(event.raw_meta or {})
    if str(meta.get("mode") or "").lower() == "signal_market":
        return "increase"
    return ""


def _hint_wait_break_probe_risk(event: LossEvent) -> str:
    if not _is_xau(event):
        return ""
    meta = dict(event.raw_meta or {})
    if str(meta.get("mode") or "").lower() == "wait_break_probe_stop":
        # A probe lost — keep the probe even smaller next time, don't kill it.
        return "decrease"
    return ""


def _hint_retest_risk_ratio(event: LossEvent) -> str:
    if not _is_xau(event):
        return ""
    meta = dict(event.raw_meta or {})
    if str(meta.get("entry_type") or "").lower() == "limit":
        return "decrease"
    return ""


def _hint_guardian_runner_r(event: LossEvent) -> str:
    if not _is_xau(event):
        return ""
    meta = dict(event.raw_meta or {})
    mfe_r = float(meta.get("mfe_r", 0.0) or 0.0)
    if mfe_r >= 1.0:
        # Trade was in profit by 1R+ and still came back to a loss — runner
        # threshold was too lax (waited too long to protect). Lower R.
        return "decrease"
    return ""


def _hint_pair_risk_max(event: LossEvent) -> str:
    if not _is_xau(event):
        return ""
    meta = dict(event.raw_meta or {})
    if meta.get("xau_pair_risk_cap_applied"):
        return "decrease"
    return ""


def _hint_risk_per_trade(event: LossEvent) -> str:
    # Triggered when an unusually large single loss happens — shrink global risk
    if event.pnl_usd <= -50.0:
        return "decrease"
    return ""


MUTABLE_KNOBS: tuple[KnobSpec, ...] = (
    KnobSpec(
        name="XAU_OPENAPI_ENTRY_ROUTER_WAIT_BREAK_PROBE_RISK_MULTIPLIER",
        min_value=0.10,
        max_value=1.0,
        step=0.05,
        narrative="Risk multiplier applied to wait-break probe stop entries.",
        direction_hint=_hint_wait_break_probe_risk,
    ),
    KnobSpec(
        name="XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_SCORE",
        min_value=4.0,
        max_value=10.0,
        step=1.0,
        narrative="Continuation score required to flip to signal-market entry.",
        direction_hint=_hint_signal_market_min_score,
    ),
    KnobSpec(
        name="XAU_OPENAPI_ENTRY_ROUTER_SIGNAL_MARKET_MIN_BIAS",
        min_value=0.50,
        max_value=0.90,
        step=0.05,
        narrative="Chart continuation bias required to flip to signal-market.",
        direction_hint=_hint_signal_market_min_bias,
    ),
    KnobSpec(
        name="XAU_OPENAPI_ENTRY_ROUTER_LIMIT_RETEST_RISK_RATIO",
        min_value=0.04,
        max_value=0.20,
        step=0.02,
        narrative="Per-thesis risk ratio for limit-retest entries.",
        direction_hint=_hint_retest_risk_ratio,
    ),
    KnobSpec(
        name="XAU_GUARDIAN_RUNNER_PRESERVE_R",
        min_value=0.8,
        max_value=3.0,
        step=0.1,
        narrative="R threshold above which guardian protects runners.",
        direction_hint=_hint_guardian_runner_r,
    ),
    KnobSpec(
        name="CTRADER_XAU_PAIR_RISK_MAX_USD",
        min_value=1.0,
        max_value=10.0,
        step=0.5,
        narrative="Maximum aggregate USD risk per XAU thesis.",
        direction_hint=_hint_pair_risk_max,
    ),
    KnobSpec(
        name="RISK_PER_TRADE",
        min_value=0.005,
        max_value=0.03,
        step=0.0025,
        narrative="Global risk fraction per trade.",
        direction_hint=_hint_risk_per_trade,
    ),
)

KNOB_BY_NAME: dict[str, KnobSpec] = {spec.name: spec for spec in MUTABLE_KNOBS}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _propose_value(spec: KnobSpec, baseline: float, direction: str, magnitude: float) -> float:
    """Return a new value, clamped to the knob's sanity band and rounded to step.

    `magnitude` is the multiplier on `spec.step` (e.g., 1.0 / 2.0 / 3.0 for the
    three candidate tiers).
    """
    delta = spec.step * float(magnitude)
    if direction == "decrease":
        delta = -delta
    raw = float(baseline) + delta
    clamped = _clamp(raw, spec.min_value, spec.max_value)
    # Round to step grid for cleanliness.
    if spec.step > 0:
        # Quantise relative to min_value to avoid floating-point drift.
        n_steps = round((clamped - spec.min_value) / spec.step)
        clamped = spec.min_value + n_steps * spec.step
        clamped = _clamp(clamped, spec.min_value, spec.max_value)
    # 6 decimals is plenty for our knobs and avoids trailing fp noise.
    return round(clamped, 6)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        v = value
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None


def _is_within_cooldown(last_utc: Optional[str], now: datetime, cooldown_hours: float) -> bool:
    if not last_utc:
        return False
    dt = _parse_iso(last_utc)
    if dt is None:
        return False
    return (now - dt) < timedelta(hours=float(cooldown_hours))


class Sampler:
    """Generates up to 3 mutation candidates per loss event.

    Three tiers per relevant knob:
    - tier 1 (conservative): step × 1
    - tier 2 (moderate):     step × 2
    - tier 3 (aggressive):   step × 3

    The output is at most three Mutation candidates total across all relevant
    knobs, picked greedily from the most-relevant knob first.
    """

    def __init__(
        self,
        *,
        baseline_resolver: Callable[[str], float],
        last_mutation_resolver: Callable[[str], Optional[str]],
        cooldown_hours: float = 24.0,
        max_candidates: int = 3,
    ) -> None:
        self._baseline = baseline_resolver
        self._last_mut = last_mutation_resolver
        self._cooldown_hours = float(cooldown_hours)
        self._max_candidates = int(max_candidates)

    def sample(self, event: LossEvent, *, now_utc: Optional[str] = None) -> list[Mutation]:
        now = _parse_iso(now_utc) or datetime.now(timezone.utc)
        relevant: list[tuple[KnobSpec, str]] = []
        for spec in MUTABLE_KNOBS:
            direction = spec.direction_hint(event)
            if direction not in ("increase", "decrease"):
                continue
            if _is_within_cooldown(self._last_mut(spec.name), now, self._cooldown_hours):
                continue
            relevant.append((spec, direction))
        if not relevant:
            return []

        # Round-robin tiers across knobs so we test diverse hypotheses first:
        # tier-1 of every relevant knob, then tier-2, etc. With max_candidates
        # smaller than len(relevant), this guarantees each candidate exercises
        # a different gene rather than three deltas of the same gene.
        candidates: list[Mutation] = []
        tiers = (1.0, 2.0, 3.0)
        for tier_idx, magnitude in enumerate(tiers, start=1):
            for spec, direction in relevant:
                baseline = float(self._baseline(spec.name))
                proposed = _propose_value(spec, baseline, direction, magnitude)
                if proposed == baseline:
                    continue  # Already at band edge in that direction.
                seed = f"{event.position_id}|{spec.name}|{direction}|{tier_idx}"
                mutation_id = f"mut_{_short_id(seed)}"
                rationale = (
                    f"Loss ${event.pnl_usd:.2f} on {event.symbol} {event.direction}: "
                    f"{spec.narrative} Direction={direction}, tier={tier_idx}."
                )
                candidates.append(
                    Mutation(
                        mutation_id=mutation_id,
                        knob=spec.name,
                        baseline_value=baseline,
                        proposed_value=proposed,
                        direction=direction,
                        rationale=rationale,
                        loss_event_position_id=event.position_id,
                        created_utc=now_utc or _utc_now_iso(),
                    )
                )
                if len(candidates) >= self._max_candidates:
                    return candidates
        return candidates


__all__ = ["KnobSpec", "MUTABLE_KNOBS", "KNOB_BY_NAME", "Sampler"]
