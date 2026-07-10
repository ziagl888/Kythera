# backtest/test_shadow_prediction_cooldown.py
"""
Unit tests for the EPD2 shadow-prediction throttle (P1.41).

The 900s gate in 10_pump_dump_detector.py only ever resets `last_alert_time` in
the LIVE-trade branch. A coin that keeps predicting inside the shadow band
(0.25 <= p < 0.60) therefore never throttles and used to INSERT a row into
ml_predictions_master on every qualifying 10s tick — up to 8640 rows/day/symbol,
which the market tracker then counted as opened signals.

The throttle now lives in core.signal_post.log_prediction (4h dedup per
module/coin/direction), the same path bots 30-33 use. These tests pin both
halves: the detector routes through the helper, and the helper actually dedupes.

Run with: pytest backtest/test_shadow_prediction_cooldown.py -v
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import sys
import unittest.mock as mock
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.config raises on missing secrets; the build machine ships an empty .env.
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

# Import the real helper BEFORE the detector: _load_detector() patches sys.modules,
# and importing core.signal_post (-> core.charting -> numpy) afterwards re-enters
# numpy's extension modules and blows up with "cannot load module more than once".
from core.signal_post import log_prediction

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


class _Cur:
    """Cursor that records every executed statement."""

    def __init__(self, sink: list[str], fetch: object = None) -> None:
        self._sink = sink
        self._fetch = fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sink.append(" ".join(str(sql).split()))

    def fetchone(self):
        return self._fetch


class FakeConn:
    def __init__(self, fetch: object = None) -> None:
        self.statements: list[str] = []
        self.commits = 0
        self._fetch = fetch

    def cursor(self, *a, **kw):
        return _Cur(self.statements, self._fetch)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


class _ShadowModel:
    """predict_proba always lands inside the shadow band (0.25 <= p < 0.60)."""

    classes_ = [0, 1, 2]

    def predict_proba(self, _x):
        return [[0.10, 0.50, 0.40]]  # best_prob = 0.40 -> LONG, shadow band


def _seed_ticks(now: datetime.datetime, n: int = 400) -> list[dict]:
    """Flat price (no spike alert), constant small volume."""
    return [
        {
            "t": (now - datetime.timedelta(seconds=10 * (n - 1 - i))).isoformat(),
            "p": "100.0",
            "v10s": "1.0",
            "v10s_valid": True,
        }
        for i in range(n)
    ]


@pytest.fixture
def detector(monkeypatch):
    now = datetime.datetime.now(UTC)
    data = _seed_ticks(now)
    # Final bucket carries the volume spike: vol_ratio = 50/1 = 50 >= 5.0 gate.
    data[-1]["v10s"] = "50.0"

    monkeypatch.setitem(det.ONE_MINUTE_DATA, SYMBOL, deque(data))
    monkeypatch.setitem(
        det.PUMP_DUMP_STATE,
        SYMBOL,
        {
            "avg_volume": 1.0,
            # 360 samples of 1.0 -> avg_volume stays 1.0
            "volume_samples": deque([1.0] * 359, maxlen=360),
            # 1970 => the 900s gate is open, exactly the P1.41 condition
            "last_alert_time": datetime.datetime(1970, 1, 1, tzinfo=UTC),
        },
    )
    monkeypatch.setitem(
        det.PRICE_VOLUME_ALERT_STATE, SYMBOL, {"last_alert_time": datetime.datetime(1970, 1, 1, tzinfo=UTC)}
    )

    monkeypatch.setattr(det, "load_pump_model", lambda: _ShadowModel())
    monkeypatch.setattr(det, "get_indicators_at_time", lambda *a, **kw: {})
    monkeypatch.setattr(det, "send_outbox", lambda *a, **kw: None)
    monkeypatch.setattr(det, "check_round_levels", lambda *a, **kw: None)
    return det


# ── The detector routes the shadow write through the throttling helper ────────


def test_shadow_branch_calls_log_prediction_not_a_raw_insert(detector, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        det,
        "log_prediction",
        lambda conn, tag, sym, direction, entry, conf, posted: calls.append({"tag": tag, "sym": sym, "posted": posted}),
    )
    conn = FakeConn()

    det.process_coin_logics(conn, SYMBOL)

    assert len(calls) == 1, "shadow band did not route through log_prediction"
    assert calls[0]["posted"] is False, "shadow row must be logged as posted=False"
    assert calls[0]["sym"] == SYMBOL

    inserts = [s for s in conn.statements if "INSERT INTO ml_predictions_master" in s]
    assert inserts == [], f"shadow branch still issues a raw un-deduped INSERT: {inserts}"
    assert conn.commits >= 1, "caller must commit (hard rule 8: log_prediction does not)"


def test_shadow_branch_does_not_reset_the_live_trade_cooldown(detector, monkeypatch):
    """Resetting last_alert_time here would silence real signals for 900s."""
    monkeypatch.setattr(det, "log_prediction", lambda *a, **kw: None)
    before = det.PUMP_DUMP_STATE[SYMBOL]["last_alert_time"]

    det.process_coin_logics(FakeConn(), SYMBOL)

    assert det.PUMP_DUMP_STATE[SYMBOL]["last_alert_time"] == before


# ── The helper actually throttles ────────────────────────────────────────────


def test_log_prediction_inserts_when_no_recent_row():
    conn = FakeConn(fetch=None)  # dedup SELECT finds nothing
    log_prediction(conn, "EPD2", SYMBOL, "LONG", 100.0, 0.4, posted=False)

    inserts = [s for s in conn.statements if s.startswith("INSERT INTO ml_predictions_master")]
    assert len(inserts) == 1


def test_log_prediction_is_a_noop_inside_the_dedup_window():
    conn = FakeConn(fetch=(1,))  # dedup SELECT finds a recent row
    log_prediction(conn, "EPD2", SYMBOL, "LONG", 100.0, 0.4, posted=False)

    inserts = [s for s in conn.statements if s.startswith("INSERT INTO ml_predictions_master")]
    assert inserts == [], "a second shadow row inside the window must not be written"
    assert conn.commits == 0, "log_prediction must not commit (caller-commit contract)"
