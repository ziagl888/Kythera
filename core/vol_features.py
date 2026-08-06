# core/vol_features.py — the one definition of "how volatile is this symbol right now".
"""Shared realized-vol builder (T-2026-KYT-9050-112).

``sym_vol_4h`` — the std of 5m percent returns over a 4h window — is the gate
feature the T-110 study validated (AUC 0.79/0.77 out-of-sample) and the T-111
decision backtest priced with money attached. The studies computed it in
``tools/tp1_speed_study.py``; the FIF2 bot needs the identical number at serve
time. Shared-builder rule (CLAUDE.md hard rule 7): one implementation, imported
by both sides, so the study and the bot can never drift apart silently.

Conventions, pinned by ``backtest/test_fif2_bot.py`` and the T-110 tests:

* Returns are simple percent returns of closes, ``(c[i+1]-c[i])/c[i]*100``.
* The std is the population std (ddof=0) over exactly ``window`` returns —
  the study's cumsum formulation, kept bit-compatible.
* ``rolling_std_pct(close, window)[i]`` uses only returns up to and including
  candle ``i`` — nothing after it, ever (hard rule 5 lives here).
* Fewer than ``window`` returns -> NaN / None, never a number computed from a
  shorter window: a bot must not act on a feature it does not have.
"""

from __future__ import annotations

import numpy as np

# 4h of 5m bars — the T-110/T-111 operating point. Studies and the bot import
# this constant rather than restating "48".
VOL_WINDOW_5M = 48
CANDLE_5M_S = 300


def rolling_std_pct(close: np.ndarray, window: int) -> np.ndarray:
    """Std of 5m pct returns over `window` returns, aligned to the candle index.

    ``out[i]`` uses returns up to and including candle ``i`` — never anything
    after it. Computed once per series with cumsums, O(n). Moved verbatim from
    ``tools/tp1_speed_study.py`` (T-110); that module now imports it from here.
    """
    n = len(close)
    out = np.full(n, np.nan)
    if n < 2:
        return out
    r = np.diff(close) / close[:-1] * 100.0
    c1 = np.concatenate(([0.0], np.cumsum(r)))
    c2 = np.concatenate(([0.0], np.cumsum(r * r)))
    # return j covers candles j -> j+1; window of returns ending at return i-1
    for_i = np.arange(window, len(r) + 1)
    mean = (c1[for_i] - c1[for_i - window]) / window
    msq = (c2[for_i] - c2[for_i - window]) / window
    out[for_i] = np.sqrt(np.maximum(0.0, msq - mean * mean))
    return out


def vol_now_pct(closes: np.ndarray, window: int = VOL_WINDOW_5M) -> float | None:
    """The newest rolling-std value, or None when history is too short.

    The serve-time entry point: pass the closes of the newest ``window + 1``
    CLOSED candles (ascending). Returns the same number the study's
    ``rolling_std_pct`` produces at the final index.
    """
    closes = np.asarray(closes, dtype=float)
    if len(closes) < window + 1:
        return None
    v = rolling_std_pct(closes, window)[-1]
    return None if np.isnan(v) else float(v)
