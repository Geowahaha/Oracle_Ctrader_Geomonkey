"""MFE Progressive Trail — lock % of MFE based on R-multiple.

Operator complaint 2026-05-18: "ระบบไม่หิวกำไร ... กำไรเกือบ 200$
แล้วมาปิดเกือบติดลบ" — "System isn't hungry for profit; sometimes
nearly $200 profit gets given back to flat."

Smoking-gun trade: pid=621794184 (xauusd_scheduled short @ 4550.79):
- Original risk: 6.86 pt to SL @ 4557.65
- MFE reached 4536.88 → 13.91 pt favorable = **2.03R**
- Closed @ 4549.57 → captured only 1.22 pt = **0.18R = 6% of MFE**
- ImpulseRunner DID NOT fire because xauusd_scheduled wasn't in
  ALLOWED_SOURCES whitelist.

This module locks a *fraction of MFE* via progressive trail levels:

    MFE >= 1.0R  →  lock 30% of MFE  (small profit guaranteed)
    MFE >= 2.0R  →  lock 55% of MFE  (more than half banked)
    MFE >= 3.0R  →  lock 70% of MFE  (most of the run preserved)
    MFE >= 4.0R  →  lock 85% of MFE  (only late-stage giveback allowed)

It runs as an ADDITIONAL pass alongside ImpulseRunner:
- It has NO source whitelist (every XAU position eligible).
- It has NO structure-break requirement (the MFE itself is the signal).
- It only TIGHTENS the SL toward profit — never widens, never moves SL
  backward.
- The trail target is computed from the current best-seen MFE, not the
  current price, so a single wick spike sets the floor permanently.

Public surface:

    from learning.mfe_progressive_trail import (
        MFEProgressiveTrail, MFEProgressiveTrailConfig,
        MFETrailInputs, MFETrailDirective,
    )
"""
from __future__ import annotations

from learning.mfe_progressive_trail.engine import (
    MFEProgressiveTrail,
    MFEProgressiveTrailConfig,
    MFETrailDirective,
    MFETrailInputs,
)


__all__ = [
    "MFEProgressiveTrail",
    "MFEProgressiveTrailConfig",
    "MFETrailInputs",
    "MFETrailDirective",
]
