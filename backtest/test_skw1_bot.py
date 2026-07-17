# backtest/test_skw1_bot.py
"""DB-freie Tests für den SKW1-Shadow-Forwarder (Bot 38, K7, T-2026-CU-9050-149).

  1. shadow_gate: SKW1 LONG+SHORT sind SHADOW, ohne Artefakt (Forwarder-Klasse D).
  2. bot_catalog: Tag "SKW1" → 38_ai_skw1_bot.py.
  3. select_deciles: Liquiditäts-Filter (unteres Terzil raus) + Skew-Dezil-Rang
     (LONG unterstes, SHORT oberstes Dezil), MIN_COINS-Guard.
  4. emit: schreibt einen SHADOW-Trade je Bein nur wenn SHADOW + kein
     Cooldown/offener Trade + Targets — nie live.

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
def test_skw1_both_legs_shadow_no_artifact():
    for d in ("LONG", "SHORT"):
        assert sg.leg_status("SKW1", d) == sg.SHADOW
        assert sg.is_shadow("SKW1", d)
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
    monkeypatch.setattr(skw1, "shadow_posting_enabled", lambda: True)
    monkeypatch.setattr(skw1, "leg_status", lambda *_: leg if leg is not None else sg.SHADOW)
    monkeypatch.setattr(skw1, "check_cooldown", lambda *a, **k: cooldown)
    monkeypatch.setattr(skw1, "has_open_ai_signal", lambda *a, **k: has_open)
    monkeypatch.setattr(skw1, "get_hvn_and_sr_levels", lambda *a, **k: ([80.0, 85.0, 90.0], [110.0, 120.0]))
    monkeypatch.setattr(skw1, "ensure_min_tp_distance", lambda *a, **k: list(targets))
    monkeypatch.setattr(skw1, "update_cooldown", lambda *a, **k: None)

    def _post(conn, tag, sym, direction, conf, e1, e2, sl, tgts, **k):
        posts.append((tag, sym, direction, e1, e2, sl))
        return True

    monkeypatch.setattr(skw1, "post_shadow_ai_signal", _post)
    return posts


def test_emit_short_and_long(monkeypatch):
    posts = _wire(monkeypatch)
    skw1.emit(_FakeConn(), "HIUSDT", "SHORT", 100.0)
    skw1.emit(_FakeConn(), "LOUSDT", "LONG", 100.0)
    assert len(posts) == 2
    short = next(p for p in posts if p[2] == "SHORT")
    long = next(p for p in posts if p[2] == "LONG")
    assert short[0] == "SKW1" and short[5] > short[3]  # SHORT-SL über Entry
    assert long[5] < long[3]  # LONG-SL unter Entry
    assert short[3] == short[4] and long[3] == long[4]  # Market-Fill


def test_emit_skips_when_not_shadow_or_gated(monkeypatch):
    posts = _wire(monkeypatch, leg=sg.LIVE)
    skw1.emit(_FakeConn(), "HIUSDT", "SHORT", 100.0)
    posts2 = _wire(monkeypatch, cooldown=True)
    skw1.emit(_FakeConn(), "HIUSDT", "SHORT", 100.0)
    posts3 = _wire(monkeypatch, has_open=True)
    skw1.emit(_FakeConn(), "HIUSDT", "SHORT", 100.0)
    assert posts == [] and posts2 == [] and posts3 == []
