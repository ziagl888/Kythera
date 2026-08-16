import datetime
import logging
import math

import numpy as np
import pandas as pd
import scipy.signal

from core.candles import read_candles
from core.time import utc_now

logger = logging.getLogger(__name__)


def format_price(p) -> str:
    """P1.4: format the price with significant digits (instead of fixed :.6f).

    Why: `:.6f` rounds sub-0.001 coins (e.g. 1000SATS, PEPE) to identical
    values → all TPs collapse to the same string → Cornix rejects the signal.
    Rule: >=1 → 4 decimal places; >=0.001 → 6; below that dynamically ~6
    significant digits (leading zeros are preserved, no scientific
    notation, so Cornix can parse the value).
    """
    try:
        v = float(p)
    except (TypeError, ValueError):
        return str(p)

    # Review hardening P1.4: NaN/Inf pass the float() cast, but would crash
    # below in math.log10 or in the f-format — return defensively as a string
    # instead of killing the message builder.
    if not math.isfinite(v):
        return str(p)

    a = abs(v)
    if a == 0:
        return "0.00"
    if a >= 1:
        decimals = 4
    elif a >= 0.001:
        decimals = 6
    else:
        # ~6 significant digits: count leading zeros after the decimal point
        decimals = 5 - math.floor(math.log10(a))
    return f"{v:.{decimals}f}"


#: Minimum gap between two PUBLISHED targets, as a percentage of entry
#: (T-2026-KYT-9050-098, operator decision Michi 2026-08-04). One source for
#: every emitter that builds its own geometry — a per-bot literal is how the
#: entry-side 1 % filter ended up applied on one hop and not the other.
MIN_TP_GAP_PCT = 1.0

#: How many targets the own-geometry emitters publish (their ``n_show``). Named
#: here rather than repeated as a literal at each call site: it is the number the
#: thinning has to reach, and a site that drifts from its own ``n_show`` would
#: either thin too little (tight ladder survives) or too much (a target vanishes).
N_PUBLISHED_TARGETS = 3


def thin_targets(
    candidates: list,
    entry: float,
    is_long: bool,
    keep: int,
    min_gap_pct: float = MIN_TP_GAP_PCT,
) -> list:
    """Drop targets that sit closer than ``min_gap_pct`` to the one before them.

    Why this exists
    ---------------
    ``hvn_sr_trade_geometry`` filters its candidates against the ENTRY only
    (``x > entry1 * 1.01``) — the first hop is guaranteed 1 %, every hop after it
    is whatever the raw S/R level list happens to give. Measured on
    ``closed_ai_signals`` since the P2.31 fix (2026-08-04): on those legs 54 %
    (EPD3) to 69 % (MAX1) of neighbouring gaps are under 1 %, and in 23–34 % of
    signals the WHOLE published TP1..TP3 ladder spans less than 1 % — three
    Cornix tranches inside one tick of movement, hit together in 16–24 % of
    trades. ``calculate_smart_targets`` never had this problem because it thins by
    ``1 x ATR``; this is the same idea as a relative floor for the legs that do
    not go through it.

    Thinning applies regardless of pool depth (T-2026-KYT-9050-147)
    ---------------------------------------------------------------
    Until T-147 a ``len(candidates) <= keep`` guard skipped thinning whenever
    the pool was not deeper than the ladder — the T-098 operator condition
    "only skip when not everything gets published" protected the
    ``n_show=len(targets)`` emitters from deleting a target without a
    replacement. Measured on the live book (2026-08-16) that guard let dense
    ladders through on BOTH sides: pool-of-3 signals on the gated legs
    (EPD3/TSM1) and every ladder-publishing leg, with published TP gaps
    down to 0.08 %. Operator decision Michi 2026-08-16 (supersedes the T-098
    condition): a too-close target is DROPPED even without a replacement —
    two real TPs beat three clustered ones. The ladder emitters that do not
    call this function (bots 7/18/24/25) keep their raw ladders for now
    (T-147 scope decision); bot 10's EPD2 legacy leg routes through here
    since it went Cornix-live (T-143).

    Gaps are measured against the PREVIOUS KEPT target, not the previous
    candidate — otherwise a run of near-identical levels would each be measured
    against its neighbour, pass individually, and still cluster.

    Returns fewer than ``keep`` entries when the pool genuinely has no further
    separated levels. That is the honest outcome and it is not padded here:
    ``ensure_min_tp_distance`` runs afterwards and supplies the 5 % backstop.
    """
    if keep <= 0 or min_gap_pct <= 0 or entry <= 0:
        return list(candidates)

    def dist(t: float) -> float:
        return ((t - entry) / entry * 100.0) if is_long else ((entry - t) / entry * 100.0)

    kept: list = []
    last_d: float | None = None
    for t in candidates:
        d = dist(float(t))
        if last_d is not None and d - last_d < min_gap_pct:
            continue
        kept.append(t)
        last_d = d
        if len(kept) >= keep:
            break
    # A pool that yields nothing separated at all still has to produce a signal:
    # falling back to the untouched head is strictly closer to today's behaviour
    # than returning empty and letting the caller bail out of the trade.
    return kept if kept else list(candidates[:keep])


def ensure_min_tp_distance(targets: list, entry: float, is_long: bool, min_pct: float = 0.05) -> list:
    """Ensures the LAST target is at least `min_pct` (default 5%) away from
    entry. If it is closer, exactly ONE additional target is appended at
    the 5% boundary — it is NOT padded to 20 targets.

    Before: all AI bots had `while len(targets) < 20: targets.append(last * 1.02)`
    → TP20 could land +48% above entry, utterly absurd for mean-reversion bots.
    """
    if not targets:
        # No real zones → single 5% target
        if is_long:
            return [float(entry * (1 + min_pct))]
        else:
            return [float(entry * (1 - min_pct))]

    targets = [float(t) for t in targets]
    last = targets[-1]
    if is_long:
        dist_pct = (last - entry) / entry
        if dist_pct < min_pct:
            targets.append(float(entry * (1 + min_pct)))
    else:
        dist_pct = (entry - last) / entry
        if dist_pct < min_pct:
            targets.append(float(entry * (1 - min_pct)))
    return targets


def get_atr(df, period=14):
    """Calculates the Average True Range (ATR) for dynamic SL/entry distances.

    PERF (MIS1 retrain 2026-07): numpy instead of pd.concat+rolling — the
    walk-forward simulator calls this hundreds of thousands of times. Semantics
    unchanged: the old np.max(DataFrame, axis=1) ran with skipna → in the first
    row (prev_close=NaN) only high-low counted; fmax replicates exactly that.
    Result is NaN as long as fewer than `period` candles are available (like rolling.mean).
    """
    h = df['high'].to_numpy(dtype=float)
    l = df['low'].to_numpy(dtype=float)  # noqa: E741
    c = df['close'].to_numpy(dtype=float)
    if len(h) < period:
        return np.nan
    prev_c = np.empty_like(c)
    prev_c[0] = np.nan
    prev_c[1:] = c[:-1]
    tr = np.fmax(h - l, np.fmax(np.abs(h - prev_c), np.abs(l - prev_c)))
    return float(np.mean(tr[-period:]))


def cap_leverage_to_sl(desired_lev, entry, sl, safety=0.5):
    """R4 (audit): cap leverage so that liquidation never occurs before the SL.

    Accepts int OR the "20x" string from get_max_leverage() and returns the
    result in the input format (string in → "12x" out), so call sites
    format the value unchanged into the Cornix message.
    (Before: int("20x") raised a ValueError — even in the except handler —
    and completely broke the signal paths of 21/28/29.)

    Isolated liquidation sits roughly at 1/lev price distance; with lev <= safety/sl_dist
    it sits at least (1/safety) times the SL distance (safety=0.5 → factor 2).
    Examples of the bug class: 100x with a 1.2% SL (P0.5) → cap 41x;
    20x with a 34% SL (P0.6) → cap 1x.
    """
    as_string = isinstance(desired_lev, str)
    try:
        lev = max(1, int(str(desired_lev).strip().lower().rstrip("x")))
    except (TypeError, ValueError):
        lev = 1
    try:
        if entry and sl and float(entry) > 0:
            sl_dist = abs(float(entry) - float(sl)) / float(entry)
            if sl_dist > 0:
                lev = max(1, min(lev, int(safety / sl_dist)))
    except (TypeError, ValueError):
        pass
    return f"{lev}x" if as_string else lev


def _cut_label_edges(bin_edges) -> np.ndarray:
    """Bit-identical reproduction of pandas' label rounding from
    pandas.core.reshape.tile._format_labels (precision = _infer_precision(3,…),
    then _round_frac per edge) — vectorised because the walk-forward simulator
    calls it hundreds of thousands of times. Formula per value x!=0: for |x|<1,
    round to (precision − floor(log10(|frac|)) − 1) decimals, otherwise to
    `precision`; precision is the smallest p ∈ [3,20) at which all rounded
    edges stay unique (fallback p=3, like pandas)."""
    x = np.asarray(bin_edges, dtype=float)

    def round_frac_vec(v: np.ndarray, precision: int) -> np.ndarray:
        out = v.copy()
        m = np.isfinite(v) & (v != 0)
        vm = v[m]
        frac, whole = np.modf(vm)
        digits = np.full(vm.shape, precision, dtype=np.int64)
        wz = whole == 0  # (frac==0 AND whole==0 would mean v==0 — filtered out above)
        digits[wz] = precision - np.floor(np.log10(np.abs(frac[wz]))).astype(np.int64) - 1
        r = np.empty_like(vm)
        for d in np.unique(digits):
            sel = digits == d
            r[sel] = np.around(vm[sel], int(d))
        out[m] = r
        return out

    for precision in range(3, 20):
        rounded = round_frac_vec(x, precision)
        if np.unique(rounded).size == x.size:
            return rounded
    return round_frac_vec(x, 3)


def compute_smart_target_levels(df, live_price) -> dict:
    """Direction-independent part of calculate_smart_targets: ATR + the
    clustered level pool (S/R, fibs, HVNs, FVGs).

    Extracted (MIS1 retrain 2026-07) so the walk-forward simulator computes
    the pool ONCE per point in time and reuses it for LONG and SHORT —
    live, the bots still compute it implicitly per call as before. Raises for <100
    candles; exception handling (live fallback) stays with the caller
    calculate_smart_targets, exactly as before."""
    if len(df) < 100:
        raise ValueError("Insufficient data")

    df = df.reset_index(drop=True)
    live_price = float(live_price)

    atr = get_atr(df, 14)
    if pd.isna(atr) or atr == 0:
        atr = live_price * 0.02

    # 💥 SAFETY CAP 1: ATR limit
    # Prevents a flash crash from ruining the ATR. Max ATR = 4% of live price.
    atr = min(atr, live_price * 0.04)

    highs, lows = df['high'].values, df['low'].values

    # 🟢 1. SUPPORT & RESISTANCE
    max_idx = scipy.signal.argrelextrema(highs, np.greater, order=20)[0]
    min_idx = scipy.signal.argrelextrema(lows, np.less, order=20)[0]
    resistances = [highs[i] for i in max_idx]
    supports = [lows[i] for i in min_idx]

    # 🟢 2. FIBONACCI
    swing_high = np.max(highs[-300:])
    swing_low = np.min(lows[-300:])
    fib_range = swing_high - swing_low

    fibs = []
    if fib_range > 0:
        for x in [0.236, 0.382, 0.5, 0.618, 0.786]:
            fibs.append(swing_high - fib_range * x)
        for x in [1.272, 1.618, 2.0, 2.618]:
            fibs.append(swing_low + fib_range * x)
        for x in [1.272, 1.618, 2.0, 2.618]:
            fibs.append(swing_high - fib_range * (x - 1))

    # 🟢 3. HIGH VOLUME NODES & FVGs
    # PERF (MIS1 retrain 2026-07): pd.cut(labels=False)+bincount instead of
    # interval labels+groupby — label formatting dominated the runtime of
    # the whole function. The mids are built from the ROUNDED
    # edges (exactly pandas' label rounding via _round_frac/
    # _infer_precision) — bit-identical to the old interval.mid; on
    # pandas-internals changes it falls back to the unrounded edges
    # (deviation ~display precision, under the 0.5% cluster grid).
    hvns = []
    try:
        bin_ids, bin_edges = pd.cut(df['close'], bins=60, labels=False, retbins=True)
        edges = _cut_label_edges(bin_edges)
        ids = bin_ids.to_numpy(dtype=np.int64)
        vols = np.bincount(ids, weights=df['volume'].to_numpy(dtype=float), minlength=60)
        occupied = np.bincount(ids, minlength=60) > 0  # like groupby(observed=True)
        # nlargest semantics: descending by volume, ties in bin order
        top = sorted(np.flatnonzero(occupied).tolist(), key=lambda b: (-vols[b], b))[:6]
        for b in top:
            hvns.append((edges[b] + edges[b + 1]) / 2.0)
    except Exception:
        pass

    # 🟢 4. FAIR VALUE GAPS (FVG)
    # A FVG is a 3-candle formation where the middle candle (i-1)
    # creates a gap between candle i-2 and candle i:
    #   Bullish FVG: high[i-2] < low[i]  AND middle candle is bullish (close > open)
    #   Bearish FVG: low[i-2] > high[i]  AND middle candle is bearish (close < open)
    #
    # The gap boundary used as level is the one that normally acts
    # as support/resistance at retest (not the gap midpoint — that price
    # does not exist on the chart as a real level).
    # Additionally: mitigation check — FVGs that have already been traded through
    # are no longer active levels.
    # PERF (MIS1 retrain 2026-07): detection + mitigation vectorised.
    # The old O(n²) mitigation scan (for every FVG, all following candles) is
    # semantically exactly "suffix-min(lows) <= gap_bottom" resp.
    # "suffix-max(highs) >= gap_top" — here as accumulate in O(n).
    # Bull/bear are constructively mutually exclusive per candle (low<=high),
    # so the elif of the old loop is covered by this.
    fvgs = []
    opens = df['open'].values
    highs_arr = df['high'].values
    lows_arr = df['low'].values
    closes_arr = df['close'].values
    n = len(df)
    if n >= 3:
        suffix_min_low = np.minimum.accumulate(lows_arr[::-1])[::-1]
        suffix_max_high = np.maximum.accumulate(highs_arr[::-1])[::-1]
        idx = np.arange(2, n)
        mid_bull = closes_arr[idx - 1] > opens[idx - 1]
        mid_bear = closes_arr[idx - 1] < opens[idx - 1]

        bull = (highs_arr[idx - 2] < lows_arr[idx]) & mid_bull
        bear = (lows_arr[idx - 2] > highs_arr[idx]) & mid_bear

        # Mitigation from candle i+1 onward; the last candle (i == n-1) has no
        # follow-up scan → never mitigated (like the old empty range).
        for i in idx[bull]:
            gap_bottom = float(highs_arr[i - 2])
            if i + 1 >= n or suffix_min_low[i + 1] > gap_bottom:
                fvgs.append(gap_bottom)
        for i in idx[bear]:
            gap_top = float(lows_arr[i - 2])
            if i + 1 >= n or suffix_max_high[i + 1] < gap_top:
                fvgs.append(gap_top)

    # 🔄 CLUSTERING
    all_levels = supports + resistances + fibs + hvns + fvgs
    all_levels = [float(lvl) for lvl in all_levels if lvl > 0]
    all_levels.sort()

    clustered_levels = []
    if all_levels:
        current_cluster = [all_levels[0]]
        for lvl in all_levels[1:]:
            if (lvl - current_cluster[-1]) / current_cluster[-1] < 0.005:
                current_cluster.append(lvl)
            else:
                clustered_levels.append(sum(current_cluster) / len(current_cluster))
                current_cluster = [lvl]
        clustered_levels.append(sum(current_cluster) / len(current_cluster))

    return {"atr": atr, "clustered_levels": clustered_levels}


def calculate_smart_targets(conn, symbol, direction, live_price, df=None, levels=None):
    """
    Combines the huge pool of real levels with intelligent clustering,
    ATR-based minimum distance and hard SAFETY-CAPS against out-of-bounds values.

    `df` (optional): a ready, chronologically ascending 1h window with
    open/high/low/close/volume. If passed, NO DB access happens —
    this lets the walk-forward simulator (tools/walkforward_sim.py, P0.10) replay
    exactly the same level/SL/target logic on historical windows as the
    live bots (one source instead of copy-paste skew). The error fallback at
    the end is deliberately identical too — the simulator is meant to measure live behaviour.

    `levels` (optional): a precomputed pool from compute_smart_target_levels —
    the simulator passes it in so as not to compute LONG and SHORT of the same
    point in time twice. Live callers omit it (behaviour unchanged).
    """
    try:
        if levels is None:
            if df is None:
                # Loading 1000 hours for proper swing-highs. R1 (T-2026-CU-9050-108):
                # read through core.candles with closed candles only — the forming 1h
                # candle no longer feeds the Swing/HVN/FVG/S-R/Fib level pool. The API
                # returns ASC already, so the old DESC-then-reverse drops out.
                df = read_candles(
                    conn,
                    symbol,
                    "1h",
                    limit=1000,
                    include_forming=False,
                    columns=("open_time", "open", "high", "low", "close", "volume"),
                )
            levels = compute_smart_target_levels(df, live_price)

        atr = levels["atr"]
        clustered_levels = levels["clustered_levels"]
        live_price = float(live_price)
        is_long = direction.upper() == "LONG"

        # 🎯 ENTRY, SL & TARGET DISTRIBUTION
        entry1 = live_price

        min_entry2_dist = atr * 1.5
        min_sl_dist = atr * 3.0
        min_tp1_dist = atr * 2.0
        min_tp_spacing = atr * 1.0

        # 💥 SAFETY CAP 2: Hard limits for Stop Loss & Entry 2
        # SL may be at most 15% from entry. Entry 2 at most 10%.
        max_sl_price = entry1 * 0.85 if is_long else entry1 * 1.15
        max_e2_price = entry1 * 0.90 if is_long else entry1 * 1.10

        if is_long:
            # Entry 2
            valid_e2 = [x for x in clustered_levels if x <= (entry1 - min_entry2_dist) and x >= max_e2_price]
            entry2 = max(valid_e2) if valid_e2 else max((entry1 - min_entry2_dist), max_e2_price)

            # Stop Loss (must be below Entry2 but above max_sl_price)
            valid_sl = [x for x in clustered_levels if x <= (entry1 - min_sl_dist) and x < entry2 and x >= max_sl_price]
            # FIX: previously the fallback had no entry2 check → with a high ATR the SL
            # could land ABOVE entry2 (LONG), which sent the trade straight into SL.
            # Now: the fallback forces SL < entry2 (with a small 0.1% buffer).
            if valid_sl:
                sl = max(valid_sl)
            else:
                sl = max((entry1 - min_sl_dist), max_sl_price)
                sl = min(sl, entry2 * 0.999)  # SL must strictly stay below entry2

            # Targets (ignore absurd fib extensions > 200%)
            raw_targets = [x for x in clustered_levels if x >= (entry1 + min_tp1_dist) and x <= (entry1 * 3.0)]
            raw_targets.sort()
        else:
            # SHORT
            valid_e2 = [x for x in clustered_levels if x >= (entry1 + min_entry2_dist) and x <= max_e2_price]
            entry2 = min(valid_e2) if valid_e2 else min((entry1 + min_entry2_dist), max_e2_price)

            valid_sl = [x for x in clustered_levels if x >= (entry1 + min_sl_dist) and x > entry2 and x <= max_sl_price]
            # FIX: same fix as LONG (mirrored) — the fallback must
            # strictly deliver SL > entry2, otherwise the trade goes straight into SL.
            if valid_sl:
                sl = min(valid_sl)
            else:
                sl = min((entry1 + min_sl_dist), max_sl_price)
                sl = max(sl, entry2 * 1.001)  # SL must strictly stay above entry2

            raw_targets = [x for x in clustered_levels if x <= (entry1 - min_tp1_dist) and x >= (entry1 * 0.1)]
            raw_targets.sort(reverse=True)

        # 🧹 Filter: maintain minimum spacing between targets
        final_targets = []
        last_t = entry1

        for t in raw_targets:
            if abs(t - last_t) >= min_tp_spacing:
                final_targets.append(t)
                last_t = t

            if len(final_targets) >= 10:
                break

        if not final_targets:
            fallback_tp = entry1 + (atr * 3.0) if is_long else entry1 - (atr * 3.0)
            final_targets = [fallback_tp]

        return {
            "entry1": float(entry1),
            "entry2": float(entry2),
            "sl": float(sl),
            "targets": [float(t) for t in final_targets],
        }

    except Exception as e:
        logger.error(f"Error for Smart Targets for {symbol}: {e}", exc_info=True)
        e1 = float(live_price)
        is_long = direction.upper() == "LONG"
        return {
            "entry1": e1,
            "entry2": e1 * 0.96 if is_long else e1 * 1.04,
            "sl": e1 * 0.92 if is_long else e1 * 1.08,
            "targets": [e1 * 1.05] if is_long else [e1 * 0.95],
        }


def get_hvn_and_sr_levels(conn, symbol, live_price, df=None):
    """Fetches historical data and calculates levels for targets/SL.

    FIX (#52): this function was copied identically 5× into:
      - 9_ai_sr_bot.py
      - 10_pump_dump_detector.py
      - 12_ai_ats_bot.py
      - 13_ai_rub_bot.py
      - 14_ai_atb_bot.py
    Now centralised here. No more copies to maintain, no drift between the bots.

    `df` (optional): a ready, chronologically ascending 1h window with
    high/low/close (ideally ~95 days). If passed, NO
    DB access happens — the same as-of pattern as `calculate_smart_targets`
    (P0.10): this lets the walk-forward adapters (RUB2/EPD2) replay exactly the
    same level logic on historical windows as the live bots.

    Uses scipy.signal.argrelextrema on 95 days of 1h candles + fibonacci levels.
    Returns (supports, resistances): two sorted lists of price levels.
    """
    if df is None:
        try:
            # FIX P2.29: the rows must be sorted (argrelextrema depends on row order,
            # else phantom extrema leak into SRA1/ATS1/RUB1 SL/TP) — core.candles
            # guarantees ASC. R1 (T-2026-CU-9050-108): closed candles only, so the
            # forming 1h candle no longer feeds the 95d S/R + Fib pool. `start`
            # reproduces the old `NOW() - INTERVAL '95 days'` as the instant 95 days
            # ago (the ≤1h DST nuance is immaterial for this warm-up lower bound).
            start = utc_now() - datetime.timedelta(days=95)
            df = read_candles(
                conn,
                symbol,
                "1h",
                start=start,
                include_forming=False,
                columns=("open_time", "high", "low", "close"),
            )
        except Exception:
            return [], []

    if df.empty or len(df) < 50:
        return [], []

    highs, lows = df['high'].values, df['low'].values
    max_idx = scipy.signal.argrelextrema(highs, np.greater, order=20)[0]
    min_idx = scipy.signal.argrelextrema(lows, np.less, order=20)[0]
    resistances = sorted([highs[i] for i in max_idx])
    supports = sorted([lows[i] for i in min_idx], reverse=True)

    swing_high, swing_low = df['high'].max(), df['low'].min()
    fib_range = swing_high - swing_low
    fib_ret = [swing_high - fib_range * x for x in [0.236, 0.382, 0.5, 0.618, 0.786]]
    fib_ext = [swing_low + fib_range * x for x in [1.272, 1.618, 2.0, 2.618]]

    return supports + fib_ret, resistances + fib_ext


def hvn_sr_trade_geometry(entry1, is_long, supps, resis):
    """Entry2/SL/target candidates from HVN/SR levels — the geometry that
    bot 10 (EPD) and bot 13 (RUB) compute identically inline (their copies
    remain for now; this function is the referenced ONE source for the
    walk-forward adapters, so replay geometry == live geometry).

    Returns (entry2, sl, target_candidates) — the candidates are then run through
    ``thin_targets(t_cands[:20], entry1, is_long, keep=N_PUBLISHED_TARGETS)`` and
    ``ensure_min_tp_distance(..., min_pct=0.05)``, in that order.

    Note what this function does and does NOT guarantee about spacing: the filters
    above (``> entry1 * 1.01``) put the FIRST candidate at least 1 % from entry, and
    nothing here separates the candidates from EACH OTHER — they are the raw level
    list. That gap is why ``thin_targets`` exists (T-2026-KYT-9050-098); a caller
    that publishes a slice of these candidates without thinning them republishes the
    clustering.
    """
    entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
    if is_long:
        sl = max([x for x in supps if x < entry2 * 0.99]) if any(x < entry2 * 0.99 for x in supps) else entry2 * 0.975
        t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
    else:
        sl = min([x for x in resis if x > entry2 * 1.01]) if any(x > entry2 * 1.01 for x in resis) else entry2 * 1.025
        t_cands = sorted([x for x in supps if x > 0 and x < (entry1 * 0.99)], reverse=True)
    return entry2, sl, t_cands
