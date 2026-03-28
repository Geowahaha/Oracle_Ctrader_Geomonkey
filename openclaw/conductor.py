"""
openclaw/conductor.py — Conductor Agent (Master Orchestrator)

Runs all specialist agents on a timer, collects findings/proposals,
routes proposals to PTS, and sends a unified Telegram summary.

Called from scheduler.py every CONDUCTOR_INTERVAL_MIN (default 30 min).

Cycle order:
  1. RiskGuardAgent  — safety first, may emit emergency actions
  2. PerformanceAgent — score all families
  3. RegimeAgent      — classify market regime (receives bottom performers)
  4. OptimizationAgent — AI proposes parameter changes (receives perf + regime)
  5. Telegram summary — only if any agent produced proposals or alerts

Design:
  - Each agent gets _safe_run() — a single agent crash never stops the cycle
  - Findings flow forward: each agent receives previous agents' output in context
  - Emergency risk actions are reported immediately, regardless of other results
"""

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Conductor:
    """Master orchestrator. Instantiate once; call run_cycle() on each tick."""

    def __init__(self) -> None:
        from openclaw.agents.risk_guard_agent import RiskGuardAgent
        from openclaw.agents.performance_agent import PerformanceAgent
        from openclaw.agents.regime_agent import RegimeAgent
        from openclaw.agents.optimization_agent import OptimizationAgent

        self._risk_guard = RiskGuardAgent()
        self._performance = PerformanceAgent()
        self._regime = RegimeAgent()
        self._optimization = OptimizationAgent()

    def run_cycle(self) -> dict:
        """
        Execute one full conductor cycle.
        Returns a summary dict with all agent results.
        """
        started_at = _utc_now_iso()
        logger.info("[conductor] cycle start %s", started_at)

        results: dict[str, Any] = {}
        context: dict = {}

        # ── 1. Risk Guard (safety first, emergency actions bypass PTS) ────────
        rg_result = self._risk_guard._safe_run(context)
        results["risk_guard"] = _serialise(rg_result)

        risk_findings = rg_result.findings if rg_result.is_ok() else {}
        emergency_actions = list(risk_findings.get("emergency_actions_taken") or [])

        # Notify immediately on emergency
        if emergency_actions:
            self._notify_emergency(emergency_actions)

        context["risk_findings"] = risk_findings

        # ── 2. Performance Agent ─────────────────────────────────────────────
        perf_result = self._performance._safe_run(context)
        results["performance"] = _serialise(perf_result)

        perf_findings = perf_result.findings if perf_result.is_ok() else {}
        context["performance_findings"] = perf_findings
        # Pass bottom performers to regime agent for family exclusion
        context["bottom_performers"] = list(perf_findings.get("bottom_performers") or [])

        # ── 3. Regime Agent ──────────────────────────────────────────────────
        regime_result = self._regime._safe_run(context)
        results["regime"] = _serialise(regime_result)

        regime_findings = regime_result.findings if regime_result.is_ok() else {}
        context["regime_findings"] = regime_findings

        # ── 4. Optimization Agent ────────────────────────────────────────────
        # Skip if shock active or cluster guard on — never propose changes during crisis
        shock_active = bool(risk_findings.get("shock_active"))
        cluster_guard = bool(risk_findings.get("cluster_guard_active"))

        if shock_active or cluster_guard:
            logger.info("[conductor] optimization skipped — shock=%s cluster_guard=%s", shock_active, cluster_guard)
            results["optimization"] = {"status": "skip", "reason": "shock_or_cluster_guard_active"}
        else:
            opt_result = self._optimization._safe_run(context)
            results["optimization"] = _serialise(opt_result)

        # ── 5. Compose Telegram summary ──────────────────────────────────────
        finished_at = _utc_now_iso()
        summary = self._build_summary(results, started_at, finished_at)

        if self._should_notify(results):
            self._send_telegram(summary)

        logger.info(
            "[conductor] cycle done — risk=%s perf=%s regime=%s opt=%s",
            results.get("risk_guard", {}).get("status"),
            results.get("performance", {}).get("status"),
            results.get("regime", {}).get("status"),
            results.get("optimization", {}).get("status"),
        )

        return {
            "ok": True,
            "started_at": started_at,
            "finished_at": finished_at,
            "results": results,
            "summary": summary,
        }

    # ── helpers ──────────────────────────────────────────────────────────────

    def _should_notify(self, results: dict) -> bool:
        """Send Telegram only if there's something worth reporting."""
        opt = results.get("optimization") or {}
        rg = results.get("risk_guard") or {}

        has_proposals = bool((opt.get("findings") or {}).get("proposals_routed"))
        has_emergency = bool((rg.get("findings") or {}).get("emergency_actions_taken"))
        has_bottom = bool((results.get("performance") or {}).get("findings", {}).get("bottom_performers"))

        return has_proposals or has_emergency or has_bottom

    def _build_summary(self, results: dict, started_at: str, finished_at: str) -> str:
        lines: list[str] = []
        lines.append(f"[Conductor] Cycle {started_at}")
        lines.append("")

        # Risk Guard
        rg = results.get("risk_guard") or {}
        rg_findings = rg.get("findings") or {}
        emergencies = list(rg_findings.get("emergency_actions_taken") or [])
        if emergencies:
            lines.append("RISK GUARD — EMERGENCY")
            for ea in emergencies:
                lines.append(f"  {ea.get('action')} — {ea.get('reason', ea.get('source', ''))}")
            lines.append("")
        else:
            cluster_alerts = list(rg_findings.get("cluster_alerts") or [])
            dd_alert = bool(rg_findings.get("drawdown_velocity_alert"))
            open_pos = int(rg_findings.get("open_positions", 0) or 0)
            rg_tag = "WARN" if (cluster_alerts or dd_alert) else "OK"
            lines.append(f"Risk Guard [{rg_tag}] positions={open_pos}")
            if cluster_alerts:
                for ca in cluster_alerts[:3]:
                    lines.append(f"  WARN {ca.get('source')} x{ca.get('loss_count')} losses ({ca.get('total_pnl', 0):.1f} USD)")
            if dd_alert:
                lines.append(f"  WARN Drawdown velocity: {rg_findings.get('drawdown_usd_per_hour', 0):.1f} USD/hr")

        # Performance
        perf = results.get("performance") or {}
        perf_findings = perf.get("findings") or {}
        top = list(perf_findings.get("top_performers") or [])
        bottom = list(perf_findings.get("bottom_performers") or [])
        if top or bottom:
            lines.append("")
            lines.append("Performance")
            for f in top[:3]:
                lines.append(f"  TOP {f.get('family')} WR={f.get('win_rate', 0):.1%} PnL={f.get('pnl_usd', 0):.1f}")
            for f in bottom[:3]:
                lines.append(f"  LOW {f.get('family')} WR={f.get('win_rate', 0):.1%} PnL={f.get('pnl_usd', 0):.1f}")

        # Regime
        regime = results.get("regime") or {}
        regime_findings = regime.get("findings") or {}
        if regime_findings:
            lines.append("")
            regime_label = str(regime_findings.get("regime", "unknown"))
            session = str(regime_findings.get("session", ""))
            rec = list(regime_findings.get("recommended_families") or [])
            lines.append(f"Regime: {regime_label} ({session})")
            if rec:
                lines.append(f"  Active: {', '.join(rec[:4])}")

        # Optimization
        opt = results.get("optimization") or {}
        opt_findings = opt.get("findings") or {}
        routed = list(opt_findings.get("proposals_routed") or [])
        if routed:
            lines.append("")
            lines.append(f"Optimization — {len(routed)} trial(s) proposed via PTS")
            for p in routed[:4]:
                lines.append(f"  trial {p.get('trial_id','?')[:8]}... {p.get('param')} -> {p.get('proposed_value')}")
            lines.append("Use /trials to review")

        return "\n".join(lines)

    def _send_telegram(self, text: str) -> None:
        try:
            from notifier.telegram_bot import notifier
            notifier._send(text, parse_mode=None, feature="conductor")
        except Exception as exc:
            logger.debug("[conductor] telegram send error: %s", exc)

    def _notify_emergency(self, emergency_actions: list) -> None:
        lines = ["[RISK GUARD] EMERGENCY"]
        for ea in emergency_actions:
            lines.append(f"  {ea.get('action')}: {ea.get('reason', ea.get('source', ''))}")
        try:
            from notifier.telegram_bot import notifier
            notifier._send("\n".join(lines), parse_mode=None, feature="conductor")
        except Exception as exc:
            logger.debug("[conductor] emergency notify error: %s", exc)


def _serialise(result: Any) -> dict:
    """Convert AgentResult to plain dict for logging/JSON."""
    if result is None:
        return {"status": "none"}
    return {
        "agent_name": getattr(result, "agent_name", ""),
        "status": getattr(result, "status", ""),
        "confidence": getattr(result, "confidence", 0.0),
        "findings": dict(getattr(result, "findings", {}) or {}),
        "proposals": list(getattr(result, "proposals", []) or []),
        "error": getattr(result, "error", ""),
        "generated_at": getattr(result, "generated_at", ""),
    }


# Module-level singleton — created on first import
_conductor: Conductor | None = None


def get_conductor() -> Conductor:
    global _conductor
    if _conductor is None:
        _conductor = Conductor()
    return _conductor


def run_conductor_cycle() -> dict:
    """Entry point called from scheduler.py."""
    return get_conductor().run_cycle()
