"""Runtime config overrides — the bridge between the Self-Mutation Loop and the trader.

Overrides are persisted to a JSON file (`data/runtime/self_mutation_overrides.json`)
and consumed by the live trading config layer. Two scopes:

- `main`   — overrides applied to every dispatch.
- `canary` — time-limited overrides applied to canary lanes only; expire
             automatically at `expires_utc`.

Writes are atomic (temp file + os.replace). Reads are cached by mtime so the hot
path pays a stat() but no JSON parse when the file hasn't changed.

The loop owns this file; the trading config layer only reads it.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


SCHEMA_KEYS = ("main", "canary")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_expired(expires_utc: Optional[str], now_utc: Optional[str] = None) -> bool:
    if not expires_utc:
        return False
    now = now_utc or _utc_now_iso()
    return str(expires_utc) <= now


@dataclass
class OverrideEntry:
    knob: str
    value: float
    applied_utc: str
    mutation_id: str
    expires_utc: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"value": self.value, "applied_utc": self.applied_utc, "mutation_id": self.mutation_id}
        if self.expires_utc:
            d["expires_utc"] = self.expires_utc
        return d


@dataclass
class OverrideSet:
    main: dict[str, OverrideEntry] = field(default_factory=dict)
    canary: dict[str, OverrideEntry] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "main": {k: v.to_dict() for k, v in self.main.items()},
            "canary": {k: v.to_dict() for k, v in self.canary.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OverrideSet":
        out = cls()
        for scope in SCHEMA_KEYS:
            block = dict((data or {}).get(scope) or {})
            for knob, raw in block.items():
                if not isinstance(raw, dict):
                    continue
                try:
                    entry = OverrideEntry(
                        knob=str(knob),
                        value=float(raw.get("value")),
                        applied_utc=str(raw.get("applied_utc") or ""),
                        mutation_id=str(raw.get("mutation_id") or ""),
                        expires_utc=raw.get("expires_utc"),
                    )
                except (TypeError, ValueError):
                    continue
                getattr(out, scope)[knob] = entry
        return out


class OverrideStore:
    """Read/write the runtime overrides JSON file atomically."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: tuple[float, OverrideSet] | None = None

    def _read_raw(self) -> dict:
        if not self.path.exists():
            return {"main": {}, "canary": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            logger.exception("self_mutation_overrides_corrupted path=%s — resetting", self.path)
            return {"main": {}, "canary": {}}

    def read(self, *, now_utc: Optional[str] = None) -> OverrideSet:
        """Return a fresh OverrideSet, dropping expired canary entries from view.

        Expired canary entries are NOT removed from disk here — only the
        Governor's promoter/rollback flow rewrites the file. This keeps the
        read path purely advisory.
        """
        with self._lock:
            mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
            if self._cache and self._cache[0] == mtime:
                cached = self._cache[1]
            else:
                cached = OverrideSet.from_dict(self._read_raw())
                self._cache = (mtime, cached)
            # Hide expired canaries from callers (live behavior).
            visible = OverrideSet(main=dict(cached.main), canary={
                k: v for k, v in cached.canary.items() if not _is_expired(v.expires_utc, now_utc)
            })
            return visible

    def write(self, override_set: OverrideSet) -> None:
        payload = override_set.to_dict()
        with self._lock:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(self.path.parent), delete=False, suffix=".tmp"
            ) as tf:
                json.dump(payload, tf, indent=2, sort_keys=True, default=str)
                tf.flush()
                os.fsync(tf.fileno())
                tmp_name = tf.name
            os.replace(tmp_name, self.path)
            self._cache = None

    # ----- convenience mutators -----------------------------------------
    def set_canary(self, entry: OverrideEntry) -> OverrideSet:
        current = OverrideSet.from_dict(self._read_raw())
        current.canary[entry.knob] = entry
        self.write(current)
        return current

    def set_main(self, entry: OverrideEntry) -> OverrideSet:
        current = OverrideSet.from_dict(self._read_raw())
        # Promoting to main supersedes any active canary for the same knob.
        current.main[entry.knob] = entry
        current.canary.pop(entry.knob, None)
        self.write(current)
        return current

    def remove_canary(self, knob: str) -> OverrideSet:
        current = OverrideSet.from_dict(self._read_raw())
        current.canary.pop(knob, None)
        self.write(current)
        return current

    def remove_main(self, knob: str) -> OverrideSet:
        current = OverrideSet.from_dict(self._read_raw())
        current.main.pop(knob, None)
        self.write(current)
        return current

    def kill_all(self) -> OverrideSet:
        """Emergency: wipe every override. Returns the empty set."""
        empty = OverrideSet()
        self.write(empty)
        return empty

    def value_for(self, knob: str, default: float) -> float:
        """Resolution order: canary (if not expired) → main → default.

        Canary takes precedence over main during the canary window so the
        trader can A/B in a single place.
        """
        view = self.read()
        if knob in view.canary:
            return float(view.canary[knob].value)
        if knob in view.main:
            return float(view.main[knob].value)
        return float(default)


__all__ = ["OverrideEntry", "OverrideSet", "OverrideStore"]
