"""Standalone (DB-free) guards for the gap filler's scan window and startup pass
(T-2026-KYT-9050-155).

Why this exists: on 2026-08-24 the 72h ExecutionTimeLimit kill took data ingestion
down and left a candle gap at 06:00-08:00 UTC. The gap filler exists to repair
exactly that and did not, for two independent reasons — both fixed here and both
pinned below:

  (a) It scanned a hardcoded 24h. By the time the next nightly run came around the
      gap was older than that, so it was invisible — permanently, since every
      later run is even further away.

  (b) It ran only in the 03:00 UTC branch. The 2026-08-25 12:56 restart, which is
      precisely when the gap existed and precisely when someone should have looked,
      did not run it.

The window must therefore cover everything since the last SUCCESSFUL run, floored
at the old 24h (never regress) and capped (never unbounded — the filler walks ~524
coins x N timeframes with a REST fetch per gap on a CPU-tight VPS).

Run: python backtest/test_gap_filler_window.py
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_PASSWORD", "test-stub")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-stub")

_spec = importlib.util.spec_from_file_location("housekeeping", str(ROOT / "6_housekeeping.py"))
assert _spec and _spec.loader
hk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hk)

SRC = (ROOT / "6_housekeeping.py").read_text(encoding="utf-8")

UTC = dt.timezone.utc
# The incident, to the minute.
LAST_GOOD_RUN = dt.datetime(2026, 8, 24, 3, 0, tzinfo=UTC)  # last nightly run before the kill
GAP_AT = dt.datetime(2026, 8, 24, 6, 0, tzinfo=UTC)  # first missing candle
RESTART_AT = dt.datetime(2026, 8, 25, 12, 56, tzinfo=UTC)  # fleet came back


def _iso(d: dt.datetime) -> str:
    return d.isoformat()


# ── (a) the scan window ───────────────────────────────────────────────────────
def test_incident_gap_is_inside_the_resolved_window():
    """The regression this task exists for: at the restart, the window must reach
    back past the first missing candle. A fixed 24h did not."""
    hours, level, _msg = hk.resolve_gap_scan_hours(_iso(LAST_GOOD_RUN), RESTART_AT)
    reaches_back_to = RESTART_AT - dt.timedelta(hours=hours)
    assert reaches_back_to < GAP_AT, (
        f"window reaches back only to {reaches_back_to:%Y-%m-%d %H:%M}, "
        f"the gap starts at {GAP_AT:%Y-%m-%d %H:%M} — it would be missed again"
    )
    assert level == "info"


def test_old_fixed_window_would_have_missed_it():
    """Pins that the bug was real, so the test above cannot silently become vacuous."""
    reaches_back_to = RESTART_AT - dt.timedelta(hours=24)
    assert reaches_back_to > GAP_AT, "if a fixed 24h already covered the gap, this task is pointless"


def test_no_watermark_falls_back_to_the_old_default():
    hours, level, msg = hk.resolve_gap_scan_hours(None, RESTART_AT)
    assert hours == hk.GAP_SCAN_MIN_HOURS
    assert level == "info"
    assert "no previous-run watermark" in msg


def test_window_never_shrinks_below_the_old_default():
    """A short hop since the last run must not scan less than before."""
    hours, _level, _msg = hk.resolve_gap_scan_hours(_iso(RESTART_AT - dt.timedelta(hours=2)), RESTART_AT)
    assert hours == hk.GAP_SCAN_MIN_HOURS


def test_normal_nightly_cadence_adds_only_the_margin():
    hours, _level, _msg = hk.resolve_gap_scan_hours(_iso(RESTART_AT - dt.timedelta(hours=24)), RESTART_AT)
    assert hours == 24.0 + hk.GAP_SCAN_MARGIN_HOURS


def test_long_absence_is_capped_and_loud():
    hours, level, msg = hk.resolve_gap_scan_hours(_iso(RESTART_AT - dt.timedelta(hours=400)), RESTART_AT)
    assert hours == hk.GAP_SCAN_MAX_HOURS, "an unbounded scan would hammer a CPU-tight VPS"
    assert level == "warning", "and silently dropping older gaps would repeat the T-154 failure mode"
    assert "stay unrepaired" in msg


def test_degenerate_watermarks_degrade_to_the_default():
    for junk in ("not-a-timestamp", "", 12345, "2026-13-45T99:99:99"):
        hours, _level, _msg = hk.resolve_gap_scan_hours(junk, RESTART_AT)
        assert hours == hk.GAP_SCAN_MIN_HOURS, f"{junk!r} must not change the window"


def test_future_watermark_is_ignored():
    hours, level, msg = hk.resolve_gap_scan_hours(_iso(RESTART_AT + dt.timedelta(hours=5)), RESTART_AT)
    assert hours == hk.GAP_SCAN_MIN_HOURS
    assert level == "warning"
    assert "future" in msg


def test_naive_watermark_is_read_as_utc():
    naive = LAST_GOOD_RUN.replace(tzinfo=None).isoformat()
    aware = hk.resolve_gap_scan_hours(_iso(LAST_GOOD_RUN), RESTART_AT)[0]
    assert hk.resolve_gap_scan_hours(naive, RESTART_AT)[0] == aware


# ── (b) the startup pass ──────────────────────────────────────────────────────
def test_startup_runs_after_a_missed_nightly_run():
    """The 2026-08-25 restart case: this is the whole point of the startup pass."""
    run, why = hk.should_gap_fill_on_start(_iso(LAST_GOOD_RUN), RESTART_AT)
    assert run is True
    assert "missed" in why


def test_ordinary_restart_stays_cheap():
    run, why = hk.should_gap_fill_on_start(_iso(RESTART_AT - dt.timedelta(hours=3)), RESTART_AT)
    assert run is False, "the filler walks ~524 coins x N timeframes — not on every restart"
    assert "nothing missed" in why


def test_first_deploy_does_not_surprise_the_operator():
    run, why = hk.should_gap_fill_on_start(None, RESTART_AT)
    assert run is False
    assert "no watermark" in why


def test_unreadable_watermark_does_not_trigger_a_startup_scan():
    run, _why = hk.should_gap_fill_on_start("garbage", RESTART_AT)
    assert run is False


# ── source-level wiring ───────────────────────────────────────────────────────
def _calls_to(name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(ast.parse(SRC))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def test_both_call_sites_go_through_run_gap_filler():
    """Startup pass and nightly branch. A helper nobody calls is a green test over
    a live bug — and the nightly site is the one that regressed here."""
    assert len(_calls_to("run_gap_filler")) == 2


def test_the_hardcoded_24h_call_is_gone():
    """The filler must not be invoked directly with a fixed window any more —
    that call is what made an older gap permanently invisible."""
    direct = _calls_to("fill_ohlcv_gaps_and_invalidate_indicators")
    assert len(direct) == 1, "expected exactly the one call inside run_gap_filler"
    assert "fill_ohlcv_gaps_and_invalidate_indicators(scan_hours=24)" not in SRC


def test_success_watermark_is_recorded():
    assert "_record_gap_success" in SRC
    assert len(_calls_to("_record_gap_success")) == 1


# ── the credibility gate on the watermark ─────────────────────────────────────
def test_a_run_where_everything_failed_does_not_advance_the_watermark():
    """Per-coin errors are swallowed inside the filler, so a run with Binance
    unreachable still returns normally and logs "no gaps found". Trusting that
    would shrink the next window and lose a real gap for good — precisely the
    failure class this task fixes."""
    assert hk.should_record_gap_success({"pairs_attempted": 3144, "errors": 3144}) is False


def test_a_clean_run_advances_the_watermark():
    assert hk.should_record_gap_success({"pairs_attempted": 3144, "errors": 0}) is True


def test_a_few_bad_coins_still_count_as_a_run():
    """One delisted or rate-limited coin must not stall the watermark forever."""
    assert hk.should_record_gap_success({"pairs_attempted": 3144, "errors": 12}) is True


def test_missing_or_empty_summary_is_not_trusted():
    for junk in (None, {}, {"pairs_attempted": 0, "errors": 0}):
        assert hk.should_record_gap_success(junk) is False, f"{junk!r} must not advance the watermark"


def test_watermark_write_is_gated_not_unconditional():
    """Source guard: the record call must sit behind the credibility check."""
    assert len(_calls_to("should_record_gap_success")) == 1
    gated = SRC.split("if should_record_gap_success(summary):")[1][:200]
    assert "_record_gap_success(now_utc)" in gated


def test_state_file_is_gitignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert os.path.basename(hk.GAP_FILL_STATE_FILE) in ignored, "runtime state must never be committed"


def test_docstring_no_longer_claims_the_legacy_tables():
    """That stale claim produced a wrong 'this safety net is dead' diagnosis during
    the incident. It cost debugging time; it must not come back."""
    doc = hk.fill_ohlcv_gaps_and_invalidate_indicators.__doc__ or ""
    assert "core.candles" in doc
    assert "KYTHERA_CANDLES_SOURCE" in doc


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
