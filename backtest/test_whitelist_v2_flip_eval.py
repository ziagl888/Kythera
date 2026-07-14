# backtest/test_whitelist_v2_flip_eval.py
"""
Standalone tests for the v1-vs-v2 whitelist flip evaluation (T-2026-CU-9050-069).

DB-free: the pure classification/aggregation layer (divergence matrix, flip
classification per gate path, snapshot join incl. missing-cell accounting,
drift metric, volume math, reason_v2 parsing) against hand-built snapshots
and events. The replay geometry itself is covered by
test_rom1_counterfactual.py / test_signal_orchestrator.py (one source, AK4).

Run: pytest backtest/test_whitelist_v2_flip_eval.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")

import tools.whitelist_v2_flip_eval as fe  # noqa: E402


def _cell(v1: bool, v2: bool | None, reason_v2: str | None = None) -> dict:
    return {"v1": v1, "reason": "x", "v2": v2, "reason_v2": reason_v2, "computed_at": None}


def _snapshot() -> dict:
    return {
        ("BotA", "CHOP", "ALT_NEUTRAL", "LONG"): _cell(True, True, "v2_pass:lb=0.500:est=0.9:src=cell:neff=55"),
        ("BotA", "CHOP", "ALT_NEUTRAL", "SHORT"): _cell(True, False, "v2_block:lb=-0.200:est=0.1:src=bot_all:neff=25"),
        ("BotB", "CHOP", "ALT_NEUTRAL", "LONG"): _cell(False, True, "v2_pass:lb=0.300:est=0.5:src=bot_regime:neff=40"),
        ("BotB", "CHOP", "ALT_NEUTRAL", "SHORT"): _cell(False, False, "v2_block:lb=-1.000:est=-0.5:src=cell:neff=30"),
        ("BotC", "HIGH_VOLA", "ALT_WEAK", "LONG"): _cell(True, None),
    }


def _event(side: str, v1_path: str, bot="BotA", regime="CHOP", alt="ALT_NEUTRAL", direction="LONG") -> dict:
    return {
        "side": side,
        "bot_name": bot,
        "regime": regime,
        "alt_context": alt,
        "direction": direction,
        "v1_path": v1_path,
        "ts": "2026-07-12 10:00:00",
    }


# ── parse_v2_reason ──────────────────────────────────────────────────────────
def test_parse_v2_reason_roundtrip():
    out = fe.parse_v2_reason("v2_pass:lb=0.123:est=0.456:src=cell:neff=32")
    assert out == {"verdict": "pass", "lb": 0.123, "est": 0.456, "src": "cell", "neff": 32.0}


def test_parse_v2_reason_tolerates_garbage_and_none():
    assert fe.parse_v2_reason(None)["lb"] is None
    out = fe.parse_v2_reason("v2_block:lb=abc:unknown=1")
    assert out["verdict"] == "block" and out["lb"] is None


# ── AK1: divergence matrix ───────────────────────────────────────────────────
def test_divergence_matrix_classes_partition_all_cells():
    m = fe.divergence_matrix(_snapshot())
    assert m["n_cells"] == 5
    assert m["totals"] == {
        fe.BOTH_OPEN: 1,
        fe.V2_WOULD_BLOCK: 1,
        fe.V2_WOULD_OPEN: 1,
        fe.BOTH_BLOCK: 1,
        fe.V2_MISSING: 1,
    }
    assert sum(m["totals"].values()) == m["n_cells"]


def test_divergence_matrix_lb_stats_per_class():
    m = fe.divergence_matrix(_snapshot())
    assert m["lb_stats"][fe.V2_WOULD_BLOCK]["median"] == -0.2
    assert m["lb_stats"][fe.V2_MISSING] is None  # no reason_v2 → no lb


# ── AK2: flip classification per gate path ───────────────────────────────────
def test_classify_forwarded_cell_open_paths_are_affected():
    snap = _snapshot()
    for path in ("wr_above_overall", "counter_trend_specialist", "insufficient_data"):
        res = fe.classify_flip_effect(_event("forwarded", path), snap)
        assert res["affected"] and res["flip_class"] == fe.BOTH_OPEN


def test_classify_forwarded_v2_would_block():
    res = fe.classify_flip_effect(_event("forwarded", "insufficient_data", direction="SHORT"), _snapshot())
    assert res["flip_class"] == fe.V2_WOULD_BLOCK
    assert res["v2_verdict"] is False
    assert res["v2_lb"] == -0.2
    assert res["bucket"] == "v2_would_block:insufficient_data"


def test_classify_suppressed_v2_would_open():
    res = fe.classify_flip_effect(_event("suppressed", "wr_below_overall", bot="BotB"), _snapshot())
    assert res["affected"] and res["flip_class"] == fe.V2_WOULD_OPEN


def test_classify_suppressed_both_block():
    res = fe.classify_flip_effect(
        _event("suppressed", "counter_trend_insufficient", bot="BotB", direction="SHORT"),
        _snapshot(),
    )
    assert res["affected"] and res["flip_class"] == fe.BOTH_BLOCK


def test_classify_fallback_paths_unaffected():
    snap = _snapshot()
    # fallback prefixes = the REAL is_regime_detector_reliable status vocabulary
    # (28_signal_orchestrator.py): no_regime / regime_is_transition / regime_unstable
    for side, path in (
        ("forwarded", "no_whitelist_entry"),
        ("forwarded", "whitelist_stale:fallback_wr_above_50"),
        ("forwarded", "no_regime:fallback_insufficient_data"),
        ("forwarded", "regime_is_transition:fallback_wr_above_50"),
        ("forwarded", ""),  # pre-B8 NULL wl_reason
        ("suppressed", "whitelist_stale:fallback_wr_below_50"),
        ("suppressed", "regime_unstable:fallback_wr_below_50"),
    ):
        res = fe.classify_flip_effect(_event(side, path), snap)
        assert not res["affected"], f"{side}/{path} must be flip-unaffected"
        assert res["flip_class"] == "unaffected"
        assert res["v1_snapshot_agree"] is None  # never enters the drift metric


# ── AK3: missing cell / missing v2 are counted, not dropped ─────────────────
def test_classify_missing_cell_counted():
    res = fe.classify_flip_effect(_event("forwarded", "wr_above_overall", bot="GhostBot"), _snapshot())
    assert not res["affected"]
    assert res["flip_class"] == "cell_missing"
    assert res["skip_reason"] == "cell_missing"


def test_classify_missing_v2_counted():
    res = fe.classify_flip_effect(
        _event("forwarded", "wr_above_overall", bot="BotC", regime="HIGH_VOLA", alt="ALT_WEAK"),
        _snapshot(),
    )
    assert not res["affected"]
    assert res["flip_class"] == fe.V2_MISSING
    assert res["skip_reason"] == "v2_missing"
    # the cell exists, so the v1 drift comparison is still possible
    assert res["v1_snapshot_agree"] is True


def test_classify_normalizes_bot_name():
    # the orchestrator writes pretty_name()-normalized keys; an event carrying
    # a raw variant must still resolve the same cell (pretty_name is idempotent)
    from core.bot_naming import pretty_name

    raw = "BotA"
    assert pretty_name(raw) == "BotA"  # sanity: unknown names pass through
    res = fe.classify_flip_effect(_event("forwarded", "wr_above_overall", bot=raw), _snapshot())
    assert res["affected"]


# ── row→event mappers (the layer where the combined-regime HIGH lived) ──────
def test_suppressed_row_splits_combined_regime_string():
    # log_suppressed (bot 28) writes regime_at_signal = f"{regime}/{alt_context}"
    # from regime_current — the join key must use the SPLIT parts, and the
    # embedded alt beats the regime_history fallback (debounced > RAW, P2.22).
    row = (
        7,
        "2026-07-12 10:00:00",
        "BotA",
        "XYZUSDT",
        "LONG",
        "CHOP/ALT_NEUTRAL",
        "bot_not_whitelisted:wr_below_overall",
        99,
        "ALT_WEAK",
    )
    ev = fe.suppressed_row_to_event(row)
    assert ev["regime"] == "CHOP"
    assert ev["alt_context"] == "ALT_NEUTRAL"  # NOT the history value ALT_WEAK
    assert ev["v1_path"] == "wr_below_overall"
    assert ev["regime_at_signal"] == "CHOP/ALT_NEUTRAL"  # raw value preserved
    # end-to-end: the split key resolves a real snapshot cell
    res = fe.classify_flip_effect(ev, _snapshot())
    assert res["affected"] and res["flip_class"] != "cell_missing"


def test_suppressed_row_without_slash_falls_back_to_history_alt():
    row = (
        8,
        "2026-07-12 10:00:00",
        "BotA",
        "XYZUSDT",
        "LONG",
        "CHOP",
        "bot_not_whitelisted:wr_below_overall",
        99,
        "ALT_NEUTRAL",
    )
    ev = fe.suppressed_row_to_event(row)
    assert ev["regime"] == "CHOP"
    assert ev["alt_context"] == "ALT_NEUTRAL"  # legacy row → history fallback


def test_suppressed_row_null_regime_is_none_not_empty():
    row = (
        9,
        "2026-07-12 10:00:00",
        "BotA",
        "XYZUSDT",
        "LONG",
        None,
        "bot_not_whitelisted:wr_below_overall",
        None,
        None,
    )
    ev = fe.suppressed_row_to_event(row)
    assert ev["regime"] is None and ev["alt_context"] is None


def test_forwarded_row_maps_native_columns():
    row = (3, "2026-07-12 10:00:00", "BotA", "XYZUSDT", "SHORT", "CHOP", "ALT_NEUTRAL", "insufficient_data", 42, 1.25)
    ev = fe.forwarded_row_to_event(row)
    assert ev["regime"] == "CHOP" and ev["alt_context"] == "ALT_NEUTRAL"
    assert ev["v1_path"] == "insufficient_data"
    assert ev["recorded_entry"] == 1.25
    res = fe.classify_flip_effect(ev, _snapshot())
    assert res["flip_class"] == fe.V2_WOULD_BLOCK  # BotA/CHOP/ALT_NEUTRAL/SHORT


# ── AK5: drift metric ────────────────────────────────────────────────────────
def test_drift_counts_only_comparable_events():
    snap = _snapshot()
    events = [
        fe.classify_flip_effect(_event("forwarded", "wr_above_overall"), snap),  # agree (v1 open)
        fe.classify_flip_effect(_event("suppressed", "wr_below_overall"), snap),  # DISagree (v1 now open)
        fe.classify_flip_effect(_event("forwarded", "no_whitelist_entry"), snap),  # excluded
        fe.classify_flip_effect(_event("forwarded", "wr_above_overall", bot="GhostBot"), snap),  # excluded
    ]
    d = fe.drift_rate(events)
    assert d["n_comparable"] == 2
    assert d["n_agree"] == 1
    assert d["agree_pct"] == 50.0


# ── AK6: volume math ─────────────────────────────────────────────────────────
def test_volume_rates_and_projection():
    events = (
        [{"flip_class": fe.BOTH_OPEN, "side": "forwarded"}] * 6
        + [{"flip_class": fe.V2_WOULD_BLOCK, "side": "forwarded"}] * 2
        + [{"flip_class": fe.V2_WOULD_OPEN, "side": "suppressed"}] * 1
        + [{"flip_class": fe.BOTH_BLOCK, "side": "suppressed"}] * 1
        + [{"flip_class": "unaffected", "side": "forwarded"}] * 8
        # undecidable forwarded traffic DID forward under v1 → constant baseline
        # in both projections, excluded from the open-rate comparison
        + [{"flip_class": "cell_missing", "side": "forwarded"}] * 1
        + [{"flip_class": fe.V2_MISSING, "side": "forwarded"}] * 1
    )
    v = fe.volume_effect(events, window_days=2.0)
    assert v["n_cell_decided"] == 10
    assert v["v1_open_rate_pct"] == 80.0  # (6+2)/10
    assert v["v2_open_rate_pct"] == 70.0  # (6+1)/10
    # per day: v1 = (8 cell-open + 8 unaffected + 2 undecidable fwd)/2 = 9.0
    assert v["forwarded_per_day_v1"] == 9.0
    assert v["forwarded_per_day_v2_projected"] == 8.5


def test_volume_empty_is_safe():
    v = fe.volume_effect([], window_days=0.0)
    assert v["v1_open_rate_pct"] is None and v["n_events_total"] == 0


# ── AK8: daily counts (outage visibility) ────────────────────────────────────
def test_daily_counts_groups_by_utc_day_and_side():
    events = [
        {"ts": "2026-07-12 10:00:00", "side": "forwarded"},
        {"ts": "2026-07-12 11:00:00", "side": "suppressed"},
        {"ts": "2026-07-13 09:00:00", "side": "forwarded"},
    ]
    dc = fe.daily_counts(events)
    assert dc["2026-07-12"] == {"forwarded": 1, "suppressed": 1}
    assert dc["2026-07-13"] == {"forwarded": 1, "suppressed": 0}


# ── AK4: geometry/replay is delegated to the 047 scorer, not rebuilt ─────────
def test_reuses_047_scorer():
    import tools.rom1_counterfactual as cf

    assert fe.score_row is cf.score_row
    assert fe.load_1h is cf.load_1h
    assert fe.aggregate is cf.aggregate
    src = open(fe.__file__, encoding="utf-8").read()
    for forbidden in ("INSERT", "UPDATE ", "DELETE FROM"):  # AK7 read-only
        assert forbidden not in src, f"write statement found: {forbidden}"


# ── portfolio comparison ─────────────────────────────────────────────────────
def test_portfolio_comparison_sums_by_selection():
    records = [
        {"scored": True, "flip_class": fe.BOTH_OPEN, "net_pnl_pct": 2.0},
        {"scored": True, "flip_class": fe.V2_WOULD_BLOCK, "net_pnl_pct": -1.0},
        {"scored": True, "flip_class": fe.V2_WOULD_OPEN, "net_pnl_pct": 3.0},
        {"scored": True, "flip_class": fe.BOTH_BLOCK, "net_pnl_pct": -5.0},  # in neither portfolio
        {"scored": False, "flip_class": fe.BOTH_OPEN, "net_pnl_pct": None},
    ]
    pc = fe.portfolio_comparison(records)
    assert pc["v1_selection"] == {"n_scored": 2, "sum_net_pnl_pct": 1.0}
    assert pc["v2_selection"] == {"n_scored": 2, "sum_net_pnl_pct": 5.0}
    assert pc["v2_removes"]["sum_net_pnl_pct"] == -1.0  # v2 removes a loser → good
    assert pc["v2_adds"]["sum_net_pnl_pct"] == 3.0
    assert pc["delta_sum_net_pnl_pct"] == 4.0
