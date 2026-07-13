"""
tools/walkforward_sim.py — gemeinsamer Walk-Forward-Simulator (Audit P0.10 / P0.11).

Zweck
-----
Spielt die BOT-EIGENEN Setup-Funktionen bar-für-bar über die historischen
Kerzen ab und scored die entstehenden Trades mit einem wick-aware
First-Touch-Forward-Scan (wie der neue Monitor nach dem P2.7-Fix), Fees
inklusive (P3.6). Die entstehenden Trade-Records sind die LABEL-QUELLE für
alle Neutrainings — NICHT closed_ai_signals (historisch nur zu 63,4% korrekt
gescored, Report 17).

Kernprinzipien (X-R1-Fix):
  * Setup-Erkennung importiert die Bot-Module bzw. deren extrahierte
    Setup-Funktionen — kein Copy-Paste-Skew.
  * Order-Geometrie = exakt die gepostete Geometrie: CMP-Entry +
    calculate_smart_targets (df-Fenster-Variante, dieselbe Funktion wie live)
    bzw. die Bot-eigenen SL/TP-Regeln (UFI1).
  * Entscheidungen nur auf GESCHLOSSENEN Kerzen bis zum Entscheidungszeitpunkt.
  * Exits: First-Touch auf den 1h-Kerzen NACH der Entscheidung, wick-aware,
    SL-first bei Ambiguität (TP und SL in derselben Kerze), Trailing-Semantik
    wie 8_ai_trade_monitor (ab TP2 rückt der SL auf targets[k-2]).
  * Fees: 0,05% pro Seite (Taker, konfigurierbar) → 0,10% Round-Trip.

Strategien
----------
  ufi1   — 29_ufi1_bot.find_ufi1_setup auf Daily-Kerzen (P0.11-Validierung:
           die "+278R" aus fib_backtest.py müssen fallen)
  td     — Three-Drive-Erkennung aus 25_smc_ml_sniper.scan_market (1h+4h)
  bb     — Breaker-Block-Erkennung aus 25_smc_ml_sniper.scan_market (1h+4h)
  abr1   — Break&Retest-Erkennung aus 18_ai_abr1_bot (1h)
  mis1   — dichte Stichprobe je geschlossener 1h-Kerze (kein Detektor-Gate),
           Features aus core.mis_features (geteilter Builder, Leakage-Fix),
           Labels horizontgekappt 72h/168h — Retrain-Priorität #1 (Report 16)

Betriebsregeln (Live-VPS!)
--------------------------
  * Prozess senkt sich selbst auf BELOW_NORMAL.
  * Vor dem Start wird die System-CPU geprüft (>90% → Abbruch, damit der
    neue core/health_monitor CPU_SATURATED-Alarm nicht getriggert wird).
  * DB strikt read-only (nur SELECTs); Ergebnisse gehen als JSONL-Files nach
    Documents\\_X\\staging_models\\replay\\ (keine neuen Tabellen).

Beispiele
---------
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
from datetime import datetime, timedelta

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
    MIS_SQL_INDICATOR_SELECT,
)
from core.mis_features import (
    add_advanced_features as mis1_add_features,
)
from core.funding_features import funding_features_asof, load_funding  # noqa: E402
from core.rub_features import build_rub_features, rub_event_type, rub_trend  # noqa: E402
from core import atb2_features as atb  # noqa: E402
from core.time import utc_now  # noqa: E402
from core.trade_utils import (  # noqa: E402
    calculate_smart_targets,
    compute_smart_target_levels,
    ensure_min_tp_distance,
    get_hvn_and_sr_levels,
    hvn_sr_trade_geometry,
)

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
FEE_PER_SIDE = 0.0005  # Taker 0,05% je Seite → 0,10% Round-Trip (P3.6)
DEFAULT_OUT_DIR = os.getenv(
    "KYTHERA_REPLAY_DIR", r"C:\Users\Michael\Documents\_X\staging_models\replay"
)
MAX_CPU_AT_START = 90.0  # health_monitor CPU_SATURATED nicht triggern

# Wie viele TPs der jeweilige Bot tatsächlich publiziert (Cornix-Message) —
# bestimmt die Positions-Fraktionierung im Ladder-Exit.
PUBLISHED_TARGETS = {"ufi1": 1, "td": 5, "bb": 5, "abr1": 3, "mis1": 5, "rub": 3, "atb2": 3}

# ATB2 (§11): Warmup so groß, dass EMA200 vor dem 1. Event konvergiert
# (MIN_HISTORY_CANDLES=1500 Kerzen ≈ 62,5 Tage → 65d Puffer); Cooldown je
# Richtung wie die anderen Ausbruch-Bots.
ATB2_WARMUP_DAYS = 65
ATB2_COOLDOWN_H = 4


def set_low_priority() -> None:
    """Der VPS läuft an der Lastgrenze — wir laufen mit BELOW_NORMAL.

    psutil ist im Live-venv nicht installiert → ctypes-Fallback direkt auf die
    WinAPI (BELOW_NORMAL_PRIORITY_CLASS = 0x4000).
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
        # Ohne explizite argtypes schlägt SetPriorityClass auf 64-bit still fehl
        # (HANDLE wird als c_int übergeben) — deshalb hier sauber deklariert.
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetPriorityClass.restype = wintypes.BOOL
        ok = k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000)
        print("Prozess-Priorität: BELOW_NORMAL" if ok else "WARNUNG: SetPriorityClass fehlgeschlagen")
    except Exception:
        print("WARNUNG: Priorität konnte nicht gesenkt werden")


def check_cpu_headroom() -> None:
    try:
        import psutil

        cpu = psutil.cpu_percent(interval=3)
        if cpu > MAX_CPU_AT_START:
            raise SystemExit(
                f"ABBRUCH: System-CPU bei {cpu:.0f}% (> {MAX_CPU_AT_START:.0f}%) — "
                f"Fleet nicht zusätzlich belasten (Audit Z0 / CPU_SATURATED)."
            )
        print(f"CPU-Check ok: {cpu:.0f}%")
    except SystemExit:
        raise
    except Exception:
        print("CPU-Check übersprungen (psutil nicht verfügbar)")


def import_bot_module(filename: str, module_name: str):
    """Importiert ein Bot-Modul mit Ziffern-Präfix-Dateinamen (z.B. 29_ufi1_bot.py)."""
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
    """Untere Fenstergrenze. Aware UTC über core.time (R3-Policy) — der obere
    Schnitt an der forming Kerze rechnet DB-seitig in core.candles."""
    return utc_now() - timedelta(days=int(days))


def load_ohlcv(conn, symbol: str, tf: str, days: int) -> pd.DataFrame | None:
    """OHLCV window, ASC, GESCHLOSSENE Kerzen (R1-Disziplin).

    Über core.candles statt roher f-String-SQL: der Cutoff dort ist Epoch-
    Arithmetik auf der DB-Uhr und damit für JEDEN Timeframe richtig. Die
    Nachbar-Loader schneiden mit `date_trunc('hour', NOW())` — für die 1d- und
    4h-Reads dieses Simulators wäre das zu grob und ließe die laufende Kerze
    stehen. Look-ahead hier vergiftet die Labels des gesamten Retrain-Programms.
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
    """OHLCV + Indikator-Join, wie ihn 25_smc_ml_sniper live liest — aber nur
    GESCHLOSSENE Kerzen. Live repaintet Bot 25 auf der forming Kerze (Report
    CANDLE_CALL_SITES §3); der Replay darf das nicht nachbauen, sonst lernt das
    Modell auf Kerzen, die es zur Entscheidungszeit nie gesehen hat."""
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
    # ffill schließt Innen-Lücken aus der VERGANGENHEIT. Ein bfill danach (wie in
    # 25_smc_ml_sniper:220) würde die verbleibenden Kopfzeilen aus der ZUKUNFT
    # füllen: die Warmup-Spalten (ema_200 braucht 200 Bars) sind am Anfang der
    # Coin-Historie NULL, und run_td_bb emittiert schon ab t=WINDOW-1=149. Der
    # Replay verwirft diese Zeilen stattdessen — ein Event ohne echte Indikatoren
    # ist kein Trainingsdatum (T-2026-CU-9050-045).
    df.ffill(inplace=True)
    df = df.dropna()
    if df.empty:
        return None
    return df.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# EXIT-SIMULATION — wick-aware First-Touch, SL-first, Fees, Monitor-Trailing
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
    """First-Touch-Scan über die Kerzen ab start_idx (alles NACH der Entry-Kerze).

    Zwei Ergebnisse in einem Pass:
      * outcome_tp1  — 1 wenn TP1 vor SL berührt wird, 0 wenn SL zuerst,
                       None wenn bis Datenende keins von beidem (Trade offen).
                       Bei TP1 UND SL in derselben Kerze: SL zuerst (konservativ).
      * ladder       — Cornix-Approximation: Position in 1/n gleiche Teile über
                       die publizierten TPs; Trailing wie 8_ai_trade_monitor
                       (ab TP2 rückt der SL auf targets[k-2]); Rest schließt am
                       (ggf. nachgezogenen) SL. Fees je Fill beidseitig.
    """
    is_long = direction.upper() == "LONG"
    tps = [float(t) for t in targets[:n_published]] if targets else []
    if not tps:
        return {"outcome_tp1": None, "exit_reason": "no_targets", "net_pnl_pct": 0.0}
    frac = 1.0 / len(tps)

    cur_sl = float(sl)
    next_tp = 0  # Index des nächsten offenen TP
    outcome_tp1 = None
    realized = 0.0  # Netto-PnL in % des Nominals (Summe über Teil-Fills)
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

        if sl_hit:  # SL-first bei Ambiguität — konservativ (Monitor-Konvention)
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
        # Datenende: Rest mark-to-market am letzten Close (Trade real noch offen)
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
    """Index der ersten Kerze mit open_time > ts (Exits beginnen NACH der Entry-Kerze).

    `times` ist das naive-UTC datetime64-Array aus `df["open_time"].values`
    (pandas strippt die TZ bei .values); tz-aware Eingaben werden angeglichen.
    """
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return int(np.searchsorted(times, ts.to_datetime64(), side="right"))


# ─────────────────────────────────────────────────────────────────────────────
# ADAPTER 1: UFI1 (P0.11-Validierung)
# ─────────────────────────────────────────────────────────────────────────────
def run_ufi1(conn, symbol: str, days: int, ufi1_mod) -> list[dict]:
    """Walk-forward über Daily-Kerzen mit der Bot-eigenen find_ufi1_setup().

    Entscheidungspunkt: Close jeder Daily-Kerze (der Bot scannt alle 4h mit
    Live-Preis; die Daily-Granularität ist die konservative Näherung — jedes
    Setup, das der Bot binnen des Tages genommen hätte, wird spätestens am
    Tagesschluss genommen). Entry = CMP (letzter Close), SL/TP1 aus dem Setup —
    exakt die gepostete Geometrie (Single-TP1, kein Trailing-Ladder!).
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
    # Naive UTC durchgängig — die 1h-Exit-Serie ist via .values ebenfalls naiv.
    df1d_idx.index = df1d_idx.index.tz_localize(None)
    dates = df1d_idx.index
    replay_start = dates.max() - pd.Timedelta(days=days)

    trades: list[dict] = []
    cooldown_until = None
    open_until = None

    for t in range(len(dates)):
        ts_close = dates[t] + pd.Timedelta(days=1)  # Kerze t ist ab hier geschlossen
        if dates[t] < replay_start:
            continue
        if cooldown_until is not None and ts_close < cooldown_until:
            continue
        if open_until is not None and ts_close < open_until:
            continue  # Bot-Dedup: aktiver UFI1-Trade auf dem Coin blockiert neue Signale

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
# ADAPTER 2/3: TD / BB (25_smc_ml_sniper-Erkennung, 1:1 nachgebaut)
# ─────────────────────────────────────────────────────────────────────────────
def _sniper_features(df: pd.DataFrame, idx: int, direction: str) -> dict:
    """== 25_smc_ml_sniper.extract_ml_features (Feature-Kerze idx)."""
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
    """Walk-forward der Sniper-Erkennung: pro geschlossener Kerze ein Scan über
    das 150-Kerzen-Fenster (wie live `ORDER BY open_time DESC LIMIT 150`).

    Emittiert ALLE Detektor-Events (auch die, die live am ML-Threshold oder am
    BB_1H-LONG-Parking scheitern würden) — Flag `live_gated` markiert sie. Für
    das Retraining sind alle Events Trainingsdaten; für Kalibrierungsvergleiche
    filtert man auf live_gated=False.
    """
    PIVOT_WINDOW = 10
    MAX_TD_SPAN = 50
    MAX_BB_AGE = 20
    WINDOW = 150

    df = load_joined(conn, symbol, tf, days)
    if df is None or len(df) < WINDOW + 10:
        return []

    # 1h-Serie für Smart-Targets-Fenster (live liest calculate_smart_targets
    # IMMER die 1h-Tabelle) und für die Exit-Simulation.
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
            # 1a. Bearish Three-Drive (SHORT)
            p3 = peak_idx[-1]
            if n_w - p3 <= PIVOT_WINDOW + 2:
                p1, p2 = peak_idx[-3], peak_idx[-2]
                if (p3 - p1) <= MAX_TD_SPAN and h_w[p1] < h_w[p2] < h_w[p3]:
                    if r_w[p1] > r_w[p2] > r_w[p3]:
                        try_emit("SHORT", lo_b + p3, t, False,
                                 {"p1": int(lo_b + p1), "p2": int(lo_b + p2), "p3": int(lo_b + p3)})
            # 1b. Bullish Three-Drive (LONG)
            q3 = trough_idx[-1]
            if n_w - q3 <= PIVOT_WINDOW + 2:
                q1, q2 = trough_idx[-3], trough_idx[-2]
                if (q3 - q1) <= MAX_TD_SPAN and l_w[q1] > l_w[q2] > l_w[q3]:
                    if r_w[q1] < r_w[q2] < r_w[q3]:
                        try_emit("LONG", lo_b + q3, t, False,
                                 {"p1": int(lo_b + q1), "p2": int(lo_b + q2), "p3": int(lo_b + q3)})

        elif which == "bb":
            # 2a. Breaker Block LONG (live geparkt für tf=1h → live_gated)
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
            # 2b. Breaker Block SHORT (live auf BEIDEN TFs aktiv — Parking-Lücke!)
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
# ADAPTER 4: ABR1 (18_ai_abr1_bot-Erkennung; Feature-Builder aus dem Bot-Modul)
# ─────────────────────────────────────────────────────────────────────────────
def run_abr1(conn, symbol: str, days: int, abr1_mod) -> list[dict]:
    """Walk-forward der ABR1-Erkennung: pro geschlossener 1h-Kerze prüft der
    Replay genau diese Kerze als Retest-Kandidat (== Bot-Verhalten seit dem
    Detektor-Rework 2026-07).

    Die Erkennung kommt komplett aus dem Bot-Modul (find_break_retest_setups:
    Richtungs-Kopplung des Retests, Hold-Check, Erst-Touch, bestätigte Pivots)
    — eine Quelle, kein Skew. Die Setup-Geometrie-Features des Detektors
    landen mit im Feature-Dict des Replay-Events.

    Indikatoren werden EINMAL über die Gesamtserie via Bot-Feature-Builder
    berechnet (== Trainer-Verhalten; minimale Abweichung zum 240h-Fenster des
    Bots bei rekursiven Indikatoren, dokumentiert im Report).
    """
    HIST = abr1_mod.LIVE_DATA_HISTORY_HOURS  # 240

    df = load_ohlcv(conn, symbol, "1h", days + 15)
    if df is None or len(df) < HIST + 10:
        return []

    # Feature-Builder des Bots (mit P0.12-Prefix-Fix) über die Gesamtserie
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
# ADAPTER 5: MIS1 (dichte Stichprobe je geschlossener 1h-Kerze — Retrain-Labels)
# ─────────────────────────────────────────────────────────────────────────────
MIS1_HORIZONS = (8, 24, 72, 168)  # alle Live-Horizonte des Bots; 2026-07-05 um 8/24 erweitert (vorher Report-16-Fokus 72/168)
MIS1_WARMUP = 30  # volume_sma20 (20) + Deltas; DB-Indikatoren kommen fertig aus dem Join


def load_mis1_frame(conn, symbol: str, days: int) -> pd.DataFrame | None:
    """1h-Kerzen + Indikator-Join mit der geteilten Spaltenliste aus
    core.mis_features. NUR geschlossene Kerzen (R1-Disziplin) — die laufende
    Stunde fliegt am date_trunc-Filter raus."""
    try:
        df = pd.read_sql_query(
            f"""SELECT h.open_time, h.open, h.high, h.low, h.close, h.volume,
                {MIS_SQL_INDICATOR_SELECT}
                FROM "{symbol}_1h" h
                LEFT JOIN "{symbol}_1h_indicators" i ON h.open_time = i.open_time
                WHERE h.open_time >= NOW() - INTERVAL '{int(days)} days'
                  AND h.open_time < date_trunc('hour', NOW())
                ORDER BY h.open_time ASC""",
            conn,
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


def run_mis1(conn, symbol: str, days: int, stride: int) -> list[dict]:
    """MIS1 ist NICHT detektor-gated — live scored der Bot jeden Coin jede Stunde.
    Der Replay sampelt deshalb dicht: jede `stride`-te geschlossene Kerze, mit
    deterministischem per-Coin-Offset (crc32), damit nicht alle Coins zur selben
    Marktstunde gesampelt werden (querschnittliche Zwillings-Korrelation).

    Je Sample und Richtung: Geometrie = calculate_smart_targets auf dem
    1000-Kerzen-Fenster BIS zur Entscheidungskerze (exakt die Live-Funktion),
    Exits horizontgekappt (8h/24h/72h/168h) in EINEM Lauf — Label = TP1-vor-SL
    INNERHALB des Horizonts; Timeout mit vollem Datenfenster ist eine 0,
    Datenende vor Horizontende bleibt None (wird beim Training verworfen).

    Bewusste Näherung: Entry = Close der frisch geschlossenen Kerze (der Bot
    nutzt den Live-Preis ~11 Minuten nach Stundenschluss).
    Kein Cooldown im Replay — Dedup übernimmt der Stride; die Live-Cooldowns
    drosseln nur das POSTING, nicht das Scoring."""
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

        # Level-Pool ist richtungsunabhängig → einmal rechnen, beide Richtungen
        # (bit-identisch zum Doppel-Call, Paritätstest 2026-07-05).
        try:
            pool = compute_smart_target_levels(win1h, current_price)
        except Exception:
            pool = None  # calculate_smart_targets läuft dann in den Live-Fallback

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
                    # weder TP1 noch SL berührt: mit vollem Horizontfenster eine
                    # ehrliche 0, bei Datenende vor Horizontende kein Label.
                    out = 0 if full_window else None
                rec[f"outcome_{hours}h"] = out
                rec[f"net_pnl_{hours}h"] = r["net_pnl_pct"]
                rec[f"exit_reason_{hours}h"] = r["exit_reason"]
                rec[f"r_multiple_{hours}h"] = r["r_multiple"]
            # Kompatibilität mit summarize()/load_replay(): Langhorizont als Hauptlabel
            rec["outcome_tp1"] = rec[f"outcome_{MIS1_HORIZONS[-1]}h"]
            rec["net_pnl_pct"] = rec[f"net_pnl_{MIS1_HORIZONS[-1]}h"]
            rec["r_multiple"] = rec[f"r_multiple_{MIS1_HORIZONS[-1]}h"]
            rec["risk_pct"] = r["risk_pct"]
            trades.append(rec)

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# ADAPTER 6: RUB (Rubberband Mean Reversion — Vorfilter-Events nachspielen)
# ─────────────────────────────────────────────────────────────────────────────
RUB_REG_WINDOW_H = 95 * 24   # Regressions-/Level-Fenster wie im Bot (95d-Query)
RUB_MIN_REG_ROWS = 50        # Bot: len(rows_90d) < 50 → skip
RUB_COOLDOWN_H = 4           # Live-Cooldown je Coin/Richtung (Bot 13)

RUB_SQL_INDICATORS = (
    "i.rsi_14, i.tsi_fast_12_7_7, i.tsi_fast_12_7_7_signal, "
    "i.macd_dif_normal_12_26_9, i.macd_dea_normal_12_26_9, "
    "i.atr_14, i.ema_200, i.donchian_lower_20, i.donchian_upper_20"
)


def load_rub_frame(conn, symbol: str, days: int) -> pd.DataFrame | None:
    """1h-Kerzen + exakt die Indikatoren, die Bot 13 abfragt (as-of pro Kerze).
    NUR geschlossene Kerzen (R1-Disziplin)."""
    try:
        df = pd.read_sql_query(
            f"""SELECT h.open_time, h.open, h.high, h.low, h.close, h.volume,
                {RUB_SQL_INDICATORS}
                FROM "{symbol}_1h" h
                LEFT JOIN "{symbol}_1h_indicators" i ON h.open_time = i.open_time
                WHERE h.open_time >= NOW() - INTERVAL '{int(days) + 100} days'
                  AND h.open_time < date_trunc('hour', NOW())
                ORDER BY h.open_time ASC""",
            conn,
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
    """NaN/Inf → default (Spiegel von get_f im Bot)."""
    v = arr[i]
    try:
        fv = float(v)
        return fv if np.isfinite(fv) else default
    except (TypeError, ValueError):
        return default


def run_rub1(conn, symbol: str, days: int) -> list[dict]:
    """Walk-forward des RUB-Vorfilters: je geschlossener 1h-Kerze wird die
    Rubberband-Bedingung geprüft (== stündlicher Live-Scan von Bot 13).

    EINE Quelle mit dem Bot (core/rub_features: Regression, Vorfilter,
    9-Feature-Vertrag) + Live-Geometrie as-of (get_hvn_and_sr_levels(df=...) +
    hvn_sr_trade_geometry + ensure_min_tp_distance). 4h-Cooldown je Richtung
    wie live. Zusätzlich die 6 Funding-Features (core/funding_features) im
    Feature-Dict — für den RUB2-Retrain (MODEL_INTENT §8).

    Bewusste Näherung: Entry = Close der frisch geschlossenen Kerze (der Bot
    nutzt den Preis kurz nach Stundenschluss)."""
    df = load_rub_frame(conn, symbol, days)
    if df is None or len(df) < RUB_MIN_REG_ROWS + 2:
        return []

    fund_by_sym = load_funding(conn, [symbol])

    t1h = df["open_time"].values
    h1h, l1h, c1h = df["high"].values, df["low"].values, df["close"].values
    ts_sec = df["open_time"].astype("int64").to_numpy() / 1e9
    n = len(df)

    # Replay-Fenster: die Warmup-Historie (Regressions-Lookback) liegt VOR dem
    # angeforderten Zeitraum, Events entstehen nur in den letzten `days` Tagen.
    start_t = max(RUB_MIN_REG_ROWS, n - days * 24)

    # t1h stammt aus .values → naive UTC-datetime64; Cooldown-Marker ebenfalls naiv.
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

        # Regression erst NACH einem billigen Vor-Vorfilter? Nein — dist_to_trend
        # steckt in der Bedingung selbst; die Closed-Form-Regression ist billig.
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


def run_atb2(conn, symbol: str, days: int) -> list[dict]:
    """Walk-forward des ATB2-Converging-Channel-Detektors (MODEL_INTENT §11).

    EINE Quelle mit Bot 14: ``core.atb2_features`` (bestätigte Pivots, Kanal-Fit,
    geschlossener Ausbruch, Feature-Vertrag). Je geschlossener 1h-Kerze wird
    geprüft, ob ein konvergierender Kanal (Wedge/Triangle/Pennant) ausbricht.

    Label-Geometrie = Measured-Move (§11: ⅓/⅔/1× Kanalbreite) — die
    kanal-native Geometrie, die der Bot postet (kein DB-Level-Pool nötig →
    Train==Serve exakt). Zusätzlich werden die Fleet-Smart-Targets derselben
    Kerze simuliert und als Vergleich (``smart_*``) ins Record geschrieben —
    §11 will Measured-Move GEGEN Smart-Targets im Replay bewertet sehen, ohne
    dafür die Trainings-Label-Quelle zu verwässern.

    4h-Cooldown je Richtung; Entry = Close der frisch geschlossenen
    Ausbruchskerze."""
    df = load_ohlcv(conn, symbol, "1h", days + ATB2_WARMUP_DAYS)
    # hist deckt Kanal-Lookback UND EMA200-Konvergenz (Paritäts-Kontrakt) ab.
    hist = max(atb.CHANNEL_MAX_SPAN + atb.CONFIRM_BARS + atb.ATR_PERIOD, atb.MIN_HISTORY_CANDLES)
    if df is None or len(df) < hist + 2:
        return []
    df_ind = atb.compute_indicators(df)
    t1h = df["open_time"].values
    H, L, C = df["high"].values, df["low"].values, df["close"].values

    start_t = max(hist, len(df) - days * 24)
    cooldown = {"LONG": pd.Timestamp.min, "SHORT": pd.Timestamp.min}
    trades: list[dict] = []
    # -1: simulate_exit braucht mindestens eine Folgekerze nach dem Break.
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

        # §11-Vergleich: dieselbe Kerze mit den Fleet-Smart-Targets.
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
    ap = argparse.ArgumentParser(description="Walk-Forward-Simulator (P0.10/P0.11)")
    ap.add_argument("--strategy", required=True,
                    choices=["ufi1", "td", "bb", "abr1", "mis1", "rub", "atb2"])
    ap.add_argument("--tf", default="1h", choices=["1h", "4h"], help="nur für td/bb")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--stride", type=int, default=24,
                    help="mis1: jede N-te geschlossene Kerze sampeln (per-Coin-Offset dedupliziert Marktstunden)")
    ap.add_argument("--coins", default=None, help="Kommagetrennte Liste; Default: coins.json")
    ap.add_argument("--limit", type=int, default=None, help="nur die ersten N Coins")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    ap.add_argument("--resume", action="store_true",
                    help="an bestehendes JSONL anhängen und bereits enthaltene Coins überspringen")
    args = ap.parse_args()

    # cp1252-Konsole: Emojis/Sonderzeichen in Fehlermeldungen dürfen den Lauf
    # nicht per UnicodeEncodeError abbrechen.
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
                    continue  # abgeschnittene letzte Zeile eines abgebrochenen Laufs
                all_trades.append(tr)
                done_symbols.add(tr["symbol"])
        # Der zuletzt geschriebene Coin könnte unvollständig sein → neu rechnen.
        if all_trades:
            last_sym = all_trades[-1]["symbol"]
            all_trades = [t for t in all_trades if t["symbol"] != last_sym]
            done_symbols.discard(last_sym)
        print(f"Resume: {len(done_symbols)} Coins / {len(all_trades)} Trades übernommen")

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
        # Auch bei Resume konsolidiert neu schreiben (übernommene Trades zuerst).
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
                        else:
                            trades = run_abr1(conn, symbol, args.days, abr1_mod)
                        break
                    except Exception as e:
                        print(f"  !! {symbol} (Versuch {attempt}): {e}")
                        # Tote Connection (z.B. DB-Neustart/Idle-Kill nach Stunden)
                        # nicht den ganzen Lauf reißen lassen — reconnecten.
                        try:
                            conn.rollback()
                        except Exception:
                            try:
                                conn.close()
                            except Exception:
                                pass
                            try:
                                conn = fresh_conn()
                                print(f"  ↻ DB-Reconnect vor erneutem Versuch von {symbol}")
                            except Exception as e2:
                                print(f"  ↻ Reconnect fehlgeschlagen: {e2}")
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
                    print(f"[{i}/{len(coins)}] {symbol}: total {len(all_trades)} Trades ({el:.0f}s)", flush=True)
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
