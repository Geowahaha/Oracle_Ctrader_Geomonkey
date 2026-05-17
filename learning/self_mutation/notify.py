"""Telegram notifier shim for the Self-Mutation Loop.

The loop should not import `notifier.telegram_bot` directly — that module owns
an event loop and side effects. Instead, the governor receives a `notifier`
callable at construction time. For production it's wired to a thin async
forwarder; tests pass a `RecordingNotifier` to assert messages without I/O.
"""
from __future__ import annotations

import logging
from typing import Callable, Protocol


logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def __call__(self, message: str) -> None: ...


class LoggingNotifier:
    """Default notifier — logs at INFO level, no Telegram I/O."""

    def __call__(self, message: str) -> None:
        logger.info("self_mutation_notify %s", message)


class RecordingNotifier:
    """Test double — records messages for assertions."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, message: str) -> None:
        self.messages.append(str(message))


def compose_promotion_message(*, knob: str, old: float, new: float, stage: str, mutation_id: str) -> str:
    arrow = "→"
    return (
        f"🧬 Self-Mutation {stage.upper()} promoted: {knob} {old} {arrow} {new} "
        f"(mutation={mutation_id})"
    )


def compose_rollback_message(*, knob: str, reverted_to: float, reason: str, mutation_id: str) -> str:
    return (
        f"↩️ Self-Mutation ROLLBACK: {knob} reverted to {reverted_to} — {reason} "
        f"(mutation={mutation_id})"
    )


def compose_verdict_message(*, mutation_id: str, knob: str, passed: bool, pnl_delta: float) -> str:
    flag = "✅" if passed else "❌"
    sign = "+" if pnl_delta >= 0 else ""
    return (
        f"{flag} Self-Mutation verdict: {knob} ({mutation_id}) "
        f"pnl_delta={sign}${pnl_delta:.2f}"
    )


__all__ = [
    "Notifier",
    "LoggingNotifier",
    "RecordingNotifier",
    "compose_promotion_message",
    "compose_rollback_message",
    "compose_verdict_message",
]
