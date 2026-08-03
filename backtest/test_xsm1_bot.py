# backtest/test_xsm1_bot.py
"""DB-free tests for the XSM1/XSR1 shadow forwarder (bot 39, K2, T-2026-CU-9050-149).

  1. shadow_gate: XSM1-LONG + XSR1-SHORT are LIVE (T-2026-CU-9050-183, → CH_ATS),
     without artifact.
  2. bot_catalog: tags "XSM1"/"XSR1" → 39_ai_xsm1_bot.py.
  3. select_top_decile: liquidity filter + top F-return decile.
  4. emit: emits per (tag,direction) via post_ai_signal_gated only when
     LIVE/SHADOW + no cooldown/open trade + targets; SILENT → nothing. → CH_ATS.

Run: pytest backtest/test_xsm1_bot.py -v
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


def _import_xsm1():
    path = os.path.join(REPO_ROOT, "39_ai_xsm1_bot.py")
    spec = importlib.util.spec_from_file_location("xsm1_bot_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["xsm1_bot_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


xsm1 = _import_xsm1()


# ── 1. shadow_gate ────────────────────────────────────────────────────────────
def test_xsm1_xsr1_legs_live_no_artifact():
    # T-2026-CU-9050-183: the emitted legs are promoted live (→ CH_ATS), no artifact.
    assert sg.leg_status("XSM1", "LONG") == sg.LIVE
    assert sg.leg_status("XSR1", "SHORT") == sg.LIVE
    assert sg.shadow_artifact_path("XSM1", "LONG") is None
    assert sg.shadow_artifact_path("XSR1", "SHORT") is None
    # the respective NOT-emitted opposite direction stays default-LIVE (no bot posts it)
    assert sg.leg_status("XSM1", "SHORT") == sg.LIVE
    assert sg.leg_status("XSR1", "LONG") == sg.LIVE


# ── 2. bot_catalog ────────────────────────────────────────────────────────────
def test_xsm1_xsr1_tags_map_to_bot39():
    assert bc.script_for_tag("XSM1") == "39_ai_xsm1_bot.py"
    assert bc.script_for_tag("XSR1") == "39_ai_xsm1_bot.py"


# ── 3. select_top_decile ──────────────────────────────────────────────────────
def test_select_top_decile_ranks_and_filters_liquidity():
    # 30 liquid coins (dv=100), F-return 0..29 %; 5 illiquid (dv=1) with EXTREMELY
    # high return (+99 %) — they must still NOT make the top decile.
    liquid = [(f"L{i:02d}", float(i) / 100.0, 100.0) for i in range(30)]
    illiquid = [(f"I{i:02d}", 0.99, 1.0) for i in range(5)]
    top = xsm1.select_top_decile(liquid + illiquid)
    # ndec = max(1, round(30 * 0.10)) = 3 → the 3 highest LIQUID returns
    assert top == ["L27", "L28", "L29"]
    assert not any(s.startswith("I") for s in top)  # illiquid excluded


def test_select_top_decile_min_coins_guard():
    rows = [(f"C{i:02d}", float(i), 100.0) for i in range(15)]  # < MIN_COINS_PER_WEEK
    assert xsm1.select_top_decile(rows) == []


# ── 4. emit ───────────────────────────────────────────────────────────────────
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
    monkeypatch.setattr(xsm1, "shadow_posting_enabled", lambda: True)
    monkeypatch.setattr(xsm1, "leg_status", lambda *_: eff_leg)
    monkeypatch.setattr(xsm1, "check_cooldown", lambda *a, **k: cooldown)
    monkeypatch.setattr(xsm1, "has_open_ai_signal", lambda *a, **k: has_open)
    monkeypatch.setattr(xsm1, "get_hvn_and_sr_levels", lambda *a, **k: ([80.0, 85.0, 90.0], [110.0, 120.0]))
    monkeypatch.setattr(xsm1, "ensure_min_tp_distance", lambda *a, **k: list(targets))
    monkeypatch.setattr(xsm1, "update_cooldown", lambda *a, **k: None)

    def _gated(conn, tag, direction, channel_id, sym, conf, e1, e2, sl, tgts, **k):
        posts.append((tag, direction, channel_id, sym, e1, e2, sl))
        return "live" if eff_leg == sg.LIVE else "shadow"

    monkeypatch.setattr(xsm1, "post_ai_signal_gated", _gated)
    return posts


def test_emit_both_hypotheses(monkeypatch):
    posts = _wire(monkeypatch)
    xsm1.emit(_FakeConn(), "TOPUSDT", "XSM1", "LONG", 100.0)
    xsm1.emit(_FakeConn(), "TOPUSDT", "XSR1", "SHORT", 100.0)
    assert len(posts) == 2
    xsm = next(p for p in posts if p[0] == "XSM1")  # (tag, dir, ch, sym, e1, e2, sl)
    xsr = next(p for p in posts if p[0] == "XSR1")
    assert xsm[1] == "LONG" and xsm[6] < xsm[4]  # LONG-SL below entry (sl < e1)
    assert xsr[1] == "SHORT" and xsr[6] > xsr[4]  # SHORT-SL above entry
    assert xsm[2] == xsm1._kcfg.CH_ATS and xsr[2] == xsm1._kcfg.CH_ATS  # former ATS channel
    assert xsm[4] == xsm[5] and xsr[4] == xsr[5]  # market fill (e1==e2)


def test_run_scan_pairs_xsm1_long_xsr1_short():
    # Since T-2026-CU-9050-183 the legs are default-LIVE — the direction is now ONLY
    # secured by the run_scan pairing (no longer by the SHADOW registration). Pin
    # this invariant (review LOW): no inverted emission.
    import inspect

    src = inspect.getsource(xsm1.run_scan)
    assert '(XSM_TAG, "LONG")' in src and '(XSR_TAG, "SHORT")' in src
    assert '(XSM_TAG, "SHORT")' not in src and '(XSR_TAG, "LONG")' not in src


def test_emit_skips_when_silent_or_gated(monkeypatch):
    posts = _wire(monkeypatch, leg=sg.SILENT)  # SILENT → nothing (LIVE/SHADOW would emit)
    xsm1.emit(_FakeConn(), "TOPUSDT", "XSM1", "LONG", 100.0)
    posts2 = _wire(monkeypatch, cooldown=True)
    xsm1.emit(_FakeConn(), "TOPUSDT", "XSM1", "LONG", 100.0)
    posts3 = _wire(monkeypatch, has_open=True)
    xsm1.emit(_FakeConn(), "TOPUSDT", "XSR1", "SHORT", 100.0)
    assert posts == [] and posts2 == [] and posts3 == []
