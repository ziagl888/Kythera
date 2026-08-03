# backtest/test_shadow_test_channel.py
"""DB-free tests for the optional shadow visibility echo (T-2026-CU-9050-150).

Security invariants:
  1. Default (CH_SHADOW_TEST=0): post_shadow_ai_signal writes ONLY ai_signals,
     NO telegram_outbox — exactly the previous shadow behaviour (backward-compat).
  2. Set (CH_SHADOW_TEST=<id>): additionally EXACTLY ONE telegram_outbox row,
     to THIS channel (never another), with a NOT-Cornix-parseable
     preview (clearly marked SHADOW, no Cornix trigger keywords).
  3. The echo runs in the same open transaction — no commit here (rule 8).

Run: pytest backtest/test_shadow_test_channel.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import core.signal_post as sp  # noqa: E402

TEST_CH = -1009999999999  # Platzhalter-Test-Channel (kein echtes Secret im Code)


class _Cur:
    def __init__(self, sink: list[tuple]) -> None:
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sink.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return None  # has_open_ai_signal -> False, log_prediction-Dedup -> proceed


class FakeConn:
    def __init__(self) -> None:
        self.ops: list[tuple] = []
        self.commits = 0

    def cursor(self, *a, **kw):
        return _Cur(self.ops)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _emit(monkeypatch, channel: int) -> FakeConn:
    monkeypatch.setattr(sp, "_shadow_test_channel", lambda: channel)
    conn = FakeConn()
    wrote = sp.post_shadow_ai_signal(conn, "LIS1", "TESTUSDT", "SHORT", 0.5, 100.0, 100.0, 105.0, [95.0, 90.0, 85.0])
    assert wrote is True
    return conn


def _outbox_rows(conn: FakeConn) -> list[tuple]:
    return [(sql, params) for (sql, params) in conn.ops if "INSERT INTO telegram_outbox" in sql]


# ── 1. Default OFF: no channel post ──────────────────────────────────────────
def test_default_off_writes_no_outbox(monkeypatch):
    conn = _emit(monkeypatch, 0)
    assert any("INSERT INTO ai_signals" in sql for sql, _ in conn.ops)  # shadow written
    assert _outbox_rows(conn) == []  # but NEVER a channel post
    assert conn.commits == 0  # rule 8: caller commits


# ── 2. Set: exactly ONE row to exactly THIS channel ──────────────────────────────
def test_set_writes_one_preview_to_test_channel(monkeypatch):
    conn = _emit(monkeypatch, TEST_CH)
    rows = _outbox_rows(conn)
    assert len(rows) == 1
    _, params = rows[0]
    channel_id, message = params
    assert channel_id == TEST_CH  # never another (trading) channel
    assert "SHADOW PREVIEW" in message and "NOT a trade signal" in message
    assert "LIS1" in message and "TESTUSDT" in message and "SHORT" in message
    assert conn.commits == 0  # still no commit here


# ── 2b. Hard boundary: never the trading channel (defense-in-depth) ──────────────
def test_shadow_test_channel_never_the_trading_channel(monkeypatch):
    from core import config

    # Misconfiguration: CH_SHADOW_TEST accidentally == trading channel.
    monkeypatch.setattr(config, "CH_SHADOW_TEST", -1002000000000, raising=False)
    monkeypatch.setattr(config, "REGIME_TRADING_CHANNEL_ID", -1002000000000, raising=False)
    assert sp._shadow_test_channel() == 0  # suppressed → no echo to trading channel
    # Another (non-trading) channel remains allowed.
    monkeypatch.setattr(config, "CH_SHADOW_TEST", TEST_CH, raising=False)
    assert sp._shadow_test_channel() == TEST_CH


# ── 3. The preview is NOT Cornix-parseable ──────────────────────────────────────
def test_preview_message_is_not_cornix_parseable():
    msg = sp._shadow_preview_message("LIS1", "TESTUSDT", "SHORT", 100.0, 105.0, [95.0, 90.0, 85.0])
    # None of the standard Cornix trigger structures (that would fire a trade).
    lower = msg.lower()
    for trigger in ("entry:", "targets:", "target:", "stop loss", "stoploss", "leverage:", "take profit"):
        assert trigger not in lower, f"Cornix trigger '{trigger}' in preview!"
    # But the reference values are visible (pure info) — as ref text.
    assert "Ref-Entry" in msg and "Ref-SL" in msg and "Ref-TPs" in msg
    assert msg.startswith("👻 SHADOW PREVIEW")
