"""XAU Guardian v2 — exposure governor / loss-entropy brake.

This is the stop-bleed complement to the Profit Reservoir guardian.  Reservoir v1
protects positive basket peaks; this governor prevents opportunity-first XAU from
adding the same side repeatedly when live/demo evidence says the current lineage
is adverse.

Direction sanity rule: realized attribution uses joined/opening direction only.
Never use ctrader_deals.direction alone as realized direction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Optional


def _sf(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return float(default)
        return v
    except Exception:
        return float(default)


def _si(value: Any, default: int = 0) -> int:
    try:
        return int(float(value if value is not None else default))
    except Exception:
        return int(default)


def _direction(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"long", "buy", "trade_side_buy", "proto_oa_trade_side_buy"}:
        return "long"
    if token in {"short", "sell", "trade_side_sell", "proto_oa_trade_side_sell"}:
        return "short"
    return "unknown"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sl_distance_entropy(distances: list[float], *, bins: int = 5) -> float:
    vals = [abs(float(v)) for v in distances if float(v) >= 0]
    if len(vals) <= 1:
        return 0.0
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return 0.0
    buckets = [0 for _ in range(max(2, bins))]
    for v in vals:
        idx = int((v - lo) / max(1e-9, hi - lo) * (len(buckets) - 1))
        buckets[max(0, min(len(buckets) - 1, idx))] += 1
    total = float(sum(buckets) or 1)
    h = 0.0
    for c in buckets:
        if c <= 0:
            continue
        p = c / total
        h -= p * math.log(p)
    return round(h, 4)


@dataclass
class GovernorConfig:
    enabled: bool = True
    mode: str = "live"  # shadow|live|off
    runtime_path: str = "data/runtime/xau_governor_state.json"
    min_same_side: int = 5
    entropy_nats: float = 0.5
    velocity_threshold_usd_per_min: float = -8.0
    freeze_min: int = 15
    base_long_usd: float = 30.0
    base_short_usd: float = 30.0
    refill_ratio: float = 0.4
    lineage_window_sec: int = 90
    loss_streak: int = 2
    cooldown_min: int = 15
    stale_tick_max_age_sec: int = 600
    kill_if_state_stale_sec: int = 90
    dryrun: bool = False

    @classmethod
    def from_config(cls, config: Any) -> "GovernorConfig":
        return cls(
            enabled=bool(getattr(config, "XAU_GOVERNOR_V2_ENABLED", True)),
            mode=str(getattr(config, "XAU_GOVERNOR_V2_MODE", "live") or "live").strip().lower(),
            runtime_path=str(getattr(config, "XAU_GOVERNOR_V2_RUNTIME_PATH", "data/runtime/xau_governor_state.json") or "data/runtime/xau_governor_state.json"),
            min_same_side=max(1, _si(getattr(config, "XAU_GOV_G1_MIN_SAMESIDE", 5), 5)),
            entropy_nats=_sf(getattr(config, "XAU_GOV_G1_ENTROPY_NATS", 0.5), 0.5),
            velocity_threshold_usd_per_min=_sf(getattr(config, "XAU_GOV_G1_VELOCITY_THRESHOLD_USD_PER_MIN", -8.0), -8.0),
            freeze_min=max(1, _si(getattr(config, "XAU_GOV_G1_FREEZE_MIN", 15), 15)),
            base_long_usd=_sf(getattr(config, "XAU_GOV_G2_BASE_LONG_USD", 30.0), 30.0),
            base_short_usd=_sf(getattr(config, "XAU_GOV_G2_BASE_SHORT_USD", 30.0), 30.0),
            refill_ratio=_sf(getattr(config, "XAU_GOV_G2_REFILL_RATIO", 0.4), 0.4),
            lineage_window_sec=max(30, _si(getattr(config, "XAU_GOV_G4_LINEAGE_WINDOW_SEC", 90), 90)),
            loss_streak=max(1, _si(getattr(config, "XAU_GOV_G4_LOSS_STREAK", 2), 2)),
            cooldown_min=max(1, _si(getattr(config, "XAU_GOV_G4_COOLDOWN_MIN", 15), 15)),
            stale_tick_max_age_sec=max(60, _si(getattr(config, "XAU_GOV_STALE_TICK_MAX_AGE_SEC", getattr(config, "XAU_GUARDIAN_STALE_TICK_MAX_AGE_SEC", 600)), 600)),
            kill_if_state_stale_sec=max(30, _si(getattr(config, "XAU_GOV_KILL_IF_STATE_STALE_SEC", 90), 90)),
            dryrun=bool(getattr(config, "XAU_GUARDIAN_V2_DRYRUN", False)),
        )


@dataclass
class GovernorVerdict:
    allowed: bool
    reason: str = "allowed"
    mode: str = "live"
    side: str = "unknown"
    family: str = ""
    dryrun: bool = False
    guards: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class XAUExposureGovernor:
    def __init__(self, db_path: str | Path, config: GovernorConfig):
        self.db_path = Path(db_path)
        self.config = config
        self.runtime_path = Path(config.runtime_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
        except Exception:
            pass
        return conn

    @staticmethod
    def _source_family(source: str) -> str:
        token = str(source or "").strip().lower().split(":", 1)[0]
        return token or "unknown"

    def _write_state(self, verdict: GovernorVerdict) -> None:
        try:
            self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
            self.runtime_path.write_text(json.dumps({"generated_utc": utc_now_iso(), "verdict": verdict.to_dict()}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _latest_mid(self, conn: sqlite3.Connection) -> tuple[float, Optional[str], float]:
        row = conn.execute("SELECT bid,ask,event_utc FROM ctrader_spot_ticks WHERE symbol='XAUUSD' ORDER BY event_utc DESC,id DESC LIMIT 1").fetchone()
        if row is None:
            return 0.0, None, 10**9
        mid = (_sf(row["bid"]) + _sf(row["ask"])) / 2.0
        age = 10**9
        try:
            ts = datetime.fromisoformat(str(row["event_utc"]).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:
            pass
        return mid, str(row["event_utc"] or ""), age

    def _open_positions(self, conn: sqlite3.Connection, side: str, mid: float) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT position_id,source,direction,volume,entry_price,stop_loss,take_profit,first_seen_utc,last_seen_utc
              FROM ctrader_positions
             WHERE symbol='XAUUSD' AND COALESCE(is_open,0)=1 AND lower(COALESCE(direction,''))=?
             ORDER BY first_seen_utc ASC, position_id ASC
            """,
            (side,),
        ).fetchall()
        out = []
        for row in rows:
            p = dict(row)
            entry = _sf(p.get("entry_price")); vol = _sf(p.get("volume")); sl = _sf(p.get("stop_loss"))
            pnl = ((mid - entry) if side == "long" else (entry - mid)) * vol / 100.0 if mid > 0 and entry > 0 else 0.0
            p["est_pnl_usd"] = round(pnl, 2)
            p["sl_distance"] = abs(entry - sl) if sl > 0 and entry > 0 else 0.0
            out.append(p)
        return out

    def _basket_velocity(self, conn: sqlite3.Connection) -> float:
        rows = conn.execute(
            "SELECT event_utc,combined_pnl FROM xau_basket_snapshots ORDER BY id DESC LIMIT 6"
        ).fetchall() if self._table_exists(conn, "xau_basket_snapshots") else []
        if len(rows) < 2:
            return 0.0
        newest, oldest = rows[0], rows[-1]
        try:
            t_new = datetime.fromisoformat(str(newest["event_utc"]).replace("Z", "+00:00"))
            t_old = datetime.fromisoformat(str(oldest["event_utc"]).replace("Z", "+00:00"))
            mins = max(0.1, (t_new - t_old).total_seconds() / 60.0)
            return round((_sf(newest["combined_pnl"]) - _sf(oldest["combined_pnl"])) / mins, 4)
        except Exception:
            return 0.0

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def _realized_dir_pnl(self, conn: sqlite3.Connection, side: str, hours: float) -> float:
        # Correct direction attribution: joined position direction only.
        row = conn.execute(
            """
            SELECT SUM(COALESCE(d.pnl_usd,0)) AS pnl
              FROM ctrader_deals d
              LEFT JOIN ctrader_positions p ON p.position_id=d.position_id
             WHERE d.symbol='XAUUSD'
               AND lower(COALESCE(p.direction,''))=?
               AND d.execution_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)
            """,
            (side, f"-{float(hours):.3f} hours"),
        ).fetchone()
        return _sf(row["pnl"] if row else 0.0)

    def _recent_loss_streak(self, conn: sqlite3.Connection, side: str, family: str) -> int:
        rows = conn.execute(
            """
            SELECT COALESCE(d.pnl_usd,0) AS pnl
              FROM ctrader_deals d
              LEFT JOIN ctrader_positions p ON p.position_id=d.position_id
             WHERE d.symbol='XAUUSD'
               AND lower(COALESCE(p.direction,''))=?
               AND lower(COALESCE(d.source,'')) LIKE ?
               AND d.execution_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)
             ORDER BY d.execution_utc DESC, d.deal_id DESC
             LIMIT 8
            """,
            (side, f"{family}%", f"-{self.config.cooldown_min * 2} minutes"),
        ).fetchall()
        streak = 0
        for row in rows:
            if _sf(row["pnl"]) < 0:
                streak += 1
            else:
                break
        return streak

    def allow_entry(self, *, symbol: str, direction: str, source: str, confidence: float = 0.0) -> GovernorVerdict:
        side = _direction(direction)
        family = self._source_family(source)
        if not self.config.enabled or self.config.mode == "off":
            return GovernorVerdict(True, "governor_disabled", mode=self.config.mode, side=side, family=family)
        if str(symbol or "").strip().upper() != "XAUUSD" or side not in {"long", "short"}:
            return GovernorVerdict(True, "non_xau_or_unknown_side", mode=self.config.mode, side=side, family=family)
        guards: list[str] = []
        metrics: dict[str, Any] = {"confidence": _sf(confidence), "source_family": family}
        try:
            with self._connect() as conn:
                mid, tick_utc, tick_age = self._latest_mid(conn)
                metrics.update({"mid": round(mid, 5), "tick_utc": tick_utc, "tick_age_sec": round(tick_age, 1)})
                if tick_age > self.config.stale_tick_max_age_sec:
                    guards.append("stale_tick_entry_freeze")
                positions = self._open_positions(conn, side, mid)
                same_side_count = len(positions)
                same_side_pnl = round(sum(_sf(p.get("est_pnl_usd")) for p in positions), 2)
                entropy = sl_distance_entropy([_sf(p.get("sl_distance")) for p in positions])
                velocity = self._basket_velocity(conn)
                realized_24h = self._realized_dir_pnl(conn, side, 24.0)
                budget = (self.config.base_long_usd if side == "long" else self.config.base_short_usd) + self.config.refill_ratio * max(0.0, realized_24h)
                loss_streak = self._recent_loss_streak(conn, side, family)
                metrics.update({
                    "same_side_count": same_side_count,
                    "same_side_pnl": same_side_pnl,
                    "sl_entropy_nats": entropy,
                    "basket_velocity_usd_per_min": velocity,
                    "realized_24h_opening_direction_pnl": round(realized_24h, 2),
                    "exposure_budget_usd": round(budget, 2),
                    "loss_streak_family_side": loss_streak,
                })
                if same_side_count >= self.config.min_same_side and same_side_pnl < 0 and velocity <= self.config.velocity_threshold_usd_per_min:
                    # Entropy is noisy when SLs are extremely wide, so the adverse velocity + crowding is enough;
                    # entropy below threshold upgrades reason, not required for first brake.
                    guards.append("G1_loss_entropy_freeze" if entropy <= self.config.entropy_nats else "G1_adverse_crowding_freeze")
                if same_side_count >= self.config.min_same_side and abs(same_side_pnl) >= budget and same_side_pnl < 0:
                    guards.append("G2_exposure_budget_freeze")
                if loss_streak >= self.config.loss_streak:
                    guards.append("G4_lineage_cooldown")
        except Exception as exc:
            guards.append("governor_error_fail_safe")
            metrics["error"] = f"{type(exc).__name__}:{exc}"
        allowed = not guards
        reason = "allowed" if allowed else ";".join(guards)
        dryrun = bool(self.config.dryrun or self.config.mode == "shadow")
        verdict = GovernorVerdict(
            allowed=(allowed or dryrun),
            reason=("dryrun:" + reason if (not allowed and dryrun) else reason),
            mode=self.config.mode,
            side=side,
            family=family,
            dryrun=dryrun,
            guards=guards,
            metrics=metrics,
        )
        self._write_state(verdict)
        return verdict
