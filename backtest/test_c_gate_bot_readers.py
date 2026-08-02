# backtest/test_c_gate_bot_readers.py
"""
C-Gate follow-up (T-2026-KYT-9050-068): the two bots that bypassed core.candles,
and the staleness guard on the legacy backend.

WHAT WENT WRONG. `16_smc_forex_metals_bot` and `21_btc_smc_strategy` built raw
SQL against the per-coin tables (`SELECT ... FROM "{symbol}_{tf}"`) instead of
going through `core.candles`. That was survivable while the per-coin tables were
authoritative. Since the write-primary cutover on 2026-07-16 they are not
written any more, so both bots read a frame frozen at 2026-07-16 16:00 UTC and
went silent for 17 days without anyone noticing. Empty input produced no output
rather than wrong output — luck, not design.

THE TRAP THIS FILE PINS. Both bots dropped the newest row (`.iloc[:-1]`) because
their raw SELECT included the forming candle. `read_candles(include_forming=False)`
already excludes it, so KEEPING that drop would silently discard the newest
CLOSED candle and delay every signal by one bar. The drop is therefore removed
on the DB path — and for bot 16 it has to SURVIVE on the yfinance path, which
still delivers a forming candle. One data path changed, the other must not.

Run with: pytest backtest/test_c_gate_bot_readers.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.config fails hard on missing secrets; the build machine ships an empty .env.
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from core import candles as candles_mod  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── helpers ──────────────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, result, raises=None):
        self._result = result
        self._raises = raises

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *a, **kw):
        if self._raises is not None:
            raise self._raises

    def fetchone(self):
        return self._result


class _FakeConn:
    def __init__(self, result, raises=None):
        self._result = result
        self._raises = raises

    def cursor(self):
        return _FakeCursor(self._result, self._raises)


@pytest.fixture(autouse=True)
def _isolate_guard_state():
    """The guard caches its verdict in module globals — one probe per process is
    the whole point. Without a teardown this file leaks that cache into every
    later test in the same pytest process: running it together with
    test_candles_db_parity.py turned 25 skips into 12 failures, because the
    parity suite then read the (dead) legacy backend unguarded. Found by running
    the files together, not separately.
    """
    before = (candles_mod._legacy_freshness_ok, candles_mod._legacy_staleness_error)
    yield
    candles_mod._legacy_freshness_ok, candles_mod._legacy_staleness_error = before


def _reset_guard():
    candles_mod._legacy_freshness_ok = None
    candles_mod._legacy_staleness_error = None


def _frame(n=5, start=None):
    """n closed candles, ascending, hourly."""
    start = start or datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc)
    return pd.DataFrame(
        {
            "open_time": [start + timedelta(hours=i) for i in range(n)],
            "open": [1.0 + i for i in range(n)],
            "high": [1.5 + i for i in range(n)],
            "low": [0.5 + i for i in range(n)],
            "close": [1.2 + i for i in range(n)],
            "volume": [100.0 + i for i in range(n)],
        }
    )


# ── the staleness guard ──────────────────────────────────────────────────────


def test_guard_passes_on_fresh_legacy_data():
    _reset_guard()
    fresh = datetime.now(timezone.utc) - timedelta(minutes=5)
    candles_mod._assert_legacy_not_stale(_FakeConn((fresh,)))
    assert candles_mod._legacy_freshness_ok is True


def test_guard_raises_on_frozen_legacy_data():
    """The real 2026-07-16 shape: a backend that answers, with dead data."""
    _reset_guard()
    frozen = datetime(2026, 7, 16, 16, 0, tzinfo=timezone.utc)
    with pytest.raises(candles_mod.CandleSourceError) as err:
        candles_mod._assert_legacy_not_stale(_FakeConn((frozen,)))
    msg = str(err.value)
    # The message has to say WHY, or the next operator flips the flag again.
    assert "not a rollback" in msg.lower() or "NOT a rollback" in msg
    assert "hyper" in msg


def test_a_stale_verdict_keeps_raising_on_every_later_read():
    """Caching the negative and then returning silently would BE the bug: first
    read blocked, all following reads served frozen data."""
    _reset_guard()
    frozen = datetime(2026, 7, 16, 16, 0, tzinfo=timezone.utc)
    conn = _FakeConn((frozen,))
    with pytest.raises(candles_mod.CandleSourceError):
        candles_mod._assert_legacy_not_stale(conn)
    for _ in range(3):
        with pytest.raises(candles_mod.CandleSourceError):
            candles_mod._assert_legacy_not_stale(conn)


def test_guard_is_cached_and_probes_once_per_process():
    _reset_guard()
    fresh = datetime.now(timezone.utc) - timedelta(minutes=1)
    conn = mock.MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (fresh,)
    candles_mod._assert_legacy_not_stale(conn)
    candles_mod._assert_legacy_not_stale(conn)
    candles_mod._assert_legacy_not_stale(conn)
    assert conn.cursor.call_count == 1


def test_unmeasurable_probe_stands_down_instead_of_asserting_staleness():
    """A failed measurement is not a negative finding — it must not become a verdict."""
    _reset_guard()
    conn = _FakeConn(None, raises=RuntimeError("relation does not exist"))
    candles_mod._assert_legacy_not_stale(conn)  # must not raise
    assert candles_mod._legacy_freshness_ok is True


def test_empty_table_stands_down():
    _reset_guard()
    candles_mod._assert_legacy_not_stale(_FakeConn((None,)))
    assert candles_mod._legacy_freshness_ok is True


def test_naive_timestamps_are_compared_in_the_same_domain():
    """Per-coin open_time is naive UTC under the R3 pool session — a tz-mismatch
    here would raise TypeError, not a verdict."""
    _reset_guard()
    fresh_naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    candles_mod._assert_legacy_not_stale(_FakeConn((fresh_naive,)))
    assert candles_mod._legacy_freshness_ok is True


def test_threshold_is_configurable():
    _reset_guard()
    old = datetime.now(timezone.utc) - timedelta(hours=10)
    with mock.patch.dict(os.environ, {candles_mod._LEGACY_MAX_AGE_ENV: "1200"}):
        candles_mod._assert_legacy_not_stale(_FakeConn((old,)))  # 10h < 20h limit
    assert candles_mod._legacy_freshness_ok is True


def test_bad_threshold_falls_back_to_the_default():
    with mock.patch.dict(os.environ, {candles_mod._LEGACY_MAX_AGE_ENV: "not-a-number"}):
        assert candles_mod._legacy_max_age_minutes() == candles_mod._LEGACY_MAX_AGE_DEFAULT_MIN
    with mock.patch.dict(os.environ, {candles_mod._LEGACY_MAX_AGE_ENV: "0"}):
        assert candles_mod._legacy_max_age_minutes() == candles_mod._LEGACY_MAX_AGE_DEFAULT_MIN


def test_hyper_reads_never_pay_for_the_guard():
    """The guard is legacy-only: a hyper-backed fleet must not run the probe."""
    _reset_guard()
    conn = mock.MagicMock()
    with mock.patch.dict(os.environ, {"KYTHERA_CANDLES_SOURCE": "hyper"}), mock.patch.object(
        candles_mod, "_fetch_df", return_value=_frame()
    ):
        candles_mod.read_candles(conn, "BTCUSDT", "1h", limit=10)
    assert candles_mod._legacy_freshness_ok is None


# ── bot 21: BTC SMC ──────────────────────────────────────────────────────────


def _load_bot21():
    spec = importlib.util.spec_from_file_location("bot21", os.path.join(REPO, "21_btc_smc_strategy.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bot21"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_bot21_reads_closed_candles_through_core_candles():
    mod = _load_bot21()
    captured = {}

    def fake_read(conn, symbol, tf, **kw):
        captured.update(kw)
        captured["symbol"] = symbol
        captured["tf"] = tf
        return _frame(5)[["open_time", "open", "high", "low", "close"]]

    with mock.patch.object(mod, "read_candles", side_effect=fake_read), mock.patch.object(
        mod, "get_db_connection", return_value=mock.MagicMock()
    ):
        df = mod.fetch_db_data()

    assert captured["symbol"] == "BTCUSDT"
    assert captured["tf"] == "1h"
    assert captured["include_forming"] is False, "signal generator must never see the forming candle"
    assert captured["limit"] == 500, "the old raw SQL took the newest 500"
    # THE REGRESSION THIS GUARDS: no second drop of the newest closed candle.
    assert len(df) == 5, "read_candles already excludes the forming candle — do not drop again"


def test_bot21_keeps_ascending_order_and_float_dtypes():
    mod = _load_bot21()
    with mock.patch.object(
        mod, "read_candles", return_value=_frame(4)[["open_time", "open", "high", "low", "close"]]
    ), mock.patch.object(mod, "get_db_connection", return_value=mock.MagicMock()):
        df = mod.fetch_db_data()
    assert df["open_time"].is_monotonic_increasing, "pivots/indices assume oldest-first"
    for col in ("open", "high", "low", "close"):
        assert df[col].dtype == float


def test_bot21_closes_its_connection_even_when_the_read_raises():
    mod = _load_bot21()
    conn = mock.MagicMock()
    with mock.patch.object(mod, "read_candles", side_effect=RuntimeError("boom")), mock.patch.object(
        mod, "get_db_connection", return_value=conn
    ):
        df = mod.fetch_db_data()
    assert df.empty
    conn.close.assert_called_once()


# ── bot 16: SMC Forex/Metals ─────────────────────────────────────────────────


def _load_bot16():
    for name in ("matplotlib", "matplotlib.pyplot", "mplfinance", "yfinance"):
        sys.modules.setdefault(name, mock.MagicMock())
    spec = importlib.util.spec_from_file_location("bot16", os.path.join(REPO, "16_smc_forex_metals_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bot16"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_bot16_db_path_reads_closed_candles_through_core_candles():
    mod = _load_bot16()
    captured = {}

    def fake_read(conn, symbol, tf, **kw):
        captured.update(kw)
        return _frame(6)

    with mock.patch.object(mod, "read_candles", side_effect=fake_read):
        df = mod.fetch_db_data(mock.MagicMock(), "BTCUSDT", "1h")

    assert captured["include_forming"] is False
    assert captured["limit"] == 300, "the old raw SQL took the newest 300"
    assert len(df) == 6, "no second drop of the newest closed candle"


def test_bot16_metals_group_uses_the_database_and_forex_does_not():
    """The split this whole task hinges on: only METALS reads the DB. FOREX comes
    from yfinance and was never affected by the C-Gate — an 'ingester' for it
    would have been work for a problem that does not exist."""
    mod = _load_bot16()
    assert mod.MARKETS["METALS"]["source"] == "database"
    assert mod.MARKETS["FOREX"]["source"] == "yfinance"
    # METALS pairs are Binance symbols and therefore live in the hypertable.
    for pair in mod.MARKETS["METALS"]["pairs"]:
        assert pair.endswith("USDT"), f"{pair} is not a Binance symbol — it would have no hyper rows"
    # FOREX tickers are Yahoo-style and must never be looked up in the DB.
    assert any("=" in p for p in mod.MARKETS["FOREX"]["pairs"])
