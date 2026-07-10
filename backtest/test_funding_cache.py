"""Standalone (DB-free) tests for the settlement-bound funding-feature cache.

T-2026-CU-9050-055. EPD serves the 6 funding features as model input, so the load
cannot move behind the trade decision — the features ARE what produces the
probability. What can go is the repetition: the 900s re-fire timer gates the ML
path but is only *set* on the live-trade branch, so a coin sitting in the shadow
band re-issues the query on every 10s tick.

The cache is only legitimate because of an invariant:

  funding_features_asof depends on ts ONLY through the searchsorted cut, i.e.
  through how many settlements lie before ts. While no new settlement has landed,
  the result is constant.

The first version of this cache keyed on the started HOUR, on the assumption that
Binance settles on whole hours. An adversarial review broke that with two
executed counterexamples, and both are pinned below:

  * an off-hour settlement (12:30) — nothing enforces hour alignment,
    tools/backfill_funding_rates.py stores funding_time at millisecond precision;
  * ingestion lag beyond the guard window — the clock-keyed cache froze a value
    that had missed the just-settled rate for the rest of the hour.

So the key comes from the DATA: an entry is valid until the next settlement that
could change the result. That boundary is the next funding_time already present in
the history (whatever wall-clock minute it sits on), or — past the last row — the
last funding_time plus the interval inferred from history. If the due row has not
landed yet, the entry is already expired and the cache reloads until it appears:
it self-corrects instead of betting on an ingestion SLA.

Run: python backtest/test_funding_cache.py
"""

import datetime
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.funding_features import (  # noqa: E402
    clear_funding_cache,
    funding_features_asof,
    funding_features_cached,
    next_feature_change,
)

SYMBOL = "BTCUSDT"
T0 = datetime.datetime(2026, 7, 1, 0, 0, tzinfo=datetime.timezone.utc)
# funding_features_asof liefert {} unterhalb von MIN_HISTORY (21 Saetze). Alle
# Zeitpunkte unten liegen bewusst DAHINTER — sonst vergliche man leere Dicts und
# jeder Test waere gruen, ohne je einen Wert angefasst zu haben.
N = 40
STEP_H = 8
LAST = T0 + datetime.timedelta(hours=STEP_H * (N - 1))  # letzte Abrechnung im Fixture
DUE = LAST + datetime.timedelta(hours=STEP_H)  # naechste faellige


def _frame(times=None, n=N, step_hours=STEP_H):
    """Funding-Sätze mit steigenden Rates; ``times`` überschreibt das Raster."""
    if times is None:
        times = [T0 + datetime.timedelta(hours=step_hours * i) for i in range(n)]
    rates = [(1.0 + 0.1 * i) / 1e4 for i in range(len(times))]
    return {SYMBOL: pd.DataFrame({"funding_time": pd.to_datetime(times, utc=True), "funding_rate": rates})}


class _CountingLoader:
    def __init__(self, by_sym):
        self.by_sym, self.calls = by_sym, 0

    def __call__(self, conn, symbols, since=None):
        self.calls += 1
        return self.by_sym


def _cached(ts, loader):
    return funding_features_cached(None, SYMBOL, ts, loader)


# ---------------------------------------------------- the invariant the cache rests on


def test_asof_is_constant_between_settlements():
    by_sym = _frame()
    base = funding_features_asof(by_sym, SYMBOL, LAST + datetime.timedelta(minutes=1))
    assert base, "fixture below MIN_HISTORY — the comparison would be between empty dicts"
    for offset_h in (0.02, 1, 3, 7.9):
        ts = LAST + datetime.timedelta(hours=offset_h)
        assert funding_features_asof(by_sym, SYMBOL, ts) == base, (
            f"as-of moved at +{offset_h}h without a settlement — the cache would be unsound"
        )


def test_asof_changes_across_a_settlement():
    """Counter-check: if it never changed, the test above would be vacuous."""
    by_sym = _frame()
    before = funding_features_asof(by_sym, SYMBOL, LAST - datetime.timedelta(seconds=1))
    after = funding_features_asof(by_sym, SYMBOL, LAST + datetime.timedelta(seconds=1))
    assert before and after, "fixture below MIN_HISTORY"
    assert before != after, "a settlement must move the as-of features, otherwise the fixture is degenerate"


def test_next_feature_change_is_the_next_settlement_in_the_data():
    """A settlement that already sits in the history IS the boundary — even if it is
    still in the future of ts, and even if it does not fall on a whole hour."""
    g = _frame()[SYMBOL]
    assert next_feature_change(g, LAST - datetime.timedelta(minutes=1)) == pd.Timestamp(LAST)
    # Hinter dem letzten Satz: Grenze aus dem geschaetzten Intervall.
    assert next_feature_change(g, LAST + datetime.timedelta(minutes=1)) == pd.Timestamp(DUE)


def test_next_feature_change_infers_the_interval_from_history():
    hourly = [T0 + datetime.timedelta(hours=i) for i in range(N)]
    g = _frame(times=hourly)[SYMBOL]
    after_last = T0 + datetime.timedelta(hours=N - 1, minutes=1)
    assert next_feature_change(g, after_last) == pd.Timestamp(T0 + datetime.timedelta(hours=N)), (
        "a 1h-funding pair must not inherit the 8h interval"
    )
    assert next_feature_change(_frame(times=[T0])[SYMBOL], T0) is None, "a single row cannot yield an interval"


# ------------------------------------------------------------------- cache behaviour


def test_cache_returns_identical_values_and_loads_once_between_settlements():
    clear_funding_cache()
    loader = _CountingLoader(_frame())
    first = LAST + datetime.timedelta(minutes=1)

    expected = funding_features_asof(loader.by_sym, SYMBOL, first)
    assert expected, "fixture below MIN_HISTORY — the cache test would compare empty dicts"
    got = [_cached(first + datetime.timedelta(minutes=m), loader) for m in (0, 1, 60, 470)]

    assert loader.calls == 1, f"cache issued {loader.calls} loads between two settlements"
    for feats in got:
        assert feats == expected, "cached funding features drifted from the uncached computation"


def test_cache_expires_when_the_next_settlement_is_due():
    clear_funding_cache()
    loader = _CountingLoader(_frame())
    _cached(LAST + datetime.timedelta(minutes=1), loader)
    _cached(DUE + datetime.timedelta(seconds=1), loader)
    assert loader.calls == 2, "the cache did not expire at the next due settlement"


def test_an_off_hour_settlement_is_not_hidden_by_the_cache():
    """CX1 from the adversarial review: nothing enforces hour alignment. A clock-keyed
    cache served the pre-12:30 value until 13:00; the data-keyed one must not."""
    times = [T0 + datetime.timedelta(hours=8 * i) for i in range(N - 1)]
    times.append(times[-1] + datetime.timedelta(hours=7, minutes=30))  # Abrechnung um :30
    by_sym = _frame(times=times)
    loader = _CountingLoader(by_sym)
    off_hour = times[-1]

    clear_funding_cache()
    before = _cached(off_hour - datetime.timedelta(minutes=1), loader)
    after = _cached(off_hour + datetime.timedelta(minutes=1), loader)

    truth = funding_features_asof(by_sym, SYMBOL, off_hour + datetime.timedelta(minutes=1))
    assert after == truth, "the cache hid an off-hour settlement — the value is stale"
    assert before != after, "the off-hour settlement must be visible right after it lands"


def test_a_late_landing_row_is_not_frozen_for_a_whole_period():
    """CX2 from the adversarial review: the due row lands late. The entry is already
    expired, so the cache reloads until it appears — and only then caches again."""
    clear_funding_cache()
    stale = _frame()  # letzte Zeile = LAST, DUE fehlt noch
    loader = _CountingLoader(stale)

    _cached(LAST + datetime.timedelta(minutes=1), loader)  # 1. Load, gueltig bis DUE
    assert loader.calls == 1

    # Abrechnung ist faellig, die Zeile fehlt noch: JEDER Aufruf laedt neu.
    _cached(DUE + datetime.timedelta(seconds=30), loader)
    _cached(DUE + datetime.timedelta(minutes=5), loader)
    assert loader.calls == 3, "an overdue settlement was served from a frozen cache entry"

    # Zeile landet (spät). Ab jetzt darf wieder gecacht werden.
    loader.by_sym = _frame(times=[T0 + datetime.timedelta(hours=STEP_H * i) for i in range(N + 1)])
    fresh = _cached(DUE + datetime.timedelta(minutes=10), loader)
    assert loader.calls == 4
    assert fresh == funding_features_asof(loader.by_sym, SYMBOL, DUE + datetime.timedelta(minutes=10))
    _cached(DUE + datetime.timedelta(minutes=20), loader)
    assert loader.calls == 4, "the cache stopped working once the late row had landed"


def test_cache_is_per_symbol():
    clear_funding_cache()
    loader = _CountingLoader(_frame())
    ts = LAST + datetime.timedelta(minutes=1)
    _cached(ts, loader)
    funding_features_cached(None, "ETHUSDT", ts, loader)
    assert loader.calls == 2, "one symbol's entry served another symbol"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("OK — funding cache is settlement-bound and value-neutral")
