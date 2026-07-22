"""params.py — every fuzzy knob of the Stoic 1-2-3 system made explicit.

Phase 1 of the task is exactly this: turn the article's discretionary language
into named, defaulted, sweepable parameters. Nothing in the state machine reads
a magic number — it all comes from here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StoicParams:
    # --- moving averages (the "both MAs" the break must clear) ---
    ma_type: str = "ema"  # "ema" | "sma" (article unspecified -> parametrized)
    ma_fast: int = 10
    ma_slow: int = 20

    # --- ATR (the volatility unit every threshold is measured in) ---
    atr_period: int = 14

    # --- "meaningful break/close": close beyond the level by k * ATR ---
    break_k_atr: float = 0.5  # a wick-through never counts; only close

    # --- base / consolidation detector (the retest) ---
    base_window: int = 5  # N consecutive bars that must be tight
    base_max_range_atr: float = 1.5  # window range < m * ATR => it is a base
    retest_touch: bool = True  # require the base to pull back to the fast MA

    # --- housekeeping / invalidation timeouts (bars) ---
    max_wait_step1: int = 20  # give up on a stale impulse
    max_wait_step2: int = 20  # give up on a stale base

    # --- HTF location filter (the hardest, most important gate) ---
    htf_ma_type: str = "ema"
    htf_ma_period: int = 20  # trend MA on the higher timeframe
    htf_slope_lookback: int = 3  # rising/falling over this many HTF bars
    htf_require_price_side: bool = True  # price must be on the trend side of the HTF MA

    def validate(self) -> None:
        if self.ma_fast >= self.ma_slow:
            raise ValueError("ma_fast must be < ma_slow")
        if self.ma_type not in ("ema", "sma") or self.htf_ma_type not in ("ema", "sma"):
            raise ValueError("ma_type/htf_ma_type must be 'ema' or 'sma'")
        for name in ("atr_period", "base_window", "ma_fast", "ma_slow", "htf_ma_period"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
