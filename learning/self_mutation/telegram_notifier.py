"""Production Telegram notifier for the Self-Mutation Loop.

The Governor receives a `notifier(message: str) -> None` callable. In tests
we pass a `RecordingNotifier`. In production we pass a `TelegramNotifier`
that forwards to the project's telegram_bot in a fire-and-forget manner.

Why a wrapper module?
- The Self-Mutation Loop must not import telegram_bot directly — that module
  owns an asyncio loop and side effects that would couple our tests to it.
- We want graceful degradation: if Telegram is unavailable or the env is
  misconfigured, the loop should keep working (mutations still get recorded).
- We want optional throttling so a burst of canary promotions does not spam
  the operator chat.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional


logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Forwards Self-Mutation messages to a Telegram chat via a user-provided sender."""

    def __init__(
        self,
        *,
        send: Callable[[str], None],
        throttle_seconds: float = 1.5,
        enabled: bool = True,
    ) -> None:
        self._send = send
        self._throttle_seconds = float(throttle_seconds)
        self._enabled = bool(enabled)
        self._last_sent_ts: float = 0.0
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        if not self._enabled:
            return
        with self._lock:
            now = time.monotonic()
            sleep_for = self._throttle_seconds - (now - self._last_sent_ts)
            if sleep_for > 0:
                # Brief pause to avoid Telegram rate-limit; we don't block long
                # because the governor expects fire-and-forget semantics.
                time.sleep(min(sleep_for, 3.0))
            self._last_sent_ts = time.monotonic()
        try:
            self._send(message)
        except Exception:
            logger.exception("self_mutation_telegram_send_failed msg=%s", message[:120])


def build_default_telegram_notifier(*, enabled: bool = True) -> Optional[TelegramNotifier]:
    """Construct a TelegramNotifier wired to the project's telegram bot.

    Returns None when the project's Telegram integration is unavailable so the
    caller can fall back to the LoggingNotifier without raising.
    """
    if not enabled:
        return None
    try:
        from notifier.telegram_bot import send_admin_message  # type: ignore
    except Exception:
        logger.info("self_mutation_telegram_unavailable")
        try:
            from notifier import telegram_bot as _tb  # type: ignore
            send_admin_message = getattr(_tb, "send_admin_message", None) or getattr(_tb, "broadcast", None)
        except Exception:
            send_admin_message = None
    if send_admin_message is None:
        return None
    return TelegramNotifier(send=send_admin_message)


__all__ = ["TelegramNotifier", "build_default_telegram_notifier"]
