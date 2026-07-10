"""AIM2-TOPN — high-conviction "Top 1-3 des Tages" channel over AIM2 scores.

The base AIM2 gate (15_ai_master_bot.py) ranks every posted fleet signal and
posts each candidate whose calibrated probability clears the artifact threshold
(~34 % pass, ~110/day). This module is the second, *selective* consumer of the
very same scores: instead of "everything above the line" it takes at most N of
the day's strongest candidates and routes them to their own channel/tag as a
high-conviction stream (task T-2026-CU-9050-051, weg 2 of T-2026-CU-9050-031).

Only the selection is here, and it is deliberately pure/DB-free so
backtest/test_aim2_topn.py can pin it without a database. The bot supplies the
already-scored, calibrated candidates plus the current rolling-24h post count;
this module decides which few, if any, become AIM2-TOPN posts.

Two structural safety properties, both covered by the tests:

  * The whole path is behind AIM2_TOPN_ENABLED (default OFF). Disabled ⇒ the bot
    never calls select_topn and base-AIM2 behaviour is byte-for-byte unchanged.
  * Even enabled it is shadow-first (AIM2_TOPN_LIVE_POSTING, default OFF) and,
    like the research bots, refuses to post live when its channel id is unset.
    Flipping either flag, or arming the base AIM2_LIVE_POSTING, is Michi's call
    (OPUS-HANDOFF §6) — nothing here does it.

"Top-N of the day" is only knowable ex-post, so the daily cap is approximated
the way the task brief prescribes: a high MIN_PROB threshold that historically
yields ~1-3/day (calibrate with tools/aim2_topn_calibrate.py) plus a hard
rolling-24h cap of N as the backstop. Rolling 24h (not calendar day) avoids the
midnight-burst hole where 23:50+00:10 could fire 2·N trades inside 20 minutes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Own routing tag: separate channel identity, NOT a model-generation tag. Kept
# <= 10 chars because it doubles as the trade_cooldowns.module key
# (core.market_utils.COOLDOWN_MODULE_MAX_LEN) and lands in ai_signals.model /
# ml_predictions_master.model_name. It must be excluded from AIM2's own
# candidate/swarm stream (F6 self-feedback fix) — see 15_ai_master_bot.py.
MODEL_TAG = "AIM2-TOPN"

# Conservative defaults for the day the operator first flips the gate: N=1 and a
# very high floor, so the first live state is maximally selective. Both are
# env-overridable; MIN_PROB should be set from the calibration tool.
DEFAULT_N = 1
DEFAULT_MIN_PROB = 0.95


@dataclass(frozen=True)
class TopNConfig:
    enabled: bool
    live: bool
    n: int
    min_prob: float


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip() == "1"


def load_config() -> TopNConfig:
    """Read the AIM2-TOPN knobs from the environment.

    AIM2_TOPN_ENABLED / AIM2_TOPN_LIVE_POSTING default OFF (default-off gate,
    shadow-first). N clamps to >= 1; MIN_PROB clamps into [0, 1].
    """
    try:
        n = int(os.getenv("AIM2_TOPN_N", str(DEFAULT_N)))
    except ValueError:
        n = DEFAULT_N
    try:
        min_prob = float(os.getenv("AIM2_TOPN_MIN_PROB", str(DEFAULT_MIN_PROB)))
    except ValueError:
        min_prob = DEFAULT_MIN_PROB
    return TopNConfig(
        enabled=_env_flag("AIM2_TOPN_ENABLED", False),
        live=_env_flag("AIM2_TOPN_LIVE_POSTING", False),
        n=max(1, n),
        min_prob=min(1.0, max(0.0, min_prob)),
    )


@dataclass(frozen=True)
class TopNCandidate:
    coin: str
    direction: str
    prob: float
    trusted: bool  # AIM2 parity-guard passed (not OOD)
    source: str
    close_price: float


def select_topn(
    candidates: list[TopNCandidate],
    n: int,
    min_prob: float,
    posts_last_24h: int,
) -> list[TopNCandidate]:
    """Pick the AIM2-TOPN posts for this cycle from already-scored candidates.

    Rules (all pinned by the tests):
      1. Eligible = trusted AND prob >= min_prob. Untrusted (parity-guard/OOD)
         candidates never post, mirroring the base AIM2 gate.
      2. Dedupe per (coin, direction): the strongest survives — one cycle must
         not spend two of the day's few slots on the same trade.
      3. Deterministic order: probability desc, then coin, then direction. The
         tie-break is stable so shadow and a later live run agree on *which*
         candidate wins the last remaining slot.
      4. Hard cap: at most max(0, n - posts_last_24h) selections. posts_last_24h
         is the rolling-24h count the bot reads from ml_predictions_master, so
         the cap behaves identically in shadow and live.

    Returns the ordered subset to post (possibly empty — a valid outcome).
    """
    remaining = n - posts_last_24h
    if remaining <= 0:
        return []

    eligible = [c for c in candidates if c.trusted and c.prob >= min_prob]

    best: dict[tuple[str, str], TopNCandidate] = {}
    for c in eligible:
        key = (c.coin, c.direction)
        cur = best.get(key)
        if cur is None or c.prob > cur.prob:
            best[key] = c

    ranked = sorted(best.values(), key=lambda c: (-c.prob, c.coin, c.direction))
    return ranked[:remaining]
