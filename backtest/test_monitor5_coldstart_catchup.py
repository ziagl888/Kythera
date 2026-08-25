"""Standalone (DB-free) guards for the cold-start catch-up in `5_trade_monitor.py`
(T-2026-KYT-9050-152).

Bot 5 was the twin left behind: AUDIT_TODO P2.7 noted for BOTH monitors that "the
watermark is lost on a process restart", T-2026-KYT-9050-150 fixed only bot 8, and
bot 5 kept scoring every open trade against a single 5m candle after each restart.
The shared mechanism now lives in `core/monitor_catchup.py` and is pinned by
`test_monitor_catchup_shared.py`; this file pins what is specific to bot 5.

Three of these guard decisions that are easy to "clean up" into bugs:

  (a) `posted` stays WALL CLOCK. Bot 8 stamps `closed_ai_signals.close_time` from
      the triggering candle — the obvious move is to mirror that here. It would
      be wrong: `closed_trades_master` has no close-time column, and per the
      comment in `close_trade` other scripts compare `posted` against
      `datetime.now() - timedelta` to decide whether a trade closed recently. A
      replayed backlog would write timestamps a whole downtime in the past and
      those freshness checks would skip the rows.

  (b) The state file must NOT be shared with bot 8. Both monitors persist a
      watermark; one file would mean each process clobbers the other's, and the
      one that restarts second would replay from a foreign timestamp.

  (c) Bot 5 emits nothing — the same invariant that makes a replay safe in bot 8.

Run: python backtest/test_monitor5_coldstart_catchup.py
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_PASSWORD", "test-stub")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-stub")

_spec = importlib.util.spec_from_file_location("trade_monitor", str(ROOT / "5_trade_monitor.py"))
assert _spec and _spec.loader
mon5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mon5)

SRC5 = (ROOT / "5_trade_monitor.py").read_text(encoding="utf-8")
SRC8 = (ROOT / "8_ai_trade_monitor.py").read_text(encoding="utf-8")


def _calls_to(source: str, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


# ── the helpers are wired, not merely imported ────────────────────────────────
def test_catchup_floor_is_wired_into_both_call_sites():
    """`coin_min_wm` decides how many candles are FETCHED per coin, the per-trade
    branch which of them SCORE. Wiring only the second filters a one-candle result
    set and silently does nothing — the original bug, but green."""
    calls = _calls_to(SRC5, "_catchup_floor")
    assert len(calls) >= 2, f"_catchup_floor is called {len(calls)}x - both call sites are required"


def test_disarm_gate_is_wired_into_the_poll_loop():
    calls = _calls_to(SRC5, "_should_disarm_catchup")
    assert len(calls) == 1, f"_should_disarm_catchup is called {len(calls)}x - expected exactly the poll loop"


def test_shared_helpers_come_from_core_not_a_local_copy():
    """The whole point of T-152: bot 5 and bot 8 drifted once already. A local
    re-inline here would let them drift again."""
    assert "from core.monitor_catchup import" in SRC5
    for name in ("_resolve_catchup", "_catchup_floor", "_should_disarm_catchup"):
        assert f"def {name}" not in SRC5, f"{name} must come from core, not be redefined here"


# ── (a) the deliberate asymmetry to bot 8 ─────────────────────────────────────
def test_posted_stays_wall_clock():
    """Bot 8 stamps close_time from the candle; bot 5 must NOT do the same to
    `posted`, because other scripts read it as a freshness signal."""
    assert "now = datetime.datetime.now(datetime.timezone.utc)" in SRC5, (
        "close_trade must keep stamping `posted` with wall-clock UTC"
    )
    assert "_close_timestamp" not in SRC5, (
        "bot 8's candle-derived close stamp must not be ported here - a replayed "
        "backlog would backdate `posted` past the freshness checks that read it"
    )


def test_bot8_does_stamp_from_the_candle():
    """The other half of the asymmetry, so this file fails if someone 'aligns'
    the two monitors in either direction without reading why they differ."""
    assert "close_ts = _close_timestamp(c_ot, open_time)" in SRC8


# ── (b) separate state files ──────────────────────────────────────────────────
def test_state_file_is_not_shared_with_bot8():
    import importlib

    mon8_spec = importlib.util.spec_from_file_location("ai_trade_monitor", str(ROOT / "8_ai_trade_monitor.py"))
    assert mon8_spec and mon8_spec.loader
    mon8 = importlib.util.module_from_spec(mon8_spec)
    mon8_spec.loader.exec_module(mon8)
    assert mon5.MONITOR_STATE_FILE != mon8.MONITOR_STATE_FILE, (
        "both monitors persist a watermark - one shared file means each process "
        "clobbers the other's and the second to restart replays from a foreign timestamp"
    )


def test_state_file_is_gitignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert os.path.basename(mon5.MONITOR_STATE_FILE) in ignored, "runtime state must never be committed"


# ── (c) the no-emission invariant the replay rests on ─────────────────────────
def _code_identifiers(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
    return {n.lower() for n in names}


def test_monitor5_emits_nothing():
    """`close_trade` documents itself as "silent - no Telegram", but a docstring
    is not evidence. Checked at the executed code."""
    identifiers = _code_identifiers(SRC5)
    for needle in ("telegram", "send_message", "signal_post", "outbox", "cornix", "bot.send"):
        hits = sorted(n for n in identifiers if needle in n)
        assert not hits, (
            f"{hits} appeared in executed code of the monitor - the cold-start "
            "catch-up must be re-evaluated before this ships (late close orders)"
        )


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
