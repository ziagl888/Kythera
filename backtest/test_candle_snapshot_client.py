# backtest/test_candle_snapshot_client.py
"""
End-to-end tests for the snapshot hook inside core/candles.py
(T-2026-KYT-9050-132).

These run a REAL service socket on an ephemeral localhost port — the request
goes through core.candles.read_* -> core.candle_snapshot client -> TCP ->
candle_snapshot_service.handle_line -> the store — so the wire format, the
freshness door and the fallback are exercised together rather than mocked apart.

DB-free: the frames are fixtures, and the fallback assertions pass ``conn=None``
on purpose. Any read that reaches the DB path dereferences that None and blows
up, which is precisely the signal the test wants — "did this take the DB path or
not" is then a fact, not an inference.

Run with: pytest backtest/test_candle_snapshot_client.py -v
"""

from __future__ import annotations

import os
import socketserver
import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import candle_snapshot_service as svc  # noqa: E402
from core import candle_snapshot as cs  # noqa: E402
from core import candles as c  # noqa: E402

CANDLE_COLS = ["symbol", "open_time", "open", "high", "low", "close", "volume"]
IND_COLS = ["symbol", "open_time", "close", "rsi_14", "atr_14"]

#: Anchored on the real clock: `serve_request` and the client's second freshness
#: check both use `datetime.now()`, so a fixture pinned to a fixed date would be
#: stale by construction.
_LATEST = c.last_closed_open_time("1h", datetime.now(timezone.utc))


def _candle_rows(n: int, *, end: datetime = _LATEST) -> list[tuple]:
    return [
        ("BTCUSDT", end - timedelta(hours=i), 100.0 + i, 101.5 + i, 98.5 + i, 100.25 + i, 1000.0 + i)
        for i in range(n - 1, -1, -1)
    ]


def _indicator_rows(n: int, *, end: datetime = _LATEST) -> list[tuple]:
    return [
        ("BTCUSDT", end - timedelta(hours=i), 100.25 + i, 55.5 - i * 0.1, 1.25 + i * 0.01) for i in range(n - 1, -1, -1)
    ]


def _store(n: int = 48, *, end: datetime = _LATEST) -> cs.SnapshotStore:
    store = cs.SnapshotStore()
    store.put("BTCUSDT", "1h", cs.KIND_CANDLES, pd.DataFrame(_candle_rows(n, end=end), columns=CANDLE_COLS))
    store.put("BTCUSDT", "1h", cs.KIND_INDICATORS, pd.DataFrame(_indicator_rows(n, end=end), columns=IND_COLS))
    return store


# ── A real service socket ─────────────────────────────────────────────────────


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return
        import json

        response = svc.handle_line(line.decode("utf-8").strip())
        self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def service(monkeypatch):
    """A live snapshot service on an ephemeral port, gate ON.

    Yields the SnapshotStore it serves so a test can make it stale, incomplete
    or empty and watch core/candles.py react.
    """
    store = _store()
    monkeypatch.setattr(svc, "STORE", store)
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(cs.GATE_ENV, "1")
    monkeypatch.setenv(cs.HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(cs.PORT_ENV, str(server.server_address[1]))
    # The legacy freshness probe would dereference the (deliberately absent)
    # connection before the snapshot is ever consulted; it is a different guard
    # with its own tests (backtest/test_candles.py).
    monkeypatch.setattr(c, "_legacy_freshness_ok", True)
    try:
        yield store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def gate_off(monkeypatch):
    monkeypatch.delenv(cs.GATE_ENV, raising=False)
    monkeypatch.setattr(c, "_legacy_freshness_ok", True)


# ── Gate off: nothing changes ─────────────────────────────────────────────────


def test_gate_is_off_by_default(monkeypatch):
    monkeypatch.delenv(cs.GATE_ENV, raising=False)
    assert c.snapshot_enabled() is False
    assert c._snapshot() is None


def test_gate_off_never_dials_the_service(gate_off, monkeypatch):
    """The no-op condition this change merges on: with the gate off the client
    is not even consulted, so the read takes the SQL path it always took (here:
    straight into the absent connection)."""
    called: list[str] = []
    monkeypatch.setattr(cs, "request", lambda payload: called.append("dialled"))
    for call in (
        lambda: c.read_candles(None, "BTCUSDT", "1h", limit=10),
        lambda: c.read_indicators(None, "BTCUSDT", "1h", limit=10),
        lambda: c.read_candles_with_indicators(None, "BTCUSDT", "1h", limit=10, indicator_columns=["rsi_14"]),
    ):
        with pytest.raises(AttributeError):  # None.cursor() — the DB path ran
            call()
    assert called == []


def test_gate_off_leaves_the_argument_validation_intact(gate_off):
    for fn in (c.read_candles, c.read_indicators, c.read_candles_with_indicators):
        with pytest.raises(ValueError):
            fn(None, "bad-symbol", "1h")
    with pytest.raises(ValueError, match="open_time"):
        c.read_candles(None, "BTCUSDT", "1h", columns=["close"])


# ── Gate on: served from RAM, without touching the connection ─────────────────


def test_read_candles_is_served_from_the_snapshot(service):
    df = c.read_candles(None, "BTCUSDT", "1h", limit=5)
    expected = pd.DataFrame(_candle_rows(48)[-5:], columns=CANDLE_COLS)
    pd.testing.assert_frame_equal(df, expected, check_dtype=True, check_exact=True)


def test_read_candles_honours_the_projection(service):
    df = c.read_candles(None, "BTCUSDT", "1h", limit=3, columns=("open_time", "close"))
    expected = pd.DataFrame(_candle_rows(48)[-3:], columns=CANDLE_COLS)[["open_time", "close"]]
    pd.testing.assert_frame_equal(df, expected.reset_index(drop=True), check_dtype=True, check_exact=True)


def test_read_indicators_is_served_from_the_snapshot(service):
    df = c.read_indicators(None, "BTCUSDT", "1h", limit=1, columns=("open_time", "close", "rsi_14"))
    expected = pd.DataFrame(_indicator_rows(48)[-1:], columns=IND_COLS)[["open_time", "close", "rsi_14"]]
    pd.testing.assert_frame_equal(df, expected.reset_index(drop=True), check_dtype=True, check_exact=True)


def test_joined_read_is_served_from_the_snapshot(service):
    """Bot 11's shape: limit=100 with the ~60-day chunk-exclusion hint below the
    frame floor, an explicit candle projection and an explicit indicator list."""
    df = c.read_candles_with_indicators(
        None,
        "BTCUSDT",
        "1h",
        limit=10,
        start=c.history_start("1h", 10),
        candle_columns=("open_time", "close", "volume"),
        indicator_columns=["rsi_14", "atr_14"],
    )
    candles = pd.DataFrame(_candle_rows(48)[-10:], columns=CANDLE_COLS)[["open_time", "close", "volume"]]
    indicators = pd.DataFrame(_indicator_rows(48)[-10:], columns=IND_COLS)[["rsi_14", "atr_14"]]
    expected = pd.concat([candles.reset_index(drop=True), indicators.reset_index(drop=True)], axis=1)
    pd.testing.assert_frame_equal(df, expected, check_dtype=True, check_exact=True)


def test_joined_read_resolves_the_indicator_columns_without_the_catalog(service):
    """`indicator_columns=None` normally costs an information_schema probe on the
    connection; served from the snapshot it must not need one at all."""
    df = c.read_candles_with_indicators(None, "BTCUSDT", "1h", limit=2)
    assert list(df.columns) == CANDLE_COLS + ["rsi_14", "atr_14"]


def test_an_as_of_read_is_served(service):
    """Bot 15's shape: the newest closed row at or before a floored instant."""
    end = _LATEST - timedelta(hours=3)
    df = c.read_candles_with_indicators(
        None, "BTCUSDT", "1h", limit=1, end=end, candle_columns=("open_time", "close"), indicator_columns=["rsi_14"]
    )
    assert len(df) == 1
    assert df["open_time"].iloc[0] == pd.Timestamp(end)


# ── Gate on, but the snapshot cannot answer: transparent fallback ─────────────


def test_forming_reads_always_take_the_db_path(service):
    """Monitors 5/8 and get_live_price read the forming candle; the store never
    holds it, and the client refuses locally without even dialling."""
    with pytest.raises(AttributeError):
        c.read_candles(None, "BTCUSDT", "1h", limit=1, include_forming=True)


def test_an_uncovered_symbol_takes_the_db_path(service):
    with pytest.raises(AttributeError):
        c.read_candles(None, "ETHUSDT", "1h", limit=1)


def test_an_uncovered_timeframe_takes_the_db_path(service):
    with pytest.raises(AttributeError):
        c.read_candles(None, "BTCUSDT", "4h", limit=1)


def test_an_uncovered_window_takes_the_db_path(service):
    """More rows asked for than the frame can prove it holds."""
    with pytest.raises(AttributeError):
        c.read_candles(None, "BTCUSDT", "1h", limit=5000)


def test_an_unknown_column_takes_the_db_path(service):
    with pytest.raises(AttributeError):
        c.read_candles(None, "BTCUSDT", "1h", limit=1, columns=("open_time", "not_a_column"))


def test_a_stale_store_takes_the_db_path(service):
    """The T-2026-KYT-9050-068 door: a frame behind the newest close is refused,
    never served. Both the service and the client check it."""
    stale = _store(end=_LATEST - timedelta(hours=4))
    service._frames = stale._frames
    service._fetched_at = stale._fetched_at
    with pytest.raises(AttributeError):
        c.read_candles(None, "BTCUSDT", "1h", limit=1)


def test_the_client_refuses_a_stale_frame_the_service_called_fresh(service, monkeypatch):
    """Second door: even if the service says ok, the client re-checks the
    watermark against its OWN clock — 'the backend said so' is exactly the
    assurance T-2026-KYT-9050-068 showed cannot stand alone."""
    real_request = cs.request

    def _lying_request(payload):
        response = real_request(payload)
        if response and response.get("ok"):
            response["frame_open_time"] = (_LATEST - timedelta(days=2)).isoformat()
        return response

    monkeypatch.setattr(cs, "request", _lying_request)
    with pytest.raises(AttributeError):
        c.read_candles(None, "BTCUSDT", "1h", limit=1)


def test_an_unreachable_service_takes_the_db_path(monkeypatch):
    """A dead or not-yet-started service is a fallback, never an error."""
    monkeypatch.setenv(cs.GATE_ENV, "1")
    monkeypatch.setenv(cs.HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(cs.PORT_ENV, "1")  # nothing listens there
    monkeypatch.setattr(c, "_legacy_freshness_ok", True)
    with pytest.raises(AttributeError):
        c.read_candles(None, "BTCUSDT", "1h", limit=1)


def test_a_missing_client_module_disables_the_path_permanently(monkeypatch):
    """An unimportable client degrades to the DB path with one warning — it must
    never take the fleet's reads down with it."""
    import core as core_pkg

    monkeypatch.setenv(cs.GATE_ENV, "1")
    monkeypatch.setattr(c, "_snapshot_module", None)
    monkeypatch.setattr(c, "_snapshot_unavailable", False)
    monkeypatch.delattr(core_pkg, "candle_snapshot", raising=False)
    monkeypatch.setitem(sys.modules, "core.candle_snapshot", None)  # import -> ImportError
    assert c._snapshot() is None
    assert c._snapshot_unavailable is True


# ── Identifier hygiene survives the new path ─────────────────────────────────


def test_a_bad_identifier_still_raises_before_anything_is_dialled(service):
    for fn in (c.read_candles, c.read_indicators, c.read_candles_with_indicators):
        with pytest.raises(ValueError):
            fn(None, "bad-symbol", "1h")
    with pytest.raises(ValueError):
        c.read_candles(None, "BTCUSDT", "3m", limit=1)


def test_an_empty_indicator_projection_still_raises(service):
    with pytest.raises(ValueError, match="no indicator columns"):
        c.read_candles_with_indicators(None, "BTCUSDT", "1h", limit=1, indicator_columns=["symbol", "close"])


# ── Health ────────────────────────────────────────────────────────────────────


def test_health_reports_the_store(service):
    response = svc.handle_line('{"cmd": "health"}')
    assert response["status"] == "ok"
    assert response["entries"] == 2
    assert response["memory_bytes"] > 0


def test_an_unknown_command_is_refused(service):
    assert svc.handle_line('{"cmd": "nope"}')["ok"] is False
    assert svc.handle_line("not json at all")["ok"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
