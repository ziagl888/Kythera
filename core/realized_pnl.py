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
    """Leverage aus dem persistierten Text ("20x", "25X", "20", 20) parsen.

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


def weighted_move_pct(
    direction: str,
    entry: float,
    close_price: float,
    targets: list[float],
    targets_hit: int,
) -> float | None:
    """Target-gewichteter Preis-Move in % (ohne Hebel), direction-korrigiert.

    Returns None on invalid input (no targets, non-positive prices, unknown
    direction) — the report skips those rows rather than approximating.
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

    n = len(target_prices)
    try:
        k = int(targets_hit)
    except (TypeError, ValueError):
        k = 0
    k = max(0, min(k, n))

    hit_moves = sum(sign * (t - entry_f) / entry_f * 100.0 for t in target_prices[:k])
    rest_move = (n - k) * sign * (close_f - entry_f) / entry_f * 100.0
    return (hit_moves + rest_move) / n


def realized_pnl_pct(
    direction: str,
    entry: float,
    close_price: float,
    targets: list[float],
    targets_hit: int,
    leverage: object,
) -> float | None:
    """Realisierter PnL in % des Einsatzes: gewichteter Move × Hebel.

    Clamped at -100% (liquidation floor). Returns None when the move is not
    computable, the leverage is missing/invalid, or the pre-leverage move
    exceeds MAX_ABS_MOVE_PCT (data bug, mirrors the per-bot post's outlier
    filter).
    """
    lev = parse_leverage(leverage)
    if lev is None:
        return None
    move = weighted_move_pct(direction, entry, close_price, targets, targets_hit)
    if move is None or abs(move) > MAX_ABS_MOVE_PCT:
        return None
    return max(move * lev, -100.0)
