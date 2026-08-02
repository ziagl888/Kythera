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


def _signed_move_pct(sign: float, entry: float, price: float) -> float:
    """Direction-korrigierter Preis-Move in % (LONG sign=+1, SHORT sign=-1)."""
    return sign * (price - entry) / entry * 100.0


# ── Persistiert ≠ gehandelt (T-2026-KYT-9050-012) ────────────────────────────
#
# Das Positionsmodell unten teilt den Einsatz in `n` gleiche Beine, mit
# n = len(targets) aus der DB. Das stimmt nur, solange ein Emitter genau die
# Targets persistiert, die er auch nach Cornix postet. Zwei tun das nicht:
#
#   ROM1 (28_signal_orchestrator:525/574)  persistiert t_cands[:20], postet 3
#   AIM2 (15_ai_master_bot:544/589)        persistiert die volle Liste, postet 3
#
# Cornix hat die übrigen TPs nie gesehen — der Einsatz ritt real auf 3 Beinen,
# nicht auf 20. Das Modell verdünnt den TP-Gewinn dadurch um (n-k)/n und
# UNTERSCHÄTZT beide Bots systematisch: für ROM1 über 7.769 geschlossene Trades
# (30 Tage) um Faktor 1,43 auf der Summe, Median 1,51 % statt 5,18 %; für AIM2
# über 2.345 Trades um Faktor 1,05.
#
# Die Zahl gehört hierher und nicht in jeden Report: `weighted_move_pct` und
# `realized_pnl_pct` nehmen jetzt optional das Modell entgegen und schneiden
# selbst zu. Ohne `model` verhalten sie sich byte-gleich wie bisher — kein
# Aufrufer ändert sich still.
#
# Die Bots selbst bleiben unangetastet (Operator-Entscheid 2026-08-02: nur die
# Messung korrigieren). Ein Kürzen der persistierten Liste würde die
# Scoring-Semantik von Monitor 8 für LAUFENDE Trades ändern — SL-Trailing und
# die ALL-TARGETS-Close-Bedingung sähen 3 statt 20 Stufen.
PUBLISHED_TARGET_COUNT: dict[str, int] = {
    "ROM1": 3,
    "AIM2": 3,
}


def traded_targets(model: object, targets: list[float]) -> list[float]:
    """Die Targets, gegen die der Trade wirklich lief (Cornix-Sicht).

    Für Emitter ohne Persist/Publish-Lücke die unveränderte Liste.
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
    """Target-gewichteter Preis-Move in % (ohne Hebel), direction-korrigiert.

    Returns None on invalid input (no targets, non-positive prices, unknown
    direction) — the report skips those rows rather than approximating.

    `model` ist optional und ändert für Emitter ohne Persist/Publish-Lücke
    nichts. Für ROM1/AIM2 schneidet es die Target-Liste auf die tatsächlich
    gepostete Länge und deckelt `targets_hit` entsprechend — sonst bekäme ein
    Trade Gutschrift für TPs, die Cornix nie hatte (gemessen: 139 von 7.769
    ROM1-Trades mit targets_hit > 3). Siehe PUBLISHED_TARGET_COUNT.
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

    # Vor der Bein-Zählung zuschneiden: n IST das Positionsmodell.
    target_prices = traded_targets(model, target_prices)
    if not target_prices:
        return None

    n = len(target_prices)
    try:
        k = int(targets_hit)
    except (TypeError, ValueError):
        k = 0
    k = max(0, min(k, n))

    # Outlier-Gate auf dem ROHEN Close-Leg, nicht erst auf dem gewichteten
    # Ergebnis: bei k von N getroffenen Targets verdünnt die Staffelung ein
    # Daten-Bug-Leg um den Faktor N/(N-k) — ein +150%-Leg passiert sonst als
    # (5+10+15+150)/4 ≈ 45% den Gesamt-Filter (Review-Finding 2026-07-13).
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
    """Realisierter PnL in % des Einsatzes: gewichteter Move × Hebel.

    `model` durchgereicht an :func:`weighted_move_pct` — ohne es bleibt das
    Verhalten unverändert, mit ihm rechnet der Aufrufer auf der real
    gehandelten Bein-Zahl statt auf der persistierten (T-2026-KYT-9050-012).

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
