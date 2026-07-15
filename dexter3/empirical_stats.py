"""Dexter3 empirical stats — Laplace-smoothed win rates by (setup, session)
from closed lane outcomes, for blending into ``hunter_brain.decide()``'s
``p_win_est`` (blueprint P5: "journal_stats will replace [base priors] with
live empirical rates").

Core function ``p_win_estimates`` is pure: it takes rows (already fetched
from the journal by the caller) and returns a plain dict — no DB access, no
I/O, fully unit-testable without a database. ``compute_from_journal`` is the
thin DB-facing wrapper ``shadow_runner`` calls in practice.

Blend policy (kept as module constants so tests and callers can reason about
them directly): below ``MIN_SAMPLES`` closed outcomes for a (setup, session)
key, fall back entirely to hunter_brain's base rate; at/above the floor,
blend 50/50 with the Laplace-smoothed empirical rate.
"""
from __future__ import annotations

import sqlite3
from typing import Any

MIN_SAMPLES = 10


def _label_matches_family(label: Any, family: Any) -> bool:
    """Family-prefix match (2026-07-15 versioned-labels design): ``label`` is
    "in" ``family`` when it equals it exactly or begins with it. ``label``
    callers pass here is now documented as a FAMILY prefix (e.g.
    "dexter3:fable"), not necessarily a full versioned label — this lets a
    version bump keep pooling stats across every version of the SAME lane,
    never a peer lane's. Duplicated locally rather than importing
    ``dexter3.executor.label_matches_family`` — same "no cross-imports of
    live/mutating internals, duplicate small pure logic" convention
    ``dexter3.basket_live`` documents for itself."""
    label_s = str(label or "")
    family_s = str(family or "")
    return bool(family_s) and (label_s == family_s or label_s.startswith(family_s))
BASE_BLEND_WEIGHT = 0.5
EMPIRICAL_BLEND_WEIGHT = 0.5
LAPLACE_ALPHA = 1.0  # add-one smoothing numerator
LAPLACE_BETA = 2.0  # add-one smoothing denominator (alpha for win + alpha for loss)


def _win_rate_laplace(wins: int, losses: int) -> float:
    """(wins + alpha) / (wins + losses + beta) — never exactly 0 or 1."""
    total = wins + losses
    return (wins + LAPLACE_ALPHA) / (total + LAPLACE_BETA)


def p_win_estimates(rows: list[dict[str, Any]], symbol: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Compute Laplace-smoothed empirical win rates keyed by (setup, session).

    ``rows`` — closed-outcome records for ``symbol``, each expected to carry
    at least: ``setup`` (str), ``session`` (str), ``pnl`` or ``won`` (a
    numeric PnL, or an explicit bool/0-1 win flag — either works). Rows
    missing ``setup``/``session`` are skipped (can't bucket them). A pnl of
    exactly 0 counts as neither a win nor a loss (breakeven — excluded from
    the win-rate denominator, consistent with how PF/win-rate reporting
    elsewhere in this repo treats scratch trades).

    Returns ``{(setup, session): {"win_rate": float, "samples": int,
    "wins": int, "losses": int, "below_min_samples": bool}}``. Pure function
    — no DB access, no I/O; safe to unit test with hand-built row lists.
    """
    buckets: dict[tuple[str, str], list[bool]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or symbol) != symbol:
            continue
        setup = str(row.get("setup") or "").strip()
        session = str(row.get("session") or "").strip()
        if not setup or not session:
            continue
        won = _row_won(row)
        if won is None:
            continue  # breakeven / unresolved — excluded from denominator
        buckets.setdefault((setup, session), []).append(won)

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, outcomes in buckets.items():
        wins = sum(1 for w in outcomes if w)
        losses = len(outcomes) - wins
        samples = len(outcomes)
        out[key] = {
            "win_rate": round(_win_rate_laplace(wins, losses), 4),
            "samples": samples,
            "wins": wins,
            "losses": losses,
            "below_min_samples": samples < MIN_SAMPLES,
        }
    return out


def _row_won(row: dict[str, Any]) -> bool | None:
    if "won" in row and row["won"] is not None:
        return bool(row["won"])
    pnl = row.get("pnl")
    if pnl is None:
        pnl = row.get("pnl_r")
    if pnl is None:
        return None
    try:
        pnl_val = float(pnl)
    except (TypeError, ValueError):
        return None
    if pnl_val > 0:
        return True
    if pnl_val < 0:
        return False
    return None  # exact breakeven — excluded


def blended_p_win(base_p_win: float, setup: str, session: str, stats: dict[tuple[str, str], dict[str, Any]] | None) -> float:
    """Blend hunter_brain's base p_win_est with the empirical rate for (setup, session).

    Below ``MIN_SAMPLES`` (or when no stats exist for the key), returns
    ``base_p_win`` unchanged. At/above the floor, returns the 50/50 blend
    per the module-level blend-weight constants. Result is clamped to
    [0.05, 0.95] to match hunter_brain's own clamp range.
    """
    if not stats:
        return round(base_p_win, 4)
    # Accept BOTH key shapes: the tuple-keyed dict from compute_from_journal
    # AND the flattened "setup|session" dict from stats_to_journal_stats_arg —
    # shadow_runner passes the FLATTENED shape into hunter_brain.decide()
    # (shadow_runner._JOURNAL_STATS_CACHE), so tuple-only lookup silently
    # returned None on every live call and the blend NEVER fired (found
    # 2026-07-10 while making the learner real).
    entry = stats.get((str(setup), str(session))) or stats.get(f"{setup}|{session}")
    if entry is None or bool(entry.get("below_min_samples", True)):
        return round(base_p_win, 4)
    empirical = float(entry.get("win_rate", base_p_win))
    blended = (BASE_BLEND_WEIGHT * base_p_win) + (EMPIRICAL_BLEND_WEIGHT * empirical)
    return round(max(0.05, min(0.95, blended)), 4)


# -- DB-facing wrapper (thin; core logic above stays DB-free) ---------------


def _closed_outcome_rows(conn: sqlite3.Connection, symbol: str, label: str | None = None) -> list[dict[str, Any]]:
    """Pull closed-outcome rows for ``symbol`` from exec_events + basket_events.

    Both tables may or may not exist yet (exec_events is created lazily by
    dexter3.executor; basket_events always exists via decision_journal's
    schema) — missing tables degrade to an empty contribution rather than
    raising, so this wrapper is safe to call before any live trade has ever
    closed.
    """
    rows: list[dict[str, Any]] = []
    rows.extend(_exec_events_outcome_rows(conn, symbol, label=label))
    # Basket events have no lane label, so including them would reintroduce
    # cross-lane contamination in the shared Fable/Grok journal. They remain
    # available for legacy/no-label analysis only.
    if label is None:
        rows.extend(_basket_events_outcome_rows(conn, symbol))
    return rows


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def _exec_events_outcome_rows(
    conn: sqlite3.Connection, symbol: str, *, label: str | None = None
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "exec_events"):
        return []
    import json

    out: list[dict[str, Any]] = []
    cur = conn.execute(
        "SELECT payload_json FROM exec_events WHERE symbol = ? AND event IN "
        "('lane_position_closed', 'naked_position_closed')",
        (symbol,),
    )
    for (payload_json,) in cur.fetchall():
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            continue
        result = payload.get("result") or {}
        setup = payload.get("setup") or result.get("setup")
        session = payload.get("session") or result.get("session")
        pnl = payload.get("pnl", result.get("pnl"))
        row_label = payload.get("label") or result.get("label")
        if label is not None and not _label_matches_family(row_label, label):
            continue
        if setup is None or session is None or pnl is None:
            continue
        out.append({"symbol": symbol, "setup": setup, "session": session, "pnl": pnl, "label": row_label})
    return out


def _basket_events_outcome_rows(conn: sqlite3.Connection, symbol: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "basket_events"):
        return []
    import json

    out: list[dict[str, Any]] = []
    cur = conn.execute(
        "SELECT payload_json FROM basket_events WHERE event IN "
        "('on_entry', 'on_m5_close') ORDER BY id"
    )
    for (payload_json,) in cur.fetchall():
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            continue
        result = payload.get("result") or {}
        detail = result.get("detail") or {}
        setup = detail.get("setup")
        session = detail.get("session")
        aggregate_r = payload.get("aggregate_r")
        if setup is None or session is None or aggregate_r is None:
            continue
        if result.get("action") not in ("close_all_in_profit", "close_all_cap_stop"):
            continue
        out.append({"symbol": symbol, "setup": setup, "session": session, "pnl": aggregate_r})
    return out


def compute_from_journal(
    journal: Any, symbol: str, *, label: str | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    """DB-facing wrapper: fetch closed outcomes for ``symbol`` and compute stats.

    Accepts a ``dexter3.decision_journal.DecisionJournal`` (reads its
    ``._conn``) or a bare ``sqlite3.Connection``. Returns the same shape as
    ``p_win_estimates``; safe to call with an empty/fresh journal (returns
    ``{}``).
    """
    conn: sqlite3.Connection = getattr(journal, "_conn", journal)
    rows = _closed_outcome_rows(conn, symbol, label=label)
    return p_win_estimates(rows, symbol)


def stats_to_journal_stats_arg(stats: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    """Serialize the tuple-keyed stats dict for passing as hunter_brain.decide(journal_stats=...).

    hunter_brain.decide() currently accepts journal_stats as an opaque dict
    reserved for future use (see hunter_brain.py docstring) — this helper
    flattens the tuple key to "setup|session" so it round-trips through any
    JSON-based transport without loss, while ``blended_p_win`` above is what
    actually consumes the tuple-keyed form directly when called in-process.
    """
    return {f"{setup}|{session}": entry for (setup, session), entry in stats.items()}
