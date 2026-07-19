"""Env-overridable posting-probability floors (T-2026-CU-9050-171).

The realized-trade analysis over closed_ai_signals ⨝ ml_predictions_master
(32.4k trades with logged confidence, 2026-03..07) showed that for some bots
the segment below a confidence floor is zero-EV: cutting it reduces trade
count sharply while keeping (or raising) total PnL. The floor NEVER replaces
the artifact's own operating point — callers combine both via
``max(artifact_threshold, floor)``, mirroring the MAX1 gate invariant
(core/max1_gate.py): a floor can only tighten a gate, never loosen it.

Deliberately a tiny pure module so backtest/test_prob_floor.py can pin the
parsing semantics without a database, and so every bot parses its floor env
var identically (clamp into [0, 1], garbage falls back to the default).
"""

from __future__ import annotations

import os


def load_prob_floor(env_var: str, default: float) -> float:
    """Read a posting floor from ``env_var``, falling back to ``default``.

    Unset/empty/unparsable values yield ``default``; parsable values are
    clamped into [0, 1]. The result is a FLOOR: apply it with
    ``max(artifact_threshold, floor)`` so it can never undercut the model's
    own operating point.
    """
    raw = os.getenv(env_var)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return min(1.0, max(0.0, value))
