"""Signal-rate delta for T-2026-CU-9050-095: RSI span→Wilder migration.

Measures how the RSI-bound entry gates change when calculate_rsi moves from
`ewm(span=period)` (alpha=2/(period+1), the old code, ~Wilder-7.5) to true
Wilder `ewm(alpha=1/period)`. Replays ONLY the RSI portion of each gate over the
DB-free regression-guard fixtures (4 symbols x {30m,1h,2h,4h,1d,1w}, ~600 closed
candles each) so the migration effect is isolated: the non-RSI conditions
(tsi/ema/close) are identical on both sides and would multiply both counts by the
same factor, so the RSI-band pass rate is the honest measure of the gate delta.

The NEW rsi is the live engine's own calculate_rsi (imported by path); the OLD
rsi is the pre-migration span formula inline. Only closed candles are scored
(the fixtures are all closed); warmup rows where either rsi is NaN are dropped.

Gates measured (verbatim thresholds from strategies/ and core/rub_features.py):
  strat_5_percent  LONG : 55<=rsi_9<=75 AND 55<=rsi_14<=75
  strat_5_percent  SHORT: rsi_9<=45 AND rsi_14<=45
  fast_in_out      LONG : 55<=rsi_9<=75
  fast_in_out      SHORT: rsi_9<=45
  RUB2 (70/30)     OB   : rsi_14>70
  RUB2 (70/30)     OS   : rsi_14<30

Run from the repo root:  python tools/wilder_rsi_signal_delta.py
"""

from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FIXTURES = os.path.join(_ROOT, "tools", "regression_guard", "fixtures")

# The engine hard-requires these at import; the RSI path never opens a
# connection or sends a message.
for _k, _v in {
    "DB_PASSWORD": "offline",
    "TELEGRAM_BOT_TOKEN": "offline",
    "DB_HOST": "127.0.0.1",
    "DB_NAME": "offline",
    "DB_USER": "offline",
    "DB_PORT": "5432",
}.items():
    os.environ.setdefault(_k, _v)

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load_engine():
    spec = importlib.util.spec_from_file_location(
        "kythera_indicator_engine", os.path.join(_ROOT, "2_indicator_engine.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rsi_old_span(series, period):
    """Pre-migration RSI: ewm(span=period) == alpha=2/(period+1). Warmup NaN
    preserved (matches the T-054 NaN-flow contract; no fillna here so the delta
    is measured only on rows where BOTH variants are defined)."""
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    roll_up = up.ewm(span=period, adjust=False).mean()
    roll_down = down.ewm(span=period, adjust=False).mean()
    rs = roll_up / roll_down
    return 100.0 - (100.0 / (1.0 + rs))


def _gates(rsi9, rsi14):
    """Boolean masks per gate, aligned to the input arrays."""
    return {
        "strat_5_percent LONG  (55<=rsi9<=75 & 55<=rsi14<=75)": (
            (rsi9 >= 55) & (rsi9 <= 75) & (rsi14 >= 55) & (rsi14 <= 75)
        ),
        "strat_5_percent SHORT (rsi9<=45 & rsi14<=45)": (rsi9 <= 45) & (rsi14 <= 45),
        "fast_in_out     LONG  (55<=rsi9<=75)": (rsi9 >= 55) & (rsi9 <= 75),
        "fast_in_out     SHORT (rsi9<=45)": (rsi9 <= 45),
        "RUB2 overbought        (rsi14>70)": (rsi14 > 70),
        "RUB2 oversold          (rsi14<30)": (rsi14 < 30),
    }


def main() -> int:
    engine = _load_engine()
    fixtures = sorted(f for f in os.listdir(_FIXTURES) if f.endswith(".npz"))
    if not fixtures:
        print("no fixtures under tools/regression_guard/fixtures/")
        return 1

    from tools.regression_guard import rgcore  # noqa: E402 - after sys.path setup

    totals_old: dict[str, int] = {}
    totals_new: dict[str, int] = {}
    total_bars = 0

    for fname in fixtures:
        df = rgcore.load_frame(os.path.join(_FIXTURES, fname))
        close = pd.Series(np.asarray(df["close"], dtype=float))

        old9 = rsi_old_span(close, 9).to_numpy()
        old14 = rsi_old_span(close, 14).to_numpy()
        new9 = engine.calculate_rsi(close, 9).to_numpy()
        new14 = engine.calculate_rsi(close, 14).to_numpy()

        # Score only rows where every rsi used is defined on BOTH sides, so the
        # warmup asymmetry never leaks into the delta.
        valid = ~(np.isnan(old9) | np.isnan(old14) | np.isnan(new9) | np.isnan(new14))
        total_bars += int(valid.sum())

        for label, mask in _gates(old9, old14).items():
            totals_old[label] = totals_old.get(label, 0) + int((mask & valid).sum())
        for label, mask in _gates(new9, new14).items():
            totals_new[label] = totals_new.get(label, 0) + int((mask & valid).sum())

    print(
        f"RSI span->Wilder signal-rate delta  |  {len(fixtures)} fixtures, "
        f"{total_bars} scored (closed, both-defined) bars\n"
    )
    header = f"{'gate':<52} {'old':>7} {'new':>7} {'delta':>7} {'old%':>7} {'new%':>7} {'d_pp':>7}"
    print(header)
    print("-" * len(header))
    for label in _gates(np.array([]), np.array([])):
        o = totals_old[label]
        n = totals_new[label]
        op = 100.0 * o / total_bars if total_bars else 0.0
        npc = 100.0 * n / total_bars if total_bars else 0.0
        print(f"{label:<52} {o:>7} {n:>7} {n - o:>7} {op:>6.2f}% {npc:>6.2f}% {npc - op:>+6.2f}")
    print(
        "\nInterpretation: old span-RSI runs hotter/faster (~Wilder-7.5), so the "
        "LONG 55-75 bands and the 70/30 extremes fire MORE often than true Wilder. "
        "The migration lowers those signal rates by design; the 55/70/75 thresholds "
        "are NOT retuned here (that follows the TD2/BB2/QM2 retrain - P1.13 doctrine)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
