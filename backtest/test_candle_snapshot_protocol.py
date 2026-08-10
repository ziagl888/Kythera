# backtest/test_candle_snapshot_protocol.py
"""
Unit tests for the shared-candle-snapshot wire format and slicing
(T-2026-KYT-9050-132).

DB-free by construction. The oracle for "what the DB would have returned" is
built in this file the way core.candles._fetch_df builds it —
``pd.DataFrame(rows, columns=cols)`` over the rows a `SELECT ... WHERE
open_time BETWEEN ... ORDER BY open_time DESC LIMIT n` would yield, re-ordered
ASC. That is the contract core/candles.py documents (contract 1 + the
``_windowed_select`` docstring); the SQL text itself needs a live libpq context
and is verified on the VPS, exactly as backtest/test_candles.py notes for the
same reason.

Run with: pytest backtest/test_candle_snapshot_protocol.py -v
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from core import candle_snapshot as cs  # noqa: E402

_ANCHOR = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)

CANDLE_COLS = ["symbol", "open_time", "open", "high", "low", "close", "volume"]
IND_COLS = ["symbol", "open_time", "close", "rsi_14", "atr_14", "regime_label"]


# ── Fixtures: the frames a DB read would have produced ────────────────────────


def _candle_rows(n: int, *, end: datetime = _ANCHOR, symbol: str = "BTCUSDT") -> list[tuple]:
    """`n` hourly candle rows ASC, newest at `end` — the psycopg2 row shape of
    the per-coin table (symbol, open_time, open, high, low, close, volume)."""
    rows = []
    for i in range(n - 1, -1, -1):
        ot = end - timedelta(hours=i)
        base = 100.0 + i
        rows.append((symbol, ot, base, base + 1.5, base - 1.5, base + 0.25, 1000.0 + i))
    return rows


def _indicator_rows(n: int, *, end: datetime = _ANCHOR, symbol: str = "BTCUSDT") -> list[tuple]:
    rows = []
    for i in range(n - 1, -1, -1):
        ot = end - timedelta(hours=i)
        rows.append((symbol, ot, 100.25 + i, 55.5 - i * 0.1, 1.25 + i * 0.01, "TREND" if i % 3 else None))
    return rows


def _frame(rows: list[tuple], cols: list[str]) -> pd.DataFrame:
    """The exact construction core.candles._fetch_df uses."""
    return pd.DataFrame(rows, columns=cols)


def _sql_oracle(
    rows: list[tuple],
    cols: list[str],
    *,
    limit: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """What ``_windowed_select`` returns: window, DESC, LIMIT, then ASC."""
    ot = cols.index("open_time")
    kept = [r for r in rows if (start is None or r[ot] >= start) and (end is None or r[ot] <= end)]
    desc = list(reversed(kept))
    if limit is not None:
        desc = desc[:limit]
    return _frame(list(reversed(desc)), cols)


def _store(candle_n: int = 48, indicator_n: int = 48, *, end: datetime = _ANCHOR) -> cs.SnapshotStore:
    store = cs.SnapshotStore()
    store.put("BTCUSDT", "1h", cs.KIND_CANDLES, _frame(_candle_rows(candle_n, end=end), CANDLE_COLS))
    store.put("BTCUSDT", "1h", cs.KIND_INDICATORS, _frame(_indicator_rows(indicator_n, end=end), IND_COLS))
    return store


#: `now` for a store whose newest candle opened at `_ANCHOR`: half an hour into
#: the next period, so `_ANCHOR` IS the newest closed candle.
_NOW = _ANCHOR + timedelta(hours=1, minutes=30)


# ── Wire format: values survive the round trip unchanged ──────────────────────


def test_encode_decode_rebuilds_the_identical_frame():
    """The core parity claim: the snapshot frame is byte-identical to the frame
    the DB path would have built from the same rows."""
    direct = _frame(_candle_rows(24), CANDLE_COLS)
    rebuilt = cs.decode_frame(cs.encode_frame(direct))
    pd.testing.assert_frame_equal(direct, rebuilt, check_dtype=True, check_exact=True)


def test_round_trip_preserves_text_and_null_columns():
    direct = _frame(_indicator_rows(12), IND_COLS)
    rebuilt = cs.decode_frame(cs.encode_frame(direct))
    pd.testing.assert_frame_equal(direct, rebuilt, check_dtype=True, check_exact=True)
    assert rebuilt["regime_label"].isna().any()  # the NULL rows really are in the fixture


def test_round_trip_preserves_nan_and_microseconds():
    ot = _ANCHOR.replace(microsecond=123456)
    direct = _frame([("BTCUSDT", ot, float("nan"))], ["symbol", "open_time", "close"])
    rebuilt = cs.decode_frame(cs.encode_frame(direct))
    assert rebuilt["open_time"].iloc[0] == pd.Timestamp(ot)
    assert pd.isna(rebuilt["close"].iloc[0])
    pd.testing.assert_frame_equal(direct, rebuilt, check_dtype=True, check_exact=True)


def test_empty_frame_round_trips_with_its_columns():
    direct = _frame([], CANDLE_COLS)
    rebuilt = cs.decode_frame(cs.encode_frame(direct))
    assert list(rebuilt.columns) == CANDLE_COLS
    assert rebuilt.empty


def test_unencodable_values_are_refused_not_coerced():
    """A Decimal would survive JSON only as a lossy float. Refusing costs a
    cache hit; coercing would hand a trading bot a different number."""
    from decimal import Decimal

    direct = _frame([("BTCUSDT", _ANCHOR, Decimal("1.10"))], ["symbol", "open_time", "close"])
    with pytest.raises(cs.SnapshotEncodeError):
        cs.encode_frame(direct)


def test_naive_datetimes_round_trip_as_naive():
    naive = _ANCHOR.replace(tzinfo=None)
    direct = _frame([("BTCUSDT", naive, 1.0)], ["symbol", "open_time", "close"])
    rebuilt = cs.decode_frame(cs.encode_frame(direct))
    assert rebuilt["open_time"].iloc[0].tzinfo is None
    pd.testing.assert_frame_equal(direct, rebuilt, check_dtype=True, check_exact=True)


# ── Slicing reproduces the SQL window/limit/order semantics ───────────────────


@pytest.mark.parametrize("limit", [1, 5, 24, 48])
def test_limit_selects_the_newest_rows_ascending(limit):
    rows = _candle_rows(48)
    sliced, reason = cs.select_slice(_frame(rows, CANDLE_COLS), limit=limit, start=None, end=None)
    assert reason == ""
    pd.testing.assert_frame_equal(sliced, _sql_oracle(rows, CANDLE_COLS, limit=limit), check_exact=True)


def test_start_and_end_bounds_match_the_sql_window():
    rows = _candle_rows(48)
    start = _ANCHOR - timedelta(hours=20)
    end = _ANCHOR - timedelta(hours=5)
    sliced, reason = cs.select_slice(_frame(rows, CANDLE_COLS), limit=10, start=start, end=end)
    assert reason == ""
    pd.testing.assert_frame_equal(
        sliced, _sql_oracle(rows, CANDLE_COLS, limit=10, start=start, end=end), check_exact=True
    )


def test_limit_zero_returns_no_rows():
    """`LIMIT 0` selects nothing; `iloc[-0:]` would hand back the whole frame."""
    sliced, reason = cs.select_slice(_frame(_candle_rows(10), CANDLE_COLS), limit=0, start=None, end=None)
    assert reason == ""
    assert sliced is not None and sliced.empty


def test_negative_limit_is_handed_to_the_db():
    sliced, reason = cs.select_slice(_frame(_candle_rows(10), CANDLE_COLS), limit=-1, start=None, end=None)
    assert sliced is None and reason == cs.REASON_BAD_REQUEST


# ── Coverage: the frame must PROVE it holds the answer ────────────────────────


def test_unbounded_read_is_never_served_from_a_truncated_frame():
    """No limit and no start: rows could exist below the frame's floor, and the
    frame cannot know. Straight to the DB."""
    sliced, reason = cs.select_slice(_frame(_candle_rows(48), CANDLE_COLS), limit=None, start=None, end=None)
    assert sliced is None and reason == cs.REASON_WINDOW


def test_limit_larger_than_the_window_falls_back():
    """48 rows in the frame, 100 asked for, no floor to lean on: the DB may hold
    older rows that belong in the answer, so the frame must not answer."""
    sliced, reason = cs.select_slice(_frame(_candle_rows(48), CANDLE_COLS), limit=100, start=None, end=None)
    assert sliced is None and reason == cs.REASON_WINDOW


def test_a_start_inside_the_frame_covers_an_unlimited_read():
    """Bots 13/34 read `start = now - 95d` without a limit. Served only while
    the frame reaches back past that floor."""
    rows = _candle_rows(48)
    df = _frame(rows, CANDLE_COLS)
    inside = _ANCHOR - timedelta(hours=40)
    sliced, reason = cs.select_slice(df, limit=None, start=inside, end=None)
    assert reason == ""
    pd.testing.assert_frame_equal(sliced, _sql_oracle(rows, CANDLE_COLS, start=inside), check_exact=True)

    below = _ANCHOR - timedelta(hours=500)
    sliced, reason = cs.select_slice(df, limit=None, start=below, end=None)
    assert sliced is None and reason == cs.REASON_WINDOW


def test_enough_rows_in_the_window_covers_a_start_below_the_frame():
    """The bots' chunk-exclusion hint (`start=history_start(...)`, ~60 d) sits
    far below the frame floor — but with `limit` rows inside the window the
    older rows can never enter the answer, so the read is still exact."""
    rows = _candle_rows(48)
    below = _ANCHOR - timedelta(days=60)
    sliced, reason = cs.select_slice(_frame(rows, CANDLE_COLS), limit=10, start=below, end=None)
    assert reason == ""
    pd.testing.assert_frame_equal(sliced, _sql_oracle(rows, CANDLE_COLS, limit=10, start=below), check_exact=True)


def test_empty_frame_is_never_served():
    sliced, reason = cs.select_slice(_frame([], CANDLE_COLS), limit=1, start=None, end=None)
    assert sliced is None and reason == cs.REASON_EMPTY_FRAME


# ── Staleness ─────────────────────────────────────────────────────────────────


def test_frame_is_fresh_when_it_holds_the_last_closed_candle():
    assert cs.frame_is_fresh(_ANCHOR, "1h", end=None, now=_ANCHOR + timedelta(minutes=90))


def test_frame_one_candle_behind_is_stale():
    """The whole T-2026-KYT-9050-068 lesson in one assertion."""
    assert not cs.frame_is_fresh(_ANCHOR - timedelta(hours=1), "1h", end=None, now=_ANCHOR + timedelta(minutes=90))


def test_as_of_reads_do_not_need_a_fresh_frame():
    """A read bounded at an instant the frame already covers is answerable
    however old the frame is."""
    end = _ANCHOR - timedelta(hours=5)
    assert cs.frame_is_fresh(_ANCHOR, "1h", end=end, now=_ANCHOR + timedelta(days=3))


def test_freshness_handles_a_naive_frame_watermark():
    naive = _ANCHOR.replace(tzinfo=None)
    assert cs.frame_is_fresh(naive, "1h", end=None, now=_ANCHOR + timedelta(minutes=90))
    assert not cs.frame_is_fresh(naive - timedelta(hours=2), "1h", end=None, now=_ANCHOR + timedelta(minutes=90))


# ── serve_request: the whole service answer ───────────────────────────────────


def _get(store, now=_NOW, **req):
    return cs.serve_request(store, {"cmd": "get", "symbol": "BTCUSDT", "tf": "1h", **req}, now=now)


def test_serve_candles_matches_the_db_frame():
    rows = _candle_rows(48)
    response = _get(_store(), kind=cs.KIND_CANDLES, limit=10)
    assert response["ok"] is True
    pd.testing.assert_frame_equal(
        cs.decode_frame(response), _sql_oracle(rows, CANDLE_COLS, limit=10), check_dtype=True, check_exact=True
    )


def test_serve_applies_the_column_projection():
    response = _get(_store(), kind=cs.KIND_CANDLES, limit=3, columns=["open_time", "close", "volume"])
    assert response["ok"] is True
    df = cs.decode_frame(response)
    assert list(df.columns) == ["open_time", "close", "volume"]
    expected = _sql_oracle(_candle_rows(48), CANDLE_COLS, limit=3)[["open_time", "close", "volume"]]
    pd.testing.assert_frame_equal(df, expected.reset_index(drop=True), check_dtype=True, check_exact=True)


def test_serve_rejects_an_unknown_column():
    response = _get(_store(), kind=cs.KIND_CANDLES, limit=3, columns=["open_time", "not_a_column"])
    assert response["ok"] is False and response["reason"] == cs.REASON_UNKNOWN_COLUMN


def test_serve_rejects_a_forming_read():
    """Monitors 5/8 and get_live_price want the forming candle; the store never
    holds it (contract 2 of core/candles.py)."""
    response = _get(_store(), kind=cs.KIND_CANDLES, limit=1, include_forming=True)
    assert response["ok"] is False and response["reason"] == cs.REASON_FORMING


def test_serve_rejects_an_uncovered_symbol():
    response = cs.serve_request(
        cs.SnapshotStore(), {"cmd": "get", "kind": cs.KIND_CANDLES, "symbol": "ETHUSDT", "tf": "1h"}, now=_NOW
    )
    assert response["ok"] is False and response["reason"] == cs.REASON_NOT_COVERED


def test_serve_rejects_a_stale_store():
    store = _store(end=_ANCHOR - timedelta(hours=6))
    response = _get(store, kind=cs.KIND_CANDLES, limit=1)
    assert response["ok"] is False and response["reason"] == cs.REASON_STALE


def test_serve_rejects_a_bad_identifier_without_raising():
    response = cs.serve_request(
        _store(), {"cmd": "get", "kind": cs.KIND_CANDLES, "symbol": "bad-symbol", "tf": "1h"}, now=_NOW
    )
    assert response["ok"] is False and response["reason"] == cs.REASON_BAD_REQUEST


def test_serve_rejects_an_unknown_kind():
    response = _get(_store(), kind="junk")
    assert response["ok"] is False and response["reason"] == cs.REASON_BAD_REQUEST


def test_serve_reports_the_frame_watermark():
    response = _get(_store(), kind=cs.KIND_CANDLES, limit=1)
    assert datetime.fromisoformat(response["frame_open_time"]) == _ANCHOR


# ── The joined read ───────────────────────────────────────────────────────────


def _joined_oracle(limit=None, start=None, end=None, icols=None):
    """candles LEFT JOIN indicators ON open_time, window/limit applied to the
    candle side — the legacy SQL in read_candles_with_indicators."""
    crows = _candle_rows(48)
    irows = {r[1]: r for r in _indicator_rows(48)}
    icols = icols or [c for c in IND_COLS if c not in cs.JOIN_DROPPED_INDICATOR_COLUMNS]
    idx = [IND_COLS.index(c) for c in icols]
    windowed = _sql_oracle(crows, CANDLE_COLS, limit=limit, start=start, end=end)
    joined_rows = [
        tuple(row) + tuple(irows[row[CANDLE_COLS.index("open_time")]][i] for i in idx)
        for row in windowed.itertuples(index=False, name=None)
    ]
    return _frame(joined_rows, CANDLE_COLS + icols)


def test_joined_read_matches_the_left_join_frame():
    response = _get(_store(), kind=cs.KIND_JOINED, limit=12)
    assert response["ok"] is True
    pd.testing.assert_frame_equal(
        cs.decode_frame(response), _joined_oracle(limit=12), check_dtype=True, check_exact=True
    )


def test_joined_read_honours_both_projections():
    icols = ["rsi_14", "atr_14"]
    response = _get(
        _store(),
        kind=cs.KIND_JOINED,
        limit=6,
        candle_columns=["open_time", "close", "volume"],
        indicator_columns=icols,
    )
    assert response["ok"] is True
    df = cs.decode_frame(response)
    assert list(df.columns) == ["open_time", "close", "volume", "rsi_14", "atr_14"]
    expected = _joined_oracle(limit=6, icols=icols)[["open_time", "close", "volume", *icols]]
    pd.testing.assert_frame_equal(df, expected.reset_index(drop=True), check_dtype=True, check_exact=True)


def test_joined_read_resolves_none_indicator_columns_like_the_catalog():
    """`indicator_columns=None` means "every indicator column minus the three
    the candle side owns" — the shape read_candles_with_indicators builds from
    information_schema, without paying for the probe."""
    response = _get(_store(), kind=cs.KIND_JOINED, limit=2)
    df = cs.decode_frame(response)
    assert list(df.columns) == CANDLE_COLS + ["rsi_14", "atr_14", "regime_label"]


def test_joined_read_falls_back_when_an_indicator_row_is_missing():
    """A candle without its indicator row makes the SQL emit a row of NULLs.
    Reproducing NULL-fill in pandas is not worth the risk — hand it to the DB."""
    store = _store()
    short = _frame(_indicator_rows(48)[1:], IND_COLS)  # oldest indicator row dropped
    store.put("BTCUSDT", "1h", cs.KIND_INDICATORS, short)
    response = _get(store, kind=cs.KIND_JOINED, limit=48)
    assert response["ok"] is False and response["reason"] == cs.REASON_INDICATOR_GAP
    # the newer window is unaffected
    assert _get(store, kind=cs.KIND_JOINED, limit=10)["ok"] is True


@pytest.mark.parametrize("kind", [cs.KIND_CANDLES, cs.KIND_INDICATORS, cs.KIND_JOINED])
def test_an_empty_frame_is_refused_not_crashed(kind):
    """An entry read back empty (new listing, ingestion gap) has no watermark to
    judge; refusing before the freshness check keeps it a fallback, not an
    exception."""
    store = _store()
    store.put("BTCUSDT", "1h", cs.KIND_CANDLES, _frame([], CANDLE_COLS))
    store.put("BTCUSDT", "1h", cs.KIND_INDICATORS, _frame([], IND_COLS))
    response = _get(store, kind=kind, limit=1)
    assert response["ok"] is False and response["reason"] == cs.REASON_EMPTY_FRAME


def test_joined_read_needs_both_frames():
    store = cs.SnapshotStore()
    store.put("BTCUSDT", "1h", cs.KIND_CANDLES, _frame(_candle_rows(10), CANDLE_COLS))
    response = _get(store, kind=cs.KIND_JOINED, limit=1)
    assert response["ok"] is False and response["reason"] == cs.REASON_NOT_COVERED


# ── Store bookkeeping ─────────────────────────────────────────────────────────


def test_store_reports_its_watermark_and_size():
    store = _store(candle_n=10, indicator_n=10)
    assert store.max_open_time("BTCUSDT", "1h", cs.KIND_CANDLES) == pd.Timestamp(_ANCHOR)
    assert store.max_open_time("BTCUSDT", "1h", "nope") is None
    stats = store.stats()
    assert stats["entries"] == 2
    assert stats["rows"] == 20
    assert stats["memory_bytes"] > 0


def test_gate_is_off_by_default(monkeypatch):
    monkeypatch.delenv(cs.GATE_ENV, raising=False)
    assert cs.snapshot_enabled() is False
    for on in ("1", "true", "TRUE", "yes", " On "):
        monkeypatch.setenv(cs.GATE_ENV, on)
        assert cs.snapshot_enabled() is True, on
    for off in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv(cs.GATE_ENV, off)
        assert cs.snapshot_enabled() is False, off


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
