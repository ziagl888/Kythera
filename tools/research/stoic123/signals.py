"""signals.py — turn the state machine's positions into a compare.py signals.csv.

The whole point of the direct-anschluss (AK4): emit a ``date,signal`` CSV with
values in {-1,0,1} that `tools/research/garch/compare.py --signals` consumes
unchanged, so one run measures the 1-2-3 edge AND whether GARCH sizing improves
it.
"""

from __future__ import annotations

import pandas as pd
from params import StoicParams
from state_machine import generate_signals


def signals_dataframe(df: pd.DataFrame, htf: pd.DataFrame, p: StoicParams | None = None) -> pd.DataFrame:
    """Return a ``date, signal`` frame (position per bar) for ``df``."""
    pos = generate_signals(df, htf, p)
    out = pd.DataFrame({"date": pd.to_datetime(df["date"].values), "signal": pos.to_numpy()})
    return out


def write_signals_csv(df: pd.DataFrame, htf: pd.DataFrame, path: str, p: StoicParams | None = None) -> str:
    """Write the signals.csv and return the path. Format matches
    ``compare.load_signals`` (date,signal, values in {-1,0,1})."""
    signals_dataframe(df, htf, p).to_csv(path, index=False)
    return path
