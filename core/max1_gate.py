"""MAX1 — high-conviction throttle over the RUB2-SHORT model (T-2026-CU-9050-067).

RUB2-SHORT is the fleet's strongest short edge (OOT +0.64 %/trade net, live
79 % TP1-WR — T-2026-CU-9050-044), but it fires ~9×/day at its artifact
threshold of 0.829. Michi's target for the main channel is 1-3 trades/day at a
very high hit rate. Instead of throttling RUB2 itself (T-2026-CU-9050-050,
wontfix — RUB2 stays untouched in its own channel), MAX1 runs the same model as
a SEPARATE bot with a selective gate and its own tag/channel.

Only the selection lives here, and it is deliberately pure/DB-free so
backtest/test_max1_gate.py can pin it without a database. The bot supplies the
already-scored candidates of one scan plus the current rolling-24h post count;
this module decides which few, if any, become MAX1 posts.

The throttle has two independent halves — either alone would be leaky:

  * a HIGH probability floor (MAX1_MIN_PROB, never below the artifact
    threshold). It is the actual selector: the 044 live curve maps 0.90 → 2.6
    posts/day, 0.91 → 1.8, 0.93 → 1.3.
  * a HARD rolling-24h cap (MAX1_MAX_PER_DAY) as the backstop, so a volatile
    day cannot turn "1-3 trades" into twenty. Rolling 24h, not calendar day —
    a calendar cap lets 23:50 + 00:10 fire 2·N inside twenty minutes.

Shape mirrors core/aim2_topn.py on purpose (same problem, same proven
solution), but deliberately as its OWN module: it carries MAX1's env namespace
and candidate shape, and a future change to the AIM2-TOPN selection must not
silently move a live MAX1 gate (the X-R1 lesson, OPUS-HANDOFF Falle 2).

Posting is behind MAX1_LIVE_POSTING (default OFF) — arming it, and the final
threshold/cap numbers, are Michi's call (OPUS-HANDOFF §6). Nothing here does it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Posting tag AND cooldown key — the default tag only; the live tag always comes
#: from the artifact meta (`meta.model_id`, harte Regel 6 / Falle 16). Kept short:
#: it lands in trade_cooldowns.module (varchar(10), COOLDOWN_MODULE_MAX_LEN).
MODEL_ID = "MAX1"

#: Live artifact path (repo root). Promotion of staging_models/max1_model_SHORT.pkl
#: into this path is an operator decision — until then the bot idles (Falle 3).
ARTIFACT_PATH = "max1_model_SHORT.pkl"

#: SHORT-only by construction: the RUB2 LONG side has no deployable model
#: (MODEL_INTENT §8 — the retrain found no profitable LONG operating point).
DIRECTION = "SHORT"

# Conservative defaults for the day the operator first flips the gate: the
# selective end of the 0.90-0.93 band from the 044 curve (~1.3 posts/day) and a
# cap at the top of the 1-3 target. Both env-overridable, both confirmed by Michi.
DEFAULT_MIN_PROB = 0.93
DEFAULT_MAX_PER_DAY = 3


@dataclass(frozen=True)
class Max1Config:
    live: bool
    min_prob: float
    max_per_day: int


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip() == "1"


def load_config() -> Max1Config:
    """Read the MAX1 knobs from the environment.

    MAX1_LIVE_POSTING defaults OFF (default-off gate, shadow-first). max_per_day
    clamps to >= 1, min_prob into [0, 1]; garbage falls back to the defaults.
    """
    try:
        max_per_day = int(os.getenv("MAX1_MAX_PER_DAY", str(DEFAULT_MAX_PER_DAY)))
    except ValueError:
        max_per_day = DEFAULT_MAX_PER_DAY
    try:
        min_prob = float(os.getenv("MAX1_MIN_PROB", str(DEFAULT_MIN_PROB)))
    except ValueError:
        min_prob = DEFAULT_MIN_PROB
    return Max1Config(
        live=_env_flag("MAX1_LIVE_POSTING", False),
        min_prob=min(1.0, max(0.0, min_prob)),
        max_per_day=max(1, max_per_day),
    )


@dataclass(frozen=True)
class Max1Candidate:
    symbol: str
    prob: float  # raw predict_proba — the domain the threshold was picked on
    close_price: float


def select_signals(
    candidates: list[Max1Candidate],
    max_per_day: int,
    min_prob: float,
    posts_last_24h: int,
) -> list[Max1Candidate]:
    """Pick this scan's MAX1 posts from the already-scored candidates.

    Rules (all pinned by backtest/test_max1_gate.py):
      1. Eligible = prob >= min_prob (the caller passes the EFFECTIVE floor,
         i.e. max(artifact threshold, configured min_prob) — MAX1 must never
         gate looser than the model's own operating point).
      2. Dedupe per symbol: the strongest survives. One scan must not spend two
         of the day's few slots on the same coin.
      3. Deterministic order: probability desc, then symbol. The tie-break is
         stable, so a shadow run and a later live run agree on which candidate
         wins the last remaining slot.
      4. Hard cap: at most max(0, max_per_day - posts_last_24h) selections.
         posts_last_24h is the rolling-24h count the bot reads back from
         ml_predictions_master, which holds shadow AND live rows — so the cap
         bites identically in both modes and the shadow is a faithful preview.

    Returns the ordered subset to post (possibly empty — a valid outcome).
    """
    remaining = max_per_day - posts_last_24h
    if remaining <= 0:
        return []

    best: dict[str, Max1Candidate] = {}
    for c in candidates:
        if c.prob < min_prob:
            continue
        cur = best.get(c.symbol)
        if cur is None or c.prob > cur.prob:
            best[c.symbol] = c

    ranked = sorted(best.values(), key=lambda c: (-c.prob, c.symbol))
    return ranked[:remaining]
