# backtest/test_rom1_counterfactual.py
"""
Standalone-Tests für den ROM1 Counterfactual-Scorer (T-2026-CU-9050-047).

DB-frei: die reine Scorer-Logik (Reason-Buckets, as-of-Indexierung ohne
Look-ahead, Horizont-Kappung, Skip-Accounting, Aggregation) gegen ein
handgebautes Kerzen-Fenster und ein Fake-Orchestrator-Objekt. Die
ROM1-Geometrie selbst ist in test_signal_orchestrator.py getestet.

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
    """Deterministische Geometrie: LONG entry=price, SL 5% drunter, TP 5% drüber.
    Prüft, dass der Scorer price=/df= korrekt durchreicht und den Exit fährt."""

    ROM1_PUBLISHED_TARGETS = 3

    def __init__(self):
        self.seen_df_len = None

    def compute_rom1_trade_params(self, conn, coin, direction, price=None, df=None):
        assert conn is None, "Scorer muss None-conn übergeben (As-of-Pfad, kein DB-Zugriff)"
        assert price is not None and df is not None, "As-of erfordert price + df"
        self.seen_df_len = len(df)
        if price is None or price <= 0:
            return None
        if direction == "LONG":
            return {"entry1": float(price), "entry2": price * 0.95,
                    "sl": price * 0.95, "targets": [price * 1.05], "leverage": "5x"}
        return {"entry1": float(price), "entry2": price * 1.05,
                "sl": price * 1.05, "targets": [price * 0.95], "leverage": "5x"}


def _frame(n=300, start="2026-01-01"):
    """Flaches 1h-Fenster; einzelne Kerzen werden im Test gezielt manipuliert."""
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


# ── as_of_index: KEIN Look-ahead (Falle R1) ─────────────────────────────────
def test_as_of_index_excludes_forming_candle():
    df = _frame(10)
    ot = df["open_time"].values
    # Signal exakt zur open_time von Kerze 5: diese Kerze ist noch forming
    # (schließt erst bei +1h). Die letzte GESCHLOSSENE ist Kerze 4.
    ts = df["open_time"].iloc[5]
    assert cf.as_of_index(ot, ts) == 4


def test_as_of_index_signal_mid_candle():
    df = _frame(10)
    ot = df["open_time"].values
    ts = df["open_time"].iloc[5] + pd.Timedelta(minutes=30)  # Kerze 5 läuft noch
    assert cf.as_of_index(ot, ts) == 4


def test_as_of_index_at_close_boundary():
    df = _frame(10)
    ot = df["open_time"].values
    ts = df["open_time"].iloc[5] + pd.Timedelta(hours=1)  # Kerze 5 gerade geschlossen
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


# ── score_row: Entry aus geschlossener Kerze, TP-Hit ────────────────────────
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
    # Entry-Kerze ist die letzte vor dem Signal. Danach schießt eine Kerze über
    # +5% → TP1 vor SL.
    sig_idx = 100
    df.loc[sig_idx + 5, "high"] = 106.0
    orch = _FakeOrch()
    ts = df["open_time"].iloc[sig_idx]  # forming → Entry = Kerze sig_idx-1 Close
    rec = cf.score_row(orch, _row(ts=ts), df, horizon_hours=168)
    assert rec["scored"] is True
    assert rec["entry"] == 100.0
    assert rec["outcome_tp1"] == 1
    assert rec["decision_candle"] == str(pd.Timestamp(df["open_time"].values[sig_idx - 1]))
    # As-of-Fenster darf nur Vergangenheit enthalten (<= Entscheidungskerze)
    assert orch.seen_df_len == sig_idx  # Kerzen 0..sig_idx-1


def test_score_row_sl_first_on_ambiguous_candle():
    df = _frame(300)
    sig_idx = 100
    # Kerze mit sowohl SL (95) als auch TP (105) berührt → SL-first (konservativ)
    df.loc[sig_idx + 3, "low"] = 94.0
    df.loc[sig_idx + 3, "high"] = 106.0
    rec = cf.score_row(_FakeOrch(), _row(ts=df["open_time"].iloc[sig_idx]), df, 168)
    assert rec["outcome_tp1"] == 0


def test_score_row_horizon_caps_scan():
    df = _frame(300)
    sig_idx = 100
    # TP erst weit nach dem Horizont → innerhalb 24h weder TP noch SL
    df.loc[sig_idx + 100, "high"] = 106.0
    rec = cf.score_row(_FakeOrch(), _row(ts=df["open_time"].iloc[sig_idx]), df, horizon_hours=24)
    assert rec["scored"] is True
    assert rec["outcome_tp1"] is None          # kein Label innerhalb des Horizonts
    assert rec["exit_reason"] == "open_at_end"
    assert rec["full_horizon"] is True


def test_score_row_insufficient_history():
    df = _frame(300)
    # Signal so früh, dass weniger als MIN_SR_ROWS geschlossene Kerzen davor liegen
    rec = cf.score_row(_FakeOrch(), _row(ts=df["open_time"].iloc[10]), df, 168)
    assert rec["scored"] is False
    assert rec["skip_reason"] == "insufficient_history"


def test_score_row_no_forward_candles():
    df = _frame(300)
    # Signal nach dem Close der letzten Kerze → Entscheidungskerze = letzte,
    # keine Kerze danach, kein Exit simulierbar.
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
    row["recorded_entry"] = 99.0  # Live-CMP wich vom 1h-Close (100) ab
    rec = cf.score_row(_FakeOrch(), row, df, 168)
    assert rec["entry_drift_pct"] == round((100.0 - 99.0) / 99.0 * 100, 4)


# ── aggregate: Buckets, WR ohne offene Trades, Skip-Zählung ─────────────────
def test_aggregate_buckets_and_winrate():
    recs = [
        {"bucket": "gateA", "bucket_class": "gate", "side": "suppressed", "scored": True,
         "outcome_tp1": 1, "net_pnl_pct": 4.0, "r_multiple": 0.8},
        {"bucket": "gateA", "bucket_class": "gate", "side": "suppressed", "scored": True,
         "outcome_tp1": 0, "net_pnl_pct": -5.0, "r_multiple": -1.0},
        {"bucket": "gateA", "bucket_class": "gate", "side": "suppressed", "scored": True,
         "outcome_tp1": None, "net_pnl_pct": 1.0, "r_multiple": None},  # offen: kein Label
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
    assert agg["tp1_first_touch_wr"] == 50.0      # 1 von 2 entschiedenen
    assert agg["sum_net_pnl_pct"] == 0.0          # 4 - 5 + 1 (offener zählt in PnL)


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
