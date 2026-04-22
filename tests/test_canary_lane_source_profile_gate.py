"""Regression tests for the canary-lane source-profile gate parity fix
(2026-04-22).

Background — live forensics from Apr 22, 2026:
  6 consecutive XAUUSD SHORT trades (xauusd_scheduled:canary lane) all hit SL
  while price rallied $4695 -> $4763. Investigation showed:

  * main lane (xauusd_scheduled): main lane went through
    `_allow_ctrader_source_profile` and was correctly FILTERED with reasons
    like `xau_scheduled_mtf_block:d1_h4_h1_block:short_vs_long` and
    `xau_scheduled_conf_below:59.9<70.0`.
  * canary lane (xauusd_scheduled:canary): jumped straight to
    `ctrader_executor.execute_signal` — bypassing MTF, conf, session,
    timeframe and entry_type checks entirely.

This test guards against regression by asserting (via source inspection)
that BOTH canary code paths now invoke `_allow_ctrader_source_profile`
before `ctrader_executor.execute_signal`:

  1. The persistent canary lane (`_maybe_execute_persistent_canary`)
  2. The family canary loop inside the same method
"""
from __future__ import annotations

import inspect
import os
import re
import sys


# Resolve repo root so the test runs from anywhere.
_HERE = os.path.dirname(__file__)
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _read_scheduler_source() -> str:
    path = os.path.join(_ROOT, "scheduler.py")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _maybe_execute_persistent_canary_source() -> str:
    """Pull JUST the body of `_maybe_execute_persistent_canary` so the
    asserts don't accidentally match unrelated callers elsewhere in the
    file."""
    src = _read_scheduler_source()
    # naive but reliable: grab from the def line until the next top-level
    # `    def ` (4-space-indented) at the same level.
    m = re.search(r"\n    def _maybe_execute_persistent_canary\b.*?(?=\n    def )", src, re.S)
    assert m, "could not locate _maybe_execute_persistent_canary in scheduler.py"
    return m.group(0)


# ── Persistent canary lane ───────────────────────────────────────────────────

def test_persistent_canary_calls_source_profile_gate_before_execute():
    body = _maybe_execute_persistent_canary_source()
    # Gate must be called inside this method.
    assert "_allow_ctrader_source_profile" in body, (
        "persistent canary lane no longer calls _allow_ctrader_source_profile "
        "— this re-opens the bug where canary executes trades the main lane "
        "filtered (MTF block, conf below threshold)."
    )
    # And the gate call must precede execute_signal in source order.
    gate_idx = body.find("_allow_ctrader_source_profile")
    exec_idx = body.find("ctrader_executor.execute_signal")
    assert 0 <= gate_idx < exec_idx, (
        "_allow_ctrader_source_profile must be called BEFORE "
        "ctrader_executor.execute_signal in the persistent canary path"
    )


def test_persistent_canary_skips_when_source_profile_blocks():
    """The pattern `if not _canary_allow:` must short-circuit and skip
    `execute_signal` — i.e. the gate result is actually honored."""
    body = _maybe_execute_persistent_canary_source()
    # Look for the negative branch that logs the skip.
    assert re.search(r"if\s+not\s+_canary_allow", body), (
        "persistent canary gate result is not branched on — fix must check "
        "`if not _canary_allow:` and skip execute_signal."
    )
    # Ensure there's a skipped log line for source_profile_blocked.
    assert "source_profile_blocked" in body, (
        "expected a skip-log message containing 'source_profile_blocked' so "
        "live monitoring can audit the canary skip reason."
    )


# ── Family canary loop ───────────────────────────────────────────────────────

def test_family_canary_loop_calls_source_profile_gate_before_execute():
    body = _maybe_execute_persistent_canary_source()
    # The family loop ends with execute_signal(_fc_signal, ...). The gate
    # call must use the same family_source.
    assert re.search(r"_allow_ctrader_source_profile\(\s*_fc_signal\s*,\s*family_source\s*\)", body), (
        "family canary loop must call _allow_ctrader_source_profile"
        "(_fc_signal, family_source) BEFORE ctrader_executor.execute_signal"
    )
    # Also check journal stamp on skip path so we don't lose audit trail.
    assert "_store_xau_family_canary_gate_journal" in body
    # And the family branch must have its own `if not _fc_allow:` check.
    assert re.search(r"if\s+not\s+_fc_allow", body), (
        "family canary gate result is not branched on — fix must check "
        "`if not _fc_allow:` and skip execute_signal."
    )


def test_family_canary_journal_uses_source_profile_stage():
    """When the family canary lane is blocked by source profile, the gate
    journal must record gate_stage='source_profile' so VM telemetry can
    distinguish it from directive blocks (gate_stage='trading_manager_directive')."""
    body = _maybe_execute_persistent_canary_source()
    # Find the family-canary journal-stamp call that fires on the skip path
    # — it must use gate_stage="source_profile".
    assert re.search(
        r"gate_stage\s*=\s*['\"]source_profile['\"]",
        body,
    ), (
        "family canary skip-on-source-profile must stamp the gate journal "
        "with gate_stage='source_profile' for telemetry."
    )
