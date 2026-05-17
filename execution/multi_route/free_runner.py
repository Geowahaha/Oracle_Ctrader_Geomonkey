"""Free-Runner engine — when one leg of a basket prints, the rest go BE+small.

Once any leg in a basket reaches `r >= promote_r`, the engine emits a
`FreeRunnerDirective` for every other leg in the same basket telling the
executor to:
  - move stop to `entry + be_padding_atr * atr` (or - for shorts)
  - reduce TP to a partial close (optional; controlled by `partial_close_share`)

The engine is stateful (it tracks which baskets have already been promoted)
so re-invocations are idempotent.

Pattern:

    engine = FreeRunnerEngine(config=...)
    # for each leg progress tick:
    directives = engine.on_leg_progress(
        basket_id="run-42",
        leg_id="run-42:probe",
        current_r=1.1,
        basket_legs=[("run-42:probe", "long", 2300.0), ("run-42:retest", "long", 2298.0)],
        atr_5m=4.0,
    )
    for d in directives:
        ctrader.modify_stop(d.leg_id, d.new_stop_loss)
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class FreeRunnerDirective:
    """Instruction to the executor to upgrade one leg into a free runner."""

    leg_id: str
    new_stop_loss: float
    partial_close_share: float
    reason: str


@dataclass
class FreeRunnerEngineConfig:
    enabled: bool = False
    promote_r: float = 0.8          # any leg reaching this R triggers promotion
    be_padding_atr: float = 0.10    # BE + (this * atr) for the new stop
    partial_close_share: float = 0.0  # 0 means "stop-only upgrade, keep size"


class FreeRunnerEngine:
    """Stateful promoter for free-runner upgrades on a basket."""

    def __init__(self, *, config: Optional[FreeRunnerEngineConfig] = None) -> None:
        self.config = config or FreeRunnerEngineConfig()
        self._promoted_baskets: set[str] = set()
        self._lock = threading.Lock()

    def on_leg_progress(
        self,
        *,
        basket_id: str,
        leg_id: str,
        current_r: float,
        basket_legs: Iterable[tuple[str, str, float]],
        atr_5m: float,
    ) -> list[FreeRunnerDirective]:
        """Return directives for the basket, or empty list if no promotion fires.

        `basket_legs` is `[(leg_id, direction, entry_price), ...]` for every leg
        in the basket — the engine emits one directive per leg other than the
        promoting one.
        """
        if not self.config.enabled:
            return []
        if float(current_r) < self.config.promote_r:
            return []
        with self._lock:
            if basket_id in self._promoted_baskets:
                return []  # idempotent
            self._promoted_baskets.add(basket_id)

        directives: list[FreeRunnerDirective] = []
        padding = max(0.0, float(atr_5m) * self.config.be_padding_atr)
        for (other_leg_id, direction, entry_price) in basket_legs:
            if other_leg_id == leg_id:
                continue
            if str(direction or "").lower() in {"long", "buy"}:
                new_stop = float(entry_price) - padding
            else:
                new_stop = float(entry_price) + padding
            directives.append(
                FreeRunnerDirective(
                    leg_id=other_leg_id,
                    new_stop_loss=round(new_stop, 6),
                    partial_close_share=float(self.config.partial_close_share),
                    reason=f"sibling_leg_R>={self.config.promote_r}",
                )
            )
        return directives

    # ----- test/debug helpers ------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self._promoted_baskets.clear()

    def is_promoted(self, basket_id: str) -> bool:
        with self._lock:
            return basket_id in self._promoted_baskets


__all__ = ["FreeRunnerEngine", "FreeRunnerEngineConfig", "FreeRunnerDirective"]
