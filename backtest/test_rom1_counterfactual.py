# backtest/test_rom1_counterfactual.py
"""
Standalone tests for the ROM1 counterfactual scorer (T-2026-CU-9050-047).

DB-free: pure scorer logic (reason buckets, as-of indexing without look-ahead,
horizon capping, skip accounting, aggregation) against a hand-built candle window
and a fake orchestrator object. ROM1 geometry itself is tested in
test_signal_orchestrator.py.

Run: pytest backtest/test_rom1_counterfactual.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")

import pandas as pd  # noqa: E402

import tools.rom1_counterfactual as cf  # noqa: E402


# ── Fake-Orchestrator ───────────────────────────────────────────────────────
class _FakeOrch:
    """Deterministic geometry: LONG entry=price, SL 5% below, TP 5% above.
    Checks that scorer passes price/df correctly and executes the exit."""

    ROM1_PUBLISHED_TARGETS = 3

    def __init__(self):
        self.seen_df_len = None

    def compute_rom1_trade_params(self, conn, coin, direction, price=None, df=None):
        assert conn is None, "Scorer must pass None conn (as-of path, no DB access)"
        assert price is not None and df is not None, "As-of requires price + df"
        self.seen_df_len = len(df)
        if price is None or price <= 0:
            return None
        if direction == "LONG":
            return {"entry1": float(price), "entry2": price * 0.95,
                    "sl": price * 0.95, "targets": [price * 1.05], "leverage": "5x"}
        return {"entry1": float(price), "entry2": price * 1.05,
                "sl": price * 1.05, "targets": [price * 0.95], "leverage": "5x"}


def _frame(n=300, start="2026-01-01"):
    """Flat 1h window; individual candles are manipulated in tests."""
    times = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame({
        "open_time": times,
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1.0,
    })
    return df


# ── parse_reason / forwarded_bucket ─────────────────────────────────────────
def test_parse_reason_splits_whitelist_path():
    assert cf.parse_reason("bot_not_whitelisted:wr_below_overall") == (
        "bot_not_whitelisted:wr_below_overall", "gate")


def test_parse_reason_dedupe_class():
    assert cf.parse_reason("same_direction_open") == ("same_direction_open", "dedupe")
    assert cf.parse_reason("orchestrator_cooldown") == ("orchestrator_cooldown", "dedupe")


def test_parse_reason_plumbing_and_unknown():
    assert cf.parse_reason("bot_unidentified")[1] == "plumbing"
    assert cf.parse_reason("something_new")[1] == "unknown"
    assert cf.parse_reason(None) == ("unknown", "unknown")


def test_forwarded_bucket_null_is_own_bucket():
    assert cf.forwarded_bucket(None) == ("forwarded:wl_reason_missing", "forward")
    assert cf.forwarded_bucket("no_whitelist_entry") == ("forwarded:no_whitelist_entry", "forward")


# ── as_of_index: NO look-ahead (trap R1) ─────────────────────────────────
def test_as_of_index_excludes_forming_candle():
    df = _frame(10)
    ot = df["open_time"].values
    # Signal exactly at open_time of candle 5: this candle is still forming
    # (closes at +1h). The last CLOSED candle is candle 4.
    ts = df["open_time"].iloc[5]
    assert cf.as_of_index(ot, ts) == 4


def test_as_of_index_signal_mid_candle():
    df = _frame(10)
    ot = df["open_time"].values
    ts = df["open_time"].iloc[5] + pd.Timedelta(minutes=30)  # Candle 5 still running
    assert cf.as_of_index(ot, ts) == 4


def test_as_of_index_at_close_boundary():
    df = _frame(10)
    ot = df["open_time"].values
    ts = df["open_time"].iloc[5] + pd.Timedelta(hours=1)  # Candle 5 just closed
    assert cf.as_of_index(ot, ts) == 5


def test_as_of_index_before_data_returns_negative():
    df = _frame(10)
    ot = df["open_time"].values
    ts = df["open_time"].iloc[0] - pd.Timedelta(hours=5)
    assert cf.as_of_index(ot, ts) == -1


def test_as_of_index_accepts_naive_and_aware():
    df = _frame(10)
    ot = df["open_time"].values
    aware = df["open_time"].iloc[5] + pd.Timedelta(hours=1)
    naive = pd.Timestamp(aware).tz_convert("UTC").tz_localize(None)
    assert cf.as_of_index(ot, aware) == cf.as_of_index(ot, naive) == 5


# ── score_row: Entry from closed candle, TP-hit ────────────────────────
def _row(coin="BTCUSDT", direction="LONG", reason="bot_not_whitelisted:wr_below_overall", ts=None):
    return {
        "side": "suppressed", "row_id": 1, "ts": ts, "bot_name": "MIS1-8h",
        "coin": coin, "direction": direction, "regime_at_signal": "BULL/normal",
        "reason": reason, "bucket": cf.parse_reason(reason)[0],
        "bucket_class": cf.parse_reason(reason)[1],
        "original_outbox_id": 42, "recorded_entry": None,
    }


def test_score_row_long_tp_hit():
    df = _frame(300)
    # Entry candle is the last before signal. Then a candle shoots above
    # +5% → TP1 before SL.
    sig_idx = 100
    df.loc[sig_idx + 5, "high"] = 106.0
    orch = _FakeOrch()
    ts = df["open_time"].iloc[sig_idx]  # forming → Entry = candle sig_idx-1 close
    rec = cf.score_row(orch, _row(ts=ts), df, horizon_hours=168)
    assert rec["scored"] is True
    assert rec["entry"] == 100.0
    assert rec["outcome_tp1"] == 1
    assert rec["decision_candle"] == str(pd.Timestamp(df["open_time"].values[sig_idx - 1]))
    # As-of window may only contain past (<= decision candle)
    assert orch.seen_df_len == sig_idx  # Candles 0..sig_idx-1


def test_score_row_sl_first_on_ambiguous_candle():
    df = _frame(300)
    sig_idx = 100
    # Candle with both SL (95) and TP (105) touched → SL-first (conservative)
    df.loc[sig_idx + 3, "low"] = 94.0
    df.loc[sig_idx + 3, "high"] = 106.0
    rec = cf.score_row(_FakeOrch(), _row(ts=df["open_time"].iloc[sig_idx]), df, 168)
    assert rec["outcome_tp1"] == 0


def test_score_row_horizon_caps_scan():
    df = _frame(300)
    sig_idx = 100
    # TP only far after horizon → within 24h neither TP nor SL
    df.loc[sig_idx + 100, "high"] = 106.0
    rec = cf.score_row(_FakeOrch(), _row(ts=df["open_time"].iloc[sig_idx]), df, horizon_hours=24)
    assert rec["scored"] is True
    assert rec["outcome_tp1"] is None          # no label within horizon
    assert rec["exit_reason"] == "open_at_end"
    assert rec["full_horizon"] is True


def test_score_row_insufficient_history():
    df = _frame(300)
    # Signal so early that fewer than MIN_SR_ROWS closed candles before it
    rec = cf.score_row(_FakeOrch(), _row(ts=df["open_time"].iloc[10]), df, 168)
    assert rec["scored"] is False
    assert rec["skip_reason"] == "insufficient_history"


def test_score_row_no_forward_candles():
    df = _frame(300)
    # Signal after close of last candle → decision candle = last,
    # no candle after, no exit simulatable.
    ts = df["open_time"].iloc[299] + pd.Timedelta(hours=2)
    rec = cf.score_row(_FakeOrch(), _row(ts=ts), df, 168)
    assert rec["scored"] is False
    assert rec["skip_reason"] == "no_forward_candles"


def test_score_row_bad_direction():
    df = _frame(300)
    rec = cf.score_row(_FakeOrch(), _row(direction="SIDEWAYS", ts=df["open_time"].iloc[100]), df, 168)
    assert rec["scored"] is False
    assert rec["skip_reason"] == "bad_direction"


def test_score_row_records_entry_drift():
    df = _frame(300)
    sig_idx = 100
    row = _row(ts=df["open_time"].iloc[sig_idx])
    row["recorded_entry"] = 99.0  # Live CMP differs from 1h close (100) by
    rec = cf.score_row(_FakeOrch(), row, df, 168)
    assert rec["entry_drift_pct"] == round((100.0 - 99.0) / 99.0 * 100, 4)


# ── aggregate: buckets, WR without open trades, skip count ──────────────────
def test_aggregate_buckets_and_winrate():
    recs = [
        {"bucket": "gateA", "bucket_class": "gate", "side": "suppressed", "scored": True,
         "outcome_tp1": 1, "net_pnl_pct": 4.0, "r_multiple": 0.8},
        {"bucket": "gateA", "bucket_class": "gate", "side": "suppressed", "scored": True,
         "outcome_tp1": 0, "net_pnl_pct": -5.0, "r_multiple": -1.0},
        {"bucket": "gateA", "bucket_class": "gate", "side": "suppressed", "scored": True,
         "outcome_tp1": None, "net_pnl_pct": 1.0, "r_multiple": None},  # open: no label
        {"bucket": "gateA", "bucket_class": "gate", "side": "suppressed", "scored": False,
         "skip_reason": "insufficient_history"},
    ]
    agg = aggregate_one(recs, "gateA")
    assert agg["n_signals"] == 4
    assert agg["n_scored"] == 3
    assert agg["n_unscorable"] == 1
    assert agg["unscorable_by_reason"] == {"insufficient_history": 1}
    assert agg["n_decided"] == 2
    assert agg["n_open_at_horizon"] == 1
    assert agg["tp1_first_touch_wr"] == 50.0      # 1 of 2 decided
    assert agg["sum_net_pnl_pct"] == 0.0          # 4 - 5 + 1 (the open one counts in PnL)


def test_aggregate_sorted_by_n_desc():
    recs = [
        {"bucket": "small", "bucket_class": "gate", "side": "suppressed", "scored": True,
         "outcome_tp1": 1, "net_pnl_pct": 1.0, "r_multiple": 1.0},
        *[{"bucket": "big", "bucket_class": "dedupe", "side": "suppressed", "scored": True,
           "outcome_tp1": 0, "net_pnl_pct": -1.0, "r_multiple": -1.0} for _ in range(3)],
    ]
    agg = cf.aggregate(recs)
    assert [a["bucket"] for a in agg] == ["big", "small"]


def aggregate_one(recs, bucket):
    return next(a for a in cf.aggregate(recs) if a["bucket"] == bucket)


def test_aggregate_all_open_has_no_winrate():
    recs = [
        {"bucket": "b", "bucket_class": "gate", "side": "suppressed", "scored": True,
         "outcome_tp1": None, "net_pnl_pct": 2.0, "r_multiple": None},
    ]
    agg = aggregate_one(recs, "b")
    assert agg["tp1_first_touch_wr"] is None
    assert agg["n_decided"] == 0
    assert agg["sum_net_pnl_pct"] == 2.0
