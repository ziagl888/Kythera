"""Standalone (DB-free) guard for the SMC-sniper forming-candle contract.

Background (T-2026-CU-9050-036, R1 / hard rule 5): scan_market reads
`ORDER BY open_time DESC LIMIT 150`, flips to ASC and used to run
`argrelextrema` over the FULL frame — including the forming candle. Its
high/low still move, so the pivot set changed within the running candle:
the drives of a Three-Drive and the level of a Breaker Block repainted
after the signal had gone out. 16/21/24 all drop the forming row; 25 did
not (docs/CANDLE_CALL_SITES.md §3).

Run: py -3.13 backtest/test_sniper_forming.py
"""

import re
from pathlib import Path

import numpy as np
import scipy.signal

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "25_smc_ml_sniper.py").read_text(encoding="utf-8")

PIVOT_WINDOW = 10


def _scan_body():
    body = re.search(r"def scan_market\(\):\n(.*?)\ndef ", SRC, re.DOTALL)
    assert body, "scan_market body not found"
    return body.group(1)


def test_pivots_built_on_closed_candles():
    body = _scan_body()
    assert re.search(r"c_highs,\s*c_lows\s*=\s*highs\[:-1\],\s*lows\[:-1\]", body), (
        "scan_market no longer derives closed-candle arrays — pivots would repaint on the forming candle"
    )


def test_argrelextrema_never_sees_the_forming_candle():
    body = _scan_body()
    calls = re.findall(r"scipy\.signal\.argrelextrema\(\s*(\w+)", body)
    assert calls, "no argrelextrema call found in scan_market"
    assert set(calls) == {"c_highs", "c_lows"}, (
        f"argrelextrema runs on {sorted(set(calls))} — it must only see the closed-candle arrays"
    )


def test_forming_candle_repaints_the_raw_pivot_set():
    """The mechanism itself: a moving last row flips the raw pivot set, but
    leaves the closed-candle pivot set untouched. Without this asymmetry the
    fix above would be cosmetic."""
    base = np.concatenate(
        [
            np.linspace(100.0, 110.0, 12),  # confirmed swing high at index 11
            np.linspace(110.0, 105.0, 12)[1:],
            np.linspace(105.0, 120.0, 12)[1:],  # last closed candle is an edge candidate
        ]
    )
    last_closed = float(base[-1])
    raw_sets, closed_sets = set(), set()
    for forming_high in (last_closed - 5.0, last_closed + 5.0):  # the running candle ticks through it
        highs = np.append(base, forming_high)
        raw_sets.add(tuple(scipy.signal.argrelextrema(highs, np.greater, order=PIVOT_WINDOW)[0]))
        closed_sets.add(tuple(scipy.signal.argrelextrema(highs[:-1], np.greater, order=PIVOT_WINDOW)[0]))
    assert len(raw_sets) == 2, "fixture no longer reproduces the repaint — the raw pivot set must move"
    assert len(closed_sets) == 1, "the closed-candle pivot set must be independent of the forming candle"
    assert closed_sets.pop(), "fixture degenerated to an empty pivot set — it would pass vacuously"


def test_slice_is_front_anchored_so_pivot_indices_address_the_full_arrays():
    """`highs[p]` / `rsis[p]` are read off the FULL arrays while `p` comes from the
    shortened one. Only a FRONT-anchored slice keeps that sound: `highs[:-1]` does,
    `highs[1:]` would silently read the wrong candle."""
    rng = np.random.default_rng(20500036)
    highs = rng.normal(size=300).cumsum() + 100.0
    pivots = scipy.signal.argrelextrema(highs[:-1], np.greater, order=PIVOT_WINDOW)[0]
    assert len(pivots) >= 3, "fixture produced too few pivots to be meaningful"
    # The back-anchored variant that must never be used: same length, same call,
    # but every pivot index would then name a candle one step to the left.
    assert all(highs[p] != highs[1:][p] for p in pivots), (
        "fixture degenerated — a back-anchored slice must misread every pivot here"
    )

    body = _scan_body()
    assert not re.search(r"argrelextrema\(\s*\w*highs\[1:\]", body), (
        "a back-anchored slice would make highs[p]/rsis[p] read the wrong candle"
    )
    for expr in ("highs[p1]", "lows[p1]", "rsis[p1]"):
        assert expr in body, f"{expr} vanished — check that pivot indices still address the full arrays"
    assert not re.search(r"df\s*=\s*df\.iloc\[:-1\]", body), (
        "dropping the row from df would shift the len(df)-1 / len(df)-2 offsets "
        "(BB feature row, breakout window) by one candle"
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("OK — sniper forming-candle contract holds")
