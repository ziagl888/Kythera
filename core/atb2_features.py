# core/atb2_features.py
"""ATB2 — Converging-Channel Breakout: shared detection and feature logic.

ONE source for bot 14 (serving), ``tools/walkforward_sim.py`` (labeling) and
``tools/retrain_from_replay.py`` (training) — X-R1 rule, no train/serve skew.

Replaces the old single-trendline detector (90d close regression line,
ATB1, audit note D: "the model never saw the event it was scoring"). Redesign
per ``docs/MODEL_INTENT.md`` §11 (Michi, 2026-07-07): converging channels
(wedge/triangle/pennant) from CONFIRMED swing pivots, breakout on a
closed candle. The five WillyAlgoTrader factors
(penetration depth/ATR, body ratio, body commitment, volume spike,
RSI momentum) do NOT go in as a hand-weighted score, but as
setup features for the XGB gate — analogous to ``18_ai_abr1_bot.GEOMETRY_FEATURES``.

Contracts
---------
* **No-repaint:** only pivots with ``CONFIRM_BARS`` candles on BOTH sides; the
  breakout is only evaluated on a closed candle. The caller must
  strip the forming candle beforehand (R1).
* **Scale-free:** every feature is a percentage, an ATR multiple, an
  oscillator or a flag — never an absolute price (ticker leakage rule).
* **Self-contained indicators:** ATR/RSI/EMA are computed deterministically here
  from OHLCV (Wilder), no ``pandas_ta`` version dependency (P0.12),
  no DB indicator columns needed — bot, simulator and trainer compute identically.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Detector parameters (§11, objectively reproducible)                          #
# --------------------------------------------------------------------------- #
CONFIRM_BARS = 5  # pivot needs CONFIRM_BARS candles on both sides
MIN_TOUCHES = 3  # §11: minimum touches per channel boundary (3 instead of 2)
TOUCH_TOL_ATR = 0.15  # §11: touch tolerance 0.15 × ATR
CONVERGENCE_MIN = 0.02  # §11: narrowing ≥ 2 % over the channel window
WIDTH_MIN_ATR = 0.5  # §11: channel width 0.5 … 120 × ATR
WIDTH_MAX_ATR = 120.0
VOL_CONTRACTION_MAX = 0.85  # §11: in-channel volume < 85 % of the lead-in
CHANNEL_MAX_SPAN = 120  # longest considered consolidation window (candles)
CHANNEL_MIN_SPAN = 20  # shortest window in which a channel can be valid

ATR_PERIOD = 14
RSI_PERIOD = 14
EMA_PERIOD = 200
VOL_AVG_WINDOW = 20
RSI_MOMENTUM_LOOKBACK = 3  # candles for RSI delta

# Train/serve parity contract (X-R1): EMA200 is long-memory. Bot, simulator
# and trainer must, for the same timestamp BEFORE the decision candle,
# have loaded at least enough candles that the SMA seed is dampened out
# ((199/201)^1300 ≈ 2·10⁻⁶) — otherwise dist_ema200/atr_pct/rsi would drift
# apart depending on the loaded window length. The simulator sets start_t
# accordingly; the bot serving path MUST load ≥ this much history.
MIN_HISTORY_CANDLES = 1500

#: The ATB2 feature contract (column names == the artifact's meta.features).
ATB2_FEATURES = [
    # --- 5 WillyAlgoTrader setup factors (as features, not as a score) ---
    "pen_depth_atr",  # penetration depth of the breakout / ATR
    "body_ratio",  # |close-open| / (high-low) of the breakout candle
    "body_commitment",  # close position towards the breakout direction (0..1)
    "vol_spike",  # breakout volume / rolling 20-candle average
    "rsi_momentum",  # RSI[break] - RSI[break-3]
    # --- Channel geometry ---
    "chan_width_atr",  # channel width at the breakout / ATR
    "chan_convergence",  # relative narrowing over the window
    "chan_touch_upper",  # confirmed touches of the upper edge
    "chan_touch_lower",  # confirmed touches of the lower edge
    "chan_slope_upper_atr",  # slope of the upper edge (ATR/candle)
    "chan_slope_lower_atr",  # slope of the lower edge (ATR/candle)
    "chan_span",  # channel length in candles
    "chan_vol_contraction",  # in-channel volume / lead-in volume
    # --- Channel type (one-hot, may be constant) ---
    "is_wedge",
    "is_triangle",
    "is_pennant",
    # --- Context ---
    "atr_pct",  # ATR / close
    "dist_ema200",  # (close - EMA200) / EMA200
    "rsi",  # RSI[break]
    "break_up",  # 1 = breakout upward (LONG), 0 = downward
]

#: Binary flags may legitimately be constant over a single coin window and
#: are not hard-checked by the startup assertion (ABR/MIS pattern).
BINARY_FLAG_FEATURES = {"is_wedge", "is_triangle", "is_pennant", "break_up"}

REQUIRED_INPUT_COLS = ["open", "high", "low", "close", "volume"]


# --------------------------------------------------------------------------- #
# Deterministic indicators (Wilder) — DB-free, version-stable                 #
# --------------------------------------------------------------------------- #
def _wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Average True Range per Wilder. Returns the same length as the input."""
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
    """Relative Strength Index per Wilder (single-domain, T-097-compliant)."""
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
    """Exponential Moving Average, **SMA-seeded** (like ta-lib/pandas_ta).

    The first value sits at index ``period-1`` = SMA of the first ``period``
    closes; NaN before that. This removes the arbitrary ``close[0]`` anchoring —
    decisive for train/serve parity (X-R1): with enough warmup
    (`MIN_HISTORY_CANDLES`) the curve converges, so bot, simulator and
    trainer compute the same EMA200 for the same timestamp, no matter how long
    the respective loaded window is.
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
    """Adds ATR/RSI/EMA200/vol_avg_20 as columns (one source for everyone).

    Expects chronologically ascending OHLCV candles. Hard error on missing
    input columns (no silent ``fillna(0)`` — P0.12 lesson).
    """
    missing = [c for c in REQUIRED_INPUT_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"ATB2 compute_indicators: missing input columns: {missing}")
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
# Pivots (no-repaint) and channel fit                                        #
# --------------------------------------------------------------------------- #
def find_confirmed_pivots(high: np.ndarray, low: np.ndarray, confirm_bars: int = CONFIRM_BARS):
    """Confirmed swing pivots: extremum with ``confirm_bars`` candles on BOTH
    sides. The edge is hard-excluded — the last ``confirm_bars``
    candles are still unconfirmed and must not shape the channel (repaint,
    ABR-R07-b lesson).

    Returns: ``(highs, lows)`` — each a list of ``(index, price)``.
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
    """Least-squares line through (index, price). Returns (slope, intercept)."""
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    A = np.vstack([xs, np.ones(len(xs))]).T
    slope, intercept = np.linalg.lstsq(A, ys, rcond=None)[0]
    return float(slope), float(intercept)


def _count_touches(points, slope, intercept, tol) -> int:
    """How many pivots lie within ``tol`` (price) of the line?"""
    return sum(1 for idx, price in points if abs(price - (slope * idx + intercept)) <= tol)


def fit_channel(df_ind: pd.DataFrame, break_idx: int):
    """Fits a converging channel in the window BEFORE ``break_idx``.

    ``df_ind`` must have gone through ``compute_indicators`` (column ``atr``).
    The consolidation window ends at ``break_idx - 1`` (the breakout candle
    itself does not belong in the fit) and spans at most ``CHANNEL_MAX_SPAN``
    candles. Validates §11 criteria: ≥3 confirmed touches per edge,
    convergence ≥2 %, width 0.5…120×ATR, volume contraction <85 %.

    Returns: a ``channel`` dict or ``None``.
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

    # Pivots relative to the window start; fit in the index space of the full df.
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

    # Channel span = from the first used pivot candle to the candle before the break.
    span_start = min(highs[0][0], lows[0][0])
    span_end = break_idx - 1
    span = span_end - span_start
    if span < CHANNEL_MIN_SPAN:
        return None

    width_start = (up_slope * span_start + up_int) - (lo_slope * span_start + lo_int)
    width_end = (up_slope * span_end + up_int) - (lo_slope * span_end + lo_int)
    # Upper edge must be above the lower edge; otherwise it's not a channel.
    if width_start <= 0 or width_end <= 0:
        return None
    convergence = (width_start - width_end) / width_start
    if convergence < CONVERGENCE_MIN:
        return None
    width_atr = width_end / atr_end
    if not (WIDTH_MIN_ATR <= width_atr <= WIDTH_MAX_ATR):
        return None

    # Volume contraction: in-channel volume vs. an equally long lead-in.
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
    """Wedge (both edges same direction), triangle (one edge flat) or
    pennant/symmetric (edges run against each other). Flatness threshold relative
    to ATR, so the classification is scale-independent.
    """
    flat = 0.02 * atr  # slope < 2 % ATR/candle counts as "flat"
    up_flat = abs(up_slope) < flat
    lo_flat = abs(lo_slope) < flat
    if up_flat or lo_flat:
        return "triangle"
    if (up_slope < 0) and (lo_slope > 0):
        return "pennant"  # symmetrically converging
    if np.sign(up_slope) == np.sign(lo_slope):
        return "wedge"
    return "pennant"


def detect_breakout(df_ind: pd.DataFrame, channel: dict, break_idx: int):
    """Checks a closed breakout of candle ``break_idx`` out of the channel.

    Returns: ``{'direction', 'boundary_price', 'penetration'}`` or ``None``.
    LONG = close above the upper edge, SHORT = close below the lower edge.
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
# Feature builder (one source for bot + simulator + trainer)                  #
# --------------------------------------------------------------------------- #
def build_atb2_features(df_ind: pd.DataFrame, channel: dict, breakout: dict, break_idx: int) -> dict:
    """Builds the ATB2_FEATURES contract as a flat dict for one breakout candle.

    ``df_ind`` = the ``compute_indicators`` frame; ``break_idx`` points to the
    closed breakout candle. All values are scale-free.
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

    # rng>0 guard BEFORE the division — a flat candle (high==low, illiquid
    # hour) must not throw a 0/0 ZeroDivisionError (would otherwise abort the
    # coin scan/replay).
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
    # Harden inf/NaN (identical in bot and trainer).
    return {k: (float(v) if np.isfinite(v) else 0.0) for k, v in feats.items()}


def measured_move_targets(channel: dict, breakout: dict, entry: float) -> dict:
    """§11 candidate geometry: measured-move targets (⅓/⅔/1× channel width) with
    the opposite channel edge as SL (capped like the fleet smart targets:
    SL max. 15 % from entry). Return value is shape-compatible with
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
    """High-level entry point for bot + simulator: fits the channel before
    ``break_idx`` and checks the closed breakout of THIS candle.

    ``break_idx`` default = last (closed) candle — the caller has already
    stripped the forming candle. Returns: a setup dict
    ``{direction, entry, features, channel, breakout}`` or ``None``.
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
    """Startup/training assertion "no feature constant" (P0.12 pattern).

    Missing columns → hard error. Continuous features must vary across the
    sample; constant binary flags (channel type, break_up) are legitimate over
    a single window and are not hard-checked.
    """
    missing = [c for c in ATB2_FEATURES if c not in df_features.columns]
    if missing:
        raise ValueError(f"ATB2 feature assertion{context}: missing columns: {missing}")
    continuous = [c for c in ATB2_FEATURES if c not in BINARY_FLAG_FEATURES]
    constant = [c for c in continuous if df_features[c].nunique(dropna=False) <= 1]
    if constant:
        raise ValueError(f"ATB2 feature assertion{context}: constant features: {constant}")
