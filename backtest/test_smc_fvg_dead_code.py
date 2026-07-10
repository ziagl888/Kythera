# backtest/test_smc_fvg_dead_code.py
"""
Guard tests for P1.26: the SMC FVG entry (16_smc_forex_metals_bot.py) was dead code.

`find_unmitigated_fvgs` scanned for mitigation up to and including the current
candle, using the very same predicate the entry trigger re-evaluates on that
candle. Every FVG that would have fired was therefore already dropped as
"mitigated". The fix stops the mitigation scan before the current candle.

These tests pin three properties:
  1. an FVG tapped by the CURRENT candle survives the scan (entry reachable),
  2. an FVG tapped by an EARLIER candle is still dropped (no over-fix),
  3. the legacy scan range would still kill case 1 (divergence canary) —
     if someone reverts the range, this test fails loudly.

Run with: pytest backtest/test_smc_fvg_dead_code.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_smc_bot():
    """Import 16_smc_forex_metals_bot.py under a stable alias (digit prefix)."""
    spec = importlib.util.spec_from_file_location(
        "smc_forex_metals_bot",
        os.path.join(REPO_ROOT, "16_smc_forex_metals_bot.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {
            "yfinance": mock.MagicMock(),
            "core.database": mock.MagicMock(),
            "core.config": mock.MagicMock(CH_SMC_METALS=-1, CH_SMC_FOREX=-2),
            "core.market_utils": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


smc = _load_smc_bot()


def _df(rows):
    """rows = [(open, high, low, close), ...] — chronological, oldest first."""
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close']).astype(float)


def _legacy_unmitigated(df, direction):
    """The pre-P1.26 scan range — kept here purely as a divergence reference."""
    fvgs = []
    for i in range(2, len(df) - 1):
        if direction == "BULLISH":
            if df['high'].iloc[i - 2] < df['low'].iloc[i] and df['close'].iloc[i - 1] > df['open'].iloc[i - 1]:
                fvgs.append({'top': df['low'].iloc[i], 'bottom': df['high'].iloc[i - 2], 'index': i})
        else:
            if df['low'].iloc[i - 2] > df['high'].iloc[i] and df['close'].iloc[i - 1] < df['open'].iloc[i - 1]:
                fvgs.append({'top': df['low'].iloc[i - 2], 'bottom': df['high'].iloc[i], 'index': i})

    out = []
    for fvg in fvgs:
        mitigated = False
        for j in range(fvg['index'] + 1, len(df)):  # includes the current candle — the bug
            if direction == "BULLISH" and df['low'].iloc[j] <= fvg['top']:
                mitigated = True
                break
            if direction == "BEARISH" and df['high'].iloc[j] >= fvg['bottom']:
                mitigated = True
                break
        if not mitigated:
            out.append(fvg)
    return out


# Bullish FVG at index 3: gap between high[1]=11.0 (bottom) and low[3]=13.0 (top).
# Candle 4 stays above the gap; candle 5 (current) taps into it.
BULL_TAPPED_NOW = _df([
    (10.0, 11.0, 9.5, 10.5),
    (10.5, 11.0, 10.0, 10.8),
    (11.0, 13.5, 10.9, 13.4),
    (13.4, 14.0, 13.0, 13.9),
    (13.9, 14.5, 13.5, 14.2),
    (14.2, 14.3, 12.5, 13.2),  # current: low 12.5 <= top 13.0
])

# Same FVG, but candle 4 already tapped it — genuinely mitigated.
BULL_TAPPED_EARLIER = _df([
    (10.0, 11.0, 9.5, 10.5),
    (10.5, 11.0, 10.0, 10.8),
    (11.0, 13.5, 10.9, 13.4),
    (13.4, 14.0, 13.0, 13.9),
    (13.9, 14.5, 12.0, 14.2),  # taps the gap
    (14.2, 14.3, 13.6, 14.0),
])

# Same FVG, never touched by any later candle.
BULL_UNTOUCHED = _df([
    (10.0, 11.0, 9.5, 10.5),
    (10.5, 11.0, 10.0, 10.8),
    (11.0, 13.5, 10.9, 13.4),
    (13.4, 14.0, 13.0, 13.9),
    (13.9, 14.5, 13.5, 14.2),
    (14.2, 14.6, 13.6, 14.4),
])

# Bearish FVG at index 3: gap between high[3]=17.5 (bottom) and low[1]=19.8 (top).
BEAR_TAPPED_NOW = _df([
    (20.0, 20.5, 19.5, 20.0),
    (20.0, 20.2, 19.8, 20.0),
    (19.9, 20.0, 17.0, 17.2),
    (17.2, 17.5, 16.8, 17.0),
    (17.0, 17.2, 16.5, 16.8),
    (16.8, 18.0, 16.7, 17.6),  # current: high 18.0 >= bottom 17.5
])

BEAR_TAPPED_EARLIER = _df([
    (20.0, 20.5, 19.5, 20.0),
    (20.0, 20.2, 19.8, 20.0),
    (19.9, 20.0, 17.0, 17.2),
    (17.2, 17.5, 16.8, 17.0),
    (17.0, 17.6, 16.5, 17.4),  # taps the gap
    (17.4, 17.5, 16.7, 17.0),
])


def _only_fvg(df, direction):
    fvgs = smc.find_unmitigated_fvgs(df, direction)
    assert len(fvgs) == 1, f"fixture should contain exactly one {direction} FVG, got {fvgs}"
    return fvgs[0]


@pytest.mark.parametrize(
    "df,direction",
    [(BULL_TAPPED_NOW, "BULLISH"), (BEAR_TAPPED_NOW, "BEARISH")],
)
def test_fvg_tapped_by_current_candle_survives_the_scan(df, direction):
    """The core P1.26 regression: a tap on the current candle is an entry, not a mitigation."""
    assert smc.find_unmitigated_fvgs(df, direction), f"{direction} FVG tapped by the current candle was dropped"


@pytest.mark.parametrize(
    "df,direction",
    [(BULL_TAPPED_EARLIER, "BULLISH"), (BEAR_TAPPED_EARLIER, "BEARISH")],
)
def test_fvg_tapped_by_earlier_candle_is_still_mitigated(df, direction):
    """No over-fix: candles before the current one still mitigate as they always did."""
    assert smc.find_unmitigated_fvgs(df, direction) == []


def test_untouched_fvg_survives_but_does_not_trigger():
    fvg = _only_fvg(BULL_UNTOUCHED, "BULLISH")
    curr = BULL_UNTOUCHED.iloc[len(BULL_UNTOUCHED) - 1]
    assert curr['low'] > fvg['top'], "fixture must not tap the gap"


def test_bullish_entry_trigger_is_reachable():
    """Replays the trigger of run_smc_analysis (16:430) against the fixed scan."""
    fvg = _only_fvg(BULL_TAPPED_NOW, "BULLISH")
    curr = BULL_TAPPED_NOW.iloc[len(BULL_TAPPED_NOW) - 1]
    price = float(curr['close'])
    assert curr['low'] <= fvg['top'] and price > (fvg['bottom'] * 0.999)


def test_bearish_entry_trigger_is_reachable():
    """Replays the trigger of run_smc_analysis (16:458) against the fixed scan."""
    fvg = _only_fvg(BEAR_TAPPED_NOW, "BEARISH")
    curr = BEAR_TAPPED_NOW.iloc[len(BEAR_TAPPED_NOW) - 1]
    price = float(curr['close'])
    assert curr['high'] >= fvg['bottom'] and price < (fvg['top'] * 1.001)


@pytest.mark.parametrize(
    "df,direction",
    [(BULL_TAPPED_NOW, "BULLISH"), (BEAR_TAPPED_NOW, "BEARISH")],
)
def test_legacy_scan_range_would_kill_the_entry(df, direction):
    """Divergence canary: the old range() is what made the entry unreachable."""
    assert _legacy_unmitigated(df, direction) == []
    assert smc.find_unmitigated_fvgs(df, direction) != []


@pytest.mark.parametrize(
    "df,direction",
    [(BULL_TAPPED_EARLIER, "BULLISH"), (BEAR_TAPPED_EARLIER, "BEARISH")],
)
def test_fix_and_legacy_agree_on_earlier_mitigation(df, direction):
    """Outside the current candle, the fix changes nothing."""
    assert smc.find_unmitigated_fvgs(df, direction) == _legacy_unmitigated(df, direction)
