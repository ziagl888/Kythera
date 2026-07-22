"""vol_target.py — turn a volatility forecast into a position-size multiplier.

The whole idea in one line:

    size = target_vol / forecast_vol        (capped so it never does anything insane)

Storm coming -> smaller position. Calm ahead -> bigger position. Same trades,
different sizes. This is the "how much" answer — it says nothing about direction.

Adapted from milesdeutscher/garchmethod (MIT) — see LICENSE.upstream. Kept
free of arch/ccxt so it imports and tests under the plain fleet Python.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Caps on the multiplier. Storm never shrinks a position below MIN_SIZE; calm
# never levers it past MAX_LEVERAGE. Same defaults as upstream.
MAX_LEVERAGE = 2.0
MIN_SIZE = 0.25


def size_from_vol(
    forecast_vol_ann: float,
    target_vol_ann: float = 15.0,
    max_leverage: float = MAX_LEVERAGE,
    min_size: float = MIN_SIZE,
) -> float:
    """Scalar size multiplier from an annualized vol forecast (%).

    A missing / non-positive / NaN forecast falls back to ``min_size`` — the
    conservative choice (we never up-size on a broken forecast).
    """
    if forecast_vol_ann is None or forecast_vol_ann <= 0 or np.isnan(forecast_vol_ann):
        return float(min_size)
    return float(np.clip(target_vol_ann / forecast_vol_ann, min_size, max_leverage))


def size_series(
    fcast_vol_ann: pd.Series,
    target_vol_ann: float = 15.0,
    max_leverage: float = MAX_LEVERAGE,
    min_size: float = MIN_SIZE,
) -> pd.Series:
    """Vectorized ``size_from_vol`` for backtests.

    NaN (no forecast yet) and non-positive vols both collapse to ``min_size``,
    matching the scalar path so the two can never disagree.
    """
    s = target_vol_ann / fcast_vol_ann
    s = s.where(fcast_vol_ann > 0)  # <=0 vol -> NaN -> min_size below
    return s.clip(lower=min_size, upper=max_leverage).fillna(min_size)


def apply_sizing(signal, size_multiplier):
    """Compose the direction engine with the vol-target sizing layer.

        order_size = signal x size_multiplier

    ``signal`` is the direction decision (``{-1, 0, 1}`` or continuous) produced
    by Kythera's existing engines; ``size_multiplier`` is ``size_from_vol`` /
    ``size_series`` output. Works elementwise for scalars or aligned pandas
    Series. This is the seam the audit calls "Direction x Size": the sizing
    layer sits *behind* the direction engine and never flips a sign.
    """
    return signal * size_multiplier
