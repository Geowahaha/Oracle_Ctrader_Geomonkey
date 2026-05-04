"""Shadow-only multi-timeframe Fibo candidate generator for XAUUSD.

P2 scope: generate telemetry/KB candidates for W1..M1 and let scheduler persist them
with block_reason='fibo_mtf_shadow'. This module must never dispatch orders.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from analysis.fibonacci import FibonacciAnalyzer
from analysis.fibo_tf_telemetry import fibo_telemetry_payload, normalize_tf
from analysis.impulse_state import compute_impulse_state
from analysis.signals import TradeSignal
from market.data_fetcher import xauusd_provider, session_manager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FiboMtfSpec:
    tf_label: str
    fetch_timeframe: str
    setup_tf: str
    parent_tf: str
    bars: int
    risk_usd: float = 1.0
    min_confidence: float = 0.0
    cooldown_sec: int = 0


DEFAULT_SPECS: tuple[FiboMtfSpec, ...] = (
    FiboMtfSpec("W1", "1w", "W1", "W1", 180),
    FiboMtfSpec("D1", "1d", "D1", "W1", 220),
    FiboMtfSpec("H4", "4h", "H4", "D1", 240),
    FiboMtfSpec("H1", "1h", "H1", "H4", 260),
    FiboMtfSpec("M30", "30m", "M30", "H1", 260),
    FiboMtfSpec("M15", "15m", "M15", "H1", 260),
    FiboMtfSpec("M5", "5m", "M5", "M15", 300),
    FiboMtfSpec("M1", "1m", "M1", "M5", 360),
)

TF_ORDER: tuple[str, ...] = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1")
TF_RANK = {tf: i for i, tf in enumerate(TF_ORDER)}


def parent_chain_for_tf(tf_label: str, *, depth: int = 3) -> list[str]:
    """Return multi-level larger-TF chain. Alignment is a booster, not a gate."""
    tf = normalize_tf(tf_label)
    idx = TF_RANK.get(tf)
    if idx is None:
        return []
    bigger = TF_ORDER[idx + 1 : idx + 1 + max(0, int(depth or 0))]
    return list(bigger)


def _score_tuple(sig: TradeSignal) -> tuple[float, float]:
    raw = dict(getattr(sig, "raw_scores", {}) or {})
    return (float(getattr(sig, "confidence", 0.0) or 0.0), float(raw.get("impulse_state_confidence", 0.0) or 0.0))


def tag_parent_impulse_groups(signals: Iterable[TradeSignal]) -> list[TradeSignal]:
    """Tag same-parent clusters without suppressing any TF opportunity.

    Multi-TF means each TF can have its own nested Elliott opportunity. Same-parent
    grouping is telemetry only; it must not mark weaker TFs as blocked/suppressed.
    """
    out = list(signals or [])
    groups: dict[str, list[TradeSignal]] = {}
    for sig in out:
        raw = dict(getattr(sig, "raw_scores", {}) or {})
        pid = str(raw.get("parent_impulse_id") or f"no_parent:{id(sig)}")
        groups.setdefault(pid, []).append(sig)
    for pid, group in groups.items():
        leader = max(group, key=_score_tuple) if group else None
        for sig in group:
            raw = dict(getattr(sig, "raw_scores", {}) or {})
            raw["parent_impulse_group_id"] = pid
            raw["parent_impulse_group_size"] = len(group)
            raw["parent_impulse_group_leader"] = bool(sig is leader)
            raw.pop("suppressed_duplicate", None)
            sig.raw_scores = raw
    return out


def apply_alignment_boosters(signals: Iterable[TradeSignal]) -> list[TradeSignal]:
    """Annotate cross-TF agreement as an opportunity booster, never a gate."""
    out = list(signals or [])
    by_dir: dict[str, list[TradeSignal]] = {}
    for sig in out:
        by_dir.setdefault(str(getattr(sig, "direction", "") or ""), []).append(sig)
    for direction, group in by_dir.items():
        aligned_tfs = sorted({normalize_tf((getattr(sig, "raw_scores", {}) or {}).get("tf_label") or getattr(sig, "timeframe", "")) for sig in group}, key=lambda tf: TF_RANK.get(tf, 999))
        aligned_tfs = [tf for tf in aligned_tfs if tf]
        aligned_count = len(aligned_tfs)
        # Conservative shadow-only score: enough to rank evidence, not enough to dispatch.
        alignment_score = min(100.0, round(max(0, aligned_count - 1) * 20.0, 1))
        confidence_bonus = min(12.0, round(max(0, aligned_count - 1) * 3.0, 1))
        for sig in group:
            raw = dict(getattr(sig, "raw_scores", {}) or {})
            base_conf = float(getattr(sig, "confidence", 0.0) or 0.0)
            raw["alignment_booster"] = True
            raw["alignment_is_gate"] = False
            raw["aligned_direction"] = direction
            raw["aligned_tf_count"] = aligned_count
            raw["aligned_tf_list"] = aligned_tfs
            raw["alignment_score"] = alignment_score
            raw["alignment_confidence_bonus"] = confidence_bonus
            raw["opportunity_score"] = round(min(100.0, base_conf + confidence_bonus), 1)
            sig.raw_scores = raw
    return out


def _bars_for_impulse_state(df, limit: int = 80) -> list[dict]:
    if df is None or getattr(df, "empty", True):
        return []
    rows = []
    tail = df.tail(limit)
    for _, row in tail.iterrows():
        rows.append(
            {
                "open": float(row.get("open", 0.0) or 0.0),
                "high": float(row.get("high", 0.0) or 0.0),
                "low": float(row.get("low", 0.0) or 0.0),
                "close": float(row.get("close", 0.0) or 0.0),
                "volume": float(row.get("volume", 0.0) or 0.0),
            }
        )
    return rows


def _safe_atr(df) -> float:
    try:
        high = df["high"].tail(15).astype(float)
        low = df["low"].tail(15).astype(float)
        return float((high - low).mean() or 0.0)
    except Exception:
        return 0.0


def _candidate_from_context(spec: FiboMtfSpec, df, fibo: FibonacciAnalyzer) -> TradeSignal | None:
    if df is None or getattr(df, "empty", True) or len(df) < 30:
        return None
    current_price = float(df["close"].iloc[-1])
    atr = _safe_atr(df)
    if current_price <= 0 or atr <= 0:
        return None
    ctx = fibo.analyze(df_structure=df, df_entry=df, current_price=current_price, atr=atr, smc_context=None)
    fib = getattr(ctx, "fib_levels", None)
    if fib is None:
        return None
    fib_dir = str(getattr(fib, "direction", "") or "").strip().lower()
    if fib_dir not in {"bullish", "bearish"}:
        return None
    direction = "long" if fib_dir == "bullish" else "short"
    confluence = float(getattr(ctx, "fibo_confluence_score", 0.0) or 0.0)
    if confluence <= 0.0:
        return None
    entry = float(getattr(ctx, "nearest_level_price", 0.0) or current_price)
    stop_anchor = float(getattr(fib, "swing_start", current_price) or current_price)
    buffer = max(atr * 0.25, 0.01)
    if direction == "long":
        stop_loss = min(stop_anchor - buffer, entry - buffer)
        risk = max(entry - stop_loss, atr * 0.25)
        tp1, tp2, tp3 = entry + risk, entry + risk * 2.0, entry + risk * 3.0
    else:
        stop_loss = max(stop_anchor + buffer, entry + buffer)
        risk = max(stop_loss - entry, atr * 0.25)
        tp1, tp2, tp3 = entry - risk, entry - risk * 2.0, entry - risk * 3.0
    impulse = compute_impulse_state(_bars_for_impulse_state(df), current_direction=direction)
    telemetry = fibo_telemetry_payload(
        entry_tf=spec.tf_label,
        setup_tf=spec.setup_tf,
        parent_tf=spec.parent_tf,
        fibo_ctx=ctx,
        fib_levels=fib,
        source="fibo_xauusd",
    )
    parent_chain = parent_chain_for_tf(spec.tf_label)
    raw = {
        "mode": "mtf_shadow",
        "fibo_mtf_shadow": True,
        "fibo_mtf_live_enabled": False,
        **telemetry,
        "parent_chain": parent_chain,
        "parent_chain_depth": len(parent_chain),
        "tf_risk_usd": float(spec.risk_usd),
        "tf_min_confidence": float(spec.min_confidence),
        "tf_cooldown_sec": int(spec.cooldown_sec),
        "opportunity_first": True,
        "tf_alignment_policy": "booster_not_gate",
        "impulse_tf_stack": {
            "parent_chain": parent_chain,
            "parent_tf": normalize_tf(spec.parent_tf),
            "setup_tf": normalize_tf(spec.setup_tf),
            "entry_tf": normalize_tf(spec.tf_label),
            "mode": "mtf_shadow",
        },
        "retracement_depth": round(float(getattr(ctx, "retracement_depth", 0.0) or 0.0), 3),
        "nearest_level_ratio": round(float(getattr(ctx, "nearest_level_ratio", 0.0) or 0.0), 3),
        "fibo_confluence": round(float(getattr(ctx, "fibo_confluence_score", 0.0) or 0.0), 2),
        "impulse_strength": round(float(getattr(fib, "impulse_strength", 0.0) or 0.0), 3),
        "wave_phase": str(getattr(ctx, "wave_phase", "unknown") or "unknown"),
        "wave_confidence": round(float(getattr(ctx, "wave_confidence", 0.0) or 0.0), 3),
        "correction_end_score": round(float(getattr(ctx, "correction_end_score", 0.0) or 0.0), 2),
        "correction_end_confirmed": bool(getattr(ctx, "correction_end_confirmed", False)),
        "impulse_state_name": str(getattr(impulse.name, "value", impulse.name)),
        "impulse_state_direction": str(impulse.direction or ""),
        "impulse_state_confidence": round(float(impulse.confidence or 0.0), 3),
        "impulse_state_reasons": list(impulse.reasons),
        "suppressed_duplicate": False,
    }
    conf = min(85.0, max(0.0, confluence))
    return TradeSignal(
        symbol="XAUUSD",
        direction=direction,
        confidence=round(conf, 1),
        entry=round(entry, 2),
        stop_loss=round(stop_loss, 2),
        take_profit_1=round(tp1, 2),
        take_profit_2=round(tp2, 2),
        take_profit_3=round(tp3, 2),
        risk_reward=3.0,
        timeframe=normalize_tf(spec.tf_label),
        session=",".join((session_manager.get_session_info() or {}).get("active_sessions", []) or []),
        trend=str(raw.get("wave_phase") or "unknown"),
        rsi=0.0,
        atr=round(atr, 2),
        pattern=f"FIBO_MTF_SHADOW_{normalize_tf(spec.tf_label)}_{str(raw.get('ratio_zone')).upper()}",
        reasons=["fibo_mtf_shadow", f"tf:{normalize_tf(spec.tf_label)}", f"zone:{raw.get('ratio_zone')}", f"impulse:{raw.get('impulse_state_name')}"],
        warnings=[],
        raw_scores=raw,
        entry_type="limit",
        sl_type="shadow_structure",
        sl_reason="fibo_mtf_shadow_no_execution",
        tp_type="shadow_rr",
        tp_reason="fibo_mtf_shadow_no_execution",
    )


def dedupe_by_parent_impulse(signals: Iterable[TradeSignal]) -> list[TradeSignal]:
    """Backward-compatible alias: tag groups without suppressing opportunities."""
    return tag_parent_impulse_groups(signals)


def annotate_opportunity_lattice(signals: Iterable[TradeSignal]) -> list[TradeSignal]:
    """Apply parent grouping and alignment boosters while emitting every TF candidate."""
    return apply_alignment_boosters(tag_parent_impulse_groups(signals))



class FiboMtfShadowScanner:
    def __init__(self, *, provider=xauusd_provider, analyzer: FibonacciAnalyzer | None = None, specs: Iterable[FiboMtfSpec] = DEFAULT_SPECS):
        self.provider = provider
        self.analyzer = analyzer or FibonacciAnalyzer()
        self.specs = tuple(specs)

    def scan(self, *, include_suppressed: bool = True, emit_all: bool = True) -> list[TradeSignal]:
        out: list[TradeSignal] = []
        for spec in self.specs:
            try:
                df = self.provider.fetch(timeframe=spec.fetch_timeframe, bars=spec.bars)
                sig = _candidate_from_context(spec, df, self.analyzer)
                if sig is not None:
                    out.append(sig)
            except Exception as exc:
                logger.debug("[FiboMTFShadow] %s scan failed: %s", spec.tf_label, exc)
        # include_suppressed is deprecated: opportunity-first telemetry emits every TF.
        # Parent grouping and alignment are boosters/labels, never blockers.
        annotated = annotate_opportunity_lattice(out)
        if emit_all or include_suppressed:
            return annotated
        return annotated
