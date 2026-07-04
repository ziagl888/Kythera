import logging
import math

import numpy as np
import pandas as pd
import scipy.signal

logger = logging.getLogger(__name__)


def format_price(p) -> str:
    """P1.4: Preis mit signifikanten Stellen formatieren (statt festem :.6f).

    Warum: `:.6f` rundet Sub-0.001-Coins (z.B. 1000SATS, PEPE) auf identische
    Werte → alle TPs kollabieren auf denselben String → Cornix rejected das Signal.
    Regel: >=1 → 4 Nachkommastellen; >=0.001 → 6; darunter dynamisch ~6
    signifikante Stellen (führende Nullen bleiben erhalten, keine
    wissenschaftliche Notation, damit Cornix den Wert parsen kann).
    """
    try:
        v = float(p)
    except (TypeError, ValueError):
        return str(p)

    a = abs(v)
    if a == 0:
        return "0.00"
    if a >= 1:
        decimals = 4
    elif a >= 0.001:
        decimals = 6
    else:
        # ~6 signifikante Stellen: führende Nullen nach dem Komma mitzählen
        decimals = 5 - math.floor(math.log10(a))
    return f"{v:.{decimals}f}"


def ensure_min_tp_distance(targets: list, entry: float, is_long: bool, min_pct: float = 0.05) -> list:
    """Ensures the LAST target is at least `min_pct` (default 5%) vom
    Entry entfernt ist. Ist es näher, wird genau EIN zusätzliches Target an
    the 5% boundary — it is NOT padded to 20 targets.

    Vorher: Alle AI-Bots hatten `while len(targets) < 20: targets.append(last * 1.02)`
    → TP20 konnte +48% über Entry landen, bei Mean-Reversion-Bots völlig absurd.
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
    """Calculates the Average True Range (ATR) for dynamic SL/entry distances."""
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=period).mean().iloc[-1]


def cap_leverage_to_sl(desired_lev, entry, sl, safety=0.5):
    """R4 (Audit): Hebel so cappen, dass die Liquidation nie vor dem SL liegt.

    Akzeptiert int ODER den "20x"-String aus get_max_leverage() und gibt das
    Ergebnis im Eingabeformat zurück (String rein → "12x" raus), damit
    Call-Sites den Wert unverändert in die Cornix-Message formatieren.
    (Vorher warf int("20x") einen ValueError — auch im except-Handler —
    und riss die Signal-Pfade von 21/28/29 komplett ab.)

    Isolierte Liquidation liegt grob bei 1/lev Preisdistanz; mit lev <= safety/sl_dist
    liegt sie bei mindestens (1/safety)-facher SL-Distanz (safety=0.5 → Faktor 2).
    Beispiele der Bug-Klasse: 100x mit 1,2%-SL (P0.5) → Cap 41x;
    20x mit 34%-SL (P0.6) → Cap 1x.
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


def calculate_smart_targets(conn, symbol, direction, live_price):
    """
    Kombiniert den riesigen Pool an echten Leveln mit intelligentem Clustering,
    ATR-based minimum distance and hard SAFETY-CAPS against out-of-bounds values.
    """
    try:
        # Loading 1000 hours for proper swing-highs
        query = f'SELECT open, high, low, close, volume FROM "{symbol}_1h" ORDER BY open_time DESC LIMIT 1000'
        df = pd.read_sql_query(query, conn)

        if len(df) < 100:
            raise ValueError("Insufficient data")

        df = df.iloc[::-1].reset_index(drop=True)
        live_price = float(live_price)
        is_long = direction.upper() == "LONG"

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
        hvns = []
        try:
            df['price_bins'] = pd.cut(df['close'], bins=60)
            vol_profile = df.groupby('price_bins', observed=True)['volume'].sum()
            top_bins = vol_profile.nlargest(6)
            for interval in top_bins.index:
                hvns.append(interval.mid)
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
        fvgs = []
        opens = df['open'].values
        highs_arr = df['high'].values
        lows_arr = df['low'].values
        closes_arr = df['close'].values
        n = len(df)
        for i in range(2, n):
            mid_close = closes_arr[i - 1]
            mid_open = opens[i - 1]
            # Bullish FVG
            if highs_arr[i - 2] < lows_arr[i] and mid_close > mid_open:
                gap_bottom = float(highs_arr[i - 2])
                # Mitigation: was the gap-bottom undercut by a later candle?
                mitigated = False
                for j in range(i + 1, n):
                    if lows_arr[j] <= gap_bottom:
                        mitigated = True
                        break
                if not mitigated:
                    fvgs.append(gap_bottom)
            # Bearish FVG
            elif lows_arr[i - 2] > highs_arr[i] and mid_close < mid_open:
                gap_top = float(lows_arr[i - 2])
                mitigated = False
                for j in range(i + 1, n):
                    if highs_arr[j] >= gap_top:
                        mitigated = True
                        break
                if not mitigated:
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
            # FIX: Vorher Fallback ohne entry2-Check → bei hohem ATR konnte SL
            # ÜBER entry2 landen (LONG), was den Trade sofort in SL laufen ließ.
            # Jetzt: Fallback erzwingt SL < entry2 (mit kleinem Puffer 0.1%).
            if valid_sl:
                sl = max(valid_sl)
            else:
                sl = max((entry1 - min_sl_dist), max_sl_price)
                sl = min(sl, entry2 * 0.999)  # SL muss zwingend unter entry2 liegen

            # Targets (Ignoriere absurde Fib-Extensions > 200%)
            raw_targets = [x for x in clustered_levels if x >= (entry1 + min_tp1_dist) and x <= (entry1 * 3.0)]
            raw_targets.sort()
        else:
            # SHORT
            valid_e2 = [x for x in clustered_levels if x >= (entry1 + min_entry2_dist) and x <= max_e2_price]
            entry2 = min(valid_e2) if valid_e2 else min((entry1 + min_entry2_dist), max_e2_price)

            valid_sl = [x for x in clustered_levels if x >= (entry1 + min_sl_dist) and x > entry2 and x <= max_sl_price]
            # FIX: Gleicher Fix wie LONG (spiegelverkehrt) — Fallback muss
            # zwingend SL > entry2 liefern, sonst Trade geht direkt in SL.
            if valid_sl:
                sl = min(valid_sl)
            else:
                sl = min((entry1 + min_sl_dist), max_sl_price)
                sl = max(sl, entry2 * 1.001)  # SL muss zwingend über entry2 liegen

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
        logger.error(f"Error for Smart Targets für {symbol}: {e}", exc_info=True)
        e1 = float(live_price)
        is_long = direction.upper() == "LONG"
        return {
            "entry1": e1,
            "entry2": e1 * 0.96 if is_long else e1 * 1.04,
            "sl": e1 * 0.92 if is_long else e1 * 1.08,
            "targets": [e1 * 1.05] if is_long else [e1 * 0.95],
        }


def get_hvn_and_sr_levels(conn, symbol, live_price):
    """Fetches historical data and calculates levels for targets/SL.

    FIX (#52): Diese Funktion war 5× identisch kopiert in:
      - 9_ai_sr_bot.py
      - 10_pump_dump_detector.py
      - 12_ai_ats_bot.py
      - 13_ai_rub_bot.py
      - 14_ai_atb_bot.py
    Jetzt zentral hier. Kein Kopie mehr pflegen, kein Drift zwischen den Bots.

    Nutzt scipy.signal.argrelextrema auf 95 Tagen 1h-Kerzen + Fibonacci-Levels.
    Returns (supports, resistances): zwei sortierte Listen von Preis-Leveln.
    """
    try:
        # FIX P2.29: ORDER BY — ohne Sortierung liefert Postgres die Rows in
        # beliebiger Reihenfolge und argrelextrema findet Phantom-Extrema,
        # die als SL/TP-Preise in SRA1/ATS1/RUB1 landen.
        df = pd.read_sql_query(
            f'SELECT high, low, close FROM "{symbol}_1h" WHERE open_time >= NOW() - INTERVAL \'95 days\' ORDER BY open_time ASC',
            conn,
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
