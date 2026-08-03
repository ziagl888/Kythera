# backtest/test_lis1_bot.py
"""DB-free tests for the LIS1 shadow forwarder (Bot 36, K5, T-2026-CU-9050-149).

Pins the safety invariants of the rule-based (artifact-less) shadow bot:
  1. core.shadow_gate: LIS1-SHORT is SHADOW, but has NO artifact (the
     forwarder class (D) — rule instead of model); FMR1/other live legs stay live.
  2. core.bot_catalog: tag "LIS1" → 36_ai_lis1_bot.py (report mapping).
  3. in_fade_window: the day-3 trigger fires ONLY within the age window [3d, 4d).
  4. process_coin: emits a SHADOW trade exactly when leg=SHADOW,
     age within the window, no cooldown/open trade, enough candles, targets present —
     and NEVER posts live (only post_shadow_ai_signal).

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

# core.config requires secrets; the build machine supplies an empty .env.
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


# ── 1. shadow_gate: SHADOW leg WITHOUT artifact (forwarder class D) ──────────
def test_lis1_short_is_shadow_but_has_no_artifact():
    assert sg.leg_status("LIS1", "SHORT") == sg.SHADOW
    assert sg.is_shadow("LIS1", "SHORT")
    assert not sg.is_live("LIS1", "SHORT")
    # Rule-based → NO model artifact: the forwarder does not score a pkl.
    assert sg.shadow_artifact_path("LIS1", "SHORT") is None
    assert sg.load_shadow_artifact("LIS1", "SHORT") is None
    # No live leg may be shadowed along with it.
    assert sg.leg_status("FMR1", "SHORT") == sg.LIVE


# ── 2. bot_catalog: tag → script ─────────────────────────────────────────────
def test_lis1_tag_maps_to_bot36():
    assert bc.script_for_tag("LIS1") == "36_ai_lis1_bot.py"
    assert bc.script_for_tag("lis1") == "36_ai_lis1_bot.py"  # case-insensitive


# ── 3. in_fade_window: the day-3 trigger ─────────────────────────────────────
@pytest.mark.parametrize(
    ("age_days", "expected"),
    [
        (2.9, False),  # still before day 3
        (3.0, True),  # exactly day 3 → fires
        (3.5, True),  # within [3d, 4d)
        (3.99, True),
        (4.0, False),  # grace over → no longer (no backfill for old coins)
        (10.0, False),  # listed long ago
    ],
)
def test_in_fade_window_boundaries(age_days, expected):
    now = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    onboard = now - datetime.timedelta(days=age_days)
    assert lis1.in_fade_window(onboard, now) is expected


# ── 4. process_coin: gating + shadow emit (helpers mocked, DB-free) ──────────
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
    """Patch the bot module globals with DB-free fakes; collects shadow posts."""
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
    assert e1 == e2 == 100.0  # market fill (cell l0.0): entry1 == entry2
    assert sl > e1  # SHORT SL sits ABOVE the entry
    assert tgts == (95.0, 90.0, 85.0)


def test_process_coin_skips_old_coin(monkeypatch):
    now = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    posts = _wire(monkeypatch)
    lis1.process_coin(_FakeConn(), "OLDUSDT", _onboard_map("OLDUSDT", 30.0, now), now)
    assert posts == []


def test_process_coin_skips_when_leg_not_shadow(monkeypatch):
    now = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    posts = _wire(monkeypatch, leg=sg.LIVE)  # promoted/live → bot stays silent (fail-safe)
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
    print(f"\n{len(fns) - failed} ok (monkeypatch tests only under pytest)")
    sys.exit(1 if failed else 0)
