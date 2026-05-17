"""Tests for the TelegramNotifier wrapper."""
from __future__ import annotations

import time

from learning.self_mutation.telegram_notifier import TelegramNotifier


def test_telegram_notifier_forwards_when_enabled():
    sent: list[str] = []
    notifier = TelegramNotifier(send=sent.append, enabled=True, throttle_seconds=0.0)
    notifier("hello")
    notifier("world")
    assert sent == ["hello", "world"]


def test_telegram_notifier_silent_when_disabled():
    sent: list[str] = []
    notifier = TelegramNotifier(send=sent.append, enabled=False)
    notifier("hello")
    assert sent == []


def test_telegram_notifier_swallows_send_exceptions():
    def boom(msg: str) -> None:
        raise RuntimeError("telegram is down")

    notifier = TelegramNotifier(send=boom, throttle_seconds=0.0)
    # Must not raise — the loop should keep running even if Telegram is down.
    notifier("hello")


def test_telegram_notifier_throttles_bursts():
    sent: list[str] = []
    notifier = TelegramNotifier(send=sent.append, throttle_seconds=0.05)
    start = time.monotonic()
    notifier("a")
    notifier("b")
    notifier("c")
    elapsed = time.monotonic() - start
    assert sent == ["a", "b", "c"]
    # 2 throttled gaps of ~0.05s each.
    assert elapsed >= 0.05
