# core/realized_pnl.py — leveraged realized-PnL math for closed trades.
#
# Shared by the Sentiment-Tracker realized-PnL report (23_market_tracker) and
# the DB-free tests in backtest/test_realized_pnl.py. Pure functions only —
# no DB, no I/O, no imports beyond stdlib.
#
# Position model (operator spec 2026-07-13, T-2026-CU-9050-115):
# the stake is split EQUALLY across the N published targets. Hitting target i
# realises 1/N of the position at that target's price; whatever was not
# realised via targets (N-k parts) closes at close_price (SL / timeout /
# ALL-TARGETS-HIT, where close_price equals the last target anyway).
# The weighted price move is then multiplied by the leverage; losses are
# clamped at -100% (a cross-margin position cannot lose more than its margin
# for reporting purposes — deeper losses in the data are artefacts).

from __future__ import annotations

# |price move| above this bound (pre-leverage, in %) is treated as a data bug
# (same rationale as OUTCOME_MAX_ABS_PNL_PCT in 23_market_tracker).
MAX_ABS_MOVE_PCT = 100.0


def parse_leverage(lev: object) -> float | None:
    """Parse leverage from persisted text ("20x", "25X", "20", 20).

    Returns None for missing / unparseable / non-positive values — callers
    must EXCLUDE such rows instead of guessing a default (exact-only rule).
    """
    if lev is None:
        return None
    if isinstance(lev, (int, float)):
        value = float(lev)
        return value if value > 0 else None
    text = str(lev).strip().lower().removesuffix("x").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def _signed_move_pct(sign: float, entry: float, price: float) -> float:
    """Direction-corrected price move in % (LONG sign=+1, SHORT sign=-1)."""
    return sign * (price - entry) / entry * 100.0


# ── Persisted ≠ traded (T-2026-KYT-9050-012) ────────────────────────────
#
# The position model below splits the stake into `n` equal legs, with
# n = len(targets) from the DB. This only holds as long as an emitter persists
# exactly the targets it posts via Cornix. Two do not:
#
#   ROM1 (28_signal_orchestrator:525/574)  persists t_cands[:20], posts 3
#   AIM2 (15_ai_master_bot:544/589)        persists full list, posts 3
#
# Cornix never saw the remaining TPs — the stake really rode on 3 legs, not 20.
# The model dilutes TP profits by (n-k)/n and UNDERESTIMATES both bots
# systematically: for ROM1 over 7,769 closed trades (30 days) by factor 1.43
# on sum, median 1.51% instead of 5.18%; for AIM2 over 2,345 trades by 1.05.
#
# The number belongs here, not in every report: `weighted_move_pct` and
# `realized_pnl_pct` now optionally take the model and trim themselves. Without
# `model` they behave byte-equal as before — no caller changes silently.
#
# The bots themselves stayed untouched at the time (operator decision 2026-08-02:
# fix measurement only), because trimming the persisted list changes monitor 8's
# scoring semantics for RUNNING trades — SL trailing and the ALL-TARGETS-close
# condition then see 3 instead of 20 legs.
#
# T-2026-KYT-9050-099 closed the ROM1 side at the source: 28_signal_orchestrator
# now persists exactly the published slice (`rom1_published_targets`). This table
# stays as it is, and BOTH entries stay correct:
#
#   * ROM1 rows written before that deploy still carry the 20-target pool, and
#     their posted ladder is the first three of it — `targets[:3]`.
#   * ROM1 rows written after it carry exactly 3, so `targets[:3]` is identity.
#
# So the same trim is right on both eras and the reports need no cutoff date. Do
# NOT drop the ROM1 entry because "the bot is fixed now" — that would re-inflate
# every historical row back to a 20-leg position model.
#
# T-2026-KYT-9050-100 closed the AIM2 side the same way (15_ai_master_bot now
# persists `targets[:n_show]`), so BOTH entries are now historical-only — and both
# stay for exactly the same reason: `[:3]` is identity on rows written after the
# respective deploy and still the posted three on every row before it. This table
# is therefore no longer a description of live bot behaviour but a permanent
# decoder for the archive. That is not a reason to prune it: ~2,400 AIM2 and ~8,000
# ROM1 closed rows still carry the long ladders, and every report that reads them
# needs this trim to keep the position model at 3 legs.
PUBLISHED_TARGET_COUNT: dict[str, int] = {
    "ROM1": 3,
    "AIM2": 3,
    # EPD2 (T-2026-KYT-9050-147): the legacy leg stored its FULL raw pool (up
    # to 20 targets) while the Cornix message always published [:3]. Since
    # T-147 the bot stores the thinned+capped 3, so — same as ROM1/AIM2 above —
    # this entry is identity on new rows and the archive decoder for every row
    # before the deploy.
    "EPD2": 3,
}


def traded_targets(model: object, targets: list[float]) -> list[float]:
    """The targets against which the trade actually ran (Cornix perspective).

    For emitters without persist/publish gaps, the unchanged list.
    """
    n = PUBLISHED_TARGET_COUNT.get(str(model or "").strip().upper())
    return list(targets)[:n] if n else list(targets)


def weighted_move_pct(
    direction: str,
    entry: float,
    close_price: float,
    targets: list[float],
    targets_hit: int,
    model: object = None,
) -> float | None:
    """Target-weighted price move in % (without leverage), direction-corrected.

    Returns None on invalid input (no targets, non-positive prices, unknown
    direction) — the report skips those rows rather than approximating.

    `model` is optional and changes nothing for emitters without persist/publish
    gaps. For ROM1/AIM2 it trims the target list to the actually posted length
    and caps `targets_hit` accordingly — otherwise a trade would get credit for
    TPs Cornix never saw (measured: 139 of 7,769 ROM1 trades with targets_hit > 3).
    See PUBLISHED_TARGET_COUNT.
    """
    try:
        entry_f = float(entry)
        close_f = float(close_price)
    except (TypeError, ValueError):
        return None
    if entry_f <= 0 or close_f <= 0 or not targets:
        return None

    side = str(direction or "").strip().upper()
    if side not in ("LONG", "SHORT"):
        return None
    sign = 1.0 if side == "LONG" else -1.0

    try:
        target_prices = [float(t) for t in targets]
    except (TypeError, ValueError):
        return None
    if any(t <= 0 for t in target_prices):
        return None

    # Trim before leg counting: n IS the position model.
    target_prices = traded_targets(model, target_prices)
    if not target_prices:
        return None

    n = len(target_prices)
    try:
        k = int(targets_hit)
    except (TypeError, ValueError):
        k = 0
    k = max(0, min(k, n))

    # Outlier gate on the RAW close leg, not just on the weighted result:
    # at k of N targets hit, the ladder dilutes a data-bug leg by factor N/(N-k) —
    # a +150% leg would otherwise pass as (5+10+15+150)/4 ≈ 45% through the
    # overall filter (review finding 2026-07-13).
    if abs(_signed_move_pct(sign, entry_f, close_f)) > MAX_ABS_MOVE_PCT:
        return None

    hit_moves = sum(_signed_move_pct(sign, entry_f, t) for t in target_prices[:k])
    rest_move = (n - k) * _signed_move_pct(sign, entry_f, close_f)
    return (hit_moves + rest_move) / n


def realized_pnl_pct(
    direction: str,
    entry: float,
    close_price: float,
    targets: list[float],
    targets_hit: int,
    leverage: object,
    model: object = None,
) -> float | None:
    """Realised PnL in % of stake: weighted move × leverage.

    `model` passed to :func:`weighted_move_pct` — without it behaviour stays
    unchanged, with it the caller calculates on the actually traded leg count
    instead of the persisted one (T-2026-KYT-9050-012).

    Clamped at -100% (liquidation floor). Returns None when the move is not
    computable, the leverage is missing/invalid, or the pre-leverage move
    exceeds MAX_ABS_MOVE_PCT (data bug, mirrors the per-bot post's outlier
    filter).
    """
    lev = parse_leverage(leverage)
    if lev is None:
        return None
    move = weighted_move_pct(direction, entry, close_price, targets, targets_hit, model)
    if move is None or abs(move) > MAX_ABS_MOVE_PCT:
        return None
    return max(move * lev, -100.0)
