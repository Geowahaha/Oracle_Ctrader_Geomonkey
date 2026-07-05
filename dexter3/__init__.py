"""Dexter3 — M5 Hunter (leader-market intraday engine, shadow phase).

Additive-only package. Never imported by or wired into the live M1 scalp
loops (scripts/xau_scalp_monitor.py, scripts/btc_scalp_monitor.py),
scheduler.py, execution/, or api/. See docs/DEXTER3_M5_HUNTER_BLUEPRINT.md.
"""
from __future__ import annotations

__version__ = "0.1.0-shadow"
