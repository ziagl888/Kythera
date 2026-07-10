# backtest/test_pump_dump_time_windows.py
"""
Unit tests for the time-based feature windows of the pump/dump detector (P1.39).

The detector used to slice its windows by list index: `prices[-7:]` meant "the
last 60 seconds" only if every 10s bucket arrived. On a WebSocket gap — most
likely exactly during a spike, when the socket is busiest — "-7" silently
spanned minutes, and the model was asked to score a stretched window.

Worse, `volumes_10s` was FILTERED on v10s_valid while `prices` was not, so
`volumes_10s[-18:]` and `prices[-18:]` referred to different instants as soon as
a single bucket was invalid.

Everything now routes through _find_bucket_before / _find_bucket_range, which
select by timestamp. These tests pin the gap behaviour, which is the only place
the old and new code disagree.

Run with: pytest backtest/test_pump_dump_time_windows.py -v
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import sys
import unittest.mock as mock
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

from core.signal_post import log_prediction  # noqa: F401  (import order: see sibling test)

UTC = datetime.timezone.utc


def _load_detector():
    spec = importlib.util.spec_from_file_location(
        "pump_dump_detector",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "10_pump_dump_detector.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {
            "core.database": mock.MagicMock(),
            "core.charting": mock.MagicMock(),
            "core.market_utils": mock.MagicMock(),
            "core.trade_utils": mock.MagicMock(),
            "core.ticker_10s": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


det = _load_detector()

SYMBOL = "TESTUSDT"

# Feature order in 10_pump_dump_detector.process_coin_logics
F_VOL_RATIO, F_P_CHG_60S, F_BUY_PRES, F_VOLAT = 0, 1, 2, 3


class _Recorder:
    """Captures the feature vector, then lands in the shadow band."""

    classes_ = [0, 1, 2]

    def __init__(self) -> None:
        self.features: list[list[float]] = []

    def predict_proba(self, x):
        self.features.append(list(x[0]))
        return [[0.10, 0.50, 0.40]]


class FakeConn:
    def cursor(self, *a, **kw):
        return mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)

    def commit(self):
        pass

    def rollback(self):
        pass


def _parse(ts_str: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def _bucket(ts: datetime.datetime, price: float, vol: float, valid: bool = True) -> dict:
    return {"t": ts.isoformat(), "p": str(price), "v10s": str(vol), "v10s_valid": valid}


@pytest.fixture
def run_tick(monkeypatch):
    """Drive process_coin_logics over a supplied bucket list; return the model."""

    def _run(buckets: list[dict], sample_vol: float = 1.0, now_offset: float = 0.0) -> _Recorder:
        """`sample_vol` seeds the legacy volume_samples deque. Post-fix the
        baseline is derived from `buckets`, so poisoning the deque is how a test
        proves the deque is no longer the source."""
        model = _Recorder()
        monkeypatch.setitem(det.ONE_MINUTE_DATA, SYMBOL, deque(buckets))
        monkeypatch.setitem(
            det.PUMP_DUMP_STATE,
            SYMBOL,
            {
                "avg_volume": 0.0,
                "volume_samples": deque([sample_vol] * 359, maxlen=360),
                "last_alert_time": datetime.datetime(1970, 1, 1, tzinfo=UTC),
            },
        )
        monkeypatch.setitem(
            det.PRICE_VOLUME_ALERT_STATE,
            SYMBOL,
            {"last_alert_time": datetime.datetime.now(UTC)},  # suppress section A alerts
        )
        monkeypatch.setattr(det, "load_pump_model", lambda: model)
        monkeypatch.setattr(det, "get_indicators_at_time", lambda *a, **kw: {})
        monkeypatch.setattr(det, "send_outbox", lambda *a, **kw: None)
        monkeypatch.setattr(det, "check_round_levels", lambda *a, **kw: None)
        monkeypatch.setattr(det, "log_prediction", lambda *a, **kw: None)
        if now_offset:
            # Freeze wall-clock `now` at `now_offset` seconds past the newest
            # bucket's grid position — the real phase offset (see below).
            frozen = _parse(buckets[-1]["t"]) + datetime.timedelta(seconds=now_offset)
            fake_dt = mock.MagicMock(wraps=datetime.datetime)
            fake_dt.now = lambda tz=None: frozen
            monkeypatch.setattr(det.datetime, "datetime", fake_dt)
        det.process_coin_logics(FakeConn(), SYMBOL)
        return model

    return _run


def _dense(now: datetime.datetime, n: int = 400, vol: float = 1.0) -> list[dict]:
    """n buckets, exactly 10s apart, flat price 100, ending at `now`."""
    return [_bucket(now - datetime.timedelta(seconds=10 * (n - 1 - i)), 100.0, vol) for i in range(n)]


# ── The 60s window is measured in seconds, not in list positions ──────────────


def test_p_chg_60s_is_measured_against_the_bucket_60s_ago(run_tick):
    now = datetime.datetime.now(UTC)
    buckets = _dense(now)
    buckets[-1] = _bucket(now, 110.0, 50.0)  # spike on the newest bucket

    model = run_tick(buckets)

    assert model.features, "model was never scored"
    # bucket 60s ago is priced 100 -> +10%
    assert model.features[0][F_P_CHG_60S] == pytest.approx(10.0)


def test_tick_is_skipped_when_the_60s_bucket_is_missing(run_tick):
    """A WS gap must not be scored with a fabricated 0 for p_chg_60s."""
    now = datetime.datetime.now(UTC)
    # An hour of history, then a 5-minute hole, then one fresh bucket.
    old = [_bucket(now - datetime.timedelta(seconds=300 + 10 * i), 100.0, 1.0) for i in range(360, 0, -1)]
    buckets = old + [_bucket(now, 110.0, 50.0)]

    model = run_tick(buckets)

    assert model.features == [], "model was scored across a 5-minute gap"


def test_old_index_math_would_have_used_a_stale_price(run_tick):
    """Regression witness: with a gap, prices[-7:] reaches across it.

    The fresh tail holds only 6 buckets, so the old `prices[-7:]` pulled in one
    bucket from before a 10-minute hole, priced 50 — and reported +100% in the
    "last 60 seconds". The time-based lookup finds the real 60s-ago bucket
    inside the tail (priced 100) and reports 0%.

    Verified: this test fails on the pre-fix file with p_chg_60s == 100.0.
    """
    now = datetime.datetime.now(UTC)
    stale = [_bucket(now - datetime.timedelta(seconds=600 + 10 * i), 50.0, 1.0) for i in range(360, 0, -1)]
    tail = [_bucket(now - datetime.timedelta(seconds=10 * (6 - i)), 100.0, 1.0) for i in range(6)]
    buckets = stale + tail
    buckets[-1] = _bucket(now, 100.0, 50.0)

    model = run_tick(buckets)
    # The 60s-ago bucket sits inside the dense tail (priced 100) -> 0% change,
    # NOT the +100% the stale 50.0 rows would have produced.
    assert model.features, "model was never scored"
    assert model.features[0][F_P_CHG_60S] == pytest.approx(0.0)


# ── The hourly volume baseline ignores invalid buckets ───────────────────────


def test_volume_baseline_skips_invalid_buckets(run_tick):
    now = datetime.datetime.now(UTC)
    buckets = _dense(now)
    # Poison every other bucket with a huge but INVALID volume.
    for i in range(0, len(buckets) - 1, 2):
        ts = datetime.datetime.fromisoformat(buckets[i]["t"])
        buckets[i] = _bucket(ts, 100.0, 1000.0, valid=False)
    buckets[-1] = _bucket(now, 110.0, 50.0)

    # Poison the legacy deque too: pre-fix it WAS the baseline, so a passing
    # assertion here would prove nothing about where the baseline comes from.
    model = run_tick(buckets, sample_vol=1000.0)

    assert model.features, "model was never scored"
    # Valid buckets carry volume 1.0 (plus the 50.0 spike), so the baseline sits
    # near 1.3 and vol_ratio lands in the tens. Had the invalid 1000.0 rows
    # leaked into the baseline it would be ~500, dragging vol_ratio below 1 —
    # under the bot's own vol_ratio >= 5 gate, i.e. the model would never run.
    assert model.features[0][F_VOL_RATIO] > 10.0


def test_volume_baseline_is_an_hour_of_wallclock_not_360_ticks(run_tick):
    """Buckets older than an hour must not enter the baseline."""
    now = datetime.datetime.now(UTC)
    # 360 recent buckets (1h) at volume 1.0, plus ancient buckets at 1000.0.
    recent = [_bucket(now - datetime.timedelta(seconds=10 * (359 - i)), 100.0, 1.0) for i in range(360)]
    ancient = [_bucket(now - datetime.timedelta(seconds=7200 + 10 * i), 100.0, 1000.0) for i in range(100, 0, -1)]
    buckets = ancient + recent
    buckets[-1] = _bucket(now, 110.0, 50.0)

    model = run_tick(buckets, sample_vol=1000.0)

    assert model.features, "model was never scored"
    # Only the last hour of buckets counts (volume 1.0). The ancient 1000.0
    # buckets and the poisoned deque must both stay out of the baseline.
    assert model.features[0][F_VOL_RATIO] == pytest.approx(50.0, rel=0.15)


# ── buy_pressure / volatility come from the same 60s window ──────────────────


def test_buy_pressure_and_volatility_use_the_60s_window(run_tick):
    now = datetime.datetime.now(UTC)
    buckets = _dense(now)
    buckets[-1] = _bucket(now, 110.0, 50.0)

    model = run_tick(buckets)
    feats = model.features[0]

    # 60s window = 7 buckets: six at 100, one at 110 -> exactly one up-move.
    assert feats[F_BUY_PRES] == pytest.approx(1 / 6)
    assert feats[F_VOLAT] > 0.0


# ── The window is anchored on the bucket grid, not on the wall clock ─────────


@pytest.mark.parametrize("offset", [0.0, 3.0, 6.0, 9.5])
def test_60s_window_is_stable_across_the_tick_phase_offset(run_tick, offset):
    """Bucket stamps are floored to the 10s grid (`_tick_epoch % 10`), but
    process_coin_logics runs at an arbitrary point inside that grid — and it
    iterates ~530 coins after a REST round-trip, so the offset drifts across the
    batch too.

    Anchored on wall-clock `now` with tolerance=5, the 60s-ago bucket fell
    outside the cutoff whenever the offset exceeded 5s: the window flipped
    between 6 and 7 buckets and buy_pres jumped between 1/5 and 1/6 while the
    market stood still. Anchored on the newest bucket's stamp, every target
    lands exactly on a grid point.
    """
    now = datetime.datetime.now(UTC)
    # Put the newest bucket on a clean grid point.
    grid = now.replace(microsecond=0) - datetime.timedelta(seconds=now.second % 10)
    buckets = [_bucket(grid - datetime.timedelta(seconds=10 * (399 - i)), 100.0, 1.0) for i in range(400)]
    buckets[-1] = _bucket(grid, 110.0, 50.0)

    model = run_tick(buckets, now_offset=offset)

    assert model.features, f"model was never scored at offset {offset}s"
    feats = model.features[0]
    # 7 buckets in the 60s window -> 6 diffs -> exactly one up-move.
    assert feats[F_BUY_PRES] == pytest.approx(1 / 6), f"window size flipped at offset {offset}s"
    assert feats[F_P_CHG_60S] == pytest.approx(10.0), f"p_chg_60s drifted at offset {offset}s"


# ── T-2026-CU-9050-035: the 10s grid is a fiction under load ─────────────────
#
# Measured on 421_350 real anchors from a live 1minute.json snapshot: median
# bucket spacing is 10s but p90 is 70s. Demanding a bucket at exactly anchor-60s
# +/- 5s therefore dropped 38.7% of all ticks unscored, and the hour baseline
# (>= 360 buckets) passed for literally 0 anchors, killing Volume-Explosion.


def _sparse(now: datetime.datetime, step: int, n: int, vol: float = 1.0, price: float = 100.0) -> list[dict]:
    """n buckets `step` seconds apart, ending exactly at `now`."""
    return [_bucket(now - datetime.timedelta(seconds=step * (n - 1 - i)), price, vol) for i in range(n)]


def test_find_bucket_nearest_returns_the_closest_age_not_the_newest():
    now = datetime.datetime.now(UTC)
    # Ages 40s (too new), 55s, 80s. Closest to 60 inside [45,150] is 55.
    data = [
        _bucket(now - datetime.timedelta(seconds=80), 1.0, 1.0),
        _bucket(now - datetime.timedelta(seconds=55), 2.0, 1.0),
        _bucket(now - datetime.timedelta(seconds=40), 3.0, 1.0),
        _bucket(now, 4.0, 1.0),
    ]
    entry, age = det._find_bucket_nearest(data, now, 60, 45, 150)
    assert float(entry["p"]) == 2.0
    assert age == pytest.approx(55.0)


def test_find_bucket_nearest_rejects_ages_outside_the_band():
    now = datetime.datetime.now(UTC)
    only_too_new = [_bucket(now - datetime.timedelta(seconds=20), 1.0, 1.0), _bucket(now, 2.0, 1.0)]
    only_too_old = [_bucket(now - datetime.timedelta(seconds=400), 1.0, 1.0), _bucket(now, 2.0, 1.0)]
    assert det._find_bucket_nearest(only_too_new, now, 60, 45, 150) is None
    assert det._find_bucket_nearest(only_too_old, now, 60, 45, 150) is None


def test_p_chg_60s_is_normalised_to_a_per_60s_rate(run_tick):
    """A 70s cadence must yield a rate, not a skipped tick and not an inflated move.

    Pre-fix this tick returned early (no bucket at 60s +/- 5s) and was never
    scored. Reference bucket is 70s old and 9.0% below spot, so the honest
    per-60s rate is 9.0 * 60/70 = 7.714%, not 9.0%.
    """
    now = datetime.datetime.now(UTC)
    buckets = _sparse(now, step=70, n=60)
    buckets[-1] = _bucket(now, 109.0, 50.0)

    model = run_tick(buckets)

    assert model.features, "70s cadence must still be scored"
    assert model.features[0][F_P_CHG_60S] == pytest.approx(9.0 * 60 / 70, rel=1e-6)


def test_normalisation_is_identity_on_a_dense_grid(run_tick):
    """The scale factor must not perturb the 10s path — 60/60 == 1."""
    now = datetime.datetime.now(UTC)
    buckets = _dense(now)
    buckets[-1] = _bucket(now, 110.0, 50.0)

    model = run_tick(buckets)
    assert model.features[0][F_P_CHG_60S] == pytest.approx(10.0)


def test_tick_is_still_skipped_when_the_band_holds_nothing(run_tick):
    """Refusing to invent a value survives the change: a 5m hole stays unscored."""
    now = datetime.datetime.now(UTC)
    old = [_bucket(now - datetime.timedelta(seconds=300 + 10 * i), 100.0, 1.0) for i in range(360, 0, -1)]
    buckets = old + [_bucket(now, 110.0, 50.0)]

    assert run_tick(buckets).features == [], "scored across a 5-minute gap"


def test_window_coverage_is_a_span_not_a_count():
    now = datetime.datetime.now(UTC)
    sparse_hour = _sparse(now, step=70, n=52)  # 52 buckets, ~3570s of span
    assert len(sparse_hour) < 360
    assert det._window_coverage_sec(sparse_hour, now) == pytest.approx(3570.0)
    assert det._window_coverage_sec([], now) == 0.0


def test_ml_baseline_refuses_a_one_sample_hour(run_tick):
    """A single surviving bucket must not become the volume baseline.

    `vol_ratio = current_vol / avg_volume` is a model input AND the
    pump_dump_events insert gate. Post-gap, `if not hour_vols` alone let one
    bucket define avg_volume, inflating vol_ratio without bound.
    """
    now = datetime.datetime.now(UTC)
    # Enough warmup ticks, but the hour window holds only a couple of buckets:
    # ancient history (>1h old) plus a short fresh tail.
    ancient = [_bucket(now - datetime.timedelta(seconds=7200 + 10 * i), 100.0, 1.0) for i in range(400, 0, -1)]
    tail = [_bucket(now - datetime.timedelta(seconds=60 - 10 * i), 100.0, 1.0) for i in range(6)]
    buckets = ancient + tail
    buckets[-1] = _bucket(now, 110.0, 500.0)

    assert run_tick(buckets).features == [], "scored against a one-sample hourly baseline"


def test_volume_explosion_fires_at_a_realistic_cadence(monkeypatch):
    """The >= 360-bucket baseline gate made this alert unreachable in production.

    Pre-fix: len(hour_vols) == 52 < 360 -> the branch was dead for every symbol.
    Post-fix the hour window is judged by the span it covers (~3570s) and a
    sample floor, so a genuine 13x volume explosion alerts again.
    """
    now = datetime.datetime.now(UTC)
    buckets = _sparse(now, step=70, n=52)  # ~1h of history at the real cadence
    # +2.5% over the last ~3m: enough for the volume-explosion price condition,
    # below every threshold of the price-move alert above it.
    buckets[-1] = _bucket(now, 102.5, 60.0)
    for k in (2, 3):  # the other two buckets inside the 180s window
        ts = _parse(buckets[-k]["t"])
        buckets[-k] = _bucket(ts, 100.0, 60.0)

    sent: list[str] = []
    monkeypatch.setitem(det.ONE_MINUTE_DATA, SYMBOL, deque(buckets))
    monkeypatch.setitem(
        det.PUMP_DUMP_STATE,
        SYMBOL,
        {
            "avg_volume": 0.0,
            "volume_samples": deque([1.0] * 359, maxlen=360),
            "last_alert_time": datetime.datetime(1970, 1, 1, tzinfo=UTC),
        },
    )
    # Let section A run: no price alert will trigger, but the volume branch will.
    monkeypatch.setitem(
        det.PRICE_VOLUME_ALERT_STATE,
        SYMBOL,
        {"last_alert_time": datetime.datetime(1970, 1, 1, tzinfo=UTC)},
    )
    monkeypatch.setattr(det, "load_pump_model", lambda: None)  # stop before the ML path
    monkeypatch.setattr(det, "get_indicators_at_time", lambda *a, **kw: {})
    monkeypatch.setattr(det, "check_round_levels", lambda *a, **kw: None)
    monkeypatch.setattr(det, "log_prediction", lambda *a, **kw: None)
    monkeypatch.setattr(det, "generate_minichart_image", lambda *a, **kw: None)
    monkeypatch.setattr(det, "send_outbox", lambda conn, chan, html, chart=None: sent.append(html))

    det.process_coin_logics(FakeConn(), SYMBOL)

    assert any("VOLUME EXPLOSION" in h for h in sent), f"volume explosion never fired at a 70s cadence; sent={sent}"
