"""state_machine.py — the causal Stoic 1-2-3 signal generator (Phase 2).

Two independent per-direction sub-machines (long, short) run in a single
forward pass over closed bars:

    WAIT -> STEP1  meaningful break of BOTH MAs (close, k*ATR) + HTF location gate
         -> STEP2  a retest forms a base; its Boundary is FIXED here
         -> STEP3  a later bar closes through the Boundary by k*ATR  => ENTRY

Exit = the complete opposite 1-2-3 (stop-and-reverse): a short completion flips a
long to short and vice-versa. The emitted value is the target position at each
bar (decided at that bar's close); the GARCH harness applies it to the next
bar's return (`ret.shift(-1)`), so there is no double counting and no lookahead.

Invariants (the 5 distortions, enforced here):
  * #1 breaks are close-based with a k*ATR margin (never a wick) — `meaningful_break`.
  * #2 the HTF gate is checked in STEP1, as-of the setup bar — no retro-fit.
  * #3 the Boundary comes from base bars with index <= base_end, and STEP3 only
    fires at t > base_end — the boundary is fixed before the breakout bar.
  * #4 WAIT->STEP3 is impossible; STEP2 (a detected base) is mandatory.
  * #5 one forward pass, a set position is never revised by a later bar
    (repaint-free). Both #3 and #5 are proven by the prefix-stability test.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from params import StoicParams
from rules import compute_indicators, detect_base, htf_location_series, meaningful_break


def _opp(direction: str) -> str:
    return "short" if direction == "long" else "long"


class _DirectionMachine:
    """Tracks one direction's WAIT->STEP1->STEP2 progress and reports the bar at
    which STEP3 (entry) completes. Reads only bars with index <= t."""

    WAIT, STEP1, STEP2 = 0, 1, 2

    def __init__(self, direction: str, ind: pd.DataFrame, htf_ok: np.ndarray, p: StoicParams):
        self.d = direction
        self.ind = ind
        self.htf_ok = htf_ok
        self.p = p
        self._reset()

    def _reset(self) -> None:
        self.state = self.WAIT
        self.break_bar = -1
        self.base_end = -1
        self.boundary = np.nan
        self.base_high = np.nan
        self.base_low = np.nan

    def _mb(self, close: float, level: float, atr: float) -> bool:
        return meaningful_break(close, level, atr, self.p.break_k_atr, self.d)

    def step(self, t: int) -> bool:
        row = self.ind.iloc[t]
        close, atr, mf, ms = row["close"], row["atr"], row["ma_fast"], row["ma_slow"]
        if not (np.isfinite(atr) and np.isfinite(ms) and np.isfinite(mf)):
            return False

        # invalidation: a meaningful break of the slow MA against the setup kills it
        if self.state in (self.STEP1, self.STEP2) and meaningful_break(
            close, ms, atr, self.p.break_k_atr, _opp(self.d)
        ):
            self._reset()

        if self.state == self.WAIT:
            # STEP1: close breaks BOTH MAs by k*ATR AND the HTF location gate passes
            if self.htf_ok[t] and self._mb(close, mf, atr) and self._mb(close, ms, atr):
                self.state = self.STEP1
                self.break_bar = t
            return False

        if self.state == self.STEP1:
            if t - self.break_bar > self.p.max_wait_step1:
                self._reset()
                return False
            w0 = t - self.p.base_window + 1
            if w0 > self.break_bar:  # the base must form strictly after the impulse
                window = self.ind.iloc[w0 : t + 1]
                base = detect_base(window, atr, self.p, self.d, mf)
                if base is not None:
                    self.boundary = base["boundary"]
                    self.base_high, self.base_low = base["base_high"], base["base_low"]
                    self.base_end = t
                    self.state = self.STEP2
            return False

        if self.state == self.STEP2:
            if t - self.base_end > self.p.max_wait_step2:
                self._reset()
                return False
            # base failed: price closed out the far side of the base
            if (self.d == "long" and close < self.base_low) or (self.d == "short" and close > self.base_high):
                self._reset()
                return False
            # STEP3: a LATER bar closes through the fixed boundary by k*ATR -> entry
            if t > self.base_end and self._mb(close, self.boundary, atr):
                self._reset()
                return True
            return False

        return False


def generate_signals(df: pd.DataFrame, htf: pd.DataFrame, p: StoicParams | None = None) -> pd.Series:
    """Emit the target position (-1/0/1) at each bar of ``df``.

    ``df``  : LTF OHLC(date), CLOSED bars only (the signal timeframe).
    ``htf`` : HTF (date, close), CLOSED bars — the location gate.
    Returns a pd.Series aligned to ``df`` rows. 0 until the first completed 1-2-3,
    then stop-and-reverse between +/-1 on each subsequent opposite completion.
    """
    p = p or StoicParams()
    p.validate()
    ind = compute_indicators(df, p)
    loc = htf_location_series(ind, htf, p)
    long_m = _DirectionMachine("long", ind, loc["htf_long_ok"].to_numpy(), p)
    short_m = _DirectionMachine("short", ind, loc["htf_short_ok"].to_numpy(), p)

    positions = np.zeros(len(ind), dtype=int)
    pos = 0
    for t in range(len(ind)):
        long_done = long_m.step(t)
        short_done = short_m.step(t)
        if long_done:  # deterministic tiebreak: long wins a (near-impossible) tie
            pos = 1
        elif short_done:
            pos = -1
        positions[t] = pos
    return pd.Series(positions, index=ind.index, name="signal")
