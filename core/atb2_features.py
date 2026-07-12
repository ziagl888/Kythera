# core/atb2_features.py
"""ATB2 — Converging-Channel Breakout: geteilte Detektions- und Feature-Logik.

EINE Quelle für Bot 14 (Serving), ``tools/walkforward_sim.py`` (Labeling) und
``tools/retrain_from_replay.py`` (Training) — X-R1-Regel, kein Train/Serve-Skew.

Ersetzt den alten Einzel-Trendlinien-Detektor (90d-Close-Regressionsgerade,
ATB1, Audit-Note D: „Das Modell sah nie das Event, das es scored"). Neu-Design
gemäß ``docs/MODEL_INTENT.md`` §11 (Michi, 2026-07-07): konvergierende Kanäle
(Wedge/Triangle/Pennant) aus BESTÄTIGTEN Swing-Pivots, Ausbruch mit
geschlossenem Kerzenschluss. Die fünf WillyAlgoTrader-Faktoren
(Penetrationstiefe/ATR, Body-Ratio, Body-Commitment, Volumen-Spike,
RSI-Momentum) gehen NICHT als handgewichteter Score ein, sondern als
Setup-Features fürs XGB-Gate — analog ``18_ai_abr1_bot.GEOMETRY_FEATURES``.

Kontrakte
---------
* **No-Repaint:** nur Pivots mit ``CONFIRM_BARS`` Kerzen auf BEIDEN Seiten; der
  Ausbruch wird nur auf einer geschlossenen Kerze bewertet. Der Aufrufer muss
  die Forming-Candle vorher abschneiden (R1).
* **Scale-free:** jedes Feature ist ein Prozentwert, ein ATR-Vielfaches, ein
  Oszillator oder ein Flag — nie ein absoluter Preis (Ticker-Leakage-Regel).
* **Selbst-enthaltene Indikatoren:** ATR/RSI/EMA werden hier deterministisch aus
  OHLCV berechnet (Wilder), keine ``pandas_ta``-Versionsabhängigkeit (P0.12),
  keine DB-Indikatorspalten nötig — Bot, Simulator und Trainer rechnen identisch.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Detektor-Parameter (§11, objektiv reproduzierbar)                            #
# --------------------------------------------------------------------------- #
CONFIRM_BARS = 5  # Pivot braucht CONFIRM_BARS Kerzen auf beiden Seiten
MIN_TOUCHES = 3  # §11: Mindest-Berührungen je Kanalgrenze (3 statt 2)
TOUCH_TOL_ATR = 0.15  # §11: Touch-Toleranz 0,15 × ATR
CONVERGENCE_MIN = 0.02  # §11: Verengung ≥ 2 % über das Kanalfenster
WIDTH_MIN_ATR = 0.5  # §11: Kanalbreite 0,5 … 120 × ATR
WIDTH_MAX_ATR = 120.0
VOL_CONTRACTION_MAX = 0.85  # §11: In-Kanal-Volumen < 85 % des Vorlaufs
CHANNEL_MAX_SPAN = 120  # längstes betrachtetes Konsolidierungsfenster (Kerzen)
CHANNEL_MIN_SPAN = 20  # kürzestes Fenster, in dem ein Kanal gültig sein kann

ATR_PERIOD = 14
RSI_PERIOD = 14
EMA_PERIOD = 200
VOL_AVG_WINDOW = 20
RSI_MOMENTUM_LOOKBACK = 3  # Kerzen für RSI-Delta

# Train/Serve-Paritäts-Kontrakt (X-R1): EMA200 ist long-memory. Bot, Simulator
# und Trainer müssen für denselben Zeitstempel VOR der Entscheidungskerze
# mindestens so viele Kerzen geladen haben, dass der SMA-Seed ausgedämpft ist
# ((199/201)^1300 ≈ 2·10⁻⁶) — sonst driften dist_ema200/atr_pct/rsi je nach
# geladener Fensterlänge auseinander. Simulator setzt start_t entsprechend;
# der Bot-Serving-Pfad MUSS ≥ diese Historie laden.
MIN_HISTORY_CANDLES = 1500

#: Der ATB2-Feature-Vertrag (Spaltennamen == meta.features des Artefakts).
ATB2_FEATURES = [
    # --- 5 WillyAlgoTrader-Setup-Faktoren (als Features, nicht als Score) ---
    "pen_depth_atr",  # Penetrationstiefe des Ausbruchs / ATR
    "body_ratio",  # |close-open| / (high-low) der Ausbruchskerze
    "body_commitment",  # Close-Position Richtung Ausbruch (0..1)
    "vol_spike",  # Ausbruchsvolumen / rollierender 20er-Schnitt
    "rsi_momentum",  # RSI[break] - RSI[break-3]
    # --- Kanal-Geometrie ---
    "chan_width_atr",  # Kanalbreite am Ausbruch / ATR
    "chan_convergence",  # relative Verengung über das Fenster
    "chan_touch_upper",  # bestätigte Berührungen der Oberkante
    "chan_touch_lower",  # bestätigte Berührungen der Unterkante
    "chan_slope_upper_atr",  # Steigung Oberkante (ATR/Kerze)
    "chan_slope_lower_atr",  # Steigung Unterkante (ATR/Kerze)
    "chan_span",  # Kanallänge in Kerzen
    "chan_vol_contraction",  # In-Kanal-Volumen / Vorlauf-Volumen
    # --- Kanaltyp (One-Hot, dürfen konstant sein) ---
    "is_wedge",
    "is_triangle",
    "is_pennant",
    # --- Kontext ---
    "atr_pct",  # ATR / close
    "dist_ema200",  # (close - EMA200) / EMA200
    "rsi",  # RSI[break]
    "break_up",  # 1 = Ausbruch nach oben (LONG), 0 = nach unten
]

#: Binär-Flags dürfen über ein einzelnes Coin-Fenster legitim konstant sein und
#: werden von der Startup-Assertion nicht hart geprüft (ABR/MIS-Muster).
BINARY_FLAG_FEATURES = {"is_wedge", "is_triangle", "is_pennant", "break_up"}

REQUIRED_INPUT_COLS = ["open", "high", "low", "close", "volume"]


# --------------------------------------------------------------------------- #
# Deterministische Indikatoren (Wilder) — DB-frei, versionsstabil             #
# --------------------------------------------------------------------------- #
def _wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Average True Range nach Wilder. Rückgabe gleiche Länge wie Input."""
    n = len(close)
    tr = np.empty(n, dtype=float)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i] - close[i - 1])
        tr[i] = max(hl, hc, lc)
    atr = np.full(n, np.nan, dtype=float)
    if n < period:
        return atr
    atr[period - 1] = tr[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def _wilder_rsi(close: np.ndarray, period: int) -> np.ndarray:
    """Relative Strength Index nach Wilder (eindomänig, T-097-konform)."""
    n = len(close)
    rsi = np.full(n, np.nan, dtype=float)
    if n <= period:
        return rsi
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = gain[:period].mean()
    avg_loss = loss[:period].mean()
    for i in range(period, n):
        g = gain[i - 1]
        loss_i = loss[i - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + loss_i) / period
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def _ema(close: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average, **SMA-seeded** (wie ta-lib/pandas_ta).

    Der erste Wert liegt bei Index ``period-1`` = SMA der ersten ``period``
    Closes; davor NaN. Das entfernt die willkürliche ``close[0]``-Verankerung —
    entscheidend für Train/Serve-Parität (X-R1): mit genügend Warmup
    (`MIN_HISTORY_CANDLES`) konvergiert die Kurve, sodass Bot, Simulator und
    Trainer für denselben Zeitstempel denselben EMA200 rechnen, egal wie lang
    das jeweils geladene Fenster ist.
    """
    n = len(close)
    ema = np.full(n, np.nan, dtype=float)
    if n < period:
        return ema
    alpha = 2.0 / (period + 1.0)
    ema[period - 1] = close[:period].mean()
    for i in range(period, n):
        ema[i] = alpha * close[i] + (1.0 - alpha) * ema[i - 1]
    return ema


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Fügt ATR/RSI/EMA200/vol_avg_20 als Spalten hinzu (eine Quelle für alle).

    Erwartet chronologisch aufsteigende OHLCV-Kerzen. Hard-Error bei fehlenden
    Eingangsspalten (kein stilles ``fillna(0)`` — P0.12-Lektion).
    """
    missing = [c for c in REQUIRED_INPUT_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"ATB2 compute_indicators: Eingangsspalten fehlen: {missing}")
    out = df.copy()
    high = out["high"].to_numpy(dtype=float)
    low = out["low"].to_numpy(dtype=float)
    close = out["close"].to_numpy(dtype=float)
    out["atr"] = _wilder_atr(high, low, close, ATR_PERIOD)
    out["rsi"] = _wilder_rsi(close, RSI_PERIOD)
    out["ema_200"] = _ema(close, EMA_PERIOD)
    out["vol_avg_20"] = out["volume"].rolling(window=VOL_AVG_WINDOW).mean()
    return out


# --------------------------------------------------------------------------- #
# Pivots (No-Repaint) und Kanal-Fit                                           #
# --------------------------------------------------------------------------- #
def find_confirmed_pivots(high: np.ndarray, low: np.ndarray, confirm_bars: int = CONFIRM_BARS):
    """Bestätigte Swing-Pivots: Extremum mit ``confirm_bars`` Kerzen auf BEIDEN
    Seiten. Der Rand wird hart ausgeschlossen — die letzten ``confirm_bars``
    Kerzen sind noch unbestätigt und dürfen den Kanal nicht mitformen (Repaint,
    ABR-R07-b-Lektion).

    Rückgabe: ``(highs, lows)`` — je Liste aus ``(index, price)``.
    """
    n = len(high)
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(confirm_bars, n - confirm_bars):
        window_hi = high[i - confirm_bars : i + confirm_bars + 1]
        window_lo = low[i - confirm_bars : i + confirm_bars + 1]
        if high[i] >= window_hi.max():
            highs.append((i, float(high[i])))
        if low[i] <= window_lo.min():
            lows.append((i, float(low[i])))
    return highs, lows


def _fit_line(points: list[tuple[int, float]]):
    """Least-Squares-Gerade durch (index, price). Rückgabe (slope, intercept)."""
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    A = np.vstack([xs, np.ones(len(xs))]).T
    slope, intercept = np.linalg.lstsq(A, ys, rcond=None)[0]
    return float(slope), float(intercept)


def _count_touches(points, slope, intercept, tol) -> int:
    """Wie viele Pivots liegen innerhalb ``tol`` (Preis) an der Gerade?"""
    return sum(1 for idx, price in points if abs(price - (slope * idx + intercept)) <= tol)


def fit_channel(df_ind: pd.DataFrame, break_idx: int):
    """Fittet einen konvergierenden Kanal im Fenster VOR ``break_idx``.

    ``df_ind`` muss ``compute_indicators`` durchlaufen haben (Spalte ``atr``).
    Das Konsolidierungsfenster endet bei ``break_idx - 1`` (die Ausbruchskerze
    selbst gehört nicht in den Fit) und umfasst höchstens ``CHANNEL_MAX_SPAN``
    Kerzen. Validiert §11-Kriterien: ≥3 bestätigte Berührungen je Kante,
    Konvergenz ≥2 %, Breite 0,5…120×ATR, Volumen-Kontraktion <85 %.

    Rückgabe: ein ``channel``-dict oder ``None``.
    """
    if break_idx < CHANNEL_MIN_SPAN + CONFIRM_BARS:
        return None
    atr = df_ind["atr"].to_numpy(dtype=float)
    atr_end = atr[break_idx - 1]
    if not np.isfinite(atr_end) or atr_end <= 0:
        return None

    win_start = max(0, break_idx - 1 - CHANNEL_MAX_SPAN)
    high = df_ind["high"].to_numpy(dtype=float)
    low = df_ind["low"].to_numpy(dtype=float)
    vol = df_ind["volume"].to_numpy(dtype=float)

    # Pivots relativ zum Fensteranfang; Fit im Index-Raum des Gesamt-df.
    win_high = high[win_start:break_idx]
    win_low = low[win_start:break_idx]
    highs_rel, lows_rel = find_confirmed_pivots(win_high, win_low)
    highs = [(win_start + i, p) for i, p in highs_rel]
    lows = [(win_start + i, p) for i, p in lows_rel]
    if len(highs) < MIN_TOUCHES or len(lows) < MIN_TOUCHES:
        return None

    up_slope, up_int = _fit_line(highs)
    lo_slope, lo_int = _fit_line(lows)

    tol = TOUCH_TOL_ATR * atr_end
    n_up = _count_touches(highs, up_slope, up_int, tol)
    n_lo = _count_touches(lows, lo_slope, lo_int, tol)
    if n_up < MIN_TOUCHES or n_lo < MIN_TOUCHES:
        return None

    # Kanalspanne = von der ersten genutzten Pivotkerze bis zur Kerze vor Break.
    span_start = min(highs[0][0], lows[0][0])
    span_end = break_idx - 1
    span = span_end - span_start
    if span < CHANNEL_MIN_SPAN:
        return None

    width_start = (up_slope * span_start + up_int) - (lo_slope * span_start + lo_int)
    width_end = (up_slope * span_end + up_int) - (lo_slope * span_end + lo_int)
    # Oberkante muss über Unterkante liegen; sonst ist es kein Kanal.
    if width_start <= 0 or width_end <= 0:
        return None
    convergence = (width_start - width_end) / width_start
    if convergence < CONVERGENCE_MIN:
        return None
    width_atr = width_end / atr_end
    if not (WIDTH_MIN_ATR <= width_atr <= WIDTH_MAX_ATR):
        return None

    # Volumen-Kontraktion: In-Kanal-Volumen vs. gleich langer Vorlauf.
    in_vol = vol[span_start : span_end + 1]
    pre_start = max(0, span_start - (span + 1))
    pre_vol = vol[pre_start:span_start]
    if len(pre_vol) == 0 or np.nanmean(pre_vol) <= 0:
        return None
    vol_contraction = float(np.nanmean(in_vol) / np.nanmean(pre_vol))
    if vol_contraction >= VOL_CONTRACTION_MAX:
        return None

    channel_type = _classify_channel(up_slope, lo_slope, atr_end)
    return {
        "up_slope": up_slope,
        "up_int": up_int,
        "lo_slope": lo_slope,
        "lo_int": lo_int,
        "span_start": int(span_start),
        "span_end": int(span_end),
        "span": int(span),
        "width_start": float(width_start),
        "width_end": float(width_end),
        "convergence": float(convergence),
        "n_touch_upper": int(n_up),
        "n_touch_lower": int(n_lo),
        "vol_contraction": vol_contraction,
        "atr": float(atr_end),
        "channel_type": channel_type,
    }


def _classify_channel(up_slope: float, lo_slope: float, atr: float) -> str:
    """Wedge (beide Kanten gleiche Richtung), Triangle (eine Kante flach) oder
    Pennant/Symmetric (Kanten laufen gegeneinander). Flachheits-Schwelle relativ
    zu ATR, damit die Klassifikation skalenunabhängig ist.
    """
    flat = 0.02 * atr  # Steigung < 2 % ATR/Kerze gilt als „flach"
    up_flat = abs(up_slope) < flat
    lo_flat = abs(lo_slope) < flat
    if up_flat or lo_flat:
        return "triangle"
    if (up_slope < 0) and (lo_slope > 0):
        return "pennant"  # symmetrisch konvergierend
    if np.sign(up_slope) == np.sign(lo_slope):
        return "wedge"
    return "pennant"


def detect_breakout(df_ind: pd.DataFrame, channel: dict, break_idx: int):
    """Prüft geschlossenen Ausbruch der Kerze ``break_idx`` aus dem Kanal.

    Rückgabe: ``{'direction', 'boundary_price', 'penetration'}`` oder ``None``.
    LONG = Close über der Oberkante, SHORT = Close unter der Unterkante.
    """
    close = float(df_ind["close"].iloc[break_idx])
    upper = channel["up_slope"] * break_idx + channel["up_int"]
    lower = channel["lo_slope"] * break_idx + channel["lo_int"]
    if close > upper:
        return {"direction": "LONG", "boundary_price": float(upper), "penetration": float(close - upper)}
    if close < lower:
        return {"direction": "SHORT", "boundary_price": float(lower), "penetration": float(lower - close)}
    return None


# --------------------------------------------------------------------------- #
# Feature-Builder (eine Quelle für Bot + Simulator + Trainer)                 #
# --------------------------------------------------------------------------- #
def build_atb2_features(df_ind: pd.DataFrame, channel: dict, breakout: dict, break_idx: int) -> dict:
    """Baut den ATB2_FEATURES-Vertrag als flaches dict für eine Ausbruchskerze.

    ``df_ind`` = ``compute_indicators``-Frame; ``break_idx`` zeigt auf die
    geschlossene Ausbruchskerze. Alle Werte sind skalenfrei.
    """
    row = df_ind.iloc[break_idx]
    o, hi, lo, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    atr = channel["atr"]
    rng = hi - lo
    is_long = breakout["direction"] == "LONG"

    vol_avg = float(row["vol_avg_20"]) if np.isfinite(row["vol_avg_20"]) and row["vol_avg_20"] > 0 else np.nan
    vol_spike = float(row["volume"]) / vol_avg if np.isfinite(vol_avg) else 0.0

    rsi_arr = df_ind["rsi"].to_numpy(dtype=float)
    rsi_now = rsi_arr[break_idx]
    rsi_prev = rsi_arr[break_idx - RSI_MOMENTUM_LOOKBACK] if break_idx >= RSI_MOMENTUM_LOOKBACK else np.nan
    rsi_momentum = float(rsi_now - rsi_prev) if np.isfinite(rsi_now) and np.isfinite(rsi_prev) else 0.0

    # rng>0-Guard VOR der Division — eine Flat-Candle (high==low, illiquide
    # Stunde) darf keinen 0/0-ZeroDivisionError werfen (bricht sonst den
    # Coin-Scan/Replay ab).
    if rng > 0:
        body_commitment = ((c - lo) / rng) if is_long else ((hi - c) / rng)
    else:
        body_commitment = 0.0

    ctype = channel["channel_type"]
    feats = {
        "pen_depth_atr": breakout["penetration"] / atr if atr > 0 else 0.0,
        "body_ratio": (abs(c - o) / rng) if rng > 0 else 0.0,
        "body_commitment": float(body_commitment),
        "vol_spike": float(vol_spike),
        "rsi_momentum": float(rsi_momentum),
        "chan_width_atr": channel["width_end"] / atr if atr > 0 else 0.0,
        "chan_convergence": channel["convergence"],
        "chan_touch_upper": float(channel["n_touch_upper"]),
        "chan_touch_lower": float(channel["n_touch_lower"]),
        "chan_slope_upper_atr": channel["up_slope"] / atr if atr > 0 else 0.0,
        "chan_slope_lower_atr": channel["lo_slope"] / atr if atr > 0 else 0.0,
        "chan_span": float(channel["span"]),
        "chan_vol_contraction": channel["vol_contraction"],
        "is_wedge": 1.0 if ctype == "wedge" else 0.0,
        "is_triangle": 1.0 if ctype == "triangle" else 0.0,
        "is_pennant": 1.0 if ctype == "pennant" else 0.0,
        "atr_pct": (atr / c) if c > 0 else 0.0,
        "dist_ema200": ((c - float(row["ema_200"])) / float(row["ema_200"]))
        if np.isfinite(row["ema_200"]) and row["ema_200"] > 0
        else 0.0,
        "rsi": float(rsi_now) if np.isfinite(rsi_now) else 50.0,
        "break_up": 1.0 if is_long else 0.0,
    }
    # inf/NaN härten (identisch in Bot und Trainer).
    return {k: (float(v) if np.isfinite(v) else 0.0) for k, v in feats.items()}


def measured_move_targets(channel: dict, breakout: dict, entry: float) -> dict:
    """§11-Kandidatengeometrie: Measured-Move-Targets (⅓/⅔/1× Kanalbreite) mit
    der gegenüberliegenden Kanalkante als SL (gecappt wie die Fleet-Smart-Targets:
    SL max. 15 % vom Entry). Rückgabe ist formkompatibel zu
    ``calculate_smart_targets`` (entry1/entry2/sl/targets).
    """
    width = channel["width_end"]
    is_long = breakout["direction"] == "LONG"
    opp = (
        channel["lo_slope"] * channel["span_end"] + channel["lo_int"]
        if is_long
        else channel["up_slope"] * channel["span_end"] + channel["up_int"]
    )
    if is_long:
        sl = max(min(opp, entry * 0.999), entry * 0.85)
        targets = [entry + width / 3.0, entry + 2.0 * width / 3.0, entry + width]
    else:
        sl = min(max(opp, entry * 1.001), entry * 1.15)
        targets = [entry - width / 3.0, entry - 2.0 * width / 3.0, entry - width]
    return {"entry1": float(entry), "entry2": float(entry), "sl": float(sl), "targets": [float(t) for t in targets]}


def find_channel_breakout(df_ind: pd.DataFrame, break_idx: int | None = None):
    """High-Level-Einstieg für Bot + Simulator: fittet den Kanal vor
    ``break_idx`` und prüft den geschlossenen Ausbruch DIESER Kerze.

    ``break_idx`` default = letzte (geschlossene) Kerze — der Aufrufer hat die
    Forming-Candle bereits abgeschnitten. Rückgabe: ein Setup-dict
    ``{direction, entry, features, channel, breakout}`` oder ``None``.
    """
    if break_idx is None:
        break_idx = len(df_ind) - 1
    channel = fit_channel(df_ind, break_idx)
    if channel is None:
        return None
    breakout = detect_breakout(df_ind, channel, break_idx)
    if breakout is None:
        return None
    feats = build_atb2_features(df_ind, channel, breakout, break_idx)
    return {
        "direction": breakout["direction"],
        "entry": float(df_ind["close"].iloc[break_idx]),
        "features": feats,
        "channel": channel,
        "breakout": breakout,
    }


def assert_features_alive(df_features: pd.DataFrame, context: str = "") -> None:
    """Startup-/Trainings-Assertion „kein Feature konstant" (P0.12-Muster).

    Fehlende Spalten → Hard-Error. Kontinuierliche Features müssen über die
    Stichprobe variieren; konstante Binär-Flags (Kanaltyp, break_up) sind über
    ein einzelnes Fenster legitim und werden nicht hart geprüft.
    """
    missing = [c for c in ATB2_FEATURES if c not in df_features.columns]
    if missing:
        raise ValueError(f"ATB2-Feature-Assertion{context}: Spalten fehlen: {missing}")
    continuous = [c for c in ATB2_FEATURES if c not in BINARY_FLAG_FEATURES]
    constant = [c for c in continuous if df_features[c].nunique(dropna=False) <= 1]
    if constant:
        raise ValueError(f"ATB2-Feature-Assertion{context}: konstante Features: {constant}")
