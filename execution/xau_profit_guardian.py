"""XAU Profit Reservoir / Basket Guardian.

Post-fill position-management intelligence for Dexter's XAU opportunity-first
policy.  This module deliberately keeps entry dispatch untouched: it observes
broker truth, remembers basket/equity peaks, classifies trend phase, and emits
PM directives that can run in shadow or staged live modes.

Direction sanity rule: never use ``ctrader_deals.direction`` alone for realized
Buy/Sell attribution.  It can be the deal/closing side.  Use statement Opening
direction or joined ``ctrader_positions.direction``; otherwise label unknown.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional


TRUTH_DIRECTION_SOURCES = {"statement_opening_direction", "ctrader_positions.direction"}


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
        return int(float(value or default))
    except Exception:
        return int(default)


def _direction(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"buy", "long", "trade_side_buy", "proto_oa_trade_side_buy"}:
        return "long"
    if token in {"sell", "short", "trade_side_sell", "proto_oa_trade_side_sell"}:
        return "short"
    return "unknown"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_csv_set(value: Any) -> set[str]:
    if isinstance(value, (set, list, tuple)):
        items = value
    else:
        items = str(value or "").replace("|", ",").split(",")
    return {str(x).strip().lower() for x in items if str(x).strip()}


def _utc_hour_token(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "h_unknown"
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"h{dt.astimezone(timezone.utc).hour:02d}"
    except Exception:
        return "h_unknown"


def direction_from_deal_and_position(deal_row: Optional[dict], position_row: Optional[dict]) -> str:
    """Return realized opening direction without trusting deal.direction alone."""
    pos = dict(position_row or {})
    for key in ("opening_direction", "position_direction", "direction"):
        d = _direction(pos.get(key))
        if d in {"long", "short"}:
            return d
    # The deal row is intentionally not used as a fallback; using it alone was
    # the analysis bug that inverted profitable opening Buy trades.
    return "unknown"


@dataclass
class CoherenceResult:
    ok: bool
    reason: str = "ok"
    rr: float = 0.0
    tp_atr: float = 0.0
    sl_atr: float = 0.0


def validate_tp_sl_coherence(
    *,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    atr: float,
    min_tp_atr: float = 0.3,
    max_tp_atr: float = 8.0,
    min_sl_atr: float = 0.2,
    max_sl_atr: float = 4.0,
    min_rr: float = 0.5,
    max_rr: float = 10.0,
) -> CoherenceResult:
    side = _direction(direction)
    entry_f = _sf(entry)
    sl = _sf(stop_loss)
    tp = _sf(take_profit)
    atr_f = max(0.01, _sf(atr, 0.0))
    if side not in {"long", "short"} or entry_f <= 0 or sl <= 0 or tp <= 0:
        return CoherenceResult(False, "missing_geometry")
    if side == "long" and not (sl < entry_f < tp):
        return CoherenceResult(False, "direction_inconsistent_geometry")
    if side == "short" and not (tp < entry_f < sl):
        return CoherenceResult(False, "direction_inconsistent_geometry")
    risk = abs(entry_f - sl)
    reward = abs(tp - entry_f)
    rr = reward / max(risk, 1e-9)
    tp_atr = reward / atr_f
    sl_atr = risk / atr_f
    if tp_atr < min_tp_atr or tp_atr > max_tp_atr:
        return CoherenceResult(False, "tp_distance_atr_out_of_range", round(rr, 4), round(tp_atr, 4), round(sl_atr, 4))
    if sl_atr < min_sl_atr or sl_atr > max_sl_atr:
        return CoherenceResult(False, "sl_distance_atr_out_of_range", round(rr, 4), round(tp_atr, 4), round(sl_atr, 4))
    if rr < min_rr or rr > max_rr:
        return CoherenceResult(False, "rr_out_of_range", round(rr, 4), round(tp_atr, 4), round(sl_atr, 4))
    return CoherenceResult(True, "ok", round(rr, 4), round(tp_atr, 4), round(sl_atr, 4))


def classify_trend_phase(features: dict[str, Any]) -> str:
    streak = _sf(features.get("hh_hl_streak"), 0.0)
    delta = _sf(features.get("delta_slope"), 0.0)
    atr_jump = _sf(features.get("atr_jump_percentile"), 0.0)
    volume_z = _sf(features.get("volume_z"), 0.0)
    swing_break = bool(features.get("swing_break", False))
    if swing_break and delta < -0.2:
        return "REVERSAL"
    if streak >= 3 and delta > 0.25 and volume_z > -0.3 and atr_jump < 0.85:
        return "IMPULSE"
    if streak >= 2 and delta >= -0.1:
        return "CONTINUATION"
    if streak >= 3 and delta < -0.25 and volume_z < -0.5:
        return "DISTRIBUTION"
    if delta < -0.4 or atr_jump > 0.9:
        return "EXHAUSTION"
    return "NEUTRAL"


def hazard_score(features: dict[str, Any]) -> float:
    score = 0.0
    if _sf(features.get("atr_jump_percentile"), 0.0) >= 0.95:
        score += 30.0
    if bool(features.get("delta_flip")) or abs(_sf(features.get("delta_slope"), 0.0)) >= 0.9:
        score += 25.0
    if bool(features.get("swing_break")):
        score += 20.0
    if bool(features.get("news_window")):
        score += 15.0
    if abs(_sf(features.get("dom_imbalance_flip"), 0.0)) >= 0.4:
        score += 10.0
    return min(100.0, score)


def snowball_capital_multiplier(*, realized_today: float, combined_peak: float, hazard_score_value: float, regime_quality: float = 0.5) -> float:
    """Convert solidified profit into capped future fuel, never euphoria sizing.

    This is the allocator scaffold from the Opus design: only realized/locked
    profit can increase future risk; high hazard suppresses the multiplier.
    Runtime entry sizing is not changed by this module yet — the multiplier is
    persisted for audit and later controlled rollout.
    """
    realized = max(0.0, _sf(realized_today))
    peak = max(1.0, _sf(combined_peak, realized))
    reservoir = min(1.0, realized / peak)
    quality = min(1.0, max(0.0, _sf(regime_quality, 0.5)))
    hazard_drag = max(0.0, 1.0 - (_sf(hazard_score_value) / 100.0))
    return round(min(1.35, max(0.25, 1.0 + 0.35 * reservoir * quality * hazard_drag)), 4)


@dataclass
class GuardianConfig:
    mode: str = "shadow"  # off|shadow|micro_live|half_live|full
    base_giveback_pct: float = 0.10
    hard_giveback_pct: float = 0.25
    max_prune_positions: int = 2
    max_actions_per_5min: int = 3
    min_atr: float = 0.01
    runner_preserve_r: float = 1.5
    stale_tick_max_age_sec: int = 120
    blind_sell_trend_threshold: float = 70.0
    micro_live_allow_schema_cancel: bool = True
    micro_live_allow_weak_prune: bool = True
    half_live_allow_partial_harvest: bool = True
    full_live_allow_harvest: bool = True
    winner_long_reservoir_enabled: bool = True
    winner_long_reservoir_hours: tuple[str, ...] = ("h12", "h20", "h22")
    winner_long_reservoir_source: str = "scalp_xauusd:winner"
    winner_long_reservoir_min_r: float = -0.25

    @classmethod
    def from_config(cls, config: Any) -> "GuardianConfig":
        return cls(
            mode=str(getattr(config, "XAU_GUARDIAN_MODE", "shadow") or "shadow").strip().lower(),
            base_giveback_pct=_sf(getattr(config, "XAU_GUARDIAN_BASE_GIVEBACK_PCT", 0.10), 0.10),
            hard_giveback_pct=_sf(getattr(config, "XAU_GUARDIAN_HARD_GIVEBACK_PCT", 0.25), 0.25),
            max_prune_positions=max(1, _si(getattr(config, "XAU_GUARDIAN_MAX_PRUNE_POSITIONS", 2), 2)),
            max_actions_per_5min=max(1, _si(getattr(config, "XAU_GUARDIAN_MAX_ACTIONS_PER_5MIN", 3), 3)),
            runner_preserve_r=_sf(getattr(config, "XAU_GUARDIAN_RUNNER_PRESERVE_R", 1.5), 1.5),
            stale_tick_max_age_sec=max(30, _si(getattr(config, "XAU_GUARDIAN_STALE_TICK_MAX_AGE_SEC", 120), 120)),
            winner_long_reservoir_enabled=bool(getattr(config, "XAU_WINNER_LONG_RESERVOIR_PM_ENABLED", True)),
            winner_long_reservoir_hours=tuple(
                sorted(f"h{int(x):02d}" if str(x).strip().isdigit() else str(x).strip().lower() for x in _parse_csv_set(getattr(config, "XAU_WINNER_LONG_RESERVOIR_PM_HOURS", "12,20,22")))
            ),
            winner_long_reservoir_source=str(getattr(config, "XAU_WINNER_LONG_RESERVOIR_PM_SOURCE", "scalp_xauusd:winner") or "scalp_xauusd:winner").strip().lower(),
            winner_long_reservoir_min_r=_sf(getattr(config, "XAU_WINNER_LONG_RESERVOIR_PM_MIN_R", -0.25), -0.25),
            # Operator-driven kill-switches for over-aggressive PM pruning
            # (lesson 2026-05-18: weak_add_prune closed 8+ trades at -$3 to
            # -$15 each before they had time to develop MFE).
            micro_live_allow_weak_prune=bool(getattr(config, "XAU_GUARDIAN_MICRO_LIVE_ALLOW_WEAK_PRUNE", True)),
            half_live_allow_partial_harvest=bool(getattr(config, "XAU_GUARDIAN_HALF_LIVE_ALLOW_PARTIAL_HARVEST", True)),
            full_live_allow_harvest=bool(getattr(config, "XAU_GUARDIAN_FULL_LIVE_ALLOW_HARVEST", True)),
        )

    @property
    def live_enabled(self) -> bool:
        return self.mode in {"micro_live", "half_live", "full"}


@dataclass
class PositionState:
    position_id: int
    direction: str
    entry: float
    current: float
    stop_loss: float
    take_profit: float
    volume: float
    source: str = ""
    first_seen_utc: str = ""
    mfe: float = 0.0
    mae: float = 0.0
    age_sec: float = 0.0
    phase: str = "NEUTRAL"
    phase_confidence: float = 0.0
    unrealized_usd: float = 0.0

    def risk_points(self) -> float:
        return abs(_sf(self.entry) - _sf(self.stop_loss)) if _sf(self.stop_loss) > 0 else 0.0

    def r_now(self) -> float:
        risk = self.risk_points()
        if risk <= 0:
            return 0.0
        points = (_sf(self.current) - _sf(self.entry)) if _direction(self.direction) == "long" else (_sf(self.entry) - _sf(self.current))
        return points / risk

    def weakness_score(self, basket_phase: str = "NEUTRAL") -> float:
        risk = max(self.risk_points(), 0.01)
        alignment_penalty = 0.0
        if _direction(self.direction) == "long" and basket_phase in {"DISTRIBUTION", "EXHAUSTION", "REVERSAL"}:
            alignment_penalty = 1.0
        if _direction(self.direction) == "short" and basket_phase in {"IMPULSE", "CONTINUATION"}:
            alignment_penalty = 1.0
        distance_sl = abs(_sf(self.current) - _sf(self.stop_loss)) / risk if _sf(self.stop_loss) > 0 else 2.0
        return (
            1.5 * (max(0.0, _sf(self.mae)) / risk)
            + 0.6 * min(3.0, max(0.0, _sf(self.age_sec)) / 1800.0)
            + 2.0 * alignment_penalty
            + 0.8 * max(0.0, 1.0 - distance_sl)
            - 1.2 * (max(0.0, _sf(self.mfe)) / risk)
        )

    def opened_hour(self) -> str:
        return _utc_hour_token(self.first_seen_utc)

    def is_winner_long_reservoir(self, cfg: GuardianConfig) -> bool:
        if not cfg.winner_long_reservoir_enabled:
            return False
        if _direction(self.direction) != "long":
            return False
        if str(self.source or "").strip().lower() != cfg.winner_long_reservoir_source:
            return False
        if self.opened_hour() not in set(cfg.winner_long_reservoir_hours):
            return False
        # Permission is not a rescue. If already materially adverse, normal PM
        # can prune. This never changes entry size/risk.
        return self.r_now() >= cfg.winner_long_reservoir_min_r


@dataclass
class OrderState:
    order_id: int
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    volume: float
    source: str = ""


@dataclass
class BasketState:
    symbol: str
    realized_today: float = 0.0
    unrealized_now: float = 0.0
    combined_pnl: float = 0.0
    combined_peak: float = 0.0
    ratchet_floor: float = 0.0
    tier_state: str = "NORMAL"
    trend_phase: str = "NEUTRAL"
    trend_direction: str = "long"
    hazard_score: float = 0.0
    data_fresh: bool = True
    data_status: str = "fresh"
    capital_multiplier: float = 1.0
    positions: list[PositionState] = field(default_factory=list)
    generated_utc: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["positions"] = [asdict(p) for p in self.positions]
        return out


@dataclass
class PMDirective:
    action: str
    reason: str
    position_id: int = 0
    order_id: int = 0
    volume: int = 0
    new_stop_loss: float = 0.0
    new_take_profit: float = 0.0
    priority: int = 50
    live_allowed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class XAUProfitGuardian:
    def __init__(self, config: GuardianConfig | None = None):
        self.config = config or GuardianConfig()

    def update_ratchet(
        self,
        basket: BasketState,
        *,
        previous: BasketState | None,
        atr_percentile: float = 0.5,
    ) -> BasketState:
        now_combined = _sf(basket.combined_pnl, _sf(basket.realized_today) + _sf(basket.unrealized_now))
        prev_peak = _sf(previous.combined_peak, 0.0) if previous else 0.0
        peak = max(prev_peak, now_combined, _sf(basket.combined_peak, 0.0))
        vol_mult = 0.8 + min(1.0, max(0.0, _sf(atr_percentile, 0.5))) * 0.8
        soft_giveback = min(0.18, max(0.06, self.config.base_giveback_pct * vol_mult))
        floor = max(_sf(previous.ratchet_floor, 0.0) if previous else 0.0, peak * (1.0 - soft_giveback)) if peak > 0 else 0.0
        giveback = (peak - now_combined) / max(abs(peak), 1e-9) if peak > 0 else 0.0
        if peak <= 0:
            tier = "NORMAL"
        elif giveback >= self.config.hard_giveback_pct or basket.hazard_score >= 80:
            tier = "T3_HARVEST"
        elif now_combined < floor or basket.hazard_score >= 60:
            tier = "T2_CRYSTALLIZE"
        elif giveback >= soft_giveback * 0.5:
            tier = "T1_PRUNE"
        else:
            tier = "NORMAL"
        basket.combined_pnl = now_combined
        basket.combined_peak = round(peak, 4)
        basket.ratchet_floor = round(floor, 4)
        basket.tier_state = tier
        if not basket.generated_utc:
            basket.generated_utc = utc_now_iso()
        return basket

    def evaluate(self, basket: BasketState, orders: Iterable[OrderState], *, atr: float = 5.0) -> list[PMDirective]:
        if not bool(getattr(basket, "data_fresh", True)):
            return [
                PMDirective(
                    action="hold",
                    reason=f"broker_truth_stale:{str(getattr(basket, 'data_status', 'stale'))}",
                    priority=1,
                    live_allowed=False,
                    metadata={"data_status": str(getattr(basket, "data_status", "stale"))},
                )
            ]
        directives: list[PMDirective] = []
        atr = max(self.config.min_atr, _sf(atr, 5.0))

        # M6: blind/surreal geometry kill. This is a schema sanity validator, not a broad entry gate.
        for order in orders:
            coh = validate_tp_sl_coherence(
                direction=order.direction,
                entry=order.entry,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                atr=atr,
            )
            if not coh.ok:
                directives.append(
                    PMDirective(
                        action="cancel_order",
                        order_id=int(order.order_id),
                        reason=f"blind_signal_schema:{coh.reason}",
                        priority=5,
                        live_allowed=self.config.live_enabled and self.config.micro_live_allow_schema_cancel,
                        metadata={"coherence": asdict(coh), "source": order.source, "direction": order.direction},
                    )
                )
                continue
        positions = list(basket.positions or [])
        if basket.tier_state in {"T1_PRUNE", "T2_CRYSTALLIZE", "T3_HARVEST"}:
            reservoir_protected: list[PositionState] = []
            weak_pool: list[PositionState] = []
            for p in positions:
                if _si(p.position_id) <= 0 or p.r_now() >= self.config.runner_preserve_r:
                    continue
                if basket.tier_state in {"T1_PRUNE", "T2_CRYSTALLIZE"} and p.is_winner_long_reservoir(self.config):
                    reservoir_protected.append(p)
                    continue
                weak_pool.append(p)
            weak = sorted(
                weak_pool,
                key=lambda p: p.weakness_score(basket.trend_phase),
                reverse=True,
            )
            for p in reservoir_protected:
                directives.append(
                    PMDirective(
                        action="hold_position",
                        position_id=int(p.position_id),
                        volume=0,
                        reason=f"winner_long_reservoir_runner_permission:{basket.tier_state}",
                        priority=18,
                        live_allowed=False,
                        metadata={
                            "source": p.source,
                            "opened_hour": p.opened_hour(),
                            "r_now": round(p.r_now(), 4),
                            "size_multiplier": 1.0,
                            "risk_usd_delta": 0.0,
                            "execution_enabled": False,
                        },
                    )
                )
            for p in weak[: max(1, self.config.max_prune_positions)]:
                directives.append(
                    PMDirective(
                        action="close_position",
                        position_id=int(p.position_id),
                        volume=int(_sf(p.volume, 0.0)),
                        reason=f"weak_add_prune:{basket.tier_state}",
                        priority=20,
                        live_allowed=self.config.live_enabled and self.config.micro_live_allow_weak_prune,
                        metadata={"weakness_score": round(p.weakness_score(basket.trend_phase), 4), "r_now": round(p.r_now(), 4)},
                    )
                )

        if basket.tier_state in {"T2_CRYSTALLIZE"}:
            winners = [p for p in positions if p.r_now() >= 1.5 and _si(p.position_id) > 0]
            for p in winners:
                harvest_fraction = 0.25
                vol = max(1, int(_sf(p.volume, 0.0) * harvest_fraction))
                directives.append(
                    PMDirective(
                        action="partial_close",
                        position_id=int(p.position_id),
                        volume=vol,
                        reason=f"profit_reservoir_crystallize:{basket.tier_state}",
                        priority=30,
                        live_allowed=self.config.mode in {"half_live", "full"} and self.config.half_live_allow_partial_harvest,
                        metadata={"r_now": round(p.r_now(), 4), "harvest_fraction": harvest_fraction},
                    )
                )

        if basket.tier_state == "T3_HARVEST":
            for p in positions:
                if _si(p.position_id) <= 0:
                    continue
                directives.append(
                    PMDirective(
                        action="close_position",
                        position_id=int(p.position_id),
                        volume=int(_sf(p.volume, 0.0)),
                        reason="equity_ratchet_full_harvest",
                        priority=40,
                        live_allowed=self.config.mode == "full" and self.config.full_live_allow_harvest,
                        metadata={"combined_peak": basket.combined_peak, "combined_pnl": basket.combined_pnl},
                    )
                )

        # Deduplicate: preserve highest priority for the same action target, but allow both partial and full distinct.
        seen: set[tuple[str, int, int]] = set()
        ordered: list[PMDirective] = []
        for d in sorted(directives, key=lambda x: x.priority):
            key = (d.action, int(d.position_id or 0), int(d.order_id or 0))
            if key in seen:
                continue
            seen.add(key)
            ordered.append(d)
        return ordered


class XAUProfitGuardianDB:
    def __init__(self, db_path: str | Path, runtime_path: str | Path | None = None, config: GuardianConfig | None = None):
        self.db_path = Path(db_path)
        self.runtime_path = Path(runtime_path) if runtime_path else self.db_path.parent / "runtime" / "xau_basket_truth.json"
        self.guardian = XAUProfitGuardian(config)

    @staticmethod
    def ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS xau_basket_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_utc TEXT NOT NULL,
                symbol TEXT NOT NULL DEFAULT 'XAUUSD',
                realized_today REAL DEFAULT 0,
                unrealized_now REAL DEFAULT 0,
                combined_pnl REAL DEFAULT 0,
                combined_peak REAL DEFAULT 0,
                ratchet_floor REAL DEFAULT 0,
                tier_state TEXT DEFAULT '',
                trend_phase TEXT DEFAULT '',
                hazard_score REAL DEFAULT 0,
                raw_json TEXT DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_xau_basket_snapshots_event ON xau_basket_snapshots(event_utc DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pm_action_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_utc TEXT NOT NULL,
                mode TEXT DEFAULT '',
                action TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                symbol TEXT DEFAULT 'XAUUSD',
                position_id INTEGER DEFAULT 0,
                order_id INTEGER DEFAULT 0,
                volume INTEGER DEFAULT 0,
                live_allowed INTEGER DEFAULT 0,
                executed INTEGER DEFAULT 0,
                ok INTEGER DEFAULT 0,
                message TEXT DEFAULT '',
                raw_json TEXT DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pm_action_journal_event ON pm_action_journal(event_utc DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS equity_peaks (
                scope TEXT PRIMARY KEY,
                updated_utc TEXT NOT NULL,
                combined_peak REAL DEFAULT 0,
                ratchet_floor REAL DEFAULT 0,
                raw_json TEXT DEFAULT '{}'
            )
            """
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
        except Exception:
            pass
        self.ensure_schema(conn)
        return conn

    @staticmethod
    def _session_scope(now_iso: str | None = None) -> str:
        token = str(now_iso or utc_now_iso())[:10]
        return f"xau_session:{token}"

    @staticmethod
    def _parse_utc(value: str) -> Optional[datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _latest_mid(self, conn: sqlite3.Connection) -> tuple[float, str]:
        row = conn.execute(
            "SELECT bid,ask,event_utc FROM ctrader_spot_ticks WHERE symbol='XAUUSD' ORDER BY event_utc DESC,id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0.0, ""
        bid = _sf(row["bid"])
        ask = _sf(row["ask"])
        return ((bid + ask) / 2.0 if bid > 0 and ask > 0 else max(bid, ask)), str(row["event_utc"] or "")

    def _load_previous(self, conn: sqlite3.Connection) -> Optional[BasketState]:
        row = conn.execute(
            "SELECT combined_peak,ratchet_floor,tier_state,raw_json FROM xau_basket_snapshots WHERE symbol='XAUUSD' AND substr(event_utc,1,10)=? ORDER BY event_utc DESC,id DESC LIMIT 1",
            (self._session_scope().split(":", 1)[1],),
        ).fetchone()
        if row is None:
            peak_row = conn.execute("SELECT combined_peak,ratchet_floor,raw_json FROM equity_peaks WHERE scope=? LIMIT 1", (self._session_scope(),)).fetchone()
            if peak_row is None:
                return None
            return BasketState(symbol="XAUUSD", combined_peak=_sf(peak_row["combined_peak"]), ratchet_floor=_sf(peak_row["ratchet_floor"]))
        try:
            raw = json.loads(str(row["raw_json"] or "{}"))
            return BasketState(
                symbol="XAUUSD",
                combined_peak=_sf(row["combined_peak"], _sf(raw.get("combined_peak"), 0.0)),
                ratchet_floor=_sf(row["ratchet_floor"], _sf(raw.get("ratchet_floor"), 0.0)),
                tier_state=str(row["tier_state"] or raw.get("tier_state") or "NORMAL"),
            )
        except Exception:
            return BasketState(symbol="XAUUSD", combined_peak=_sf(row["combined_peak"]), ratchet_floor=_sf(row["ratchet_floor"]))

    def load_state(self, *, features: Optional[dict[str, Any]] = None) -> tuple[BasketState, list[OrderState], float]:
        features = dict(features or {})
        with self._connect() as conn:
            current, tick_utc = self._latest_mid(conn)
            tick_dt = self._parse_utc(tick_utc)
            tick_age = (datetime.now(timezone.utc) - tick_dt).total_seconds() if tick_dt is not None else 1e9
            data_fresh = bool(current > 0 and tick_age <= self.guardian.config.stale_tick_max_age_sec)
            data_status = "fresh" if data_fresh else f"stale_tick:{int(tick_age)}s"
            positions: list[PositionState] = []
            for row in conn.execute(
                """
                SELECT position_id,source,direction,volume,entry_price,stop_loss,take_profit,first_seen_utc,last_seen_utc
                  FROM ctrader_positions
                 WHERE symbol='XAUUSD' AND COALESCE(is_open,0)=1
                 ORDER BY first_seen_utc ASC, position_id ASC
                """
            ).fetchall():
                direction = _direction(row["direction"])
                entry = _sf(row["entry_price"])
                volume = _sf(row["volume"])
                pts = (current - entry) if direction == "long" else (entry - current)
                unrealized = pts * (volume / 100.0) if current > 0 and entry > 0 else 0.0
                positions.append(
                    PositionState(
                        position_id=_si(row["position_id"]),
                        direction=direction,
                        entry=entry,
                        current=current,
                        stop_loss=_sf(row["stop_loss"]),
                        take_profit=_sf(row["take_profit"]),
                        volume=volume,
                        source=str(row["source"] or ""),
                        first_seen_utc=str(row["first_seen_utc"] or ""),
                        mfe=max(0.0, pts) if current > 0 else 0.0,
                        mae=max(0.0, -pts) if current > 0 else 0.0,
                        age_sec=0.0,
                        unrealized_usd=unrealized,
                    )
                )
            orders: list[OrderState] = []
            for row in conn.execute(
                """
                SELECT order_id,source,direction,volume,entry_price,stop_loss,take_profit
                  FROM ctrader_orders
                 WHERE symbol='XAUUSD' AND COALESCE(is_open,0)=1
                 ORDER BY first_seen_utc ASC, order_id ASC
                """
            ).fetchall():
                orders.append(
                    OrderState(
                        order_id=_si(row["order_id"]),
                        direction=_direction(row["direction"]),
                        entry=_sf(row["entry_price"]),
                        stop_loss=_sf(row["stop_loss"]),
                        take_profit=_sf(row["take_profit"]),
                        volume=_sf(row["volume"]),
                        source=str(row["source"] or ""),
                    )
                )
            realized = _sf(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(d.pnl_usd),0) AS pnl
                      FROM ctrader_deals d
                      LEFT JOIN ctrader_positions p ON p.position_id=d.position_id
                     WHERE d.symbol='XAUUSD'
                       AND substr(COALESCE(d.execution_utc,''),1,10)=substr(datetime('now'),1,10)
                    """
                ).fetchone()["pnl"],
                0.0,
            )
            prev = self._load_previous(conn)
        unrealized = sum(p.unrealized_usd for p in positions)
        trend_phase = classify_trend_phase(features)
        hscore = hazard_score(features)
        basket = BasketState(
            symbol="XAUUSD",
            realized_today=round(realized, 4),
            unrealized_now=round(unrealized, 4),
            combined_pnl=round(realized + unrealized, 4),
            trend_phase=trend_phase,
            trend_direction=str(features.get("trend_direction") or "long"),
            hazard_score=hscore,
            data_fresh=data_fresh,
            data_status=data_status,
            capital_multiplier=snowball_capital_multiplier(
                realized_today=realized,
                combined_peak=realized + unrealized,
                hazard_score_value=hscore,
                regime_quality=_sf(features.get("regime_quality"), 0.5),
            ),
            positions=positions,
            generated_utc=utc_now_iso(),
        )
        basket = self.guardian.update_ratchet(basket, previous=prev, atr_percentile=_sf(features.get("atr_percentile"), 0.5))
        return basket, orders, max(self.guardian.config.min_atr, _sf(features.get("atr"), 5.0))

    def persist(self, basket: BasketState, directives: list[PMDirective], *, executed: Optional[list[dict[str, Any]]] = None) -> None:
        executed = list(executed or [])
        event_utc = basket.generated_utc or utc_now_iso()
        raw = basket.to_dict()
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.runtime_path.with_suffix(self.runtime_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"basket": raw, "directives": [d.to_dict() for d in directives]}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self.runtime_path)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO xau_basket_snapshots(event_utc,symbol,realized_today,unrealized_now,combined_pnl,combined_peak,ratchet_floor,tier_state,trend_phase,hazard_score,raw_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_utc,
                    basket.symbol,
                    basket.realized_today,
                    basket.unrealized_now,
                    basket.combined_pnl,
                    basket.combined_peak,
                    basket.ratchet_floor,
                    basket.tier_state,
                    basket.trend_phase,
                    basket.hazard_score,
                    json.dumps(raw, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.execute(
                """
                INSERT INTO equity_peaks(scope,updated_utc,combined_peak,ratchet_floor,raw_json)
                VALUES(?,?,?,?,?)
                ON CONFLICT(scope) DO UPDATE SET updated_utc=excluded.updated_utc, combined_peak=max(equity_peaks.combined_peak, excluded.combined_peak), ratchet_floor=max(equity_peaks.ratchet_floor, excluded.ratchet_floor), raw_json=excluded.raw_json
                """,
                (self._session_scope(event_utc), event_utc, basket.combined_peak, basket.ratchet_floor, json.dumps(raw, ensure_ascii=False, sort_keys=True)),
            )
            executed_by_key = {(str(x.get("action")), _si(x.get("position_id")), _si(x.get("order_id"))): x for x in executed}
            for d in directives:
                x = executed_by_key.get((d.action, int(d.position_id or 0), int(d.order_id or 0)), {})
                conn.execute(
                    """
                    INSERT INTO pm_action_journal(event_utc,mode,action,reason,symbol,position_id,order_id,volume,live_allowed,executed,ok,message,raw_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event_utc,
                        self.guardian.config.mode,
                        d.action,
                        d.reason,
                        basket.symbol,
                        int(d.position_id or 0),
                        int(d.order_id or 0),
                        int(d.volume or 0),
                        1 if d.live_allowed else 0,
                        1 if x else 0,
                        1 if bool(x.get("ok")) else 0,
                        str(x.get("message") or ""),
                        json.dumps(d.to_dict(), ensure_ascii=False, sort_keys=True),
                    ),
                )
            conn.commit()

    def apply_live_directives(self, executor: Any, directives: list[PMDirective]) -> list[dict[str, Any]]:
        if self.guardian.config.mode == "shadow":
            return []
        executed: list[dict[str, Any]] = []
        count = 0
        try:
            with self._connect() as conn:
                recent = conn.execute(
                    """
                    SELECT COUNT(*) FROM pm_action_journal
                     WHERE executed=1 AND event_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-5 minutes')
                    """
                ).fetchone()[0]
            count = int(recent or 0)
        except Exception:
            count = 0
        for d in sorted(directives, key=lambda x: x.priority):
            if count >= self.guardian.config.max_actions_per_5min:
                break
            if not d.live_allowed:
                continue
            result = None
            if d.action == "cancel_order" and int(d.order_id or 0) > 0:
                result = executor.cancel_order(order_id=int(d.order_id))
            elif d.action in {"close_position", "partial_close"} and int(d.position_id or 0) > 0:
                result = executor.close_position(position_id=int(d.position_id), volume=int(d.volume or 0))
            elif d.action == "amend_sltp" and int(d.position_id or 0) > 0:
                result = executor.amend_position_sltp(
                    position_id=int(d.position_id),
                    stop_loss=_sf(d.new_stop_loss),
                    take_profit=_sf(d.new_take_profit),
                    trailing_stop_loss=False,
                )
            if result is not None:
                count += 1
                executed.append(
                    {
                        "action": d.action,
                        "position_id": int(d.position_id or 0),
                        "order_id": int(d.order_id or 0),
                        "ok": bool(getattr(result, "ok", False)),
                        "status": str(getattr(result, "status", "") or ""),
                        "message": str(getattr(result, "message", "") or ""),
                    }
                )
        return executed

    def run_once(self, *, executor: Any = None, features: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        basket, orders, atr = self.load_state(features=features)
        directives = self.guardian.evaluate(basket, orders, atr=atr)
        executed = self.apply_live_directives(executor, directives) if executor is not None else []
        self.persist(basket, directives, executed=executed)
        return {
            "ok": True,
            "mode": self.guardian.config.mode,
            "basket": basket.to_dict(),
            "directives": [d.to_dict() for d in directives],
            "executed": executed,
        }
