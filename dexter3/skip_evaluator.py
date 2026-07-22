"""Dexter3 skip evaluator — the blueprint's "fear cost" KPI.

For each ``decisions`` row with ``action == 'skip'`` older than
``SKIP_EVAL_DELAY_MIN`` minutes with no ``skip_outcomes`` row yet, simulate
the would-have trade over the following window and record whether it would
have won, so the journal can answer: how much would participation-first
entry have earned versus how much fear (skipping) actually cost?

Simulation inputs:
  - side: the features snapshot's recorded leading side, recomputed from the
    stored lens sub-dicts via ``market_lens.leader_score``.  A skip without a
    recorded side is unevaluable: deriving direction from bars after the
    decision would be look-ahead bias.
  - entry: the close of the decision's own bar (``ts_close``).
  - SL: 1.0x the true-range quantile (median TR) of the recent bars at
    decision time.
  - TP: 1.2x that same distance.

The simulation walks the M5 bars in ``[ts_close, ts_close + window_min]``
bar-by-bar and checks which level (SL or TP) is touched first (high/low
intrabar), consistent with how a real stop/limit would have resolved.
Missing history (MCP has nothing for the window, e.g. market was closed,
or fewer than 2 bars available) marks the outcome "unevaluable" rather than
guessing.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from dexter3 import market_lens
from dexter3.mcp_client import Dexter3McpClient, McpClientError, McpZombieError

SKIP_EVAL_DELAY_MIN = 30
SKIP_EVAL_WINDOW_MIN = 30
SL_TR_MULTIPLE = 1.0
TP_RR_MULTIPLE = 1.2
TR_LOOKBACK_BARS = 20
FEAR_COST_DEFAULT_HOURS = 24


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _recompute_leading_side(features: dict[str, Any]) -> str | None:
    """Recompute the leading side from a skip decision's stored lens sub-dicts.

    The features snapshot written by hunter_brain._build_features_snapshot
    carries the raw lens component dicts (liquidity_sweep, displacement,
    compression_release, close_location_pressure, swing_structure) but not
    the composite leader_score dict itself — market_lens.leader_score()
    only needs those raw components, so recomputing here is cheap and pure
    (no re-fetch of bars needed).
    """
    if not isinstance(features, dict):
        return None
    try:
        ls = market_lens.leader_score(features)
    except (KeyError, TypeError, AttributeError):
        return None
    side = ls.get("side")
    return side if side in ("buy", "sell") else None


def _median_true_range(bars: list[dict[str, Any]]) -> float:
    trs = [tr for tr in market_lens.true_ranges(bars) if tr > 0]
    if not trs:
        return 0.0
    return statistics.median(trs)


def determine_candidate_side(features: dict[str, Any]) -> tuple[str | None, str]:
    """Return the side recorded at decision time, never a future-derived side."""
    side = _recompute_leading_side(features)
    if side is not None:
        return side, "features_candidate"
    # Bias fallback (2026-07-22, owner audit): vp/daytrend/scalp skips never
    # populate the hunt-lens component keys _recompute_leading_side needs, so
    # they always fell through to here as "no_recorded_candidate" even when a
    # clear day-open-bias direction existed at decision time (shadow_runner
    # stamps that bias into features["skip_bias_side"] for exactly those 3
    # lanes -- see the run loop's insert_decision call site). Kept as a
    # distinct side_source (not "features_candidate") so fear_cost analysis
    # can always tell a hunt-lens-scored candidate from a bias-derived one.
    bias_side = features.get("skip_bias_side") if isinstance(features, dict) else None
    if bias_side in ("buy", "sell"):
        return bias_side, "bias_fallback_candidate"
    return None, "no_recorded_candidate"


def simulate_would_have_trade(
    *,
    side: str,
    entry: float,
    sl: float,
    tp: float,
    window_bars: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bar-by-bar walk of ``window_bars`` checking which of SL/TP is touched first.

    Conservative tie-break: if a single bar's range spans BOTH the SL and
    TP price (a large or gapping bar), the SL is assumed to have been hit
    first — the same "assume the worse outcome" convention used by
    ``BasketManager``'s hard-cap philosophy (never over-credit an unresolved
    ambiguity as a win).
    """
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return {"hit": "unevaluable", "pnl_r": 0.0, "reason": "zero_risk_distance"}

    for bar in window_bars:
        high = float(bar.get("high") or 0.0)
        low = float(bar.get("low") or 0.0)
        if side == "buy":
            hit_sl = low <= sl
            hit_tp = high >= tp
        else:
            hit_sl = high >= sl
            hit_tp = low <= tp
        if hit_sl and hit_tp:
            return {"hit": "loss", "pnl_r": -1.0, "reason": "same_bar_ambiguous_assume_loss", "bar_ts": bar.get("ts")}
        if hit_sl:
            return {"hit": "loss", "pnl_r": -1.0, "bar_ts": bar.get("ts")}
        if hit_tp:
            pnl_r = reward / risk
            return {"hit": "win", "pnl_r": round(pnl_r, 4), "bar_ts": bar.get("ts")}

    # Window elapsed without touching either level — mark-to-close on the
    # last available bar, expressed in R (may be positive or negative but
    # is NOT counted as a clean win/loss for the win-rate KPI).
    if not window_bars:
        return {"hit": "unevaluable", "pnl_r": 0.0, "reason": "no_bars_in_window"}
    last_close = float(window_bars[-1].get("close") or entry)
    direction = 1.0 if side == "buy" else -1.0
    open_pnl_r = (direction * (last_close - entry)) / risk
    return {"hit": "open_at_window_end", "pnl_r": round(open_pnl_r, 4)}


def evaluate_decision(
    decision_row: dict[str, Any],
    mcp: Dexter3McpClient,
) -> dict[str, Any]:
    """Evaluate a single skip decision row. Never raises — returns a result dict.

    ``decision_row`` is expected to carry at least: ``id``, ``symbol``,
    ``ts_close``, ``features`` (already JSON-decoded, as returned by
    ``DecisionJournal.recent_decisions``).
    """
    decision_id = int(decision_row.get("id") or 0)
    symbol = str(decision_row.get("symbol") or "")
    ts_close = str(decision_row.get("ts_close") or "")
    features = decision_row.get("features") or {}

    close_dt = _parse_ts(ts_close)
    if close_dt is None or not symbol:
        return {"decision_id": decision_id, "evaluated": False, "reason": "invalid_decision_row"}

    window_end = close_dt + timedelta(minutes=SKIP_EVAL_WINDOW_MIN)
    try:
        bars = mcp.get_trendbars(symbol, "m5", 200)
    except (McpClientError, McpZombieError) as exc:
        return {"decision_id": decision_id, "evaluated": False, "reason": f"mcp_error:{exc}"}

    window_bars = [b for b in bars if close_dt <= _parse_bar_ts(b) <= window_end]
    if len(window_bars) < 1:
        return {"decision_id": decision_id, "evaluated": False, "reason": "insufficient_history_for_window"}

    side, side_source = determine_candidate_side(features)
    if side is None:
        return {"decision_id": decision_id, "evaluated": False, "reason": "no_determinable_side"}

    lookback_bars = [b for b in bars if _parse_bar_ts(b) <= close_dt][-TR_LOOKBACK_BARS:]
    tr = _median_true_range(lookback_bars if lookback_bars else window_bars)
    if tr <= 0:
        return {"decision_id": decision_id, "evaluated": False, "reason": "zero_true_range"}

    # entry = close at the decision bar itself. window_bars is filtered to
    # [ts_close, ts_close + window_min], so its first element IS the
    # decision bar when history includes it; fall back to the last
    # lookback bar (closed at-or-before ts_close) if the decision bar
    # itself is absent from history (e.g. a data gap right at ts_close).
    decision_bar = next((b for b in window_bars if str(b.get("ts") or "") == ts_close), None)
    if decision_bar is None:
        decision_bar = lookback_bars[-1] if lookback_bars else window_bars[0]
    entry = float(decision_bar.get("close") or 0.0)
    sl_distance = tr * SL_TR_MULTIPLE
    tp_distance = sl_distance * TP_RR_MULTIPLE
    if side == "buy":
        sl = entry - sl_distance
        tp = entry + tp_distance
    else:
        sl = entry + sl_distance
        tp = entry - tp_distance

    # Simulate only bars AFTER the decision/entry bar — the entry bar's own
    # high/low must not be allowed to "trigger" its own just-opened SL/TP
    # (that would be a same-bar lookahead artifact, not a real fill path).
    simulation_bars = [b for b in window_bars if b is not decision_bar]
    sim = simulate_would_have_trade(side=side, entry=entry, sl=sl, tp=tp, window_bars=simulation_bars)
    would_have_result = {
        "side": side,
        "side_source": side_source,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "hit": sim["hit"],
        "pnl_r": sim["pnl_r"],
        "detail": {k: v for k, v in sim.items() if k not in ("hit", "pnl_r")},
    }
    return {
        "decision_id": decision_id,
        "evaluated": True,
        "would_have_result": would_have_result,
        "pnl_r": sim["pnl_r"],
        "hit": sim["hit"],
    }


def _parse_bar_ts(bar: dict[str, Any]) -> datetime:
    return _parse_ts(str(bar.get("ts") or "")) or datetime.min.replace(tzinfo=timezone.utc)


def evaluate_pending_skips(
    journal: Any,
    mcp: Dexter3McpClient,
    *,
    now: datetime | None = None,
    delay_min: int = SKIP_EVAL_DELAY_MIN,
    limit: int = 200,
    label: str | None = None,
) -> dict[str, Any]:
    """Evaluate all eligible pending skip decisions and write skip_outcomes rows.

    A decision is eligible when: ``action == 'skip'``, ``ts_close`` is at
    least ``delay_min`` minutes before ``now``, and no ``skip_outcomes`` row
    already references it. Never raises — MCP/history failures degrade a
    single decision to "unevaluable" and move on to the next one.

    ``label`` (H2, 2026-07-15 cross-lane entanglement audit; FAMILY-PREFIX
    semantics added 2026-07-15 versioned-labels design): when provided,
    restricts eligible decisions to rows whose label equals this value OR
    begins with it (a FAMILY prefix, e.g. "dexter3:fable" — spans every
    version of that lane's label) — same exclusion convention as
    ``dexter3.empirical_stats``'s own ``label`` param (``None`` = no
    filter/legacy pooled behavior; a value excludes both unlabeled rows AND a
    peer lane's rows). Two lanes sharing this journal previously raced to
    evaluate the SAME unlabeled skip rows (each writing its own skip_outcomes
    row for identical decisions); passing each lane's own label/family here
    means every lane only ever claims its OWN rows, which incidentally also
    kills that duplicate-evaluation race — no unique index needed, the label
    filter alone makes the row sets disjoint.
    """
    now = now or datetime.now(timezone.utc)
    conn = getattr(journal, "_conn", journal)
    pending = _fetch_pending_skip_rows(conn, now=now, delay_min=delay_min, limit=limit, label=label)

    evaluated_count = 0
    unevaluable_count = 0
    for row in pending:
        result = evaluate_decision(row, mcp)
        decision_id = result["decision_id"]
        row_label = row.get("label")
        if result.get("evaluated"):
            would_have_result = result["would_have_result"]
            _insert_skip_outcome(
                conn,
                decision_id=decision_id,
                would_have_result=json.dumps(would_have_result, ensure_ascii=False, default=str),
                would_have_pnl=float(result["pnl_r"]),
                label=row_label,
            )
            evaluated_count += 1
        else:
            _insert_skip_outcome(
                conn,
                decision_id=decision_id,
                would_have_result=json.dumps({"unevaluable": True, "reason": result.get("reason")}, ensure_ascii=False),
                would_have_pnl=None,
                label=row_label,
            )
            unevaluable_count += 1

    return {
        "checked": len(pending),
        "evaluated": evaluated_count,
        "unevaluable": unevaluable_count,
    }


def _fetch_pending_skip_rows(
    conn: Any, *, now: datetime, delay_min: int, limit: int, label: str | None = None
) -> list[dict[str, Any]]:
    cutoff = _iso_z(now - timedelta(minutes=delay_min))
    query = """
        SELECT d.id, d.ts_close, d.symbol, d.action, d.features_json, d.label
        FROM decisions d
        LEFT JOIN skip_outcomes so ON so.decision_id = d.id
        WHERE d.action = 'skip' AND d.ts_close <= ? AND so.id IS NULL
    """
    params: list[Any] = [cutoff]
    if label is not None:
        # H2 exclusion convention (matches empirical_stats.py): a lane must
        # only ever evaluate ITS OWN decisions — legacy unlabeled rows and a
        # peer lane's rows are excluded, never pooled in. FAMILY-PREFIX match
        # (2026-07-15 versioned-labels design): equals `label` exactly OR
        # begins with it, so a version bump keeps evaluating every version of
        # the SAME lane's rows. Family values in this repo never contain SQL
        # LIKE wildcards (%, _), so a plain LIKE-prefix is safe here.
        query += " AND (d.label = ? OR d.label LIKE ?)"
        params.append(label)
        params.append(f"{label}%")
    query += " ORDER BY d.id ASC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(query, tuple(params)).fetchall()
    out: list[dict[str, Any]] = []
    for row_id, ts_close, symbol, action, features_json, row_label in rows:
        try:
            features = json.loads(features_json or "{}")
        except json.JSONDecodeError:
            features = {}
        out.append(
            {
                "id": row_id,
                "ts_close": ts_close,
                "symbol": symbol,
                "action": action,
                "features": features,
                "label": row_label,
            }
        )
    return out


def _insert_skip_outcome(
    conn: Any,
    *,
    decision_id: int,
    would_have_result: str | None,
    would_have_pnl: float | None,
    label: str | None = None,
) -> int:
    from datetime import datetime as _dt

    cur = conn.execute(
        """INSERT INTO skip_outcomes (decision_id, would_have_result, would_have_pnl, evaluated_at, label)
           VALUES (?, ?, ?, ?, ?)""",
        (
            int(decision_id),
            would_have_result,
            would_have_pnl,
            _dt.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            label,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# -- fear cost KPI ------------------------------------------------------------


def fear_cost_summary(
    journal: Any,
    hours: int = FEAR_COST_DEFAULT_HOURS,
    *,
    now: datetime | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Validated fear-cost KPI over the last ``hours``.

    Reads only already-evaluated ``skip_outcomes`` rows (does not trigger
    evaluation itself — call ``evaluate_pending_skips`` first in the loop).
    Unevaluable rows (``would_have_pnl IS NULL``) are excluded from both the
    win count and the PnL sum, but their count is reported separately so the
    KPI is honest about coverage gaps.  Legacy rows whose side was derived
    from future bars are excluded and reported as ``invalid_lookahead``.

    ``label`` (H2, 2026-07-15 cross-lane entanglement audit; FAMILY-PREFIX
    semantics added 2026-07-15 versioned-labels design): restricts the KPI to
    decisions whose label equals this value OR begins with it (a FAMILY
    prefix — spans every version of that lane's label) — same exclusion
    convention as ``evaluate_pending_skips``/``empirical_stats`` (``None`` =
    no filter, legacy pooled-across-lanes behavior). Filters on the
    DECISION's own label (the source of truth), not skip_outcomes' redundant
    copy.
    """
    conn = getattr(journal, "_conn", journal)
    cutoff = _iso_z((now or datetime.now(timezone.utc)) - timedelta(hours=hours))
    query = """
        SELECT so.would_have_result, so.would_have_pnl
        FROM skip_outcomes so
        JOIN decisions d ON d.id = so.decision_id
        WHERE d.ts_close >= ?
    """
    params: list[Any] = [cutoff]
    if label is not None:
        # Family-prefix match — see _fetch_pending_skip_rows's comment above.
        query += " AND (d.label = ? OR d.label LIKE ?)"
        params.append(label)
        params.append(f"{label}%")
    rows = conn.execute(query, tuple(params)).fetchall()

    skips_evaluated = 0
    unevaluable = 0
    invalid_lookahead = 0
    would_have_wins = 0
    would_have_pnl_r = 0.0
    for would_have_result_json, would_have_pnl in rows:
        if would_have_pnl is None:
            unevaluable += 1
            continue
        try:
            payload = json.loads(would_have_result_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        # Results created before the P0 fix may have selected direction from
        # the *future* evaluation window.  Keep them in the DB for audit, but
        # do not let them influence the KPI or a promotion decision.
        if payload.get("side_source") == "day_range_drift":
            invalid_lookahead += 1
            continue
        skips_evaluated += 1
        would_have_pnl_r += float(would_have_pnl)
        if payload.get("hit") == "win":
            would_have_wins += 1

    return {
        "hours": hours,
        "skips_evaluated": skips_evaluated,
        "unevaluable": unevaluable,
        "invalid_lookahead": invalid_lookahead,
        "would_have_wins": would_have_wins,
        "would_have_pnl_r": round(would_have_pnl_r, 4),
    }
