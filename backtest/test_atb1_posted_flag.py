# backtest/test_atb1_posted_flag.py
"""
Unit tests for ATB1's posted-flag on the ml_predictions_master insert (P1.47).

ATB1 logs a prediction row whenever ml_prob >= 0.25 (the shadow band), but only
TRADES when ml_prob >= threshold. The row used to be written with a hardcoded
posted=False, so a live ATB1 trade left only a posted=False row. The market
tracker's created_at JOIN (m.posted = TRUE, P1.44) therefore never matched an
ATB1 row, and open ATB1 positions read as NOW() forever.

The flag now mirrors the live-trade predicate. The boundary is `threshold`, NOT
the 0.25 shadow gate — this is the exact thing a later "simplification" would
get wrong, so it is pinned here.

Run with: pytest backtest/test_atb1_posted_flag.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.config fails hard on missing secrets; the build machine ships an empty .env.
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")


def _load_atb1():
    spec = importlib.util.spec_from_file_location(
        "atb1_bot",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "14_ai_atb_bot.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    # 14_ai_atb_bot imports heavy plotting / DB deps at module load; stub them so
    # the pure helper can be imported standalone and DB-free.
    with mock.patch.dict(
        "sys.modules",
        {
            "core.database": mock.MagicMock(),
            "core.charting": mock.MagicMock(),
            "core.market_utils": mock.MagicMock(),
            "core.trade_utils": mock.MagicMock(),
            "matplotlib": mock.MagicMock(),
            "matplotlib.pyplot": mock.MagicMock(),
            "matplotlib.gridspec": mock.MagicMock(),
            "matplotlib.ticker": mock.MagicMock(),
            "pandas_ta": mock.MagicMock(),
            "scipy.signal": mock.MagicMock(),
            "scipy.stats": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


atb1 = _load_atb1()


# ── posted mirrors the trade decision, not the shadow gate ───────────────────


def test_traded_prediction_is_posted_true():
    # ml_prob at/above threshold => the live trade fires => posted must be True
    assert atb1._atb1_posted_flag(0.80, 0.70) is True


def test_shadow_prediction_is_posted_false():
    # in the shadow band (0.25 <= ml_prob < threshold) => logged but not traded
    assert atb1._atb1_posted_flag(0.40, 0.70) is False


def test_boundary_is_threshold_inclusive():
    # exactly at threshold trades (>= matches send_signal's predicate)
    assert atb1._atb1_posted_flag(0.70, 0.70) is True
    # a hair below does not
    assert atb1._atb1_posted_flag(0.699999, 0.70) is False


def test_boundary_is_not_the_025_shadow_gate():
    """The regression guard: a prediction of 0.30 is above the 0.25 shadow gate
    but below a 0.70 threshold — it must NOT be posted=True. A fix that keyed on
    0.25 (the insert's own guard) instead of threshold would fail here."""
    assert atb1._atb1_posted_flag(0.30, 0.70) is False


def test_returns_plain_bool_not_numpy():
    # psycopg2 wants a real bool for the posted column, not numpy.bool_
    out = atb1._atb1_posted_flag(0.80, 0.70)
    assert type(out) is bool
