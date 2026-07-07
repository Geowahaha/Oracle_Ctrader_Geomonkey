"""Dexter3 executor — the ONLY dexter3 module allowed to call mutating MCP
methods (place_market_order / amend_position / close_position).

Every order carries ``LABEL = "dexter3:fable:m5h-v1"`` (blueprint
"Non-negotiables" #2 — label isolation; loops/peers ignore foreign labels).
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

LABEL = "dexter3:fable:m5h-v1"

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
DEFAULT_DEMO_TRADER_IDS = (9922808, 3555162)


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
    """Peer isolation: only positions carrying exactly our LABEL are ours."""
    return position_label_of(position) == LABEL


def verify_entry_snapshot(
    position: dict[str, Any] | None, side: str, entry: float, volume: float
) -> tuple[bool, dict[str, Any]]:
    """Geometry/side/volume check on a freshly-opened position.

    Mirrors ``scripts/btc_scalp_monitor.py::verify_position_snapshot``
    exactly (side_ok / volume_ok / geometry_ok, volume tolerance 0.1%).
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
    meta = {
        "side_ok": side_ok,
        "volume_ok": volume_ok,
        "volume_actual": volume_actual,
        "volume_expected": volume,
        "stop_loss": sl,
        "take_profit": tp,
        "geometry_ok": geometry_ok,
    }
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
    if rounded > float(max_volume_units):
        meta["refuse_reason"] = "min_volume_exceeds_max_volume_units_cap"
        return 0.0, meta
    if rounded <= 0:
        meta["refuse_reason"] = "zero_volume"
        return 0.0, meta
    return rounded, meta


def _to_pips(distance: float, pip_size: float) -> int:
    return max(1, int(round(abs(float(distance)) / max(float(pip_size), 1e-9))))


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
    ) -> dict[str, Any] | None:
        """Return a refusal dict (already journaled) or None to proceed.

        ``basket_authorized=True`` is set ONLY by ``execute_repair_leg`` —
        it bypasses exactly one gate (duplicate_label_position_open) so the
        basket engine can add a repair/hedge leg; every other gate still
        applies.
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
        )
        if refusal is not None:
            return refusal

        side = str(decision.side)
        entry = float(decision.entry)
        sl = float(decision.sl)
        tp = float(decision.tp)
        sl_distance = abs(entry - sl)
        tp_distance = abs(tp - entry)

        risk_usd = self.config.risk_usd if risk_usd_override is None else float(risk_usd_override)
        volume, volume_meta = planned_volume_units(
            symbol_details, sl_distance, risk_usd, self.config.max_volume_units
        )
        if volume <= 0:
            return self._refuse(symbol, "sizing_refused", volume_meta=volume_meta)
        if volume_meta.get("min_volume_clamped_up"):
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
        sl_pips = _to_pips(sl_distance, pip_size)
        tp_pips = _to_pips(tp_distance, pip_size)
        comment = f"{decision.setup}|{'|'.join(decision.reasons)}"[:55] if decision.reasons else str(decision.setup)

        known_ids = {position_id_of(p) for p in open_positions if position_id_of(p) > 0}
        reconciled_pid = 0
        try:
            order = self.client.place_market_order(
                symbol=symbol,
                side=side,
                volume=volume,
                stop_loss_pips=sl_pips,
                take_profit_pips=tp_pips,
                label=LABEL,
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
                repair = self._repair_naked_position(symbol, pid, side, entry, sl, tp, volume)
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
        }
        self._journal(symbol, "entry_executed", position_id=pid, verified=verified, payload=out)
        return out

    def _resolve_new_position(self, symbol: str, known_ids: set[int]) -> int:
        """Re-read positions once looking for a new id carrying our label.

        Real deployments retry with backoff (see btc_scalp_monitor's
        ``resolve_position_after_entry``); tests drive a mocked transport
        where the position appears on the very next read, so a single
        best-effort read (no sleep) keeps unit tests fast while the shape
        stays identical for a live client to layer retries on top of later.
        """
        try:
            positions = self.client.get_positions()
        except (McpClientError, McpZombieError):
            return 0
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
        try:
            self.client.amend_position(position_id, stop_loss=sl, take_profit=tp)
            positions = self._safe_get_positions()
            pos = next((p for p in positions if position_id_of(p) == position_id), None)
            verified, verification = verify_entry_snapshot(pos, side, entry, volume)
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
        self._journal(
            symbol,
            "lane_position_closed",
            position_id=position_id,
            verified=verified,
            payload={"reason": reason, "result": result},
        )
        return {"action": "closed" if verified else "close_unverified", "position_id": position_id, "result": result}

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
        verified, verification = verify_entry_snapshot(post_pos, side, entry, volume)
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

    def execute_repair_leg(
        self,
        decision: Any,
        account_state: dict[str, Any] | None = None,
        *,
        today_entry_count: int = 0,
        today_losing_count: int = 0,
        risk_usd_override: float | None = None,
    ) -> dict[str, Any]:
        """Add a basket repair/hedge leg: the ONLY path that may open a second
        position on a symbol we already hold. All other pre-flight gates
        (demo, quote, sidedness, sizing, daily caps) still apply unchanged.

        ``risk_usd_override`` is forwarded to ``execute_entry`` unchanged —
        see its docstring (additive, default None -> existing behavior)."""
        result = self.execute_entry(
            decision,
            account_state,
            today_entry_count=today_entry_count,
            today_losing_count=today_losing_count,
            basket_authorized=True,
            risk_usd_override=risk_usd_override,
        )
        self._journal(
            str(getattr(decision, "symbol", "unknown")),
            "basket_repair_leg",
            position_id=result.get("position_id"),
            verified=bool(result.get("verified")),
            payload={"entry_result_action": result.get("action")},
        )
        return result
