# backtest/test_tsm1_bot.py
"""DB-freie Tests für den TSM1-Shadow-Forwarder (Bot 37, K1, T-2026-CU-9050-149).

  1. shadow_gate: TSM1-SHORT ist LIVE (T-2026-CU-9050-183, → CH_FIF1), ohne
     Artefakt (Forwarder-Klasse D); die nicht-emittierte LONG-Richtung ebenfalls LIVE.
  2. bot_catalog: Tag "TSM1" → 37_ai_tsm1_bot.py.
  3. short_crossing: das 4h-ROC-Crossing-Prädikat (außen→innen) feuert genau am
     Bar mit dem Durchbruch, nicht davor.
  4. process_coin: emittiert über post_ai_signal_gated nur bei Crossing +
     LIVE/SHADOW-Bein + kein Cooldown/offener Trade + genug Kerzen + Targets;
     SILENT/retired → nichts. Live-Post geht an den geerbten CH_FIF1.

Run: pytest backtest/test_tsm1_bot.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import core.bot_catalog as bc  # noqa: E402
from core import shadow_gate as sg  # noqa: E402


def _import_tsm1():
    path = os.path.join(REPO_ROOT, "37_ai_tsm1_bot.py")
    spec = importlib.util.spec_from_file_location("tsm1_bot_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tsm1_bot_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


tsm1 = _import_tsm1()


def _base_walk(n):
    """Deterministischer Kurs mit ECHTER Varianz (~1 % Returns) → σ>0, reales Band."""
    t = np.arange(n)
    rets = 0.01 * np.sin(t * 1.7) + 0.005 * np.cos(t * 0.3)
    return 100.0 * np.exp(np.cumsum(rets))


def _crossing_closes():
    """ROC[-1] = −20 % (weit unter −Band), ROC[-2] = +1 % (innerhalb) → SHORT-Crossing."""
    close = _base_walk(tsm1.MIN_4H_ROWS + 5).copy()
    close[-1] = close[-1 - tsm1.ROC_L] * 0.80  # ROC[-1] = -0.20
    close[-2] = close[-2 - tsm1.ROC_L] * 1.01  # ROC[-2] = +0.01 (außen? nein: innen)
    return close


def _quiet_closes():
    """Beide letzten ROCs positiv → kein SHORT-Crossing."""
    close = _base_walk(tsm1.MIN_4H_ROWS + 5).copy()
    close[-1] = close[-1 - tsm1.ROC_L] * 1.01
    close[-2] = close[-2 - tsm1.ROC_L] * 1.01
    return close


# ── 1. shadow_gate ────────────────────────────────────────────────────────────
def test_tsm1_short_is_live_no_artifact():
    # T-2026-CU-9050-183: TSM1 SHORT live promotet (→ CH_FIF1); Forwarder ohne
    # Artefakt (Klasse D), also weiterhin kein Staging-Modell.
    assert sg.leg_status("TSM1", "SHORT") == sg.LIVE
    assert not sg.is_shadow("TSM1", "SHORT")
    assert sg.shadow_artifact_path("TSM1", "SHORT") is None
    assert sg.load_shadow_artifact("TSM1", "SHORT") is None
    # LONG wird nicht emittiert → ebenfalls Default-LIVE (kein Bot postet es).
    assert sg.leg_status("TSM1", "LONG") == sg.LIVE


# ── 2. bot_catalog ────────────────────────────────────────────────────────────
def test_tsm1_tag_maps_to_bot37():
    assert bc.script_for_tag("TSM1") == "37_ai_tsm1_bot.py"
    assert bc.script_for_tag("tsm1") == "37_ai_tsm1_bot.py"


# ── 3. short_crossing ─────────────────────────────────────────────────────────
def test_short_crossing_fires_on_breakout():
    assert tsm1.short_crossing(_crossing_closes()) is True


def test_short_crossing_quiet_series_no_fire():
    assert tsm1.short_crossing(_quiet_closes()) is False


def test_short_crossing_needs_full_history():
    assert tsm1.short_crossing(_base_walk(50)) is False  # < MIN_4H_ROWS


# ── 4. process_coin: Gating + Shadow-Emit ─────────────────────────────────────
class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass


class _FakeConn:
    def cursor(self, *a, **k):
        return _Cur()

    def commit(self):
        pass

    def rollback(self):
        pass


def _df(close):
    import pandas as pd

    return pd.DataFrame({"open_time": range(len(close)), "close": close})


def _wire(monkeypatch, *, leg=None, cooldown=False, has_open=False, closes=None, targets=(95.0, 90.0, 85.0)):
    posts: list[tuple] = []
    eff_leg = leg if leg is not None else sg.LIVE
    monkeypatch.setattr(tsm1, "shadow_posting_enabled", lambda: True)
    monkeypatch.setattr(tsm1, "leg_status", lambda *_: eff_leg)
    monkeypatch.setattr(tsm1, "check_cooldown", lambda *a, **k: cooldown)
    monkeypatch.setattr(tsm1, "has_open_ai_signal", lambda *a, **k: has_open)
    monkeypatch.setattr(tsm1, "read_candles", lambda *a, **k: _df(_crossing_closes() if closes is None else closes))
    monkeypatch.setattr(tsm1, "get_hvn_and_sr_levels", lambda *a, **k: ([80.0, 85.0, 90.0], [110.0, 120.0]))
    monkeypatch.setattr(tsm1, "ensure_min_tp_distance", lambda *a, **k: list(targets))
    monkeypatch.setattr(tsm1, "update_cooldown", lambda *a, **k: None)

    def _gated(conn, tag, direction, channel_id, sym, conf, e1, e2, sl, tgts, **k):
        posts.append((tag, direction, channel_id, sym, e1, e2, sl, tuple(tgts)))
        return "live" if eff_leg == sg.LIVE else "shadow"

    monkeypatch.setattr(tsm1, "post_ai_signal_gated", _gated)
    return posts


def test_process_coin_emits_on_crossing(monkeypatch):
    posts = _wire(monkeypatch)
    assert tsm1.process_coin(_FakeConn(), "NEWUSDT") is True
    assert len(posts) == 1
    tag, direction, channel_id, sym, e1, e2, sl, _ = posts[0]
    assert (tag, direction, sym) == ("TSM1", "SHORT", "NEWUSDT")
    assert channel_id == tsm1._kcfg.CH_FIF1  # von FIF1 geerbter Ziel-Channel
    assert e1 == e2  # Market-Fill
    assert sl > e1  # SHORT-SL über Entry


def test_process_coin_no_emit_without_crossing(monkeypatch):
    posts = _wire(monkeypatch, closes=_quiet_closes())
    assert tsm1.process_coin(_FakeConn(), "NEWUSDT") is False
    assert posts == []


def test_process_coin_emits_when_leg_shadow(monkeypatch):
    # Rückzug in den Shadow (SHADOW-Bein) → emittiert weiter, aber als Shadow.
    posts = _wire(monkeypatch, leg=sg.SHADOW)
    assert tsm1.process_coin(_FakeConn(), "NEWUSDT") is True
    assert len(posts) == 1


def test_process_coin_no_emit_when_silent(monkeypatch):
    posts = _wire(monkeypatch, leg=sg.SILENT)
    assert tsm1.process_coin(_FakeConn(), "NEWUSDT") is False
    assert posts == []


def test_process_coin_no_emit_on_cooldown_or_open(monkeypatch):
    posts = _wire(monkeypatch, cooldown=True)
    tsm1.process_coin(_FakeConn(), "NEWUSDT")
    posts2 = _wire(monkeypatch, has_open=True)
    tsm1.process_coin(_FakeConn(), "NEWUSDT")
    assert posts == [] and posts2 == []


def test_process_coin_no_emit_too_few_candles(monkeypatch):
    posts = _wire(monkeypatch, closes=_base_walk(50))
    assert tsm1.process_coin(_FakeConn(), "NEWUSDT") is False
    assert posts == []
