"""DB-freie Tests für den FMR2-Normalisierungs-Exit (K4).

Load-bearing Logik: (1) das Exit-Predikat fmr2_funding_normalized (SHORT exits
sobald funding_cs_pctl<0.80 ODER funding_z_30d<1.0; LONG symmetrisch) und
(2) der Settlement-Walk simulate_normalization_exit (Time-Stop nach 9
Settlements, Normalisierungs-Exit am Settlement-Kerzen-Close, harter
Katastrophen-SL als First-Touch-Netz, open_at_end).

Kein DB-Zugriff — läuft standalone (siehe __main__).
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.research_features import (  # noqa: E402
    FMR2_CATASTROPHE_SL_PCT,
    FMR2_TIME_STOP_SETTLEMENTS,
    fmr2_catastrophe_sl,
    fmr2_funding_normalized,
)
from tools.fmr1_build_dataset import simulate_normalization_exit  # noqa: E402


# --------------------------------------------------------------------------- #
# Exit-Predikat                                                                #
# --------------------------------------------------------------------------- #
def test_short_exit_predicate():
    # SHORT im Extrem (cs hoch, z hoch) → NICHT normalisiert.
    assert fmr2_funding_normalized("SHORT", 0.99, 3.0) is False
    # cs zurück unter 0.80 → normalisiert (OR-Zweig 1).
    assert fmr2_funding_normalized("SHORT", 0.79, 3.0) is True
    # z zurück unter 1.0 → normalisiert (OR-Zweig 2), auch wenn cs noch extrem.
    assert fmr2_funding_normalized("SHORT", 0.99, 0.9) is True
    # exakt an den Schwellen (strikt <): 0.80 / 1.0 sind NOCH extrem.
    assert fmr2_funding_normalized("SHORT", 0.80, 1.0) is False


def test_long_exit_predicate_symmetric():
    # LONG im Extrem (cs niedrig, z niedrig) → NICHT normalisiert.
    assert fmr2_funding_normalized("LONG", 0.01, -3.0) is False
    # cs zurück über 0.20 → normalisiert.
    assert fmr2_funding_normalized("LONG", 0.21, -3.0) is True
    # z zurück über -1.0 → normalisiert.
    assert fmr2_funding_normalized("LONG", 0.01, -0.9) is True
    assert fmr2_funding_normalized("LONG", 0.20, -1.0) is False


def test_predicate_nan_is_fail_safe():
    # NaN in einer Größe → beide Vergleiche False → NICHT normalisiert (weiter halten).
    assert fmr2_funding_normalized("SHORT", float("nan"), float("nan")) is False
    assert fmr2_funding_normalized("LONG", float("nan"), float("nan")) is False
    # cs NaN, aber z trippt trotzdem → normalisiert (OR).
    assert fmr2_funding_normalized("SHORT", float("nan"), 0.5) is True


def test_catastrophe_sl_prices():
    frac = FMR2_CATASTROPHE_SL_PCT / 100.0
    assert math.isclose(fmr2_catastrophe_sl("LONG", 100.0), 100.0 * (1 - frac))
    assert math.isclose(fmr2_catastrophe_sl("SHORT", 100.0), 100.0 * (1 + frac))


# --------------------------------------------------------------------------- #
# Settlement-Walk-Fixture                                                      #
# --------------------------------------------------------------------------- #
def make_walk(n_settle=20, ev_pos=5, cs=None, rate=0.0001, price=100.0):
    """Stündliche Kerzen (flach bei ``price``) + 8h-Settlement-Historie.

    entry_idx = 8*ev_pos → Entry-Kerze 1h VOR dem Event-Settlement; der Walk
    startet auf der Event-Settlement-Kerze, das erste Exit-fähige Settlement
    liegt 8h (8 Kerzen) später. Rückgabe: alle Argumente für simulate_normalization_exit.
    """
    t0 = pd.Timestamp("2026-03-01 00:00")
    f_ts = np.array([(t0 + pd.Timedelta(hours=8 * k)).to_datetime64() for k in range(n_settle)])
    f_rates = np.full(n_settle, rate, dtype=np.float64)
    if cs is None:
        cs = np.full(n_settle, 0.99, dtype=np.float64)
    else:
        cs = np.asarray(cs, dtype=np.float64)

    start = t0 - pd.Timedelta(hours=1)
    end = t0 + pd.Timedelta(hours=8 * (n_settle - 1) + 8)
    n_h = int((end - start) / pd.Timedelta(hours=1)) + 1
    times = np.array([(start + pd.Timedelta(hours=i)).to_datetime64() for i in range(n_h)])
    highs = np.full(n_h, price, dtype=np.float64)
    lows = np.full(n_h, price, dtype=np.float64)
    closes = np.full(n_h, price, dtype=np.float64)
    entry_idx = 8 * ev_pos
    return dict(
        times=times,
        highs=highs,
        lows=lows,
        closes=closes,
        entry_idx=entry_idx,
        f_ts=f_ts,
        f_rates=f_rates,
        cs_pctl=cs,
        ev_pos=ev_pos,
    )


def test_walk_time_stop_at_9_settlements():
    """cs bleibt extrem (0.99), z=NaN (konstante Raten) → nie normalisiert →
    Zwangsschluss nach exakt FMR2_TIME_STOP_SETTLEMENTS Settlements."""
    fx = make_walk()
    res = simulate_normalization_exit("SHORT", 100.0, **fx)
    assert res["exit_reason"] == "time_stop"
    assert res["settlements"] == FMR2_TIME_STOP_SETTLEMENTS
    assert res["net_pnl_pct"] is not None


def test_walk_normalized_exit():
    """cs fällt am 3. Halte-Settlement (ev_pos+3) zurück unter 0.80 → Exit dort."""
    cs = np.full(20, 0.99)
    cs[5 + 3] = 0.5  # ev_pos=5 default → 3. forward settlement
    fx = make_walk(cs=cs)
    res = simulate_normalization_exit("SHORT", 100.0, **fx)
    assert res["exit_reason"] == "normalized"
    assert res["settlements"] == 3


def test_walk_catastrophe_sl_first_touch():
    """SHORT, Preis-Spike >15% in der ersten Halte-Kerze → touch-basierter
    Katastrophen-SL schlägt vor jedem Settlement zu."""
    fx = make_walk()
    sl = fmr2_catastrophe_sl("SHORT", 100.0)  # 115.0
    fx["highs"][fx["entry_idx"] + 1] = sl + 5.0  # Spike über den SL
    res = simulate_normalization_exit("SHORT", 100.0, **fx)
    assert res["exit_reason"] == "catastrophe_sl"
    assert res["settlements"] == 0
    # PnL = −15% (SHORT gegen Spike) minus Fees.
    assert res["net_pnl_pct"] < -FMR2_CATASTROPHE_SL_PCT + 0.5


def test_walk_open_at_end():
    """Kein forward Settlement in Reichweite (ev_pos = letztes) → Trade läuft bis
    Datenende offen → label-tragendes None."""
    fx = make_walk(n_settle=7, ev_pos=6)
    res = simulate_normalization_exit("SHORT", 100.0, **fx)
    assert res["exit_reason"] == "open_at_end"
    assert res["net_pnl_pct"] is None


def test_walk_normalized_prices_at_settlement_close():
    """Normalisierungs-Exit preist am Close der Settlement-Kerze (nicht TP/SL).
    Bei flachem Preis == entry → PnL = reine Fees (negativ, klein)."""
    cs = np.full(20, 0.99)
    cs[5 + 2] = 0.1
    fx = make_walk(cs=cs)
    res = simulate_normalization_exit("SHORT", 100.0, **fx)
    assert res["exit_reason"] == "normalized" and res["settlements"] == 2
    assert -0.2 < res["net_pnl_pct"] < 0.0  # nur Round-Trip-Fees


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK  {name}")
    print("\nAlle FMR2-Exit-Tests grün.")
