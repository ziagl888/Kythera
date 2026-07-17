# backtest/test_shadow_test_channel.py
"""DB-freie Tests für den optionalen Shadow-Sichtbarkeits-Echo (T-2026-CU-9050-150).

Sicherheits-Invarianten:
  1. Default (CH_SHADOW_TEST=0): post_shadow_ai_signal schreibt NUR ai_signals,
     KEINE telegram_outbox — exakt das bisherige Shadow-Verhalten (rückwärtskompat).
  2. Gesetzt (CH_SHADOW_TEST=<id>): zusätzlich GENAU EINE telegram_outbox-Zeile,
     an DIESEN Channel (nie an einen anderen), mit einer NICHT-Cornix-parsebaren
     Vorschau (klar als SHADOW markiert, keine Cornix-Trigger-Keywords).
  3. Der Echo läuft in derselben offenen Transaktion — kein Commit hier (Regel 8).

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


# ── 1. Default AUS: kein Kanal-Post ───────────────────────────────────────────
def test_default_off_writes_no_outbox(monkeypatch):
    conn = _emit(monkeypatch, 0)
    assert any("INSERT INTO ai_signals" in sql for sql, _ in conn.ops)  # Shadow geschrieben
    assert _outbox_rows(conn) == []  # aber NIE ein Kanal-Post
    assert conn.commits == 0  # Regel 8: Caller committet


# ── 2. Gesetzt: genau EINE Zeile an genau DIESEN Channel ──────────────────────
def test_set_writes_one_preview_to_test_channel(monkeypatch):
    conn = _emit(monkeypatch, TEST_CH)
    rows = _outbox_rows(conn)
    assert len(rows) == 1
    _, params = rows[0]
    channel_id, message = params
    assert channel_id == TEST_CH  # nie ein anderer (Handels-)Channel
    assert "SHADOW-VORSCHAU" in message and "KEIN Handelssignal" in message
    assert "LIS1" in message and "TESTUSDT" in message and "SHORT" in message
    assert conn.commits == 0  # weiterhin kein Commit hier


# ── 2b. Harte Schranke: nie der Handels-Channel (Defense-in-Depth) ────────────
def test_shadow_test_channel_never_the_trading_channel(monkeypatch):
    from core import config

    # Fehlkonfiguration: CH_SHADOW_TEST versehentlich == Handels-Channel.
    monkeypatch.setattr(config, "CH_SHADOW_TEST", -1002000000000, raising=False)
    monkeypatch.setattr(config, "REGIME_TRADING_CHANNEL_ID", -1002000000000, raising=False)
    assert sp._shadow_test_channel() == 0  # unterdrückt → kein Echo an den Handels-Channel
    # Ein anderer (Nicht-Handels-)Channel bleibt erlaubt.
    monkeypatch.setattr(config, "CH_SHADOW_TEST", TEST_CH, raising=False)
    assert sp._shadow_test_channel() == TEST_CH


# ── 3. Die Vorschau ist NICHT Cornix-parsebar ─────────────────────────────────
def test_preview_message_is_not_cornix_parseable():
    msg = sp._shadow_preview_message("LIS1", "TESTUSDT", "SHORT", 100.0, 105.0, [95.0, 90.0, 85.0])
    # Keine der Standard-Cornix-Trigger-Strukturen (die einen Trade auslösen würden).
    lower = msg.lower()
    for trigger in ("entry:", "targets:", "target:", "stop loss", "stoploss", "leverage:", "take profit"):
        assert trigger not in lower, f"Cornix-Trigger '{trigger}' in der Vorschau!"
    # Aber die Referenzwerte sind sichtbar (reine Info) — als Ref-Text.
    assert "Ref-Entry" in msg and "Ref-SL" in msg and "Ref-Ziele" in msg
    assert msg.startswith("👻 SHADOW-VORSCHAU")
