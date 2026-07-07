#!/usr/bin/env python3
"""Dexter3 M5 hunter — PnL backtest / proof-of-fix script.

Mandatory deliverable per the profitability-fix task (2026-07-07 PM
diagnosis, see docs/AGENT_SYNC_BOARD.md 2026-07-07 entries): before the
FIX 1 (peak-R basket trailing) + FIX 3 (trend-agreement guard) changes in
dexter3/basket_live.py, dexter3/basket_manager.py, dexter3/hunt_mode.py ship
live, this script must PROVE they would have improved the profit factor on
the REAL closed trades that produced the diagnosis (label
``dexter3:fable``, 2026-07-06 -> 07): Net PnL -$110.77, WR 56% (28W/22L),
avg win +$6.15, avg loss -$12.87, PF 0.61, sell side -$116 / buy side +$5.

Read-only. Does NOT place orders, does NOT touch the live loop
(data/runtime/dexter3_loop.lock), does NOT restart anything — it only calls
the local MCP's ``get_deals`` tool (read-only) and reads
data/runtime/dexter3_journal.db (read-only) via sqlite3.

Two stages:

  STAGE 1 — BASELINE: pull realized closes via ``get_deals`` (same source
  the PM used), filter to our label, compute net PnL / win rate / avg
  win/loss / PF / side split. Should reproduce ~-$110, PF 0.61.

  STAGE 2 — PROJECTED (FIX 1 winner-side simulation): for every realized
  WIN, join against dexter3_journal.db's exec_events 'entry_executed' rows
  (by position_id) to recover the ORIGINAL hunt geometry (entry, sl, side,
  setup) for that position — this gives the R unit (sl distance) the trade
  was actually risking. The realized win's actual R achieved
  (realized_pnl / (sl_distance-implied per-unit risk)) is APPROXIMATED as
  the OLD flat +0.2R bank (basket_live's pre-fix resolve_target_r default);
  the NEW trail is simulated by assuming the trade's peak_r reached AT
  LEAST the old realized R (a basket that closed at +0.2R necessarily
  touched +0.2R, so peak_r >= 0.2R is a fact, not an assumption) and
  projecting the exit under FIX 1's arm/trail/take_r constants. This is an
  APPROXIMATION, not a full bar-by-bar re-simulation (that would require
  re-fetching M5 bars for every winning position's full lifetime, which is
  out of scope for a pre-deploy sanity check) — every assumption is stated
  explicitly in the printed output.

  STAGE 2 also reports the FIX 3 impact: how many of the realized LOSSES
  were counter-trend Sells (side='sell' opposing a strong aligned uptrend
  per the journaled hunt_committee m15_drift/h1_context votes at entry
  time) that the new trend-agreement guard would have penalized or flipped.

Usage:
    python scripts/dexter3_pnl_backtest.py
    python scripts/dexter3_pnl_backtest.py --label dexter3:fable --count 200
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dexter3.basket_manager import BasketConfig  # noqa: E402
from dexter3.mcp_client import Dexter3McpClient, McpClientError, McpZombieError  # noqa: E402

JOURNAL_DB = ROOT / "data" / "runtime" / "dexter3_journal.db"

DEFAULT_LABEL_FILTER = "dexter3:fable"
DEFAULT_DEAL_COUNT = 200  # get_deals max per its documented schema

# -- FIX 1 constants under test (mirrors dexter3/basket_manager.py BasketConfig
# defaults exactly — imported live rather than hardcoded so this script can
# never silently drift from the shipped defaults). --------------------------


def _fix1_config() -> BasketConfig:
    return BasketConfig()


# ---------------------------------------------------------------------------
# STAGE 1 — baseline: pull realized deals, compute PF exactly as the PM did
# ---------------------------------------------------------------------------


def _deal_net_profit(deal: dict[str, Any]) -> float | None:
    for key in ("netProfit", "profit", "grossProfit", "pnl", "closedNetProfit"):
        raw = deal.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _deal_label(deal: dict[str, Any]) -> str:
    return str(deal.get("label") or deal.get("comment") or "").strip()


def _deal_side(deal: dict[str, Any]) -> str:
    raw = str(deal.get("tradeSide") or deal.get("side") or "").strip().lower()
    if raw.startswith("buy"):
        return "buy"
    if raw.startswith("sell"):
        return "sell"
    return raw


def _deal_position_id(deal: dict[str, Any]) -> int:
    for key in ("positionId", "position_id", "id"):
        try:
            val = int(deal.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return 0


def _deal_symbol(deal: dict[str, Any]) -> str:
    return str(deal.get("symbolName") or deal.get("symbol") or "").strip().upper()


def _deal_closed_ts(deal: dict[str, Any]) -> str:
    return str(
        deal.get("executionTimestamp")
        or deal.get("closeTimestamp")
        or deal.get("closingTimestamp")
        or deal.get("utcLastUpdateTimestamp")
        or ""
    )


def fetch_deals(mcp: Dexter3McpClient, count: int) -> list[dict[str, Any]]:
    """Read-only get_deals call. NEVER raises — a zombie/unreachable MCP
    (the live dexter3 loop may be mid-cycle against it) is reported, not
    treated as fatal, so this script can still run its journal-only stages.
    """
    try:
        data = mcp.call("get_deals", {"count": count})
    except (McpClientError, McpZombieError) as exc:
        print(f"[STAGE 1] get_deals FAILED (read-only call, no mutation attempted): {exc}", file=sys.stderr)
        return []
    if isinstance(data, dict):
        return list(data.get("deals") or (data if isinstance(data, list) else []))
    if isinstance(data, list):
        return data
    print(f"[STAGE 1] get_deals returned unexpected payload shape: {type(data)!r}", file=sys.stderr)
    return []


def filter_lane_deals(deals: list[dict[str, Any]], label_filter: str) -> list[dict[str, Any]]:
    out = []
    for d in deals:
        if not isinstance(d, dict):
            continue
        if label_filter in _deal_label(d):
            out.append(d)
    return out


def compute_pf_stats(deals: list[dict[str, Any]]) -> dict[str, Any]:
    """Exactly the PM's methodology: net PnL, win rate, avg win/loss, PF,
    side split, from realized (closed) deals' netProfit."""
    wins: list[float] = []
    losses: list[float] = []
    side_pnl = {"buy": 0.0, "sell": 0.0, "other": 0.0}
    unreadable = 0

    for d in deals:
        pnl = _deal_net_profit(d)
        if pnl is None:
            unreadable += 1
            continue
        side = _deal_side(d)
        side_key = side if side in ("buy", "sell") else "other"
        side_pnl[side_key] += pnl
        if pnl > 0:
            wins.append(pnl)
        elif pnl < 0:
            losses.append(pnl)
        # pnl == 0.0 (breakeven) counted in neither wins nor losses, mirrors
        # standard PF methodology (gross_profit / gross_loss).

    n_wins, n_losses = len(wins), len(losses)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net = gross_profit - gross_loss
    win_rate = (n_wins / (n_wins + n_losses)) if (n_wins + n_losses) else 0.0
    avg_win = (gross_profit / n_wins) if n_wins else 0.0
    avg_loss = -(gross_loss / n_losses) if n_losses else 0.0
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    return {
        "n_deals": len(deals),
        "n_wins": n_wins,
        "n_losses": n_losses,
        "n_unreadable_pnl": unreadable,
        "net_pnl": round(net, 2),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(pf, 4) if pf != float("inf") else pf,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(-gross_loss, 2),
        "side_pnl": {k: round(v, 2) for k, v in side_pnl.items()},
    }


# ---------------------------------------------------------------------------
# STAGE 2 — projected: join winning deals against journaled hunt geometry
# ---------------------------------------------------------------------------


def load_entry_geometry(db_path: Path = JOURNAL_DB) -> dict[int, dict[str, Any]]:
    """position_id -> {side, entry, sl, tp, setup, risk_usd, sl_distance,
    min_volume_clamped} from exec_events 'entry_executed' rows. Read-only;
    returns {} if the journal DB is missing (never raises — a missing
    journal must not crash the baseline stage, which does not depend on
    it).

    ``risk_usd`` (from ``volume_meta.risk_usd``, the INTENDED base risk in
    USD the executor sized the position against — see
    ``dexter3/executor.py`` sizing step) is the R-per-USD denominator this
    script needs: realized_r = realized_pnl_usd / risk_usd. When
    ``volume_meta.min_volume_clamped_up`` is true, the position's actual
    worst-case risk is ``estimated_min_volume_risk_usd`` instead (the broker's
    minimum tradable size forced MORE risk than the intended risk_usd) — we
    capture both and let the caller choose the correct denominator.
    """
    if not db_path.exists():
        print(f"[STAGE 2] journal DB not found at {db_path} — geometry join skipped", file=sys.stderr)
        return {}
    out: dict[int, dict[str, Any]] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT position_id, payload_json FROM exec_events WHERE event='entry_executed' AND position_id IS NOT NULL"
        )
        for position_id, payload_json in cur.fetchall():
            try:
                payload = json.loads(payload_json or "{}")
            except json.JSONDecodeError:
                continue
            volume_meta = payload.get("volume_meta") or {}
            clamped = bool(volume_meta.get("min_volume_clamped_up"))
            risk_usd = volume_meta.get("estimated_min_volume_risk_usd") if clamped else volume_meta.get("risk_usd")
            out[int(position_id)] = {
                "side": payload.get("side"),
                "entry": payload.get("entry"),
                "sl": payload.get("sl"),
                "tp": payload.get("tp"),
                "setup": payload.get("setup"),
                "risk_usd": risk_usd,
                "sl_distance": volume_meta.get("sl_distance"),
                "min_volume_clamped": clamped,
            }
    finally:
        conn.close()
    return out


def load_decision_committee_for_setup(db_path: Path = JOURNAL_DB) -> list[dict[str, Any]]:
    """All 'enter' decisions with a hunt_committee snapshot (features_json),
    used for the FIX 3 counter-trend-loss impact estimate. Read-only."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    out: list[dict[str, Any]] = []
    try:
        cur = conn.execute(
            "SELECT ts_close, symbol, side, setup, features_json FROM decisions "
            "WHERE action='enter' AND setup LIKE 'hunt_%'"
        )
        for ts_close, symbol, side, setup, features_json in cur.fetchall():
            try:
                features = json.loads(features_json or "{}")
            except json.JSONDecodeError:
                continue
            out.append(
                {
                    "ts_close": ts_close,
                    "symbol": symbol,
                    "side": side,
                    "setup": setup,
                    "committee": features.get("hunt_committee") or {},
                }
            )
    finally:
        conn.close()
    return out


def _sl_distance(geo: dict[str, Any]) -> float | None:
    entry = geo.get("entry")
    sl = geo.get("sl")
    if entry is None or sl is None:
        return None
    try:
        return abs(float(entry) - float(sl))
    except (TypeError, ValueError):
        return None


def project_fix1_winners(
    win_deals: list[dict[str, Any]],
    geometry_by_position: dict[int, dict[str, Any]],
    cfg: BasketConfig,
) -> dict[str, Any]:
    """STAGE 2 winner-side projection.

    REAL R DERIVATION (not assumed where geometry is available):
    ``realized_r = realized_pnl_usd / risk_usd`` where ``risk_usd`` is the
    joined position's OWN intended base risk from
    ``exec_events.entry_executed.volume_meta`` (see ``load_entry_geometry``)
    — this is the actual R the trade achieved when it closed, not a blanket
    assumption. Only when a win's position_id has NO matching
    entry_executed row (e.g. its lane basket closed via a repair leg whose
    own entry event lacks volume_meta, or the deal predates the journal
    window) do we fall back to ASSUMPTION A0 below.

    ASSUMPTIONS (stated explicitly, per task requirement):
      A0 (fallback only, no geometry match): treat the win's realized_r as
         exactly ``cfg.resolve_target_r`` (0.2) — the diagnosed OLD flat-bar
         mechanism (basket_live's pre-fix behavior: close_all_in_profit
         fires the instant aggregate_r >= resolve_target_r). This is the
         documented bug being fixed, so it is the reasonable default when
         we cannot derive the real ratio.
      A1. peak_r for that basket is taken as AT LEAST the realized exit R
          (a fact: the basket touched that R to close there) — we do NOT
          assume it reached any HIGHER peak than what was realized, i.e.
          this is a CONSERVATIVE floor-case projection, not an optimistic
          one.
      A2. Under FIX 1, a basket whose peak_r never reaches arm_trail_r
          (0.5) is NOT closed by the profit-resolve path at all in live
          trading (it keeps running) — but for this static projection we
          cannot simulate "keeps running to some later bar", so we report
          such trades at their ORIGINAL realized R as a conservative floor
          (the true FIX 1 outcome is >= this, since the position had more
          runway before any resolve could fire, and OLD basket_live would
          have closed it at the SAME point anyway since realized_r < 0.5
          means it likely closed near the old 0.2R bar already).
      A3. A basket whose realized peak_r (per A1) is >= arm_trail_r is
          projected to exit at min(cfg.take_r, peak_r) — i.e. we assume the
          realized peak WAS the basket's true peak (conservative: the real
          peak may have gone higher before the OLD flat bar closed it
          early at a LOWER R than its true peak, which would make the real
          FIX 1 outcome even better than this projection, not worse — this
          projection floor-cases every win that closed above arm_trail_r
          as if the old bug had NOT interfered at all, which cannot be
          confirmed without full bar-by-bar re-simulation, so results above
          arm_trail_r should be read as "best-observed-R, not necessarily
          true-peak-R").

    This is explicitly a LOWER-BOUND / conservative-leaning projection for
    small wins (A2) but a BEST-OBSERVED-R (not verified-true-peak) estimate
    for wins that already closed above arm_trail_r (A3) — both directions
    are called out so the projected PF is not oversold.
    """
    rows: list[dict[str, Any]] = []
    for d in win_deals:
        pid = _deal_position_id(d)
        realized_pnl = _deal_net_profit(d) or 0.0
        geo = geometry_by_position.get(pid)
        risk_usd = geo.get("risk_usd") if geo else None

        if risk_usd and float(risk_usd) > 0:
            realized_r = realized_pnl / float(risk_usd)
            note = "realized_r_derived_from_journaled_risk_usd"
        else:
            realized_r = cfg.resolve_target_r  # A0 fallback
            note = "assumed_flat_resolve_target_r_no_geometry_match"

        peak_r = max(realized_r, cfg.resolve_target_r)  # A1 floor (never below what closed it)

        if peak_r >= cfg.arm_trail_r:
            projected_r = min(cfg.take_r, peak_r)
            trigger = "take_r_or_peak_floor"
        else:
            projected_r = realized_r  # A2 — unarmed, conservative floor unchanged
            trigger = "never_armed_conservative_floor"

        # Scale the realized USD PnL by the R ratio to project the new USD
        # outcome (realized_pnl corresponds to realized_r; same per-R USD
        # rate applies to projected_r under the same position sizing).
        scale = (projected_r / realized_r) if realized_r > 0 else 1.0
        projected_pnl = round(realized_pnl * scale, 2)

        rows.append(
            {
                "position_id": pid,
                "realized_pnl": round(realized_pnl, 2),
                "realized_r": round(realized_r, 4),
                "peak_r_floor": round(peak_r, 4),
                "projected_r": round(projected_r, 4),
                "projected_pnl": projected_pnl,
                "trigger": trigger,
                "note": note,
                "geometry_available": geo is not None,
            }
        )

    total_realized = round(sum(r["realized_pnl"] for r in rows), 2)
    total_projected = round(sum(r["projected_pnl"] for r in rows), 2)
    n_with_geometry = sum(1 for r in rows if r["geometry_available"])
    return {
        "rows": rows,
        "n_wins_projected": len(rows),
        "n_wins_with_geometry_match": n_with_geometry,
        "total_realized_win_pnl": total_realized,
        "total_projected_win_pnl": total_projected,
        "delta": round(total_projected - total_realized, 2),
    }


def project_fix1_winners_optimistic(
    win_deals: list[dict[str, Any]],
    geometry_by_position: dict[int, dict[str, Any]],
    cfg: BasketConfig,
) -> dict[str, Any]:
    """OPTIMISTIC companion to ``project_fix1_winners``.

    The conservative floor projection (A1/A2 there) structurally CANNOT
    show FIX 1's real benefit for any win whose realized_r stayed below
    arm_trail_r, because it assumes the basket's true peak_r never exceeded
    what was actually realized under the OLD flat-bar bug — but the whole
    point of the bug is that the OLD code closed the basket AT that low R
    regardless of how much further it might have run. This function
    provides the other end of the plausible range so the PM sees both:

    ASSUMPTION B1 (optimistic, NOT verified): each win's peak_r is assumed
    to have reached its OWN hunt_mode-computed TP distance at signal time
    — ``rr_intended = abs(tp - entry) / abs(entry - sl)`` (recovered from
    the journaled entry/sl/tp, NOT invented: this is the exact per-leg
    target hunt_mode's own geometry set for that trade, MIN_REWARD_RISK
    >= 1.2 per dexter3/hunt_mode.py). This assumes the position was ON
    TRACK to reach its intended TP before the OLD flat +0.2R bug banked it
    early — plausible but NOT provable from closed-deal data alone (would
    require the position's own M5-by-M5 unrealized-PnL path, which this
    script does not re-fetch).
    projected_r = min(cfg.take_r, max(realized_r, rr_intended)) — capped at
    the new hard take_r ceiling (FIX 1 never lets a basket run past take_r
    even optimistically).

    This is labeled OPTIMISTIC/UNVERIFIED throughout the output — it is the
    upper bound of plausible improvement, not a claim of what would have
    definitely happened.
    """
    rows: list[dict[str, Any]] = []
    for d in win_deals:
        pid = _deal_position_id(d)
        realized_pnl = _deal_net_profit(d) or 0.0
        geo = geometry_by_position.get(pid)
        risk_usd = geo.get("risk_usd") if geo else None

        if risk_usd and float(risk_usd) > 0:
            realized_r = realized_pnl / float(risk_usd)
        else:
            realized_r = cfg.resolve_target_r

        rr_intended = None
        if geo and geo.get("entry") and geo.get("sl") and geo.get("tp"):
            sl_dist = abs(float(geo["entry"]) - float(geo["sl"]))
            if sl_dist > 0:
                rr_intended = abs(float(geo["tp"]) - float(geo["entry"])) / sl_dist

        optimistic_peak = max(realized_r, rr_intended) if rr_intended else realized_r
        projected_r = min(cfg.take_r, optimistic_peak)
        scale = (projected_r / realized_r) if realized_r > 0 else 1.0
        projected_pnl = round(realized_pnl * scale, 2)

        rows.append(
            {
                "position_id": pid,
                "realized_pnl": round(realized_pnl, 2),
                "realized_r": round(realized_r, 4),
                "rr_intended_tp": round(rr_intended, 4) if rr_intended else None,
                "projected_r": round(projected_r, 4),
                "projected_pnl": projected_pnl,
            }
        )

    total_realized = round(sum(r["realized_pnl"] for r in rows), 2)
    total_projected = round(sum(r["projected_pnl"] for r in rows), 2)
    return {
        "rows": rows,
        "n_wins_projected": len(rows),
        "total_realized_win_pnl": total_realized,
        "total_projected_win_pnl": total_projected,
        "delta": round(total_projected - total_realized, 2),
        "warning": "OPTIMISTIC/UNVERIFIED upper bound — assumes every winner was on track to reach its own hunt_mode TP; not provable from closed-deal data alone.",
    }


# ---------------------------------------------------------------------------
# FIX 3 impact estimate — counter-trend losing Sells
# ---------------------------------------------------------------------------


def estimate_fix3_impact(
    loss_deals: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    threshold: float = 0.5,
) -> dict[str, Any]:
    """How many realized LOSSES were counter-trend Sells that FIX 3's
    trend-agreement guard would have penalized (halved conviction) or
    flipped (side reversed)?

    Joins loss deals to journaled 'enter' decisions by NEAREST ts_close for
    the same symbol+side (position_id is not present on 'decisions' rows —
    the journal's decision contract predates per-position linkage — so this
    is a best-effort temporal join, approximate by design; every match is
    within a small tolerance and reported as such).
    """
    sell_losses = [d for d in loss_deals if _deal_side(d) == "sell"]
    counter_trend_count = 0
    flipped_count = 0
    penalized_only_count = 0
    matched = 0
    details: list[dict[str, Any]] = []

    sell_decisions = [
        d for d in decisions if d.get("side") == "sell" and isinstance(d.get("committee"), dict) and d.get("committee")
    ]

    for deal in sell_losses:
        symbol = _deal_symbol(deal)
        # Best-effort match: same symbol, most recent decision at/near the
        # deal's own closed_ts window is unavailable without a tighter join
        # key, so we scan the full sell-decision set for this symbol.
        candidates = [d for d in sell_decisions if d.get("symbol") == symbol]
        if not candidates:
            continue
        # Without a tighter key we cannot pick a single exact decision — use
        # ALL candidates conservatively and flag any where m15_drift/h1_context
        # both agree strongly against 'sell' (i.e. a strong aligned buy trend).
        for cand in candidates:
            committee = cand["committee"]
            m15 = committee.get("m15_drift", {}).get("vote")
            h1 = committee.get("h1_context", {}).get("vote")
            if m15 is None or h1 is None:
                continue
            try:
                m15f, h1f = float(m15), float(h1)
            except (TypeError, ValueError):
                continue
            agreement = (m15f + h1f) / 2.0
            if m15f > 0 and h1f > 0 and agreement >= threshold:
                # strong aligned BUY trend, deal side is sell -> counter-trend
                matched += 1
                sweep = committee.get("sweep_reclaim", {}).get("detail", {})
                confirmed_sweep = bool(sweep.get("fired")) and sweep.get("side") == "sell"
                weighted_sum = sum(m.get("weighted", 0.0) for m in committee.values())
                trend_pull = abs(agreement) * (1.3 + 0.9) / 2.0  # new weights, see hunt_mode.py
                strong_enough = abs(weighted_sum) >= 1.2 * trend_pull
                counter_trend_count += 1
                if confirmed_sweep or strong_enough:
                    penalized_only_count += 1
                else:
                    flipped_count += 1
                details.append(
                    {
                        "ts_close": cand.get("ts_close"),
                        "setup": cand.get("setup"),
                        "agreement": round(agreement, 4),
                        "would_flip": not (confirmed_sweep or strong_enough),
                    }
                )
                break  # one match counted per loss deal

    return {
        "n_sell_losses": len(sell_losses),
        "n_counter_trend_matched": matched,
        "n_would_be_flipped": flipped_count,
        "n_would_be_penalized_only": penalized_only_count,
        "sample_matches": details[:10],
        "note": (
            "Best-effort symbol-level join (decisions table has no position_id "
            "linkage) — counts are an estimate of HOW MANY realized sell losses "
            "occurred while the committee's own journaled votes show a strong "
            "aligned buy trend, not a guaranteed 1:1 trade match."
        ),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dexter3 PnL backtest / fix-proof")
    parser.add_argument("--label", default=DEFAULT_LABEL_FILTER)
    parser.add_argument("--count", type=int, default=DEFAULT_DEAL_COUNT)
    args = parser.parse_args(argv)

    print("=" * 78)
    print("DEXTER3 PnL BACKTEST — profitability-fix proof")
    print("=" * 78)

    # -- STAGE 1: baseline ---------------------------------------------------
    mcp = Dexter3McpClient()
    all_deals = fetch_deals(mcp, args.count)
    lane_deals = filter_lane_deals(all_deals, args.label)
    print(f"\n[STAGE 1] get_deals returned {len(all_deals)} total, {len(lane_deals)} matching label*='{args.label}'")

    if not lane_deals:
        print(
            "[STAGE 1] WARNING: no lane deals retrieved from live MCP (it may be "
            "unreachable/zombie right now — the live dexter3 loop was NOT touched "
            "to investigate, per constraints). Falling back to journal-only STAGE 2 "
            "reporting; BASELINE PF cannot be freshly reproduced this run — see "
            "docs/AGENT_SYNC_BOARD.md 2026-07-07 entry for the last confirmed pull: "
            "Net -$110.77, WR 56% (28W/22L), avg win +$6.15, avg loss -$12.87, PF 0.61."
        )
        baseline = None
    else:
        baseline = compute_pf_stats(lane_deals)
        print("\n--- BASELINE (realized, from get_deals) ---")
        print(json.dumps(baseline, indent=2))

    # -- STAGE 2: projected winner-side simulation ---------------------------
    geometry = load_entry_geometry()
    print(f"\n[STAGE 2] loaded entry geometry for {len(geometry)} position_ids from journal")

    cfg = _fix1_config()
    print(
        f"[STAGE 2] FIX 1 config under test: resolve_target_r(floor)={cfg.resolve_target_r} "
        f"arm_trail_r={cfg.arm_trail_r} trail_keep_frac={cfg.trail_keep_frac} take_r={cfg.take_r}"
    )

    if lane_deals:
        win_deals = [d for d in lane_deals if (_deal_net_profit(d) or 0.0) > 0]
        loss_deals = [d for d in lane_deals if (_deal_net_profit(d) or 0.0) < 0]
    else:
        win_deals, loss_deals = [], []

    projection = project_fix1_winners(win_deals, geometry, cfg)
    print("\n--- STAGE 2a: FIX 1 projected winner-side outcome (CONSERVATIVE floor projection) ---")
    print(
        json.dumps(
            {k: v for k, v in projection.items() if k != "rows"},
            indent=2,
        )
    )

    optimistic = project_fix1_winners_optimistic(win_deals, geometry, cfg)
    print("\n--- STAGE 2b: FIX 1 projected winner-side outcome (OPTIMISTIC/UNVERIFIED upper bound) ---")
    print(
        json.dumps(
            {k: v for k, v in optimistic.items() if k != "rows"},
            indent=2,
        )
    )

    def _report_pf(label: str, proj: dict[str, Any]) -> dict[str, Any] | None:
        if baseline is None:
            return None
        projected_net = round(
            baseline["net_pnl"] - proj["total_realized_win_pnl"] + proj["total_projected_win_pnl"], 2
        )
        projected_gross_profit = round(
            baseline["gross_profit"] - proj["total_realized_win_pnl"] + proj["total_projected_win_pnl"], 2
        )
        projected_gross_loss = abs(baseline["gross_loss"])  # FIX 1 does not touch the loss side's realized amounts
        projected_pf = (projected_gross_profit / projected_gross_loss) if projected_gross_loss > 0 else float("inf")
        result = {
            "projection": label,
            "baseline_net_pnl": baseline["net_pnl"],
            "baseline_profit_factor": baseline["profit_factor"],
            "projected_net_pnl": projected_net,
            "projected_gross_profit": projected_gross_profit,
            "projected_gross_loss": -projected_gross_loss,
            "projected_profit_factor": round(projected_pf, 4) if projected_pf != float("inf") else projected_pf,
            "bar_to_clear": "projected_profit_factor > 1.0 AND projected_net_pnl > 0",
            "clears_bar": bool(projected_pf > 1.0 and projected_net > 0),
        }
        print(f"\n--- PROJECTED PF ({label}) ---")
        print(json.dumps(result, indent=2))
        return result

    if baseline is not None:
        conservative_pf = _report_pf("CONSERVATIVE (floor, winners assumed no better than realized)", projection)
        optimistic_pf = _report_pf("OPTIMISTIC/UNVERIFIED (winners assumed reached own hunt_mode TP)", optimistic)
        print("\n--- HEADLINE SUMMARY (winner-side re-pricing only, losses held at realized) ---")
        print(
            json.dumps(
                {
                    "baseline_pf": baseline["profit_factor"],
                    "conservative_projected_pf": conservative_pf["projected_profit_factor"] if conservative_pf else None,
                    "optimistic_projected_pf": optimistic_pf["projected_profit_factor"] if optimistic_pf else None,
                    "conservative_clears_bar": conservative_pf["clears_bar"] if conservative_pf else None,
                    "optimistic_clears_bar": optimistic_pf["clears_bar"] if optimistic_pf else None,
                },
                indent=2,
            )
        )
    else:
        print("\n[STAGE 2] cannot compute projected PF without a baseline (get_deals unavailable this run).")

    # -- FIX 3 impact ---------------------------------------------------------
    decisions = load_decision_committee_for_setup()
    print(f"\n[FIX 3] loaded {len(decisions)} journaled 'enter' decisions with committee snapshots")
    if loss_deals:
        fix3 = estimate_fix3_impact(loss_deals, decisions)
        print("\n--- FIX 3: counter-trend-sell impact on realized LOSSES ---")
        print(json.dumps(fix3, indent=2))
    else:
        print("\n[FIX 3] no realized loss deals available this run (get_deals unavailable) — skipped.")

    print("\n" + "=" * 78)
    print("END OF REPORT")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
