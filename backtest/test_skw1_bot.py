# backtest/test_skw1_bot.py
"""DB-free tests for the SKW1 shadow forwarder (bot 38, K7, T-2026-CU-9050-149).

  1. shadow_gate: SKW1 LONG+SHORT are LIVE (T-2026-CU-9050-183, → CH_ATS),
     without artifact (forwarder class D).
  2. bot_catalog: tag "SKW1" → 38_ai_skw1_bot.py.
  3. select_deciles: liquidity filter (lowest tercile excluded) + skew decile rank
     (LONG lowest, SHORT highest decile), MIN_COINS guard.
  4. emit: emits per leg via post_ai_signal_gated only when LIVE/SHADOW +
     no cooldown/open trade + targets; SILENT → nothing. Live post to CH_ATS.

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
    # T-2026-CU-9050-183: both legs promoted live (→ CH_ATS), forwarder without artifact.
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
    # 25 liquid coins (dv=100), skew −12..+12; 5 illiquid (dv=1) with EXTREMELY
    # high skew (+50) — they must NOT be shorted despite the extreme skew.
    liquid = [(f"L{i:02d}", float(i - 12), 100.0) for i in range(25)]  # skew -12..12
    illiquid = [(f"I{i:02d}", 50.0, 1.0) for i in range(5)]
    longs, shorts = skw1.select_deciles(liquid + illiquid)
    # ndec = round(25/10) = 2 → 2 per side from the LIQUID set
    assert longs == ["L00", "L01"]  # lowest skew (−12, −11)
    assert shorts == ["L23", "L24"]  # highest LIQUID skew (11, 12), NOT the I coins
    assert not any(s.startswith("I") for s in longs + shorts)  # illiquid excluded


def test_select_deciles_min_coins_guard():
    rows = [(f"C{i:02d}", float(i), 100.0) for i in range(15)]  # < MIN_COINS_PER_WEEK
    assert skw1.select_deciles(rows) == ([], [])


# ── 4. emit: gating + shadow emit ─────────────────────────────────────────────
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
    assert short[2] == skw1._kcfg.CH_ATS and long[2] == skw1._kcfg.CH_ATS  # former ATS channel
    assert short[6] > short[4]  # SHORT-SL above entry (sl > e1)
    assert long[6] < long[4]  # LONG-SL below entry
    assert short[4] == short[5] and long[4] == long[5]  # market fill (e1==e2)


def test_emit_skips_when_silent_or_gated(monkeypatch):
    posts = _wire(monkeypatch, leg=sg.SILENT)  # SILENT → nothing (LIVE/SHADOW would emit)
    skw1.emit(_FakeConn(), "HIUSDT", "SHORT", 100.0)
    posts2 = _wire(monkeypatch, cooldown=True)
    skw1.emit(_FakeConn(), "HIUSDT", "SHORT", 100.0)
    posts3 = _wire(monkeypatch, has_open=True)
    skw1.emit(_FakeConn(), "HIUSDT", "SHORT", 100.0)
    assert posts == [] and posts2 == [] and posts3 == []
