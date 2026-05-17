"""Self-Mutation Loop — autonomous config evolution.

Every loss is a teacher; every config knob is a gene; every config delta is a
mutation; every shadow backtest is natural selection.

Public entry point: `from learning.self_mutation import Governor`.
"""
from __future__ import annotations

from learning.self_mutation.governor import Governor
from learning.self_mutation.types import (
    BacktestOutcome,
    LossEvent,
    Mutation,
    Promotion,
    Rollback,
    Verdict,
)

__all__ = [
    "Governor",
    "LossEvent",
    "Mutation",
    "BacktestOutcome",
    "Verdict",
    "Promotion",
    "Rollback",
]
