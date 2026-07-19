# backtest/test_detector_scan_optimization.py — T-2026-CU-9050-172, DB-free.
"""
Parity guards for the classic-detector scan optimization (behaviour-invariant
by operator directive): query bundling, per-cycle guard snapshots and the
active-trade prefilter must NOT change the set of emitted signal dicts.

Pinned behaviour:
  (3)  detect_volume_spike_in_period: ONE bundled 15d read + pandas split is
       byte-equivalent to the two old window reads (5d period + 10d baseline,
       baseline end = open_time_1st_hit - 30m). Edge cases: spike at i==0,
       empty baseline, empty period window, boundary candles.
  (4a) check_recent_trades memoisation: same result, one query per distinct
       (direction, hours, count) per cycle.
  (4b) DetectorCycle.is_trade_active / all_directions_active mirror
       is_trade_already_active; the whole-coin prefilter and the SR/Main/Vol
       early skips only ever skip coins that could not emit anyway.
  (4c) DetectorCycle.cooldown_active mirrors check_cooldown incl. the to_utc
       normalisation of naive DB timestamps.
  (1)  DETECTOR_INDICATOR_COLUMNS ⊇ every column the five strategies read
       (P2.43: a missing column kills signals SILENTLY) and ⊆ the engine DDL
       (a typo'd name would fail the read and silently skip the coin).

Run with: pytest backtest/test_detector_scan_optimization.py -v
      or: python backtest/test_detector_scan_optimization.py
"""

from __future__ import annotations

import ast
import datetime
import importlib.util
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# Offline cred shim: core.config hard-requires these at import (never connects here).
for _k, _v in {
    "DB_PASSWORD": "test", "TELEGRAM_BOT_TOKEN": "test",
    "DB_HOST": "127.0.0.1", "DB_NAME": "t", "DB_USER": "t", "DB_PORT": "5432",
}.items():
    os.environ.setdefault(_k, _v)

import core.market_utils as mu  # noqa: E402
import strategies.strat_5_percent as s5  # noqa: E402
import strategies.strat_fast_in_out as sf  # noqa: E402
import strategies.strat_main_channel as smc  # noqa: E402
import strategies.strat_support_resistance as ssr  # noqa: E402
import strategies.strat_volume_indicator as vol  # noqa: E402
from core.candles import timeframe_delta  # noqa: E402
from core.time import utc_now  # noqa: E402


def _load_by_path(module_name: str, filename: str):
    """Import a digit-prefixed module by path (mirrors the regression guard)."""
    path = os.path.join(REPO_ROOT, filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DET = _load_by_path("kythera_detectors", "3_detectors.py")


# ── fakes ─────────────────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        q = " ".join(str(query).split())
        c = self._conn
        c.query_log.append(q)
        if q.startswith("SELECT strategy, coin, direction FROM active_trades_master"):
            self._result = list(c.active_rows)
        elif "SELECT EXISTS" in q and "active_trades_master" in q:
            coin, direction, strategy = params
            self._result = [((strategy, coin, direction) in set(c.active_rows),)]
        elif q.startswith("SELECT last_posted_at FROM trade_cooldowns"):
            ts = c.cooldown_rows.get((params[0], params[1], params[2]))
            self._result = [] if ts is None else [(ts,)]
        elif q.startswith("SELECT coin, direction, last_posted_at FROM trade_cooldowns"):
            self._result = [(k[1], k[2], v) for k, v in c.cooldown_rows.items() if k[0] == params[0]]
        elif "FROM closed_trades_master" in q:
            c.recent_query_count += 1
            self._result = [(c.recent_counts.get(params[0], 0),)]
        else:
            self._result = []  # DDL / INSERT from write_signal_atomic etc.

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeConn:
    """Serves exactly the guard queries the classic strategies issue."""

    def __init__(self, active_rows=(), cooldown_rows=None, recent_counts=None):
        self.active_rows = list(active_rows)  # (strategy, coin, direction)
        self.cooldown_rows = dict(cooldown_rows or {})  # (module, coin, direction) -> ts
        self.recent_counts = dict(recent_counts or {})  # direction -> count
        self.recent_query_count = 0
        self.query_log = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        self.rollbacks = getattr(self, "rollbacks", 0) + 1

    def close(self):
        pass


class FakeCandleSource:
    """read_candles stand-in serving inclusive [start, end] windows from one frame."""

    def __init__(self, df):
        self.df = df
        self.calls = 0

    def read(self, conn, symbol, tf, *, start=None, end=None, limit=None,
             include_forming=False, columns=None):
        self.calls += 1
        out = self.df
        if start is not None:
            out = out[out["open_time"] >= start]
        if end is not None:
            out = out[out["open_time"] <= end]
        if limit is not None:
            out = out.tail(limit)
        cols = list(columns) if columns is not None else list(out.columns)
        return out[cols].reset_index(drop=True)


def _grid(start, n):
    """n consecutive 30m open_times from `start` (aware UTC)."""
    return [start + datetime.timedelta(minutes=30 * i) for i in range(n)]


# ── (3) bundled 15d spike read: parity against the old two-read version ───────


def _old_detect_volume_spike_in_period(read_fn, conn, symbol, open_time_1st_hit, open_time_hit):
    """Verbatim pre-T-172 implementation (two reads), against the same fake."""
    try:
        df_period = read_fn(
            conn, symbol, "30m", start=open_time_1st_hit, end=open_time_hit,
            include_forming=False, columns=("open_time", "close", "volume"),
        )
        if df_period.empty:
            return 0
        hist_start = open_time_1st_hit - datetime.timedelta(days=10)
        df_hist = read_fn(
            conn, symbol, "30m", start=hist_start,
            end=open_time_1st_hit - timeframe_delta("30m"),
            include_forming=False, columns=("open_time", "close", "volume"),
        )
        if df_hist.empty:
            return 0
        volume_mean, volume_std = df_hist["volume"].mean(), df_hist["volume"].std()
        spike_threshold = volume_mean + 3 * volume_std
        return vol._classify_latest_volume_spike(df_period, spike_threshold)
    except Exception:
        return 0


def _spike_parity(candles_df, open_time_1st_hit, open_time_hit):
    """Runs old vs new implementation on the same candle set; returns both."""
    source = FakeCandleSource(candles_df)
    old = _old_detect_volume_spike_in_period(source.read, object(), "TESTUSDT", open_time_1st_hit, open_time_hit)
    old_calls = source.calls
    source.calls = 0
    orig = vol.read_candles
    vol.read_candles = source.read
    try:
        new = vol.detect_volume_spike_in_period(object(), "TESTUSDT", open_time_1st_hit, open_time_hit)
    finally:
        vol.read_candles = orig
    return old, new, old_calls, source.calls


BASE = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _candles(rows):
    """rows: (open_time, close, volume)."""
    return pd.DataFrame(rows, columns=["open_time", "close", "volume"])


def test_spike_parity_buy_and_sell():
    times = _grid(BASE, 20)
    first_hit, hit = times[10], times[19]
    # baseline: times[0..9] volume 10; period: spike at index 15 (close up = buy)
    closes = [100.0 + i * 0.1 for i in range(20)]
    vols = [10.0] * 20
    vols[15] = 500.0
    old, new, old_calls, new_calls = _spike_parity(_candles(list(zip(times, closes, vols))), first_hit, hit)
    assert old == new == 1
    assert old_calls == 2 and new_calls == 1  # the bundling is the point

    closes_down = [100.0 - i * 0.1 for i in range(20)]
    old, new, _, _ = _spike_parity(_candles(list(zip(times, closes_down, vols))), first_hit, hit)
    assert old == new == -1
    print("OK  spike parity: buy/sell classification identical, 2 reads → 1 read")


def test_spike_parity_spike_on_first_period_candle():
    times = _grid(BASE, 20)
    first_hit, hit = times[10], times[19]
    vols = [10.0] * 20
    vols[10] = 500.0  # exactly the first in-period candle → i==0 → discarded (P2.42b)
    closes = [100.0 + i * 0.1 for i in range(20)]
    old, new, _, _ = _spike_parity(_candles(list(zip(times, closes, vols))), first_hit, hit)
    assert old == new == 0
    print("OK  spike parity: i==0 spike discarded in both implementations")


def test_spike_parity_empty_baseline():
    times = _grid(BASE, 10)
    first_hit, hit = times[0], times[9]  # no candle before first_hit → baseline empty
    vols = [10.0] * 10
    vols[5] = 500.0
    closes = [100.0 + i * 0.1 for i in range(10)]
    old, new, _, _ = _spike_parity(_candles(list(zip(times, closes, vols))), first_hit, hit)
    assert old == new == 0
    print("OK  spike parity: empty baseline → 0 in both implementations")


def test_spike_parity_empty_period():
    times = _grid(BASE, 10)  # data ends before the period window starts
    first_hit = times[-1] + datetime.timedelta(hours=5)
    hit = first_hit + datetime.timedelta(days=1)
    old, new, _, _ = _spike_parity(
        _candles([(t, 100.0, 10.0) for t in times]), first_hit, hit
    )
    assert old == new == 0
    print("OK  spike parity: empty period window → 0 in both implementations")


def test_spike_parity_boundary_candles():
    """The candle at first_hit-30m belongs to the BASELINE, the one at first_hit
    to the PERIOD, the one at hit is included, one bar after hit excluded. A
    misassigned boundary candle shifts mean/std or the classification — the
    huge volumes make any drift visible."""
    times = _grid(BASE, 21)
    first_hit, hit = times[10], times[19]  # times[20] lies after the window
    vols = [10.0] * 21
    vols[9] = 400.0    # boundary baseline candle (first_hit - 30m) → lifts threshold
    vols[19] = 5000.0  # spike exactly on the hit bar (close down → sell)
    vols[20] = 9000.0  # after the window — must be invisible
    closes = [100.0 + i * 0.1 for i in range(21)]
    closes[19] = 90.0
    old, new, _, _ = _spike_parity(_candles(list(zip(times, closes, vols))), first_hit, hit)
    assert old == new == -1
    print("OK  spike parity: inclusive/exclusive window boundaries identical")


def test_spike_parity_misaligned_bar_excluded_from_both_windows():
    """A (contract-violating) bar strictly inside (1st_hit-30m, 1st_hit) was in
    NEITHER old window. A naive complement split would pull it into the baseline
    and shift mean/std — the three-way split must keep it excluded."""
    times = _grid(BASE, 20)
    first_hit, hit = times[10], times[19]
    vols = [10.0] * 20
    vols[15] = 500.0  # real in-period spike (close up → buy)
    closes = [100.0 + i * 0.1 for i in range(20)]
    rows = list(zip(times, closes, vols))
    # Misaligned monster bar 15 minutes before the period start: with it in the
    # baseline, std explodes and the threshold would swallow the 500-spike.
    rows.append((first_hit - datetime.timedelta(minutes=15), 100.0, 50000.0))
    df = _candles(rows).sort_values("open_time").reset_index(drop=True)
    old, new, _, _ = _spike_parity(df, first_hit, hit)
    assert old == new == 1
    print("OK  spike parity: misaligned bar stays outside both windows (3-way split)")


def test_spike_parity_randomised_sweep():
    rng = np.random.default_rng(7)
    for case in range(60):
        n = int(rng.integers(2, 40))
        times = _grid(BASE, n)
        split = int(rng.integers(0, n))  # first in-period index
        first_hit = times[split]
        hit = times[-1]
        closes = 100.0 + rng.normal(0, 1.0, n).cumsum()
        vols = rng.uniform(5.0, 20.0, n)
        for _ in range(int(rng.integers(0, 3))):  # sprinkle spikes
            vols[int(rng.integers(0, n))] = float(rng.uniform(100.0, 1000.0))
        df = _candles(list(zip(times, closes, vols)))
        old, new, _, _ = _spike_parity(df, first_hit, hit)
        assert old == new, f"sweep case {case}: old={old} new={new}"
    print("OK  spike parity: 60-case randomised sweep identical")


# ── (4b/4c) guard snapshots: parity against the per-call queries ──────────────

ACTIVE_ROWS = [
    ("5 Percent", "AAAUSDT", "LONG"),
    ("Volume Indicator", "BBBUSDT", "SHORT"),
    ("Fast In And Out", "CCCUSDT", "LONG"),
    ("Fast In And Out", "CCCUSDT", "SHORT"),
    ("Volume Indicator", "CCCUSDT", "LONG"),
    ("Volume Indicator", "CCCUSDT", "SHORT"),
]


def test_active_trade_snapshot_parity():
    conn = FakeConn(active_rows=ACTIVE_ROWS)
    cycle = mu.DetectorCycle(conn)
    for strategy in ["5 Percent", "Volume Indicator", "Fast In And Out", "Support Resistance"]:
        for coin in ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]:
            for direction in ["LONG", "SHORT"]:
                assert cycle.is_trade_active(coin, direction, strategy) == mu.is_trade_already_active(
                    conn, coin, direction, strategy
                ), (strategy, coin, direction)
    print("OK  snapshot parity: is_trade_active == is_trade_already_active (32-way matrix)")


def test_whole_coin_prefilter_condition():
    cycle = mu.DetectorCycle(FakeConn(active_rows=ACTIVE_ROWS))
    # CCCUSDT: both 30m strategies WORKING in both directions → skippable.
    assert cycle.all_directions_active("CCCUSDT", ("Fast In And Out", "Volume Indicator")) is True
    # AAAUSDT: only one pair occupied → must still be scanned.
    assert cycle.all_directions_active("AAAUSDT", ("5 Percent", "Support Resistance")) is False
    # Empty roster is never skippable via this helper's contract in 3_detectors
    # (the caller guards `roster and ...`), but all() over nothing is True —
    # assert the caller-side contract exists:
    assert DET._strategies_for("30m", "AAAUSDT") == ("Fast In And Out", "Volume Indicator")
    assert DET._strategies_for("1h", "AAAUSDT") == ("5 Percent", "Support Resistance")
    assert DET._strategies_for("2h", "AAAUSDT") == ()
    print("OK  prefilter: whole-coin skip only when every (strategy, direction) is WORKING")


def test_cooldown_snapshot_parity():
    now = utc_now()
    rows = {
        ("VolIndic", "AAAUSDT", "LONG"): now - datetime.timedelta(hours=1),   # active
        ("VolIndic", "BBBUSDT", "SHORT"): now - datetime.timedelta(hours=13),  # expired
        # naive timestamp (legacy storage) — to_utc interprets as UTC on both paths
        ("VolIndic", "CCCUSDT", "LONG"): (now - datetime.timedelta(hours=2)).replace(tzinfo=None),
    }
    conn = FakeConn(cooldown_rows=rows)
    cycle = mu.DetectorCycle(conn)
    for coin in ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]:
        for direction in ["LONG", "SHORT"]:
            assert cycle.cooldown_active("VolIndic", coin, direction, 12) == mu.check_cooldown(
                conn, "VolIndic", coin, direction, 12
            ), (coin, direction)
    # The module snapshot was loaded exactly once for all eight checks.
    snapshot_queries = [q for q in conn.query_log if q.startswith("SELECT coin, direction, last_posted_at")]
    assert len(snapshot_queries) == 1
    print("OK  snapshot parity: cooldown_active == check_cooldown incl. naive-ts normalisation")


def test_cycle_memo_runs_once_per_key():
    calls = []
    cycle = mu.DetectorCycle(FakeConn())
    assert cycle.memo(("k", 1), lambda: calls.append(1) or "a") == "a"
    assert cycle.memo(("k", 1), lambda: calls.append(1) or "b") == "a"  # replayed
    assert cycle.memo(("k", 2), lambda: calls.append(1) or "c") == "c"
    assert len(calls) == 2
    print("OK  memo: one evaluation per key per cycle")


def test_note_signal_written_mirrors_db_state():
    conn = FakeConn()
    cycle = mu.DetectorCycle(conn)
    assert cycle.cooldown_active("VolIndic", "AAAUSDT", "LONG", 12) is False  # loads snapshot
    assert cycle.is_trade_active("AAAUSDT", "LONG", "Volume Indicator") is False
    cycle.note_signal_written("Volume Indicator", "AAAUSDT", "LONG", cooldown_module="VolIndic")
    assert cycle.is_trade_active("AAAUSDT", "LONG", "Volume Indicator") is True
    assert cycle.cooldown_active("VolIndic", "AAAUSDT", "LONG", 12) is True
    print("OK  note_signal_written: own write visible to later in-cycle lookups")


# ── end-to-end strategy parity: cycle path vs per-call path ───────────────────


def _long_pass_row():
    """Per-bar indicator row satisfying every non-S/R LONG condition of both
    5-Percent and Fast-In-Out (close=100); mirrors test_window_features."""
    return {
        "close": 100.0,
        "rsi_9": 65.0, "rsi_14": 65.0,
        "tsi_fast_12_7_7": 20.0, "tsi_fast_12_7_7_signal": 10.0,
        "ema_9": 99.0, "ema_12": 98.0, "ema_21": 97.0, "ema_26": 96.0,
        "ema_55": 90.0, "ema_89": 89.0, "ema_200": 88.0,
        "wma_9": 98.0, "wma_12": 97.0, "wma_21": 93.0, "wma_26": 92.0,
        "kama_9": 91.0, "kama_12": 90.5, "kama_21": 90.2,
        "macd_dif_fast_9_21_9": 1.0, "macd_dea_fast_9_21_9": 0.5,
        "donchian_mid_4": 95.0, "boll_mid_20": 96.0, "atr_14": 1.5,
    }


def _long_frame():
    row = {**_long_pass_row(), "support_price": 98.0, "resistance_price": 110.0}
    older = {**_long_pass_row(), "support_price": np.nan, "resistance_price": np.nan}
    return pd.DataFrame([row, older, older])


def test_fast_in_out_signal_parity_and_memo():
    df = _long_frame()
    plain = sf.analyze_coin(FakeConn(), "TESTUSDT", df, 100.0)

    conn = FakeConn()
    cycle = mu.DetectorCycle(conn)
    via_cycle = sf.analyze_coin(conn, "TESTUSDT", df, 100.0, cycle=cycle)
    assert plain is not None and plain == via_cycle

    # Second coin in the same cycle: check_recent_trades must be memoised.
    sf.analyze_coin(conn, "XXXUSDT", df, 100.0, cycle=cycle)
    assert conn.recent_query_count == 1

    # Blocked variants behave identically on both paths.
    blocked_conn = FakeConn(active_rows=[("Fast In And Out", "TESTUSDT", "LONG")])
    assert sf.analyze_coin(blocked_conn, "TESTUSDT", df, 100.0) is None
    assert sf.analyze_coin(blocked_conn, "TESTUSDT", df, 100.0, cycle=mu.DetectorCycle(blocked_conn)) is None

    hot_conn = FakeConn(recent_counts={"LONG": 501, "SHORT": 501})
    assert sf.analyze_coin(hot_conn, "TESTUSDT", df, 100.0) is None
    assert sf.analyze_coin(hot_conn, "TESTUSDT", df, 100.0, cycle=mu.DetectorCycle(hot_conn)) is None
    print("OK  Fast In And Out: identical signal dicts, memoised direction cooldown")


def test_5_percent_signal_parity():
    df = _long_frame()
    plain = s5.analyze_coin(FakeConn(), "TESTUSDT", df, 100.0)
    conn = FakeConn()
    via_cycle = s5.analyze_coin(conn, "TESTUSDT", df, 100.0, cycle=mu.DetectorCycle(conn))
    assert plain is not None and plain == via_cycle

    blocked_conn = FakeConn(active_rows=[("5 Percent", "TESTUSDT", "LONG")])
    assert s5.analyze_coin(blocked_conn, "TESTUSDT", df, 100.0) is None
    assert s5.analyze_coin(blocked_conn, "TESTUSDT", df, 100.0, cycle=mu.DetectorCycle(blocked_conn)) is None
    print("OK  5 Percent: identical signal dicts on both paths")


def _vol_setup():
    """15d 30m grid with one fresh buy spike; indicator frame for analyze_coin."""
    n = 15 * 48
    times = _grid(BASE, n)
    closes = [100.0 + (i % 7) * 0.01 for i in range(n)]
    vols = [10.0] * n
    vols[n - 3] = 500.0             # newest spike, close up vs predecessor → buy
    closes[n - 3] = closes[n - 4] + 1.0
    candles = _candles(list(zip(times, closes, vols)))
    latest = times[-1]
    ind = pd.DataFrame({"close": [closes[-1], closes[-2]]}, index=[latest, times[-2]])
    return candles, ind


def test_volume_indicator_signal_parity():
    candles, ind = _vol_setup()
    source = FakeCandleSource(candles)
    orig_read, orig_hvn = vol.read_candles, vol.detect_high_volume_zone
    vol.read_candles = source.read
    vol.detect_high_volume_zone = lambda *a, **k: True  # unchanged code path, not under test
    try:
        plain = vol.analyze_coin(FakeConn(), "TESTUSDT", ind, 100.0)
        conn = FakeConn()
        via_cycle = vol.analyze_coin(conn, "TESTUSDT", ind, 100.0, cycle=mu.DetectorCycle(conn))
        assert plain is not None and plain["direction"] == "LONG"
        assert plain == via_cycle

        # Direction on cooldown → None on both paths.
        cd = {("VolIndic", "TESTUSDT", "LONG"): utc_now() - datetime.timedelta(hours=1)}
        cd_conn = FakeConn(cooldown_rows=cd)
        assert vol.analyze_coin(cd_conn, "TESTUSDT", ind, 100.0) is None
        assert vol.analyze_coin(cd_conn, "TESTUSDT", ind, 100.0, cycle=mu.DetectorCycle(cd_conn)) is None

        # Direction already active → None on both paths.
        act_conn = FakeConn(active_rows=[("Volume Indicator", "TESTUSDT", "LONG")])
        assert vol.analyze_coin(act_conn, "TESTUSDT", ind, 100.0) is None
        assert vol.analyze_coin(act_conn, "TESTUSDT", ind, 100.0, cycle=mu.DetectorCycle(act_conn)) is None

        # BOTH directions active + cycle → skip BEFORE the spike read (0 candle
        # reads); the per-call path still returns None (after its reads).
        both = FakeConn(active_rows=[
            ("Volume Indicator", "TESTUSDT", "LONG"),
            ("Volume Indicator", "TESTUSDT", "SHORT"),
        ])
        source.calls = 0
        assert vol.analyze_coin(both, "TESTUSDT", ind, 100.0, cycle=mu.DetectorCycle(both)) is None
        assert source.calls == 0
        assert vol.analyze_coin(both, "TESTUSDT", ind, 100.0) is None
        assert source.calls >= 1
    finally:
        vol.read_candles, vol.detect_high_volume_zone = orig_read, orig_hvn
    print("OK  Volume Indicator: identical signals; both-directions-active skips the spike read")


def _sr_frame():
    """DESC 1h frame: newest bar carries S/R and hits support; rising RSI."""
    n = 60
    idx = pd.date_range("2026-01-10", periods=n, freq="h", tz="UTC")[::-1]
    rows = []
    for i in range(n):
        rows.append({
            "support_price": 100.0 if i == 0 else np.nan,
            "resistance_price": 110.0 if i == 0 else np.nan,
            "close": 100.5 if i == 0 else 100.3,  # inside the support hit zone
            "rsi_9": 65.0 if i == 0 else 60.0,
            "rsi_14": 65.0 if i == 0 else 60.0,
            "atr_14": 1.0,
        })
    return pd.DataFrame(rows, index=idx)


def _ohlcv(n=250):
    times = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({
        "open_time": times,
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0,
    })


def test_support_resistance_parity_and_early_skip():
    df = _sr_frame()
    source = FakeCandleSource(_ohlcv())
    orig = (ssr.read_candles, ssr.find_support_resistance_zones, ssr.calculate_obv)
    ssr.read_candles = source.read
    ssr.find_support_resistance_zones = lambda d: ([(95.0, 3)], [(102.0, 3), (104.0, 2)])
    ssr.calculate_obv = lambda *a, **k: 1
    try:
        plain = ssr.analyze_coin(FakeConn(), "TESTUSDT", df, 100.5)
        conn = FakeConn()
        via_cycle = ssr.analyze_coin(conn, "TESTUSDT", df, 100.5, cycle=mu.DetectorCycle(conn))
        assert plain is not None and plain["direction"] == "LONG"
        assert plain == via_cycle

        # LONG occupied: None on both paths — the cycle path must skip BEFORE
        # the 480-bar OHLCV read, the per-call path reads first (old order).
        blocked = FakeConn(active_rows=[("Support Resistance", "TESTUSDT", "LONG")])
        source.calls = 0
        assert ssr.analyze_coin(blocked, "TESTUSDT", df, 100.5, cycle=mu.DetectorCycle(blocked)) is None
        assert source.calls == 0
        assert ssr.analyze_coin(blocked, "TESTUSDT", df, 100.5) is None
        assert source.calls == 1
    finally:
        ssr.read_candles, ssr.find_support_resistance_zones, ssr.calculate_obv = orig
    print("OK  Support Resistance: identical signals; early skip avoids the OHLCV read")


def test_main_channel_parity_and_early_skip():
    df = _sr_frame()
    source = FakeCandleSource(_ohlcv())
    orig = (smc.read_candles, smc.find_support_resistance_zones, smc.calculate_obv)
    smc.read_candles = source.read
    smc.find_support_resistance_zones = lambda d: ([(95.0, 3)], [(102.0, 3), (104.0, 2)])
    smc.calculate_obv = lambda *a, **k: 1
    try:
        plain = smc.analyze_coin(FakeConn(), "TESTUSDT", df, 100.5)
        conn = FakeConn()
        via_cycle = smc.analyze_coin(conn, "TESTUSDT", df, 100.5, cycle=mu.DetectorCycle(conn))
        assert plain is not None and plain["direction"] == "LONG"
        assert plain == via_cycle

        blocked = FakeConn(active_rows=[("Main Channel", "TESTUSDT", "LONG")])
        source.calls = 0
        assert smc.analyze_coin(blocked, "TESTUSDT", df, 100.5, cycle=mu.DetectorCycle(blocked)) is None
        assert source.calls == 0
        assert smc.analyze_coin(blocked, "TESTUSDT", df, 100.5) is None
        assert source.calls == 1
    finally:
        smc.read_candles, smc.find_support_resistance_zones, smc.calculate_obv = orig
    print("OK  Main Channel: identical signals; early skip avoids the OHLCV read")


# ── (1) projection tests: ⊇ strategy reads, ⊆ engine DDL ─────────────────────

# Variables that hold the DETECTOR indicator frame (or rows of it) inside the
# strategy modules. Candle frames (df_hist/df_period/df_all/df_ohlcv) are
# intentionally NOT in this set — they come from read_candles, not from the
# projected indicator read.
INDICATOR_FRAME_VARS = {"data", "df_indicators", "last_row", "sr_row", "current_row", "first_hit_row", "row"}

STRATEGY_FILES = [
    "strategies/strat_5_percent.py",
    "strategies/strat_fast_in_out.py",
    "strategies/strat_volume_indicator.py",
    "strategies/strat_support_resistance.py",
    "strategies/strat_main_channel.py",
]


def _ast_indicator_column_reads(path):
    with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    cols = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in INDICATOR_FRAME_VARS
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            cols.add(node.slice.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in INDICATOR_FRAME_VARS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            cols.add(node.args[0].value)
    return cols


def test_projection_superset_of_every_strategy_read():
    proj = set(DET.DETECTOR_INDICATOR_COLUMNS)
    assert "open_time" in proj  # frame index + read ordering
    missing = set(s5.REQUIRED_COLUMNS) - proj
    assert not missing, f"strat_5_percent REQUIRED_COLUMNS not projected: {missing}"
    missing = set(sf.REQUIRED_COLUMNS) - proj
    assert not missing, f"strat_fast_in_out REQUIRED_COLUMNS not projected: {missing}"
    for path in STRATEGY_FILES:
        reads = _ast_indicator_column_reads(path)
        missing = reads - proj
        assert not missing, f"{path}: per-row column reads not projected: {missing}"
    print("OK  projection ⊇ REQUIRED_COLUMNS ∪ AST-collected per-row reads (all 5 strategies)")


def test_projection_subset_of_engine_ddl():
    engine = _load_by_path("kythera_indicator_engine", "2_indicator_engine.py")
    ddl = {"symbol", "open_time", "close"} | {name.lower() for name in engine.get_indicator_definitions()}
    unknown = set(DET.DETECTOR_INDICATOR_COLUMNS) - ddl
    assert not unknown, f"projected columns missing from the engine DDL: {unknown}"
    print("OK  projection ⊆ engine DDL (a typo cannot silently skip coins)")


# ── detector wiring: projection used, prefilter applied, cycle passed ─────────


def test_run_detectors_wiring_and_prefilter():
    read_calls, scan_calls = [], []

    def fake_read_indicators(conn, symbol, timeframe, *, limit=None, include_forming=True, columns=None):
        assert limit == 480 and include_forming is False
        assert tuple(columns) == DET.DETECTOR_INDICATOR_COLUMNS
        read_calls.append(symbol)
        times = pd.date_range("2026-01-01", periods=3, freq="30min", tz="UTC")
        frame = {col: [1.0, 1.0, 1.0] for col in DET.DETECTOR_INDICATOR_COLUMNS if col != "open_time"}
        frame["open_time"] = times
        return pd.DataFrame(frame)

    def fake_analyze(conn, symbol, frame, live_price, cycle=None):
        scan_calls.append((symbol, isinstance(cycle, mu.DetectorCycle)))
        return None

    # BBBUSDT: both 30m strategies WORKING in both directions → prefilter skip.
    conn = FakeConn(active_rows=[
        ("Fast In And Out", "BBBUSDT", "LONG"), ("Fast In And Out", "BBBUSDT", "SHORT"),
        ("Volume Indicator", "BBBUSDT", "LONG"), ("Volume Indicator", "BBBUSDT", "SHORT"),
    ])

    saved = {name: getattr(DET, name) for name in
             ("read_indicators", "analyze_fast", "analyze_vol", "get_db_connection", "get_live_prices_batch")}
    tmpdir = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        with open(os.path.join(tmpdir, "coins.json"), "w", encoding="utf-8") as f:
            json.dump(["AAAUSDT", "BBBUSDT"], f)
        os.chdir(tmpdir)
        DET.read_indicators = fake_read_indicators
        DET.analyze_fast = fake_analyze
        DET.analyze_vol = fake_analyze
        DET.get_db_connection = lambda: conn
        DET.get_live_prices_batch = lambda: {"AAAUSDT": 100.0, "BBBUSDT": 100.0}
        DET.run_detectors_for_timeframe("30m")
    finally:
        os.chdir(cwd)
        for name, value in saved.items():
            setattr(DET, name, value)

    assert read_calls == ["AAAUSDT"], f"prefilter must skip BBBUSDT before its read: {read_calls}"
    assert scan_calls == [("AAAUSDT", True), ("AAAUSDT", True)], scan_calls
    print("OK  wiring: projection + prefilter applied, cycle passed to strategies")


def test_failed_indicator_read_rolls_back_and_isolates_the_coin():
    """A failed read (missing table / drifted DDL missing a projected column)
    aborts the transaction — without a rollback every later coin's read would
    die with InFailedSqlTransaction (review fix). The detector must roll back
    and continue scanning the remaining coins."""
    read_calls, scan_calls = [], []

    def fake_read_indicators(conn, symbol, timeframe, *, limit=None, include_forming=True, columns=None):
        read_calls.append(symbol)
        if symbol == "AAAUSDT":
            raise RuntimeError("relation does not exist")
        times = pd.date_range("2026-01-01", periods=3, freq="30min", tz="UTC")
        frame = {col: [1.0, 1.0, 1.0] for col in DET.DETECTOR_INDICATOR_COLUMNS if col != "open_time"}
        frame["open_time"] = times
        return pd.DataFrame(frame)

    def fake_analyze(conn, symbol, frame, live_price, cycle=None):
        scan_calls.append(symbol)
        return None

    conn = FakeConn()
    saved = {name: getattr(DET, name) for name in
             ("read_indicators", "analyze_fast", "analyze_vol", "get_db_connection", "get_live_prices_batch")}
    tmpdir = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        with open(os.path.join(tmpdir, "coins.json"), "w", encoding="utf-8") as f:
            json.dump(["AAAUSDT", "BBBUSDT"], f)
        os.chdir(tmpdir)
        DET.read_indicators = fake_read_indicators
        DET.analyze_fast = fake_analyze
        DET.analyze_vol = fake_analyze
        DET.get_db_connection = lambda: conn
        DET.get_live_prices_batch = lambda: {"AAAUSDT": 100.0, "BBBUSDT": 100.0}
        DET.run_detectors_for_timeframe("30m")
    finally:
        os.chdir(cwd)
        for name, value in saved.items():
            setattr(DET, name, value)

    assert read_calls == ["AAAUSDT", "BBBUSDT"]
    assert getattr(conn, "rollbacks", 0) >= 1, "failed read must roll back the aborted transaction"
    assert scan_calls == ["BBBUSDT", "BBBUSDT"], "the next coin must still be scanned"
    print("OK  wiring: failed indicator read rolls back and does not poison the cycle")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    fns = [
        test_spike_parity_buy_and_sell,
        test_spike_parity_spike_on_first_period_candle,
        test_spike_parity_empty_baseline,
        test_spike_parity_empty_period,
        test_spike_parity_boundary_candles,
        test_spike_parity_misaligned_bar_excluded_from_both_windows,
        test_spike_parity_randomised_sweep,
        test_active_trade_snapshot_parity,
        test_whole_coin_prefilter_condition,
        test_cooldown_snapshot_parity,
        test_cycle_memo_runs_once_per_key,
        test_note_signal_written_mirrors_db_state,
        test_fast_in_out_signal_parity_and_memo,
        test_5_percent_signal_parity,
        test_volume_indicator_signal_parity,
        test_support_resistance_parity_and_early_skip,
        test_main_channel_parity_and_early_skip,
        test_projection_superset_of_every_strategy_read,
        test_projection_subset_of_engine_ddl,
        test_run_detectors_wiring_and_prefilter,
        test_failed_indicator_read_rolls_back_and_isolates_the_coin,
    ]
    for fn in fns:
        fn()
    print(f"\nAlle {len(fns)} Detector-Scan-Optimierungs-Tests bestanden (T-2026-CU-9050-172).")
