"""DB-free guards for the PCL1 shadow leg (T-2026-KYT-9050-146).

Pins the T-145 candidate-cell contract (trigger, geometry, 24h exit), the
shadow-only register state, the runner hosting, and the additive
expiry_hours/lev extension of post_shadow_ai_signal. No DB, no network.

Run: python -m pytest backtest/test_pcl1_shadow.py -q
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import shadow_gate as sg  # noqa: E402
from core import shadow_scanners as scn  # noqa: E402
from core.bot_catalog import script_for_tag  # noqa: E402
from core.signal_post import post_shadow_ai_signal  # noqa: E402

_spec = importlib.util.spec_from_file_location("PCL1_BOT", ROOT / "47_ai_pcl1_bot.py")
pcl = importlib.util.module_from_spec(_spec)
sys.modules["PCL1_BOT"] = pcl
_spec.loader.exec_module(pcl)


# ---------------------------------------------------------------- register


def test_pcl1_leg_is_shadow_and_never_live():
    assert sg.leg_status("PCL1", "LONG") == sg.SHADOW
    assert sg.is_shadow("PCL1", "LONG")
    assert not sg.is_live("PCL1", "LONG")
    # Case-normalised lookup (leg_status _norm()s).
    assert sg.leg_status("pcl1", "long") == sg.SHADOW


def test_pcl1_is_hosted_by_the_shadow_scanner_runner_hourly_41():
    specs = [s for s in scn.SCANNERS if s.script == "47_ai_pcl1_bot.py"]
    assert len(specs) == 1
    assert specs[0].cadence == scn.HOURLY
    assert specs[0].minute == 41
    assert specs[0].logger_name == "PCL1_BOT"
    assert "47_ai_pcl1_bot.py" in scn.HOSTED_SCRIPTS


def test_pcl1_tag_resolves_to_bot_47_in_the_catalog():
    assert script_for_tag("PCL1") == "47_ai_pcl1_bot.py"


# ---------------------------------------------------------------- contract


def test_trigger_matches_the_t145_candidate_cell():
    # dpx_24h >= 75% on the implied price, boundary inclusive.
    assert pcl.PUMP_PCT == 75.0
    assert pcl.is_pump(1.75, 1.0)  # exactly +75%
    assert not pcl.is_pump(1.7499, 1.0)
    assert not pcl.is_pump(1.75, 0.0)  # zero/garbage past price => no event
    assert pcl.EXPIRY_HOURS == 24  # the study's hard 24h time exit
    assert pcl.COOLDOWN_HOURS == 24  # study dedupe window


def test_geometry_is_sl25_with_single_capped_target():
    sl, targets = pcl.trade_geometry(100.0)
    assert sl == 75.0  # 25% below entry — the candidate band's lower edge
    assert targets == [150.0]  # ONE +50% winner-cap target (documented divergence)


# ------------------------------------------------- signal_post extension


class _Cursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    def fetchone(self):
        return None  # has_open_ai_signal: no open trade

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self):
        self.calls = []

    def cursor(self):
        return _Cursor(self.calls)


def _insert_call(conn):
    return next(c for c in conn.calls if "INSERT INTO ai_signals" in c[0])


def test_post_shadow_ai_signal_writes_expiry_and_lev_when_given():
    conn = _Conn()
    ok = post_shadow_ai_signal(
        conn, "PCL1", "AAAUSDT", "LONG", 0.5, 100.0, 100.0, 75.0, [150.0],
        n_show=1, expiry_hours=24, lev="10x",
    )
    assert ok
    sql, params = _insert_call(conn)
    assert "expiry_hours" in sql and "lev" in sql
    assert params[-2] == 24 and params[-1] == "10x"
    assert json.loads(params[-3]) == [150.0]


def test_post_shadow_ai_signal_defaults_keep_null_expiry_and_lev():
    # Additive contract: existing callers (no kwargs) must land NULL/NULL —
    # the monitor then behaves exactly as before the extension.
    conn = _Conn()
    ok = post_shadow_ai_signal(conn, "LIS1", "AAAUSDT", "SHORT", 0.5, 100.0, 100.0, 105.0, [95.0])
    assert ok
    _sql, params = _insert_call(conn)
    assert params[-2] is None and params[-1] is None


# ------------------------------------------- behavioural fail-safe (M3)


def _fresh_candles(minutes_old: float = 3.0, close: float = 100.0):
    import datetime

    import pandas as pd

    now = datetime.datetime.now(datetime.timezone.utc)
    t1 = now - datetime.timedelta(minutes=minutes_old)
    return pd.DataFrame({"open_time": [t1 - datetime.timedelta(minutes=5), t1], "close": [close, close]})


def _wire(monkeypatch, *, leg=None, cooldown=False, has_open=False, candles="fresh"):
    """Patch bot-module globals with DB-free fakes; collect shadow posts."""
    posts: list[tuple] = []
    monkeypatch.setattr(pcl, "shadow_posting_enabled", lambda: True)
    monkeypatch.setattr(pcl, "leg_status", lambda *_: leg if leg is not None else sg.SHADOW)
    monkeypatch.setattr(pcl, "check_cooldown", lambda *a, **k: cooldown)
    monkeypatch.setattr(pcl, "has_open_ai_signal", lambda *a, **k: has_open)
    use_fresh = isinstance(candles, str) and candles == "fresh"
    monkeypatch.setattr(pcl, "read_candles", lambda *a, **k: _fresh_candles() if use_fresh else candles)
    monkeypatch.setattr(pcl, "update_cooldown", lambda *a, **k: None)

    def _post(conn, tag, sym, direction, conf, e1, e2, sl, tgts, **kw):
        posts.append((tag, sym, direction, e1, sl, tuple(tgts), kw))
        return True

    monkeypatch.setattr(pcl, "post_shadow_ai_signal", _post)
    return posts


def test_process_symbol_posts_the_candidate_cell_with_expiry_and_lev(monkeypatch):
    posts = _wire(monkeypatch)
    pcl.process_symbol(object(), "ACEUSDT")
    assert len(posts) == 1
    tag, sym, direction, e1, sl, tgts, kw = posts[0]
    assert (tag, sym, direction) == ("PCL1", "ACEUSDT", "LONG")
    assert e1 == 100.0 and sl == 75.0 and tgts == (150.0,)
    assert kw["expiry_hours"] == 24 and kw["lev"] == "10x"


def test_process_symbol_stays_silent_when_leg_is_not_shadow(monkeypatch):
    # THE fail-safe: a promoted/reconfigured leg must silence the bot, never
    # turn it into a live poster.
    posts = _wire(monkeypatch, leg=sg.LIVE)
    pcl.process_symbol(object(), "ACEUSDT")
    assert posts == []


def test_process_symbol_skips_on_cooldown_and_open_trade(monkeypatch):
    posts = _wire(monkeypatch, cooldown=True)
    pcl.process_symbol(object(), "ACEUSDT")
    assert posts == []
    posts2 = _wire(monkeypatch, has_open=True)
    pcl.process_symbol(object(), "ACEUSDT")
    assert posts2 == []


def test_process_symbol_voids_on_missing_or_stale_entry_candle(monkeypatch):
    posts = _wire(monkeypatch, candles=None)  # missing feed => void (P0.12)
    pcl.process_symbol(object(), "ACEUSDT")
    assert posts == []
    # Stale feed (older than MAX_STALE_MIN): the OI trigger may still fire
    # while the candle ingest is dead — no hours-old close as entry.
    posts2 = _wire(monkeypatch, candles=_fresh_candles(minutes_old=pcl.MAX_STALE_MIN + 5))
    pcl.process_symbol(object(), "ACEUSDT")
    assert posts2 == []


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("OK — PCL1 shadow-leg contracts hold")
