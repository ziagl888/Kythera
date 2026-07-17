# backtest/test_lis1_bot.py
"""DB-freie Tests für den LIS1-Shadow-Forwarder (Bot 36, K5, T-2026-CU-9050-149).

Pinnt die Sicherheits-Invarianten des regelbasierten (artefaktlosen) Shadow-Bots:
  1. core.shadow_gate: LIS1-SHORT ist SHADOW, hat ABER KEIN Artefakt (die
     Forwarder-Klasse (D) — Regel statt Modell); FMR1/andere Live-Beine bleiben live.
  2. core.bot_catalog: Tag "LIS1" → 36_ai_lis1_bot.py (Report-Zuordnung).
  3. in_fade_window: der Tag-3-Trigger feuert NUR im Alters-Fenster [3d, 4d).
  4. process_coin: emittiert einen SHADOW-Trade genau dann, wenn Bein=SHADOW,
     Alter im Fenster, kein Cooldown/offener Trade, genug Kerzen, Targets da —
     und postet NIE live (nur post_shadow_ai_signal).

Run: pytest backtest/test_lis1_bot.py -v
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# core.config verlangt Secrets; die Build-Maschine liefert ein leeres .env.
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import core.bot_catalog as bc  # noqa: E402
from core import shadow_gate as sg  # noqa: E402

UTC = datetime.timezone.utc


def _import_lis1():
    path = os.path.join(REPO_ROOT, "36_ai_lis1_bot.py")
    spec = importlib.util.spec_from_file_location("lis1_bot_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lis1_bot_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


lis1 = _import_lis1()


# ── 1. shadow_gate: SHADOW-Bein OHNE Artefakt (Forwarder-Klasse D) ────────────
def test_lis1_short_is_shadow_but_has_no_artifact():
    assert sg.leg_status("LIS1", "SHORT") == sg.SHADOW
    assert sg.is_shadow("LIS1", "SHORT")
    assert not sg.is_live("LIS1", "SHORT")
    # Regelbasiert → KEIN Modell-Artefakt: der Forwarder scored kein pkl.
    assert sg.shadow_artifact_path("LIS1", "SHORT") is None
    assert sg.load_shadow_artifact("LIS1", "SHORT") is None
    # Kein Live-Bein darf mitgeshadowt werden.
    assert sg.leg_status("FMR1", "SHORT") == sg.LIVE


# ── 2. bot_catalog: Tag → Skript ─────────────────────────────────────────────
def test_lis1_tag_maps_to_bot36():
    assert bc.script_for_tag("LIS1") == "36_ai_lis1_bot.py"
    assert bc.script_for_tag("lis1") == "36_ai_lis1_bot.py"  # case-insensitiv


# ── 3. in_fade_window: der Tag-3-Trigger ─────────────────────────────────────
@pytest.mark.parametrize(
    ("age_days", "expected"),
    [
        (2.9, False),  # noch vor Tag 3
        (3.0, True),  # exakt Tag 3 → feuert
        (3.5, True),  # innerhalb [3d, 4d)
        (3.99, True),
        (4.0, False),  # Grace vorbei → nicht mehr (kein Backfill alter Coins)
        (10.0, False),  # längst gelistet
    ],
)
def test_in_fade_window_boundaries(age_days, expected):
    now = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    onboard = now - datetime.timedelta(days=age_days)
    assert lis1.in_fade_window(onboard, now) is expected


# ── 4. process_coin: Gating + Shadow-Emit (helpers gemockt, DB-frei) ──────────
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


def _candles(n=60, last_close=100.0):
    import pandas as pd

    base = datetime.datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
    return pd.DataFrame(
        {
            "open_time": [base + datetime.timedelta(hours=i) for i in range(n)],
            "close": [last_close] * n,
        }
    )


def _wire(monkeypatch, *, leg=None, cooldown=False, has_open=False, candles=None, targets=(95.0, 90.0, 85.0)):
    """Patch die Bot-Modul-Globals auf DB-freie Fakes; sammelt Shadow-Posts."""
    posts: list[tuple] = []
    monkeypatch.setattr(lis1, "shadow_posting_enabled", lambda: True)
    monkeypatch.setattr(lis1, "leg_status", lambda *_: leg if leg is not None else sg.SHADOW)
    monkeypatch.setattr(lis1, "check_cooldown", lambda *a, **k: cooldown)
    monkeypatch.setattr(lis1, "has_open_ai_signal", lambda *a, **k: has_open)
    monkeypatch.setattr(lis1, "read_candles", lambda *a, **k: _candles() if candles is None else candles)
    monkeypatch.setattr(lis1, "get_hvn_and_sr_levels", lambda *a, **k: ([80.0, 85.0, 90.0], [110.0, 120.0]))
    monkeypatch.setattr(lis1, "ensure_min_tp_distance", lambda *a, **k: list(targets))
    monkeypatch.setattr(lis1, "update_cooldown", lambda *a, **k: None)

    def _post(conn, tag, sym, direction, conf, e1, e2, sl, tgts, **k):
        posts.append((tag, sym, direction, e1, e2, sl, tuple(tgts)))
        return True

    monkeypatch.setattr(lis1, "post_shadow_ai_signal", _post)
    return posts


def _onboard_map(symbol, age_days, now):
    onboard = now - datetime.timedelta(days=age_days)
    return {symbol: int(onboard.timestamp() * 1000)}


def test_process_coin_emits_shadow_on_day3(monkeypatch):
    now = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    posts = _wire(monkeypatch)
    lis1.process_coin(_FakeConn(), "NEWUSDT", _onboard_map("NEWUSDT", 3.5, now), now)
    assert len(posts) == 1
    tag, sym, direction, e1, e2, sl, tgts = posts[0]
    assert (tag, sym, direction) == ("LIS1", "NEWUSDT", "SHORT")
    assert e1 == e2 == 100.0  # Market-Fill (Zelle l0.0): entry1 == entry2
    assert sl > e1  # SHORT-SL liegt ÜBER dem Entry
    assert tgts == (95.0, 90.0, 85.0)


def test_process_coin_skips_old_coin(monkeypatch):
    now = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    posts = _wire(monkeypatch)
    lis1.process_coin(_FakeConn(), "OLDUSDT", _onboard_map("OLDUSDT", 30.0, now), now)
    assert posts == []


def test_process_coin_skips_when_leg_not_shadow(monkeypatch):
    now = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    posts = _wire(monkeypatch, leg=sg.LIVE)  # promotet/live → Bot schweigt (fail-safe)
    lis1.process_coin(_FakeConn(), "NEWUSDT", _onboard_map("NEWUSDT", 3.5, now), now)
    assert posts == []


def test_process_coin_skips_on_open_trade_and_cooldown(monkeypatch):
    now = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    m = _onboard_map("NEWUSDT", 3.5, now)
    posts = _wire(monkeypatch, has_open=True)
    lis1.process_coin(_FakeConn(), "NEWUSDT", m, now)
    assert posts == []
    posts2 = _wire(monkeypatch, cooldown=True)
    lis1.process_coin(_FakeConn(), "NEWUSDT", m, now)
    assert posts2 == []


def test_process_coin_skips_when_too_few_candles(monkeypatch):
    now = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    posts = _wire(monkeypatch, candles=_candles(n=10))  # < MIN_1H_ROWS
    lis1.process_coin(_FakeConn(), "NEWUSDT", _onboard_map("NEWUSDT", 3.5, now), now)
    assert posts == []


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            if "monkeypatch" in getattr(fn, "__code__", None).co_varnames[: fn.__code__.co_argcount]:
                print(f"skip  {fn.__name__} (needs pytest monkeypatch)")
                continue
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed} ok (monkeypatch-Tests nur unter pytest)")
    sys.exit(1 if failed else 0)
