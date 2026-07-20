# backtest/test_skw1_bot.py
"""DB-freie Tests für den SKW1-Shadow-Forwarder (Bot 38, K7, T-2026-CU-9050-149).

  1. shadow_gate: SKW1 LONG+SHORT sind LIVE (T-2026-CU-9050-183, → CH_ATS),
     ohne Artefakt (Forwarder-Klasse D).
  2. bot_catalog: Tag "SKW1" → 38_ai_skw1_bot.py.
  3. select_deciles: Liquiditäts-Filter (unteres Terzil raus) + Skew-Dezil-Rang
     (LONG unterstes, SHORT oberstes Dezil), MIN_COINS-Guard.
  4. emit: emittiert je Bein über post_ai_signal_gated nur wenn LIVE/SHADOW +
     kein Cooldown/offener Trade + Targets; SILENT → nichts. Live-Post an CH_ATS.

Run: pytest backtest/test_skw1_bot.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import core.bot_catalog as bc  # noqa: E402
from core import shadow_gate as sg  # noqa: E402


def _import_skw1():
    path = os.path.join(REPO_ROOT, "38_ai_skw1_bot.py")
    spec = importlib.util.spec_from_file_location("skw1_bot_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["skw1_bot_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


skw1 = _import_skw1()


# ── 1. shadow_gate ────────────────────────────────────────────────────────────
def test_skw1_both_legs_live_no_artifact():
    # T-2026-CU-9050-183: beide Beine live promotet (→ CH_ATS), Forwarder ohne Artefakt.
    for d in ("LONG", "SHORT"):
        assert sg.leg_status("SKW1", d) == sg.LIVE
        assert not sg.is_shadow("SKW1", d)
        assert sg.shadow_artifact_path("SKW1", d) is None
    assert sg.load_shadow_artifact("SKW1", "SHORT") is None


# ── 2. bot_catalog ────────────────────────────────────────────────────────────
def test_skw1_tag_maps_to_bot38():
    assert bc.script_for_tag("SKW1") == "38_ai_skw1_bot.py"


# ── 3. select_deciles ─────────────────────────────────────────────────────────
def test_select_deciles_ranks_and_filters_liquidity():
    # 25 liquide Coins (dv=100), Skew −12..+12; 5 illiquide (dv=1) mit EXTREM
    # hoher Skew (+50) — die dürfen trotz Extrem-Skew NICHT geshortet werden.
    liquid = [(f"L{i:02d}", float(i - 12), 100.0) for i in range(25)]  # skew -12..12
    illiquid = [(f"I{i:02d}", 50.0, 1.0) for i in range(5)]
    longs, shorts = skw1.select_deciles(liquid + illiquid)
    # ndec = round(25/10) = 2 → 2 je Seite aus dem LIQUIDEN Set
    assert longs == ["L00", "L01"]  # niedrigste Skew (−12, −11)
    assert shorts == ["L23", "L24"]  # höchste LIQUIDE Skew (11, 12), NICHT die I-Coins
    assert not any(s.startswith("I") for s in longs + shorts)  # illiquide raus


def test_select_deciles_min_coins_guard():
    rows = [(f"C{i:02d}", float(i), 100.0) for i in range(15)]  # < MIN_COINS_PER_WEEK
    assert skw1.select_deciles(rows) == ([], [])


# ── 4. emit: Gating + Shadow-Emit ─────────────────────────────────────────────
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


def _wire(monkeypatch, *, leg=None, cooldown=False, has_open=False, targets=(95.0, 90.0, 85.0)):
    posts: list[tuple] = []
    eff_leg = leg if leg is not None else sg.LIVE
    monkeypatch.setattr(skw1, "shadow_posting_enabled", lambda: True)
    monkeypatch.setattr(skw1, "leg_status", lambda *_: eff_leg)
    monkeypatch.setattr(skw1, "check_cooldown", lambda *a, **k: cooldown)
    monkeypatch.setattr(skw1, "has_open_ai_signal", lambda *a, **k: has_open)
    monkeypatch.setattr(skw1, "get_hvn_and_sr_levels", lambda *a, **k: ([80.0, 85.0, 90.0], [110.0, 120.0]))
    monkeypatch.setattr(skw1, "ensure_min_tp_distance", lambda *a, **k: list(targets))
    monkeypatch.setattr(skw1, "update_cooldown", lambda *a, **k: None)

    def _gated(conn, tag, direction, channel_id, sym, conf, e1, e2, sl, tgts, **k):
        posts.append((tag, direction, channel_id, sym, e1, e2, sl))
        return "live" if eff_leg == sg.LIVE else "shadow"

    monkeypatch.setattr(skw1, "post_ai_signal_gated", _gated)
    return posts


def test_emit_short_and_long(monkeypatch):
    posts = _wire(monkeypatch)
    skw1.emit(_FakeConn(), "HIUSDT", "SHORT", 100.0)
    skw1.emit(_FakeConn(), "LOUSDT", "LONG", 100.0)
    assert len(posts) == 2
    short = next(p for p in posts if p[1] == "SHORT")  # (tag, dir, ch, sym, e1, e2, sl)
    long = next(p for p in posts if p[1] == "LONG")
    assert short[0] == "SKW1"
    assert short[2] == skw1._kcfg.CH_ATS and long[2] == skw1._kcfg.CH_ATS  # ehem. ATS-Channel
    assert short[6] > short[4]  # SHORT-SL über Entry (sl > e1)
    assert long[6] < long[4]  # LONG-SL unter Entry
    assert short[4] == short[5] and long[4] == long[5]  # Market-Fill (e1==e2)


def test_emit_skips_when_silent_or_gated(monkeypatch):
    posts = _wire(monkeypatch, leg=sg.SILENT)  # SILENT → nichts (LIVE/SHADOW würden emittieren)
    skw1.emit(_FakeConn(), "HIUSDT", "SHORT", 100.0)
    posts2 = _wire(monkeypatch, cooldown=True)
    skw1.emit(_FakeConn(), "HIUSDT", "SHORT", 100.0)
    posts3 = _wire(monkeypatch, has_open=True)
    skw1.emit(_FakeConn(), "HIUSDT", "SHORT", 100.0)
    assert posts == [] and posts2 == [] and posts3 == []
