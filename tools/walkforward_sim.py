"""
tools/walkforward_sim.py — shared walk-forward simulator (Audit P0.10 / P0.11).

Purpose
-------
Replays the BOT-OWN setup functions bar-by-bar over historical candles and
scores the resulting trades with a wick-aware first-touch forward scan (like
the new monitor after the P2.7 fix), fees included (P3.6). The resulting trade
records are the LABEL SOURCE for all retraining — NOT closed_ai_signals
(historically only 63.4% correctly scored, Report 17).

Core principles (X-R1 fix):
  * Setup detection imports bot modules or their extracted setup functions —
    no copy-paste skew.
  * Order geometry = exactly the posted geometry: CMP entry +
    calculate_smart_targets (df-window variant, same function as live) or
    the bot's own SL/TP rules (UFI1).
  * Decisions only on CLOSED candles until the decision point.
  * Exits: first-touch on the 1h candles AFTER the decision, wick-aware,
    SL-first in case of ambiguity (TP and SL in the same candle), trailing
    semantics like 8_ai_trade_monitor (from TP2 onwards, SL moves to
    targets[k-2]).
  * Fees: 0.05% per side (taker, configurable) → 0.10% round-trip.

Strategies
----------
  ufi1   — 29_ufi1_bot.find_ufi1_setup on daily candles (P0.11 validation:
           the "+278R" from fib_backtest.py must be removed)
  td     — three-drive detection from 25_smc_ml_sniper.scan_market (1h+4h)
  bb     — breaker-block detection from 25_smc_ml_sniper.scan_market (1h+4h)
  abr1   — break & retest detection from 18_ai_abr1_bot (1h)
  mis1   — dense sample per closed 1h candle (no detector gate), features from
           core.mis_features (shared builder, leakage fix), labels capped
           72h/168h — retrain priority #1 (Report 16)

Operating rules (Live VPS!)
---------------------------
  * Process lowers itself to BELOW_NORMAL.
  * Before start, system CPU is checked (>90% → abort so the new
    core/health_monitor CPU_SATURATED alarm is not triggered).
  * DB strictly read-only (SELECT only); results go as JSONL files to
    Documents\\_X\\staging_models\\replay\\ (no new tables).

Examples
--------
  python tools/walkforward_sim.py --strategy ufi1 --days 365
  python tools/walkforward_sim.py --strategy td --tf 1h --days 540
  python tools/walkforward_sim.py --strategy bb --tf 4h --days 540 --limit 50
  python tools/walkforward_sim.py --strategy abr1 --days 365
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import scipy.signal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.candles import read_candles, read_candles_with_indicators  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.market_utils import load_coins as _core_load_coins  # noqa: E402
from core.mis_features import (  # noqa: E402
    FEATURE_COLS as MIS1_FEATURE_COLS,
)
from core.mis_features import (
    LEGACY_ONLY_COLS as MIS1_LEGACY_COLS,
)
from core.mis_features import (
    MIS_INDICATOR_COLUMNS,
    MIS_RENAME_MAP,
)
from core.mis_features import (
    add_advanced_features as mis1_add_features,
)
from core.funding_features import funding_features_asof, load_funding  # noqa: E402
from core.ats_features import (  # noqa: E402
    ATS_CANDLE_COLUMNS,
    ATS_INDICATOR_COLUMNS,
    TSI_LINE_COL,
    TSI_SIGNAL_COL,
    ats_cross,
    build_ats_features,
)
from core.rub_features import build_rub_features, rub_event_type, rub_trend  # noqa: E402
from core import atb2_features as atb  # noqa: E402
from core.time import epoch_seconds, utc_now  # noqa: E402
from core.trade_utils import (  # noqa: E402
    calculate_smart_targets,
    compute_smart_target_levels,
    ensure_min_tp_distance,
    get_hvn_and_sr_levels,
    hvn_sr_trade_geometry,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
FEE_PER_SIDE = 0.0005  # Taker 0.05% per side → 0.10% round-trip (P3.6)
DEFAULT_OUT_DIR = os.getenv(
    "KYTHERA_REPLAY_DIR", r"C:\Users\Michael\Documents\_X\staging_models\replay"
)
MAX_CPU_AT_START = 90.0  # do not trigger health_monitor CPU_SATURATED

# How many TPs the respective bot actually publishes (Cornix message) —
# determines position fractionation in ladder exit.
PUBLISHED_TARGETS = {"ufi1": 1, "td": 5, "bb": 5, "abr1": 3, "mis1": 5, "rub": 3, "atb2": 3, "ats": 3}

# ATS/TSI (Bot 12): live, the bot reads the LATEST 500 closed 1h candles
# and normalizes OBV to the window start — the replay must pass the same 500-candle
# window through. Warmup 100 days covers both the 500-candle OBV window
# (~21 d) and the 95d S/R level pool, so EVERY event in the requested
# timeframe has a complete window (OBV-baseline parity, hard rule 7).
ATS_WARMUP_DAYS = 100
ATS_FEATURE_WINDOW = 500   # Bot 12: read_candles_with_indicators(limit=500)
ATS_SR_WINDOW_H = 95 * 24  # get_hvn_and_sr_levels uses 95 days of 1h candles
ATS_MIN_HISTORY = 50       # Bot-12 floor: `if len(df) < 50: continue`

# ATB2 (§11): warmup large enough so EMA200 converges before 1st event
# (MIN_HISTORY_CANDLES=1500 candles ≈ 62.5 days → 65d buffer); cooldown per
# direction like the other breakout bots.
ATB2_WARMUP_DAYS = 65
ATB2_COOLDOWN_H = 4


def set_low_priority() -> None:
    """The VPS runs at the load limit — we run with BELOW_NORMAL.

    psutil is not installed in the live venv → ctypes fallback directly to WinAPI
    (BELOW_NORMAL_PRIORITY_CLASS = 0x4000).
    """
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        return
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Without explicit argtypes, SetPriorityClass fails on 64-bit
        # (HANDLE is passed as c_int) — so declared cleanly here.
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetPriorityClass.restype = wintypes.BOOL
        ok = k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000)
        print("Process priority: BELOW_NORMAL" if ok else "WARNING: SetPriorityClass failed")
    except Exception:
        print("WARNING: Priority could not be lowered")


def check_cpu_headroom() -> None:
    try:
        import psutil

        cpu = psutil.cpu_percent(interval=3)
        if cpu > MAX_CPU_AT_START:
            raise SystemExit(
                f"ABORT: System CPU at {cpu:.0f}% (> {MAX_CPU_AT_START:.0f}%) — "
                f"do not add load to fleet (Audit Z0 / CPU_SATURATED)."
            )
        print(f"CPU check ok: {cpu:.0f}%")
    except SystemExit:
        raise
    except Exception:
        print("CPU check skipped (psutil not available)")


def import_bot_module(filename: str, module_name: str):
    """Imports a bot module with digit-prefix filename (e.g. 29_ufi1_bot.py)."""
    path = os.path.join(REPO_ROOT, filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────────────────────────────────────────────────────────
# DATEN (read-only)
# ─────────────────────────────────────────────────────────────────────────────
def load_coins() -> list[str]:
    # P3.1: read/dict-unwrap/USDT-filter/symbol-validation via the canon.
    return _core_load_coins(os.path.join(REPO_ROOT, "coins.json"), usdt_only=True, uppercase=True)


OHLCV_COLUMNS = ("open_time", "open", "high", "low", "close", "volume")


def _window_start(days: int) -> datetime:
    """Lower window boundary. Aware UTC via core.time (R3 policy) — the upper
    cut at the forming candle is handled DB-side in core.candles."""
    return utc_now() - timedelta(days=int(days))


def load_ohlcv(conn, symbol: str, tf: str, days: int) -> pd.DataFrame | None:
    """OHLCV window, ASC, CLOSED candles (R1 discipline).

    Via core.candles instead of raw f-string SQL: the cutoff there is epoch
    arithmetic on the DB clock and thus correct for EVERY timeframe. The
    neighbour loaders cut with `date_trunc('hour', NOW())` — for the 1d and
    4h reads of this simulator, that would be too coarse and leave the running
    candle standing. Look-ahead here poisons the labels of the entire retrain
    program.
    """
    try:
        df = read_candles(
            conn, symbol, tf, start=_window_start(days), include_forming=False, columns=OHLCV_COLUMNS
        )
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["high", "low", "close"]).reset_index(drop=True)


SNIPER_PRICE_INDICATORS = [
    "ema_9", "ema_21", "ema_50", "ema_200", "kama_21", "wma_21",
    "donchian_upper_20", "donchian_lower_20", "donchian_mid_20",
    "boll_upper_20", "boll_lower_20",
]
SNIPER_ABS_INDICATORS = ["rsi_14", "tsi_25_13_13", "macd_dif_normal_12_26_9", "macd_dea_normal_12_26_9"]
SNIPER_JOIN_INDICATORS = SNIPER_PRICE_INDICATORS + SNIPER_ABS_INDICATORS + ["atr_14", "trend_direction"]


def load_joined(conn, symbol: str, tf: str, days: int) -> pd.DataFrame | None:
    """OHLCV + indicator join, as read live by 25_smc_ml_sniper — but only
    CLOSED candles. Live, bot 25 repaints on the forming candle (Report
    CANDLE_CALL_SITES §3); the replay must not replicate this, otherwise the
    model learns on candles it has never seen at decision time."""
    try:
        df = read_candles_with_indicators(
            conn, symbol, tf,
            start=_window_start(days),
            include_forming=False,
            candle_columns=OHLCV_COLUMNS,
            indicator_columns=SNIPER_JOIN_INDICATORS,
        )
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in df.columns:
        if c not in ("open_time", "trend_direction"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # ffill closes gaps from the PAST. A bfill afterwards (like in
    # 25_smc_ml_sniper:220) would fill remaining rows from the FUTURE:
    # the warmup columns (ema_200 needs 200 bars) are NULL at the start of the
    # coin history, and run_td_bb already emits from t=WINDOW-1=149. The replay
    # discards these rows instead — an event without real indicators is not a
    # training datum (T-2026-CU-9050-045).
    df.ffill(inplace=True)
    df = df.dropna()
    if df.empty:
        return None
    return df.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# EXIT SIMULATION — wick-aware first-touch, SL-first, fees, monitor trailing
# ─────────────────────────────────────────────────────────────────────────────
def simulate_exit(
    times: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    start_idx: int,
    direction: str,
    entry: float,
    sl: float,
    targets: list[float],
    n_published: int,
    fee_per_side: float = FEE_PER_SIDE,
) -> dict:
    """First-touch scan over candles from start_idx (everything AFTER the entry candle).

    Two results in one pass:
      * outcome_tp1 — 1 when TP1 is touched before SL, 0 if SL first,
                      None if neither by end of data (trade open).
                      If TP1 AND SL in the same candle: SL first (conservative).
      * ladder — Cornix approximation: position in 1/n equal parts over
                 the published TPs; trailing like 8_ai_trade_monitor
                 (from TP2 on, SL moves to targets[k-2]); rest closes at the
                 (possibly trailing) SL. Fees per fill both ways.
    """
    is_long = direction.upper() == "LONG"
    tps = [float(t) for t in targets[:n_published]] if targets else []
    if not tps:
        return {"outcome_tp1": None, "exit_reason": "no_targets", "net_pnl_pct": 0.0}
    frac = 1.0 / len(tps)

    cur_sl = float(sl)
    next_tp = 0  # Index of next open TP
    outcome_tp1 = None
    realized = 0.0  # Net PnL in % of notional (sum over partial fills)
    exit_reason, exit_time = None, None

    def leg_pnl(exit_price: float, fraction: float) -> float:
        gross = (exit_price - entry) / entry if is_long else (entry - exit_price) / entry
        return (gross - 2.0 * fee_per_side) * fraction

    n = len(times)
    i = start_idx
    while i < n and next_tp < len(tps):
        hi, lo = highs[i], lows[i]
        sl_hit = (lo <= cur_sl) if is_long else (hi >= cur_sl)
        tp_hit = (hi >= tps[next_tp]) if is_long else (lo <= tps[next_tp])

        if sl_hit:  # SL-first in case of ambiguity — conservative (monitor convention)
            if outcome_tp1 is None:
                outcome_tp1 = 0 if next_tp == 0 else 1
            remaining = 1.0 - next_tp * frac
            realized += leg_pnl(cur_sl, remaining)
            exit_reason, exit_time = f"sl_after_tp{next_tp}", times[i]
            break

        while next_tp < len(tps) and ((hi >= tps[next_tp]) if is_long else (lo <= tps[next_tp])):
            realized += leg_pnl(tps[next_tp], frac)
            next_tp += 1
            if outcome_tp1 is None:
                outcome_tp1 = 1
            # Trailing wie 8_ai: nach TP k (1-based, k>=2) → SL = targets[k-2]
            if next_tp >= 2:
                cur_sl = tps[next_tp - 2]
        if next_tp >= len(tps):
            exit_reason, exit_time = "all_targets", times[i]
            break
        if not tp_hit and not sl_hit:
            pass
        i += 1

    if exit_reason is None:
        # Data end: rest mark-to-market at last close (trade still really open)
        remaining = 1.0 - next_tp * frac
        if remaining > 0 and n > start_idx:
            realized += leg_pnl(closes[n - 1], remaining)
        exit_reason = "open_at_end"
        exit_time = times[n - 1] if n > start_idx else None

    risk_pct = abs(entry - sl) / entry if entry else 0.0
    return {
        "outcome_tp1": outcome_tp1,
        "exit_reason": exit_reason,
        "exit_time": str(exit_time) if exit_time is not None else None,
        "net_pnl_pct": round(realized * 100, 4),  # in % des Nominals
        "risk_pct": round(risk_pct * 100, 4),
        "r_multiple": round(realized / risk_pct, 4) if risk_pct > 0 else None,
    }


def first_idx_after(times: np.ndarray, ts) -> int:
    """Index of first candle with open_time > ts (exits start AFTER the entry candle).

    `times` is the naive-UTC datetime64 array from `df["open_time"].values`
    (pandas strips TZ at .values); tz-aware inputs are aligned.
    """
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return int(np.searchsorted(times, ts.to_datetime64(), side="right"))


# ─────────────────────────────────────────────────────────────────────────────
# ADAPTER 1: UFI1 (P0.11 validation)
# ─────────────────────────────────────────────────────────────────────────────
def run_ufi1(conn, symbol: str, days: int, ufi1_mod) -> list[dict]:
    """Walk-forward over daily candles with the bot's own find_ufi1_setup().

    Decision point: close of each daily candle (the bot scans every 4h with
    live price; daily granularity is the conservative approximation — every
    setup the bot would have taken during the day is taken by end of day at the
    latest). Entry = CMP (last close), SL/TP1 from setup — exactly the posted
    geometry (single-TP1, no trailing ladder!).
    """
    lookback = getattr(ufi1_mod, "DAILY_BARS_LOOKBACK", 120)
    cooldown_h = getattr(ufi1_mod, "COOLDOWN_HOURS", 48)

    df1d = load_ohlcv(conn, symbol, "1d", days + lookback + 10)
    if df1d is None or len(df1d) < 30:
        return []
    df1h = load_ohlcv(conn, symbol, "1h", days + 5)
    if df1h is None or len(df1h) < 100:
        return []

    t1h = df1h["open_time"].values
    h1h, l1h, c1h = df1h["high"].values, df1h["low"].values, df1h["close"].values

    df1d_idx = df1d.set_index("open_time")
    # Naive UTC throughout — the 1h exit series is also naive via .values.
    df1d_idx.index = df1d_idx.index.tz_localize(None)
    dates = df1d_idx.index
    replay_start = dates.max() - pd.Timedelta(days=days)

    trades: list[dict] = []
    cooldown_until = None
    open_until = None

    for t in range(len(dates)):
        ts_close = dates[t] + pd.Timedelta(days=1)  # Candle t is closed from here
        if dates[t] < replay_start:
            continue
        if cooldown_until is not None and ts_close < cooldown_until:
            continue
        if open_until is not None and ts_close < open_until:
            continue  # Bot dedup: active UFI1 trade on the coin blocks new signals

        window = df1d_idx.iloc[max(0, t + 1 - lookback): t + 1]
        if len(window) < 15:
            continue
        live_price = float(window["close"].iloc[-1])
        setup = ufi1_mod.find_ufi1_setup(window, live_price)
        if setup is None:
            continue

        entry = live_price  # Bot postet CMP-Entry
        sl, tp1 = float(setup["sl_price"]), float(setup["tp1_price"])
        start = first_idx_after(t1h, ts_close - pd.Timedelta(hours=1))
        result = simulate_exit(t1h, h1h, l1h, c1h, start, "SHORT", entry, sl, [tp1], 1)

        trades.append({
            "strategy": "ufi1", "symbol": symbol, "direction": "SHORT",
            "signal_time": str(ts_close), "entry": entry, "sl": sl, "targets": [tp1],
            "swing_pct": setup["swing_pct"], "entry_date_setup": str(setup["entry_date"]),
            **result,
        })

        cooldown_until = ts_close + pd.Timedelta(hours=cooldown_h)
        if result["exit_reason"] == "open_at_end":
            open_until = dates[-1] + pd.Timedelta(days=2)
        elif result["exit_time"] is not None:
            open_until = pd.Timestamp(result["exit_time"])

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# ADAPTER 2/3: TD / BB (25_smc_ml_sniper detection, rebuilt 1:1)
# ─────────────────────────────────────────────────────────────────────────────
def _sniper_features(df: pd.DataFrame, idx: int, direction: str) -> dict:
    """== 25_smc_ml_sniper.extract_ml_features (feature candle idx)."""
    close_prev = float(df["close"].iloc[idx])
    feats = {
        "dir_num": 1 if direction == "LONG" else 0,
        "atr_14_pct": (float(df["atr_14"].iloc[idx]) / close_prev) * 100 if close_prev else 0.0,
    }
    for ind in SNIPER_ABS_INDICATORS:
        feats[ind] = float(df[ind].iloc[idx])
    for ind in SNIPER_PRICE_INDICATORS:
        v = float(df[ind].iloc[idx])
        feats[f"{ind}_dist_pct"] = ((v - close_prev) / close_prev) * 100 if close_prev else 0.0
    trend = str(df["trend_direction"].iloc[idx])
    feats["trend_UP"] = 1 if trend == "UP" else 0
    feats["trend_DOWN"] = 1 if trend == "DOWN" else 0
    feats["trend_SIDEWAYS"] = 1 if trend == "SIDEWAYS" else 0
    return feats


def run_td_bb(conn, symbol: str, tf: str, days: int, which: str) -> list[dict]:
    """Walk-forward of the sniper detection: per closed candle, a scan over
    the 150-candle window (like live `ORDER BY open_time DESC LIMIT 150`).

    Emits ALL detector events (even those that would fail live at the ML
    threshold or at BB_1H-LONG parking) — flag `live_gated` marks them. For
    retraining, all events are training data; for calibration comparisons,
    filter on live_gated=False.
    """
    PIVOT_WINDOW = 10
    MAX_TD_SPAN = 50
    MAX_BB_AGE = 20
    WINDOW = 150

    df = load_joined(conn, symbol, tf, days)
    if df is None or len(df) < WINDOW + 10:
        return []

    # 1h series for smart targets window (live reads calculate_smart_targets
    # ALWAYS the 1h table) and for exit simulation.
    df1h = df[["open_time", "open", "high", "low", "close", "volume"]] if tf == "1h" else load_ohlcv(conn, symbol, "1h", days)
    if df1h is None or len(df1h) < 100:
        return []
    t1h = df1h["open_time"].values
    h1h, l1h, c1h = df1h["high"].values, df1h["low"].values, df1h["close"].values

    H, L, C = df["high"].values, df["low"].values, df["close"].values
    R = df["rsi_14"].values
    times = df["open_time"].values
    tf_hours = {"1h": 1, "4h": 4}[tf]
    cd_hours = 4 if tf == "1h" else 12

    trades: list[dict] = []
    cooldown: dict[str, pd.Timestamp] = {}
    open_until: dict[str, pd.Timestamp] = {}

    def try_emit(direction: str, feat_idx_abs: int, t: int, live_gated: bool, pattern_meta: dict):
        ts_decision = pd.Timestamp(times[t]) + pd.Timedelta(hours=tf_hours)  # Kerze t geschlossen
        key = direction
        if key in cooldown and ts_decision < cooldown[key]:
            return
        if key in open_until and ts_decision < open_until[key]:
            return
        current_price = float(C[t])

        # Smart Targets auf dem 1h-Fenster BIS zur Entscheidung (letzte 1000 Kerzen)
        cut = first_idx_after(t1h, ts_decision - pd.Timedelta(hours=1))
        win1h = df1h.iloc[max(0, cut - 1000): cut]
        if len(win1h) < 100:
            return
        setup = calculate_smart_targets(None, symbol, direction, current_price, df=win1h)

        start = first_idx_after(t1h, ts_decision - pd.Timedelta(hours=1))
        result = simulate_exit(
            t1h, h1h, l1h, c1h, start, direction,
            setup["entry1"], setup["sl"], setup["targets"], PUBLISHED_TARGETS[which],
        )
        feats = _sniper_features(df, feat_idx_abs, direction)
        trades.append({
            "strategy": which, "tf": tf, "symbol": symbol, "direction": direction,
            "signal_time": str(ts_decision), "entry": setup["entry1"], "entry2": setup["entry2"],
            "sl": setup["sl"], "targets": setup["targets"][:PUBLISHED_TARGETS[which]],
            "live_gated": live_gated, "features": feats, **pattern_meta, **result,
        })
        cooldown[key] = ts_decision + pd.Timedelta(hours=cd_hours)
        if result["exit_reason"] == "open_at_end":
            open_until[key] = pd.Timestamp(times[-1]) + pd.Timedelta(days=365)
        elif result["exit_time"] is not None:
            open_until[key] = pd.Timestamp(result["exit_time"])

    for t in range(WINDOW - 1, len(df)):
        lo_b = t - WINDOW + 1
        h_w, l_w, c_w, r_w = H[lo_b: t + 1], L[lo_b: t + 1], C[lo_b: t + 1], R[lo_b: t + 1]
        n_w = WINDOW
        current_price = c_w[-1]

        peak_idx = scipy.signal.argrelextrema(h_w, np.greater, order=PIVOT_WINDOW)[0]
        trough_idx = scipy.signal.argrelextrema(l_w, np.less, order=PIVOT_WINDOW)[0]
        if len(peak_idx) < 3 or len(trough_idx) < 3:
            continue

        if which == "td":
            # 1a. Bearish three-drive (SHORT)
            p3 = peak_idx[-1]
            if n_w - p3 <= PIVOT_WINDOW + 2:
                p1, p2 = peak_idx[-3], peak_idx[-2]
                if (p3 - p1) <= MAX_TD_SPAN and h_w[p1] < h_w[p2] < h_w[p3]:
                    if r_w[p1] > r_w[p2] > r_w[p3]:
                        try_emit("SHORT", lo_b + p3, t, False,
                                 {"p1": int(lo_b + p1), "p2": int(lo_b + p2), "p3": int(lo_b + p3)})
            # 1b. Bullish three-drive (LONG)
            q3 = trough_idx[-1]
            if n_w - q3 <= PIVOT_WINDOW + 2:
                q1, q2 = trough_idx[-3], trough_idx[-2]
                if (q3 - q1) <= MAX_TD_SPAN and l_w[q1] > l_w[q2] > l_w[q3]:
                    if r_w[q1] < r_w[q2] < r_w[q3]:
                        try_emit("LONG", lo_b + q3, t, False,
                                 {"p1": int(lo_b + q1), "p2": int(lo_b + q2), "p3": int(lo_b + q3)})

        elif which == "bb":
            # 2a. Breaker block LONG (live parked for tf=1h → live_gated)
            p_res = peak_idx[-2]
            pivot_res = h_w[p_res]
            if pivot_res * 0.995 <= current_price <= pivot_res * 1.005:
                breakout_idx = -1
                for i in range(p_res + 1, n_w - 1):
                    if c_w[i] > pivot_res:
                        breakout_idx = i
                        break
                if breakout_idx != -1 and (n_w - 1 - breakout_idx) <= MAX_BB_AGE:
                    if max(h_w[breakout_idx: n_w - 1]) > pivot_res * 1.003:
                        try_emit("LONG", lo_b + n_w - 2, t, tf == "1h",
                                 {"level": float(pivot_res), "breakout_idx": int(lo_b + breakout_idx)})
            # 2b. Breaker block SHORT (live active on BOTH TFs — parking gap!)
            p_sup = trough_idx[-2]
            pivot_sup = l_w[p_sup]
            if pivot_sup * 0.995 <= current_price <= pivot_sup * 1.005:
                breakdown_idx = -1
                for i in range(p_sup + 1, n_w - 1):
                    if c_w[i] < pivot_sup:
                        breakdown_idx = i
                        break
                if breakdown_idx != -1 and (n_w - 1 - breakdown_idx) <= MAX_BB_AGE:
                    if min(l_w[breakdown_idx: n_w - 1]) < pivot_sup * 0.997:
                        try_emit("SHORT", lo_b + n_w - 2, t, False,
                                 {"level": float(pivot_sup), "breakdown_idx": int(lo_b + breakdown_idx)})

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# ADAPTER 4: ABR1 (18_ai_abr1_bot detection; feature builder from bot module)
# ─────────────────────────────────────────────────────────────────────────────
def run_abr1(conn, symbol: str, days: int, abr1_mod) -> list[dict]:
    """Walk-forward of the ABR1 detection: per closed 1h candle, the replay
    checks exactly this candle as a retest candidate (== bot behavior since the
    detector rework 2026-07).

    The detection comes entirely from the bot module (find_break_retest_setups:
    direction coupling of retest, hold check, first-touch, confirmed pivots) —
    one source, no skew. The detector's setup geometry features end up in the
    feature dict of the replay event.

    Indicators are calculated ONCE over the entire series via bot feature builder
    (== trainer behavior; minimal deviation from the bot's 240h window for
    recursive indicators, documented in the report).
    """
    HIST = abr1_mod.LIVE_DATA_HISTORY_HOURS  # 240

    df = load_ohlcv(conn, symbol, "1h", days + 15)
    if df is None or len(df) < HIST + 10:
        return []

    # Bot feature builder (with P0.12 prefix fix) over the entire series
    df_ind = abr1_mod.calculate_technical_indicators(df.copy())
    feature_cols = abr1_mod.FEATURE_COLUMNS

    t1h = df["open_time"].values
    H, L, C = df["high"].values, df["low"].values, df["close"].values
    h1h, l1h, c1h = H, L, C

    trades: list[dict] = []
    cooldown: dict[str, pd.Timestamp] = {}

    for t in range(HIST, len(df)):
        ts_decision = pd.Timestamp(t1h[t]) + pd.Timedelta(hours=1)
        lo_b = t - HIST + 1
        win_df = df.iloc[lo_b: t + 1].reset_index(drop=True)

        levels = abr1_mod.find_pivot_levels(win_df)
        if not levels:
            continue

        retest_idx = len(win_df) - 1  # genau die frisch geschlossene Kerze
        for bnr_setup in abr1_mod.find_break_retest_setups(win_df, retest_idx, levels):
            direction = bnr_setup["direction"]
            if direction in cooldown and ts_decision < cooldown[direction]:
                continue
            entry = float(C[t])
            win1h = df.iloc[max(0, t + 1 - 1000): t + 1][["open", "high", "low", "close", "volume"]]
            setup = calculate_smart_targets(None, symbol, direction, entry, df=win1h)

            start = t + 1
            result = simulate_exit(
                t1h, h1h, l1h, c1h, start, direction,
                setup["entry1"], setup["sl"], setup["targets"], PUBLISHED_TARGETS["abr1"],
            )
            feats = {k: float(df_ind[k].iloc[t]) for k in feature_cols}
            feats.update({k: float(v) for k, v in bnr_setup["features"].items()})
            trades.append({
                "strategy": "abr1", "tf": "1h", "symbol": symbol, "direction": direction,
                "signal_time": str(ts_decision), "entry": setup["entry1"], "entry2": setup["entry2"],
                "sl": setup["sl"], "targets": setup["targets"][:PUBLISHED_TARGETS["abr1"]],
                "level_price": float(bnr_setup["level_price"]), "features": feats, **result,
            })
            cooldown[direction] = ts_decision + pd.Timedelta(hours=4)

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# ADAPTER 5: MIS1 (dense sample per closed 1h candle — retrain labels)
# ─────────────────────────────────────────────────────────────────────────────
MIS1_HORIZONS = (8, 24, 72, 168)  # all bot live horizons; extended 2026-07-05 for 8/24 (previously report-16 focus 72/168)
MIS1_WARMUP = 30  # volume_sma20 (20) + deltas; DB indicators come ready from join


def load_mis1_frame(conn, symbol: str, days: int) -> pd.DataFrame | None:
    """1h candles + indicator join with the shared column list from
    core.mis_features. ONLY closed candles (R1 discipline) — the running
    hour is filtered out by date_trunc."""
    # R1: the h⋈i JOIN → read_candles_with_indicators. The candle side keeps full
    # OHLCV (labeling needs high/low); the indicator side is the shared
    # MIS_INDICATOR_COLUMNS. include_forming=False == the old
    # `h.open_time < date_trunc('hour', NOW())` closed filter; MIS_RENAME_MAP
    # reproduces the three tsi/macd aliases so the frame stays byte-equal to
    # 11_ai_mis (hard rule 7, Trainer == Serving). The days-window is a soft
    # lower bound (Python-vs-DB now() skew can't straddle an hourly candle).
    try:
        df = read_candles_with_indicators(
            conn,
            symbol,
            "1h",
            start=datetime.now(timezone.utc) - timedelta(days=int(days)),
            include_forming=False,
            candle_columns=("open_time", "open", "high", "low", "close", "volume"),
            indicator_columns=MIS_INDICATOR_COLUMNS,
        )
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    df = df.rename(columns=MIS_RENAME_MAP)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in df.columns:
        if c != "open_time":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["high", "low", "close"]).reset_index(drop=True)


def run_mis1(conn, symbol: str, days: int, stride: int) -> list[dict]:
    """MIS1 is NOT detector gated — live, the bot scores every coin every hour.
    The replay therefore samples densely: every `stride`-th closed candle, with
    deterministic per-coin offset (crc32), so not all coins are sampled at the
    same market hour (cross-sectional twin correlation).

    Per sample and direction: geometry = calculate_smart_targets on the
    1000-candle window UNTIL the decision candle (exactly the live function),
    exits capped horizontally (8h/24h/72h/168h) in ONE run — label = TP1-before-SL
    WITHIN the horizon; timeout with full data window is 0, data end before
    horizon end remains None (discarded during training).

    Intentional approximation: entry = close of freshly closed candle (the bot
    uses live price ~11 minutes after the hour closes).
    No cooldown in replay — stride handles dedup; live cooldowns only throttle
    POSTING, not SCORING."""
    import zlib

    df = load_mis1_frame(conn, symbol, days)
    if df is None or len(df) < 250:
        return []

    feats_df = mis1_add_features(df, include_legacy=True)

    t1h = df["open_time"].values
    h1h, l1h, c1h = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    offset = zlib.crc32(symbol.encode()) % max(stride, 1)

    trades: list[dict] = []
    for t in range(MIS1_WARMUP + offset, n - 1, max(stride, 1)):
        ts_decision = pd.Timestamp(t1h[t]) + pd.Timedelta(hours=1)
        current_price = float(c1h[t])
        if current_price <= 0:
            continue
        win1h = df.iloc[max(0, t + 1 - 1000): t + 1][["open", "high", "low", "close", "volume"]]
        if len(win1h) < 100:
            continue

        features = {k: round(float(feats_df[k].iloc[t]), 6) for k in MIS1_FEATURE_COLS}
        legacy = {k: round(float(feats_df[k].iloc[t]), 6) for k in MIS1_LEGACY_COLS}

        # Level pool is direction independent → calculate once, both directions
        # (bit-identical to double call, parity test 2026-07-05).
        try:
            pool = compute_smart_target_levels(win1h, current_price)
        except Exception:
            pool = None  # calculate_smart_targets then falls back to live

        for direction in ("LONG", "SHORT"):
            setup = calculate_smart_targets(None, symbol, direction, current_price, df=win1h, levels=pool)
            start = t + 1
            rec = {
                "strategy": "mis1", "tf": "1h", "symbol": symbol, "direction": direction,
                "signal_time": str(ts_decision), "entry": setup["entry1"], "entry2": setup["entry2"],
                "sl": setup["sl"], "targets": setup["targets"][:PUBLISHED_TARGETS["mis1"]],
                "features": features, "legacy_features": legacy,
            }
            for hours in MIS1_HORIZONS:
                end = start + hours
                full_window = end <= n
                r = simulate_exit(
                    t1h[:end], h1h[:end], l1h[:end], c1h[:end], start, direction,
                    setup["entry1"], setup["sl"], setup["targets"], PUBLISHED_TARGETS["mis1"],
                )
                out = r["outcome_tp1"]
                if out is None:
                    # neither TP1 nor SL touched: with full horizon window an
                    # honest 0, data end before horizon end has no label.
                    out = 0 if full_window else None
                rec[f"outcome_{hours}h"] = out
                rec[f"net_pnl_{hours}h"] = r["net_pnl_pct"]
                rec[f"exit_reason_{hours}h"] = r["exit_reason"]
                rec[f"r_multiple_{hours}h"] = r["r_multiple"]
            # Compatibility with summarize()/load_replay(): long horizon as main label
            rec["outcome_tp1"] = rec[f"outcome_{MIS1_HORIZONS[-1]}h"]
            rec["net_pnl_pct"] = rec[f"net_pnl_{MIS1_HORIZONS[-1]}h"]
            rec["r_multiple"] = rec[f"r_multiple_{MIS1_HORIZONS[-1]}h"]
            rec["risk_pct"] = r["risk_pct"]
            trades.append(rec)

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# ADAPTER 6: RUB (Rubberband mean reversion — replay filter events)
# ─────────────────────────────────────────────────────────────────────────────
RUB_REG_WINDOW_H = 95 * 24   # Regression/level window like in bot (95d query)
RUB_MIN_REG_ROWS = 50        # Bot: len(rows_90d) < 50 → skip
RUB_COOLDOWN_H = 4           # Live cooldown per coin/direction (bot 13)

# The 9 indicator columns bot 13/34 (RUB2/MAX1) read as-of each closed candle —
# same set as 34_ai_max1_bot's query_ind (Trainer == Serving). Raw DB names, no
# aliasing, so read_candles_with_indicators needs no rename. R1: replaced the old
# i.-prefixed RUB_SQL_INDICATORS JOIN-SELECT fragment.
_RUB_INDICATOR_COLUMNS = [
    "rsi_14",
    "tsi_fast_12_7_7",
    "tsi_fast_12_7_7_signal",
    "macd_dif_normal_12_26_9",
    "macd_dea_normal_12_26_9",
    "atr_14",
    "ema_200",
    "donchian_lower_20",
    "donchian_upper_20",
]


def load_rub_frame(conn, symbol: str, days: int) -> pd.DataFrame | None:
    """1h candles + exactly the indicators bot 13 queries (as-of per candle).
    ONLY closed candles (R1 discipline)."""
    # R1: h⋈i JOIN → read_candles_with_indicators (OHLCV candle side for labeling +
    # _RUB_INDICATOR_COLUMNS, raw names, no rename). include_forming=False == the old
    # `h.open_time < date_trunc('hour', NOW())` closed filter; the days+100 window is
    # a soft lower bound.
    try:
        df = read_candles_with_indicators(
            conn,
            symbol,
            "1h",
            start=datetime.now(timezone.utc) - timedelta(days=int(days) + 100),
            include_forming=False,
            candle_columns=("open_time", "open", "high", "low", "close", "volume"),
            indicator_columns=_RUB_INDICATOR_COLUMNS,
        )
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in df.columns:
        if c != "open_time":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["high", "low", "close"]).reset_index(drop=True)


def _rub_val(arr, i, default):
    """NaN/Inf → default (mirror of get_f in bot)."""
    v = arr[i]
    try:
        fv = float(v)
        return fv if np.isfinite(fv) else default
    except (TypeError, ValueError):
        return default


def run_rub1(conn, symbol: str, days: int) -> list[dict]:
    """Walk-forward of the RUB filter: for each closed 1h candle, the
    rubberband condition is checked (== hourly live scan of bot 13).

    ONE source with the bot (core/rub_features: regression, filter,
    9-feature contract) + live geometry as-of (get_hvn_and_sr_levels(df=...) +
    hvn_sr_trade_geometry + ensure_min_tp_distance). 4h cooldown per direction
    like live. Additionally, the 6 funding features (core/funding_features) in
    the feature dict — for the RUB2 retrain (MODEL_INTENT §8).

    Intentional approximation: entry = close of freshly closed candle (the bot
    uses price shortly after the hour closes)."""
    df = load_rub_frame(conn, symbol, days)
    if df is None or len(df) < RUB_MIN_REG_ROWS + 2:
        return []

    fund_by_sym = load_funding(conn, [symbol])

    t1h = df["open_time"].values
    h1h, l1h, c1h = df["high"].values, df["low"].values, df["close"].values
    # Epoch axis of rub_trend's regression. NOT `astype("int64") / 1e9`: that
    # divides by the column's own resolution, so under pandas >= 3.0 (datetime64[us])
    # the axis shrinks 1000x and `slope_trend` — a RUB2 model input — comes out
    # 1000x too large, while `dist_to_trend` still matches. The live bot goes
    # through datetime.timestamp() and is always in seconds; this keeps the replay
    # on the same axis regardless of the generating interpreter (T-2026-KYT-9050-008).
    ts_sec = epoch_seconds(df["open_time"])
    n = len(df)

    # Replay window: the warmup history (regression lookback) lies BEFORE the
    # requested timeframe, events only occur in the last `days` days.
    start_t = max(RUB_MIN_REG_ROWS, n - days * 24)

    # t1h comes from .values → naive UTC-datetime64; cooldown markers also naive.
    cooldown = {"LONG": pd.Timestamp.min, "SHORT": pd.Timestamp.min}
    trades: list[dict] = []
    for t in range(start_t, n - 1):
        curr_close = float(c1h[t])
        if not np.isfinite(curr_close) or curr_close <= 0:
            continue

        lo = max(0, t + 1 - RUB_REG_WINDOW_H)
        if t + 1 - lo < RUB_MIN_REG_ROWS:
            continue

        rsi = _rub_val(df["rsi_14"].values, t, 50.0)
        tsi_line = _rub_val(df["tsi_fast_12_7_7"].values, t, 0.0)
        dc_lower = _rub_val(df["donchian_lower_20"].values, t, curr_close)
        dc_upper = _rub_val(df["donchian_upper_20"].values, t, curr_close)

        # Regression only AFTER a cheap pre-filter? No — dist_to_trend is
        # in the condition itself; the closed-form regression is cheap.
        dist_pct, slope_day = rub_trend(ts_sec[lo: t + 1], c1h[lo: t + 1], curr_close)
        event_type = rub_event_type(dist_pct, rsi, tsi_line, curr_close, dc_lower, dc_upper)
        if not event_type:
            continue

        direction = "LONG" if event_type == "REVERSION_UP" else "SHORT"
        ts_decision = pd.Timestamp(t1h[t]) + pd.Timedelta(hours=1)
        if ts_decision < cooldown[direction]:
            continue
        cooldown[direction] = ts_decision + pd.Timedelta(hours=RUB_COOLDOWN_H)

        features = build_rub_features(
            dist_pct, slope_day, curr_close, rsi, tsi_line,
            _rub_val(df["tsi_fast_12_7_7_signal"].values, t, 0.0),
            _rub_val(df["macd_dif_normal_12_26_9"].values, t, 0.0),
            _rub_val(df["macd_dea_normal_12_26_9"].values, t, 0.0),
            _rub_val(df["atr_14"].values, t, 0.0),
            _rub_val(df["ema_200"].values, t, curr_close),
        )
        features.update(funding_features_asof(fund_by_sym, symbol, ts_decision))

        is_long = direction == "LONG"
        win95 = df.iloc[lo: t + 1][["high", "low", "close"]]
        supps, resis = get_hvn_and_sr_levels(None, symbol, curr_close, df=win95)
        entry1 = curr_close
        entry2, sl, t_cands = hvn_sr_trade_geometry(entry1, is_long, supps, resis)
        targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)
        if not targets or sl <= 0:
            continue

        res = simulate_exit(t1h, h1h, l1h, c1h, t + 1, direction,
                            entry1, sl, targets, PUBLISHED_TARGETS["rub"])
        trades.append({
            "strategy": "rub", "tf": "1h", "symbol": symbol, "direction": direction,
            "signal_time": str(ts_decision), "entry": entry1, "entry2": entry2,
            "sl": sl, "targets": targets[:PUBLISHED_TARGETS["rub"]],
            "dist_to_trend_pct": round(dist_pct, 6),
            "features": features, **res,
        })

    return trades


def load_ats_frame(conn, symbol: str, days: int) -> pd.DataFrame | None:
    """1h candles + exactly the indicators bot 12 joins (core.ats_features),
    ONLY closed candles (R1). Numeric + fillna(0) like bot 12 (line 120),
    so the derived features are bit-identical."""
    try:
        df = read_candles_with_indicators(
            conn,
            symbol,
            "1h",
            start=_window_start(days + ATS_WARMUP_DAYS),
            include_forming=False,
            candle_columns=ATS_CANDLE_COLUMNS,
            indicator_columns=ATS_INDICATOR_COLUMNS,
        )
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in df.columns:
        if c != "open_time":
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df.reset_index(drop=True)


def run_ats(conn, symbol: str, days: int) -> list[dict]:
    """Walk-forward of the ATS/TSI sniper (bot 12) for the ATS2 retrain.

    ONE source with the bot: core.ats_features (TSI-crossover filter,
    29-feature contract, OBV/VWAP) + same HVN/S-R geometry
    (core.trade_utils.hvn_sr_trade_geometry, byte-identical to bot inline
    geometry). For each closed 1h candle, we check for a TSI signal line
    crossover (== hourly live scan of bot 12); entry = close of freshly closed
    candle, label = first-touch TP1-before-SL of posted geometry
    (simulate_exit, fees included).

    No cooldown: TSI-crossovers are edge-triggered and alternate per direction —
    unlike the RUB-threshold filter, they don't fire multiple times in a row in
    the same direction.

    OBV-baseline parity (hard rule 7): the bot normalizes OBV to the start of
    its 500-candle window; the replay passes for each event exactly this window
    (df.iloc[t+1-500 : t+1]). The warmup (ATS_WARMUP_DAYS) ensures that old
    coins have a full 500-candle window; young coins are also congruent because
    their window like the bot's starts at the coin start."""
    df = load_ats_frame(conn, symbol, days)
    if df is None or len(df) < ATS_MIN_HISTORY + 2:
        return []

    t1h = df["open_time"].values
    h1h, l1h, c1h = df["high"].values, df["low"].values, df["close"].values
    tsi_line = df[TSI_LINE_COL].values
    tsi_sig = df[TSI_SIGNAL_COL].values
    n = len(df)

    # Events only in the requested timeframe; the warmup before guarantees each
    # event a full 500-candle/95d window.
    start_t = max(ATS_MIN_HISTORY, n - days * 24)
    trades: list[dict] = []
    for t in range(start_t, n - 1):
        direction = ats_cross(tsi_line[t - 1], tsi_sig[t - 1], tsi_line[t], tsi_sig[t])
        if direction is None:
            continue
        curr_close = float(c1h[t])
        if not np.isfinite(curr_close) or curr_close <= 0:
            continue

        feat_lo = max(0, t + 1 - ATS_FEATURE_WINDOW)
        window = df.iloc[feat_lo: t + 1]
        if len(window) < ATS_MIN_HISTORY:
            continue
        features = build_ats_features(window)

        is_long = direction == "LONG"
        sr_lo = max(0, t + 1 - ATS_SR_WINDOW_H)
        win95 = df.iloc[sr_lo: t + 1][["high", "low", "close"]]
        supps, resis = get_hvn_and_sr_levels(None, symbol, curr_close, df=win95)
        entry1 = curr_close
        entry2, sl, t_cands = hvn_sr_trade_geometry(entry1, is_long, supps, resis)
        targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)
        if not targets or sl <= 0:
            continue

        ts_decision = pd.Timestamp(t1h[t]) + pd.Timedelta(hours=1)
        res = simulate_exit(t1h, h1h, l1h, c1h, t + 1, direction,
                            entry1, sl, targets, PUBLISHED_TARGETS["ats"])
        trades.append({
            "strategy": "ats", "tf": "1h", "symbol": symbol, "direction": direction,
            "signal_time": str(ts_decision), "entry": entry1, "entry2": entry2,
            "sl": sl, "targets": targets[:PUBLISHED_TARGETS["ats"]],
            "features": features, **res,
        })
    return trades


def run_atb2(conn, symbol: str, days: int) -> list[dict]:
    """Walk-forward of the ATB2 converging-channel detector (MODEL_INTENT §11).

    ONE source with bot 14: ``core.atb2_features`` (confirmed pivots, channel fit,
    closed breakout, feature contract). For each closed 1h candle, we check if a
    converging channel (wedge/triangle/pennant) breaks out.

    Label geometry = measured move (§11: ⅓/⅔/1× channel width) — the channel-native
    geometry that the bot posts (no DB level pool needed → Train==Serve exactly).
    Additionally, the fleet smart targets of the same candle are simulated and
    written as comparison (``smart_*``) to the record — §11 wants measured-move
    evaluated AGAINST smart targets in replay, without diluting the training
    label source.

    4h cooldown per direction; entry = close of freshly closed breakout candle."""
    df = load_ohlcv(conn, symbol, "1h", days + ATB2_WARMUP_DAYS)
    # hist covers channel lookback AND EMA200 convergence (parity contract).
    hist = max(atb.CHANNEL_MAX_SPAN + atb.CONFIRM_BARS + atb.ATR_PERIOD, atb.MIN_HISTORY_CANDLES)
    if df is None or len(df) < hist + 2:
        return []
    df_ind = atb.compute_indicators(df)
    t1h = df["open_time"].values
    H, L, C = df["high"].values, df["low"].values, df["close"].values

    start_t = max(hist, len(df) - days * 24)
    cooldown = {"LONG": pd.Timestamp.min, "SHORT": pd.Timestamp.min}
    trades: list[dict] = []
    # -1: simulate_exit needs at least one candle after the break.
    for t in range(start_t, len(df) - 1):
        setup = atb.find_channel_breakout(df_ind, t)
        if setup is None:
            continue
        direction = setup["direction"]
        ts_decision = pd.Timestamp(t1h[t]) + pd.Timedelta(hours=1)
        if ts_decision < cooldown[direction]:
            continue
        cooldown[direction] = ts_decision + pd.Timedelta(hours=ATB2_COOLDOWN_H)

        entry = setup["entry"]
        mm = atb.measured_move_targets(setup["channel"], setup["breakout"], entry)
        if not mm["targets"] or mm["sl"] <= 0:
            continue
        res = simulate_exit(t1h, H, L, C, t + 1, direction,
                            mm["entry1"], mm["sl"], mm["targets"], PUBLISHED_TARGETS["atb2"])

        # §11 comparison: same candle with fleet smart targets.
        win1h = df.iloc[max(0, t + 1 - 1000): t + 1][["open", "high", "low", "close", "volume"]]
        try:
            smart = calculate_smart_targets(None, symbol, direction, entry, df=win1h)
            res_smart = simulate_exit(t1h, H, L, C, t + 1, direction,
                                      smart["entry1"], smart["sl"], smart["targets"],
                                      PUBLISHED_TARGETS["atb2"])
        except Exception:
            res_smart = {"outcome_tp1": None, "net_pnl_pct": None, "exit_reason": "smart_error"}

        trades.append({
            "strategy": "atb2", "tf": "1h", "symbol": symbol, "direction": direction,
            "signal_time": str(ts_decision), "entry": float(entry), "entry2": mm["entry2"],
            "sl": mm["sl"], "targets": mm["targets"][:PUBLISHED_TARGETS["atb2"]],
            "channel_type": setup["channel"]["channel_type"],
            "features": setup["features"], **res,
            "smart_outcome_tp1": res_smart.get("outcome_tp1"),
            "smart_net_pnl_pct": res_smart.get("net_pnl_pct"),
            "smart_exit_reason": res_smart.get("exit_reason"),
        })
    return trades


# ─────────────────────────────────────────────────────────────────────────────
# DRIVER
# ─────────────────────────────────────────────────────────────────────────────
def summarize(trades: list[dict], label: str) -> dict:
    closed = [t for t in trades if t.get("outcome_tp1") is not None]
    open_t = [t for t in trades if t.get("outcome_tp1") is None]
    wins = sum(1 for t in closed if t["outcome_tp1"] == 1)
    r_vals = [t["r_multiple"] for t in closed if t.get("r_multiple") is not None]
    pnl_vals = [t["net_pnl_pct"] for t in closed]
    summary = {
        "label": label,
        "n_signals": len(trades),
        "n_closed": len(closed),
        "n_open_at_end": len(open_t),
        "tp1_first_touch_wr": round(wins / len(closed) * 100, 2) if closed else None,
        "sum_r": round(sum(r_vals), 2) if r_vals else None,
        "avg_r": round(float(np.mean(r_vals)), 4) if r_vals else None,
        "sum_net_pnl_pct": round(sum(pnl_vals), 2) if pnl_vals else None,
        "avg_net_pnl_pct": round(float(np.mean(pnl_vals)), 4) if pnl_vals else None,
        "median_net_pnl_pct": round(float(np.median(pnl_vals)), 4) if pnl_vals else None,
    }
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward simulator (P0.10/P0.11)")
    ap.add_argument("--strategy", required=True,
                    choices=["ufi1", "td", "bb", "abr1", "mis1", "rub", "atb2", "ats"])
    ap.add_argument("--tf", default="1h", choices=["1h", "4h"], help="only for td/bb")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--stride", type=int, default=24,
                    help="mis1: sample every N-th closed candle (per-coin offset deduplicates market hours)")
    ap.add_argument("--coins", default=None, help="comma-separated list; default: coins.json")
    ap.add_argument("--limit", type=int, default=None, help="only the first N coins")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    ap.add_argument("--resume", action="store_true",
                    help="append to existing JSONL and skip already contained coins")
    args = ap.parse_args()

    # cp1252 console: emojis/special characters in error messages must not break
    # the run via UnicodeEncodeError.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    set_low_priority()
    check_cpu_headroom()

    coins = args.coins.split(",") if args.coins else load_coins()
    if args.limit:
        coins = coins[: args.limit]

    os.makedirs(args.out, exist_ok=True)
    tag = f"{args.strategy}{'_' + args.tf if args.strategy in ('td', 'bb') else ''}"
    out_path = os.path.join(args.out, f"{tag}_replay_{args.days}d.jsonl")

    ufi1_mod = import_bot_module("29_ufi1_bot.py", "ufi1_bot") if args.strategy == "ufi1" else None
    abr1_mod = import_bot_module("18_ai_abr1_bot.py", "abr1_bot") if args.strategy == "abr1" else None

    all_trades: list[dict] = []
    done_symbols: set[str] = set()
    if args.resume and os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    tr = json.loads(line)
                except json.JSONDecodeError:
                    continue  # truncated last line of interrupted run
                all_trades.append(tr)
                done_symbols.add(tr["symbol"])
        # The last written coin could be incomplete → recalculate.
        if all_trades:
            last_sym = all_trades[-1]["symbol"]
            all_trades = [t for t in all_trades if t["symbol"] != last_sym]
            done_symbols.discard(last_sym)
        print(f"Resume: {len(done_symbols)} coins / {len(all_trades)} trades taken over")

    def fresh_conn():
        c = get_db_connection()
        try:
            c.set_session(readonly=True)
        except Exception:
            pass
        return c

    conn = fresh_conn()
    t0 = time.time()
    try:
        # Even with resume, consolidate and rewrite (taken trades first).
        with open(out_path, "w", encoding="utf-8") as fh:
            for tr in all_trades:
                fh.write(json.dumps(tr, default=str) + "\n")
            for i, symbol in enumerate(coins, 1):
                if symbol in done_symbols:
                    continue
                trades = None
                for attempt in (1, 2):
                    try:
                        if args.strategy == "ufi1":
                            trades = run_ufi1(conn, symbol, args.days, ufi1_mod)
                        elif args.strategy in ("td", "bb"):
                            trades = run_td_bb(conn, symbol, args.tf, args.days, args.strategy)
                        elif args.strategy == "mis1":
                            trades = run_mis1(conn, symbol, args.days, args.stride)
                        elif args.strategy == "rub":
                            trades = run_rub1(conn, symbol, args.days)
                        elif args.strategy == "atb2":
                            trades = run_atb2(conn, symbol, args.days)
                        elif args.strategy == "ats":
                            trades = run_ats(conn, symbol, args.days)
                        else:
                            trades = run_abr1(conn, symbol, args.days, abr1_mod)
                        break
                    except Exception as e:
                        print(f"  !! {symbol} (attempt {attempt}): {e}")
                        # Dead connection (e.g. DB restart/idle kill after hours)
                        # don't let it break the entire run — reconnect.
                        try:
                            conn.rollback()
                        except Exception:
                            try:
                                conn.close()
                            except Exception:
                                pass
                            try:
                                conn = fresh_conn()
                                print(f"  ↻ DB-reconnect before retry of {symbol}")
                            except Exception as e2:
                                print(f"  ↻ Reconnect failed: {e2}")
                                time.sleep(30)
                                conn = fresh_conn()
                if trades is None:
                    continue
                for tr in trades:
                    fh.write(json.dumps(tr, default=str) + "\n")
                fh.flush()
                all_trades.extend(trades)
                if i % 25 == 0 or i == len(coins):
                    el = time.time() - t0
                    print(f"[{i}/{len(coins)}] {symbol}: total {len(all_trades)} trades ({el:.0f}s)", flush=True)
    finally:
        conn.close()

    summary = summarize(all_trades, tag)
    summary["days"] = args.days
    summary["n_coins"] = len(coins)
    summary["fee_per_side"] = FEE_PER_SIDE
    with open(os.path.join(args.out, f"{tag}_replay_{args.days}d_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    print("\n===== SUMMARY =====")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\nTrades: {out_path}")


if __name__ == "__main__":
    main()
