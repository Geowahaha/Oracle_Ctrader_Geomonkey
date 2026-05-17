"""KnobResolver — the bridge between Self-Mutation overrides and live trading code.

Resolution order (highest priority first):
  1. Active canary override   (if exists and not expired)
  2. Active main override     (if exists)
  3. Config attribute         (the env-driven default)
  4. KnobSpec.min_value       (last resort)

This is the only path live trading code should use to read a *mutable* knob.
Calling `resolver.get("XAU_GUARDIAN_RUNNER_PRESERVE_R")` will transparently
pick up an active canary tweak when one is in place, and otherwise return the
operator's baseline. Knobs NOT in `MUTABLE_KNOBS` raise immediately — the
resolver only services the whitelist by design.

The module exposes a process-singleton `knob_resolver` lazily wired to the
project's `config` and the default override file path. Tests construct their
own `KnobResolver` instance with custom paths.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from learning.self_mutation.overrides import OverrideStore
from learning.self_mutation.sampler import KNOB_BY_NAME, KnobSpec


logger = logging.getLogger(__name__)


class KnobResolver:
    """Single source of truth for any whitelisted mutable knob.

    Thread-safe: the underlying OverrideStore holds a lock; this class adds
    none of its own state beyond construction-time references.
    """

    def __init__(self, *, overrides: OverrideStore, config_obj: object):
        self._overrides = overrides
        self._config = config_obj

    def is_mutable(self, knob: str) -> bool:
        return knob in KNOB_BY_NAME

    def baseline(self, knob: str) -> float:
        """Return the config-default for a knob (ignoring any override)."""
        spec = KNOB_BY_NAME.get(knob)
        if spec is None:
            raise KeyError(f"knob_not_in_whitelist:{knob}")
        return float(getattr(self._config, knob, spec.min_value))

    def get(self, knob: str) -> float:
        """Return the live value — canary > main > config default > spec.min."""
        spec = KNOB_BY_NAME.get(knob)
        if spec is None:
            raise KeyError(f"knob_not_in_whitelist:{knob}")
        default = float(getattr(self._config, knob, spec.min_value))
        return float(self._overrides.value_for(knob, default))

    def describe(self, knob: str) -> dict:
        """Diagnostic helper — returns the resolved value and its source.

        Useful in dashboards and the `/self_mutation_status` Telegram command.
        """
        spec = KNOB_BY_NAME.get(knob)
        if spec is None:
            return {"knob": knob, "source": "unknown", "value": None}
        baseline = self.baseline(knob)
        view = self._overrides.read()
        if knob in view.canary:
            return {
                "knob": knob,
                "source": "canary",
                "value": float(view.canary[knob].value),
                "baseline": baseline,
                "mutation_id": view.canary[knob].mutation_id,
                "expires_utc": view.canary[knob].expires_utc,
            }
        if knob in view.main:
            return {
                "knob": knob,
                "source": "main",
                "value": float(view.main[knob].value),
                "baseline": baseline,
                "mutation_id": view.main[knob].mutation_id,
            }
        return {"knob": knob, "source": "baseline", "value": baseline, "baseline": baseline}

    def snapshot(self) -> dict:
        """Return one describe() per mutable knob — handy for `/status`."""
        return {spec.name: self.describe(spec.name) for spec in KNOB_BY_NAME.values()}


# ---------------------------------------------------------------------------
# Process singleton
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_singleton: Optional[KnobResolver] = None


def get_default_resolver() -> KnobResolver:
    """Return the process-singleton resolver, wiring it on first call.

    Live trading code uses this; tests construct their own KnobResolver
    instances pointing at temp paths.
    """
    global _singleton
    with _lock:
        if _singleton is None:
            # Lazy imports to avoid a global config dependency at module load.
            from config import config as _config
            overrides_path = getattr(_config, "SELF_MUTATION_OVERRIDES_PATH", "data/runtime/self_mutation_overrides.json")
            store = OverrideStore(overrides_path)
            _singleton = KnobResolver(overrides=store, config_obj=_config)
        return _singleton


def reset_default_resolver_for_tests() -> None:
    """Test hook — clears the singleton so the next call rewires from config."""
    global _singleton
    with _lock:
        _singleton = None


__all__ = ["KnobResolver", "get_default_resolver", "reset_default_resolver_for_tests"]
