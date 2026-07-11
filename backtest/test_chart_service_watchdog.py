# backtest/test_chart_service_watchdog.py
"""
Unit tests for the chart_data_service watchdog + off-loop snapshot (P2.20,
T-2026-CU-9050-092).

Two fixes:
  1. `async for msg in ws` had no timeout — a silent connection (Binance accepts
     the handshake but sends 0 messages) hung the worker forever without ever
     reconnecting. `_consume_with_watchdog` now returns after
     CHART_WS_MESSAGE_WATCHDOG_SEC of silence so the worker reconnects.
  2. The ~12MB JSON snapshot + os.replace ran synchronously on the event loop
     every 60s, stalling the WS consumers. The dump now runs in a thread
     (asyncio.to_thread) and the interval widened to 300s.

DB-free, no real websocket. Run with:
    pytest backtest/test_chart_service_watchdog.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import chart_data_service as chart  # noqa: E402


# ── The ledger constants are pinned ──────────────────────────────────────────


def test_snapshot_interval_widened_to_300():
    assert chart.SNAPSHOT_INTERVAL_SEC == 300


def test_message_watchdog_is_120s():
    assert chart.CHART_WS_MESSAGE_WATCHDOG_SEC == 120


# ── The watchdog: silence -> return (reconnect), messages -> buffered ─────────


class _SilentWS:
    """recv() never yields — models a connection Binance accepts but keeps mute."""

    async def recv(self):
        await asyncio.sleep(3600)


class _ChattyThenSilentWS:
    """Yields one finalized kline, then goes silent."""

    def __init__(self, messages):
        self._messages = list(messages)

    async def recv(self):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(3600)


def test_silent_connection_triggers_reconnect_within_watchdog(monkeypatch):
    """A mute connection makes _consume_with_watchdog RETURN (not hang) fast."""
    monkeypatch.setattr(chart, "CHART_WS_MESSAGE_WATCHDOG_SEC", 0.05)

    async def run():
        # If the watchdog were broken this would hang; wrap in an outer timeout.
        await asyncio.wait_for(chart._consume_with_watchdog(_SilentWS(), worker_id=1), timeout=2.0)

    asyncio.run(run())  # returns cleanly == watchdog fired


def test_message_is_buffered_then_watchdog_fires(monkeypatch):
    monkeypatch.setattr(chart, "CHART_WS_MESSAGE_WATCHDOG_SEC", 0.05)
    chart._BUFFERS.clear()

    kline = json.dumps(
        {
            "data": {
                "k": {
                    "x": True,  # finalized
                    "s": "TESTUSDT",
                    "t": 1_700_000_000_000,
                    "o": "1.0",
                    "h": "2.0",
                    "l": "0.5",
                    "c": "1.5",
                    "v": "100.0",
                }
            }
        }
    )

    async def run():
        await asyncio.wait_for(
            chart._consume_with_watchdog(_ChattyThenSilentWS([kline]), worker_id=1), timeout=2.0
        )

    asyncio.run(run())
    assert "TESTUSDT" in chart._BUFFERS
    assert list(chart._BUFFERS["TESTUSDT"])[-1][4] == 1.5  # close price landed
    chart._BUFFERS.clear()


# ── The off-loop snapshot writes valid JSON atomically ───────────────────────


def test_write_snapshot_to_disk_atomic(tmp_path, monkeypatch):
    target = tmp_path / "snap.json"
    monkeypatch.setattr(chart, "SNAPSHOT_FILE", str(target))

    snapshot = {"BTCUSDT": [[1, 2.0, 3.0, 1.0, 2.5, 10.0]]}
    chart._write_snapshot_to_disk(snapshot)

    assert target.exists()
    assert not (tmp_path / "snap.json.tmp").exists(), "tmp file must be renamed away"
    assert json.loads(target.read_text()) == snapshot


def test_save_snapshot_offloads_the_dump_to_a_thread(tmp_path, monkeypatch):
    """save_snapshot must hand the blocking dump to asyncio.to_thread, not run it
    inline on the event loop."""
    target = tmp_path / "snap.json"
    monkeypatch.setattr(chart, "SNAPSHOT_FILE", str(target))
    chart._BUFFERS.clear()
    chart._BUFFERS["ETHUSDT"] = __import__("collections").deque([[1, 2.0, 3.0, 1.0, 2.5, 9.0]])

    seen = {"to_thread": False}
    real_to_thread = asyncio.to_thread

    async def spy(func, *a, **kw):
        seen["to_thread"] = True
        return await real_to_thread(func, *a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    asyncio.run(chart.save_snapshot())

    assert seen["to_thread"], "the dump was not offloaded to a thread"
    assert json.loads(target.read_text()) == {"ETHUSDT": [[1, 2.0, 3.0, 1.0, 2.5, 9.0]]}
    chart._BUFFERS.clear()
