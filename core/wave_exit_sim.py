# core/wave_exit_sim.py — high-fidelity trade replay engine (T-2026-KYT-9050-035).
#
# Pure, DB-free sequencing engine. Replays ONE signal (multi-entry DCA geometry,
# SL, laddered targets) over a wick-aware candle series and reports, per entry-
# leg: how many targets were reached, the close price, the exit reason and time.
# The %-PnL math lives in `core.realized_pnl` (shared trainer/serving/replay
# builder, Regel #7) — this module only decides the (targets_hit, close_price)
# that feed it, so there is exactly ONE definition of realised PnL fleet-wide.
#
# Backbone = candles, not the 10s tape (T-035 discovery)
# ------------------------------------------------------
# `ticker_10s` turned out to be a ~40s-sampled, gappy snapshot: it misses ~81%
# of the stop-loss touches the exchange 1h/5m candles capture, which inflated a
# pure-tick replay's realised PnL 2.7x. So touch detection runs on COMPLETE,
# wick-aware 5m OHLC candles (12x finer than the live monitor's 1h) — this
# reproduces closed_ai_signals faithfully. The 10s ticks keep exactly one job in
# the exit path: resolving SL-vs-TP ORDER inside a single candle that touched
# both (the one thing 8_ai_trade_monitor can only guess with its conservative
# SL-first rule). That resolver is INJECTED (`order_resolver`) so this module
# stays pure and DB-free; when no ticks cover the candle it falls back to
# SL-first, exactly like the monitor.
#
# Fidelity contract — mirrors 8_ai_trade_monitor:
#   * SL checked before targets each candle; SL-first at ambiguity unless the
#     injected 10s resolver proves TP came first.
#   * After TP1 the leg's SL trails to its own entry (breakeven); after TP k>=2
#     to targets[k-2] (monitor rule).
#   * ALL-targets closes the remainder at the last target; a leg still open at
#     series end is marked-to-market at the last close (trade really still open).
#
# Multi-entry DCA model (operator decision, T-035): each Cornix entry (entry1 =
# CMP/market, entry2 = DCA limit) is an INDEPENDENT sub-leg with its own weight
# and entry price, sharing the absolute target/SL levels and the trailing rule.
# entry1 fills at the first candle; entry2 fills the first candle its price is
# touched. An untouched entry contributes zero (the position was that much
# smaller). Total realised = weight-sum over filled legs.

from __future__ import annotations

import numpy as np

# order_resolver return values
FIRST_SL = "sl"
FIRST_TP = "tp"


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY / EXIT SEQUENCING (per leg, over the candle series)
# ─────────────────────────────────────────────────────────────────────────────


def _leg_exit(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    fill_idx: int,
    is_long: bool,
    entry: float,
    sl: float,
    targets: list[float],
    order_resolver=None,
) -> dict:
    """Wick-aware ladder + trailing-SL scan for one filled leg, from `fill_idx`.

    Returns (targets_hit, close_price, exit_reason, exit_idx, trailed_sl).
    When a candle's wick touches BOTH the trailed SL and the next target, the
    order is ambiguous: `order_resolver(idx, is_long, sl_level, tp_level)` (10s
    ticks) decides; absent a resolver or ticks it defaults SL-first (monitor
    convention). `close_price` is exactly the price the remaining fraction
    realises at, so `core.realized_pnl.weighted_move_pct` reproduces leg PnL.
    """
    n = len(highs)
    cur_sl = float(sl)
    next_tp = 0
    exit_reason = None
    close_price = None
    exit_idx = fill_idx  # always overwritten below; init to a valid int (not None)

    i = fill_idx
    while i < n:
        hi, lo = highs[i], lows[i]
        sl_hit = (lo <= cur_sl) if is_long else (hi >= cur_sl)
        tp_hit = (hi >= targets[next_tp]) if is_long else (lo <= targets[next_tp])

        if sl_hit and tp_hit:
            first = order_resolver(i, is_long, cur_sl, targets[next_tp]) if order_resolver else FIRST_SL
            if first != FIRST_TP:
                close_price = cur_sl
                exit_reason = f"sl_after_tp{next_tp}"
                exit_idx = i
                break
            # else: TP proved first → fall through to the TP block below
        elif sl_hit:
            close_price = cur_sl
            exit_reason = f"sl_after_tp{next_tp}"
            exit_idx = i
            break

        # Cross every target this candle's high/low reaches; trail the SL.
        crossed = False
        while next_tp < len(targets) and ((hi >= targets[next_tp]) if is_long else (lo <= targets[next_tp])):
            crossed = True
            next_tp += 1
            if next_tp == 1:
                cur_sl = entry  # breakeven after TP1
            else:
                cur_sl = float(targets[next_tp - 2])
        if next_tp >= len(targets):
            close_price = float(targets[-1])
            exit_reason = "all_targets"
            exit_idx = i
            break
        # If TP was proved first AND after crossing the SL now sits below/above
        # where the same candle's wick already was, the monitor would still let
        # the trailed SL arm on the NEXT candle — matches its per-candle loop.
        _ = crossed
        i += 1

    if exit_reason is None:  # never stopped / never completed → open at series end
        close_price = float(closes[n - 1]) if n > fill_idx else float(entry)
        exit_reason = "open_at_end"
        exit_idx = n - 1 if n > fill_idx else fill_idx

    return {
        "targets_hit": next_tp,
        "close_price": close_price,
        "exit_reason": exit_reason,
        "exit_idx": int(exit_idx),
        "trailed_sl": cur_sl,
    }


def _fill_index(highs: np.ndarray, lows: np.ndarray, is_long: bool, entry: float, start_idx: int) -> int | None:
    """First candle at/after `start_idx` whose wick touches the limit `entry`.

    LONG DCA entry sits below CMP → fills when a low dips to it (low <= entry);
    SHORT DCA entry sits above CMP → fills when a high rises to it (high >= entry).
    Returns None if never touched within the series.
    """
    for i in range(start_idx, len(highs)):
        if (is_long and lows[i] <= entry) or (not is_long and highs[i] >= entry):
            return i
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL REPLAY (position = one or more entry legs)
# ─────────────────────────────────────────────────────────────────────────────


def simulate_signal(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    direction: str,
    entries: list[tuple[float, float]],
    sl: float,
    targets: list[float],
    *,
    entry1_market: bool = True,
    order_resolver=None,
) -> dict:
    """Replay one signal over wick-aware candles.

    `entries` is `[(entry_price, weight), ...]`; `entries[0]` is CMP/entry1 and,
    with `entry1_market=True`, fills at candle 0 (market at signal). Every other
    leg is a limit that fills on first wick touch. Unfilled legs report
    `filled=False`. Pure geometry — no PnL %, no leverage (that stays in the
    shared builder). `order_resolver` is threaded to `_leg_exit`.
    """
    is_long = str(direction).upper() == "LONG"
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)
    tps = [float(t) for t in targets]
    if len(highs) == 0 or not tps:
        return {"legs": [], "any_filled": False, "n_candles": int(len(highs))}

    legs = []
    for j, (ep, w) in enumerate(entries):
        ep = float(ep)
        if j == 0 and entry1_market:
            fill_idx: int | None = 0
        else:
            fill_idx = _fill_index(highs, lows, is_long, ep, 0)
        if fill_idx is None:
            legs.append(
                {
                    "entry": ep,
                    "weight": float(w),
                    "filled": False,
                    "fill_idx": None,
                    "targets_hit": 0,
                    "close_price": None,
                    "exit_reason": "entry_not_filled",
                    "exit_idx": None,
                }
            )
            continue
        res = _leg_exit(highs, lows, closes, fill_idx, is_long, ep, sl, tps, order_resolver)
        legs.append({"entry": ep, "weight": float(w), "filled": True, "fill_idx": int(fill_idx), **res})

    return {
        "legs": legs,
        "any_filled": any(leg["filled"] for leg in legs),
        "n_candles": int(len(highs)),
        "direction": "LONG" if is_long else "SHORT",
        "n_targets": len(tps),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MARK-TO-MARKET TRAJECTORY (unrealised wave — Phase-2 overlay substrate)
# ─────────────────────────────────────────────────────────────────────────────


def mark_to_market_series(
    prices: np.ndarray,
    direction: str,
    entries: list[tuple[float, float]],
    sl: float,
    targets: list[float],
    *,
    highs: np.ndarray | None = None,
    lows: np.ndarray | None = None,
    entry1_market: bool = True,
    order_resolver=None,
) -> np.ndarray:
    """Per-step open-position PnL fraction (locked + unrealised), for overlays.

    This is the "wave" the operator watches: at each step the total mark =
    fractions already realised at reached targets (locked) PLUS the still-open
    remainder marked at the current price. Peaks are the unrealised tops that
    evaporate under hold-to-TP/SL. Fraction of nominal (not %, not levered),
    aligned 1:1 with `prices`.

    `prices` is the per-step mark price (5m close, or the 10s tape for a finer
    wave). `highs`/`lows` default to `prices` (using closes for touch too); pass
    the candle wicks to detect ladder crossings the closes would miss. Pass the
    same `order_resolver` as the realised replay so the wave's exit index agrees
    with it on both-touch candles. Used ONLY to drive Phase-2 close rules; the
    headline realised metric goes through `core.realized_pnl`.
    """
    is_long = str(direction).upper() == "LONG"
    prices = np.asarray(prices, dtype=float)
    hi = np.asarray(highs, dtype=float) if highs is not None else prices
    lo = np.asarray(lows, dtype=float) if lows is not None else prices
    n = len(prices)
    tps = [float(t) for t in targets]
    out = np.zeros(n, dtype=float)
    if n == 0 or not tps:
        return out

    def signed(entry: float, p: float) -> float:
        return (p - entry) / entry if is_long else (entry - p) / entry

    frac = 1.0 / len(tps)
    for j, (ep, w) in enumerate(entries):
        ep = float(ep)
        if j == 0 and entry1_market:
            fidx: int | None = 0
        else:
            fidx = _fill_index(hi, lo, is_long, ep, 0)
        if fidx is None:
            continue
        res = _leg_exit(hi, lo, prices, fidx, is_long, ep, sl, tps, order_resolver)
        exit_idx = int(res["exit_idx"])
        next_tp = 0
        locked = 0.0
        leg_val = 0.0
        for i in range(fidx, n):
            if i <= exit_idx:
                p_hi, p_lo = hi[i], lo[i]
                while next_tp < len(tps) and ((p_hi >= tps[next_tp]) if is_long else (p_lo <= tps[next_tp])):
                    locked += frac * signed(ep, tps[next_tp])
                    next_tp += 1
                open_frac = 1.0 - next_tp * frac
                if i == exit_idx:
                    leg_val = locked + open_frac * signed(ep, res["close_price"])
                    out[i] += w * leg_val
                else:
                    out[i] += w * (locked + open_frac * signed(ep, prices[i]))
            else:
                out[i] += w * leg_val
    return out


# ─────────────────────────────────────────────────────────────────────────────
# PHASE-2 OVERLAY TRIGGERS (pure — operate on a mark-to-market series)
# ─────────────────────────────────────────────────────────────────────────────


def trailing_tp_trigger(mtm: np.ndarray, x_frac: float, activation: float = 0.0) -> int | None:
    """First step where the MTM wave retraces `x_frac` of its running peak.

    Overlay (a): once the trade is in profit (running peak > `activation`), close
    when the mark gives back a fraction `x_frac` of that peak, i.e.
    `mtm[i] <= peak * (1 - x_frac)`. Returns the trigger index, or None if it
    never fires (→ the trade rides to its natural TP/SL exit). Only arms above
    `activation` so an underwater trade is never "trailed" out of a loss early.
    """
    peak = -np.inf
    thresh_mult = 1.0 - float(x_frac)
    for i, v in enumerate(np.asarray(mtm, dtype=float)):
        if v > peak:
            peak = v
        if peak > activation and v <= peak * thresh_mult:
            return i
    return None


def portfolio_trailing_trigger(agg: np.ndarray, y_frac: float, activation: float = 0.0) -> int | None:
    """First step where an AGGREGATE unrealised curve retraces `y_frac` of its peak.

    Overlay (c): same shape as `trailing_tp_trigger` but applied to the summed
    open-position mark across all trades alive at each grid step — the visible
    portfolio wave. The caller flattens every open trade at the returned index.
    """
    return trailing_tp_trigger(agg, y_frac, activation)


def portfolio_circuit_breaker(trades: list[dict], glen: int, y_frac: float) -> dict[int, int]:
    """Overlay (c) walk — decide which open trades get flattened at which grid step.

    `trades[i] = {"gi": grid-index array, "lm": levered-mark array}` (same length,
    a trade's per-candle account mark placed on the common grid). Walks the grid:
    trades enter at `gi[0]`, contribute their current mark to the aggregate open
    wave, and leave naturally at `gi[-1]`. When the aggregate retraces `y_frac`
    from its running peak, EVERY open trade is flattened at that grid step. The
    peak is reset both after a flatten AND whenever the open book empties — a wave
    has no peak when nothing is open, so the next cohort starts fresh (this reset
    is the fix for the stale-peak bug that otherwise flattens every newly-entered
    trade at its entry candle).

    Returns `{trade_index: flatten_grid_step}` for the flattened trades; trades
    absent from the map ran to their natural close.
    """
    enters: dict[int, list[int]] = {}
    nat_close: dict[int, list[int]] = {}
    for idx, t in enumerate(trades):
        enters.setdefault(int(t["gi"][0]), []).append(idx)
        nat_close.setdefault(int(t["gi"][-1]), []).append(idx)

    def mark_at(t: dict, g: int) -> float:
        pos = int(np.searchsorted(t["gi"], g, side="right")) - 1
        pos = max(0, min(pos, len(t["lm"]) - 1))
        return float(t["lm"][pos])

    open_set: dict[int, None] = {}
    flat_at: dict[int, int] = {}
    peak = 0.0
    for g in range(glen):
        for idx in enters.get(g, []):
            open_set[idx] = None
        agg = sum(mark_at(trades[idx], g) for idx in open_set)
        if agg > peak:
            peak = agg
        if peak > 0 and open_set and agg <= peak * (1.0 - y_frac):
            for idx in list(open_set):
                flat_at[idx] = g
                del open_set[idx]
            peak = 0.0
        for idx in nat_close.get(g, []):
            open_set.pop(idx, None)
        if not open_set:  # no open wave → no peak; next cohort starts fresh
            peak = 0.0
    return flat_at
