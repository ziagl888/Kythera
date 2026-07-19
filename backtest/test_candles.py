# backtest/test_candles.py
"""
Unit tests for core/candles.py — the candle/indicator access API (C1 prep,
T-2026-CU-9050-034).

DB-free by construction: everything tested here is either pure arithmetic
(the closed-candle cutoff), identifier hygiene, or an argument-validation path
that raises before the connection is ever touched. The SQL text itself needs a
live libpq context to render (psycopg2 quote_ident) and is therefore verified on
the VPS via `python tools/candles_parity.py --self-check`.

Run with: pytest backtest/test_candles.py -v
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import candles as c


def _utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# ── The closed-candle cutoff (R1 core) ────────────────────────────────────────


def test_period_start_floors_intraday():
    now = _utc(2026, 7, 9, 14, 37, 12)
    assert c.period_start("1h", now) == _utc(2026, 7, 9, 14)
    assert c.period_start("5m", now) == _utc(2026, 7, 9, 14, 35)
    assert c.period_start("4h", now) == _utc(2026, 7, 9, 12)
    assert c.period_start("1d", now) == _utc(2026, 7, 9)


def test_period_start_weekly_anchors_on_monday():
    # 2026-07-09 is a Thursday. Epoch-floor without the Monday offset would
    # return the Thursday — Binance weekly klines open on Monday 00:00 UTC.
    for probe in (_utc(2026, 7, 9, 14), _utc(2026, 7, 6), _utc(2026, 7, 12, 23, 59, 59)):
        start = c.period_start("1w", probe)
        assert start == _utc(2026, 7, 6), probe
        assert start.weekday() == 0


def test_period_start_is_idempotent_on_a_boundary():
    boundary = _utc(2026, 7, 9, 14)
    assert c.period_start("1h", boundary) == boundary


def test_period_start_is_timezone_independent():
    utc_now = _utc(2026, 7, 9, 14, 37)
    tokyo = utc_now.astimezone(timezone(timedelta(hours=9)))
    assert c.period_start("1h", tokyo) == c.period_start("1h", utc_now)


def test_period_start_honours_the_grace_period():
    """The Python mirror must apply the same grace the SQL applies."""
    just_past_the_hour = _utc(2026, 7, 9, 14, 0, 1)
    assert c.period_start("1h", just_past_the_hour) == _utc(2026, 7, 9, 14)
    os.environ["KYTHERA_CANDLES_CLOSE_GRACE_SEC"] = "5"
    try:
        # With 5 s of grace the 14:00 candle is not yet considered forming-safe,
        # so the cutoff falls back into the 13:00 period.
        assert c.period_start("1h", just_past_the_hour) == _utc(2026, 7, 9, 13)
        assert c.period_start("1h", just_past_the_hour, grace_seconds=0.0) == _utc(2026, 7, 9, 14)
    finally:
        os.environ.pop("KYTHERA_CANDLES_CLOSE_GRACE_SEC", None)


def test_period_start_rejects_naive_datetime():
    with pytest.raises(ValueError):
        c.period_start("1h", datetime(2026, 7, 9, 14))


def test_last_closed_open_time_is_one_period_back():
    now = _utc(2026, 7, 9, 14, 37)
    assert c.last_closed_open_time("1h", now) == _utc(2026, 7, 9, 13)
    assert c.last_closed_open_time("1w", now) == _utc(2026, 6, 29)


def test_a_candle_is_closed_exactly_when_its_period_elapsed():
    """The contract every reader depends on: open_time < period_start ⇔ closed."""
    now = _utc(2026, 7, 9, 14, 0, 0)
    forming = _utc(2026, 7, 9, 14)  # opened at 14:00, closes at 15:00
    closed = _utc(2026, 7, 9, 13)
    cutoff = c.period_start("1h", now)
    assert not (forming < cutoff)
    assert closed < cutoff


# ── Identifier hygiene (P3.3) ─────────────────────────────────────────────────


def test_table_names():
    assert c.candles_table("BTCUSDT", "1h") == "BTCUSDT_1h"
    assert c.indicators_table("BTCUSDT", "1h") == "BTCUSDT_1h_indicators"


@pytest.mark.parametrize(
    "bad",
    ['BTC"; DROP TABLE x; --', "btcusdt", "BTC-USDT", "", "A", "BTC USDT", "BTCUSDT_1h", "X" * 25],
)
def test_validate_symbol_rejects_non_identifiers(bad):
    with pytest.raises(ValueError):
        c.validate_symbol(bad)


def test_validate_symbol_accepts_the_real_shapes():
    for sym in ("BTCUSDT", "ETHBTC", "BTCDOMUSDT", "1000PEPEUSDT", "XAUUSDT"):
        assert c.validate_symbol(sym) == sym


def test_validate_timeframe_rejects_unknown():
    with pytest.raises(ValueError):
        c.validate_timeframe("3m")


def test_whitelist_is_enforced_when_installed():
    c.set_symbol_whitelist(["BTCUSDT"])
    try:
        assert c.validate_symbol("BTCUSDT") == "BTCUSDT"
        with pytest.raises(ValueError, match="whitelist"):
            c.validate_symbol("ETHUSDT")
    finally:
        c.set_symbol_whitelist(None)
    # cleared again → regex-only
    assert c.validate_symbol("ETHUSDT") == "ETHUSDT"


def test_joined_read_accepts_an_as_of_bound():
    """15_ai_master reads the newest joined row before a floored ts — `end=` must exist."""
    import inspect

    sig = inspect.signature(c.read_candles_with_indicators)
    assert {"start", "end", "limit", "include_forming"} <= set(sig.parameters)


def test_projection_must_carry_open_time():
    with pytest.raises(ValueError, match="open_time"):
        c.read_candles(None, "BTCUSDT", "1h", columns=["close"])


def test_column_identifiers_are_validated():
    with pytest.raises(ValueError):
        c._columns_sql(["close; DROP TABLE x"])


# ── Timeframes stay in sync with core.config ──────────────────────────────────


def test_tf_seconds_matches_config_timeframes():
    """core/candles.py duplicates the timeframe list; this is the drift guard."""
    os.environ.setdefault("DB_PASSWORD", "unit-test")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")
    from core import config as kcfg

    assert set(kcfg.TIMEFRAMES) == set(c.TF_SECONDS)
    assert set(kcfg.INDICATOR_TIMEFRAMES) <= set(c.TF_SECONDS)


def test_timeframe_delta():
    assert c.timeframe_delta("4h") == timedelta(hours=4)
    assert c.timeframe_delta("1w") == timedelta(days=7)


# ── Argument validation happens before the connection is touched ──────────────


def test_reads_reject_bad_symbol_without_a_connection():
    for fn in (c.read_candles, c.read_indicators, c.read_candles_with_indicators):
        with pytest.raises(ValueError):
            fn(None, "bad-symbol", "1h")


def test_upsert_candles_rejects_non_bool_closed():
    """`closed` comes from the Binance kline flag; a truthy int must not slip through."""
    truthy_not_bool: Any = 1
    with pytest.raises(TypeError):
        c.upsert_candles(None, "BTCUSDT", "1h", [("BTCUSDT", _utc(2026, 7, 9), 1, 1, 1, 1, 1)], closed=truthy_not_bool)


def test_upsert_candles_on_empty_rows_is_a_noop():
    assert c.upsert_candles(None, "BTCUSDT", "1h", [], closed=True) == 0


def test_upsert_indicators_needs_key_columns():
    import pandas as pd

    with pytest.raises(ValueError):
        c.upsert_indicators(None, pd.DataFrame({"rsi": [1.0]}), "BTCUSDT", "1h")


def test_dual_write_flag_parsing(monkeypatch):
    """Phase-2 dual-write is OFF unless the flag is explicitly truthy. Read at call
    time (per-process override, no reimport). The behaviour itself is byte-tested
    against the live hypertables in test_candles_db_parity.py."""
    monkeypatch.delenv("KYTHERA_CANDLES_DUAL_WRITE", raising=False)
    assert c._dual_write_enabled() is False
    for on in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv("KYTHERA_CANDLES_DUAL_WRITE", on)
        assert c._dual_write_enabled() is True, on
    for off in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("KYTHERA_CANDLES_DUAL_WRITE", off)
        assert c._dual_write_enabled() is False, off


# ── Block-6 API-gap functions (T-2026-CU-9050-114) ────────────────────────────


def test_table_for_kind_selects_the_right_table():
    assert c._table_for_kind("BTCUSDT", "1h", "candles") == "BTCUSDT_1h"
    assert c._table_for_kind("BTCUSDT", "1h", "indicators") == "BTCUSDT_1h_indicators"
    with pytest.raises(ValueError):
        c._table_for_kind("BTCUSDT", "1h", "junk")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("BTCUSDT_1h", ("BTCUSDT", "1h", "candles")),
        ("BTCUSDT_1h_indicators", ("BTCUSDT", "1h", "indicators")),
        ("1000PEPEUSDT_5m", ("1000PEPEUSDT", "5m", "candles")),
        ("BTCDOMUSDT_15m", ("BTCDOMUSDT", "15m", "candles")),
        # non-candle tables never match the shape
        ("active_trades_master", None),
        ("telegram_outbox", None),
        ("pump_dump_events", None),
        ("regime_history", None),
        ("oi_5m", None),  # lowercase symbol fails the regex
        ("BTCUSDT_3m", None),  # 3m is not a known timeframe
        ("_1h", None),  # empty symbol
    ],
)
def test_parse_coin_table(name, expected):
    assert c._parse_coin_table(name) == expected


def test_latest_open_time_kind_is_validated_before_the_connection():
    # bad symbol → ValueError before conn is touched, for both kinds
    for kind in ("candles", "indicators"):
        with pytest.raises(ValueError):
            c.latest_open_time(None, "bad-symbol", "1h", kind=kind)
    # unknown kind → ValueError, conn never touched
    with pytest.raises(ValueError):
        c.latest_open_time(None, "BTCUSDT", "1h", kind="junk")


def test_delete_candles_before_validates_before_the_connection():
    aware = _utc(2026, 1, 1)
    # naive cutoff is rejected up front
    with pytest.raises(ValueError, match="timezone-aware"):
        c.delete_candles_before(None, "BTCUSDT", "1h", datetime(2026, 1, 1))
    # bad symbol / bad kind never reach the (None) connection
    with pytest.raises(ValueError):
        c.delete_candles_before(None, "bad-symbol", "1h", aware)
    with pytest.raises(ValueError):
        c.delete_candles_before(None, "BTCUSDT", "1h", aware, kind="junk")


def test_delete_indicators_from_validates_before_the_connection():
    with pytest.raises(ValueError, match="timezone-aware"):
        c.delete_indicators_from(None, "BTCUSDT", "1h", datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        c.delete_indicators_from(None, "bad-symbol", "1h", _utc(2026, 1, 1))


def test_list_coin_tables_validates_before_the_connection():
    with pytest.raises(ValueError):
        c.list_coin_tables(None, "3m")  # unknown timeframe
    with pytest.raises(ValueError):
        c.list_coin_tables(None, kind="junk")


# ── Backend switch (phase-4 read-cutover) ─────────────────────────────────────


def test_candle_source_resolves_known_backends(monkeypatch):
    # T-2026-CU-9050-172 hygiene: an operator .env further up the tree may
    # legitimately set the flag (any earlier test importing core.config runs
    # load_dotenv, whose upward search escapes a worktree). The DEFAULT must be
    # asserted on a clean env, not on ambient operator state.
    monkeypatch.delenv("KYTHERA_CANDLES_SOURCE", raising=False)
    assert c._candle_source() == "legacy"  # default
    for src in ("legacy", "hyper"):
        monkeypatch.setenv("KYTHERA_CANDLES_SOURCE", src)
        assert c._candle_source() == src


def test_write_primary_resolves_known_backends(monkeypatch):
    """KYTHERA_CANDLES_WRITE_PRIMARY selects the primary WRITE backend (AK7):
    default 'legacy', 'hyper' for the Phase-5 perf-trial, typo fails closed."""
    monkeypatch.delenv("KYTHERA_CANDLES_WRITE_PRIMARY", raising=False)
    assert c._write_primary() == "legacy"  # default
    for primary in ("legacy", "hyper"):
        monkeypatch.setenv("KYTHERA_CANDLES_WRITE_PRIMARY", primary)
        assert c._write_primary() == primary


def test_write_primary_unknown_is_rejected(monkeypatch):
    monkeypatch.setenv("KYTHERA_CANDLES_WRITE_PRIMARY", "banana")
    with pytest.raises(c.CandleSourceError):
        c._write_primary()


def test_unknown_backend_is_rejected_everywhere():
    """A typo'd backend must fail closed — on reads, writes and deletes alike —
    before the connection is ever touched."""
    os.environ["KYTHERA_CANDLES_SOURCE"] = "banana"
    row = ("BTCUSDT", _utc(2026, 1, 1), 1, 1, 1, 1, 1)
    try:
        for call in (
            lambda: c.read_candles(None, "BTCUSDT", "1h"),
            lambda: c.read_indicators(None, "BTCUSDT", "1h"),
            lambda: c.read_candles_with_indicators(None, "BTCUSDT", "1h"),
            lambda: c.latest_open_time(None, "BTCUSDT", "1h"),
            lambda: c.list_coin_tables(None),
            lambda: c.upsert_candles(None, "BTCUSDT", "1h", [row], closed=True),
            lambda: c.delete_candles_before(None, "BTCUSDT", "1h", _utc(2026, 1, 1)),
            lambda: c.delete_indicators_from(None, "BTCUSDT", "1h", _utc(2026, 1, 1)),
        ):
            with pytest.raises(c.CandleSourceError):
                call()
    finally:
        os.environ.pop("KYTHERA_CANDLES_SOURCE", None)


def test_hyper_backend_is_accepted_and_still_validates():
    """'hyper' is a real backend now — no CandleSourceError. Identifier hygiene
    still bites first, so a bad symbol/tf raises ValueError before the (None)
    connection is dereferenced (proves the hyper read path validates up front)."""
    os.environ["KYTHERA_CANDLES_SOURCE"] = "hyper"
    try:
        for fn in (c.read_candles, c.read_indicators, c.read_candles_with_indicators):
            with pytest.raises(ValueError):
                fn(None, "bad-symbol", "1h")
        with pytest.raises(ValueError):
            c.latest_open_time(None, "BTCUSDT", "3m")  # unknown tf
        with pytest.raises(ValueError):
            c.latest_open_time(None, "BTCUSDT", "1h", kind="junk")  # bad kind
        # the hyper scope predicate validates and carries the params in order
        pred, params = c._hyper_scope("BTCUSDT", "1h")
        assert params == ["BTCUSDT", "1h"]
        with pytest.raises(ValueError):
            c._hyper_scope("bad-symbol", "1h")
    finally:
        os.environ.pop("KYTHERA_CANDLES_SOURCE", None)


def test_grace_seconds_is_env_driven():
    assert c._grace_seconds() == 0.0
    os.environ["KYTHERA_CANDLES_CLOSE_GRACE_SEC"] = "2.5"
    try:
        assert c._grace_seconds() == 2.5
    finally:
        os.environ.pop("KYTHERA_CANDLES_CLOSE_GRACE_SEC", None)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
