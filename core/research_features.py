# core/research_features.py — geteilte Feature-Builder der Research-Bots 30–33
# (PEX1, FMR1, TRM1, FIF1 — Report 15: S6, S8, S10, S11).
#
# EINE Quelle für Bot, Dataset-Builder und Trainer (X-R-Fix "Trainer importiert
# den Feature-Builder des Bots", vgl. core/mis_features.py / core/aim2_features.py).
# Jedes Feature ist skalenfrei (%, Ratio, Oszillator, Flag) — Preisskala-Spalten
# gehen hier konstruktiv nicht rein (Report-13-Leakage-Klasse 13-P1).

from __future__ import annotations

import math

import numpy as np
import pandas as pd

# ── Gemeinsamer Markt-Kontext (1h-Kerzen + Indikator-Join) ───────────────────
# SELECT-Fragment für den Indikator-Join (h = Kerzen-Tabelle, i = Indikatoren).
CONTEXT_SQL_SELECT = """
    i.rsi_14, i.ema_21, i.ema_200, i.atr_14,
    i.boll_upper_20, i.boll_lower_20
"""

CONTEXT_FEATURES = [
    "ret_1h_pct",
    "ret_4h_pct",
    "ret_24h_pct",
    "atr_14_pct",
    "ctx_rsi_14",
    "vol_ratio_sma20",
    "dist_ema21_pct",
    "dist_ema200_pct",
    "boll_pos_20",
]

# Mindestfenster, damit ret_24h + SMA20 rechenbar sind.
CONTEXT_MIN_CANDLES = 30


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    if b is None or a is None or not np.isfinite(a) or not np.isfinite(b) or b == 0:
        return default
    return a / b


def candle_context_features(df: pd.DataFrame, idx: int) -> dict:
    """Skalenfreier Markt-Kontext der Kerze bei ``idx`` (letzte GESCHLOSSENE Kerze).

    ``df``: chronologisch ASC sortiert, Spalten close, volume + CONTEXT_SQL_SELECT.
    Der Aufrufer garantiert idx >= CONTEXT_MIN_CANDLES − 1 (sonst ValueError) —
    kein stilles fillna über ein zu kurzes Fenster (P0.12-Fehlermodus).
    """
    if idx < CONTEXT_MIN_CANDLES - 1:
        raise ValueError(f"Kontext-Fenster zu kurz (idx={idx}, min={CONTEXT_MIN_CANDLES - 1})")

    close = float(df["close"].iloc[idx])
    if close <= 0:
        raise ValueError("close <= 0 — Kontext nicht berechenbar")

    def ret_pct(back: int) -> float:
        prev = float(df["close"].iloc[idx - back])
        return _safe_div(close - prev, prev) * 100.0

    vol = float(df["volume"].iloc[idx])
    vol_sma20 = float(df["volume"].iloc[idx - 19 : idx + 1].mean())

    def num(col: str, default: float) -> float:
        # NaN ist truthy — `to_numeric(...) or default` würde den Default nie
        # treffen und legitime 0.0-Werte überschreiben (Review-Fix 2026-07-06).
        v = pd.to_numeric(df[col].iloc[idx], errors="coerce")
        return default if pd.isna(v) else float(v)

    atr = num("atr_14", 0.0)
    rsi = num("rsi_14", 50.0)
    ema21 = num("ema_21", 0.0)
    ema200 = num("ema_200", 0.0)
    b_up = num("boll_upper_20", 0.0)
    b_lo = num("boll_lower_20", 0.0)

    feats = {
        "ret_1h_pct": ret_pct(1),
        "ret_4h_pct": ret_pct(4),
        "ret_24h_pct": ret_pct(24),
        "atr_14_pct": _safe_div(atr, close) * 100.0,
        "ctx_rsi_14": rsi,
        "vol_ratio_sma20": _safe_div(vol, vol_sma20, 1.0),
        "dist_ema21_pct": _safe_div(close - ema21, ema21) * 100.0,
        "dist_ema200_pct": _safe_div(close - ema200, ema200) * 100.0,
        "boll_pos_20": _safe_div(close - b_lo, b_up - b_lo, 0.5),
    }
    # Imputation identisch Bot == Trainer (P2.34): inf → 0, NaN → 0.
    return {k: (float(v) if np.isfinite(v) else 0.0) for k, v in feats.items()}


# ── Regime-Kontext (regime_history / regime_current) ─────────────────────────
REGIME_CLASSES = ["TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOLA", "TRANSITION"]

REGIME_FEATURES = [f"regime_is_{r}" for r in REGIME_CLASSES] + ["regime_conf", "regime_age_min"]


def regime_features(regime_row: dict | None, age_min: float) -> dict:
    """One-Hot des Regimes + Confidence + Alter. ``regime_row=None`` (keine
    Historie verfügbar) → alle Hots 0, conf 0, Alter gedeckelt 360 min."""
    out = {f"regime_is_{r}": 0.0 for r in REGIME_CLASSES}
    conf = 0.0
    if regime_row is not None:
        r = str(regime_row.get("regime", "")).upper()
        if f"regime_is_{r}" in out:
            out[f"regime_is_{r}"] = 1.0
        c = regime_row.get("confidence")
        conf = float(c) if c is not None and np.isfinite(float(c)) else 0.0
    out["regime_conf"] = conf
    out["regime_age_min"] = float(min(age_min, 360.0))
    return out


# ── S6 / PEX1 — Pump-Exhaustion-Short ────────────────────────────────────────
# Event-Features = exakt die 4 Messwerte, die 10_pump_dump_detector.py in
# pump_dump_events schreibt (die Indikator-Spalten der Tabelle bleiben seit
# P1.40 NULL — deshalb kommt der Indikator-Kontext aus dem 1h-Join).
PEX1_EVENT_FEATURES = ["ev_volume_ratio", "ev_price_change_60s", "ev_buy_pressure", "ev_volatility"]

PEX1_FEATURES = PEX1_EVENT_FEATURES + CONTEXT_FEATURES

# Gate wie im Training UND im EPD1-Live-Pfad (Report 13 EPD1-P0): nur Events
# mit volume_ratio >= 5 sind in-distribution. Pumps = positive 60s-Änderung.
PEX1_MIN_VOL_RATIO = 5.0
PEX1_MIN_PUMP_PCHG_60S = 1.5


def build_pex1_row(event: dict, df: pd.DataFrame, idx: int) -> dict:
    feats = {
        "ev_volume_ratio": float(event["volume_ratio"]),
        "ev_price_change_60s": float(event["price_change_60s"]),
        "ev_buy_pressure": float(event["buy_pressure"]),
        "ev_volatility": float(event["volatility"]),
    }
    feats.update(candle_context_features(df, idx))
    return feats


# ── S8 / FMR1 — Funding-Extreme Mean-Reversion ───────────────────────────────
FMR1_FEATURES = [
    "funding_rate_bps",  # aktuelle Rate in Basispunkten (rate × 1e4)
    "funding_cs_pctl",  # Cross-Sectional-Perzentil über alle Coins (0..1)
    "funding_z_30d",  # Z-Score gegen die eigenen letzten 90 Settlements
    "funding_delta_8h_bps",  # Änderung gegen das vorherige Settlement
    "funding_sum_3d_bps",  # kumulierte Rate der letzten 9 Settlements (Carry)
    "side_short",  # 1 = SHORT (Top-Extrem), 0 = LONG (Bottom-Extrem)
] + CONTEXT_FEATURES

# Cross-Sectional-Extrem-Gates (Report 15 S8): oberstes/unterstes Perzentil.
FMR1_SHORT_PCTL = 0.95
FMR1_LONG_PCTL = 0.05
FMR1_HISTORY_SETTLEMENTS = 90  # 30 Tage à 3 Settlements


def funding_stats(rates: list[float]) -> dict:
    """Statistik-Features aus der Settlement-Historie EINES Symbols.

    ``rates``: chronologisch ASC, letztes Element = aktuellste Rate.
    Braucht >= 10 Settlements, sonst ValueError (kein stilles Default-Raten).
    """
    if len(rates) < 10:
        raise ValueError(f"Funding-Historie zu kurz ({len(rates)} Settlements)")
    arr = np.asarray(rates, dtype=np.float64) * 1e4  # → bps
    cur = float(arr[-1])
    hist = arr[-FMR1_HISTORY_SETTLEMENTS:]
    std = float(hist.std())
    return {
        "funding_rate_bps": cur,
        "funding_z_30d": _safe_div(cur - float(hist.mean()), std),
        "funding_delta_8h_bps": cur - float(arr[-2]),
        "funding_sum_3d_bps": float(arr[-9:].sum()),
    }


def build_fmr1_row(stats: dict, cs_pctl: float, side: str, df: pd.DataFrame, idx: int) -> dict:
    feats = dict(stats)
    feats["funding_cs_pctl"] = float(cs_pctl)
    feats["side_short"] = 1.0 if side.upper() == "SHORT" else 0.0
    feats.update(candle_context_features(df, idx))
    return feats


# ── S10 / TRM1 — Transition-Resolution ───────────────────────────────────────
# Fenster = die letzten TRM1_WINDOW_CHECKS regime_history-Zeilen (5-min-Raster)
# bis einschließlich des Events. Alle Inputs sind bereits skalenfrei.
TRM1_WINDOW_CHECKS = 12  # 1h Historie

TRM1_FEATURES = [
    "btc_return_1h",
    "btc_return_4h",
    "btc_atr_1h_pct",
    "btc_atr_4h_pct",
    "btcdom_return_24h",
    "confidence_btc",
    "confidence_alt",
    "minutes_in_transition",
    "frac_up_1h",
    "frac_down_1h",
    "frac_chop_1h",
    "frac_highvola_1h",
    "btc_ret4h_delta_1h",
    "btc_ret4h_mean_1h",
    "btc_atr4h_delta_1h",
]

# Klassen-Vertrag des TRM1-Modells (multi:softprob) — Trainer UND Bot lesen ihn
# von hier: 0 = keine handelbare Auflösung, 1 = LONG-These, 2 = SHORT-These.
TRM1_CLASS_OTHER = 0
TRM1_CLASS_UP = 1
TRM1_CLASS_DOWN = 2


def build_trm1_row(window_rows: list[dict], minutes_in_transition: float) -> dict:
    """``window_rows``: chronologisch ASC, letzte Zeile = aktueller Check.

    Braucht mindestens 2 Zeilen; die Fraktions-Features rechnen über das
    tatsächlich vorhandene Fenster (Lücken im 5-min-Raster sind live möglich).
    """
    if len(window_rows) < 2:
        raise ValueError("TRM1-Fenster braucht >= 2 regime_history-Zeilen")
    window_rows = window_rows[-TRM1_WINDOW_CHECKS:]
    cur = window_rows[-1]

    def f(row: dict, key: str) -> float:
        v = row.get(key)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        return v if np.isfinite(v) else 0.0

    regimes = [str(r.get("regime", "")).upper() for r in window_rows]
    n = float(len(regimes))
    ret4h_series = [f(r, "btc_return_4h") for r in window_rows]
    atr4h_series = [f(r, "btc_atr_4h_pct") for r in window_rows]

    return {
        "btc_return_1h": f(cur, "btc_return_1h"),
        "btc_return_4h": f(cur, "btc_return_4h"),
        "btc_atr_1h_pct": f(cur, "btc_atr_1h_pct"),
        "btc_atr_4h_pct": f(cur, "btc_atr_4h_pct"),
        "btcdom_return_24h": f(cur, "btcdom_return_24h"),
        "confidence_btc": f(cur, "confidence_btc"),
        "confidence_alt": f(cur, "confidence_alt"),
        "minutes_in_transition": float(min(minutes_in_transition, 1440.0)),
        "frac_up_1h": regimes.count("TREND_UP") / n,
        "frac_down_1h": regimes.count("TREND_DOWN") / n,
        "frac_chop_1h": regimes.count("CHOP") / n,
        "frac_highvola_1h": regimes.count("HIGH_VOLA") / n,
        "btc_ret4h_delta_1h": ret4h_series[-1] - ret4h_series[0],
        "btc_ret4h_mean_1h": float(np.mean(ret4h_series)),
        "btc_atr4h_delta_1h": atr4h_series[-1] - atr4h_series[0],
    }


# ── S11 / FIF1 — FIFO-Filter (Meta-Klassifier über Fast-In-And-Out-Signale) ──
FIF1_FEATURES = (
    ["side_short"] + CONTEXT_FEATURES + REGIME_FEATURES + ["fifo_same_dir_24h", "fifo_fleet_1h", "hod_sin", "hod_cos"]
)


def build_fif1_row(
    direction: str,
    df: pd.DataFrame,
    idx: int,
    regime_row: dict | None,
    regime_age_min: float,
    fifo_same_dir_24h: int,
    fifo_fleet_1h: int,
    ts,
) -> dict:
    """``ts``: Signalzeitpunkt (naive UTC) — nur für die Tageszeit-Features."""
    ts = pd.Timestamp(ts)
    hod = ts.hour + ts.minute / 60.0
    feats = {
        "side_short": 1.0 if direction.upper() == "SHORT" else 0.0,
        "fifo_same_dir_24h": float(fifo_same_dir_24h),
        "fifo_fleet_1h": float(fifo_fleet_1h),
        "hod_sin": math.sin(2 * math.pi * hod / 24.0),
        "hod_cos": math.cos(2 * math.pi * hod / 24.0),
    }
    feats.update(candle_context_features(df, idx))
    feats.update(regime_features(regime_row, regime_age_min))
    return feats


# ── Gemeinsamer Kontext-Frame-Fetch (Bots 30/31/33) ──────────────────────────
# Spiegel des Trainings-Gates MAX_JOIN_STALENESS_H (tools/research_dataset_common):
# eine Feature-Kerze, die älter als 3h relativ zum Entscheidungszeitpunkt ist,
# hätte das Training verworfen — live darf sie kein Signal speisen (Review-Fix
# 2026-07-06: vorher fehlte der Guard, Ingestion-Lag → Signale auf Stunden-alten
# Preisen).
CONTEXT_MAX_STALENESS_H = 3


def fetch_context_frame(conn, symbol: str, lookback: int = 60, as_of=None):
    """Letzte 1h-Kerzen + Kontext-Indikatoren (CONTEXT_SQL_SELECT-Join).

    ``as_of``: Entscheidungszeitpunkt (naive UTC oder aware; Default = jetzt).
    Die Feature-Kerze ist die letzte GESCHLOSSENE Kerze VOR der as_of-Stunde —
    exakt der floor-1-Join der Dataset-Builder (Training-Serving-Parität, R1).
    Event-Bots (PEX1) übergeben die Event-Zeit, damit ein über eine
    Stundengrenze verarbeitetes Event dieselbe Kerze sieht wie im Training.

    Rückgabe ``(df ASC, idx der Feature-Kerze)`` oder None bei zu wenig Daten
    oder wenn die Feature-Kerze staler als CONTEXT_MAX_STALENESS_H ist.
    """
    import datetime as _dt

    query = f"""
        SELECT h.open_time, h.close, h.volume,
               {CONTEXT_SQL_SELECT}
        FROM "{symbol}_1h" h
        LEFT JOIN "{symbol}_1h_indicators" i ON h.open_time = i.open_time
        ORDER BY h.open_time DESC LIMIT {int(lookback)}
    """
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
        if len(rows) < CONTEXT_MIN_CANDLES + 1:
            return None
        cols = [d[0] for d in cur.description]
    df = pd.DataFrame(rows, columns=cols).iloc[::-1].reset_index(drop=True)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True).dt.tz_localize(None)
    for c in df.columns:
        if c != "open_time":
            df[c] = pd.to_numeric(df[c], errors="coerce")

    if as_of is None:
        as_of = _dt.datetime.now(_dt.timezone.utc)
    as_of = pd.Timestamp(as_of)
    if as_of.tzinfo is not None:
        as_of = as_of.tz_convert("UTC").tz_localize(None)
    cur_hour = as_of.floor("h")

    times = df["open_time"]
    idx = int(times.searchsorted(cur_hour, side="left")) - 1  # letzte Kerze VOR der as_of-Stunde
    if idx < CONTEXT_MIN_CANDLES - 1:
        return None
    if (cur_hour - times.iloc[idx]) > pd.Timedelta(hours=CONTEXT_MAX_STALENESS_H):
        return None  # stale Join — Training hätte das Event verworfen
    return df, idx


# ── Gemeinsame Assertion (P0.12-Muster) ──────────────────────────────────────
def assert_features_alive(
    rows: list[dict], feature_cols: list[str], binary_ok: set[str] | None = None, context: str = ""
) -> None:
    """ "Kein kontinuierliches Feature konstant" über eine Stichprobe von
    Feature-Dicts. ``binary_ok``: Flags, die legitim konstant sein dürfen."""
    if not rows:
        raise ValueError(f"Feature-Assertion{context}: leere Stichprobe")
    binary_ok = binary_ok or set()
    df = pd.DataFrame(rows)
    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Feature-Assertion{context}: Spalten fehlen: {missing}")
    continuous = [c for c in feature_cols if c not in binary_ok]
    constant = [c for c in continuous if df[c].nunique(dropna=False) <= 1]
    if constant:
        raise ValueError(f"Feature-Assertion{context}: konstante Features: {constant}")
