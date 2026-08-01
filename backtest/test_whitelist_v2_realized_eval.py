"""Standalone tests for tools/whitelist_v2_realized_eval.py (T-2026-KYT-9050-007).

DB-free: every function under test is pure. Run directly:

    python backtest/test_whitelist_v2_realized_eval.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
# The import chain reaches core.config, which hard-fails on a missing secret.
# Placeholders keep this file runnable on the build machine (no .env) — nothing
# here opens a connection (same convention as test_whitelist_v2_flip_eval.py).
os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")

from tools.whitelist_v2_realized_eval import (  # noqa: E402
    V2_WOULD_BLOCK,
    V2_WOULD_OPEN,
    TwinIndex,
    _claim_nearest,
    agreement_split,
    attach_rom1_legs,
    attach_trigger_legs,
    by_bot_direction,
    flip_delta,
    nearest_index,
    path_breakdown,
    pick_domain,
    realized_from_ai,
    realized_from_classic,
    summarize_legs,
)

# Naive on purpose: every timestamp this tool compares comes from a
# TIMESTAMP WITHOUT TIME ZONE column (core/time.py — naive wall clock, UTC or
# LEGACY_WRITER_TZ). An aware fixture would not exercise the real code path.
T0 = datetime(2026, 7, 20, 12, 0, 0)  # noqa: DTZ001


# ── nearest_index ────────────────────────────────────────────────────────────
def test_nearest_index_empty():
    assert nearest_index([], T0) is None


def test_nearest_index_picks_closest():
    stamps = [T0 - timedelta(minutes=10), T0 - timedelta(minutes=1), T0 + timedelta(minutes=5)]
    assert nearest_index(stamps, T0) == 1


def test_nearest_index_clamps_both_ends():
    stamps = [T0, T0 + timedelta(hours=1)]
    assert nearest_index(stamps, T0 - timedelta(days=1)) == 0
    assert nearest_index(stamps, T0 + timedelta(days=1)) == 1


def test_nearest_index_exact_hit():
    stamps = [T0 - timedelta(hours=1), T0, T0 + timedelta(hours=1)]
    assert nearest_index(stamps, T0) == 1


# ── pick_domain ──────────────────────────────────────────────────────────────
def test_pick_domain_prefers_more_matches():
    assert pick_domain(0, 50) == "legacy"
    assert pick_domain(50, 0) == "utc"


def test_pick_domain_ties_and_zeros_go_to_utc():
    assert pick_domain(0, 0) == "utc"
    assert pick_domain(7, 7) == "utc"


# ── realized leg builders ────────────────────────────────────────────────────
def test_realized_from_ai_win_uses_staged_move():
    row = {
        "status": "TARGET HIT",
        "targets_hit": 2,
        "direction": "LONG",
        "entry": 100.0,
        "close_price": 100.0,
        "targets": [110.0, 120.0, 130.0, 140.0],
        "lev": "10x",
    }
    leg = realized_from_ai(row)
    assert leg["outcome"] == "win"
    assert leg["staffed"] is True
    # (10 + 20 + 0 + 0) / 4 = 7.5 % unlevered, x10 leverage
    assert abs(leg["move"] - 7.5) < 1e-9
    assert abs(leg["lev_pnl"] - 75.0) < 1e-9


def test_realized_from_ai_loss_short():
    row = {
        "status": "SL0",
        "targets_hit": 0,
        "direction": "SHORT",
        "entry": 100.0,
        "close_price": 105.0,
        "targets": [95.0],
        "lev": "5",
    }
    leg = realized_from_ai(row)
    assert leg["outcome"] == "loss"
    assert abs(leg["move"] - (-5.0)) < 1e-9
    assert abs(leg["lev_pnl"] - (-25.0)) < 1e-9


def test_realized_from_ai_censored_carries_no_pnl():
    leg = realized_from_ai(
        {"status": "DELISTED / CLEANUP", "targets_hit": 0, "direction": "LONG", "entry": 1.0, "close_price": 2.0}
    )
    assert leg["outcome"] == "censored"
    assert leg["move"] is None and leg["lev_pnl"] is None


def test_realized_from_classic_win_from_status_index():
    row = {
        "status": "2",
        "direction": "LONG",
        "entry": 100.0,
        "close_price": 100.0,
        "target1": 110.0,
        "target2": 120.0,
        "target3": None,
        "target4": 0,
        "lev": "10x",
    }
    leg = realized_from_classic(row)
    assert leg["outcome"] == "win"
    # two published targets, both hit -> (10 + 20)/2 = 15 %
    assert abs(leg["move"] - 15.0) < 1e-9


def test_realized_from_classic_loss():
    row = {
        "status": "0",
        "direction": "LONG",
        "entry": 100.0,
        "close_price": 95.0,
        "target1": 110.0,
        "target2": None,
        "target3": None,
        "target4": None,
        "lev": None,
    }
    leg = realized_from_classic(row)
    assert leg["outcome"] == "loss"
    assert abs(leg["move"] - (-5.0)) < 1e-9
    assert leg["lev_pnl"] is None  # missing leverage -> excluded, never guessed


# ── summarize_legs ───────────────────────────────────────────────────────────
def _ev(tag, direction, leg, ts=T0):
    return {"tag": tag, "direction": direction, "trigger": leg, "ts": ts}


def test_summarize_legs_counts_missing_legs_separately():
    win = {"outcome": "win", "move": 2.0, "staffed": True, "lev_pnl": 20.0, "r": None}
    events = [_ev("A", "LONG", win), _ev("A", "LONG", None)]
    s = summarize_legs(events, "trigger")
    assert s["n_events"] == 2
    assert s["n_with_leg"] == 1
    assert s["n_no_leg"] == 1
    assert s["leg_coverage_pct"] == 50.0
    assert s["n_decided"] == 1


def test_summarize_legs_empty_population():
    s = summarize_legs([], "trigger")
    assert s["n_events"] == 0 and s["n_with_leg"] == 0 and s["leg_coverage_pct"] is None


def test_by_bot_direction_splits_and_sorts():
    big = {"outcome": "win", "move": 9.0, "staffed": False, "lev_pnl": None, "r": None}
    small = {"outcome": "loss", "move": -1.0, "staffed": False, "lev_pnl": None, "r": None}
    rows = by_bot_direction([_ev("A", "LONG", big), _ev("B", "SHORT", small)], "trigger")
    assert [(r["tag"], r["direction"]) for r in rows] == [("A", "LONG"), ("B", "SHORT")]


def test_path_breakdown_splits_crutch_from_merit():
    """The cell matrix and the traffic can disagree about what v2 changes."""
    win = {"outcome": "win", "move": 1.0, "staffed": False, "lev_pnl": None, "r": None}
    loss = {"outcome": "loss", "move": -1.0, "staffed": False, "lev_pnl": None, "r": None}
    events = [
        {**_ev("A", "LONG", loss), "bucket": "v2_would_block:wr_above_overall"},
        {**_ev("B", "LONG", loss), "bucket": "v2_would_block:wr_above_overall"},
        {**_ev("C", "LONG", win), "bucket": "v2_would_block:insufficient_data"},
    ]
    rows = path_breakdown(events, "trigger")
    assert [r["v1_path"] for r in rows] == ["wr_above_overall", "insufficient_data"]
    assert rows[0]["stats"]["n_events"] == 2
    assert rows[0]["stats"]["sum_move_pct"] == -2.0
    assert rows[1]["stats"]["sum_move_pct"] == 1.0


def test_agreement_split_separates_drifted_events():
    """A moved cell compares two v1 states, not v1 against v2 — keep it apart."""
    win = {"outcome": "win", "move": 3.0, "staffed": False, "lev_pnl": None, "r": None}
    loss = {"outcome": "loss", "move": -1.0, "staffed": False, "lev_pnl": None, "r": None}
    events = [
        {**_ev("A", "LONG", win), "v1_snapshot_agree": True},
        {**_ev("B", "LONG", loss), "v1_snapshot_agree": False},
        {**_ev("C", "LONG", loss), "v1_snapshot_agree": None},
    ]
    split = agreement_split(events, "trigger")
    assert split["v1_agree"]["n_events"] == 1
    assert split["v1_agree"]["sum_move_pct"] == 3.0
    assert split["v1_drifted"]["n_events"] == 1
    assert split["v1_drifted"]["sum_move_pct"] == -1.0
    assert split["n_unknown"] == 1


def test_path_breakdown_handles_missing_bucket():
    rows = path_breakdown([_ev("A", "LONG", None)], "trigger")
    assert rows[0]["v1_path"] == "unknown"


# ── flip_delta ───────────────────────────────────────────────────────────────
def test_flip_delta_sign_convention():
    per_class = {
        V2_WOULD_BLOCK: {"n_decided": 3, "sum_move_pct": 6.0, "net_mean_pct": 1.9},
        V2_WOULD_OPEN: {"n_decided": 2, "sum_move_pct": -4.0, "net_mean_pct": -2.1},
    }
    d = flip_delta(per_class)
    assert d["v2_removes_sum_move_pct"] == 6.0
    assert d["v2_adds_sum_move_pct"] == -4.0
    # v2 gives up +6 and buys -4 -> the flip is worth -10 on this population
    assert d["delta_sum_move_pct"] == -10.0


def test_flip_delta_missing_classes_are_zero_not_none():
    d = flip_delta({})
    assert d["v2_removes_sum_move_pct"] == 0.0
    assert d["v2_adds_sum_move_pct"] == 0.0
    assert d["delta_sum_move_pct"] == 0.0


# ── claim / twin matching ────────────────────────────────────────────────────
def test_claim_nearest_never_reuses_a_trade():
    row = {"source": "ai"}
    e1, e2 = {}, {}
    n, coll = _claim_nearest([(120.0, 1, e2, row), (5.0, 0, e1, row)])
    assert n == 1 and coll == 1
    assert "_claim" in e1 and "_claim" not in e2  # the closer event wins


def _close(tag, coin, direction, ts_utc, ts_legacy, status="SL0", entry=100.0, close=95.0):
    return {
        "tag": tag,
        "coin": coin,
        "direction": direction,
        "ts_utc": ts_utc,
        "ts_legacy": ts_legacy,
        "status": status,
        "targets_hit": 0,
        "targets": [110.0],
        "entry": entry,
        "close_price": close,
        "lev": "10x",
        "source": "ai",
    }


def test_twin_index_detects_legacy_domain():
    """Rows stamped +3h (Europe/Bucharest) must still match their gate events."""
    stored = T0 + timedelta(hours=3)  # what the bot wrote into the naive column
    closes = [_close("EPD3", "BTCUSDT", "LONG", stored, T0)]
    events = [{"side": "suppressed", "tag": "EPD3", "coin": "BTCUSDT", "direction": "LONG", "ts": T0}]
    idx = TwinIndex(closes)
    idx.calibrate(events, 600)
    assert idx.domain_by_tag["EPD3"] == "legacy"
    assert idx.domain_stats["EPD3"]["matched_as_utc"] == 0
    assert idx.domain_stats["EPD3"]["matched_as_legacy"] == 1
    stats = attach_trigger_legs(events, idx, 600)
    assert stats["n_matched"] == 1
    assert events[0]["trigger"]["outcome"] == "loss"


def test_twin_index_detects_utc_domain():
    closes = [_close("ROM1", "BTCUSDT", "LONG", T0, T0 - timedelta(hours=3))]
    events = [{"side": "forwarded", "tag": "ROM1", "coin": "BTCUSDT", "direction": "LONG", "ts": T0}]
    idx = TwinIndex(closes)
    idx.calibrate(events, 600)
    assert idx.domain_by_tag["ROM1"] == "utc"


def test_attach_trigger_legs_marks_unmatched():
    closes = [_close("EPD3", "BTCUSDT", "LONG", T0 + timedelta(days=5), T0 + timedelta(days=5))]
    events = [{"side": "suppressed", "tag": "EPD3", "coin": "BTCUSDT", "direction": "LONG", "ts": T0}]
    idx = TwinIndex(closes)
    idx.calibrate(events, 600)
    stats = attach_trigger_legs(events, idx, 600)
    assert stats["n_matched"] == 0
    assert events[0]["trigger"] is None
    assert events[0]["trigger_skip"] == "no_twin"


def test_attach_rom1_legs_only_touches_forwarded():
    closes = [_close("ROM1", "BTCUSDT", "LONG", T0, T0 - timedelta(hours=3))]
    events = [
        {"side": "forwarded", "tag": "SR", "coin": "BTCUSDT", "direction": "LONG", "ts": T0},
        {"side": "suppressed", "tag": "SR", "coin": "BTCUSDT", "direction": "LONG", "ts": T0},
    ]
    idx = TwinIndex(closes)
    idx.calibrate([{"tag": "ROM1", "coin": "BTCUSDT", "direction": "LONG", "ts": T0}], 120)
    stats = attach_rom1_legs(events, idx, 120)
    assert stats["n_forwarded"] == 1 and stats["n_matched"] == 1
    assert events[0]["rom1"] is not None
    assert events[1]["rom1"] is None and events[1]["rom1_skip"] == "not_forwarded"


def test_suppressed_side_never_gets_a_rom1_leg():
    """Structural asymmetry: a blocked signal was never traded as ROM1."""
    closes = [_close("ROM1", "BTCUSDT", "LONG", T0, T0)]
    events = [{"side": "suppressed", "tag": "SR", "coin": "BTCUSDT", "direction": "LONG", "ts": T0}]
    idx = TwinIndex(closes)
    stats = attach_rom1_legs(events, idx, 120)
    assert stats["n_forwarded"] == 0
    assert events[0]["rom1"] is None


def _run() -> int:
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:  # noqa: BLE001 — standalone runner
            failed += 1
            print(f"  FAIL {name}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
