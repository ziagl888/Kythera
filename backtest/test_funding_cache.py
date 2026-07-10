"""Standalone (DB-free) tests for the per-hour funding-feature cache.

T-2026-CU-9050-055. EPD serves the 6 funding features as model input, so the
load cannot move behind the trade decision — the features ARE what produces the
probability. What can go is the repetition: the 900s re-fire timer gates the ML
path but is only *set* on the live-trade branch, so a coin sitting in the shadow
band re-issues the query on every 10s tick.

The cache is only legitimate because of an invariant, and that invariant is what
these tests pin down:

  funding_features_asof depends on ts ONLY through the searchsorted cut, i.e.
  through how many settlements lie strictly before ts. Binance settles on whole
  hours. So within one started hour the result cannot change, and a cache keyed
  on the hour returns bit-identical values — it is not an approximation.

A naive time-TTL would be one: it can straddle a settlement and hand the model
stale funding, breaking train/serve parity. The tests below therefore check the
hour boundary explicitly, and the ingestion-lag escape (the hh:00 row lands a
few seconds late) that would otherwise freeze a stale value for a full hour.

Run: python backtest/test_funding_cache.py
"""

import datetime
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.funding_features import (  # noqa: E402
    CACHE_MIN_AGE_S,
    clear_funding_cache,
    funding_features_asof,
    funding_features_cached,
)

SYMBOL = "BTCUSDT"
T0 = datetime.datetime(2026, 7, 1, 0, 0, tzinfo=datetime.timezone.utc)
# funding_features_asof liefert {} unterhalb von MIN_HISTORY (21 Saetze). Alle
# Zeitpunkte unten liegen bewusst DAHINTER — sonst vergliche man leere Dicts und
# jeder Test waere gruen, ohne je einen Wert angefasst zu haben.
#
# Die gemessene Abrechnung liegt bewusst auf 08:00, NICHT auf 00:00: fiele sie
# auf eine Tagesgrenze, bestuende ein (falscher) Tages-Key den Rollover-Test
# genauso wie der Stunden-Key.
SETTLED = 31  # Index der Abrechnung, um die herum gemessen wird → hh=08:00
BOUNDARY = T0 + datetime.timedelta(hours=8 * SETTLED)
MID_HOUR = BOUNDARY + datetime.timedelta(hours=1)  # zwischen zwei Abrechnungen


def _frame(n_settlements=40, step_hours=8):
    """n Funding-Sätze im 8h-Raster ab T0, leicht steigende Rates."""
    times = [T0 + datetime.timedelta(hours=step_hours * i) for i in range(n_settlements)]
    rates = [(1.0 + 0.1 * i) / 1e4 for i in range(n_settlements)]
    return {SYMBOL: pd.DataFrame({"funding_time": pd.to_datetime(times, utc=True), "funding_rate": rates})}


class _CountingLoader:
    def __init__(self, by_sym):
        self.by_sym, self.calls = by_sym, 0

    def __call__(self, conn, symbols, since=None):
        self.calls += 1
        return self.by_sym


# ---------------------------------------------------- the invariant the cache rests on


def test_asof_is_constant_within_a_started_hour():
    """No settlement lands between hh:00 and hh:59, so the as-of cut is the same."""
    by_sym = _frame()
    hour = MID_HOUR
    base = funding_features_asof(by_sym, SYMBOL, hour)
    assert base, "fixture below MIN_HISTORY — the comparison would be between empty dicts"
    for minute in (0, 1, 17, 59):
        for second in (0, 30, 59):
            ts = hour + datetime.timedelta(minutes=minute, seconds=second)
            assert funding_features_asof(by_sym, SYMBOL, ts) == base, (
                f"as-of moved inside one hour at +{minute}m{second}s — the hour key would be unsound"
            )


def test_asof_changes_across_a_settlement_boundary():
    """The counter-check: if it never changed, the test above would be vacuous."""
    by_sym = _frame()
    before = funding_features_asof(by_sym, SYMBOL, BOUNDARY - datetime.timedelta(seconds=1))
    after = funding_features_asof(by_sym, SYMBOL, BOUNDARY + datetime.timedelta(seconds=1))
    assert before and after, "fixture below MIN_HISTORY"
    assert before != after, "a settlement must move the as-of features, otherwise the fixture is degenerate"


# ------------------------------------------------------------------- cache behaviour


def test_cache_returns_identical_values_and_loads_once_per_hour():
    clear_funding_cache()
    loader = _CountingLoader(_frame())
    hour = MID_HOUR
    first_ts = hour + datetime.timedelta(seconds=CACHE_MIN_AGE_S)  # nach dem Ingestion-Lag-Fenster

    expected = funding_features_asof(loader.by_sym, SYMBOL, first_ts)
    assert expected, "fixture below MIN_HISTORY — the cache test would compare empty dicts"
    got = [
        funding_features_cached(None, SYMBOL, first_ts + datetime.timedelta(seconds=s), loader)
        for s in (0, 10, 900, 3599 - CACHE_MIN_AGE_S)
    ]

    assert loader.calls == 1, f"cache issued {loader.calls} loads inside one hour"
    for feats in got:
        assert feats == expected, "cached funding features drifted from the uncached computation"


def test_cache_reloads_on_the_next_hour():
    clear_funding_cache()
    loader = _CountingLoader(_frame())
    h1 = BOUNDARY - datetime.timedelta(hours=1) + datetime.timedelta(seconds=CACHE_MIN_AGE_S)
    h2 = BOUNDARY + datetime.timedelta(seconds=CACHE_MIN_AGE_S)  # neue Stunde MIT Abrechnung

    a = funding_features_cached(None, SYMBOL, h1, loader)
    b = funding_features_cached(None, SYMBOL, h2, loader)
    assert a and b, "fixture below MIN_HISTORY"
    assert loader.calls == 2, "the cache did not reload on the hour rollover"
    assert a != b, "the settlement between the two hours must be visible"


def test_cache_does_not_freeze_a_value_built_during_the_ingestion_lag():
    """The hh:00 row lands a few seconds after hh:00. An entry built at hh:00:05
    may have missed it; caching it for the rest of the hour would serve the model
    a stale funding rate right after every settlement."""
    clear_funding_cache()
    loader = _CountingLoader(_frame())
    hour = BOUNDARY

    funding_features_cached(None, SYMBOL, hour + datetime.timedelta(seconds=5), loader)
    funding_features_cached(None, SYMBOL, hour + datetime.timedelta(seconds=30), loader)
    assert loader.calls == 2, "an entry built inside the ingestion-lag window was reused"

    # Ab CACHE_MIN_AGE_S greift der Cache wieder.
    funding_features_cached(None, SYMBOL, hour + datetime.timedelta(seconds=CACHE_MIN_AGE_S), loader)
    funding_features_cached(None, SYMBOL, hour + datetime.timedelta(seconds=CACHE_MIN_AGE_S + 10), loader)
    assert loader.calls == 3, "the cache stopped working after the lag window"


def test_cache_is_per_symbol():
    clear_funding_cache()
    loader = _CountingLoader(_frame())
    ts = MID_HOUR + datetime.timedelta(seconds=CACHE_MIN_AGE_S)
    funding_features_cached(None, SYMBOL, ts, loader)
    funding_features_cached(None, "ETHUSDT", ts, loader)
    assert loader.calls == 2, "one symbol's entry served another symbol"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("OK — funding cache is value-neutral")
