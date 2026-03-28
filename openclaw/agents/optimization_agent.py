"""
openclaw/agents/optimization_agent.py — Parameter Optimization Agent

Uses AI (Gemini primary, Ollama fallback) to reason over performance + regime
findings and propose specific parameter changes. All proposals are routed
through the PTS safety gate (never applied directly).

Proposal types supported:
  - confidence_threshold: tighten/loosen per-family min confidence
  - risk_per_trade: adjust CTRADER_RISK for a family
  - (future) session_filter: enable/disable family for a session
"""

import json
import logging
from typing import Any

from openclaw.agents.base import AgentResult, BaseAgent

logger = logging.getLogger(__name__)

# Only touch confidence params — risk params need explicit user approval
_TUNABLE_PARAMS: dict[str, str] = {
    # XAU families
    "xau_scalp_pullback_limit": "XAU_DIRECT_LANE_MIN_CONFIDENCE",
    "xau_scalp_tick_depth_filter": "XAU_TDF_MIN_CONFIDENCE",
    "xau_scalp_microtrend_follow_up": "XAU_MFU_MIN_CONFIDENCE",
    "xau_scalp_flow_short_sidecar": "XAU_FLOW_SHORT_SIDECAR_MIN_CONFIDENCE",
    "xau_scalp_failed_fade_follow_stop": "XAU_FFFS_MIN_CONFIDENCE",
    "xau_scalp_range_repair": "XAU_RANGE_REPAIR_MIN_CONFIDENCE",
    # BTC families — all active on weekend
    "btc_weekday_lob_momentum": "BTC_WEEKDAY_LOB_MIN_CONFIDENCE",
    "btc_fss": "BTC_FSS_MIN_CONFIDENCE",
    "btc_fls": "BTC_FLS_MIN_CONFIDENCE",
    # ETH family
    "eth_weekday_overlap_probe": "ETH_WEEKDAY_PROBE_MIN_CONFIDENCE",
}

# Weekend-only: source tokens for BTC/ETH fills used in auto-tune
_CRYPTO_FAMILIES = {
    "btc_weekday_lob_momentum",
    "btc_fss",
    "btc_fls",
    "eth_weekday_overlap_probe",
}

_SYSTEM_PROMPT = """You are Dexter Pro's Parameter Optimization Agent — an expert autonomous
trading system optimizer. You receive structured performance data and market regime context,
then propose SPECIFIC, CONSERVATIVE parameter adjustments.

Rules:
1. Only propose changes when data is clear (resolved >= 5 trades).
2. Step size for confidence: ±0.5 maximum per cycle.
3. Never propose loosening if win_rate < 0.50 with >= 5 trades.
4. Never propose tightening if win_rate > 0.65 with >= 5 trades.
5. Prefer "skip" over uncertain changes.
6. Output ONLY valid JSON — no commentary.

Output format:
{
  "proposals": [
    {
      "param": "XAU_DIRECT_LANE_MIN_CONFIDENCE",
      "family": "xau_scalp_pullback_limit",
      "current_value": 71.5,
      "proposed_value": 71.0,
      "direction": "loosen",
      "reason": "win_rate=0.67 with 8 resolved trades, regime=trending_bull"
    }
  ],
  "skip_reason": ""
}"""


def _build_prompt(perf_findings: dict, regime_findings: dict) -> str:
    from datetime import datetime, timezone
    family_scores = list(perf_findings.get("family_scores") or [])
    regime = str(regime_findings.get("regime", "unknown"))
    recommended = list(regime_findings.get("recommended_families") or [])
    bottom = [f.get("family", "") for f in (perf_findings.get("bottom_performers") or [])]
    top = [f.get("family", "") for f in (perf_findings.get("top_performers") or [])]
    is_weekend = datetime.now(timezone.utc).weekday() >= 5

    # On weekends prioritize crypto families; weekdays include all
    if is_weekend:
        tunable_scores = [
            f for f in family_scores
            if f.get("family") in _TUNABLE_PARAMS
            and f.get("resolved", 0) >= 3  # lower bar on weekend — fewer fills
        ]
        focus_note = (
            "WEEKEND MODE: XAU market is closed. Focus ONLY on BTC and ETH families. "
            "BTC/ETH trade 24/7. Even 3+ resolved trades are sufficient for weekend tuning."
        )
    else:
        tunable_scores = [
            f for f in family_scores
            if f.get("family") in _TUNABLE_PARAMS and f.get("resolved", 0) >= 5
        ]
        focus_note = "Weekday mode: XAU + BTC/ETH all active."

    context = {
        "regime": regime,
        "is_weekend": is_weekend,
        "focus_note": focus_note,
        "recommended_families": recommended,
        "top_performers": top,
        "bottom_performers": bottom,
        "family_performance": tunable_scores,
        "tunable_params": _TUNABLE_PARAMS,
    }

    return (
        "Analyze this Dexter Pro performance snapshot and propose parameter adjustments:\n\n"
        + json.dumps(context, indent=2)
        + "\n\nRespond with JSON only."
    )


class OptimizationAgent(BaseAgent):
    """
    Receives findings from PerformanceAgent + RegimeAgent.
    Uses Gemini (or Ollama fallback) to reason over the data.
    Routes all proposals through PTS via _propose_parameter_trial().

    Findings:
      - ai_proposals_raw: raw JSON from AI
      - proposals_routed: list of trial IDs created in PTS
      - proposals_skipped: list of proposals rejected by local validation
    """

    name = "optimization_agent"

    def run(self, context: dict) -> AgentResult:
        perf_findings = context.get("performance_findings") or {}
        regime_findings = context.get("regime_findings") or {}

        if not perf_findings:
            return self._skip("no performance findings in context")

        # ── build AI prompt ─────────────────────────────────────────────────
        prompt = _build_prompt(perf_findings, regime_findings)

        # ── call AI ─────────────────────────────────────────────────────────
        ai_raw = self._call_ai(prompt)
        if not ai_raw:
            return self._skip("AI returned empty response")

        # ── parse AI JSON ────────────────────────────────────────────────────
        try:
            ai_json = self._extract_json(ai_raw)
        except Exception as exc:
            return self._error(f"AI JSON parse error: {exc} | raw={ai_raw[:200]}")

        ai_proposals = list(ai_json.get("proposals") or [])
        skip_reason = str(ai_json.get("skip_reason") or "").strip()

        if not ai_proposals:
            return self._skip(skip_reason or "AI proposed no changes")

        # ── validate + route through PTS ─────────────────────────────────────
        try:
            from learning.live_profile_autopilot import live_profile_autopilot as lpa
            from config import config
        except Exception as exc:
            return self._error(f"import error: {exc}")

        routed: list[dict] = []
        skipped: list[dict] = []

        for prop in ai_proposals:
            try:
                param = str(prop.get("param", "")).strip()
                current_value = prop.get("current_value")
                proposed_value = prop.get("proposed_value")
                direction = str(prop.get("direction", "")).strip()
                reason = str(prop.get("reason", "ai_optimization_agent")).strip()

                if not param or proposed_value is None:
                    skipped.append({**prop, "skip_reason": "missing param or proposed_value"})
                    continue

                # Safety: step size check
                try:
                    if abs(float(proposed_value) - float(current_value or 0)) > 1.5:
                        skipped.append({**prop, "skip_reason": "step_too_large"})
                        continue
                except Exception:
                    pass

                # Get real current value from config
                real_current = getattr(config, param, None)
                if real_current is None:
                    skipped.append({**prop, "skip_reason": f"param_not_in_config: {param}"})
                    continue

                tid = lpa._propose_parameter_trial(
                    param=param,
                    current_value=real_current,
                    proposed_value=proposed_value,
                    direction=direction,
                    reason=f"[opt_agent] {reason}",
                    source="optimization_agent",
                )
                if tid:
                    routed.append({"trial_id": tid, "param": param, "proposed_value": proposed_value})
                    logger.info("[optimization_agent] trial proposed: %s → %s=%s", tid, param, proposed_value)
                else:
                    skipped.append({**prop, "skip_reason": "pts_rejected_duplicate_or_cap"})

            except Exception as exc:
                skipped.append({**prop, "skip_reason": str(exc)})

        findings = {
            "ai_proposals_raw": ai_proposals,
            "proposals_routed": routed,
            "proposals_skipped": skipped,
            "ai_skip_reason": skip_reason,
        }

        confidence = min(1.0, len(routed) / max(1, len(ai_proposals)))
        return self._ok(findings=findings, proposals=routed, confidence=confidence)

    # ── AI call ──────────────────────────────────────────────────────────────

    def _call_ai(self, prompt: str) -> str:
        """Try Gemini first, then Ollama."""
        from config import config
        import requests

        # ── Gemini ───────────────────────────────────────────────────────────
        if config.has_gemini_key():
            try:
                model = config.model_for_provider("gemini")
                mode = config.gemini_mode()
                if mode == "vertex":
                    endpoint = f"https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent"
                    api_key = config.GEMINI_VERTEX_AI_API_KEY
                else:
                    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                    api_key = config.GEMINI_API_KEY

                payload = {
                    "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.1, "maxOutputTokens": 800},
                }
                resp = requests.post(
                    endpoint,
                    params={"key": api_key},
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    timeout=45,
                )
                if resp.status_code < 400:
                    data = resp.json()
                    candidates = data.get("candidates") or []
                    if candidates:
                        parts = (candidates[0].get("content") or {}).get("parts") or []
                        text = "".join(p.get("text", "") for p in parts).strip()
                        if text:
                            return text
            except Exception as exc:
                logger.debug("[optimization_agent] Gemini error: %s", exc)

        # ── Ollama ───────────────────────────────────────────────────────────
        ollama_host = str(getattr(config, "OLLAMA_HOST", "http://localhost:11434") or "http://localhost:11434")
        ollama_model = str(getattr(config, "OLLAMA_MODEL", "qwen3:1.5b") or "qwen3:1.5b")
        try:
            resp = requests.post(
                f"{ollama_host}/api/generate",
                json={
                    "model": ollama_model,
                    "system": _SYSTEM_PROMPT,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 600},
                },
                timeout=60,
            )
            if resp.status_code < 400:
                return str(resp.json().get("response") or "").strip()
        except Exception as exc:
            logger.debug("[optimization_agent] Ollama error: %s", exc)

        return ""

    @staticmethod
    def _extract_json(text: str) -> dict:
        """Extract first JSON object from AI response."""
        import re
        # Strip markdown code fences
        cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
        # Find JSON block
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end])
        return json.loads(cleaned)
