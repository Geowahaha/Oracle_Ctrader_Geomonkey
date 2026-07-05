"""Dexter3 hunter brain — the M5 decision engine.

``decide()`` turns a lens feature snapshot into a ``Decision`` matching the
JSON contract in docs/DEXTER3_M5_HUNTER_BLUEPRINT.md exactly. Candidate
setups:

- ``dragon_shelf_short``   — Dragon upper-shelf rejection short. Reuses
  ``scripts/xau_intraday_dragon.py::classify_dragon_setup`` (imported, not
  reimplemented) when importable; the module is pure (no I/O, no cTrader
  import) so importing it here does not touch the live loop.
- ``shelf_reclaim_long``   — Dragon lower-shelf reclaim long (same source).
- ``leader_continuation``  — market_lens.leader_score >= LEADER_MIN_SCORE in
  the direction of swing structure.
- ``sweep_reclaim``        — liquidity sweep + confirmed reclaim reversal.

Participation-first: every M5 close produces a Decision. When
``action == "skip"``, ``reasons`` must name the concrete missing condition
(e.g. "leader_score 0.41 below floor 0.64 / leader score ต่ำกว่าเกณฑ์"),
never a generic "no edge" / "fear" message.

Every ``enter`` decision carries an exact ``sl`` (structural invalidation)
and a ``tp`` at the next shelf/swing level, with reward:risk >= 1.2 enforced
before the decision is returned (setups that can't satisfy it degrade to a
skip with the RR shortfall stated).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from dexter3 import market_lens

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

try:
    from xau_intraday_dragon import classify_dragon_setup as _classify_dragon_setup
except ImportError:  # pragma: no cover - defensive; module is pure and present in-repo
    _classify_dragon_setup = None

MIN_REWARD_RISK = 1.2

# Base empirical win-rate priors by setup, before leader_score/session
# adjustment (journal_stats will replace these with live empirical rates in
# a later phase — see blueprint P5). These are conservative starting points,
# not measured values.
BASE_P_WIN_BY_SETUP: dict[str, float] = {
    "dragon_shelf_short": 0.50,
    "shelf_reclaim_long": 0.48,
    "leader_continuation": 0.52,
    "sweep_reclaim": 0.50,
}

# Session multiplier nudges p_win_est — overlap/london are the XAU
# high-liquidity windows; off_hours/asian get a small haircut. BTC callers
# still pass a session tag (24/7 market) but the adjustment is mild either
# way (documented per blueprint P1 "tag anyway").
SESSION_P_WIN_ADJUST: dict[str, float] = {
    "london": 0.03,
    "overlap": 0.04,
    "ny": 0.02,
    "asian": -0.02,
    "off_hours": -0.03,
    "unknown": 0.0,
}

REASON_BILINGUAL: dict[str, str] = {
    "leader_score_below_floor": "leader score ต่ำกว่าเกณฑ์ / leader score below floor",
    "not_at_upper_shelf": "ราคาไม่อยู่ upper shelf / price not at upper day-range shelf",
    "not_at_lower_shelf": "ราคาไม่อยู่ lower shelf / price not at lower day-range shelf",
    "no_rejection_confirmed": "ไม่มีแท่งยืนยัน rejection / no confirming rejection bar",
    "no_reclaim_confirmed": "ไม่มีแท่งยืนยัน reclaim / no confirming reclaim bar",
    "no_liquidity_sweep": "ไม่มี liquidity sweep / no liquidity sweep detected",
    "sweep_not_reclaimed": "sweep แล้วแต่ยังไม่ reclaim / swept but not yet reclaimed",
    "swing_structure_unclear": "โครงสร้าง swing ไม่ชัดเจน / swing structure unclear (no trend classification)",
    "rr_below_floor": "reward:risk ต่ำกว่าเกณฑ์ขั้นต่ำ 1.2 / reward:risk below the 1.2 floor",
    "missing_invalidation_level": "ไม่มีระดับ invalidation ที่ชัดเจน / no clear structural invalidation level",
    "insufficient_bars": "แท่งเทียนไม่พอสำหรับวิเคราะห์ / insufficient bars for analysis",
    "no_candidate_setup_matched": "ไม่มี setup ใดเข้าเงื่อนไข / no candidate setup met its entry conditions",
}


def _bilingual(key: str, *extra: str) -> str:
    base = REASON_BILINGUAL.get(key, key)
    if extra:
        return base + " (" + "; ".join(extra) + ")"
    return base


@dataclass
class Decision:
    ts_close: str
    symbol: str
    action: str  # enter|skip|manage
    side: str | None
    entry_type: str | None  # market|limit|stop
    entry: float | None
    sl: float | None
    tp: float | None
    size_class: str  # small|normal
    leader_score: float
    p_win_est: float
    setup: str  # dragon_shelf_short|shelf_reclaim_long|leader_continuation|sweep_reclaim|none
    reasons: list[str] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _reward_risk(side: str, entry: float, sl: float, tp: float) -> float:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return 0.0
    return reward / risk


def _skip(
    *,
    ts_close: str,
    symbol: str,
    leader_score_val: float,
    reasons: list[str],
    features: dict[str, Any],
) -> Decision:
    return Decision(
        ts_close=ts_close,
        symbol=symbol,
        action="skip",
        side=None,
        entry_type=None,
        entry=None,
        sl=None,
        tp=None,
        size_class="none",
        leader_score=round(leader_score_val, 4),
        p_win_est=0.0,
        setup="none",
        reasons=reasons,
        features=features,
    )


def _p_win_est(setup: str, leader_score_val: float, session_label: str) -> float:
    base = BASE_P_WIN_BY_SETUP.get(setup, 0.50)
    leader_adjust = (leader_score_val - market_lens.LEADER_MIN_SCORE) * 0.25
    session_adjust = SESSION_P_WIN_ADJUST.get(session_label, 0.0)
    return round(max(0.05, min(0.95, base + leader_adjust + session_adjust)), 4)


def _build_features_snapshot(
    symbol: str,
    m5_bars: list[dict[str, Any]],
    lens_features: dict[str, Any],
) -> dict[str, Any]:
    snapshot = {"symbol": symbol, "bar_count_m5": len(m5_bars)}
    snapshot.update(lens_features)
    return snapshot


def _run_lens(m5_bars: list[dict[str, Any]], ts_close: str) -> dict[str, Any]:
    """Compute the full lens feature set once per decision call."""
    return {
        "swing_structure": market_lens.swing_structure(m5_bars),
        "liquidity_sweep": market_lens.liquidity_sweep(m5_bars),
        "displacement": market_lens.displacement(m5_bars),
        "compression_release": market_lens.compression_release(m5_bars),
        "close_location_pressure": market_lens.close_location_pressure(m5_bars),
        "day_range_position": market_lens.day_range_position(m5_bars),
        "session_context": market_lens.session_context(ts_close),
        "volatility_state": market_lens.volatility_state(m5_bars),
    }


def _dragon_snapshot(symbol: str, m5_bars: list[dict[str, Any]], lens: dict[str, Any]) -> dict[str, Any]:
    """Adapt lens/bar data into the snapshot shape classify_dragon_setup expects."""
    if not m5_bars:
        return {}
    last = m5_bars[-1]
    drp = lens["day_range_position"]
    swing = lens["swing_structure"]
    nearest_resistance = 0.0
    nearest_support = 0.0
    last_high = swing.get("last_swing_high") or {}
    last_low = swing.get("last_swing_low") or {}
    if last_high.get("price") is not None:
        nearest_resistance = float(last_high["price"])
    if last_low.get("price") is not None:
        nearest_support = float(last_low["price"])
    return {
        "mid": float(last.get("close") or 0.0),
        "day_hi": drp.get("day_hi") or float(last.get("high") or 0.0),
        "day_lo": drp.get("day_lo") or float(last.get("low") or 0.0),
        "m1_atr": max(0.1, float(market_lens.true_range(last, None))),
        "m15_bias": "",
        "m1": [last],
        "nearest_resistance": nearest_resistance,
        "nearest_support": nearest_support,
        "spread": 0.0,
    }


def _try_dragon_setup(
    symbol: str,
    m5_bars: list[dict[str, Any]],
    lens: dict[str, Any],
) -> tuple[str | None, str | None, float | None, float | None, float | None, list[str]]:
    """Return (setup, side, entry, sl, tp, reasons) if a Dragon shelf pattern fires."""
    empty: tuple[None, None, None, None, None, list[str]] = (None, None, None, None, None, [])
    if _classify_dragon_setup is None or not m5_bars:
        return empty

    snapshot = _dragon_snapshot(symbol, m5_bars, lens)
    if not snapshot:
        return empty
    result = _classify_dragon_setup(snapshot)
    if result.action != "trade":
        return empty

    entry = float(snapshot["mid"])
    sl = float(result.invalidation) if result.invalidation is not None else None
    tp = float(result.target) if result.target is not None else None
    setup = "dragon_shelf_short" if result.setup == "upper_shelf_rejection_short" else "shelf_reclaim_long"
    reasons = list(result.reasons)
    return setup, result.side, entry, sl, tp, reasons


def _try_sweep_reclaim_setup(
    m5_bars: list[dict[str, Any]],
    lens: dict[str, Any],
) -> tuple[str | None, str | None, float | None, float | None, float | None, list[str]]:
    """Liquidity sweep + confirmed reclaim reversal.

    Requires liquidity_sweep to have fired AND the reclaim() check against
    the swept level to confirm on the same (or immediately following) bar —
    the lens's liquidity_sweep already encodes "closed back inside" as part
    of its detection, so a positive sweep here IS the reclaim confirmation.
    SL sits beyond the sweep wick extreme; TP targets the opposite lens
    swing point (next shelf/swing per blueprint).
    """
    empty: tuple[None, None, None, None, None, list[str]] = (None, None, None, None, None, [])
    sweep = lens["liquidity_sweep"]
    if not sweep.get("value") or not m5_bars:
        return empty

    last = m5_bars[-1]
    close = float(last.get("close") or 0.0)
    side = sweep["side"]  # "buy" or "sell" — direction of the reversal trade
    if side == "buy":
        # swept below prior low, closed back above it -> long
        sl = float(last.get("low") or 0.0)
        swing = lens["swing_structure"].get("last_swing_high") or {}
        tp = float(swing["price"]) if swing.get("price") is not None else close + abs(close - sl) * 1.5
    elif side == "sell":
        sl = float(last.get("high") or 0.0)
        swing = lens["swing_structure"].get("last_swing_low") or {}
        tp = float(swing["price"]) if swing.get("price") is not None else close - abs(sl - close) * 1.5
    else:
        return empty

    reasons = [f"liquidity_sweep_reclaim: {sweep.get('evidence')}"]
    return "sweep_reclaim", side, close, sl, tp, reasons


def _try_leader_continuation_setup(
    m5_bars: list[dict[str, Any]],
    lens: dict[str, Any],
) -> tuple[str | None, str | None, float | None, float | None, float | None, list[str]]:
    """Leader continuation: leader_score >= floor, aligned with swing direction.

    SL sits at the most recent opposing swing point (structural invalidation
    in the trade direction); TP projects to 1.5x that risk distance in the
    absence of a further swing target (kept conservative — RR floor is
    enforced by the caller regardless).
    """
    empty: tuple[None, None, None, None, None, list[str]] = (None, None, None, None, None, [])
    if not m5_bars:
        return empty

    ls = market_lens.leader_score(lens)
    if ls["value"] < market_lens.LEADER_MIN_SCORE or ls["side"] is None:
        return empty

    swing = lens["swing_structure"]
    swing_dir = {"uptrend": "buy", "downtrend": "sell"}.get(swing.get("value"))
    if swing_dir is not None and swing_dir != ls["side"]:
        return empty  # leader score disagrees with swing direction -> not a clean continuation

    close = float(m5_bars[-1].get("close") or 0.0)
    side = ls["side"]
    if side == "buy":
        swing_low = swing.get("last_swing_low") or {}
        sl = float(swing_low["price"]) if swing_low.get("price") is not None else close - abs(
            market_lens.true_range(m5_bars[-1], None)
        ) * 1.5
        risk = abs(close - sl)
        tp = close + risk * 1.5
    else:
        swing_high = swing.get("last_swing_high") or {}
        sl = float(swing_high["price"]) if swing_high.get("price") is not None else close + abs(
            market_lens.true_range(m5_bars[-1], None)
        ) * 1.5
        risk = abs(sl - close)
        tp = close - risk * 1.5

    reasons = [f"leader_continuation: {ls.get('evidence')}", f"swing_alignment: {swing.get('value')}"]
    return "leader_continuation", side, close, sl, tp, reasons


def decide(
    symbol: str,
    features: dict[str, Any] | None,
    m5_bars: list[dict[str, Any]],
    m15_bars: list[dict[str, Any]] | None = None,
    h1_bars: list[dict[str, Any]] | None = None,
    journal_stats: dict[str, Any] | None = None,
) -> Decision:
    """Decide enter/skip/manage for one M5 close.

    ``features`` may be a pre-computed lens snapshot (as produced by
    ``_run_lens``); if ``None``, the lens is computed here from ``m5_bars``.
    ``m15_bars``/``h1_bars`` are accepted for future multi-timeframe
    confluence (Phase 2+) but Phase 1 candidate setups only require M5.
    ``journal_stats`` is reserved for empirical p_win_est by
    setup/session/regime (blueprint P5) — accepted as ``None`` for now.
    """
    ts_close = str(m5_bars[-1]["ts"]) if m5_bars else ""

    if len(m5_bars) < 20:
        lens_partial = features or {}
        return _skip(
            ts_close=ts_close,
            symbol=symbol,
            leader_score_val=0.0,
            reasons=[_bilingual("insufficient_bars", f"have={len(m5_bars)} need>=20")],
            features=_build_features_snapshot(symbol, m5_bars, lens_partial),
        )

    lens = features if features is not None else _run_lens(m5_bars, ts_close)
    ls = market_lens.leader_score(lens)
    session_label = str(lens.get("session_context", {}).get("value") or "unknown")
    features_snapshot = _build_features_snapshot(symbol, m5_bars, lens)

    # Candidate setups, in priority order: Dragon shelf patterns first (most
    # specific structural read), then sweep_reclaim, then leader continuation
    # (most general). First candidate that produces a valid, RR-passing
    # entry wins; if none do, we skip with the most informative reason set.
    candidates: list[tuple[str, str, float, float, float, list[str]]] = []
    skip_reasons: list[str] = []

    setup, side, entry, sl, tp, reasons = _try_dragon_setup(symbol, m5_bars, lens)
    if setup and side and entry is not None and sl is not None and tp is not None:
        candidates.append((setup, side, entry, sl, tp, reasons))
    else:
        drp = lens["day_range_position"]
        zone = drp.get("zone")
        if zone == "upper_shelf":
            skip_reasons.append(_bilingual("no_rejection_confirmed", f"day_range_position={drp.get('value')}"))
        elif zone == "lower_shelf":
            skip_reasons.append(_bilingual("no_reclaim_confirmed", f"day_range_position={drp.get('value')}"))
        else:
            skip_reasons.append(_bilingual("not_at_upper_shelf", f"day_range_position={drp.get('value')}"))

    setup, side, entry, sl, tp, reasons = _try_sweep_reclaim_setup(m5_bars, lens)
    if setup and side and entry is not None and sl is not None and tp is not None:
        candidates.append((setup, side, entry, sl, tp, reasons))
    else:
        sweep = lens["liquidity_sweep"]
        if not sweep.get("value"):
            skip_reasons.append(_bilingual("no_liquidity_sweep", str(sweep.get("evidence") or "")))
        else:
            skip_reasons.append(_bilingual("sweep_not_reclaimed", str(sweep.get("evidence") or "")))

    setup, side, entry, sl, tp, reasons = _try_leader_continuation_setup(m5_bars, lens)
    if setup and side and entry is not None and sl is not None and tp is not None:
        candidates.append((setup, side, entry, sl, tp, reasons))
    else:
        if ls["value"] < market_lens.LEADER_MIN_SCORE:
            skip_reasons.append(
                _bilingual(
                    "leader_score_below_floor",
                    f"score={ls['value']} floor={market_lens.LEADER_MIN_SCORE}",
                )
            )
        else:
            skip_reasons.append(_bilingual("swing_structure_unclear", f"swing={lens['swing_structure'].get('value')}"))

    # Evaluate candidates for RR >= floor; keep the first that clears it.
    rr_failures: list[str] = []
    for setup, side, entry, sl, tp, reasons in candidates:
        rr = _reward_risk(side, entry, sl, tp)
        if rr >= MIN_REWARD_RISK:
            p_win = _p_win_est(setup, ls["value"], session_label)
            size_class = "normal" if ls["band"] == "strong" else "small"
            entry_type = "market" if setup in ("sweep_reclaim", "leader_continuation") else "stop"
            full_reasons = list(reasons) + [f"reward_risk={rr:.2f}>=floor={MIN_REWARD_RISK}"]
            return Decision(
                ts_close=ts_close,
                symbol=symbol,
                action="enter",
                side=side,
                entry_type=entry_type,
                entry=round(entry, 5),
                sl=round(sl, 5),
                tp=round(tp, 5),
                size_class=size_class,
                leader_score=round(ls["value"], 4),
                p_win_est=p_win,
                setup=setup,
                reasons=full_reasons,
                features=features_snapshot,
            )
        rr_failures.append(f"{setup}: rr={rr:.2f}<floor={MIN_REWARD_RISK}")

    if rr_failures:
        skip_reasons = [_bilingual("rr_below_floor", "; ".join(rr_failures))] + skip_reasons
    if not skip_reasons:
        skip_reasons = [_bilingual("no_candidate_setup_matched")]

    return _skip(
        ts_close=ts_close,
        symbol=symbol,
        leader_score_val=ls["value"],
        reasons=skip_reasons,
        features=features_snapshot,
    )
