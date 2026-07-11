# backtest/test_window_features.py — T-2026-CU-9050-084 (P1.12), DB-free.
#
# Guards the "window-global as-of-now" change on both sides of the seam:
#
#   WRITE  (2_indicator_engine.calculate_indicators_optimized): the window-global
#          indicators (one trendline/channel fit, one HVN/POC histogram, one S/R
#          pivot scan, one Fibonacci range) are kept ONLY on the newest CLOSED bar
#          and NULLed (NaN / None) on the forming bar and every older bar. Before
#          the fix they were broadcast onto every row — a bar from thousands of
#          candles ago carried today's POC/support (look-ahead in stored history).
#
#   READ   (the S/R serving readers): each reads the level from the bar that still
#          carries it (the newest closed / reference bar), not a fixed positional
#          index that a NULLed row would poison. strat_5_percent / strat_fast_in_out
#          read the forming bar (iloc[0]) for their per-bar checks and would read
#          NaN S/R without the fix; strat_support_resistance / strat_main_channel
#          read iloc[1] and must stay on the value-bearing bar even if the forming
#          bar is absent.
#
# Every test below fails on the pre-fix code. Run: python backtest/test_window_features.py

import importlib.util
import os
import sys

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# Offline cred shim: the engine hard-requires these at import (it never connects here).
for _k, _v in {
    "DB_PASSWORD": "test", "TELEGRAM_BOT_TOKEN": "test",
    "DB_HOST": "127.0.0.1", "DB_NAME": "t", "DB_USER": "t", "DB_PORT": "5432",
}.items():
    os.environ.setdefault(_k, _v)


def _load_engine():
    """Import the digit-prefixed engine module by path (mirrors the guard)."""
    path = os.path.join(REPO_ROOT, "2_indicator_engine.py")
    spec = importlib.util.spec_from_file_location("kythera_indicator_engine", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENGINE = _load_engine()

WINDOW_GLOBAL_NUMERIC = list(ENGINE._WINDOW_GLOBAL_NUMERIC_COLS)
WINDOW_GLOBAL_TEXT = list(ENGINE._WINDOW_GLOBAL_TEXT_COLS)
WINDOW_GLOBAL_ALL = WINDOW_GLOBAL_NUMERIC + WINDOW_GLOBAL_TEXT
# Genuinely per-bar columns — the change must NEVER touch these.
PER_BAR_COLS = ["RSI_14", "EMA_21", "ATR_14", "MACD_DIF_NORMAL_12_26_9", "DONCHIAN_UPPER_20"]


# ── synthetic OHLCV ───────────────────────────────────────────────────────────
def _synth_ohlcv(n, tf, last_open):
    """n deterministic bars of `tf` ending at `last_open` (UTC Timestamp)."""
    step = ENGINE.get_timeframe_delta(tf)
    times = [last_open - step * (n - 1 - i) for i in range(n)]
    rng = np.random.default_rng(4)
    close = 100.0 * np.exp(np.cumsum(rng.standard_normal(n) * 0.01))
    high = close * (1.0 + np.abs(rng.standard_normal(n)) * 0.004)
    low = close * (1.0 - np.abs(rng.standard_normal(n)) * 0.004)
    open_ = np.concatenate([[close[0]], close[:-1]])
    vol = rng.uniform(10.0, 1000.0, n)
    return pd.DataFrame({
        "open_time": pd.to_datetime(times, utc=True),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": vol, "symbol": "TESTUSDT",
    })


def _assert_window_global_only_on(ind, ref_open, label):
    """Every window-global column is present on the ref bar and NULL everywhere else."""
    ot = pd.to_datetime(ind["open_time"], utc=True)
    ref = (ot == ref_open).to_numpy()
    assert ref.sum() == 1, f"{label}: expected exactly one reference bar, got {ref.sum()}"

    for col in WINDOW_GLOBAL_NUMERIC:
        vals = pd.to_numeric(ind[col], errors="coerce")
        assert vals[ref].notna().all(), f"{label}: window-global {col} missing on the reference bar"
        # The pre-fix broadcast leaves these non-NaN on every bar → this line fails there.
        assert vals[~ref].isna().all(), (
            f"{label}: window-global {col} still set on non-reference bars (look-ahead / broadcast not stripped)"
        )
    for col in WINDOW_GLOBAL_TEXT:
        assert ind[col][ref].notna().all(), f"{label}: {col} missing on the reference bar"
        assert ind[col][~ref].isna().all(), f"{label}: {col} still set on non-reference bars"


def test_engine_all_historical_reference_is_last_bar():
    """A frame lying entirely in the past: every bar is closed, so the reference
    is the last bar. Deterministic regardless of when the test runs — this is why
    the regression-guard golden stays stable."""
    ind = ENGINE.calculate_indicators_optimized(_synth_ohlcv(250, "1h", pd.Timestamp("2024-03-01", tz="UTC")), "1h")
    ref_open = pd.to_datetime(ind["open_time"], utc=True).max()
    _assert_window_global_only_on(ind, ref_open, "historical")
    # Per-bar indicators must be untouched (present on the body, not stripped).
    for col in PER_BAR_COLS:
        body = pd.to_numeric(ind[col], errors="coerce").iloc[50:]
        assert body.notna().any(), f"per-bar {col} was wrongly NULLed by the window-global strip"
    print("OK  engine: historical frame — window-global only on the last (newest closed) bar")


def test_engine_forming_bar_is_nulled_reference_is_newest_closed():
    """A frame whose last bar is the currently-forming candle: the reference must be
    the newest CLOSED bar, and the forming bar must be NULL. This is the Regel-5
    guarantee — the value lives on a closed candle that the serving readers read."""
    tf = "1h"
    forming_open = ENGINE.period_start(tf, ENGINE.utc_now())
    step = ENGINE.get_timeframe_delta(tf)
    ind = ENGINE.calculate_indicators_optimized(_synth_ohlcv(250, tf, forming_open), tf)
    ot = pd.to_datetime(ind["open_time"], utc=True)

    _assert_window_global_only_on(ind, forming_open - step, "forming")
    # Explicitly: the forming bar itself carries no window-global value (Regel 5).
    forming = (ot == forming_open).to_numpy()
    assert forming.sum() == 1, "forming bar not present in the frame"
    for col in WINDOW_GLOBAL_NUMERIC:
        assert pd.to_numeric(ind[col], errors="coerce")[forming].isna().all(), (
            f"forming bar carries window-global {col} — Regel 5 violated"
        )
    print("OK  engine: forming bar NULLed, window-global on the newest CLOSED bar (Regel 5)")


# ── reader guards: strat_5_percent / strat_fast_in_out (DB-free evaluate_conditions) ──
def _long_pass_row():
    """A per-bar indicator row that satisfies every non-S/R LONG condition of both
    5-Percent and Fast-In-Out (close=100)."""
    return {
        "close": 100.0,
        "rsi_9": 65.0, "rsi_14": 65.0,
        "tsi_fast_12_7_7": 20.0, "tsi_fast_12_7_7_signal": 10.0,
        "ema_9": 99.0, "ema_12": 98.0, "ema_21": 97.0, "ema_26": 96.0,
        "ema_55": 90.0, "ema_89": 89.0, "ema_200": 88.0,
        "wma_9": 98.0, "wma_12": 97.0, "wma_21": 93.0, "wma_26": 92.0,
        "kama_9": 91.0, "kama_12": 90.5, "kama_21": 90.2,
        "macd_dif_fast_9_21_9": 1.0, "macd_dea_fast_9_21_9": 0.5,
        "donchian_mid_4": 95.0, "boll_mid_20": 96.0, "atr_14": 1.5,
    }


def _desc_frame_forming_null_ref(sr_support, sr_resistance):
    """DESC frame (iloc[0]=forming with NaN S/R, iloc[1]=reference carrying S/R)."""
    forming = {**_long_pass_row(), "support_price": np.nan, "resistance_price": np.nan}
    ref = {**_long_pass_row(), "support_price": sr_support, "resistance_price": sr_resistance}
    older = {**_long_pass_row(), "support_price": np.nan, "resistance_price": np.nan}
    return pd.DataFrame([forming, ref, older])


def test_strat_5pct_reads_reference_row_not_forming():
    import strategies.strat_5_percent as s5

    # Reference S/R gives LONG headroom (close 100 < resistance*0.95, >= support*0.999).
    df = _desc_frame_forming_null_ref(sr_support=98.0, sr_resistance=110.0)
    assert s5.evaluate_conditions(df, "LONG") is True, (
        "5-Percent did not read S/R from the reference bar (pre-fix reads the forming bar's NaN → False)"
    )
    # Canary: a reference resistance with no headroom must veto — proves the level is really used.
    df_veto = _desc_frame_forming_null_ref(sr_support=98.0, sr_resistance=100.0)
    assert s5.evaluate_conditions(df_veto, "LONG") is False, "5-Percent ignored the reference resistance level"
    print("OK  strat_5_percent: S/R read from the reference bar, not the forming bar")


def test_strat_fast_reads_reference_row_not_forming():
    import strategies.strat_fast_in_out as sf

    df = _desc_frame_forming_null_ref(sr_support=98.0, sr_resistance=110.0)
    assert sf.evaluate_conditions(df, "LONG") is True, (
        "Fast-In-Out did not read resistance from the reference bar (pre-fix reads the forming bar's NaN → False)"
    )
    df_veto = _desc_frame_forming_null_ref(sr_support=98.0, sr_resistance=100.0)
    assert sf.evaluate_conditions(df_veto, "LONG") is False, "Fast-In-Out ignored the reference resistance level"
    print("OK  strat_fast_in_out: resistance read from the reference bar, not the forming bar")


# ── reader guards: strat_support_resistance / strat_main_channel (analyze_coin) ──
class _Sentinel(Exception):
    pass


def _sr_gate_frame():
    """A 60-bar DESC frame where the newest bar (iloc[0]) is the reference carrying
    the S/R level (a 'forming-absent' snapshot), and every older bar has NaN S/R.
    Reading iloc[1] (pre-fix) sees NaN → no hit; reading the reference (post-fix)
    hits support. Older bars sit inside the support hit-zone so the first-hit scan
    proceeds to the DB read, which we intercept."""
    n = 60
    idx = pd.date_range("2024-03-01", periods=n, freq="h", tz="UTC")[::-1]  # DESC
    rows = []
    for i in range(n):
        rows.append({
            "support_price": 100.0 if i == 0 else np.nan,
            "resistance_price": 110.0 if i == 0 else np.nan,
            "close": 100.5 if i == 0 else 100.3,  # all in [100, 100.75] support zone
            "rsi_9": 60.0, "rsi_14": 60.0,
        })
    return pd.DataFrame(rows, index=idx)


def _run_analyze_reaches_db(analyze_fn):
    """Returns True if analyze_fn reached the OHLCV DB read (i.e. an S/R hit fired),
    False if it returned None first. read_sql_query is stubbed to raise _Sentinel."""
    df = _sr_gate_frame()
    orig = pd.read_sql_query

    def _boom(*a, **k):
        raise _Sentinel

    pd.read_sql_query = _boom
    try:
        result = analyze_fn(object(), "TESTUSDT", df, 100.5)
        return False, result
    except _Sentinel:
        return True, None
    finally:
        pd.read_sql_query = orig


def test_strat_support_resistance_reads_reference_row():
    import strategies.strat_support_resistance as sr

    reached, result = _run_analyze_reaches_db(sr.analyze_coin)
    assert reached, (
        "Support/Resistance did not hit on the reference bar's S/R "
        "(pre-fix reads iloc[1]=NaN → no hit → None before the DB read)"
    )
    print("OK  strat_support_resistance: S/R hit fires off the reference bar")


def test_strat_main_channel_reads_reference_row():
    import strategies.strat_main_channel as mc

    reached, result = _run_analyze_reaches_db(mc.analyze_coin)
    assert reached, (
        "Main Channel did not hit on the reference bar's S/R "
        "(pre-fix reads iloc[1]=NaN → no hit → None before the DB read)"
    )
    print("OK  strat_main_channel: S/R hit fires off the reference bar")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    test_engine_all_historical_reference_is_last_bar()
    test_engine_forming_bar_is_nulled_reference_is_newest_closed()
    test_strat_5pct_reads_reference_row_not_forming()
    test_strat_fast_reads_reference_row_not_forming()
    test_strat_support_resistance_reads_reference_row()
    test_strat_main_channel_reads_reference_row()
    print("\nAlle P1.12 Window-Feature-Guards bestanden (Engine-Schreibseite + 4 S/R-Reader).")
