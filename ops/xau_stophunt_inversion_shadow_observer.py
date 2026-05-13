#!/usr/bin/env python3
"""Live shadow observer for mined XAU stop-hunt inversion diamonds.

This module never sends orders. It watches recent closed losing positions and
emits observe-only candidates when they match statistically mined inversion
clusters.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ops.xau_mistake_diamond_miner import _direction, _parse_dt, load_positions
from ops.xau_winner_archetype_report import classify_family, session_bucket


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _loss_key(loss: dict, bucket_level: str) -> list[str]:
    family = str(loss.get("family") or "unknown")
    session = str(loss.get("session") or "unknown")
    direction = str(loss.get("direction") or "unknown")
    if bucket_level == "family_session_dir":
        return [family, session, direction]
    if bucket_level == "family_dir":
        return [family, direction]
    if bucket_level == "session_dir":
        return [session, direction]
    if bucket_level == "family_session":
        return [family, session]
    if bucket_level == "family":
        return [family]
    if bucket_level == "direction":
        return [direction]
    return [family, session, direction]


def _loss_matches_cluster(loss: dict, cluster: dict) -> bool:
    if not cluster.get("phase_a_shadow_candidate"):
        return False
    return _loss_key(loss, str(cluster.get("bucket_level") or "family_session_dir")) == list(cluster.get("bucket_key") or [])


def _loss_diagnostic(loss: dict, diamonds: list[dict], current: datetime) -> dict:
    """Explain why a recent loss did or did not match a mined diamond."""
    closed = _parse_dt(loss.get("close_utc")) or _parse_dt(loss.get("last_seen_utc"))
    age_min = None if closed is None else (current - closed).total_seconds() / 60.0
    checked: list[dict] = []
    for cluster in diamonds:
        bucket_level = str(cluster.get("bucket_level") or "family_session_dir")
        window = int(cluster.get("window_minutes") or 0)
        have = _loss_key(loss, bucket_level)
        need = list(cluster.get("bucket_key") or [])
        checked.append(
            {
                "cluster_id": cluster.get("cluster_id"),
                "bucket_level": bucket_level,
                "loss_key": have,
                "cluster_key": need,
                "window_minutes": window,
                "within_window": bool(age_min is not None and age_min >= 0 and window > 0 and age_min <= window),
                "bucket_match": have == need,
                "samples": cluster.get("samples"),
                "profit_factor": cluster.get("profit_factor"),
            }
        )
    return {
        "position_id": loss.get("position_id"),
        "family": loss.get("family"),
        "direction": loss.get("direction"),
        "session": loss.get("session"),
        "pnl_usd": loss.get("pnl_usd"),
        "close_utc": loss.get("close_utc") or loss.get("last_seen_utc"),
        "age_minutes": None if age_min is None else round(age_min, 2),
        "matched": any(bool(item.get("within_window")) and bool(item.get("bucket_match")) for item in checked),
        "checked_diamonds": checked,
    }


def build_shadow_observations(recent_losses: list[dict], diamonds: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Build observe-only inversion candidates from recent losses and mined diamonds."""
    current = now or _now()
    observations: list[dict] = []
    for loss in recent_losses:
        closed = _parse_dt(loss.get("close_utc")) or _parse_dt(loss.get("last_seen_utc"))
        if closed is None:
            continue
        age_min = (current - closed).total_seconds() / 60.0
        if age_min < 0:
            continue
        for cluster in diamonds:
            window = int(cluster.get("window_minutes") or 0)
            if window <= 0 or age_min > window:
                continue
            if not _loss_matches_cluster(loss, cluster):
                continue
            observations.append(
                {
                    "family": "xau_stophunt_inversion_shadow",
                    "phase": "A_observe_only",
                    "execution_enabled": False,
                    "live_enabled": False,
                    "symbol": "XAUUSD",
                    "direction": cluster.get("theoretical_direction"),
                    "matched_position_id": loss.get("position_id"),
                    "matched_loss_family": loss.get("family"),
                    "matched_loss_direction": loss.get("direction"),
                    "matched_loss_session": loss.get("session"),
                    "matched_cluster_id": cluster.get("cluster_id"),
                    "bucket_level": cluster.get("bucket_level"),
                    "window_minutes": window,
                    "age_minutes": round(age_min, 2),
                    "samples": cluster.get("samples"),
                    "profit_factor": cluster.get("profit_factor"),
                    "winrate": cluster.get("winrate"),
                    "inversion_net_usd": cluster.get("inversion_net_usd"),
                    "created_utc": current.isoformat().replace("+00:00", "Z"),
                    "block_reason": "xau_stophunt_inversion_shadow:observe_only",
                }
            )
            break
    return observations


def build_loss_diagnostics(recent_losses: list[dict], diamonds: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Return compact explainability for recent losses that did not emit observations."""
    current = now or _now()
    return [_loss_diagnostic(loss, diamonds, current) for loss in recent_losses]


def load_recent_losses(db_path: Path, symbol: str = "XAUUSD", lookback_minutes: int = 90) -> list[dict]:
    cutoff = (_now() - timedelta(minutes=max(1, int(lookback_minutes)))).isoformat().replace("+00:00", "Z")
    conn = connect_ro(db_path)
    try:
        rows = conn.execute(
            """
            WITH pos_pnl AS (
                SELECT p.position_id,
                       p.direction,
                       p.label,
                       p.comment,
                       p.source,
                       p.first_seen_utc,
                       p.last_seen_utc,
                       SUM(COALESCE(d.pnl_usd, 0.0)) AS pnl_usd,
                       MAX(d.execution_utc) AS close_utc
                  FROM ctrader_positions p
                  LEFT JOIN ctrader_deals d ON d.position_id = p.position_id
                 WHERE (p.symbol LIKE ? OR p.broker_symbol LIKE ?)
                 GROUP BY p.position_id
            )
            SELECT * FROM pos_pnl
             WHERE pnl_usd < 0.0
               AND COALESCE(close_utc, last_seen_utc, '') >= ?
             ORDER BY COALESCE(close_utc, last_seen_utc) DESC
            """,
            (f"%{symbol}%", f"%{symbol}%", cutoff),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        item = dict(row)
        item["family"] = classify_family(str(item.get("label") or ""), str(item.get("comment") or ""))
        item["direction"] = _direction(item.get("direction"))
        item["session"] = session_bucket(item.get("first_seen_utc"))
        item["pnl_usd"] = float(item.get("pnl_usd") or 0.0)
        out.append(item)
    return out


def load_diamonds(report_path: Path) -> list[dict]:
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        report = {"diamonds": []}
    return list(report.get("diamonds") or [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/ctrader_openapi.db")
    ap.add_argument("--diamond-report", default="data/reports/mistake_clusters_report.json")
    ap.add_argument("--out", default="data/reports/stophunt_inversion_shadow_live.json")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--lookback-minutes", type=int, default=90)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    losses = load_recent_losses(Path(args.db), symbol=args.symbol, lookback_minutes=args.lookback_minutes)
    diamonds = load_diamonds(Path(args.diamond_report))
    observations = build_shadow_observations(losses, diamonds)
    diagnostics = build_loss_diagnostics(losses, diamonds)
    report = {
        "family": "xau_stophunt_inversion_shadow",
        "phase": "A_observe_only",
        "execution_enabled": False,
        "live_enabled": False,
        "recent_losses": len(losses),
        "diamonds_loaded": len(diamonds),
        "observations": observations,
        "loss_diagnostics": diagnostics,
        "created_utc": _now().isoformat().replace("+00:00", "Z"),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"xau_stophunt_inversion_shadow observations={len(observations)} recent_losses={len(losses)} out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
