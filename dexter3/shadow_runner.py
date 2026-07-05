#!/usr/bin/env python3
"""Dexter3 M5 shadow/live loop — lens -> brain -> journal (+ demo micro-entries in Phase 2).

Without ``--live``, this file behaves EXACTLY as Phase 1: a pure observation
loop that, on every NEW M5 close per symbol, fetches M5/M15/H1 trendbars +
spot via ``dexter3.mcp_client`` (read-only), runs ``market_lens`` ->
``hunter_brain.decide()``, journals the decision, and drives a paper
(simulated-fill) ``BasketManager`` so basket-management telemetry
accumulates before any money is at risk.

Phase 2 adds an OPT-IN live micro-entry path: passing ``--live`` AND setting
the environment variable ``DEXTER3_LIVE=1`` (double opt-in — either alone is
not enough) constructs a ``dexter3.executor.Dexter3Executor`` and invokes it
for the newest bar's ``action == 'enter'`` decisions only (never for
historical catch-up bars). The executor itself refuses to trade unless the
bound account is confirmed demo (see ``dexter3/executor.py``), so a live
loop pointed at the wrong account still cannot place an order.

Every ``LEARNING_REFRESH_MINUTES`` (default 15), the loop also refreshes
empirical p_win_est stats (``dexter3.empirical_stats``) and evaluates
pending skip decisions for the "fear cost" KPI
(``dexter3.skip_evaluator``) — both additive, both no-ops until enough
closed trades/skips exist in the journal.

CLI:
    python -m dexter3.shadow_runner --symbols XAUUSD,BTCUSD --once
    python -m dexter3.shadow_runner --symbols XAUUSD,BTCUSD --loop --poll-sec 20
    DEXTER3_LIVE=1 python -m dexter3.shadow_runner --symbols BTCUSD --loop --live

Single-instance lock: ``data/runtime/dexter3_loop.lock`` (own lock, separate
from the live scalp loops' locks — blueprint "Non-negotiables" #2). Stale
lock (pid no longer running) is taken over automatically.

State: ``data/runtime/dexter3_shadow_state.json`` tracks the last-seen M5
close id per symbol so each M5 close triggers exactly one decision.

Logging: one line per decision to stdout AND
``data/runtime/dexter3_shadow.log`` (UTF-8, Thai-safe).

Failure handling:
  - ``McpZombieError``: log loudly, sleep 60s, continue the loop.
  - any other exception: log full traceback, continue the loop (never die).
  - ``KeyboardInterrupt``: release the lock and exit cleanly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dexter3 import empirical_stats, hunter_brain, market_lens, skip_evaluator
from dexter3.basket_manager import BasketConfig, BasketManager, Leg
from dexter3.decision_journal import DecisionJournal
from dexter3.executor import LABEL as LIVE_ORDER_LABEL
from dexter3.executor import Dexter3Executor, ExecutorConfig
from dexter3.mcp_client import Dexter3McpClient, McpClientError, McpZombieError

RUNTIME = ROOT / "data" / "runtime"
STATE_FILE = RUNTIME / "dexter3_shadow_state.json"
LOG_FILE = RUNTIME / "dexter3_shadow.log"
LOCK_FILE = RUNTIME / "dexter3_loop.lock"

DEFAULT_SYMBOLS = ("XAUUSD", "BTCUSD")
DEFAULT_POLL_SEC = 20
M5_BAR_SEC = 300
MCP_ZOMBIE_SLEEP_SEC = 60
MIN_M5_BARS = 60
M15_BARS_NEEDED = 60
H1_BARS_NEEDED = 60

# -- Phase 2: live micro-entry double opt-in ---------------------------------
# Placing a real (demo) order requires BOTH the --live CLI flag AND this env
# var set to "1" — blueprint P2 "double opt-in" so a stray --live in a test
# harness or a copy-pasted command can never place an order by accident.
DEXTER3_LIVE_ENV_VAR = "DEXTER3_LIVE"

# -- Phase 2: periodic learning-loop refresh ---------------------------------
# "every N cycles (default every 15 min)" per spec section 5; expressed in
# cycles (not minutes) because run_once has no wall-clock of its own — the
# caller's poll interval determines how many cycles fit in 15 minutes.
LEARNING_REFRESH_MINUTES = 15


def _learning_refresh_every_cycles(poll_sec: int) -> int:
    return max(1, int((LEARNING_REFRESH_MINUTES * 60) / max(1, poll_sec)))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# single-instance lock (own lock file — never the live loops' lock)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_loop_lock() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old_pid = 0
        if old_pid > 0 and _pid_alive(old_pid):
            raise SystemExit(f"another dexter3 shadow loop is running (pid={old_pid})")
        # stale lock (pid dead or unreadable) — take it over
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")


def release_loop_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# shadow state (last-seen M5 close id per symbol) — M5-close detection
# ---------------------------------------------------------------------------


def load_shadow_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"symbols": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"symbols": {}}
        data.setdefault("symbols", {})
        return data
    except (OSError, json.JSONDecodeError):
        return {"symbols": {}}


def save_shadow_state(state: dict[str, Any]) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def is_new_m5_close(state: dict[str, Any], symbol: str, bars_m5: list[dict[str, Any]]) -> tuple[bool, str | None]:
    """Return (is_new, close_ts) for the last bar in bars_m5 vs. the tracked state.

    Pure w.r.t. its inputs except for reading ``state`` — does not mutate
    it. Callers must call ``mark_m5_close_seen`` after successfully
    processing the close, so a crash mid-processing does not silently skip
    the bar on the next tick.
    """
    if not bars_m5:
        return False, None
    last_ts = str(bars_m5[-1].get("ts") or "")
    if not last_ts:
        return False, None
    sym_state = state.get("symbols", {}).get(symbol, {})
    last_seen = str(sym_state.get("last_m5_close_ts") or "")
    return last_ts != last_seen, last_ts


def mark_m5_close_seen(state: dict[str, Any], symbol: str, close_ts: str) -> None:
    symbols = state.setdefault("symbols", {})
    sym_state = symbols.setdefault(symbol, {})
    sym_state["last_m5_close_ts"] = close_ts
    sym_state["last_seen_at"] = utc_now_iso()


def _iso_to_epoch(ts: str) -> float:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0


FRESHNESS_GRACE_SEC = 10
FRESHNESS_RETRIES = 3
FRESHNESS_RETRY_SLEEP_SEC = 7
FRESHNESS_MAX_GAP_SEC = 1800  # newest bar older than this = market closed/idle, no retries
MAX_CATCHUP_BARS = 6


def pending_m5_closes(
    state: dict[str, Any], symbol: str, bars_m5: list[dict[str, Any]], max_catchup: int = MAX_CATCHUP_BARS
) -> list[int]:
    """Indices (oldest→newest) of completed bars not yet decided.

    The blueprint contract is one decision per M5 close with no silent gaps,
    so a data gap must yield ALL missed bars, not only the newest. First run
    for a symbol decides only the newest bar (no history replay); gaps larger
    than ``max_catchup`` are truncated to the newest bars so a long MCP outage
    cannot trigger a replay storm.
    """
    if not bars_m5:
        return []
    sym_state = state.get("symbols", {}).get(symbol, {})
    last_seen = str(sym_state.get("last_m5_close_ts") or "")
    if not last_seen:
        return [len(bars_m5) - 1]
    pending = [i for i, b in enumerate(bars_m5) if str(b.get("ts") or "") > last_seen]
    return pending[-max_catchup:]


def fetch_fresh_m5(mcp: Dexter3McpClient, symbol: str, count: int = MIN_M5_BARS) -> list[dict[str, Any]]:
    """Fetch M5 bars, retrying briefly when the newest completed bar is missing.

    The local MCP can serve a snapshot one full bar stale (observed
    2026-07-05: the 08:15 bar stayed absent until 08:25 while the 08:20 bar
    then appeared within 19s). When ``now`` is past a boundary plus grace and
    the response lacks that bar, refetch with a varied ``count`` so any
    request-shaped cache is bypassed. Idle markets (weekend XAU) are exempt:
    retries fire only when the newest bar is within FRESHNESS_MAX_GAP_SEC of
    the expected one.
    """
    bars = mcp.get_trendbars(symbol, "m5", count)
    for attempt in range(1, FRESHNESS_RETRIES + 1):
        if not bars:
            return bars
        now_epoch = datetime.now(timezone.utc).timestamp()
        expected_open = (int(now_epoch - FRESHNESS_GRACE_SEC) // M5_BAR_SEC * M5_BAR_SEC) - M5_BAR_SEC
        gap = expected_open - _iso_to_epoch(str(bars[-1].get("ts") or ""))
        if gap <= 0 or gap > FRESHNESS_MAX_GAP_SEC:
            return bars
        time.sleep(FRESHNESS_RETRY_SLEEP_SEC)
        bars = mcp.get_trendbars(symbol, "m5", count + attempt)
    if bars:
        log_line(
            f"{utc_now_iso()} {symbol} data_stale newest_m5={bars[-1].get('ts')} "
            f"(expected newer bar; will catch up on a later poll)"
        )
    return bars[-count:] if len(bars) > count else bars


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------


def log_line(text: str) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    print(text)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def log_decision_line(decision: hunter_brain.Decision, late_sec: float = 0.0) -> None:
    reasons_short = "; ".join(decision.reasons)[:200]
    late_txt = f" late={int(late_sec)}s" if late_sec > 90 else ""
    text = (
        f"{utc_now_iso()} {decision.symbol} action={decision.action} setup={decision.setup} "
        f"leader_score={decision.leader_score:.3f} p_win={decision.p_win_est:.3f}{late_txt} "
        f"reasons={reasons_short}"
    )
    log_line(text)


def log_error(context: str, exc: BaseException) -> None:
    log_line(f"{utc_now_iso()} ERROR {context}: {exc!r}")
    tb = traceback.format_exc()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(tb + "\n")


# ---------------------------------------------------------------------------
# paper basket per symbol (simulated fills — NO real orders, ever)
# ---------------------------------------------------------------------------


class PaperBasket:
    """Wraps a BasketManager with simple simulated-fill bookkeeping.

    Phase 1 only needs the basket state machine to be EXERCISED so its
    telemetry accumulates in the journal; fills are simulated at the
    decision's entry price and PnL is tracked in R-multiples using the
    decision's own sl distance as the risk unit.
    """

    def __init__(self, symbol: str, journal: DecisionJournal) -> None:
        self.symbol = symbol
        self.journal = journal
        self.manager = BasketManager(BasketConfig())
        self._now_min = 0.0

    def advance_clock(self, minutes: float) -> None:
        self._now_min += minutes

    def on_decision(self, decision: hunter_brain.Decision) -> None:
        if decision.action != "enter" or decision.entry is None or decision.sl is None:
            return
        risk_usd = abs(decision.entry - decision.sl)
        if self.manager.state.state == "FLAT":
            leg = Leg(
                side=str(decision.side),
                entry=float(decision.entry),
                sl=float(decision.sl),
                risk_usd=risk_usd,
                opened_at_min=self._now_min,
                label=f"dexter3:fable:m5h-v1:{decision.setup}",
            )
            result = self.manager.on_entry(leg)
            self.journal.insert_basket_event(
                self.manager.state.basket_id or 0,
                "on_entry",
                {"decision_action": decision.action, "result": result.to_dict()},
            )

    def on_m5_close_tick(self, current_price: float | None) -> None:
        if self.manager.state.state == "FLAT" or current_price is None:
            return
        legs = self.manager.state.legs
        if not legs:
            return
        base_risk = self.manager.state.base_risk_usd or 1.0
        # Simple aggregate PnL proxy: mean directional distance across legs,
        # expressed in R relative to the basket's base risk. This is a
        # shadow-only approximation — Phase 2 replaces it with broker PnL.
        total_r = 0.0
        for leg in legs:
            direction = 1.0 if leg.side == "buy" else -1.0
            pnl = direction * (current_price - leg.entry)
            total_r += pnl / base_risk if base_risk else 0.0
        aggregate_r = total_r / len(legs)
        result = self.manager.on_m5_close(
            bars=[],
            features={},
            positions_pnl={"now_min": self._now_min, "aggregate_r": aggregate_r, "structure_evidence": {}},
        )
        if result.action != "none":
            self.journal.insert_basket_event(
                self.manager.state.basket_id or 0,
                "on_m5_close",
                {"result": result.to_dict(), "aggregate_r": aggregate_r},
            )


# ---------------------------------------------------------------------------
# one cycle per symbol
# ---------------------------------------------------------------------------


def _completed_by(bar_ts: str, close_epoch: float, tf_min: int) -> bool:
    """True when a bar (labeled by open ts) has fully closed by ``close_epoch``."""
    ts_epoch = _iso_to_epoch(bar_ts)
    return ts_epoch > 0 and (ts_epoch + tf_min * 60) <= close_epoch + 1e-6


def run_symbol_cycle(
    mcp: Dexter3McpClient,
    journal: DecisionJournal,
    baskets: dict[str, PaperBasket],
    state: dict[str, Any],
    symbol: str,
    *,
    executor: Dexter3Executor | None = None,
    journal_stats: dict[str, Any] | None = None,
) -> str:
    """Run one evaluation cycle for ``symbol``. Returns a short status string.

    Decides EVERY pending completed M5 bar (catch-up), not only the newest.
    Catch-up decisions use only bars completed by that close (no lookahead in
    M5 prefix or M15/H1 context) and carry their lateness in the log line.

    ``executor`` is optional (None in Phase 1 shadow mode and whenever
    --live is absent — see ``main``/``run_loop``). When provided, it is
    invoked ONLY for the newest bar's ``enter`` decisions — historical
    catch-up bars never place a live order (that would be a backfill/
    lookahead order against a price that has already moved on).
    """
    m5_bars = fetch_fresh_m5(mcp, symbol)
    if len(m5_bars) < MIN_M5_BARS:
        return f"insufficient_m5_bars({len(m5_bars)})"

    pending = pending_m5_closes(state, symbol, m5_bars)
    if not pending:
        return "no_new_m5_close"

    m15_bars = mcp.get_trendbars(symbol, "m15", M15_BARS_NEEDED)
    h1_bars = mcp.get_trendbars(symbol, "h1", H1_BARS_NEEDED)

    statuses: list[str] = []
    for i in pending:
        bar_ts = str(m5_bars[i].get("ts") or "")
        close_epoch = _iso_to_epoch(bar_ts) + M5_BAR_SEC
        late_sec = max(0.0, datetime.now(timezone.utc).timestamp() - close_epoch)
        prefix = m5_bars[: i + 1]
        m15_ctx = [b for b in m15_bars if _completed_by(str(b.get("ts") or ""), close_epoch, 15)]
        h1_ctx = [b for b in h1_bars if _completed_by(str(b.get("ts") or ""), close_epoch, 60)]
        is_newest = i == len(m5_bars) - 1

        decision = hunter_brain.decide(
            symbol, None, prefix, m15_ctx, h1_ctx, journal_stats=journal_stats if is_newest else None
        )
        journal.insert_decision(decision)
        log_decision_line(decision, late_sec=late_sec)

        basket = baskets.setdefault(symbol, PaperBasket(symbol, journal))
        basket.advance_clock(M5_BAR_SEC / 60.0)
        basket.on_decision(decision)
        if is_newest:
            try:
                spot = mcp.get_spot_price(symbol)
                mid = (float(spot.get("bid", 0.0) or 0.0) + float(spot.get("ask", 0.0) or 0.0)) / 2.0
            except (McpClientError, McpZombieError) as exc:
                # never silent — a broken quote path hid the wrong-tool-name
                # bug for hours on 2026-07-05
                log_line(f"{utc_now_iso()} {symbol} spot_read_failed (paper tick falls back to bar close): {exc}")
                mid = float(m5_bars[i].get("close") or 0.0)
        else:
            # historical catch-up bar: use that bar's close, never fresh spot (no lookahead)
            mid = float(m5_bars[i].get("close") or 0.0)
        basket.on_m5_close_tick(mid)

        status = f"decided:{decision.action}:{decision.setup}"
        if is_newest and executor is not None and decision.action == "enter":
            exec_result = _execute_live_entry(executor, decision)
            status += f":live_{exec_result.get('action', 'unknown')}"

        mark_m5_close_seen(state, symbol, bar_ts)
        save_shadow_state(state)
        if late_sec > 90:
            status += f":late{int(late_sec)}s"
        statuses.append(status)
    return ";".join(statuses)


def _execute_live_entry(executor: Dexter3Executor, decision: hunter_brain.Decision) -> dict[str, Any]:
    """Resolve account state and place a live micro-entry. Never raises."""
    account_state: dict[str, Any] = {}
    # get_balance() intermittently returns without traderId (observed live
    # 2026-07-05 11:00:21Z → demo gate refused a valid entry). One short
    # retry before giving the executor a state it will refuse.
    for attempt in range(2):
        try:
            balance = executor.client.get_balance()
            account_state = {"traderId": balance.get("traderId")}
        except (McpClientError, McpZombieError) as exc:
            log_line(f"{utc_now_iso()} {decision.symbol} live_entry_balance_read_failed: {exc}")
            account_state = {}
        if account_state.get("traderId") is not None:
            break
        if attempt == 0:
            time.sleep(2)
    try:
        result = executor.execute_entry(decision, account_state)
    except Exception as exc:  # noqa: BLE001 - live path must never crash the loop
        log_error(f"execute_entry({decision.symbol})", exc)
        return {"action": "exception"}
    log_line(
        f"{utc_now_iso()} {decision.symbol} LIVE_ENTRY action={result.get('action')} "
        f"position_id={result.get('position_id')} verified={result.get('verified')}"
    )
    return result


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------


_LAST_STATUS: dict[str, str] = {}
_STATUS_REPEATS: dict[str, int] = {}
STATUS_HEARTBEAT_EVERY = 90  # re-log an unchanged status every ~30 min at 20s poll

# per-symbol empirical stats cache, refreshed every _learning_refresh_every_cycles
# cycles (see run_once). Module-level so --once callers (which run a single
# cycle) simply never populate it — hunter_brain.decide() then falls back to
# its own base p_win priors, byte-identical to Phase 1.
_JOURNAL_STATS_CACHE: dict[str, dict[str, Any]] = {}
_CYCLE_COUNTER = 0


def _refresh_learning_loops(symbols: list[str], journal: DecisionJournal, mcp: Dexter3McpClient) -> None:
    """Every N cycles: refresh empirical p_win stats + evaluate pending skips.

    Never raises — a failure here must not interrupt the decision loop, it
    only means stats stay stale for another refresh interval.
    """
    for symbol in symbols:
        try:
            _JOURNAL_STATS_CACHE[symbol] = empirical_stats.stats_to_journal_stats_arg(
                empirical_stats.compute_from_journal(journal, symbol)
            ) or None
        except Exception as exc:  # noqa: BLE001 - stats refresh must never break the loop
            log_error(f"empirical_stats.compute_from_journal({symbol})", exc)
    try:
        result = skip_evaluator.evaluate_pending_skips(journal, mcp)
        log_line(
            f"{utc_now_iso()} skip_evaluator checked={result['checked']} "
            f"evaluated={result['evaluated']} unevaluable={result['unevaluable']}"
        )
        fear_cost = skip_evaluator.fear_cost_summary(journal)
        log_line(
            f"{utc_now_iso()} fear_cost hours={fear_cost['hours']} "
            f"skips_evaluated={fear_cost['skips_evaluated']} "
            f"would_have_wins={fear_cost['would_have_wins']} "
            f"would_have_pnl_r={fear_cost['would_have_pnl_r']}"
        )
    except Exception as exc:  # noqa: BLE001 - loop must never die
        log_error("skip_evaluator.evaluate_pending_skips", exc)


def run_once(
    symbols: list[str],
    mcp: Dexter3McpClient,
    journal: DecisionJournal,
    baskets: dict[str, PaperBasket],
    *,
    executor: Dexter3Executor | None = None,
    refresh_every_cycles: int | None = None,
) -> None:
    global _CYCLE_COUNTER
    state = load_shadow_state()
    for symbol in symbols:
        try:
            status = run_symbol_cycle(
                mcp,
                journal,
                baskets,
                state,
                symbol,
                executor=executor,
                journal_stats=_JOURNAL_STATS_CACHE.get(symbol),
            )
            repeats = _STATUS_REPEATS.get(symbol, 0) + 1 if _LAST_STATUS.get(symbol) == status else 0
            _STATUS_REPEATS[symbol] = repeats
            _LAST_STATUS[symbol] = status
            if repeats == 0 or repeats % STATUS_HEARTBEAT_EVERY == 0:
                suffix = f" (x{repeats + 1})" if repeats else ""
                log_line(f"{utc_now_iso()} {symbol} cycle_status={status}{suffix}")
        except McpZombieError as exc:
            log_line(f"{utc_now_iso()} {symbol} MCP_ZOMBIE: {exc}")
        except Exception as exc:  # noqa: BLE001 - loop must never die on a per-symbol error
            log_error(f"run_symbol_cycle({symbol})", exc)

    if refresh_every_cycles:
        _CYCLE_COUNTER += 1
        if _CYCLE_COUNTER % refresh_every_cycles == 0:
            _refresh_learning_loops(symbols, journal, mcp)


def _resolve_live_executor(mcp: Dexter3McpClient, journal: DecisionJournal, live_flag: bool) -> Dexter3Executor | None:
    """Double opt-in: --live CLI flag AND DEXTER3_LIVE=1 env var, both required.

    Returns None (shadow-only, byte-identical to Phase 1) unless both gates
    pass — this is deliberately redundant with the demo-refusal gate inside
    Dexter3Executor itself; the two gates protect against different mistakes
    (a stray --live vs. a misconfigured/live-bound MCP).
    """
    if not live_flag:
        return None
    if os.environ.get(DEXTER3_LIVE_ENV_VAR) != "1":
        log_line(
            f"{utc_now_iso()} --live requested but {DEXTER3_LIVE_ENV_VAR}=1 env var not set — "
            "staying in shadow mode (double opt-in required, see blueprint P2)"
        )
        return None
    log_line(f"{utc_now_iso()} DEXTER3 LIVE MODE ENABLED — demo micro-entries may be placed (label={LIVE_ORDER_LABEL})")
    return Dexter3Executor(mcp, journal, ExecutorConfig())


def run_loop(symbols: list[str], poll_sec: int, live: bool = False) -> None:
    acquire_loop_lock()
    mcp = Dexter3McpClient()
    baskets: dict[str, PaperBasket] = {}
    try:
        with DecisionJournal() as journal:
            executor = _resolve_live_executor(mcp, journal, live)
            refresh_every_cycles = _learning_refresh_every_cycles(poll_sec)
            log_line(
                f"{utc_now_iso()} dexter3 shadow loop started symbols={symbols} poll_sec={poll_sec} "
                f"live={'ON' if executor is not None else 'off'}"
            )
            while True:
                try:
                    run_once(symbols, mcp, journal, baskets, executor=executor, refresh_every_cycles=refresh_every_cycles)
                except McpZombieError as exc:
                    log_line(f"{utc_now_iso()} MCP_ZOMBIE (loop-level): {exc}")
                    time.sleep(MCP_ZOMBIE_SLEEP_SEC)
                    continue
                except Exception as exc:  # noqa: BLE001 - loop must never die
                    log_error("run_loop", exc)
                time.sleep(max(1, poll_sec))
    except KeyboardInterrupt:
        log_line(f"{utc_now_iso()} dexter3 shadow loop stopped (KeyboardInterrupt)")
    finally:
        release_loop_lock()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dexter3 M5 shadow/live loop")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="comma-separated symbol list")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run a single evaluation cycle per symbol and exit")
    mode.add_argument("--loop", action="store_true", help="run continuously until interrupted")
    parser.add_argument("--poll-sec", type=int, default=DEFAULT_POLL_SEC, help="seconds between loop iterations")
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Enable demo micro-entries for 'enter' decisions. Also requires the "
            f"{DEXTER3_LIVE_ENV_VAR}=1 environment variable (double opt-in) — "
            "default is OFF/shadow-only."
        ),
    )
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("no symbols provided", file=sys.stderr)
        return 2

    if args.loop:
        run_loop(symbols, args.poll_sec, live=args.live)
        return 0

    # --once: no lock required for a single pass, but still respect an
    # already-running loop's lock to avoid racing its state file.
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old_pid = 0
        if old_pid > 0 and _pid_alive(old_pid):
            print(f"dexter3 shadow loop already running (pid={old_pid}) — skipping --once", file=sys.stderr)
            return 1

    mcp = Dexter3McpClient()
    baskets: dict[str, PaperBasket] = {}
    try:
        with DecisionJournal() as journal:
            executor = _resolve_live_executor(mcp, journal, args.live)
            run_once(symbols, mcp, journal, baskets, executor=executor)
    except McpZombieError as exc:
        log_line(f"{utc_now_iso()} MCP_ZOMBIE (--once): {exc}")
        return 3
    except Exception as exc:  # noqa: BLE001 - report but do not crash ugly
        log_error("main(--once)", exc)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
