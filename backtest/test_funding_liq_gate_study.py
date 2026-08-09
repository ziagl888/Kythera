# backtest/test_funding_liq_gate_study.py — DB-free tests for the funding×liq gate pilot
"""Synthetic-fixture tests for tools/funding_liq_gate_study.py and
tools/gate_snapshot.py (T-2026-KYT-9050-120). No DB, no snapshot file needed
except the tmp_path DuckDB roundtrip test.

Run standalone (repo convention — backtest suites per file):
  python -m pytest backtest/test_funding_liq_gate_study.py -q
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.funding_liq_gate_study import (  # noqa: E402
    CASCADE_MIN_N_60,
    EXTREME_BPS,
    MIN_SINCE_CAP_MIN,
    MKT_CASCADE_SYMS,
    ROUND_TRIP_FEE,
    attach_funding,
    chrono_halves,
    eval_gate,
    funding_zone,
    gate_masks,
    liq_coverage_days,
    liq_features,
    liq_state,
    prepare_trades,
    restrict_to_liq_window,
)
from tools.gate_snapshot import read_snapshot, write_snapshot  # noqa: E402


def utc(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def make_trades(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["open_time_utc"] = pd.to_datetime(df["open_time_utc"], utc=True).astype("datetime64[ns, UTC]")
    return df


def make_liq(rows: list[tuple[str, str, str, float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "symbol", "side", "value_usdt"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).astype("datetime64[ns, UTC]")
    return df


# ── population ────────────────────────────────────────────────────────────────


def test_prepare_trades_dedup_pnl_and_tz():
    raw = pd.DataFrame(
        {
            "id": [2, 1, 3, 4],
            "symbol": ["AAAUSDT"] * 4,
            "model": ["M"] * 4,
            "direction": ["LONG", "LONG", "SHORT", "LONG"],
            "entry": [100.0, 100.0, 100.0, 100.0],
            "close_price": [110.0, 110.0, 90.0, 105.0],
            # naive wall clock Europe/Bucharest: Jan = EET (+2), Jul = EEST (+3)
            "open_time": [
                pd.Timestamp("2026-01-15 12:00"),
                pd.Timestamp("2026-01-15 12:00"),
                pd.Timestamp("2026-01-15 12:00"),
                pd.Timestamp("2026-07-15 12:00"),
            ],
            "close_time": [pd.NaT] * 4,
            "status": ["CLOSED", "CLOSED", "CLOSED", "ENTRY_NOT_FILLED"],
        }
    )
    out = prepare_trades(raw)
    # duplicate (same symbol/model/direction/open_time) keeps LOWEST id
    longs = out[out["direction"] == "LONG"]
    assert list(longs["id"]) == [1]
    # ENTRY_NOT_FILLED dropped
    assert 4 not in set(out["id"])
    # net PnL: LONG +10% gross − 0.10% fee; SHORT +10% gross − fee
    assert out.loc[out["id"] == 1, "outcome_pct"].iloc[0] == pytest.approx((0.10 - ROUND_TRIP_FEE) * 100)
    assert out.loc[out["id"] == 3, "outcome_pct"].iloc[0] == pytest.approx((0.10 - ROUND_TRIP_FEE) * 100)
    # DST-aware localization: winter naive 12:00 Bucharest = 10:00 UTC
    assert out.loc[out["id"] == 1, "open_time_utc"].iloc[0] == utc("2026-01-15 10:00")


# ── liq features ──────────────────────────────────────────────────────────────


def test_liq_window_counts_direction_mapping_and_no_lookahead():
    t0 = "2026-08-05 12:00:00"
    trades = make_trades(
        [
            {"id": 1, "symbol": "AAAUSDT", "direction": "LONG", "open_time_utc": t0},
            {"id": 2, "symbol": "AAAUSDT", "direction": "SHORT", "open_time_utc": t0},
        ]
    )
    liq = make_liq(
        [
            ("2026-08-05 11:59:00", "AAAUSDT", "SELL", 1000.0),  # long-liq, in 15m window
            ("2026-08-05 11:50:00", "AAAUSDT", "SELL", 2000.0),  # long-liq, in 15m window
            ("2026-08-05 11:10:00", "AAAUSDT", "SELL", 4000.0),  # long-liq, only in 60m window
            ("2026-08-05 11:58:00", "AAAUSDT", "BUY", 500.0),  # short-liq
            ("2026-08-05 12:00:00", "AAAUSDT", "SELL", 9999.0),  # AT entry ts → excluded (no lookahead)
            ("2026-08-05 12:01:00", "AAAUSDT", "SELL", 9999.0),  # after entry → excluded
        ]
    )
    out = liq_features(trades, liq)
    lng = out[out["id"] == 1].iloc[0]
    sht = out[out["id"] == 2].iloc[0]
    # LONG: against = SELL (longs force-closed)
    assert lng["liq_n_against_15m"] == 2
    assert lng["liq_n_against_60m"] == 3
    assert lng["liq_n_with_15m"] == 1
    # SHORT: against = BUY
    assert sht["liq_n_against_15m"] == 1
    assert sht["liq_n_with_60m"] == 3
    # notional against (secondary): 60m sum for LONG = 1000+2000+4000
    assert lng["liq_val_against_60m"] == pytest.approx(7000.0)
    # imbalance sign: LONG sees more against than with → positive
    assert lng["liq_imb_15m"] == pytest.approx((2 - 1) / 3)
    # recency: last event before entry is 11:59 → 1 minute
    assert lng["min_since_liq"] == pytest.approx(1.0)


def test_liq_features_no_events_and_cap():
    trades = make_trades([{"id": 1, "symbol": "BBBUSDT", "direction": "LONG", "open_time_utc": "2026-08-05 12:00:00"}])
    liq = make_liq([("2026-08-01 12:00:00", "AAAUSDT", "SELL", 1.0)])  # other symbol only
    out = liq_features(trades, liq).iloc[0]
    assert out["liq_n_against_15m"] == 0 and out["liq_n_against_60m"] == 0
    assert out["min_since_liq"] == MIN_SINCE_CAP_MIN
    assert np.isnan(out["liq_imb_15m"])  # 0/0 → NaN, not a fake 0 signal


def test_market_breadth_counts_distinct_symbols():
    trades = make_trades([{"id": 1, "symbol": "AAAUSDT", "direction": "LONG", "open_time_utc": "2026-08-05 12:00:00"}])
    liq = make_liq(
        [
            ("2026-08-05 11:50:00", "AAAUSDT", "SELL", 1.0),
            ("2026-08-05 11:51:00", "BBBUSDT", "BUY", 1.0),
            ("2026-08-05 11:52:00", "BBBUSDT", "SELL", 1.0),  # same symbol again — still 2 distinct
            ("2026-08-05 11:20:00", "CCCUSDT", "SELL", 1.0),  # outside 15m
        ]
    )
    assert liq_features(trades, liq).iloc[0]["mkt_syms_15m"] == 2


# ── funding ───────────────────────────────────────────────────────────────────


def test_attach_funding_uses_shared_builder():
    # 22 settled rates (>= MIN_HISTORY=21), 8h grid; last 3 rates before entry
    # are 30/20/10 bps → fund_24h = 20 bps.
    times = pd.date_range("2026-07-01", periods=22, freq="8h", tz="UTC")
    rates = np.full(22, 1e-4)
    rates[-3:] = [30e-4, 20e-4, 10e-4]  # 30/20/10 bps in rate units (bps = rate × 1e4)
    funding = pd.DataFrame({"symbol": "AAAUSDT", "funding_time": times, "funding_rate": rates})
    trades = make_trades(
        [
            {
                "id": 1,
                "symbol": "AAAUSDT",
                "direction": "LONG",
                "open_time_utc": (times[-1] + pd.Timedelta(hours=1)).isoformat(),
            },
            {
                "id": 2,
                "symbol": "ZZZUSDT",
                "direction": "LONG",
                "open_time_utc": (times[-1] + pd.Timedelta(hours=1)).isoformat(),
            },
        ]
    )
    out = attach_funding(trades, funding)
    assert out.loc[out["id"] == 1, "fund_24h"].iloc[0] == pytest.approx(20.0)
    assert np.isnan(out.loc[out["id"] == 2, "fund_24h"].iloc[0])  # unknown symbol → NaN


def test_funding_zone_cuts():
    z = funding_zone(pd.Series([5.0, 3.0, -5.0, 0.0, np.nan]))
    assert list(z) == ["EXTREME_POS", "NEUTRAL", "EXTREME_NEG", "NEUTRAL", "UNKNOWN"]
    assert (z[pd.Series([5.0, 3.0, -5.0, 0.0, np.nan]).abs() == EXTREME_BPS] == "NEUTRAL").all()


# ── states + gates ────────────────────────────────────────────────────────────


def _feature_frame(rows: list[dict]) -> pd.DataFrame:
    defaults = {"liq_n_against_15m": 0, "liq_n_against_60m": 0, "liq_n_with_60m": 0, "mkt_syms_15m": 0, "fund_24h": 0.0}
    return pd.DataFrame([{**defaults, **r} for r in rows])


def test_liq_state_precedence_and_threshold():
    t = _feature_frame(
        [
            {"liq_n_against_60m": CASCADE_MIN_N_60, "liq_n_with_60m": CASCADE_MIN_N_60},  # against wins
            {"liq_n_with_60m": CASCADE_MIN_N_60},
            {"liq_n_against_60m": CASCADE_MIN_N_60 - 1},
        ]
    )
    assert list(liq_state(t)) == ["CASCADE_AGAINST", "CASCADE_WITH", "QUIET"]


def test_h1_gate_requires_crowding_and_cascade():
    t = _feature_frame(
        [
            {"direction": "LONG", "fund_24h": 5.0, "liq_n_against_60m": 3},  # crowded longs + flush → veto
            {"direction": "LONG", "fund_24h": 5.0, "liq_n_against_60m": 0},  # crowded, no cascade
            {"direction": "LONG", "fund_24h": -5.0, "liq_n_against_60m": 3},  # cascade, not crowded for LONG
            {"direction": "SHORT", "fund_24h": -5.0, "liq_n_against_60m": 3},  # crowded shorts + squeeze → veto
            {"direction": "SHORT", "fund_24h": 5.0, "liq_n_against_60m": 3},  # not crowded for SHORT
            {"direction": "LONG", "fund_24h": np.nan, "liq_n_against_60m": 3},  # unknown funding → fail-open
        ]
    )
    h1 = gate_masks(t)["H1 crowded-side flush/squeeze veto"]
    assert list(h1) == [True, False, False, True, False, False]


def test_h3_threshold_is_a_tail_cut():
    # Smoke run 2026-08-09: the market has liquidations printing at all times
    # (median 78 distinct symbols/15 min) — a single-digit breadth cut skips
    # ~100% of entries and the gate is degenerate. Pin the threshold above the
    # observed median so a future "simplification" cannot silently regress it.
    assert MKT_CASCADE_SYMS > 78
    t = _feature_frame(
        [
            {"direction": "LONG", "mkt_syms_15m": MKT_CASCADE_SYMS},
            {"direction": "LONG", "mkt_syms_15m": MKT_CASCADE_SYMS - 1},
        ]
    )
    h3 = gate_masks(t)[f"H3 market-cascade veto (>={MKT_CASCADE_SYMS} syms/15m)"]
    assert list(h3) == [True, False]


# ── evaluation ────────────────────────────────────────────────────────────────


def _eval_frame(n_half: int, skip_loses: bool) -> tuple[pd.DataFrame, pd.Series]:
    """2×n_half LONG trades. Skipped trades lose −5%, kept win +1% — a gate
    that removes exactly the losers. skip_loses=False marks winners instead."""
    rows = []
    for half, day in (("VAL", "2026-08-04"), ("TEST", "2026-08-20")):
        for i in range(n_half):
            skipped = i < n_half // 2
            pnl = -5.0 if skipped else 1.0
            rows.append(
                {"direction": "LONG", "half": half, "outcome_pct": pnl, "skip": skipped, "open_time_utc": utc(day)}
            )
    df = pd.DataFrame(rows)
    skip = df["skip"] if skip_loses else ~df["skip"]
    return df, skip


def test_eval_gate_candidate_when_skipping_losers_both_halves():
    df, skip = _eval_frame(n_half=40, skip_loses=True)
    res = eval_gate(df, skip)
    assert res["LONG"]["candidate"] is True
    assert res["LONG"]["all"]["kept"]["wr"] == 1.0
    assert res["candidate_both_directions"] is False  # no SHORT population at all


def test_eval_gate_rejects_gate_that_skips_winners():
    df, skip = _eval_frame(n_half=40, skip_loses=False)
    assert eval_gate(df, skip)["LONG"]["candidate"] is False


def test_eval_gate_rejects_tiny_skip_count():
    df, skip = _eval_frame(n_half=10, skip_loses=True)  # skips 2×5=10 < MIN_SKIP_N
    assert eval_gate(df, skip)["LONG"]["candidate"] is False


def test_chrono_halves_and_coverage_and_window():
    liq = make_liq(
        [
            ("2026-08-03 00:00:00", "AAAUSDT", "SELL", 1.0),
            ("2026-08-09 00:00:00", "AAAUSDT", "SELL", 1.0),
        ]
    )
    assert liq_coverage_days(liq) == pytest.approx(6.0)
    trades = make_trades(
        [
            {"id": 1, "symbol": "A", "direction": "LONG", "open_time_utc": "2026-08-02 23:00:00"},  # pre-coverage
            {"id": 2, "symbol": "A", "direction": "LONG", "open_time_utc": "2026-08-03 00:30:00"},  # inside warm-up
            {"id": 3, "symbol": "A", "direction": "LONG", "open_time_utc": "2026-08-04 00:00:00"},
            {"id": 4, "symbol": "A", "direction": "LONG", "open_time_utc": "2026-08-08 00:00:00"},
        ]
    )
    kept = restrict_to_liq_window(trades, liq)
    assert list(kept["id"]) == [3, 4]
    halves = chrono_halves(kept)
    assert list(halves) == ["VAL", "TEST"]


# ── snapshot roundtrip ────────────────────────────────────────────────────────


def test_snapshot_roundtrip_preserves_tz_contract(tmp_path):
    path = tmp_path / "snap.duckdb"
    liq = make_liq([("2026-08-05 11:00:00", "AAAUSDT", "SELL", 1.0)])
    trades = pd.DataFrame(
        {
            "id": [1],
            "symbol": ["AAAUSDT"],
            "open_time": [pd.Timestamp("2026-08-05 14:00")],  # NAIVE legacy wall clock
        }
    )
    write_snapshot({"liq": liq, "trades": trades}, path, created_at_utc="2026-08-09T00:00:00+00:00")
    back = read_snapshot(path)
    # tz-aware column comes back UTC-aware ns
    assert str(back["liq"]["ts"].dtype) == "datetime64[ns, UTC]"
    assert back["liq"]["ts"].iloc[0] == utc("2026-08-05 11:00:00")
    # naive column stays naive (never reinterpreted as UTC here)
    assert str(back["trades"]["open_time"].dtype) == "datetime64[ns]"
    assert back["trades"]["open_time"].iloc[0] == pd.Timestamp("2026-08-05 14:00")
    with pytest.raises(KeyError):
        read_snapshot(path, tables=["nope"])
