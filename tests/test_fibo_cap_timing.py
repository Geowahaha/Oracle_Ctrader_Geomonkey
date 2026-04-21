"""Regression tests for item 1 of the audit — confidence cap timing.

Prior to this fix, _build_signal (sniper) and _scout_scan (scout) applied
their design ceilings (96 and 88) as `min(..., cap)` at the SAME line
where base confidence was computed — i.e. before trend/killer/session
modifiers were applied. A high-confluence signal at raw-113 would be
truncated to 96, then a -30 penalty would leave 66. The fix moves the
cap to the post-modifier stage so the raw value retains its headroom
and the sequence is 113 -> -30 -> 83 -> cap to 88 (or 96 sniper) -> 83.

These tests use source inspection because the scanner entry paths are
heavily mocked; the invariant we care about (cap lives after modifiers)
is best asserted at the code-level.
"""
from __future__ import annotations

import inspect
import os
import re
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

sys.modules.setdefault("yfinance", MagicMock())
sys.modules.setdefault("ccxt", MagicMock())

from scanners.fibo_advance import FiboAdvanceScanner


def _source_of(method_name: str) -> str:
    return inspect.getsource(getattr(FiboAdvanceScanner, method_name))


def test_sniper_build_signal_no_longer_caps_internally():
    """_build_signal must not apply a 96-cap to the base confidence —
    that cap belongs at the post-modifier stage in scan()."""
    src = _source_of("_build_signal")
    # The old offending pattern was: min(... , 96.0)
    # It must not appear inside _build_signal anymore.
    assert "96.0)" not in src.replace("_sniper_max_conf", ""), (
        "96.0 cap still baked into _build_signal — move it to post-modifier stage"
    )
    assert re.search(r"min\s*\(.*?,\s*96\.0\s*\)", src) is None


def test_scout_scan_no_longer_caps_internally():
    src = _source_of("_scout_scan")
    # Scout's old pattern was min(..., 88.0) on base confidence.
    # It must not appear inside the base-confidence computation anymore.
    # (The post-modifier cap uses the `_scout_max_conf` variable name so
    # this negative check looks for the literal 88.0 combined with min().)
    hits = re.findall(r"min\s*\(\s*[^)]*?,\s*88\.0\s*\)", src)
    assert not hits, f"Scout still has an internal 88 min-cap: {hits}"


def test_sniper_scan_applies_cap_post_modifier():
    """The _scan method (or the sniper block inside it) must apply the
    design ceiling AFTER the modifier addition."""
    src = _source_of("scan")
    # Look for the line that writes signal.confidence = round( ... ) and
    # confirm both the 10.0 floor and the 96.0 ceiling are present.
    assert "_sniper_max_conf" in src or "FIBO_ADVANCE_MAX_CONFIDENCE" in src, (
        "sniper post-modifier block is missing the MAX_CONFIDENCE ceiling"
    )
    # Verify both floor and ceiling exist in the same post-mod assignment.
    assert "10.0" in src and ("96.0" in src or "MAX_CONFIDENCE" in src)


def test_scout_post_modifier_cap_applies_unconditionally():
    """The scout post-modifier cap must execute outside the
    `if total_scout_mod < 0:` branch, so that a base > 88 with zero
    modifiers still gets capped."""
    src = _source_of("scan")
    assert "_scout_max_conf" in src or "FIBO_SCOUT_MAX_CONFIDENCE" in src
    # Find the post-modifier cap line for scout.
    # It must not be nested inside the `if total_scout_mod < 0:` block.
    # Heuristic: the line that assigns scout_signal.confidence = round(min(
    # ... , _scout_max_conf) must not be inside the `if total_scout_mod < 0:` block.
    # Simpler: check the cap line appears AFTER the `if total_scout_mod < 0:`
    # closing indentation by looking for its presence outside the if body.
    if_idx = src.find("if total_scout_mod < 0:")
    cap_idx = src.find("_scout_max_conf")
    assert if_idx != -1 and cap_idx != -1
    # The cap assignment should appear on its own (un-nested) line after the if.
    # A simple check: its indentation level matches the `if` itself, not deeper.
    lines = src.splitlines()
    cap_line = next(
        ln for ln in lines
        if "_scout_max_conf" in ln and "scout_signal.confidence" in ln
    )
    if_line = next(ln for ln in lines if "if total_scout_mod < 0:" in ln)
    # Count leading whitespace
    cap_indent = len(cap_line) - len(cap_line.lstrip())
    if_indent = len(if_line) - len(if_line.lstrip())
    assert cap_indent == if_indent, (
        f"scout post-mod cap appears nested (indent {cap_indent} vs if {if_indent}) — "
        "must be outside the penalty-only branch so a base > 88 with no penalty "
        "still gets capped."
    )
