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
    FUNDING_FEATURES,
    clear_funding_cache,
    funding_features_asof,
    funding_features_cached,
    next_feature_change,
)

SYMBOL = "BTCUSDT"
T0 = datetime.datetime(2026, 7, 1, 0, 0, tzinfo=datetime.timezone.utc)
# funding_features_asof returns {} below MIN_HISTORY (21 settlements). All
# timestamps below sit deliberately PAST that — otherwise you'd be comparing empty
# dicts and every test would be green without ever touching a real value.
N = 40
STEP_H = 8
LAST = T0 + datetime.timedelta(hours=STEP_H * (N - 1))  # last settlement in the fixture
DUE = LAST + datetime.timedelta(hours=STEP_H)  # next due settlement


def _frame(times=None, n=N, step_hours=STEP_H):
    """Funding settlements with rising rates; ``times`` overrides the grid."""
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
    # Past the last settlement: boundary from the estimated interval.
    assert next_feature_change(g, LAST + datetime.timedelta(minutes=1)) == pd.Timestamp(DUE)


def test_next_feature_change_infers_the_interval_from_history():
    hourly = [T0 + datetime.timedelta(hours=i) for i in range(N)]
    g = _frame(times=hourly)[SYMBOL]
    after_last = T0 + datetime.timedelta(hours=N - 1, minutes=1)
    assert next_feature_change(g, after_last) == pd.Timestamp(T0 + datetime.timedelta(hours=N)), (
        "a 1h-funding pair must not inherit the 8h interval"
    )
    assert next_feature_change(_frame(times=[T0])[SYMBOL], T0) is None, "a single row cannot yield an interval"


def test_interval_estimate_never_overshoots_a_shortening_cadence():
    """The estimate must be CONSERVATIVE. Overestimating parks the cache across a real
    settlement and serves a stale value (parity break); underestimating only costs an
    extra load. A coin switching 8h→1h — or a gap skewing the recent diffs — is exactly
    where a median overshoots by hours. The minimum cannot.

    The rasters are deliberately non-uniform: with a uniform one, minimum, median and
    last-diff all coincide and the assertion would be vacuous."""
    # (a) Cadence flips 8h → 1h: a median of the last spacings would see +8h.
    times = [T0 + datetime.timedelta(hours=8 * i) for i in range(N)]
    times += [times[-1] + datetime.timedelta(hours=i) for i in (1, 2)]
    last = times[-1]
    due = next_feature_change(_frame(times=times)[SYMBOL], last + datetime.timedelta(minutes=1))
    assert due == pd.Timestamp(last + datetime.timedelta(hours=1)), (
        f"estimate {due} overshoots the true 1h cadence — a median of the last diffs would say +8h"
    )

    # (b) The LAST spacing is an ingestion gap (16h). Taking just the last diff
    #     overestimates just as the median would — the minimum does not.
    gapped = [*times, last + datetime.timedelta(hours=16)]
    gap_last = gapped[-1]
    due = next_feature_change(_frame(times=gapped)[SYMBOL], gap_last + datetime.timedelta(minutes=1))
    assert due == pd.Timestamp(gap_last + datetime.timedelta(hours=1)), (
        f"estimate {due} inherited the 16h gap as the interval — the last diff is not a safe estimator"
    )


def test_a_shortening_cadence_never_serves_a_stale_value():
    """The behavioural half of the test above, straight from the adversarial finding."""
    clear_funding_cache()
    times = [T0 + datetime.timedelta(hours=8 * i) for i in range(N)]
    times += [times[-1] + datetime.timedelta(hours=i) for i in (1, 2)]
    loader = _CountingLoader(_frame(times=times))
    last = times[-1]

    _cached(last + datetime.timedelta(minutes=1), loader)  # cached until last+1h

    # The next (1h) settlement lands. The cache MUST see it.
    loader.by_sym = _frame(times=[*times, last + datetime.timedelta(hours=1)])
    ts = last + datetime.timedelta(hours=1, minutes=5)
    served = _cached(ts, loader)
    assert served == funding_features_asof(loader.by_sym, SYMBOL, ts), (
        "the cache sat across a real settlement and served a stale value"
    )


def test_the_boundary_matches_the_asof_cut_at_an_exact_settlement_timestamp():
    """Both sides must cut with searchsorted 'left'. With 'right', a query landing
    exactly on a funding_time would reuse an entry whose value changes a nanosecond
    later."""
    by_sym = _frame()
    g = by_sym[SYMBOL]
    exact = LAST  # ts sits exactly on a settlement
    assert next_feature_change(g, exact) == pd.Timestamp(exact), (
        "the boundary must be the settlement itself when ts sits exactly on it"
    )
    # No-lookahead: the settlement AT ts does not enter (funding_time < ts, strict).
    assert funding_features_asof(by_sym, SYMBOL, exact) == funding_features_asof(
        by_sym, SYMBOL, exact - datetime.timedelta(seconds=1)
    ), "a settlement exactly at ts leaked into the features — that is lookahead"

    clear_funding_cache()
    loader = _CountingLoader(by_sym)
    assert _cached(exact, loader) == funding_features_asof(by_sym, SYMBOL, exact)
    after = _cached(exact + datetime.timedelta(seconds=1), loader)
    assert after == funding_features_asof(by_sym, SYMBOL, exact + datetime.timedelta(seconds=1))
    assert loader.calls == 2, "the entry cached exactly on a settlement was reused one second later"


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
    times.append(times[-1] + datetime.timedelta(hours=7, minutes=30))  # settlement at :30
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
    stale = _frame()  # last row = LAST, DUE is still missing
    loader = _CountingLoader(stale)

    _cached(LAST + datetime.timedelta(minutes=1), loader)  # 1st load, valid until DUE
    assert loader.calls == 1

    # Settlement is due, the row is still missing: EVERY call reloads.
    _cached(DUE + datetime.timedelta(seconds=30), loader)
    _cached(DUE + datetime.timedelta(minutes=5), loader)
    assert loader.calls == 3, "an overdue settlement was served from a frozen cache entry"

    # Row lands (late). From here on, caching is allowed again.
    loader.by_sym = _frame(times=[T0 + datetime.timedelta(hours=STEP_H * i) for i in range(N + 1)])
    fresh = _cached(DUE + datetime.timedelta(minutes=10), loader)
    assert loader.calls == 4
    assert fresh == funding_features_asof(loader.by_sym, SYMBOL, DUE + datetime.timedelta(minutes=10))
    _cached(DUE + datetime.timedelta(minutes=20), loader)
    assert loader.calls == 4, "the cache stopped working once the late row had landed"


def test_an_empty_result_below_min_history_is_not_cached():
    """funding_features_asof returns {} below MIN_HISTORY. Caching that would serve
    empty funding features until the estimated interval elapses — right when the coin
    crosses the threshold and becomes tradeable. The row that lifts it over can land
    sooner than the estimate, so the empty result must never be cached."""
    clear_funding_cache()
    below = [T0 + datetime.timedelta(hours=8 * i) for i in range(20)]  # < MIN_HISTORY (21)
    loader = _CountingLoader(_frame(times=below))
    last = below[-1]

    assert funding_features_asof(loader.by_sym, SYMBOL, last + datetime.timedelta(minutes=1)) == {}
    _cached(last + datetime.timedelta(minutes=1), loader)  # must NOT be cached

    # The 21st settlement lands after 1h (earlier than the 8h interval). Query at last+2h.
    loader.by_sym = _frame(times=[*below, last + datetime.timedelta(hours=1)])
    ts = last + datetime.timedelta(hours=2)
    served = _cached(ts, loader)
    assert served == funding_features_asof(loader.by_sym, SYMBOL, ts), (
        "an empty pre-threshold result was cached and hid the real funding features"
    )
    assert served, "the coin is over MIN_HISTORY now — features must be non-empty"


def test_cached_values_equal_the_asof_values_field_by_field():
    """The load-bearing invariant of this whole module is Train == Serve (rule 7): the
    served funding numbers must be the ones the trainer/replay compute. Pin the actual
    VALUES, not just the load count — otherwise a regression in a feature definition
    (e.g. the fund_24h window) rides through green."""
    clear_funding_cache()
    loader = _CountingLoader(_frame())
    ts = LAST + datetime.timedelta(minutes=1)
    served = _cached(ts, loader)
    truth = funding_features_asof(loader.by_sym, SYMBOL, ts)
    assert set(served) == set(FUNDING_FEATURES), f"served keys drifted from the contract: {set(served)}"
    for k in FUNDING_FEATURES:
        assert served[k] == truth[k], f"{k}: cache {served[k]} != as-of {truth[k]}"

    # Hand-computed expected values, so the FEATURE DEFINITION itself is also pinned —
    # served==truth alone would not catch a window regression, since both sides use
    # the same (broken) definition. Fixture: 40 settlements, bps = 1.0..4.9 (+0.1/settlement).
    expected = {
        "fund_last": 4.9,  # rates[-1]
        "fund_24h": 4.8,  # mean(4.7, 4.8, 4.9) — the 3-settlement window
        "fund_72h": 4.5,  # mean(4.1 … 4.9) — 9 settlements
        "fund_7d_cum": 81.9,  # sum(2.9 … 4.9) — 21 settlements
        "fund_pctl_90d": 100.0,  # 4.9 is the maximum
        "fund_trend": 0.3,  # fund_24h − fund_72h
    }
    for k, want in expected.items():
        assert abs(served[k] - want) < 1e-6, f"{k}: {served[k]} != expected {want} — feature definition changed"


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
