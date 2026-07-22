"""Stoic 1-2-3 direction module (T-2026-KYT-9050-024).

Deterministic, lookahead-free translation of the discretionary "Stoic Edge
System / 1-2-3 Sequence" into a signal generator whose ``date,signal`` output
plugs into the GARCH validation harness (`tools/research/garch/compare.py`).

Flat modules (each runnable as a CLI) re-exported here so callers can
``from tools.research.stoic123 import generate_signals, StoicParams``. ``ccxt``
is imported lazily (only the backtest CLI needs it). See SPEC.md / README.md.
"""

import os
import sys

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from params import StoicParams  # noqa: E402
from rules import (  # noqa: E402
    compute_indicators,
    detect_base,
    htf_location_series,
    meaningful_break,
    moving_average,
    wilder_atr,
)
from signals import signals_dataframe, write_signals_csv  # noqa: E402
from state_machine import generate_signals  # noqa: E402

__all__ = [
    "StoicParams",
    "generate_signals",
    "signals_dataframe",
    "write_signals_csv",
    "compute_indicators",
    "moving_average",
    "wilder_atr",
    "meaningful_break",
    "detect_base",
    "htf_location_series",
]
