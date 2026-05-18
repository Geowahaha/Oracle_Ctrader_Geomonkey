"""Missed Opportunity Detector — turn early exits into Self-Mutation seeds.

Lesson learned 2026-05-18: trade closed +$44 at 05:56, then price continued
60+ points in the same direction over the next hour. The system captured 6%
of the available move. This module scans closed positions and detects this
pattern, emitting `MissedRunnerEvent` rows that feed into the Self-Mutation
Loop as a special trigger (separate from the loss-event trigger).

The detector is *pure read-only*: it does not place orders, modify config,
or alter the broker journal. It writes to its own SQLite table that the
Self-Mutation Loop can subscribe to.
"""
from __future__ import annotations

from learning.missed_opportunity.detector import (
    MissedOpportunityDetector,
    MissedRunnerEvent,
    MissedRunnerStore,
)


__all__ = [
    "MissedOpportunityDetector",
    "MissedRunnerEvent",
    "MissedRunnerStore",
]
