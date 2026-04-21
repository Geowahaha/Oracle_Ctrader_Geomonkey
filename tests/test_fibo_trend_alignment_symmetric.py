"""Regression tests for the symmetric trend-alignment gate in fibo_advance.

Background: prior to this commit, both _sniper_scan and _scout_scan contained an
asymmetric block:

    elif direction == "short" and h4_bias != "short":
        ... blocked = True

This killed shorts whenever H4 was neutral or long, even when D1 disagreed with
H4 (i.e. a legitimate mean-reversion/counter-H4 short). It had no long-side
mirror, so longs passed freely in neutral/mixed H4 conditions.

This test guards against regression by:
  1. Asserting the asymmetric line no longer appears in scanners/fibo_advance.py.
  2. Asserting both symmetric D1+H4 agreement gates remain (both sides covered).
"""
from __future__ import annotations

import os
import re


def _read_fibo_source() -> str:
    here = os.path.dirname(__file__)
    path = os.path.normpath(os.path.join(here, "..", "scanners", "fibo_advance.py"))
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_asymmetric_short_only_rule_is_removed():
    """The old `short and h4_bias != 'short'` hard block must not reappear."""
    src = _read_fibo_source()
    # Match the core asymmetric shape, tolerant of whitespace.
    pattern = re.compile(
        r'direction\s*==\s*"short"\s+and\s+h4_bias\s*!=\s*"short"',
    )
    assert pattern.search(src) is None, (
        "Asymmetric short-only gate has reappeared in fibo_advance.py. "
        "This re-introduces the BUY-only bias we fixed. Use the symmetric "
        "D1+H4 gate instead (both sides covered) and rely on the per-side "
        "trend_confidence_modifier for weighting."
    )
    # Also check the scout mirror.
    pattern_scout = re.compile(
        r'scout_direction\s*==\s*"short"\s+and\s+h4_bias\s*!=\s*"short"',
    )
    assert pattern_scout.search(src) is None


def test_symmetric_d1_h4_gates_are_present_both_sides():
    """Both directions must have a matching D1+H4-agree-against-us block."""
    src = _read_fibo_source()
    long_vs_bear = re.compile(
        r'direction\s*==\s*"long"\s+and\s+d1_bias\s*==\s*"short"\s+and\s+h4_bias\s*==\s*"short"'
    )
    short_vs_bull = re.compile(
        r'direction\s*==\s*"short"\s+and\s+d1_bias\s*==\s*"long"\s+and\s+h4_bias\s*==\s*"long"'
    )
    scout_long_vs_bear = re.compile(
        r'scout_direction\s*==\s*"long"\s+and\s+d1_bias\s*==\s*"short"\s+and\s+h4_bias\s*==\s*"short"'
    )
    scout_short_vs_bull = re.compile(
        r'scout_direction\s*==\s*"short"\s+and\s+d1_bias\s*==\s*"long"\s+and\s+h4_bias\s*==\s*"long"'
    )
    assert long_vs_bear.search(src), "Sniper: missing long-vs-bearish symmetric gate"
    assert short_vs_bull.search(src), "Sniper: missing short-vs-bullish symmetric gate"
    assert scout_long_vs_bear.search(src), "Scout: missing long-vs-bearish symmetric gate"
    assert scout_short_vs_bull.search(src), "Scout: missing short-vs-bullish symmetric gate"
