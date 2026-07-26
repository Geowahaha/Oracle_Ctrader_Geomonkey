"""Dexter3 executor — the ONLY dexter3 module allowed to call mutating MCP
methods (place_market_order / amend_position / close_position).

Every order carries ``LABEL = f"{LABEL_FAMILY}:{VERSION}"`` (blueprint
"Non-negotiables" #2 — label isolation; loops/peers ignore foreign labels).
``VERSION`` comes from env ``DEXTER3_FABLE_VERSION`` (sanitized; see
``sanitize_label_version``) so every trade is attributable to the exact code
version that placed it (owner directive, 2026-07-15 "versioned labels").
Ownership/matching checks (``is_our_position``, lane reconciliation) use
FAMILY-PREFIX matching (``label_matches_family``) rather than exact-label
equality, so a version bump never orphans positions/journal rows a PRIOR
version of this same lane opened — see that function's docstring.
Demo-only by hard refusal (blueprint #3): ``execute_entry`` will not place an
order unless the bound account can be confirmed as demo.

Order envelope (place/amend/close) is copied field-for-field from
``scripts/btc_scalp_monitor.py`` via ``dexter3.mcp_client``'s mutating
methods — this module does not talk HTTP/MCP protocol directly, it only
calls ``Dexter3McpClient`` methods and applies the pre-flight/post-flight
gates the blueprint requires.

Flow (``execute_entry``):
    1. demo refusal gate (accounts must be demo; unknown -> refuse)
    2. pre-flight gates (quote sanity, SL/TP sidedness, min-volume/step,
       duplicate-label guard, daily entry cap, daily loss-stop)
    3. sizing (risk_usd / sl_distance -> units, clamp to [minVolume, cap])
    4. place MARKET order with SL/TP attached
    5. post-flight: re-read broker position, verify side/volume/SL/TP
       (btc_scalp_monitor's ``verify_position_snapshot`` geometry check);
       naked-position repair (amend, else close) if SL is missing.

Every gate refusal and every exec event is journaled to the ``exec_events``
table (added to ``dexter3.decision_journal`` by this module) so nothing that
happens on the live micro-entry path is silent.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from dexter3.mcp_client import (
    Dexter3McpClient,
    McpClientError,
    McpMutationUncertain,
    McpZombieError,
)
from dexter3.smart_exit import disaster_stop_distance

# -- versioned broker label (owner directive, 2026-07-15) --------------------
# LABEL_FAMILY identifies the LANE (fable); it never changes across a version
# bump — every "is this mine" check below matches on FAMILY, not the exact
# versioned string, so a version bump never orphans a position/journal row a
# prior version of this same lane wrote. Grok/VP keep their own frozen,
# UNCHANGED label constants (dexter3.grok_v10.GROK_LABEL,
# dexter3.volume_profile.VP_LABEL) — only their FAMILY roots
# ("dexter3:grok" / "dexter3:vp", see _KNOWN_LABEL_FAMILIES below)
# participate in this same matching mechanism.
LABEL_FAMILY = "dexter3:fable"

# Same env var dexter3.shadow_runner already reads (previously only for a log
# line — see run_loop's ``fable_version``) — this is what actually wires it
# into the broker-facing LABEL. Default mirrors shadow_runner's own default so
# an unset env var is byte-identical to today's logged value.
DEXTER3_FABLE_VERSION_ENV = "DEXTER3_FABLE_VERSION"
DEFAULT_FABLE_VERSION = "v1.7-selective-edge"

# cTrader label ceiling: dexter3.openapi_client's place_market_order/_execute_order
# payload construction truncates any incoming label to 64 chars
# (``label=str(label or "")[:64]`` — see that module) before it ever reaches
# the broker. 60 keeps "family:version" comfortably under that hard ceiling
# with headroom to spare, without needing to touch the broker-facing
# truncation itself.
MAX_LABEL_LEN = 60

# Anything outside [A-Za-z0-9._-] (spaces, colons, Thai/unicode text, or any
# other value an operator might set DEXTER3_FABLE_VERSION to) collapses to a
# single '-' — the assembled label must always be a broker-safe ASCII token.
_VERSION_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _env_int(key: str, default: int) -> int:
    """Int env with a safe fallback (2026-07-26 audit helpers)."""
    try:
        return int(str(os.environ.get(key, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float_local(key: str, default: float) -> float:
    try:
        return float(str(os.environ.get(key, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def sanitize_label_version(raw: Any) -> str:
    """Collapse invalid characters to '-'; empty/all-invalid input -> "unknown"
    rather than producing an empty/degenerate version segment."""
    cleaned = _VERSION_SANITIZE_RE.sub("-", str(raw or "")).strip("-")
    return cleaned or "unknown"


def build_versioned_label(family: str, version: str, *, max_len: int = MAX_LABEL_LEN) -> str:
    """``f"{family}:{sanitize_label_version(version)}"``, truncated (from the
    version's tail — the family root must never be cut) so the assembled
    label never exceeds ``max_len``."""
    family = str(family or "")
    safe_version = sanitize_label_version(version)
    label = f"{family}:{safe_version}"
    if len(label) > max_len:
        keep = max(1, max_len - len(family) - 1)  # "-1" = room for the ":" separator
        safe_version = safe_version[:keep]
        label = f"{family}:{safe_version}"
    return label


VERSION = sanitize_label_version(os.environ.get(DEXTER3_FABLE_VERSION_ENV, DEFAULT_FABLE_VERSION))
LABEL = build_versioned_label(LABEL_FAMILY, VERSION)


def label_matches_family(label: Any, family: Any) -> bool:
    """Family-prefix ownership match: True when ``label`` IS ``family``
    exactly, or begins with it — covers every past/future VERSION of that
    family's label (e.g. both the pre-2026-07-15 "dexter3:fable:m5h-v1" and
    today's "dexter3:fable:v1.7-selective-edge" match family "dexter3:fable").

    Deliberately a PLAIN prefix check (no mandatory ':' boundary after
    ``family``) rather than the stricter ``label.startswith(family + ':')``:
    Grok's pre-existing, deliberately-UNCHANGED label
    ("dexter3:grok-v1.0:scalper") separates its version with a HYPHEN, not a
    colon, so a colon-bound rule would silently stop matching family
    "dexter3:grok" against Grok's OWN label. Safe given this repo's actual
    family roots (dexter3:fable / dexter3:grok / dexter3:vp) are mutually
    non-prefixing — mirrors the plain ``.startswith()`` convention
    ``dexter3.basket_live.lane_positions`` already uses.
    """
    label_s = str(label or "")
    family_s = str(family or "")
    if not family_s:
        return False
    if label_s == family_s:
        return True
    rest = label_s[len(family_s):] if label_s.startswith(family_s) else ""
    if not rest:
        return False
    # Normal case: the family root is followed by a ':' separated segment
    # ("dexter3:fable" -> "dexter3:fable:v1.8-size-the-edge").
    if rest[0] == ":":
        return True
    # LEGACY hyphen-versioned labels: Grok separates its VERSION with a hyphen
    # ("dexter3:grok" -> "dexter3:grok-v1.0:scalper"), so a strictly
    # colon-bounded rule would stop matching Grok against its own label.
    #
    # 2026-07-25 audit fix: the old rule was a bare ``startswith(family)``,
    # which also made "dexter3:dpull" match "dexter3:dpull-cs:canary" — a
    # DIFFERENT LANE, not a version of dpull. That let the dpull lane read
    # dpull-cs's deals and positions as its own (premature LOSS_STOPPED on a
    # peer's losses, foreign legs dragging its basket aggregate) and it
    # contaminated the very forward A/B those two lanes existed to run. The
    # hyphen is therefore honoured ONLY when what follows is a version token
    # (``v`` + digit), which admits "-v1.0..." and rejects "-cs:canary"
    # without hardcoding any lane name.
    if len(rest) >= 3 and rest[0] == "-" and rest[1] == "v" and rest[2].isdigit():
        return True
    return False


# Known family roots this module can recognize on an arbitrary CURRENT
# ``LABEL`` value (module global, patched per-process by shadow_runner for
# grok/vp — see run_loop/main). Grok/VP's constants are duplicated here as
# plain string literals (not imported) — same "no cross-imports, duplicate
# small pure logic" convention dexter3.basket_live documents for itself, to
# avoid coupling this module to grok_v10/volume_profile.
_KNOWN_LABEL_FAMILIES = (LABEL_FAMILY, "dexter3:vp")


def _label_family_root(label: Any) -> str:
    """Best-known family root for ``label`` (typically the CURRENT ``LABEL``
    module global). Falls back to ``label`` itself (an exact-match-only
    singleton family) for anything unrecognized, so ownership checks never
    silently widen to "any dexter3 label" for a label this module doesn't
    know about."""
    label_s = str(label or "")
    for root in _KNOWN_LABEL_FAMILIES:
        if label_matches_family(label_s, root):
            return root
    return label_s

# -- config defaults (spec section 2) ---------------------------------------
DEFAULT_MAX_SPREAD_BPS = 15.0
DEFAULT_MAX_LIVE_ENTRIES_PER_DAY = 6
DEFAULT_STOP_AFTER_DAILY_LOSSES = 2
DEFAULT_RISK_USD = 0.50
DEFAULT_MAX_VOLUME_UNITS = 0.05
# Demo account allow-list — blueprint "Non-negotiables" #3: "Demo 9922808
# until readiness gates pass". get_balance() on the Local MCP has no
# isLive/isDemo field (verified against the ctrader-mcp-servers skill and
# scripts/btc_scalp_monitor.py, which does not check one either); the
# authoritative active-account identifier it DOES expose is
# get_balance().traderId (skill: "Q-L15"). We treat membership in this
# allow-list as the demo proof; anything else (including a missing/
# unparseable traderId) is refused, never assumed safe.
# Two ids for the SAME demo account: 9922808 is the broker login shown in
# cTrader Desktop / AGENTS.md; 3555162 is what get_balance().traderId actually
# returns for it (measured live 2026-07-05, balance cross-checked against the
# known demo equity). accountType is NOT a demo flag — it returns the margin
# mode ("Hedged") — so traderId pinning stays the only account proof available.
# All THREE are the same demo account (login 9922808), just different cTrader
# identifiers: 9922808 = login, 3555162 = traderId (Local MCP get_balance),
# 46670728 = ctidTraderAccountId (OpenAPI get_balance().traderId). The gate must
# accept the OpenAPI identity too or every VM-transport entry fails
# account_not_confirmed_demo (found at cutover 2026-07-10). Overridable via
# DEXTER3_DEMO_TRADER_IDS_CSV for a future account change.
DEFAULT_DEMO_TRADER_IDS = (9922808, 3555162, 46670728)

# H4 (2026-07-15 cross-lane entanglement audit): combined real-account
# open-risk ceiling. Two live lanes (Fable/Grok/VP) share ONE cTrader
# account, so any single lane's own per-entry risk cap cannot bound the
# ACCOUNT's total open risk across all of them simultaneously. Env
# DEXTER3_ACCOUNT_MAX_OPEN_RISK_USD (default 40.0 USD; <=0 disables) caps
# sum(|entry-SL| x volume) across every OPEN position labeled "dexter3*"
# (own lane + every peer lane) plus this entry's own planned risk — see
# ``Dexter3Executor._account_open_risk_cap_refusal``.
DEFAULT_ACCOUNT_MAX_OPEN_RISK_USD = 40.0

# Twin-entry cross-lane duplicate guard (2026-07-15 17:04Z-00:54Z live audit,
# docs/AGENT_SYNC_BOARD.md): both the fable and grok lanes run the SAME
# producer (decide_hunt) over the SAME bars, so they can fire near-identical
# trades on the same symbol+side within seconds/minutes of each other with
# zero diversification (net -34.00 / 19 trades that window; e.g. 18:40Z buy
# hunt_swing_structure sl_dist=10.35 + 18:45Z buy hunt_swing_structure
# sl_dist=7.31, both full-SL losses; 19:05Z both lanes buy
# hunt_sweep_reclaim with IDENTICAL sl_dist=4.65). Env
# ``DEXTER3_CROSS_LANE_DEDUP``: "off" (default -- byte-identical pre-fix
# behavior) | "skip" (refuse the duplicate entry, same refusal shape as
# every other pre-flight gate) | "downsize" (place it anyway at
# ``DEXTER3_CROSS_LANE_DEDUP_MULT`` x risk instead of refusing outright).
# Any unrecognized value collapses to "off" -- an operator typo must never
# silently start gating/downsizing live entries.
DEXTER3_CROSS_LANE_DEDUP_ENV = "DEXTER3_CROSS_LANE_DEDUP"
DEXTER3_CROSS_LANE_DEDUP_WINDOW_MIN_ENV = "DEXTER3_CROSS_LANE_DEDUP_WINDOW_MIN"
DEXTER3_CROSS_LANE_DEDUP_MULT_ENV = "DEXTER3_CROSS_LANE_DEDUP_MULT"
DEFAULT_CROSS_LANE_DEDUP_WINDOW_MIN = 30.0
DEFAULT_CROSS_LANE_DEDUP_MULT = 0.5


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ExecutorConfig:
    max_spread_bps: float = DEFAULT_MAX_SPREAD_BPS
    max_live_entries_per_day: int = DEFAULT_MAX_LIVE_ENTRIES_PER_DAY
    stop_after_daily_losses: int = DEFAULT_STOP_AFTER_DAILY_LOSSES
    risk_usd: float = DEFAULT_RISK_USD
    max_volume_units: float = DEFAULT_MAX_VOLUME_UNITS
    allow_basket_legs: bool = False
    demo_trader_ids: tuple[int, ...] = DEFAULT_DEMO_TRADER_IDS


# -- exec_events schema (added here; decision_journal owns the connection) --

_EXEC_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    event TEXT NOT NULL,
    position_id INTEGER,
    verified INTEGER,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exec_events_symbol ON exec_events(symbol);
CREATE INDEX IF NOT EXISTS idx_exec_events_event ON exec_events(event);
CREATE INDEX IF NOT EXISTS idx_exec_events_position_id ON exec_events(position_id);
"""


def ensure_exec_events_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_EXEC_EVENTS_SCHEMA)
    conn.commit()


def insert_exec_event(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    event: str,
    position_id: int | None = None,
    verified: bool | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    ensure_exec_events_table(conn)
    cur = conn.execute(
        """INSERT INTO exec_events (ts, symbol, event, position_id, verified, payload_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            utc_now_iso(),
            str(symbol),
            str(event),
            int(position_id) if position_id else None,
            None if verified is None else int(bool(verified)),
            json.dumps(payload or {}, ensure_ascii=False, default=str),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def recent_exec_events(
    conn: sqlite3.Connection, symbol: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    ensure_exec_events_table(conn)
    query = "SELECT id, ts, symbol, event, position_id, verified, payload_json FROM exec_events"
    params: tuple[Any, ...] = ()
    if symbol:
        query += " WHERE symbol = ?"
        params = (symbol,)
    query += " ORDER BY id DESC LIMIT ?"
    params = params + (int(limit),)
    rows = conn.execute(query, params).fetchall()
    out: list[dict[str, Any]] = []
    for row_id, ts, sym, event, position_id, verified, payload_json in rows:
        out.append(
            {
                "id": row_id,
                "ts": ts,
                "symbol": sym,
                "event": event,
                "position_id": position_id,
                "verified": None if verified is None else bool(verified),
                "payload": json.loads(payload_json or "{}"),
            }
        )
    return out


# -- position field helpers (mirror scripts/btc_scalp_monitor.py exactly) ---


def position_id_of(position: dict[str, Any]) -> int:
    try:
        return int(position.get("positionId") or position.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def position_label_of(position: dict[str, Any]) -> str:
    return str(position.get("label") or position.get("comment") or "").strip()


def position_symbol_of(position: dict[str, Any]) -> str:
    return str(position.get("symbolName") or position.get("symbol") or "").strip().upper()


def position_side_of(position: dict[str, Any]) -> str:
    raw = str(position.get("tradeSide") or position.get("side") or "").strip().lower()
    if raw.startswith("buy"):
        return "buy"
    if raw.startswith("sell"):
        return "sell"
    return raw


def position_volume_of(position: dict[str, Any]) -> float:
    for key in ("volumeInUnits", "volume", "volumeUnits"):
        try:
            value = float(position.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def position_stop_loss_of(position: dict[str, Any]) -> float:
    try:
        return float(position.get("stopLoss", position.get("stopLossPrice", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def position_take_profit_of(position: dict[str, Any]) -> float:
    try:
        return float(position.get("takeProfit", position.get("takeProfitPrice", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def is_our_position(position: dict[str, Any]) -> bool:
    """Peer isolation: a position is ours when its label belongs to the SAME
    FAMILY as the currently active ``LABEL`` (module global; grok/vp patch
    ``LABEL`` per-process at startup — see shadow_runner.run_loop/main), not
    merely an exact match against today's exact versioned string. This lets a
    version bump keep managing every position a PRIOR version of the SAME
    lane opened (owner directive, 2026-07-15 "versioned labels")."""
    return label_matches_family(position_label_of(position), _label_family_root(LABEL))


def verify_entry_snapshot(
    position: dict[str, Any] | None, side: str, entry: float, volume: float,
    expect_sl: float | None = None, expect_tp: float | None = None,
    price_tol: float = 0.05,
) -> tuple[bool, dict[str, Any]]:
    """Geometry/side/volume check on a freshly-opened position.

    Mirrors ``scripts/btc_scalp_monitor.py::verify_position_snapshot``
    exactly (side_ok / volume_ok / geometry_ok, volume tolerance 0.1%).

    ``expect_sl``/``expect_tp`` (2026-07-25 audit fix): the base check only
    proves the stop is on the CORRECT SIDE of entry — not that it is where the
    caller asked for. An amend whose request the broker REFUSED therefore came
    back ``verified=True`` because the OLD stop was still on the right side, so
    the caller logged success and moved on. That is how the dpull-cs backstop
    could report "amended" while the position silently kept its tight soft stop
    (three retries burned, then given up on permanently) — reinstating the exact
    wick-stop-out the backstop exists to prevent. When an expected price is
    supplied, it must MATCH within ``price_tol``. Optional so entry-time callers
    that legitimately have no target price keep the previous behaviour.
    """
    if not position:
        return False, {"reason": "position_not_found"}
    side_ok = position_side_of(position) == side
    volume_actual = position_volume_of(position)
    volume_ok = volume_actual >= max(0.0, float(volume) * 0.999)
    sl = position_stop_loss_of(position)
    tp = position_take_profit_of(position)
    if side == "buy":
        geometry_ok = sl > 0 and tp > 0 and sl < entry < tp
    else:
        geometry_ok = sl > 0 and tp > 0 and tp < entry < sl

    sl_matches: bool | None = None
    tp_matches: bool | None = None
    if expect_sl is not None:
        try:
            sl_matches = abs(float(sl) - float(expect_sl)) <= float(price_tol)
        except (TypeError, ValueError):
            sl_matches = False
        geometry_ok = bool(geometry_ok) and bool(sl_matches)
    if expect_tp is not None:
        try:
            tp_matches = abs(float(tp) - float(expect_tp)) <= float(price_tol)
        except (TypeError, ValueError):
            tp_matches = False
        geometry_ok = bool(geometry_ok) and bool(tp_matches)
    meta = {
        "side_ok": side_ok,
        "volume_ok": volume_ok,
        "volume_actual": volume_actual,
        "volume_expected": volume,
        "stop_loss": sl,
        "take_profit": tp,
        "geometry_ok": geometry_ok,
    }
    if expect_sl is not None:
        meta["sl_expected"] = expect_sl
        meta["sl_matches"] = sl_matches
    if expect_tp is not None:
        meta["tp_expected"] = expect_tp
        meta["tp_matches"] = tp_matches
    return bool(side_ok and volume_ok and geometry_ok), meta


def floor_to_step(value: float, step: float) -> float:
    """Round ``value`` down to the nearest multiple of ``step`` (no float drift).

    Mirrors ``scripts/btc_scalp_monitor.py::floor_to_step`` (Decimal-based,
    same rounding direction — never round UP past the caller's sizing math).
    """
    if step <= 0:
        return max(0.0, value)
    from decimal import ROUND_FLOOR, Decimal

    def _dec(v: float) -> Decimal:
        text = format(float(v), ".10f").rstrip("0").rstrip(".")
        return Decimal(text or "0")

    val = _dec(value)
    stp = _dec(step)
    units = (val / stp).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * stp)


def planned_volume_units(
    symbol_details: dict[str, Any], sl_distance: float, risk_usd: float, max_volume_units: float
) -> tuple[float, dict[str, Any]]:
    """risk_usd / sl_distance -> units, clamp to [minVolume, max_volume_units].

    Mirrors ``scripts/btc_scalp_monitor.py::planned_volume_units``'s
    min-volume clamp-up behavior: if the risk-sized volume rounds below the
    symbol's minVolume, clamp UP to minVolume and journal the resulting
    estimated_min_volume_risk_usd as ACCEPTED micro-risk (per spec 2.c) —
    this is not a refusal, it is a documented cost of trading a $60k+ asset
    at $0.50 risk on a broker with a coarse volume step.
    """
    min_volume = float(symbol_details.get("minVolume", 0.0) or 0.0)
    step = float(symbol_details.get("volumeStep", 0.0) or 0.0)
    lot_size = float(symbol_details.get("lotSize", 1.0) or 1.0)
    sl_distance = max(float(sl_distance), 0.0001)
    raw_units = max(0.0, float(risk_usd) / sl_distance)
    rounded = floor_to_step(raw_units, step) if step > 0 else raw_units
    rounded = min(rounded, float(max_volume_units))
    estimated_min_volume_risk_usd = round(min_volume * sl_distance, 4)
    meta = {
        "risk_usd": float(risk_usd),
        "sl_distance": sl_distance,
        "raw_units": round(raw_units, 8),
        "rounded_units": rounded,
        "minVolume": min_volume,
        "volumeStep": step,
        "lotSize": lot_size,
        "max_volume_units": float(max_volume_units),
        "estimated_min_volume_risk_usd": estimated_min_volume_risk_usd,
    }
    if rounded < min_volume:
        rounded = min_volume
        meta["rounded_units"] = rounded
        meta["min_volume_clamped_up"] = True
        # DEXTER3_MIN_VOLUME_RISK_RATIO_CAP (default 0 = off, byte-identical
        # legacy accept): when the min-volume floor inflates actual risk beyond
        # ratio_cap x the designed risk_usd, REFUSE instead of silently
        # accepting. Found 2026-07-09 on the Grok scalp lane: design risk $4.8
        # but 1-oz XAU floor paid -$7..-$11 on losses while small-lock wins
        # banked ~0.35R of the DESIGN risk — a structural negative skew that
        # only exists because of this clamp-up.
        try:
            ratio_cap = float(os.environ.get("DEXTER3_MIN_VOLUME_RISK_RATIO_CAP", "0") or 0.0)
        except ValueError:
            ratio_cap = 0.0
        if ratio_cap > 0 and estimated_min_volume_risk_usd > float(risk_usd) * ratio_cap:
            meta["refuse_reason"] = "min_volume_risk_exceeds_ratio_cap"
            meta["min_volume_risk_ratio_cap"] = ratio_cap
            return 0.0, meta
    if rounded > float(max_volume_units):
        meta["refuse_reason"] = "min_volume_exceeds_max_volume_units_cap"
        return 0.0, meta
    if rounded <= 0:
        meta["refuse_reason"] = "zero_volume"
        return 0.0, meta
    return rounded, meta


def _to_pips(distance: float, pip_size: float) -> int:
    return max(1, int(round(abs(float(distance)) / max(float(pip_size), 1e-9))))


# -- cross-lane duplicate-entry guard: env readers --------------------------


def _cross_lane_dedup_mode() -> str:
    """"off" (default) | "skip" | "downsize". Anything else -> "off"."""
    raw = str(os.environ.get(DEXTER3_CROSS_LANE_DEDUP_ENV, "off") or "off").strip().lower()
    return raw if raw in ("off", "skip", "downsize") else "off"


def _cross_lane_dedup_window_min() -> float:
    try:
        raw = os.environ.get(DEXTER3_CROSS_LANE_DEDUP_WINDOW_MIN_ENV, "")
        return float(raw) if raw.strip() else DEFAULT_CROSS_LANE_DEDUP_WINDOW_MIN
    except ValueError:
        return DEFAULT_CROSS_LANE_DEDUP_WINDOW_MIN


def _cross_lane_dedup_mult() -> float:
    try:
        raw = os.environ.get(DEXTER3_CROSS_LANE_DEDUP_MULT_ENV, "")
        return float(raw) if raw.strip() else DEFAULT_CROSS_LANE_DEDUP_MULT
    except ValueError:
        return DEFAULT_CROSS_LANE_DEDUP_MULT


class Dexter3Executor:
    """Places/manages LIVE micro-entries for Dexter3 decisions (demo only)."""

    def __init__(
        self,
        client: Dexter3McpClient,
        journal: Any,
        config: ExecutorConfig | None = None,
    ) -> None:
        self.client = client
        self.journal = journal
        self.config = config or ExecutorConfig()
        # journal is expected to expose a raw sqlite3 connection at
        # journal._conn (dexter3.decision_journal.DecisionJournal's shape);
        # accept a bare sqlite3.Connection too for lighter-weight tests.
        self._conn: sqlite3.Connection = getattr(journal, "_conn", journal)
        ensure_exec_events_table(self._conn)
        # Capture THIS executor's own lane label at construction time (Fable
        # vs Grok vs VP run as separate processes, each patching the module
        # global LABEL before constructing its Dexter3Executor — see
        # shadow_runner.run_loop/main). reconcile_vanished_lane_positions
        # needs a per-instance value (not a live re-read of the mutable
        # module global) so two lanes stay distinguishable even when both
        # share one process (tests) — captured here, it is byte-identical to
        # the live per-process patch-then-construct order.
        self._own_label = LABEL
        # FAMILY counterpart of the above (2026-07-15 versioned labels): the
        # candidate filter in reconcile_vanished_lane_positions matches on
        # FAMILY (spans every version of this lane), while self._own_label
        # above stays the exact string stamped onto NEW writes.
        self._own_label_family = _label_family_root(LABEL)

    # -- journaling helper ----------------------------------------------

    def _journal(
        self,
        symbol: str,
        event: str,
        *,
        position_id: int | None = None,
        verified: bool | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        return insert_exec_event(
            self._conn,
            symbol=symbol,
            event=event,
            position_id=position_id,
            verified=verified,
            payload=payload,
        )

    def _refuse(self, symbol: str, reason: str, **extra: Any) -> dict[str, Any]:
        """Return + journal a refusal. ``extra`` may never override ``action``/``reason``.

        Callers sometimes pass diagnostic kwargs derived from arbitrary
        upstream dicts (e.g. ``decision.action`` or a demo-check meta dict);
        without this guard, a coincidental ``action=`` or ``reason=`` key in
        ``extra`` would silently overwrite the refusal's own action/reason in
        the dict literal below (last-key-wins). Diagnostic values that
        collide with reserved keys are kept, just renamed with a
        ``decision_``/``meta_`` prefix instead of being dropped.
        """
        safe_extra = dict(extra)
        for reserved in ("action", "reason"):
            if reserved in safe_extra:
                safe_extra[f"decision_{reserved}"] = safe_extra.pop(reserved)
        payload = {"reason": reason, **safe_extra}
        self._journal(symbol, "entry_refused", verified=False, payload=payload)
        return {"action": "refused", "reason": reason, **safe_extra}

    # -- demo gate --------------------------------------------------------

    def _is_demo_account(self, account_state: dict[str, Any] | None) -> tuple[bool, dict[str, Any]]:
        """Refuse unless the bound account can be confirmed demo.

        ``account_state`` may directly carry an explicit ``is_demo`` bool
        (test/override path) — otherwise we resolve ``traderId`` the same
        way the ctrader-mcp-servers skill documents (Q-L15: get_balance() is
        the authoritative active-account source, NOT get_accounts_list())
        and check it against ``config.demo_trader_ids``. A missing or
        unparseable traderId is a refusal, never an assumption of safety.
        """
        state = dict(account_state or {})
        if "is_demo" in state:
            is_demo = bool(state["is_demo"])
            return is_demo, {"source": "account_state.is_demo", "is_demo": is_demo}
        trader_id_raw = state.get("traderId", state.get("trader_id"))
        try:
            trader_id = int(trader_id_raw) if trader_id_raw is not None else None
        except (TypeError, ValueError):
            trader_id = None
        if trader_id is None:
            return False, {
                "source": "get_balance.traderId",
                "detail": "traderId_missing_or_unparseable",
                "raw": trader_id_raw,
            }
        is_demo = trader_id in self.config.demo_trader_ids
        return is_demo, {
            "source": "get_balance.traderId",
            "trader_id": trader_id,
            "demo_trader_ids": list(self.config.demo_trader_ids),
            "is_demo": is_demo,
        }

    # -- pre-flight gates ---------------------------------------------------

    def _pre_flight(
        self,
        decision: Any,
        *,
        spot: dict[str, Any],
        symbol_details: dict[str, Any],
        open_positions: list[dict[str, Any]],
        today_entry_count: int,
        today_losing_count: int,
        basket_authorized: bool = False,
        planned_risk_usd: float = 0.0,
    ) -> dict[str, Any] | None:
        """Return a refusal dict (already journaled) or None to proceed.

        ``basket_authorized=True`` is set ONLY by ``execute_repair_leg`` —
        it bypasses exactly one gate (duplicate_label_position_open) so the
        basket engine can add a repair/hedge leg; every other gate still
        applies.

        ``planned_risk_usd`` (H4, 2026-07-15 cross-lane entanglement audit):
        this entry's own intended USD risk (``risk_usd_override`` or
        ``self.config.risk_usd``), folded into the account-wide open-risk
        ceiling check below — see ``_account_open_risk_cap_refusal``.
        """
        symbol = str(decision.symbol)
        side = str(decision.side or "")
        entry = float(decision.entry or 0.0)
        sl = decision.sl
        tp = decision.tp

        bid = float(spot.get("bid", 0.0) or 0.0)
        ask = float(spot.get("ask", 0.0) or 0.0)
        if bid <= 0 or ask <= 0 or bid >= ask:
            return self._refuse(symbol, "spot_quote_insane", bid=bid, ask=ask)
        mid = (bid + ask) / 2.0
        spread_bps = ((ask - bid) / mid) * 10000.0 if mid > 0 else 9999.0
        if spread_bps > self.config.max_spread_bps:
            return self._refuse(
                symbol, "spread_too_wide", spread_bps=round(spread_bps, 4), cap_bps=self.config.max_spread_bps
            )

        if side not in ("buy", "sell") or sl is None or tp is None or entry <= 0:
            return self._refuse(symbol, "incomplete_decision_geometry", side=side, entry=entry, sl=sl, tp=tp)
        sl = float(sl)
        tp = float(tp)
        if side == "buy" and not (sl < entry < tp):
            return self._refuse(symbol, "sltp_sidedness_invalid", side=side, entry=entry, sl=sl, tp=tp)
        if side == "sell" and not (tp < entry < sl):
            return self._refuse(symbol, "sltp_sidedness_invalid", side=side, entry=entry, sl=sl, tp=tp)

        min_volume = float(symbol_details.get("minVolume", 0.0) or 0.0)
        if min_volume <= 0:
            return self._refuse(symbol, "symbol_details_missing_min_volume", symbol_details=symbol_details)

        our_open = [
            p
            for p in open_positions
            if position_symbol_of(p) == symbol and is_our_position(p)
        ]
        if our_open and not (self.config.allow_basket_legs or basket_authorized):
            return self._refuse(
                symbol,
                "duplicate_label_position_open",
                open_position_ids=[position_id_of(p) for p in our_open],
            )

        # Cross-lane duplicate-entry guard (2026-07-15 twin-entry audit): only
        # the "skip" mode refuses here -- "downsize" is a sizing adjustment,
        # not a refusal, and is applied later in execute_entry (after
        # sizing's own inputs are available); "off" (default) never even
        # calls the matcher, byte-identical to pre-fix behavior.
        if _cross_lane_dedup_mode() == "skip":
            cross_lane_dup = self._cross_lane_duplicate_match(symbol, side, open_positions)
            if cross_lane_dup is not None:
                return self._refuse(
                    symbol,
                    "cross_lane_duplicate",
                    foreign_position_id=position_id_of(cross_lane_dup),
                    foreign_label=position_label_of(cross_lane_dup),
                    side=side,
                    window_min=_cross_lane_dedup_window_min(),
                )

        if today_entry_count >= self.config.max_live_entries_per_day:
            return self._refuse(
                symbol,
                "max_live_entries_per_day_reached",
                today_entry_count=today_entry_count,
                cap=self.config.max_live_entries_per_day,
            )
        if today_losing_count >= self.config.stop_after_daily_losses:
            return self._refuse(
                symbol,
                "stop_after_daily_losses_reached",
                today_losing_count=today_losing_count,
                cap=self.config.stop_after_daily_losses,
            )

        cap_refusal = self._account_open_risk_cap_refusal(symbol, open_positions, planned_risk_usd)
        if cap_refusal is not None:
            return cap_refusal
        return None

    def _account_open_risk_cap_refusal(
        self, symbol: str, open_positions: list[dict[str, Any]], planned_risk_usd: float
    ) -> dict[str, Any] | None:
        """H4 (2026-07-15 cross-lane entanglement audit): combined
        real-account open-risk ceiling across ALL dexter3 lanes.

        Env ``DEXTER3_ACCOUNT_MAX_OPEN_RISK_USD`` (default 40.0 USD; <=0
        disables) caps ``sum(|entry-SL| x volume)`` — the SAME $-risk
        formula this file's own sizing already uses (``planned_volume_units``
        inverts risk_usd = sl_distance x volume with an implicit USD-per-
        unit-per-price-point of 1.0, and
        ``dexter3.shadow_runner._lane_actual_risk_usd`` uses the identical
        formula for the same reason — every symbol this repo currently
        trades prices at that point value) — across every OPEN position
        whose label starts with ``"dexter3"`` (own lane + every peer lane,
        via the SAME unfiltered ``open_positions`` list ``execute_entry``
        already fetched for the duplicate-label check above — no extra MCP
        read), plus THIS entry's own ``planned_risk_usd``.

        A position with NO stop loss cannot be priced by that formula (its
        risk is unbounded) — rather than guess, it is treated as consuming
        the ENTIRE cap on its own, mirroring this file's existing naked-
        position posture (``_repair_naked_position``: an SL-less position is
        always repaired or closed, never left open) by making a naked peer
        position alone block further entries until it is resolved, instead
        of being silently ignored.

        Fail-open on any read/compute error — a bug in this new gate must
        never block a live entry the rest of the pipeline already approved
        (same posture as every other optional sizing/risk gate in this
        repo, e.g. ``shadow_runner._apply_anti_chase_gate``).
        """
        try:
            raw_cap = os.environ.get("DEXTER3_ACCOUNT_MAX_OPEN_RISK_USD", "")
            cap = float(raw_cap) if raw_cap.strip() else DEFAULT_ACCOUNT_MAX_OPEN_RISK_USD
        except ValueError:
            cap = DEFAULT_ACCOUNT_MAX_OPEN_RISK_USD
        if cap <= 0:
            return None
        try:
            existing_total = 0.0
            for pos in open_positions or []:
                label = position_label_of(pos)
                if not label.startswith("dexter3"):
                    continue
                vol = position_volume_of(pos)
                if vol <= 0:
                    continue
                entry_price = float(pos.get("entryPrice") or pos.get("price") or 0.0)
                sl_price = position_stop_loss_of(pos)
                if sl_price <= 0 or entry_price <= 0:
                    # Unpriceable risk (no SL, or a position snapshot missing
                    # its own entry price) — never fabricate a distance from
                    # a zero placeholder; treat it the same conservative way
                    # a naked position is treated above (see docstring).
                    existing_total = max(existing_total, cap)
                    continue
                existing_total += abs(entry_price - sl_price) * vol
            projected_total = existing_total + max(0.0, float(planned_risk_usd))
            if projected_total > cap:
                return self._refuse(
                    symbol,
                    "account_open_risk_cap",
                    existing_open_risk_usd=round(existing_total, 4),
                    planned_risk_usd=round(float(planned_risk_usd), 4),
                    projected_total_usd=round(projected_total, 4),
                    cap_usd=cap,
                )
            return None
        except Exception as exc:  # noqa: BLE001 - fail-open: a gate bug must never block an approved entry
            self._journal(
                symbol,
                "account_open_risk_gate_failed",
                payload={"error": str(exc), "note": "fail-open — entry proceeds ungated by this check"},
            )
            return None

    def _cross_lane_duplicate_match(
        self, symbol: str, side: str, open_positions: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """First OPEN position belonging to a DIFFERENT dexter3 lane (label
        starts with "dexter3" but is NOT our own family — an own-family
        duplicate is the existing ``duplicate_label_position_open`` gate's
        job, never this one's) on the SAME symbol, in the SAME side, opened
        within ``DEXTER3_CROSS_LANE_DEDUP_WINDOW_MIN`` minutes of now.

        Reads open time via ``basket_live._position_open_ts``'s own
        key-fallback chain (openTime/openTimestamp/open_ts/openedAt/ts —
        the exact shape ``dexter3.openapi_client._normalize_position_for_dexter3``
        emits) and ``basket_live._age_minutes`` for the ISO-epoch diff — the
        SAME helpers the basket engine already uses for basket age, no new
        parsing logic duplicated here. Missing/unparseable open-time is
        treated as WITHIN the window (conservative: the position IS
        currently open, so silently excluding it on a missing timestamp
        could hide a live twin entry).

        Fail-open on any exception (consistent with this file's other
        optional gates, e.g. ``_account_open_risk_cap_refusal``) — a bug in
        this check must never block an entry the rest of the pipeline
        already approved.
        """
        try:
            from dexter3.basket_live import _age_minutes, _position_open_ts

            window_min = _cross_lane_dedup_window_min()
            now_iso = utc_now_iso()
            for pos in open_positions or []:
                if not isinstance(pos, dict):
                    continue
                if position_symbol_of(pos) != symbol:
                    continue
                label = position_label_of(pos)
                if not label.startswith("dexter3"):
                    continue
                if is_our_position(pos):
                    continue  # own family -- the duplicate_label_position_open gate's job
                if position_side_of(pos) != side:
                    continue
                age_min = _age_minutes(_position_open_ts(pos), now_iso)
                if age_min is not None and age_min > window_min:
                    continue  # stale -- outside the dedup window
                return pos
            return None
        except Exception as exc:  # noqa: BLE001 - fail-open: a gate bug must never block an approved entry
            self._journal(
                symbol,
                "cross_lane_dedup_gate_failed",
                payload={"error": str(exc), "note": "fail-open — entry proceeds ungated by this check"},
            )
            return None

    # -- entry --------------------------------------------------------------

    def execute_entry(
        self,
        decision: Any,
        account_state: dict[str, Any] | None = None,
        *,
        today_entry_count: int = 0,
        today_losing_count: int = 0,
        basket_authorized: bool = False,
        risk_usd_override: float | None = None,
        smart_exit: dict[str, Any] | None = None,
        repair_context: dict[str, Any] | None = None,
        label_override: str | None = None,
    ) -> dict[str, Any]:
        """Place a demo micro-entry for an ``enter`` decision.

        Never raises on broker/MCP failure — every path returns a dict and
        journals an ``exec_events`` row; callers (shadow_runner) treat any
        non-"entered" action as "no order was placed, reason is in the
        return value and the journal".

        ``risk_usd_override`` (additive, default None): when provided by a
        caller (the Daily Mission Governor via
        ``dexter3/shadow_runner.py::_execute_live_entry``), sizing uses this
        USD risk amount INSTEAD OF ``self.config.risk_usd`` for this single
        call only — ``self.config`` itself is never mutated. This lets the
        governor shape per-entry size (streak ladder x session multiplier)
        without touching ``ExecutorConfig``'s own defaults/caps. Omitting it
        (the default) is byte-identical to pre-governor behavior.

        ``smart_exit`` (additive, default None -> byte-identical pre-smart-
        exit behavior): a dict shaped like
        ``dexter3.smart_exit.resolve_stop_regime``'s return value. When
        ``smart_exit['regime'] == 'disaster'``, the BROKER stop is placed at
        ``smart_exit['disaster_mult'] x sl_distance`` (WIDE — survives noise
        wicks per the proven backtest, see ``dexter3/smart_exit.py``) and
        sizing (``planned_volume_units``) is computed against THAT wider
        distance using the SAME ``risk_usd`` — this is what shrinks volume by
        ~``1/disaster_mult`` so $ risk to the disaster stop stays equal to
        the intended risk (never larger). TP is untouched either way. Any
        other value (None, missing, or ``regime == 'tight'``) places the
        stop at the decision's own tight ``sl`` exactly as before smart exit
        existed.

        ``repair_context`` (additive, default None -> byte-identical to
        pre-repair-lineage behavior; set ONLY by ``execute_repair_leg`` via
        its own ``_build_repair_lineage``): a pre-resolved dict of basket
        repair-lineage facts (parent_position_ids, parent_setup, basket_id,
        repair_side_mode, basket_agg_r_at_repair, basket_pnl_at_repair,
        level_lost, close_beyond) nested under the ``entry_executed`` journal
        row's own ``repair_context`` key (2026-07-15 repair-lineage-
        enrichment fix) so a repair leg's entry is forever traceable to the
        basket state that triggered it — never present for a normal (non-
        repair) entry.

        ``label_override`` (additive, default None -> byte-identical to the
        module-global ``LABEL``): the exact broker label to stamp on THIS
        order/journal row instead of ``LABEL``. Used by the Repair-Scalp
        Harvester (``dexter3.shadow_runner``, 2026-07-15) to suffix its scalp
        legs (``f"{LABEL}:{suffix}"``) so ``dexter3.basket_live.lane_positions``
        can exclude them from basket/OM aggregation while family-prefix
        ownership (duplicate-gate bypass via ``basket_authorized``, vanish
        reconcile, the H4 account-risk cap, governor realized) still
        recognizes them — a plain prefix check, so appending a suffix never
        breaks family matching. Ownership checks inside THIS call
        (``is_our_position`` for the duplicate-label probe and
        ``_resolve_new_position``) always use the module-global ``LABEL``
        unchanged — only the value SENT to the broker/journal changes.
        """
        symbol = str(decision.symbol)
        if str(decision.action) != "enter":
            return self._refuse(symbol, "decision_action_not_enter", action=decision.action)

        is_demo, demo_meta = self._is_demo_account(account_state)
        if not is_demo:
            return self._refuse(symbol, "account_not_confirmed_demo", **demo_meta)

        try:
            symbol_details = self.client.get_symbol_details(symbol)
            spot = self.client.get_spot_price(symbol)
            open_positions = self.client.get_positions()
        except (McpClientError, McpZombieError) as exc:
            return self._refuse(symbol, "mcp_read_failed_preflight", error=str(exc))

        refusal = self._pre_flight(
            decision,
            spot=spot,
            symbol_details=symbol_details,
            open_positions=open_positions,
            today_entry_count=today_entry_count,
            today_losing_count=today_losing_count,
            basket_authorized=basket_authorized,
            planned_risk_usd=(self.config.risk_usd if risk_usd_override is None else float(risk_usd_override)),
        )
        if refusal is not None:
            return refusal

        side = str(decision.side)
        entry = float(decision.entry)
        sl = float(decision.sl)
        tp = float(decision.tp)
        sl_distance = abs(entry - sl)
        tp_distance = abs(tp - entry)

        # -- smart exit (additive, default None -> unchanged behavior) ------
        # 'disaster' regime: broker stop widens to disaster_mult x sl_distance
        # and BOTH sizing and the pip distance sent to the broker use that
        # wider distance (with the SAME risk_usd) — this is what keeps $ risk
        # to the disaster stop equal to the intended tight-stop risk (size
        # shrinks ~1/disaster_mult). TP distance/price is never touched. A
        # 'tight' regime (or smart_exit=None/missing) keeps sl_distance as-is,
        # byte-identical to pre-smart-exit behavior.
        smart_exit_regime = str((smart_exit or {}).get("regime") or "tight")
        disaster_mult = float((smart_exit or {}).get("disaster_mult") or 1.0)
        if smart_exit_regime == "disaster" and disaster_mult > 1.0:
            broker_sl_distance = disaster_stop_distance(sl_distance, disaster_mult)
        else:
            broker_sl_distance = sl_distance

        risk_usd = self.config.risk_usd if risk_usd_override is None else float(risk_usd_override)
        # Cross-lane duplicate-entry guard, "downsize" mode (2026-07-15
        # twin-entry audit): halve (or DEXTER3_CROSS_LANE_DEDUP_MULT x) this
        # entry's risk when a foreign-lane duplicate is open, instead of
        # refusing outright -- see _pre_flight's "skip" branch for the
        # refusal counterpart. "off"/"skip" never reach here with a
        # multiplier != 1.0 (mode check below short-circuits for both).
        if _cross_lane_dedup_mode() == "downsize":
            cross_lane_dup = self._cross_lane_duplicate_match(symbol, side, open_positions)
            if cross_lane_dup is not None:
                mult = _cross_lane_dedup_mult()
                downsized_risk_usd = risk_usd * mult
                self._journal(
                    symbol,
                    "cross_lane_dedup_downsized",
                    payload={
                        "foreign_position_id": position_id_of(cross_lane_dup),
                        "foreign_label": position_label_of(cross_lane_dup),
                        "mult": mult,
                        "base_risk_usd": risk_usd,
                        "downsized_risk_usd": round(downsized_risk_usd, 6),
                    },
                )
                risk_usd = downsized_risk_usd
        volume, volume_meta = planned_volume_units(
            symbol_details, broker_sl_distance, risk_usd, self.config.max_volume_units
        )
        # -- min-volume x disaster-stop interaction (2026-07-10 live lesson) --
        # The disaster widening trades size-for-distance at equal $ risk
        # (size shrinks ~1/disaster_mult). At the volume FLOOR the size cannot
        # shrink, so widening only multiplies the real $ risk by disaster_mult
        # (VM cutover day: a ~9pt tight stop became an 18pt broker stop at the
        # 1oz XAU floor = -$18.11 realized on a $1.68-design trade). When the
        # sized volume clamps to minVolume AND the stop was widened, revert to
        # the TIGHT stop — that restores the equal-$-risk invariant the
        # disaster regime promises. DEXTER3_MIN_VOL_DISASTER_TIGHTEN=0 restores
        # legacy behavior.
        if (
            volume_meta.get("min_volume_clamped_up")
            and broker_sl_distance > sl_distance
            and str(os.environ.get("DEXTER3_MIN_VOL_DISASTER_TIGHTEN", "1") or "1").strip() != "0"
        ):
            self._journal(
                symbol,
                "min_vol_disaster_tightened",
                payload={
                    "widened_sl_distance": round(broker_sl_distance, 6),
                    "tight_sl_distance": round(sl_distance, 6),
                    "widened_min_vol_risk_usd": volume_meta.get("estimated_min_volume_risk_usd"),
                    "note": "at the volume floor, widening cannot shrink size — reverting to tight stop",
                },
            )
            broker_sl_distance = sl_distance
            volume, volume_meta = planned_volume_units(
                symbol_details, broker_sl_distance, risk_usd, self.config.max_volume_units
            )
        if volume <= 0:
            return self._refuse(symbol, "sizing_refused", volume_meta=volume_meta)
        if volume_meta.get("min_volume_clamped_up"):
            # -- absolute $ ceiling for volume-floored trades ----------------
            # The ratio cap (planned_volume_units) keys off the DESIGN risk,
            # which the sizing chain can crush to cents — a 1.5x ratio then
            # blocks every entry (cutover day: 27 refusals / 0 fills). This cap
            # keys off ACCOUNT economics instead: refuse only when the floored
            # trade's real risk exceeds an absolute dollar ceiling. Default 0
            # = off (legacy accept).
            try:
                abs_cap = float(os.environ.get("DEXTER3_MIN_VOLUME_RISK_ABS_CAP_USD", "0") or 0.0)
            except ValueError:
                abs_cap = 0.0
            est_floor_risk = float(volume_meta.get("estimated_min_volume_risk_usd", 0.0) or 0.0)
            # 2026-07-26 replay-vs-live audit — a SELF-INFLICTED adverse filter.
            # At the XAU 1-ounce floor real risk == stop distance, so this cap
            # refuses precisely the WIDE-STOP trades, and live data says those
            # are the winners: win rate by stop width runs 11% (<2pt) -> 33% ->
            # 41% -> 47% -> **60% at >=5pt (+$50.17)**, holding within-lane 3/3.
            # Replay counted those trades at full weight (flat $12/R), which is
            # part of why replay looked profitable and live did not. Refusing
            # them is therefore not prudence, it is deleting the best cohort.
            # DEXTER3_MIN_VOLUME_RISK_ABS_CAP_MULT scales the ceiling by the
            # trade's own volatility (stop distance / reference), so a genuinely
            # volatile setup is allowed proportionally more dollar risk while a
            # runaway stop is still refused. Unset => the flat cap, unchanged.
            eff_cap = abs_cap
            cap_mult_meta: dict[str, Any] = {}
            if abs_cap > 0:
                try:
                    cap_mult = float(os.environ.get("DEXTER3_MIN_VOLUME_RISK_ABS_CAP_MULT", "0") or 0.0)
                except ValueError:
                    cap_mult = 0.0
                if cap_mult > 1.0:
                    eff_cap = abs_cap * cap_mult
                    cap_mult_meta = {"abs_cap_base_usd": abs_cap, "abs_cap_mult": cap_mult}
            if eff_cap > 0 and est_floor_risk > eff_cap:
                return self._refuse(
                    symbol,
                    "min_volume_risk_exceeds_abs_cap",
                    estimated_min_volume_risk_usd=est_floor_risk,
                    abs_cap_usd=eff_cap,
                    volume_meta={**volume_meta, **cap_mult_meta},
                )
            self._journal(
                symbol,
                "sizing_min_volume_clamped",
                payload={
                    "estimated_min_volume_risk_usd": volume_meta["estimated_min_volume_risk_usd"],
                    "configured_risk_usd": self.config.risk_usd,
                    "note": "accepted micro-risk on demo per blueprint P2 spec",
                },
            )

        pip_size = float(symbol_details.get("pipSize", 0.01) or 0.01)
        sl_pips = _to_pips(broker_sl_distance, pip_size)
        tp_pips = _to_pips(tp_distance, pip_size)
        comment = f"{decision.setup}|{'|'.join(decision.reasons)}"[:55] if decision.reasons else str(decision.setup)
        self._journal(
            symbol,
            "smart_exit_classified",
            payload={
                "regime": smart_exit_regime,
                "disaster_mult": disaster_mult,
                "tight_sl": sl,
                "tight_sl_distance": round(sl_distance, 6),
                "broker_sl_distance": round(broker_sl_distance, 6),
                "smart_exit_meta": smart_exit or {},
            },
        )

        effective_label = str(label_override) if label_override else LABEL

        known_ids = {position_id_of(p) for p in open_positions if position_id_of(p) > 0}
        reconciled_pid = 0
        try:
            order = self.client.place_market_order(
                symbol=symbol,
                side=side,
                volume=volume,
                stop_loss_pips=sl_pips,
                take_profit_pips=tp_pips,
                label=effective_label,
                comment=comment,
            )
        except McpMutationUncertain as exc:
            # Transport died mid-order: it MAY have filled broker-side.
            # Reconcile instead of retrying (the 2026-07-05 double-fill).
            time.sleep(3.0)
            reconciled_pid = self._resolve_new_position(symbol, known_ids)
            if reconciled_pid <= 0:
                return self._refuse(
                    symbol, "mutation_uncertain_not_filled", error=str(exc), volume=volume
                )
            order = {"transport": "uncertain_reconciled", "detail": str(exc)}
        except (McpClientError, McpZombieError) as exc:
            return self._refuse(
                symbol, "place_market_order_failed", error=str(exc), volume=volume, sl_pips=sl_pips, tp_pips=tp_pips
            )

        deal_status = str(order.get("dealStatus") or order.get("status") or "").upper()
        if deal_status in {"REJECTED", "ERROR", "INTERNALLY_REJECTED"}:
            self._journal(symbol, "entry_rejected_by_broker", verified=False, payload={"order": order})
            return {"action": "rejected", "order": order}

        # The actual broker-side stop PRICE for this entry — tight sl for the
        # 'tight' regime (byte-identical to pre-smart-exit), or the widened
        # disaster price when this entry is under the 'disaster' regime. Used
        # for naked-position repair (must repair to what was INTENDED, not
        # the tight structural sl) and for journaling/OM regime lookups.
        broker_sl = (
            (entry - broker_sl_distance if side == "buy" else entry + broker_sl_distance)
            if smart_exit_regime == "disaster" and disaster_mult > 1.0
            else sl
        )

        pid = reconciled_pid or self._resolve_new_position(symbol, known_ids)
        post_positions = self._safe_get_positions()
        new_pos = next(
            (p for p in post_positions if position_id_of(p) == pid and is_our_position(p)), None
        )
        verified, verification = verify_entry_snapshot(new_pos, side, entry, volume)

        repair: dict[str, Any] | None = None
        if new_pos is not None and not verified:
            sl_missing = position_stop_loss_of(new_pos) <= 0
            tp_missing = position_take_profit_of(new_pos) <= 0
            if sl_missing or tp_missing:
                repair = self._repair_naked_position(symbol, pid, side, entry, broker_sl, tp, volume)
                verified = bool(repair.get("verified"))
                verification = dict(repair.get("verification") or verification)

        out = {
            "action": "entered",
            "symbol": symbol,
            "position_id": pid,
            "order": order,
            "verified": verified,
            "verification": verification,
            "repair": repair,
            "volume": volume,
            "volume_meta": volume_meta,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "setup": decision.setup,
            # Persist the exact lane identity. The Fable and Grok VM services
            # share the journal database, so empirical learning must never
            # pool their outcomes merely because they trade the same symbol.
            "label": effective_label,
            # session bucket at decision time — the learner's second key
            # (empirical_stats buckets by (setup, session)); getattr keeps
            # legacy/foreign decision objects without the field valid.
            "session": str(getattr(decision, "session", "") or ""),
            "smart_exit_regime": smart_exit_regime,
            "broker_sl": round(broker_sl, 6),
            "broker_sl_distance": round(broker_sl_distance, 6),
        }
        if repair_context:
            # 2026-07-15 repair-lineage-enrichment fix: nested (not flattened
            # into the top level) so a repair leg's entry_executed row keeps
            # the same top-level shape every OTHER entry_executed row has —
            # only its presence, never its absence, is new.
            out["repair_context"] = dict(repair_context)
        self._journal(symbol, "entry_executed", position_id=pid, verified=verified, payload=out)
        return out

    @staticmethod
    def _deal_position_id(deal: dict[str, Any]) -> int:
        """Deal's position id across transport shapes (camelCase local-MCP /
        openapi-normalized ``positionId``, snake_case daemon ``position_id``)."""
        for key in ("positionId", "position_id"):
            raw = deal.get(key)
            if raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        return 0

    def _entry_context_of(self, symbol: str, position_id: int) -> dict[str, Any]:
        """Look up this position's OWN entry_executed journal row -> the
        learner keys (setup, session) recorded at entry time. Empty dict when
        not found — callers must treat the keys as best-effort."""
        try:
            cur = self._conn.execute(
                "SELECT payload_json FROM exec_events WHERE symbol = ? AND event = 'entry_executed' "
                "AND position_id = ? ORDER BY id DESC LIMIT 1",
                (str(symbol), int(position_id)),
            )
            row = cur.fetchone()
            if not row:
                return {}
            payload = json.loads(row[0] or "{}")
            return {
                "setup": str(payload.get("setup") or "") or None,
                "session": str(payload.get("session") or "") or None,
                "label": str(payload.get("label") or "") or None,
            }
        except Exception:  # noqa: BLE001 - learner enrichment must never break closes
            return {}

    def _resolve_new_position(self, symbol: str, known_ids: set[int]) -> int:
        """Re-read positions once looking for a new id carrying our label.

        Real deployments retry with backoff (see btc_scalp_monitor's
        ``resolve_position_after_entry``); tests drive a mocked transport
        where the position appears on the very next read, so a single
        best-effort read (no sleep) keeps unit tests fast while the shape
        stays identical for a live client to layer retries on top of later.
        """
        # 2026-07-26 audit: a single blind read meant that whenever
        # get_positions had not yet propagated the fill, pid stayed 0 -> the
        # naked-repair branch was SKIPPED entirely (voiding this module's
        # "never leave naked" promise) and insert_exec_event wrote a NULL
        # position_id, which vanish-reconcile can never resolve
        # (`AND e.position_id > 0`), so the learner stayed permanently blind to
        # that trade. Bounded retry: the propagation window is ~1s, and the
        # docstring above always intended a live client to layer retries here.
        attempts = max(1, int(_env_int("DEXTER3_RESOLVE_POSITION_TRIES", 3)))
        delay = max(0.0, _env_float_local("DEXTER3_RESOLVE_POSITION_DELAY_SEC", 0.7))
        positions: list[dict[str, Any]] = []
        for attempt in range(attempts):
            try:
                positions = self.client.get_positions()
            except (McpClientError, McpZombieError):
                positions = []
            if any(position_symbol_of(p) == symbol and is_our_position(p)
                   and position_id_of(p) > 0 and position_id_of(p) not in known_ids
                   for p in positions):
                break
            if attempt < attempts - 1 and delay > 0:
                time.sleep(delay)
        for pos in positions:
            if position_symbol_of(pos) == symbol and is_our_position(pos):
                pid = position_id_of(pos)
                if pid > 0 and pid not in known_ids:
                    return pid
        return 0

    def _safe_get_positions(self) -> list[dict[str, Any]]:
        try:
            return self.client.get_positions()
        except (McpClientError, McpZombieError):
            return []

    def _repair_naked_position(
        self,
        symbol: str,
        position_id: int,
        side: str,
        entry: float,
        sl: float,
        tp: float,
        volume: float,
    ) -> dict[str, Any]:
        """SL/TP missing after entry -> amend; amend fails -> close. Never leave naked."""
        if position_id <= 0:
            self._journal(
                symbol,
                "naked_position_unresolvable",
                verified=False,
                payload={"reason": "no_position_id_to_repair"},
            )
            return {"verified": False, "verification": {"reason": "no_position_id_to_repair"}}
        uncertain_note = ""
        try:
            try:
                self.client.amend_position(position_id, stop_loss=sl, take_profit=tp)
            except McpMutationUncertain as exc:
                # 2026-07-25 audit fix: McpMutationUncertain SUBCLASSES
                # McpClientError, so the broad handler below used to swallow it
                # and fall straight through to close_position — MARKET-CLOSING A
                # HEALTHY POSITION whenever the amend had actually reached the
                # broker and applied but its HTTP response outran the daemon
                # timeout. That false-negative is live-observed on this account
                # (27 amend_failed lines in 3 days, with fresh reads confirming
                # the SL really did move). The sibling amend_lane_sl_tp already
                # handles the identical signal correctly; this mirrors it —
                # settle, then let the authoritative re-read below decide.
                uncertain_note = str(exc)
                time.sleep(2.0)
            positions = self._safe_get_positions()
            pos = next((p for p in positions if position_id_of(p) == position_id), None)
            verified, verification = verify_entry_snapshot(
                pos, side, entry, volume, expect_sl=sl, expect_tp=tp
            )
            if uncertain_note:
                verification["amend_uncertain"] = uncertain_note
            self._journal(
                symbol,
                "naked_position_repaired_via_amend",
                position_id=position_id,
                verified=verified,
                payload={"verification": verification},
            )
            if verified:
                return {"verified": True, "verification": verification, "action": "amended"}
        except (McpClientError, McpZombieError) as exc:
            self._journal(
                symbol,
                "naked_position_amend_failed",
                position_id=position_id,
                verified=False,
                payload={"error": str(exc)},
            )

        # Amend failed or still not verified -> close rather than leave naked.
        try:
            close_result = self.client.close_position(position_id)
            self._journal(
                symbol,
                "naked_position_closed",
                position_id=position_id,
                verified=False,
                payload={"close_result": close_result, "reason": "amend_repair_failed_or_unverified"},
            )
            return {
                "verified": False,
                "verification": {"reason": "closed_after_failed_repair"},
                "action": "closed",
                "close_result": close_result,
            }
        except (McpClientError, McpZombieError) as exc:
            self._journal(
                symbol,
                "naked_position_close_failed",
                position_id=position_id,
                verified=False,
                payload={"error": str(exc)},
            )
            return {
                "verified": False,
                "verification": {"reason": "close_also_failed", "error": str(exc)},
                "action": "close_failed",
            }

    # -- lane helpers ---------------------------------------------------

    def close_lane_position(self, position_id: int, reason: str) -> dict[str, Any]:
        """Close a position — refuses if it does not carry our LABEL (peer isolation)."""
        positions = self._safe_get_positions()
        pos = next((p for p in positions if position_id_of(p) == int(position_id)), None)
        if pos is None:
            return self._refuse("unknown", "position_not_found", position_id=position_id)
        symbol = position_symbol_of(pos)
        if not is_our_position(pos):
            return self._refuse(
                symbol, "refused_foreign_label", position_id=position_id, label=position_label_of(pos)
            )
        try:
            result = self.client.close_position(int(position_id))
        except McpMutationUncertain as exc:
            # close MAY have executed — the post re-read below is the truth
            time.sleep(2.0)
            result = {"transport": "uncertain_reconciled", "detail": str(exc)}
        except (McpClientError, McpZombieError) as exc:
            return self._refuse(symbol, "close_position_failed", position_id=position_id, error=str(exc))
        post_positions = self._safe_get_positions()
        still_open = any(position_id_of(p) == int(position_id) for p in post_positions)
        verified = not still_open
        # -- learner payload (2026-07-10: make self-learning real) ----------
        # empirical_stats needs (setup, session, pnl) on this event or it
        # skips the row entirely (the audit's "learner reads zero"). setup +
        # session come from this position's own entry_executed row; pnl is
        # the pre-close floating PnL snapshot (market close -> sign-accurate,
        # which is all the Laplace win-rate needs). All best-effort — a miss
        # journals None and the learner skips just that row, never raises.
        from dexter3.basket_live import _position_pnl

        entry_ctx = self._entry_context_of(symbol, int(position_id))
        pnl_snapshot = _position_pnl(pos)
        self._journal(
            symbol,
            "lane_position_closed",
            position_id=position_id,
            verified=verified,
            payload={
                "reason": reason,
                "result": result,
                "setup": entry_ctx.get("setup"),
                "session": entry_ctx.get("session"),
                "label": entry_ctx.get("label"),
                "pnl": pnl_snapshot,
                "exit_reason": reason,
            },
        )
        return {"action": "closed" if verified else "close_unverified", "position_id": position_id, "result": result}

    def reconcile_vanished_lane_positions(
        self, symbol: str, open_positions: list[dict[str, Any]] | None, *, max_candidates: int = 20
    ) -> list[dict[str, Any]]:
        """Journal learner outcomes for lane positions the BROKER closed (SL/TP).

        Broker-side closes never pass through ``close_lane_position``, so
        without this reconcile those outcomes stay invisible to
        ``empirical_stats`` (the biggest coverage gap after 2026-07-10's
        learner repair). Candidates are this executor's OWN ``entry_executed``
        rows (label FAMILY-matches ``self._own_label_family`` — see
        ``label_matches_family``; foreign-lane and unlabeled rows are never
        candidates, same exclusion rule empirical_stats.py's
        ``_exec_events_outcome_rows`` uses) with NO close row yet — the
        journal write below IS the dedup marker, so every vanish is recorded
        exactly once, restart-safe.

        ``open_positions`` must be the CURRENT label-filtered lane list the
        caller already fetched this bar (None = unknown broker state -> no-op).
        Realized pnl is summed from closing deals when available; a transient
        deals failure skips the whole round (retried next bar), and a
        candidate with NO deal rows at all in the fetched window is deferred
        individually (retried next bar too) rather than journaling a
        pnl=0.0 guess — writing a guessed pnl was exactly how a foreign
        lane's still-open position (only its zero-pnl entry leg visible in
        the window) got mis-journaled as a real close (2026-07-15 cross-lane
        vanish incident; the label filter above independently closes that
        hole too, this is belt-and-suspenders for the own-lane case). Never
        raises — a reconcile bug must not block the trading loop.
        """
        if open_positions is None:
            return []
        try:
            open_ids = {position_id_of(p) for p in open_positions}
            cur = self._conn.execute(
                "SELECT e.position_id, e.payload_json FROM exec_events e "
                "WHERE e.symbol = ? AND e.event = 'entry_executed' "
                "AND e.position_id IS NOT NULL AND e.position_id > 0 "
                "AND NOT EXISTS (SELECT 1 FROM exec_events c WHERE c.event IN "
                "('lane_position_closed', 'naked_position_closed') "
                "AND c.position_id = e.position_id) "
                "ORDER BY e.id DESC LIMIT ?",
                (str(symbol), int(max_candidates)),
            )
            candidates: list[tuple[int, dict[str, Any]]] = []
            for pid, payload_json in cur.fetchall():
                pid = int(pid)
                if pid in open_ids:
                    continue
                try:
                    entry_payload = json.loads(payload_json or "{}")
                except json.JSONDecodeError:
                    entry_payload = {}
                row_label = str(entry_payload.get("label") or "")
                if not label_matches_family(row_label, self._own_label_family):
                    continue  # foreign lane or unlabeled entry row — never ours to reconcile
                candidates.append((pid, entry_payload))
            if not candidates:
                return []
            try:
                deals = self.client.get_deals(200) or []
            except Exception as exc:  # noqa: BLE001 - transient deals failure -> retry next bar
                self._journal(
                    symbol,
                    "vanish_reconcile_deferred",
                    verified=False,
                    payload={"error": str(exc), "pending_position_ids": [pid for pid, _ in candidates]},
                )
                return []
            pnl_by_pid: dict[int, float] = {}
            for d in deals:
                if not isinstance(d, dict):
                    continue
                # Only CLOSE legs carry realized pnl. The openapi client
                # stamps netProfit=0.0 on ENTRY legs too (pnl_usd absent ->
                # 0.0), so an own-lane open position's entry leg could still
                # satisfy the defer check and journal a pnl=0.0 guess. When
                # the deal shape exposes a close-detail marker, trust it;
                # shapes without one (local MCP) keep the legacy sum.
                if any(k in d for k in ("hasCloseDetail", "has_close_detail", "closePositionDetail")):
                    if not (d.get("hasCloseDetail") or d.get("has_close_detail") or d.get("closePositionDetail")):
                        continue
                dpid = self._deal_position_id(d)
                if dpid <= 0:
                    continue
                raw = d.get("netProfit", d.get("net_profit"))
                try:
                    pnl_by_pid[dpid] = pnl_by_pid.get(dpid, 0.0) + float(raw)
                except (TypeError, ValueError):
                    continue
            out: list[dict[str, Any]] = []
            for pid, entry_payload in candidates:
                if pid not in pnl_by_pid:
                    # No deal row at all for this position in the fetched
                    # window — defer to next round rather than guess pnl=0.0;
                    # no journal write means no dedup marker, so it stays a
                    # candidate and is retried as soon as the deal appears.
                    continue
                pnl = pnl_by_pid[pid]
                record = {
                    "reason": "broker_side_close_reconciled",
                    "setup": str(entry_payload.get("setup") or "") or None,
                    "session": str(entry_payload.get("session") or "") or None,
                    "label": str(entry_payload.get("label") or "") or None,
                    "pnl": pnl,
                    "exit_reason": "broker_side_close",
                    "reconciled": True,
                }
                self._journal(symbol, "lane_position_closed", position_id=pid, verified=True, payload=record)
                out.append({"position_id": pid, **record})
            return out
        except Exception:  # noqa: BLE001 - reconcile must never break the loop
            return []

    def amend_lane_sl_tp(
        self, position_id: int, sl: float | None, tp: float | None
    ) -> dict[str, Any]:
        """Amend SL/TP — refuses if the position does not carry our LABEL (peer isolation)."""
        positions = self._safe_get_positions()
        pos = next((p for p in positions if position_id_of(p) == int(position_id)), None)
        if pos is None:
            return self._refuse("unknown", "position_not_found", position_id=position_id)
        symbol = position_symbol_of(pos)
        if not is_our_position(pos):
            return self._refuse(
                symbol, "refused_foreign_label", position_id=position_id, label=position_label_of(pos)
            )
        side = position_side_of(pos)
        entry = float(pos.get("entryPrice") or pos.get("price") or 0.0)
        volume = position_volume_of(pos)
        try:
            self.client.amend_position(int(position_id), stop_loss=sl, take_profit=tp)
        except McpMutationUncertain as exc:
            # amend MAY have applied — the post re-read verification decides
            time.sleep(2.0)
            log_note = str(exc)  # noqa: F841 - captured in journal payload below via verification
        except (McpClientError, McpZombieError) as exc:
            return self._refuse(symbol, "amend_failed", position_id=position_id, error=str(exc))
        post_positions = self._safe_get_positions()
        post_pos = next((p for p in post_positions if position_id_of(p) == int(position_id)), None)
        # 2026-07-25 audit fix: pass the REQUESTED prices so a broker-refused
        # amend can no longer report verified=True merely because the OLD stop
        # is still on the correct side of entry. Only the values actually asked
        # for are checked (sl/tp may be None = "leave unchanged").
        verified, verification = verify_entry_snapshot(
            post_pos, side, entry, volume,
            expect_sl=sl if sl is not None else None,
            expect_tp=tp if tp is not None else None,
        )
        self._journal(
            symbol,
            "lane_position_amended",
            position_id=position_id,
            verified=verified,
            payload={"sl": sl, "tp": tp, "verification": verification},
        )
        return {"action": "amended", "position_id": position_id, "verified": verified, "verification": verification}

    # -- basket engine surface (HUNT MODE) ------------------------------------

    def execute_close_all(self, position_ids: list[int], reason: str) -> dict[str, Any]:
        """Close every lane position in ``position_ids`` (close-all-in-profit /
        cap-stop resolution). Foreign labels are refused per-position by
        ``close_lane_position`` — a stray peer id in the list cannot be closed."""
        results: list[dict[str, Any]] = []
        closed = 0
        for pid in position_ids:
            res = self.close_lane_position(int(pid), reason=reason)
            results.append(res)
            if res.get("action") == "closed":
                closed += 1
        all_closed = closed == len(position_ids) and bool(position_ids)
        self._journal(
            "basket",
            "basket_close_all",
            position_id=None,
            verified=all_closed,
            payload={"reason": reason, "requested": len(position_ids), "closed": closed},
        )
        return {
            "action": "closed_all" if all_closed else "close_all_partial",
            "requested": len(position_ids),
            "closed": closed,
            "results": results,
        }

    def _build_repair_lineage(self, decision: Any, repair_context: dict[str, Any] | None) -> dict[str, Any]:
        """Resolve+normalize repair-lineage facts threaded onto BOTH the
        ``entry_executed`` and ``basket_repair_leg`` journal rows for a
        repair/hedge leg (2026-07-15 repair-lineage-enrichment fix — closes
        defect #3 of docs/AGENT_SYNC_BOARD.md's 2026-07-15 ~07:30Z "OM
        ROUND-TRIP INCIDENT": today's ``basket_repair_leg`` payload carried
        no parent link/session at all).

        ``repair_context`` (caller-supplied, from ``shadow_runner.py`` — the
        only place that has the basket-level aggregate/evidence facts this
        method cannot derive itself) is expected to optionally carry:
        ``parent_position_ids`` (list[int], oldest leg first),
        ``basket_id`` (the basket's own identity key — this repo already
        uses ``oldest_open_ts`` for that, see ``shadow_runner._basket_runtime_for``),
        ``basket_side`` (str), ``basket_agg_r_at_repair`` (float),
        ``basket_pnl_at_repair`` (float), ``level_lost``/``close_beyond``
        (bool evidence flags). ``parent_setup`` is resolved HERE from the
        oldest parent leg's own ``entry_executed`` row via
        ``_entry_context_of`` — the one piece of lineage only this executor
        (owner of the journal connection) can look up. Never raises: any
        resolution failure degrades that one field to ``None`` rather than
        blocking the repair leg itself."""
        ctx = dict(repair_context or {})
        parent_ids: list[int] = []
        for raw_id in (ctx.get("parent_position_ids") or []):
            try:
                pid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if pid > 0:
                parent_ids.append(pid)
        parent_setup = None
        if parent_ids:
            try:
                first_ctx = self._entry_context_of(str(getattr(decision, "symbol", "") or ""), parent_ids[0])
                parent_setup = first_ctx.get("setup")
            except Exception:  # noqa: BLE001 - lineage enrichment must never block a repair leg
                parent_setup = None
        basket_side = str(ctx.get("basket_side") or "").strip().lower() or None
        decision_side = str(getattr(decision, "side", "") or "").strip().lower() or None
        repair_side_mode = None
        if basket_side and decision_side:
            # Same literal values as dexter3.basket_live.REPAIR_MODE_SAME_SIDE
            # / REPAIR_MODE_HEDGE_LOCK — NOT imported (this module never
            # imports basket_live, mirroring basket_live's own documented
            # "no cross-import of a peer module's internals" convention),
            # just the identical strings so payload data stays consistent
            # across both modules without coupling them.
            repair_side_mode = "same_side" if decision_side == basket_side else "hedge_lock"
        return {
            "parent_position_ids": parent_ids,
            "parent_setup": parent_setup,
            "basket_id": ctx.get("basket_id"),
            "repair_side_mode": repair_side_mode,
            "basket_agg_r_at_repair": ctx.get("basket_agg_r_at_repair"),
            "basket_pnl_at_repair": ctx.get("basket_pnl_at_repair"),
            "level_lost": ctx.get("level_lost"),
            "close_beyond": ctx.get("close_beyond"),
        }

    def execute_repair_leg(
        self,
        decision: Any,
        account_state: dict[str, Any] | None = None,
        *,
        today_entry_count: int = 0,
        today_losing_count: int = 0,
        risk_usd_override: float | None = None,
        repair_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add a basket repair/hedge leg: the ONLY path that may open a second
        position on a symbol we already hold. All other pre-flight gates
        (demo, quote, sidedness, sizing, daily caps) still apply unchanged.

        ``risk_usd_override`` is forwarded to ``execute_entry`` unchanged —
        see its docstring (additive, default None -> existing behavior).

        ``repair_context`` (additive, default None -> byte-identical
        pre-lineage-enrichment behavior): see ``_build_repair_lineage`` for
        the caller-supplied shape. The RESOLVED lineage (never the raw
        ``repair_context``) is threaded onto both this leg's own
        ``entry_executed`` row (via ``execute_entry``'s own
        ``repair_context`` param) and the ``basket_repair_leg`` row below."""
        lineage = self._build_repair_lineage(decision, repair_context)
        result = self.execute_entry(
            decision,
            account_state,
            today_entry_count=today_entry_count,
            today_losing_count=today_losing_count,
            basket_authorized=True,
            risk_usd_override=risk_usd_override,
            repair_context=lineage,
        )
        self._journal(
            str(getattr(decision, "symbol", "unknown")),
            "basket_repair_leg",
            position_id=result.get("position_id"),
            verified=bool(result.get("verified")),
            payload={"entry_result_action": result.get("action"), **lineage},
        )
        return result
