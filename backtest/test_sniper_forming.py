"""Standalone (DB-free) guard for the SMC-sniper forming-candle contract.

Background (T-2026-CU-9050-036, R1 / hard rule 5): scan_market read
`ORDER BY open_time DESC LIMIT 150`, flipped to ASC and ran `argrelextrema`
over the FULL frame — including the forming candle. Its high/low still move,
so the pivot set changed within the running candle: the drives of a
Three-Drive and the level of a Breaker Block repainted after the signal had
gone out. The first fix sliced the forming row off inside scan_market
(`c_highs, c_lows = highs[:-1], lows[:-1]`).

T-2026-CU-9050-111 (commit 80d0e09, 2026-07-13) then moved the bot onto
`core.candles.read_candles_with_indicators(..., include_forming=False)`. The
protection moved one layer down with it: the frame no longer CONTAINS the
forming bar, so the in-bot slice was removed and the newest closed candle is
`len(df) - 1` again. The guards below were not carried along and kept asserting
the removed slice for three weeks (T-2026-KYT-9050-083) — they are now written
against the contract that actually holds:

  * the frame is closed-only AT THE SOURCE (asserted on the real call, not on
    the source text — this is the load-bearing one),
  * pivot indices therefore address the full frame arrays,
  * `len(df) - 1` is the newest closed bar everywhere (pivot edge filter,
    breakout window, BB feature anchor),
  * and re-introducing a `[:-1]` slice would now silently drop the newest
    CLOSED candle instead of the forming one.

Run: py -3.13 backtest/test_sniper_forming.py   (or: pytest -q)
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import unittest.mock as mock

import numpy as np
import pandas as pd
import scipy.signal  # noqa: F401  (pre-seed C-ext before any sys.modules patch)
import scipy.stats  # noqa: F401  (pre-seed C-ext before any sys.modules patch)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(REPO_ROOT, "25_smc_ml_sniper.py"), encoding="utf-8").read()

PIVOT_WINDOW = 10


def _load_sniper():
    """Import 25_smc_ml_sniper.py without DB or artifacts.

    The bot loads its XGB artifacts at import and calls exit(1) on failure, so
    joblib.load is mocked to a valid artifact dict. pandas / numpy / scipy are
    imported above before the sys.modules patch (memory
    patch-dict-sys-modules-numpy-teardown). Same loader as
    test_sniper_edge_pivots.py / test_sniper_retest_level.py.
    """
    spec = importlib.util.spec_from_file_location(
        "smc_ml_sniper", os.path.join(REPO_ROOT, "25_smc_ml_sniper.py")
    )
    mod = importlib.util.module_from_spec(spec)
    fake_artifact = {
        "model": mock.MagicMock(),
        "features": ["dir_num"],
        "optimal_threshold": None,
        "calibrator_isotonic": None,
        "meta": {},
    }
    fake_joblib = mock.MagicMock()
    fake_joblib.load.return_value = fake_artifact
    with mock.patch.dict(
        "sys.modules",
        {
            "joblib": fake_joblib,
            "core.database": mock.MagicMock(),
            "core.config": mock.MagicMock(CH_SNIPER_BB=-1, CH_SNIPER_TD=-2),
            "core.market_utils": mock.MagicMock(COOLDOWN_MODULE_MAX_LEN=10),
            "core.trade_utils": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


sniper = _load_sniper()


def _scan_body():
    body = re.search(r"def scan_market\(\):\n(.*?)\ndef ", SRC, re.DOTALL)
    assert body, "scan_market body not found"
    return body.group(1)


# ---------------------------------------------------------------------------
# The load-bearing guard: the frame is closed-only at the SOURCE
# ---------------------------------------------------------------------------
def test_scan_market_reads_closed_candles_only():
    """Runs the real scan_market against a recording reader.

    This replaces the old text guard on the `[:-1]` slice. The slice was the old
    mechanism; the frame being closed-only is the actual contract, and it is only
    observable at the call. A frame shorter than the 100-candle floor makes
    scan_market skip each symbol right after the read, so the loop stays cheap
    and never reaches scoring or posting.
    """
    calls = []

    def recording_reader(conn, symbol, tf, **kwargs):
        calls.append({"symbol": symbol, "tf": tf, **kwargs})
        return pd.DataFrame({"close": np.arange(10.0)})  # < 100 rows → `continue`

    with mock.patch.object(sniper, "read_candles_with_indicators", recording_reader), \
         mock.patch.object(sniper, "load_coins", lambda: ["BTCUSDT"]), \
         mock.patch.object(sniper, "get_db_connection", mock.MagicMock()), \
         mock.patch.object(sniper, "get_live_prices_batch", lambda: {"BTCUSDT": 100.0}):
        sniper.scan_market()

    assert calls, "scan_market performed no candle read at all — the guard would pass vacuously"
    assert len(calls) == len(sniper.TIMEFRAMES), (
        f"expected one read per timeframe, got {len(calls)} for {sniper.TIMEFRAMES}"
    )
    for call in calls:
        assert call.get("include_forming") is False, (
            f"candle read for {call['symbol']}/{call['tf']} passed "
            f"include_forming={call.get('include_forming')!r} — the forming candle would "
            "re-enter the frame and pivots would repaint (hard rule 5)"
        )


def test_no_raw_candle_query_bypasses_core_candles():
    """A hand-rolled SELECT would sidestep the include_forming contract above."""
    body = _scan_body()
    assert "read_candles_with_indicators(" in body, (
        "scan_market no longer reads through core.candles — the closed-only contract "
        "is enforced there and nowhere else"
    )
    assert not re.search(r"SELECT\s.*\sFROM\s", body, re.IGNORECASE), (
        "a raw candle SELECT reappeared in scan_market — it would bypass "
        "include_forming=False (docs/CANDLE_CALL_SITES.md)"
    )


# ---------------------------------------------------------------------------
# Why the contract matters: the mechanism it defends against
# ---------------------------------------------------------------------------
def test_forming_candle_repaints_the_raw_pivot_set():
    """A moving last row flips the pivot set, a closed-only frame does not.

    Unchanged since T-2026-CU-9050-036: this is why `include_forming=False` is
    load-bearing rather than cosmetic.
    """
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


# ---------------------------------------------------------------------------
# The consequences of an all-closed frame: no slice, and len(df) - 1 anchors
# ---------------------------------------------------------------------------
def test_pivots_are_built_on_the_full_closed_frame():
    """No slice between the frame and argrelextrema.

    With `include_forming=False` the frame is already closed-only, so a
    re-introduced trailing slice would drop the newest CLOSED candle — the exact
    inversion of the P1.46 bug it once fixed. The arrays handed to argrelextrema
    must be the frame arrays themselves.
    """
    body = _scan_body()
    assert re.search(r"c_highs,\s*c_lows\s*=\s*highs,\s*lows\b", body), (
        "the closed-candle arrays are no longer the full frame arrays — with an "
        "all-closed frame a slice would discard the newest closed candle"
    )
    assert not re.search(r"(highs|lows)\[:-1\]", body), (
        "a trailing slice reappeared — the frame is closed-only since "
        "T-2026-CU-9050-111, so this now drops the newest CLOSED candle"
    )
    calls = re.findall(r"scipy\.signal\.argrelextrema\(\s*(\w+)", body)
    assert calls, "no argrelextrema call found in scan_market"
    assert set(calls) == {"c_highs", "c_lows"}, (
        f"argrelextrema runs on {sorted(set(calls))} — it must only see the closed-candle arrays"
    )


def test_newest_closed_bar_is_the_last_row():
    """`len(df) - 1` is the newest closed candle in an all-closed frame.

    Three consumers share that anchor — the pivot edge filter, the breakout
    window bound `n_closed`, and the BB feature row. A `len(df) - 2` anywhere
    would be the pre-T-111 offset and would silently address the wrong candle.
    """
    body = _scan_body()
    assert re.search(r"last_closed\s*=\s*len\(df\)\s*-\s*1\b", body), (
        "last_closed anchor lost — with an all-closed frame the newest closed bar is len(df)-1"
    )
    assert re.search(r"n_closed\s*=\s*len\(df\)\s*$", body, re.MULTILINE), (
        "n_closed must be len(df): the frame is all-closed, so the breakout search "
        "covers every closed candle"
    )
    assert not re.search(r"len\(df\)\s*-\s*2", body), (
        "a len(df)-2 offset reappeared — that was the anchor while the frame still "
        "carried the forming bar; it now points one candle too far back"
    )


def test_pivot_indices_address_the_full_frame_arrays():
    """`highs[p1]` / `rsis[p1]` read off the same arrays the pivots came from.

    Sound only while no slice sits in between (see the test above). A frame-level
    `df = df.iloc[:-1]` would shift every len(df)-1 offset by one candle and is
    equally forbidden.
    """
    rng = np.random.default_rng(20500036)
    highs = rng.normal(size=300).cumsum() + 100.0
    pivots = scipy.signal.argrelextrema(highs, np.greater, order=PIVOT_WINDOW)[0]
    assert len(pivots) >= 3, "fixture produced too few pivots to be meaningful"
    # A back-anchored slice would make every pivot index name the wrong candle.
    assert all(highs[p] != highs[1:][p] for p in pivots if p < len(highs) - 1), (
        "fixture degenerated — a back-anchored slice must misread every pivot here"
    )

    body = _scan_body()
    assert not re.search(r"argrelextrema\(\s*\w*highs\[1:\]", body), (
        "a back-anchored slice would make highs[p]/rsis[p] read the wrong candle"
    )
    for expr in ("highs[p1]", "lows[p1]", "rsis[p1]"):
        assert expr in body, f"{expr} vanished — check that pivot indices still address the frame arrays"
    assert not re.search(r"df\s*=\s*df\.iloc\[:-1\]", body), (
        "dropping the row from df would shift the len(df)-1 offsets "
        "(BB feature row, breakout window) by one candle"
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("OK — sniper forming-candle contract holds")
