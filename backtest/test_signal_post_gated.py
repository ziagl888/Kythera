# backtest/test_signal_post_gated.py
"""DB-free tests for signal_post.post_ai_signal_gated (T-2026-CU-9050-183).

The central shadow→live routing of the WS2 promotion pattern, tested against REAL
shadow_gate legs (no leg_status monkeypatch):
  * LIVE   (TSM1 SHORT)  → post_ai_signal (Cornix post to channel_id).
  * SHADOW (LIS1 SHORT)  → post_shadow_ai_signal (monitored, no Cornix).
  * SILENT (FIF1)        → no-op (parked).

Run: pytest backtest/test_signal_post_gated.py -v
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from core import shadow_gate as sg  # noqa: E402
from core import signal_post as sp  # noqa: E402

ARGS = dict(
    symbol="ABCUSDT",
    confidence=0.5,
    entry1=100.0,
    entry2=100.0,
    sl=105.0,
    targets=[95.0, 90.0],
    source_desc="test",
)


def _capture(monkeypatch):
    calls: dict[str, list] = {"live": [], "shadow": []}
    monkeypatch.setattr(sp, "post_ai_signal", lambda *a, **k: calls["live"].append((a, k)))
    monkeypatch.setattr(sp, "post_shadow_ai_signal", lambda *a, **k: (calls["shadow"].append((a, k)), True)[1])
    return calls


def test_live_leg_routes_to_cornix_post(monkeypatch):
    assert sg.leg_status("TSM1", "SHORT") == sg.LIVE  # T-2026-CU-9050-183
    calls = _capture(monkeypatch)
    out = sp.post_ai_signal_gated(None, "TSM1", "SHORT", 123, **ARGS)
    assert out == "live"
    assert len(calls["live"]) == 1 and calls["shadow"] == []
    # channel_id is passed as 2nd positional (after conn) to post_ai_signal
    assert calls["live"][0][0][1] == 123


def test_shadow_leg_routes_to_monitored(monkeypatch):
    assert sg.leg_status("LIS1", "SHORT") == sg.SHADOW  # stays shadow (still falsified)
    calls = _capture(monkeypatch)
    out = sp.post_ai_signal_gated(None, "LIS1", "SHORT", 123, **ARGS)
    assert out == "shadow"
    assert len(calls["shadow"]) == 1 and calls["live"] == []


def test_silent_leg_is_noop(monkeypatch):
    # ATS1 is muted (T-2026-CU-9050-127). (FIF1 was formerly the SILENT
    # example, but since T-2026-KYT-9050-033 revived as SHADOW.)
    assert sg.leg_status("ATS1", "LONG") == sg.SILENT
    assert sg.leg_status("ATS1", "SHORT") == sg.SILENT
    calls = _capture(monkeypatch)
    out = sp.post_ai_signal_gated(None, "ATS1", "LONG", 123, **ARGS)
    assert out is None
    assert calls["live"] == [] and calls["shadow"] == []


def test_live_leg_with_zero_channel_falls_back_to_shadow(monkeypatch):
    # Fail-safe: LIVE leg but channel_id=0 (unconfigured channel) → NEVER a
    # Cornix post to channel 0, instead monitored shadow post.
    assert sg.leg_status("TSM1", "SHORT") == sg.LIVE
    calls = _capture(monkeypatch)
    out = sp.post_ai_signal_gated(None, "TSM1", "SHORT", 0, **ARGS)
    assert out == "shadow"
    assert calls["live"] == [] and len(calls["shadow"]) == 1


def test_shadow_dedup_returns_none(monkeypatch):
    # post_shadow_ai_signal reports False (open shadow trade → dedup) → gated None.
    _capture(monkeypatch)
    monkeypatch.setattr(sp, "post_shadow_ai_signal", lambda *a, **k: False)
    out = sp.post_ai_signal_gated(None, "LIS1", "SHORT", 123, **ARGS)
    assert out is None
